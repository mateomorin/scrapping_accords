"""
Fourni une base de metadata, récupère tous les documents liés et applique une
transformation docx -> markdown. Si un document docx contient des images, il
est converti en PDF puis passé dans un pipeline OCR basé sur un LLM.

Le traitement est découpé en "shards" (SHARD_SIZE documents chacun) :
  - chaque shard est sauvegardé sur S3 dès qu'il est terminé (checkpoint) ;
  - un shard déjà présent sur S3 est ignoré au redémarrage (reprise après
    crash / retry Argo), on ne retraite jamais tout depuis le début ;
  - toute erreur sur un document individuel est capturée : le markdown est
    remplacé par une chaîne vide et le détail (chemin, exception, traceback,
    horodatage) est écrit dans un fichier de log dédié au shard sur S3 ;
  - les fichiers de sortie restent petits (un fichier par shard) pour ne
    jamais dépasser la limite de taille souhaitée.
"""
import argparse
import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import traceback
import zipfile

import pandas as pd
from tqdm.asyncio import tqdm_asyncio
import xml.etree.ElementTree as ET

import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("docling").setLevel(logging.WARNING)


def fetch_metadata(metadata_name: str):
    """
    metadata must have columns dateSignature and cid
    """
    metadata = pd.read_parquet(config.METADATA_PATH + metadata_name, filesystem=config.fs)
    return metadata


def build_doc_paths(metadata: pd.DataFrame):
    """
    doc_paths must be of format DOCUMENTS_PATH/[YYYY]/[MM]/[document_cid].docx
    """
    metadata["dateSignature"] = pd.to_datetime(metadata["dateSignature"])
    metadata["YYYY"] = metadata["dateSignature"].dt.year.astype(str).str.zfill(4)
    metadata["MM"] = metadata["dateSignature"].dt.month.astype(str).str.zfill(2)

    metadata["docx_path"] = (
        config.DOCUMENTS_PATH
        + metadata["YYYY"] + "/"
        + metadata["MM"] + "/"
        + metadata["cid"] + ".docx"
    )

    return metadata["docx_path"].to_list()


def docx_body_has_images_fast(local_docx: str) -> bool:
    """
    Made by Gemini to extract only images from docx body (zipfile is more stable than python-docx)
    """
    with zipfile.ZipFile(local_docx, "r") as z:
        namelist = z.namelist()

        rels_path = "word/_rels/document.xml.rels"
        docx_path = "word/document.xml"

        # 1. Check if relation files are in the document, if not set to True
        if rels_path not in namelist or docx_path not in namelist:
            return True

        # 2. Recover rIds linked to images
        rels_xml = z.read(rels_path)
        rels_root = ET.fromstring(rels_xml)

        image_rids = set()
        for rel in rels_root:
            rel_type = rel.attrib.get("Type", "")
            # Type standard OpenXML pour une image
            if rel_type.endswith("/image"):
                rel_id = rel.attrib.get("Id")
                if rel_id:
                    image_rids.add(rel_id)

        if not image_rids:
            return False

        # 3. Check if one of the rIds is used in word/document.xml
        doc_xml = z.read(docx_path).decode("utf-8", errors="ignore")

        for rid in image_rids:
            if f'="{rid}"' in doc_xml:
                return True

    return False


def basic_conversion(local_docx: str):
    return config.basic_converter.convert(local_docx).markdown


def repair_docx(input_docx: str, output_docx: str):
    """
    Made by Gemini to avoid potentially corrupted docx files.
    Clean them by deleting [trash] folders in DOCX archive.
    """
    # Temp dir to extract folders
    extract_dir = input_docx + "_extracted"

    try:
        # Extract folders
        with zipfile.ZipFile(input_docx, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)

        # Delete [trash] if it exists
        trash_dir = os.path.join(extract_dir, "[trash]")
        if os.path.exists(trash_dir):
            shutil.rmtree(trash_dir)

        # Re-create clean zip
        with zipfile.ZipFile(output_docx, 'w', zipfile.ZIP_DEFLATED) as zip_out:
            for root, _, files in os.walk(extract_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    arcname = os.path.relpath(full_path, extract_dir)
                    zip_out.write(full_path, arcname)

    finally:
        # Clean-up
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)


