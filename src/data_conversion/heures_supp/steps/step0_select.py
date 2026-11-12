"""
Step 0 - Subset selection.

Reads (read-only) every metadata parquet in METADATA_PATH, deduplicates by
cid, and keeps the documents whose boolean column
"Heures supplémentaires (contingent, majoration)" is True.

Writes (authorized zone): logs/selection.parquet
Columns: cid, reference, title, legalStatus, nature, dateSignature,
         dateDiffusion, YYYY, MM, docx_path, __source_file + theme columns.

Run:
    uv run src/data_conversion/heures_supp/steps/step0_select.py
"""
import logging
import os
import sys

import pandas as pd

# Make the heures_supp package importable when run as a script
# (package root: src/data_conversion; no package install needed).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from heures_supp.core import config, utils  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def to_bool(v):
    """Robust conversion of a theme-column value to boolean."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "oui")
    return False


def main():
    files = [
        f for f in config.fs.ls(config.METADATA_PATH, detail=False)
        if f.endswith(".parquet")
    ]
    logger.info(f"{len(files)} metadata files found in {config.METADATA_PATH}")

    frames = []
    for f in files:
        df = pd.read_parquet(f, filesystem=config.fs)
        df["__source_file"] = f.split("/")[-1]
        frames.append(df)
        logger.info(f"  {f.split('/')[-1]}: {len(df)} rows")

    all_md = pd.concat(frames, ignore_index=True)
    n_before = len(all_md)
    all_md = all_md.drop_duplicates(subset="cid", keep="first")
    logger.info(f"{n_before} rows -> {len(all_md)} unique cids")

    # theme mask
    theme_mask = pd.Series(False, index=all_md.index)
    for col in config.THEME_COLUMNS:
        if col in all_md.columns:
            theme_mask |= all_md[col].map(to_bool)
            logger.info(f"Theme column {col!r}: {theme_mask.sum()} True")
        else:
            logger.warning(f"Theme column not found: {col!r}")

    sel = all_md[theme_mask].copy()
    logger.info(f"Subset: {len(sel)} documents")

    # raw document paths (YYYY/MM/cid.docx from dateSignature)
    sel["dateSignature"] = pd.to_datetime(sel["dateSignature"], errors="coerce")
    n_missing_date = int(sel["dateSignature"].isna().sum())
    if n_missing_date:
        logger.warning(f"{n_missing_date} documents with no usable dateSignature, dropped.")
        sel = sel.dropna(subset=["dateSignature"])

    sel = sel.reset_index(drop=True)
    sel["YYYY"] = sel["dateSignature"].dt.year.astype(int).astype(str).str.zfill(4)
    sel["MM"] = sel["dateSignature"].dt.month.astype(int).astype(str).str.zfill(2)
    sel["docx_path"] = (
        config.DOCUMENTS_PATH + sel["YYYY"] + "/" + sel["MM"] + "/" + sel["cid"] + ".docx"
    )

    keep = [
        "cid", "reference", "title", "legalStatus", "nature",
        "dateSignature", "dateDiffusion", "YYYY", "MM", "docx_path",
        "__source_file",
    ] + list(config.THEME_COLUMNS)
    keep = [c for c in keep if c in sel.columns]
    out = sel[keep].reset_index(drop=True)

    ok = utils.put_parquet(out, config.SELECTION_FILE)
    if not ok:
        raise SystemExit(f"Could not write: {config.SELECTION_FILE}")
    logger.info(f"Selection written: {config.SELECTION_FILE} ({len(out)} documents)")


if __name__ == "__main__":
    main()
