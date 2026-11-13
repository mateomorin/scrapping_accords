"""
Step 2b - Refresh the structure of already-extracted documents (NO LLM, NO
figure re-upload). Faster alternative to `step2 --force` after a change to
structure detection (core/structure.py) or to text normalization (core/utils.py
normalize_ws / core/doc_process.py).

Why a dedicated step instead of `step2 --force`?
  `step2 --force` re-downloads AND re-uploads every figure kept by the triage
  (thousands of S3 PUTs). The figure set is DETERMINISTIC (triage depends only
  on the raw bytes + triage heuristics, both unchanged), so the figures already
  on S3 are still valid. Step 2b therefore:
    - re-downloads the raw bytes (required: the per-paragraph features used by
      the scorer -- bold_frac, caps, ... -- are NOT stored in doc_json),
    - re-runs the deterministic extraction + structure recovery,
    - does NOT re-upload figures: it restores each kept figure's `fig_s3` at
      the deterministic path `{figs_base}{cid}/{fid}.{ext}` (already present on
      S3 from the original step 2 run),
    - rewrites every shard.

Because figure ids are unchanged, data/ocr_results.jsonl stays valid and step 3
needs no re-run for figures that were already OCR'd.

IMPORTANT: do not run step2b concurrently with an active step 3 OCR run (the
shard parquet files are rewritten while step 3 may be reading them).

Resume: a per-shard sidecar (data/shard_XXXXX.refreshed) records that a shard
was refreshed with the expected row count. With --skip-complete, completed
shards are skipped (useful after a crash). By default every shard is rewritten.

Run:
    uv run src/data_conversion/heures_supp/steps/step2b_refresh_structure.py
    uv run src/data_conversion/heures_supp/steps/step2b_refresh_structure.py --skip-complete
    uv run src/data_conversion/heures_supp/steps/step2b_refresh_structure.py --limit 30
    uv run src/data_conversion/heures_supp/steps/step2b_refresh_structure.py --workers 8
"""
import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from tqdm import tqdm

# Make the heures_supp package importable when run as a script
# (package root: src/data_conversion; no package install needed).
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
sys.path.insert(0, HERE)  # so `from step2_extract import ...` resolves

from heures_supp.core import config, utils  # noqa: E402
from heures_supp.core.doc_process import process_document_bytes, record_to_json  # noqa: E402
from step2_extract import shard_output_path  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("s3fs").setLevel(logging.WARNING)

_OCRABLE_EXT = ("png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp")


def _sidecar_path(shard_idx: int) -> str:
    return f"{config.DATA_PATH}shard_{shard_idx:05d}.refreshed"


def _sidecar_complete(shard_idx: int, expected: int) -> bool:
    try:
        with config.fs.open(_sidecar_path(shard_idx), "rb") as f:
            meta = json.loads(f.read().decode("utf-8"))
        return meta.get("n_rows") == expected
    except Exception:
        return False


def _write_sidecar(shard_idx: int, n_rows: int) -> None:
    payload = json.dumps({"n_rows": n_rows, "ts": time.time()}).encode("utf-8")
    if not utils.put_bytes(_sidecar_path(shard_idx), payload):
        logger.warning(
            f"Could not write sidecar for shard {shard_idx} (it will be redone on resume)."
        )


def process_one(row, figs_base: str) -> dict:
    """Re-extract one document (no figure upload) and return the record."""
    cid = row["cid"]
    data = utils.download_bytes(row["docx_path"])
    if data is None:
        return {
            "cid": cid, "reference": row.get("reference"),
            "dateSignature": str(row.get("dateSignature")),
            "doc_type": "missing", "format": "missing",
            "n_sections": 0, "n_tables": 0, "n_figures": 0,
            "n_ocr_kept": 0, "n_ocr_dropped": 0,
            "error": "file missing or download failed",
            "doc_json": record_to_json({"cid": cid, "errors": ["missing file"]}),
        }
    try:
        # upload_figure=None -> no S3 PUT. Triage still runs (deterministic) so
        # n_ocr_kept/dropped are identical to the original step 2 run.
        rec = process_document_bytes(
            data, cid, row.get("reference"), upload_figure=None, figs_base=None,
        )
        # Restore fig_s3 for the kept figures at the deterministic path. The
        # files already exist on S3 from the original step 2 run.
        for f in rec.get("figures", []):
            if f.get("triage", {}).get("keep"):
                ext = f["fmt"] if f["fmt"] in _OCRABLE_EXT else "bin"
                f["fig_s3"] = f"{figs_base}{cid}/{f['id']}.{ext}"
        return {
            "cid": cid,
            "reference": row.get("reference"),
            "dateSignature": str(row.get("dateSignature")),
            "doc_type": rec["doc_type"],
            "format": rec["format"],
            "n_sections": rec["n_sections"],
            "n_tables": rec["n_tables"],
            "n_figures": rec["n_figures"],
            "n_ocr_kept": rec["n_ocr_kept"],
            "n_ocr_dropped": rec["n_ocr_dropped"],
            "error": "; ".join(rec["errors"][:3]) if rec["errors"] else None,
            "doc_json": record_to_json(rec),
        }
    except Exception as e:
        import traceback

        logger.error(f"Unexpected failure on {cid}: {e}\n{traceback.format_exc()}")
        return {
            "cid": cid, "reference": row.get("reference"),
            "dateSignature": str(row.get("dateSignature")),
            "doc_type": "error", "format": "unknown",
            "n_sections": 0, "n_tables": 0, "n_figures": 0,
            "n_ocr_kept": 0, "n_ocr_dropped": 0,
            "error": f"{type(e).__name__}: {e}",
            "doc_json": record_to_json({"cid": cid, "errors": [str(e)]}),
        }


