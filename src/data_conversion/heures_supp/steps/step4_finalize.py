"""
Step 4 - Finalization: OCR merge + RAG-consumable outputs.

Reads the step 2 shards and data/ocr_results.jsonl, then writes:
  data/final_shard_XXXXX.parquet  : full record per document (figures with ocr_md)
  data/chunks.jsonl               : section chunks + TOC + preamble + signature
  data/tables.jsonl               : table objects (structured grid + md)
  logs/summary.json               : global statistics

Run:
    uv run src/data_conversion/heures_supp/steps/step4_finalize.py
"""
import json
import logging
import os
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

CHUNKS_FILE = config.DATA_PATH + "chunks.jsonl"
TABLES_FILE = config.DATA_PATH + "tables.jsonl"
SUMMARY_FILE = config.LOGS_PATH + "summary.json"


def load_ocr_results() -> dict:
    """
    fig_ref -> (ocr_md, ok); the last successful result wins.

    OCR text is normalized here (NBSP variants -> space): it also cleans
    results produced by older runs, and covers both merge points (figures
    and the pdf_scanned "FULL" result).
    """
    out = {}
    for r in utils.read_jsonl(config.DATA_PATH + "ocr_results.jsonl"):
        if r.get("ok"):
            out[r["fig_ref"]] = (utils.normalize_ws(r.get("ocr_md")), True)
        elif r["fig_ref"] not in out:
            out[r["fig_ref"]] = (None, False)
    return out


def final_shard_path(idx: int) -> str:
    return f"{config.DATA_PATH}final_shard_{idx:05d}.parquet"


def list_shards() -> list:
    files = [
        f for f in config.fs.ls(config.DATA_PATH, detail=False)
        if "/shard_" in f and f.endswith(".parquet") and "final_shard" not in f
    ]
    idxs = []
    for f in files:
        try:
            idxs.append(int(f.split("/")[-1][len("shard_"):-len(".parquet")]))
        except ValueError:
            continue
    return sorted(idxs)


def toc_md(rec: dict) -> str:
    lines = []
    for t in rec.get("toc", []):
        indent = "  " * (max(t["level"] - 1, 0))
        num = f"{t['number']} " if t.get("number") else ""
        lines.append(f"{indent}{num}{t['title']}".strip())
    return "\n".join(lines)


def build_chunks(rec: dict) -> list:
    chunks = []
    cid = rec["cid"]
    base = {
        "cid": cid,
        "reference": rec.get("reference"),
        "doc_type": rec.get("doc_type"),
        "format": rec.get("format"),
    }
    if rec.get("toc"):
        chunks.append({
            **base, "chunk_id": f"{cid}:TOC", "chunk_type": "toc",
            "toc_md": toc_md(rec),
        })
    if (rec.get("preamble_md") or "").strip():
        chunks.append({
            **base, "chunk_id": f"{cid}:PREAMBLE", "chunk_type": "preamble",
            "body_md": rec["preamble_md"],
            "table_ids": rec.get("preamble_table_ids", []),
            "figure_ids": rec.get("preamble_figure_ids", []),
            "n_chars": len(rec["preamble_md"]),
        })
    for s in rec.get("sections", []):
        chunks.append({
            **base,
            "chunk_id": f"{cid}:{s['id']}",
            "chunk_type": "section",
            "section_id": s["id"],
            "level": s["level"],
            "number": s.get("number"),
            "title": s["title"],
            "confidence": s.get("confidence"),
            "parent": s.get("parent"),
            "body_md": s.get("body_md", ""),
            "table_ids": s.get("table_ids", []),
            "figure_ids": s.get("figure_ids", []),
            "n_chars": len(s.get("body_md", "")),
        })
    if (rec.get("signature_md") or "").strip():
        chunks.append({
            **base, "chunk_id": f"{cid}:SIGNATURE", "chunk_type": "signature",
            "body_md": rec["signature_md"],
        })
    return chunks


def build_tables(rec: dict) -> list:
    sec_titles = {s["id"]: s["title"] for s in rec.get("sections", [])}
    out = []
    for t in rec.get("tables", []):
        sec = t.get("section")
        if sec == "root":
            sec_title = "(preamble / out of section)"
        elif sec == "signature":
            sec_title = "(signature)"
        else:
            sec_title = sec_titles.get(sec, "")
        header = None
        grid = t.get("grid") or []
        if grid and any((c or "").strip() for c in grid[0]):
            header = grid[0]
        out.append({
            "table_id": f"{rec['cid']}:{t['id']}",
            "cid": rec["cid"],
            "reference": rec.get("reference"),
            "section_id": sec,
            "section_title": sec_title,
            "context": t.get("context", ""),
            "n_rows": t.get("n_rows"),
            "n_cols": t.get("n_cols"),
            "empty": t.get("empty", False),
            "header": header,
            "rows": grid,
            "spans": t.get("spans"),
            "vmerge": t.get("vmerge"),
            "md": t.get("md", ""),
        })
    return out


