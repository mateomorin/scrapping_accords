"""
Step 5 - Validation.

- Prints a per-document report for the sampled documents: section tree
  (level / confidence / number), a table sample, the figure triage
  decisions, errors.
- Option --compare: measures the token-overlap rate between our
  reconstructed text (sections + tables) and the old LLM output present in
  s3://mateomorin/legifrance/full_data/data/ (read-only).
- Writes logs/validation_report.json

Run:
    uv run src/data_conversion/heures_supp/steps/step5_validate.py --limit 3
    uv run src/data_conversion/heures_supp/steps/step5_validate.py --limit 5 --compare
"""
import argparse
import json
import logging
import os
import re
import sys
from collections import Counter

import pandas as pd

# Make the heures_supp package importable when run as a script
# (package root: src/data_conversion; no package install needed).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from heures_supp.core import config, utils  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("s3fs").setLevel(logging.WARNING)

EXISTING_DATA_PREFIX = "s3://mateomorin/legifrance/full_data/data/"
REPORT_FILE = config.LOGS_PATH + "validation_report.json"


def final_shards() -> list:
    """Shards to read: final_shard_* if step 4 has run, otherwise shard_*."""
    try:
        files = config.fs.ls(config.DATA_PATH, detail=False)
    except (FileNotFoundError, IsADirectoryError):
        return []
    finals = [f for f in files if "/final_shard_" in f and f.endswith(".parquet")]
    if finals:
        return sorted(finals)
    return sorted(f for f in files if "/shard_" in f and f.endswith(".parquet"))


def load_records(shard_files: list) -> pd.DataFrame:
    frames = []
    for f in shard_files:
        frames.append(pd.read_parquet(f, filesystem=config.fs))
    return pd.concat(frames, ignore_index=True)


def tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9àâäéèêëîïôöùûüçœ]{2,}", (text or "").lower()))


