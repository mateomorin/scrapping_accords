"""
Provided a metadata database, fetch all documents linked to it and apply a dox to markdown transformation.
If a docx document contains images, convert it to pdf and use LLM-based OCR. 
"""
import argparse
import tempfile

import docx
import docx2pdf
import pandas as pd
from tqdm import tqdm

import config


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


def count_images(doc_path: str):
    with config.fs.open(doc_path, "rb") as f:
        doc = docx.Document(f)
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
    with tempfile.TemporaryDirectory() as tmp_dir:
        local_docx = tmp_dir + "input.docx"
        local_pdf = tmp_dir + "output.pdf"

        config.fs.get(doc_path, local_docx)
        docx2pdf.convert(local_docx, local_pdf)

        markdown = config.llm_converter.convert(local_pdf).markdown

    return markdown


def convert_docx_to_markdown(doc_path: str):
    n_images = count_images(doc_path=doc_path)

    if n_images == 0:
        markdown = basic_conversion(doc_path=doc_path)
    else:
        markdown = llm_conversion(doc_path=doc_path)

    return markdown


def convert_all_docx_to_markdown(doc_paths: list[str]):
    markdowns = []
    for doc_path in tqdm(doc_paths):
        markdown = convert_docx_to_markdown(doc_path)
        markdowns.append(markdown)

    return markdowns


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

    markdowns = convert_all_docx_to_markdown(doc_paths=doc_paths)

    metadata["markdown"] = markdowns

    output_name = configure_output_name(
        output_name=args.output_name,
        metadata_name=args.metadata_name
    )

    export_full_data(data=metadata, output_name=output_name)


if __name__ == "__main__":
    main()