def main():
    ocr = load_ocr_results()
    logger.info(f"{len(ocr)} OCR results loaded")

    shards = list_shards()
    logger.info(f"{len(shards)} step 2 shards to finalize: {shards}")

    n_docs = n_chunks = n_tables = 0
    fmt_counts = Counter()
    type_counts = Counter()
    triage_reasons = Counter()
    ocr_ok = ocr_err = 0

    all_chunks = []
    all_tables = []

    for idx in shards:
        df = pd.read_parquet(f"{config.DATA_PATH}shard_{idx:05d}.parquet",
                             filesystem=config.fs)
        final_rows = []
        for _, row in df.iterrows():
            try:
                rec = json.loads(row["doc_json"])
            except (TypeError, json.JSONDecodeError):
                final_rows.append({**row.to_dict(), "n_ocr_ok": 0})
                continue
            n_ocr_ok = 0
            for f in rec.get("figures", []):
                tri = f.get("triage", {})
                triage_reasons[f"{tri.get('reason', '?')}"] += 1
                ref = f"{rec['cid']}:{f['id']}"
                if ref in ocr:
                    md, ok = ocr[ref]
                    f["ocr_md"] = md
                    f["ocr_ok"] = ok
                    if ok:
                        n_ocr_ok += 1
                        ocr_ok += 1
                elif not tri.get("keep"):
                    f["ocr_md"] = None
                    f["ocr_ok"] = None
                else:
                    f["ocr_md"] = None
                    f["ocr_ok"] = False
                    ocr_err += 1
            # scanned PDFs: full OCR attached to the document
            if rec.get("doc_type") == "pdf_scanned":
                ref = f"{rec['cid']}:FULL"
                if ref in ocr:
                    md, ok = ocr[ref]
                    rec["pdf_ocr_md"] = md if ok else None
                    rec["pdf_ocr_ok"] = ok
                    if ok:
                        ocr_ok += 1
            rec["n_ocr_ok"] = n_ocr_ok
            final_rows.append({
                "cid": row["cid"],
                "reference": row.get("reference"),
                "dateSignature": row.get("dateSignature"),
                "doc_type": rec.get("doc_type"),
                "format": rec.get("format"),
                "n_sections": rec.get("n_sections"),
                "n_tables": rec.get("n_tables"),
                "n_figures": rec.get("n_figures"),
                "n_ocr_kept": rec.get("n_ocr_kept"),
                "n_ocr_dropped": rec.get("n_ocr_dropped"),
                "n_ocr_ok": n_ocr_ok,
                "error": row.get("error"),
                "doc_json": json.dumps(rec, ensure_ascii=False),
            })
            n_docs += 1
            fmt_counts[rec.get("format")] += 1
            type_counts[rec.get("doc_type")] += 1
            all_chunks.extend(build_chunks(rec))
            all_tables.extend(build_tables(rec))

        fdf = pd.DataFrame(final_rows)
        if not utils.put_parquet(fdf, final_shard_path(idx)):
            raise RuntimeError(f"Could not write: {final_shard_path(idx)}")
        n_chunks += len(all_chunks)
        logger.info(f"final_shard_{idx:05d} written ({len(fdf)} documents)")

    # chunks + tables (full files, rebuilt on every run)
    n_chunks = len(all_chunks)
    n_tables = len(all_tables)
    if not utils.write_jsonl(CHUNKS_FILE, all_chunks):
        raise RuntimeError(f"Could not write: {CHUNKS_FILE}")
    if not utils.write_jsonl(TABLES_FILE, all_tables):
        raise RuntimeError(f"Could not write: {TABLES_FILE}")

    summary = {
        "n_documents": n_docs,
        "n_chunks": n_chunks,
        "n_tables": n_tables,
        "formats": dict(fmt_counts),
        "doc_types": dict(type_counts),
        "figure_triage_reasons": dict(triage_reasons),
        "ocr": {"ok": ocr_ok, "pending_or_failed": ocr_err},
        "outputs": {
            "shards": f"{config.DATA_PATH}final_shard_*.parquet",
            "chunks": CHUNKS_FILE,
            "tables": TABLES_FILE,
        },
    }
    if not utils.put_bytes(SUMMARY_FILE, json.dumps(summary, ensure_ascii=False, indent=2).encode()):
        raise RuntimeError(f"Could not write: {SUMMARY_FILE}")

    logger.info(json.dumps(summary, ensure_ascii=False, indent=2))
    logger.info("Step 4 done.")


if __name__ == "__main__":
    main()
