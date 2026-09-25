"""
Provided a metadata database, fetch all documents linked to it and apply a dox to markdown transformation.
If a docx document contains images, convert it to pdf and use LLM-based OCR. 
"""
import argparse
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

import pandas as pd
from tqdm import tqdm
import xml.etree.ElementTree as ET

import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("httpx2").setLevel(logging.WARNING)


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

    cmd = [
        'libreoffice',
        '--headless',
        '--convert-to', 'pdf',
        '--outdir', tmp_dir,
        cleaned_docx
    ]

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)

    markdown = config.llm_converter.convert(expected_pdf).markdown

    # Clean-up
    if os.path.exists(cleaned_docx):
        os.remove(cleaned_docx)

    return markdown, expected_pdf


def convert_docx_to_markdown(doc_path: str, tmp_dir: str):
    filename = doc_path.split("/")[-1]
    local_docx = os.path.join(tmp_dir, filename)
    local_pdf = None

    try:
        config.fs.get(doc_path, local_docx)

        has_images = docx_body_has_images_fast(local_docx=local_docx)
        if not has_images:
            markdown = basic_conversion(local_docx=local_docx)
        else:
            markdown, local_pdf = llm_conversion(local_docx=local_docx, tmp_dir=tmp_dir)

        return markdown, has_images

    finally:
        # Always clear files at the end
        if os.path.exists(local_docx):
            os.remove(local_docx)
        if local_pdf and os.path.exists(local_pdf):
            os.remove(local_pdf)


def convert_all_docx_to_markdown(doc_paths: list[str]):
    markdowns = []
    total_docs_with_images = 0

    # Unique temp dir for document creation
    with tempfile.TemporaryDirectory() as tmp_dir:
        for doc_path in tqdm(doc_paths):
            markdown, has_images = convert_docx_to_markdown(doc_path=doc_path, tmp_dir=tmp_dir)

            markdowns.append(markdown)
            total_docs_with_images += has_images

    return markdowns, total_docs_with_images


def configure_output_name(output_name: str, metadata_name: str):
    if output_name == "default":
        output_name = metadata_name.replace("metadata", "data")

    return output_name


def export_full_data(data: pd.DataFrame, output_name: str):
    data.to_parquet(config.FULL_DATA_PATH + output_name)


def main():
    parser = argparse.ArgumentParser(
        description="Scrapping of ACCO by month and year"
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
        help="Name of the output file (parquet), or 'default'"
    )

    args = parser.parse_args()

    metadata = fetch_metadata(metadata_name=args.metadata_name)

    doc_paths = build_doc_paths(metadata=metadata)

    markdowns, total_docs_with_images = convert_all_docx_to_markdown(doc_paths=doc_paths)

    logger.info(f"Total document with images treated: {total_docs_with_images}.")

    metadata["markdown"] = markdowns

    output_name = configure_output_name(
        output_name=args.output_name,
        metadata_name=args.metadata_name
    )

    export_full_data(data=metadata, output_name=output_name)


if __name__ == "__main__":
    main()
