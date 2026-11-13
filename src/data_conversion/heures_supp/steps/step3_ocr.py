"""
Step 3 - LLM OCR of the images kept by the triage (step 2).

One VLM call per image (on the image itself, not on PDF pages).
Resume: figures already present in data/ocr_results.jsonl with a successful
result are never reprocessed.

Option --ocr-scanned: scanned PDFs (no text layer) go through the full
Docling VLM pipeline (heavy path, reserved for the residual).

Output (authorized zone): data/ocr_results.jsonl
  {fig_ref, cid, fig_id, ocr_md, model, ok, error, ts}

Run:
    uv run src/data_conversion/heures_supp/steps/step3_ocr.py
    uv run src/data_conversion/heures_supp/steps/step3_ocr.py --max-images 20
    uv run src/data_conversion/heures_supp/steps/step3_ocr.py --ocr-scanned
"""
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd

# Make the heures_supp package importable when run as a script
# (package root: src/data_conversion; no package install needed).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from heures_supp.core import config, utils  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("s3fs").setLevel(logging.WARNING)

OCR_RESULTS_FILE = config.DATA_PATH + "ocr_results.jsonl"
CHECKPOINT_EVERY = 25

MIME_BY_EXT = utils.MIME_BY_EXT
OCRABLE_EXT = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "svg"}


# ------------------------------------------------------------------- helpers
def collect_queue(ocr_scanned: bool) -> list:
    """Collect the OCR queue from the step 2 shards."""
    items = []
    try:
        shard_files = [
            f for f in config.fs.ls(config.DATA_PATH, detail=False)
            if "/shard_" in f and f.endswith(".parquet")
            and "final_shard" not in f
        ]
    except (FileNotFoundError, IsADirectoryError):
        logger.error(f"No shards found in {config.DATA_PATH} (run step 2 first).")
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
    fig_refs processed successfully. Failures (transient or permanent) are
    NOT marked as done: a re-run of step 3 retries the failures (permanent
    failures like EMF without a rasterizer are cheap).
    """
    return {r["fig_ref"] for r in utils.read_jsonl(OCR_RESULTS_FILE) if r.get("ok")}


def rasterize_vector(data: bytes, ext: str):
    """
    Try to rasterize an EMF/WMF via ImageMagick. Returns (bytes, mime) or
    None if impossible (the caller then marks a skip).
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
                check=True, capture_output=True, timeout=240,
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
    Full OCR of a scanned PDF via the Docling VLM pipeline (same settings
    as the old conversion chain).
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import VlmConvertOptions, VlmPipelineOptions
    from docling.datamodel.vlm_engine_options import ApiVlmEngineOptions, VlmEngineType
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.pipeline.vlm_pipeline import VlmPipeline

    raw = utils.download_bytes(docx_path)
    if raw is None:
        raise RuntimeError("could not download the PDF")

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
    # docling expects a path (or DocumentStream): go through a temp file
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
                    "ok": False, "error": "image_download_failed", "ts": ts}

        ext = item["fmt"]
        if ext in ("emf", "wmf"):
            r = rasterize_vector(data, ext)
            if r is None:
                return {**base, "ocr_md": None, "model": None,
                        "ok": False, "error": "emf_wmf_rasterizer_unavailable", "ts": ts}
            data, mime = r
        elif ext in MIME_BY_EXT:
            mime = MIME_BY_EXT[ext]
        else:
            return {**base, "ocr_md": None, "model": None,
                    "ok": False, "error": f"image_format_not_ocrable:{ext}", "ts": ts}

        # deterministic resize / re-encoding before sending to the VLM
        data, mime = utils.prepare_image_for_ocr(data, ext)
        md = utils.clean_ocr_md(utils.llm_ocr_image(data, mime))
        return {**base, "ocr_md": md, "model": config.LLM_MODEL,
                "ok": True, "error": None, "ts": ts}
    except Exception as e:
        return {**base, "ocr_md": None, "model": None,
                "ok": False, "error": f"{type(e).__name__}: {e}", "ts": ts}


def main():
    parser = argparse.ArgumentParser(description="Step 3: OCR of the kept images")
    parser.add_argument("--max-images", type=int, default=0,
                        help="Max number of OCR calls (0 = all)")
    parser.add_argument("--ocr-scanned", action="store_true",
                        help="Also process scanned PDFs via Docling (heavy)")
    args = parser.parse_args()

    # raw document paths (for the scanned PDFs)
    sel = pd.read_parquet(config.SELECTION_FILE, filesystem=config.fs)
    doc_paths = dict(zip(sel["cid"], sel["docx_path"]))

    queue = collect_queue(ocr_scanned=args.ocr_scanned)
    done = done_refs()
    todo = [q for q in queue if q["fig_ref"] not in done]
    if args.max_images > 0:
        todo = todo[: args.max_images]

    logger.info(
        f"OCR queue: {len(queue)} total, {len(done)} already done, "
        f"{len(todo)} to process now."
    )
    if not todo:
        logger.info("Nothing to do.")
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
                    raise RuntimeError(f"Could not write: {OCR_RESULTS_FILE}")
                logger.info(f"OCR: {i + 1}/{len(todo)} (ok={n_ok}, err={n_err})")

    logger.info(f"Step 3 done: {n_ok} OK, {n_err} errors. Output: {OCR_RESULTS_FILE}")


if __name__ == "__main__":
    main()