def process_shard(rows: pd.DataFrame, shard_idx: int, workers: int) -> None:
    figs_base = config.FIGS_PATH
    records = [None] * len(rows)

    if workers > 1 and len(rows) > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(process_one, row.to_dict(), figs_base): i
                for i, row in rows.iterrows()
            }
            for fut in tqdm(as_completed(futs), total=len(futs), desc=f"shard {shard_idx:05d}"):
                records[futs[fut]] = fut.result()
    else:
        for i, (_, row) in enumerate(rows.iterrows()):
            records[i] = process_one(row.to_dict(), figs_base)
            if (i + 1) % 25 == 0:
                logger.info(f"shard {shard_idx:05d}: {i + 1}/{len(rows)} processed")

    df = pd.DataFrame(records)
    if not utils.put_parquet(df, shard_output_path(shard_idx)):
        raise RuntimeError(
            f"Could not write shard {shard_idx} to S3 after "
            f"{config.S3_WRITE_RETRIES} attempts."
        )
    n_err = int(df["error"].notna().sum())
    logger.info(
        f"Shard {shard_idx:05d} refreshed ({len(df)} documents, {n_err} with errors)."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Step 2b: refresh structure (no LLM, no figure re-upload)"
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="Process only the first N documents (0 = all)")
    parser.add_argument("--skip-complete", action="store_true",
                        help="Skip shards already refreshed (per-shard sidecar), "
                             "to resume after a crash")
    parser.add_argument("--workers", type=int, default=16,
                        help=f"Parallel S3 downloads (default 16; config default "
                             f"is {config.MAX_CONCURRENCY_DOWNLOAD})")
    parser.add_argument("--sequential", action="store_true",
                        help="Disable parallelism (debug)")
    args = parser.parse_args()

    sel = pd.read_parquet(config.SELECTION_FILE, filesystem=config.fs)
    if args.limit > 0:
        sel = sel.head(args.limit).reset_index(drop=True)
        logger.info(f"--limit mode: only {len(sel)} documents")
    else:
        logger.info(f"{len(sel)} documents to refresh")

    n_shards = max(1, -(-len(sel) // config.SHARD_SIZE))
    workers = 1 if args.sequential else args.workers

    total_ok = 0
    total_err = 0
    n_skipped = 0
    for shard_idx in range(n_shards):
        expected = min(config.SHARD_SIZE, len(sel) - shard_idx * config.SHARD_SIZE)
        if args.skip_complete and _sidecar_complete(shard_idx, expected):
            logger.info(f"Shard {shard_idx:05d} already refreshed -> skipped (resume).")
            n_skipped += 1
            continue
        rows = sel.iloc[shard_idx * config.SHARD_SIZE:(shard_idx + 1) * config.SHARD_SIZE].reset_index(drop=True)
        t0 = time.time()
        process_shard(rows, shard_idx, workers)
        _write_sidecar(shard_idx, expected)
        dt = time.time() - t0
        logger.info(f"Shard {shard_idx:05d}: {dt:.0f}s")
        df = pd.read_parquet(shard_output_path(shard_idx), filesystem=config.fs)
        total_ok += int(df["error"].isna().sum())
        total_err += int(df["error"].notna().sum())

    logger.info(
        f"Step 2b done: {total_ok} documents OK, {total_err} with errors, "
        f"{n_skipped} shards skipped. Shards written to {config.DATA_PATH}"
    )
    logger.info(
        "Figures were NOT re-uploaded; ocr_results.jsonl stays valid. "
        "Run step 3 (retry) only if you want to re-OCR, then step 4, then step 5."
    )


if __name__ == "__main__":
    main()
