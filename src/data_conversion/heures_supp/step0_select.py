"""
Step 0 - Sélection du sous-ensemble.

Lit (en lecture seule) tous les parquets de metadata dans METADATA_PATH,
déduplique par cid, et conserve les documents dont la colonne booléenne
"Heures supplémentaires (contingent, majoration)" est True.

Écrit (zone autorisée) : logs/selection.parquet
Colonnes : cid, reference, title, legalStatus, nature, dateSignature,
           dateDiffusion, YYYY, MM, docx_path, __source_file + colonnes thème.

Lancement :
    uv run src/data_conversion/heures_supp/step0_select.py
"""
import logging

import pandas as pd

import config
import utils

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def to_bool(v):
    """Conversion robuste d'une valeur de colonne thème en booléen."""
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
    logger.info(f"{len(files)} fichiers de metadata trouvés dans {config.METADATA_PATH}")

    frames = []
    for f in files:
        df = pd.read_parquet(f, filesystem=config.fs)
        df["__source_file"] = f.split("/")[-1]
        frames.append(df)
        logger.info(f"  {f.split('/')[-1]}: {len(df)} lignes")

    all_md = pd.concat(frames, ignore_index=True)
    n_before = len(all_md)
    all_md = all_md.drop_duplicates(subset="cid", keep="first")
    logger.info(f"{n_before} lignes -> {len(all_md)} cids uniques")

    # Masque thème
    theme_mask = pd.Series(False, index=all_md.index)
    for col in config.THEME_COLUMNS:
        if col in all_md.columns:
            theme_mask |= all_md[col].map(to_bool)
            logger.info(f"Colonne thème {col!r}: {theme_mask.sum()} True")
        else:
            logger.warning(f"Colonne thème introuvable : {col!r}")

    sel = all_md[theme_mask].copy()
    logger.info(f"Sous-ensemble : {len(sel)} documents")

    # Chemins des documents bruts (YYYY/MM/cid.docx d'après dateSignature)
    sel["dateSignature"] = pd.to_datetime(sel["dateSignature"], errors="coerce")
    n_missing_date = int(sel["dateSignature"].isna().sum())
    if n_missing_date:
        logger.warning(f"{n_missing_date} documents sans dateSignature exploitable, écartés.")
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
        raise SystemExit(f"Écriture impossible : {config.SELECTION_FILE}")
    logger.info(f"Sélection écrite : {config.SELECTION_FILE} ({len(out)} documents)")


if __name__ == "__main__":
    main()