def llm_conversion(local_docx: str, tmp_dir: str):
    file_stem = Path(local_docx).stem
    cleaned_docx = os.path.join(tmp_dir, f"{file_stem}_clean.docx")
    expected_pdf = os.path.join(tmp_dir, f"{file_stem}_clean.pdf")

    repair_docx(local_docx, cleaned_docx)

    # User profile for async mode
    user_profile_dir = os.path.join(tmp_dir, "lo_profile")
    os.makedirs(user_profile_dir, exist_ok=True)

    cmd = [
        'libreoffice',
        f'-env:UserInstallation=file://{user_profile_dir}',
        '--headless',
        '--convert-to', 'pdf',
        '--outdir', tmp_dir,
        cleaned_docx
    ]

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)

    markdown = config.docling_llm_converter.convert(expected_pdf).document.export_to_markdown()

    # Clean-up
    if os.path.exists(cleaned_docx):
        os.remove(cleaned_docx)

    return markdown


async def convert_docx_to_markdown(doc_path: str, semaphore: asyncio.Semaphore):
    """
    Designed to never raise Exceptions, returns empty markdown if error.
    """
    async with semaphore:
        with tempfile.TemporaryDirectory() as task_tmp_dir:
            filename = doc_path.split("/")[-1]
            local_docx = os.path.join(task_tmp_dir, filename)

            try:
                await asyncio.to_thread(config.fs.get, doc_path, local_docx)

                has_images = await asyncio.to_thread(docx_body_has_images_fast, local_docx)
                if not has_images:
                    markdown = await asyncio.to_thread(basic_conversion, local_docx)
                else:
                    markdown = await asyncio.to_thread(
                        llm_conversion, local_docx, task_tmp_dir
                    )

                return {"markdown": markdown, "has_images": has_images, "error": None, "traceback": None}

            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                tb = traceback.format_exc()
                logger.error(f"Error while treating {doc_path}: {error_msg}")
                return {"markdown": "", "has_images": False, "error": error_msg, "traceback": tb}


def _s3_write_with_retry(write_fn, description: str, *args, **kwargs) -> bool:
    """
    Apply write_fn with several trials. Never raises errors except too many trials.
    """
    last_exc = None
    for attempt in range(config.S3_WRITE_RETRIES):
        try:
            write_fn(*args, **kwargs)
            return True
        except Exception as e:
            last_exc = e
            wait = config.S3_WRITE_RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                f"S3 writing failure ({description}), attempt "
                f"{attempt + 1}/{config.S3_WRITE_RETRIES} : {e}. "
                f"New attempt in {wait}s."
            )
            time.sleep(wait)

    logger.error(f"S3 writing definitively failed ({description}) : {last_exc}")
    return False


def _write_shard_parquet(df: pd.DataFrame, path: str):
    df.to_parquet(path, filesystem=config.fs, index=False)


def _write_error_log(errors: list, path: str):
    with config.fs.open(path, "w") as f:
        for record in errors:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def shard_output_path(output_name: str, shard_idx: int) -> str:
    return f"{config.FULL_DATA_PATH}{output_name}/shard_{shard_idx:05d}.parquet"


def shard_error_log_path(output_name: str, shard_idx: int) -> str:
    return f"{config.LOGS_PATH}{output_name}_errors/shard_{shard_idx:05d}.jsonl"


def list_existing_shards(output_name: str) -> set:
    """
    List shards already in S3 after crash or retry by Argo.
    Do not treat an already-finished one.
    """
    prefix = f"{config.FULL_DATA_PATH}{output_name}/"
    try:
        files = config.fs.ls(prefix)
    except FileNotFoundError:
        return set()

    existing = set()
    for f in files:
        name = f.split("/")[-1]
        if name.startswith("shard_") and name.endswith(".parquet"):
            try:
                existing.add(int(name[len("shard_"):-len(".parquet")]))
            except ValueError:
                continue
    return existing


