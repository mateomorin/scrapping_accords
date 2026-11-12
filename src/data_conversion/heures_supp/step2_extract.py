"""
Step 2 - Extraction de structure déterministe (AUCUN LLM).

Pour chaque document de la sélection : téléchargement du fichier brut,
sniffing du format réel, extraction XML (docx / pptx embarqués / pdf / rtf /
html), récupération de la structure (sections), triage heuristique des
images. Les images conservées par le triage sont uploadées sous
data/figs/{cid}/ pour l'OCR de la step 3.

Sortie (zone autorisée) : data/shard_XXXXX.parquet
  Colonnes : cid, reference, dateSignature, doc_type, format, n_sections,
             n_tables, n_figures, n_ocr_kept, n_ocr_dropped, error, doc_json
Reprise : les shards déjà présents sur S3 ne sont jamais retraités.

Lancement :
    uv run src/data_conversion/heures_supp/step2_extract.py
    uv run src/data_conversion/heures_supp/step2_extract.py --limit 30   # essai rapide
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

import config
import utils
from doc_process import process_document_bytes, record_to_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("s3fs").setLevel(logging.WARNING)


def existing_shards() -> dict:
    """
    idx -> nb de lignes des shards déjà écrits.
    Un shard n'est considéré COMPLET (et donc définitif) que s'il contient
    SHARD_SIZE lignes ; un shard partiel (essai --limit, crash en cours
    d'écriture) sera re-traité à la prochaine relance.
    """
    prefix = config.DATA_PATH
    try:
        files = config.fs.ls(prefix, detail=False)
    except (FileNotFoundError, IsADirectoryError):
        return {}
    out = {}
    for f in files:
        name = f.split("/")[-1]
        if name.startswith("shard_") and name.endswith(".parquet"):
            try:
                idx = int(name[len("shard_"):-len(".parquet")])
            except ValueError:
                continue
            try:
                out[idx] = len(pd.read_parquet(f, filesystem=config.fs))
            except Exception:
                out[idx] = 0
    return out


def shard_output_path(shard_idx: int) -> str:
    return f"{config.DATA_PATH}shard_{shard_idx:05d}.parquet"


def upload_figure(fig_s3: str, data: bytes) -> bool:
    if data is None:
        return False
    return utils.put_bytes(fig_s3, data)


def process_one(row, figs_base: str) -> dict:
    """Traite un document (ligne de la sélection) et retourne le record."""
    cid = row["cid"]
    data = utils.download_bytes(row["docx_path"])
    if data is None:
        return {
            "cid": cid, "reference": row.get("reference"),
            "dateSignature": str(row.get("dateSignature")),
            "doc_type": "missing", "format": "missing",
            "n_sections": 0, "n_tables": 0, "n_figures": 0,
            "n_ocr_kept": 0, "n_ocr_dropped": 0,
            "error": "fichier introuvable ou téléchargement impossible",
            "doc_json": record_to_json({"cid": cid, "errors": ["missing file"]}),
        }
    try:
        rec = process_document_bytes(
            data, cid, row.get("reference"),
            upload_figure=upload_figure, figs_base=figs_base,
        )
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

        logger.error(f"Échec inattendu sur {cid} : {e}\n{traceback.format_exc()}")
        return {
            "cid": cid, "reference": row.get("reference"),
            "dateSignature": str(row.get("dateSignature")),
            "doc_type": "error", "format": "unknown",
            "n_sections": 0, "n_tables": 0, "n_figures": 0,
            "n_ocr_kept": 0, "n_ocr_dropped": 0,
            "error": f"{type(e).__name__}: {e}",
            "doc_json": record_to_json({"cid": cid, "errors": [str(e)]}),
        }


def process_shard(rows: pd.DataFrame, shard_idx: int, use_threads: bool) -> None:
    figs_base = config.FIGS_PATH
    records = [None] * len(rows)

    if use_threads and len(rows) > 1:
        with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENCY_DOWNLOAD) as ex:
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
                logger.info(f"shard {shard_idx:05d}: {i + 1}/{len(rows)} traités")

    df = pd.DataFrame(records)
    ok = utils.put_parquet(df, shard_output_path(shard_idx))
    if not ok:
        raise RuntimeError(
            f"Impossible d'écrire le shard {shard_idx} sur S3 après "
            f"{config.S3_WRITE_RETRIES} tentatives."
        )
    n_err = int(df["error"].notna().sum())
    logger.info(
        f"Shard {shard_idx:05d} sauvegardé ({len(df)} documents, {n_err} avec erreur)."
    )


def main():
    parser = argparse.ArgumentParser(description="Step 2 : extraction de structure")
    parser.add_argument("--limit", type=int, default=0,
                        help="Ne traiter que les N premiers documents (0 = tout)")
    parser.add_argument("--sequential", action="store_true",
                        help="Désactiver le parallélisme (debug)")
    args = parser.parse_args()

    sel = pd.read_parquet(config.SELECTION_FILE, filesystem=config.fs)
    if args.limit > 0:
        sel = sel.head(args.limit).reset_index(drop=True)
        logger.info(f"Mode --limit : {len(sel)} documents seulement")
    else:
        logger.info(f"{len(sel)} documents à traiter")

    n_shards = max(1, -(-len(sel) // config.SHARD_SIZE))
    done = existing_shards()
    use_threads = not args.sequential

    total_ok = 0
    total_err = 0
    for shard_idx in range(n_shards):
        expected = min(config.SHARD_SIZE, len(sel) - shard_idx * config.SHARD_SIZE)
        if done.get(shard_idx, 0) >= expected:
            logger.info(f"Shard {shard_idx:05d} complet sur S3 -> ignoré (reprise).")
            continue
        rows = sel.iloc[shard_idx * config.SHARD_SIZE:(shard_idx + 1) * config.SHARD_SIZE].reset_index(drop=True)
        t0 = time.time()
        process_shard(rows, shard_idx, use_threads)
        dt = time.time() - t0
        logger.info(f"Shard {shard_idx:05d} : {dt:.0f}s")
        # petite statistique sur le shard
        df = pd.read_parquet(shard_output_path(shard_idx), filesystem=config.fs)
        total_ok += int(df["error"].isna().sum())
        total_err += int(df["error"].notna().sum())

    logger.info(
        f"Step 2 terminée : {total_ok} documents OK, {total_err} avec erreur, "
        f"shards écrits dans {config.DATA_PATH}"
    )


if __name__ == "__main__":
    main()