def overlap_f1(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return 2 * inter / (len(ta) + len(tb))


def load_existing_markdown(cids: set, cid_year: dict | None = None) -> dict:
    """
    cid -> markdown of the old conversion (read-only).
    The old chain wrote one directory per metadata batch
    (full_data/data/acco_data_{YEAR}/shard_*.parquet): first the directories
    matching the signature year of the sampled cids are read, then the rest
    is scanned if needed.
    """
    out = {}

    def scan_dir(d: str) -> None:
        if len(out) >= len(cids):
            return
        try:
            files = config.fs.ls(f"{EXISTING_DATA_PREFIX}{d}/", detail=False)
        except (FileNotFoundError, IsADirectoryError):
            return
        for f in files:
            if not f.endswith(".parquet"):
                continue
            try:
                df = pd.read_parquet(f, filesystem=config.fs)
            except Exception:
                continue
            if "cid" not in df.columns or "markdown" not in df.columns:
                continue
            for cid, md in zip(df["cid"], df["markdown"]):
                if cid in cids and cid not in out:
                    out[cid] = md
            if len(out) >= len(cids):
                return

    try:
        entries = config.fs.ls(EXISTING_DATA_PREFIX, detail=False)
    except (FileNotFoundError, IsADirectoryError):
        return out
    dirs = sorted({e.split("/")[-1] for e in entries if not e.endswith(".parquet")})

    # 1) directories targeted by signature year
    if cid_year:
        years = {cid_year[c] for c in cids if c in cid_year and cid_year.get(c)}
        targeted = [
            d for d in dirs
            if any(d == f"acco_data_{y}" or d.startswith(f"acco_data_{y}_") for y in years)
        ]
        for d in targeted:
            scan_dir(d)

    # 2) scan the rest (if some cids are not found)
    for d in dirs:
        if len(out) >= len(cids):
            break
        if d in targeted:
            continue
        scan_dir(d)
    return out


def doc_text(rec: dict) -> str:
    parts = [rec.get("preamble_md") or ""]
    for s in rec.get("sections", []):
        parts.append(s["title"])
        parts.append(s.get("body_md") or "")
    for t in rec.get("tables", []):
        parts.append(t.get("md") or "")
    return "\n".join(parts)


def report_doc(rec: dict, verbose: bool) -> dict:
    out = {
        "cid": rec.get("cid"),
        "doc_type": rec.get("doc_type"),
        "format": rec.get("format"),
        "n_paras": rec.get("n_paras"),
        "n_sections": rec.get("n_sections"),
        "n_tables": rec.get("n_tables"),
        "n_figures": rec.get("n_figures"),
        "n_ocr_kept": rec.get("n_ocr_kept"),
        "errors": rec.get("errors", []),
        "section_confidence": dict(Counter(
            s.get("confidence") for s in rec.get("sections", [])
        )),
        "figure_reasons": dict(Counter(
            f.get("triage", {}).get("reason") for f in rec.get("figures", [])
        )),
    }
    print(f"\n{'=' * 90}\nDOC {rec.get('cid')}  [{rec.get('doc_type')}/{rec.get('format')}]  "
          f"sections={rec.get('n_sections')} tables={rec.get('n_tables')} "
          f"figures={rec.get('n_figures')} (kept={rec.get('n_ocr_kept')})")
    for t in rec.get("toc", [])[:25]:
        indent = "  " * max(t["level"] - 1, 0)
        num = f"{t['number']} " if t.get("number") else ""
        print(f"{indent}- L{t['level']} [{t['confidence']}] {num}{t['title'][:75]}")
    if rec.get("toc") and len(rec["toc"]) > 25:
        print(f"  ... +{len(rec['toc']) - 25} sections")
    if verbose and rec.get("tables"):
        t = rec["tables"][0]
        print(f"  [table {t['id']} -> section {t['section']}] context: {t.get('context', '')[:80]}")
        print("  " + "\n  ".join((t.get("md") or "").splitlines()[:8]))
    if verbose and rec.get("figures"):
        for f in rec["figures"][:6]:
            tr = f.get("triage", {})
            print(f"  [fig {f['id']}] {f.get('fmt')} {f.get('size')}B "
                  f"origin={f.get('origin')} keep={tr.get('keep')} ({tr.get('reason')})")
    if rec.get("errors"):
        print(f"  ERRORS: {rec['errors'][:3]}")
    return out


def main():
    parser = argparse.ArgumentParser(description="Step 5: validation")
    parser.add_argument("--limit", type=int, default=3, help="Number of documents to display")
    parser.add_argument("--compare", action="store_true",
                        help="Compare with the old LLM output (full_data)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    shards = final_shards()
    if not shards:
        logger.error("No shards found (run step 2, then step 4).")
        return
    df = load_records(shards)
    logger.info(f"{len(df)} documents available (shards: {len(shards)})")

    sample = df.sample(n=min(args.limit, len(df)), random_state=args.seed)
    reports = []
    f1s = []

    existing = {}
    if args.compare:
        cids = set(sample["cid"])
        cid_year = {}
        if "dateSignature" in sample.columns:
            for cid, ds in zip(sample["cid"], sample["dateSignature"]):
                try:
                    cid_year[cid] = pd.to_datetime(ds).year
                except Exception:
                    pass
        logger.info(f"Loading the old conversion for {len(cids)} cids...")
        existing = load_existing_markdown(cids, cid_year)
        logger.info(f"{len(existing)} cids found in {EXISTING_DATA_PREFIX}")

    for _, row in sample.iterrows():
        rec = json.loads(row["doc_json"])
        r = report_doc(rec, verbose=True)
        if args.compare and rec["cid"] in existing:
            f1 = overlap_f1(doc_text(rec), existing[rec["cid"]])
            r["overlap_f1_vs_llm"] = round(f1, 4)
            f1s.append(f1)
            print(f"  [compare] token F1 vs old LLM output: {f1:.3f}")
        reports.append(r)

    report = {
        "n_documents_available": len(df),
        "n_sampled": len(reports),
        "doc_types": dict(Counter(df["doc_type"])),
        "formats": dict(Counter(df["format"])),
        "n_with_error": int(df["error"].notna().sum()),
        "mean_overlap_f1_vs_llm": round(sum(f1s) / len(f1s), 4) if f1s else None,
        "documents": reports,
    }
    ok = utils.put_bytes(REPORT_FILE, json.dumps(report, ensure_ascii=False, indent=2).encode())
    if ok:
        logger.info(f"Report written: {REPORT_FILE}")
    logger.info(f"Summary: doc_types={report['doc_types']} formats={report['formats']} "
                f"errors={report['n_with_error']}")


if __name__ == "__main__":
    main()
