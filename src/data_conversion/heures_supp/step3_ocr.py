"""
Step 3 - OCR LLM des images conservées par le triage (step 2).

Un seul appel VLM par image (sur l'image elle-même, pas sur des pages PDF).
Reprise : les figures déjà présentes dans data/ocr_results.jsonl ne sont
jamais retraitées.

Option --ocr-scanned : les PDF scannés (sans couche texte) sont passés dans
le pipeline VLM Docling complet (chemin lourd, réservé au résidu).

Sortie (zone autorisée) : data/ocr_results.jsonl
  {fig_ref, cid, fig_id, ocr_md, model, ok, error, ts}

Lancement :
    uv run src/data_conversion/heures_supp/step3_ocr.py
    uv run src/data_conversion/heures_supp/step3_ocr.py --max-images 20
    uv run src/data_conversion/heures_supp/step3_ocr.py --ocr-scanned
"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd

import config
import utils

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("s3fs").setLevel(logging.WARNING)

OCR_RESULTS_FILE = config.DATA_PATH + "ocr_results.jsonl"
CHECKPOINT_EVERY = 25

MIME_BY_EXT = utils.MIME_BY_EXT
OCRABLE_EXT = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "svg"}


# ------------------------------------------------------------------- helpers
def collect_queue(ocr_scanned: bool) -> list:
    """Collecte la file d'OCR depuis les shards step 2."""
    items = []
    try:
        shard_files = [
            f for f in config.fs.ls(config.DATA_PATH, detail=False)
            if "/shard_" in f and f.endswith(".parquet")
            and "final_shard" not in f
        ]
    except (FileNotFoundError, IsADirectoryError):
        logger.error(f"Aucun shard trouvé dans {config.DATA_PATH} (lancer step2 d'abord).")
        return []

    for f in sorted(shard_files):
        df = pd.read_parquet(f, filesystem=config.fs)
        for _, row in df.iterrows():
            try:
                rec = json.loads(row["doc_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            cid = rec.get("cid") or row["cid"]
            for fig in rec.get("figures", []):
                tri = fig.get("triage", {})
                if tri.get("keep") and fig.get("fig_s3"):
                    items.append({
                        "fig_ref": f"{cid}:{fig['id']}",
                        "cid": cid,
                        "fig_id": fig["id"],
                        "fig_s3": fig["fig_s3"],
                        "fmt": (fig.get("fmt") or "").lower(),
                        "kind": "image",
                    })
            if ocr_scanned and rec.get("doc_type") == "pdf_scanned":
                items.append({
                    "fig_ref": f"{cid}:FULL",
                    "cid": cid,
                    "fig_id": "FULL",
                    "fig_s3": None,
                    "fmt": "pdf",
                    "kind": "pdf_scanned",
                })
    return items


def done_refs() -> set:
    """
    fig_refs traités avec succès. Les échecs (transitoires ou durables) ne
    sont PAS marqués comme faits : une relance de step 3 retente les échecs
    (les échecs durables type EMF sans rasteriseur sont peu coûteux).
    """
    return {r["fig_ref"] for r in utils.read_jsonl(OCR_RESULTS_FILE) if r.get("ok")}


def rasterize_vector(data: bytes, ext: str):
    """
    Tente de rasteriser un EMF/WMF via ImageMagick. Retourne (bytes, mime)
    ou None si impossible (l'appelant signale alors un skip).
    """
    bin_ = shutil.which("magick") or shutil.which("convert")
    if not bin_:
        return None
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, f"in.{ext}")
        dst = os.path.join(td, "out")
        with open(src, "wb") as f:
            f.write(data)
        try:
            subprocess.run(
                [bin_, src, "-density", "150", f"{dst}.png"],
                check=True, capture_output=True, timeout=120,
            )
        except Exception:
            return None
        png = os.path.join(td, "out.png")
        if os.path.exists(png):
            with open(png, "rb") as f:
                return f.read(), "image/png"
    return None


def ocr_pdf_scanned(docx_path: str) -> str:
    """
    OCR complet d'un PDF scanné via le pipeline VLM Docling
    (mêmes réglages que l'ancienne chaîne de conversion).
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import VlmConvertOptions, VlmPipelineOptions
    from docling.datamodel.vlm_engine_options import ApiVlmEngineOptions, VlmEngineType
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.pipeline.vlm_pipeline import VlmPipeline

    raw = utils.download_bytes(docx_path)
    if raw is None:
        raise RuntimeError("téléchargement du PDF impossible")

    engine_options = ApiVlmEngineOptions(
        runtime_type=VlmEngineType.API,
        url=f"{config.LLM_API_URL.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
        params={"model": config.LLM_MODEL, "temperature": 0.0},
        timeout=config.OCR_TIMEOUT,
        concurrency=2,
    )
    vlm_options = VlmConvertOptions.from_preset(
        "gemma_27b", engine_options=engine_options
    )
    pipeline_options = VlmPipelineOptions(vlm_options=vlm_options, enable_remote_services=True)
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=VlmPipeline, pipeline_options=pipeline_options
            )
        }
    )
    # docling attend un chemin (ou DocumentStream) : on passe par un fichier tmp
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(raw)
        tmp_path = tf.name
    try:
        conv = converter.convert(tmp_path)
        return conv.document.export_to_markdown()
    finally:
        os.unlink(tmp_path)


# ------------------------------------------------------------------- workers
def ocr_one(item: dict, doc_paths: dict) -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    base = {"fig_ref": item["fig_ref"], "cid": item["cid"], "fig_id": item["fig_id"]}
    try:
        if item["kind"] == "pdf_scanned":
            md = utils.clean_ocr_md(ocr_pdf_scanned(doc_paths[item["cid"]]))
            return {**base, "ocr_md": md, "model": config.LLM_MODEL,
                    "ok": True, "error": None, "ts": ts}

        data = utils.download_bytes(item["fig_s3"])
        if data is None:
            return {**base, "ocr_md": None, "model": None,
                    "ok": False, "error": "téléchargement image impossible", "ts": ts}

        ext = item["fmt"]
        if ext in ("emf", "wmf"):
            r = rasterize_vector(data, ext)
            if r is None:
                return {**base, "ocr_md": None, "model": None,
                        "ok": False, "error": "rasterisation_emf_wmf_indisponible", "ts": ts}
            data, mime = r
        elif ext in MIME_BY_EXT:
            mime = MIME_BY_EXT[ext]
        else:
            return {**base, "ocr_md": None, "model": None,
                    "ok": False, "error": f"format_image_non_ocrable:{ext}", "ts": ts}

        # redimensionnement / ré-encodage déterministe avant envoi au VLM
        data, mime = utils.prepare_image_for_ocr(data, ext)
        md = utils.clean_ocr_md(utils.llm_ocr_image(data, mime))
        return {**base, "ocr_md": md, "model": config.LLM_MODEL,
                "ok": True, "error": None, "ts": ts}
    except Exception as e:
        return {**base, "ocr_md": None, "model": None,
                "ok": False, "error": f"{type(e).__name__}: {e}", "ts": ts}


def main():
    parser = argparse.ArgumentParser(description="Step 3 : OCR des images conservées")
    parser.add_argument("--max-images", type=int, default=0,
                        help="Nombre max d'appels OCR (0 = tous)")
    parser.add_argument("--ocr-scanned", action="store_true",
                        help="Traiter aussi les PDF scannés via Docling (lourd)")
    args = parser.parse_args()

    # chemin brut des documents (pour les PDF scannés)
    sel = pd.read_parquet(config.SELECTION_FILE, filesystem=config.fs)
    doc_paths = dict(zip(sel["cid"], sel["docx_path"]))

    queue = collect_queue(ocr_scanned=args.ocr_scanned)
    done = done_refs()
    todo = [q for q in queue if q["fig_ref"] not in done]
    if args.max_images > 0:
        todo = todo[: args.max_images]

    logger.info(
        f"File d'OCR : {len(queue)} au total, {len(done)} déjà faits, "
        f"{len(todo)} à traiter maintenant."
    )
    if not todo:
        logger.info("Rien à faire.")
        return

    results = utils.read_jsonl(OCR_RESULTS_FILE)
    n_ok = n_err = 0
    with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENCY_OCR) as ex:
        futs = {ex.submit(ocr_one, item, doc_paths): item for item in todo}
        for i, fut in enumerate(futs):
            r = fut.result()
            results.append(r)
            if r["ok"]:
                n_ok += 1
            else:
                n_err += 1
            if (i + 1) % CHECKPOINT_EVERY == 0 or i + 1 == len(todo):
                if not utils.write_jsonl(OCR_RESULTS_FILE, results):
                    raise RuntimeError(f"Écriture impossible : {OCR_RESULTS_FILE}")
                logger.info(f"OCR : {i + 1}/{len(todo)} (ok={n_ok}, err={n_err})")

    logger.info(f"Step 3 terminée : {n_ok} OK, {n_err} erreurs. Sortie : {OCR_RESULTS_FILE}")


if __name__ == "__main__":
    main()