async def process_dataset(metadata_name: str, output_name: str):
    metadata = fetch_metadata(metadata_name=metadata_name)
    metadata = metadata.reset_index(drop=True)
    doc_paths = build_doc_paths(metadata=metadata)

    n_docs = len(doc_paths)
    n_shards = math.ceil(n_docs / config.SHARD_SIZE)
    logger.info(f"[{output_name}] {n_docs} documents à traiter, découpés en {n_shards} shards de {config.SHARD_SIZE}.")

    existing_shards = list_existing_shards(output_name)
    if existing_shards:
        logger.info(
            f"[{output_name}] {len(existing_shards)} shard(s) déjà présents sur S3, "
            f"ils seront ignorés (reprise après crash/retry)."
        )

    semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_DOC)

    total_with_images = 0
    total_errors = 0

    for shard_idx in range(n_shards):
        if shard_idx in existing_shards:
            continue

        start = shard_idx * config.SHARD_SIZE
        end = min(start + config.SHARD_SIZE, n_docs)
        shard_paths = doc_paths[start:end]
        shard_metadata = metadata.iloc[start:end].copy()

        logger.info(f"[{output_name}] Shard {shard_idx + 1}/{n_shards} ({start}-{end - 1})")

        tasks = [convert_docx_to_markdown(doc_path=p, semaphore=semaphore) for p in shard_paths]
        results = await tqdm_asyncio.gather(*tasks, desc=f"{output_name} - shard {shard_idx}")

        shard_metadata["markdown"] = [r["markdown"] for r in results]
        shard_metadata["has_images"] = [r["has_images"] for r in results]
        shard_metadata["conversion_error"] = [r["error"] for r in results]

        shard_errors = []
        for path, r in zip(shard_paths, results):
            if r["error"] is not None:
                shard_errors.append({
                    "doc_path": path,
                    "shard_idx": shard_idx,
                    "error": r["error"],
                    "traceback": r["traceback"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        total_with_images += sum(r["has_images"] for r in results)
        total_errors += len(shard_errors)

        # --- Sauvegarde immédiate du shard sur S3 (checkpoint) ---
        ok = _s3_write_with_retry(
            _write_shard_parquet,
            f"shard {shard_idx} de {output_name}",
            shard_metadata,
            shard_output_path(output_name, shard_idx),
        )
        if not ok:
            # On arrête proprement ce dataset : Argo pourra relancer le job,
            # qui reprendra à partir de ce shard grâce à list_existing_shards.
            raise RuntimeError(
                f"[{output_name}] Impossible d'écrire le shard {shard_idx} sur S3 "
                f"après {config.S3_WRITE_RETRIES} tentatives."
            )

        # --- Sauvegarde du log d'erreurs du shard (uniquement s'il y en a) ---
        if shard_errors:
            _s3_write_with_retry(
                _write_error_log,
                f"log d'erreurs shard {shard_idx} de {output_name}",
                shard_errors,
                shard_error_log_path(output_name, shard_idx),
            )

        logger.info(
            f"[{output_name}] Shard {shard_idx} sauvegardé "
            f"({len(shard_errors)} erreur(s) sur {len(shard_paths)} documents)."
        )

    logger.info(
        f"[{output_name}] Terminé. Documents avec images : {total_with_images}. "
        f"Documents en erreur (texte vide) : {total_errors}/{n_docs}."
    )


def configure_output_name(output_name: str, metadata_name: str):
    if output_name == "default":
        output_name = metadata_name.replace("metadata", "data").replace(".parquet", "")

    return output_name


def main():
    parser = argparse.ArgumentParser(
        description="Transformation docx -> markdown with sharding, S3 checkpoints S3 and crash recovery"
    )

    parser.add_argument(
        "--metadata_name",
        type=str,
        required=True,
        help="Name of the metadata file (parquet)"
    )

    parser.add_argument(
        "--output_name",
        type=str,
        required=False,
        default="default",
        help="Name of the output directory (will contain shard_*.parquet), or 'default'"
    )

    args = parser.parse_args()

    output_name = configure_output_name(
        output_name=args.output_name,
        metadata_name=args.metadata_name
    )

    asyncio.run(process_dataset(metadata_name=args.metadata_name, output_name=output_name))


if __name__ == "__main__":
    main()
