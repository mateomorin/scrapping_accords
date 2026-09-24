"""
Provided a metadata database, fetch all documents linked to it and apply a dox to markdown transformation.
If a docx document contains images, convert it to pdf and use LLM-based OCR. 
"""
import tempfile

import docx
import docx2pdf
import pandas as pd

import config


def count_images(doc_path: str):
    doc = docx.Document(doc_path)
    count = 0
    for par in doc.paragraphs:
        if 'graphicData' in par._p.xml or 'imagedata' in par._p.xml:
            count += 1

    return count


def basic_conversion(doc_path: str):
    with config.fs.open(doc_path, "rb") as f:
        markdown = config.basic_converter.convert(f).markdown

    return markdown


def llm_conversion(doc_path: str):
    with config.fs.open(doc_path, "rb") as f, tempfile.TemporaryFile() as pdf_path:
        docx2pdf.convert(f, pdf_path)
        markdown = config.llm_converter.convert(pdf_path).markdown

    return markdown


def convert_docx_to_markdown(doc_path: str):
    n_images = count_images(doc_path=doc_path)

    if n_images == 0:
        markdown = basic_conversion(doc_path=doc_path)
    else:
        markdown = llm_conversion(doc_path=doc_path)

    return markdown


def fetch_metadata(metadata_path: str):
    """
    metadata must have columns dateSignature and cid
    """
    metadata = pd.read_parquet(metadata_path, filesystem=config.fs)

    return metadata


def build_doc_paths(metadata: pd.DataFrame):
    """
    doc_paths must be of format DOCUMENTS_PATH/[YYYY]/[MM]/[document_cid].docx
    """
    metadata["dateSignature"] = metadata["dateSignature"].to_datetime()
    metadata["YYYY"] = metadata["dateSignature"].dt.year
    metadata["MM"] = metadata["dateSignature"].dt.month.str.format(r"2d0")

    return metadata[["cid", "year", "month"]]



