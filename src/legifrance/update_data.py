import argparse
import asyncio
import logging

import numpy as np
import pandas as pd

import config
import scrap_metadata
import scrap_texts

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


def fetch_update_dates(parquet_path: str):
    """
    Init a dictionary describing the parquet at parquet_path with the following keys:
        - min_diff_month: last month of update in the parquet
        - min_diff_year: last year of update in the parquet
        - min_sign_month: first month of dateSignature in the parquet
        - min_sign_year: first year of dateSignature in the parquet
        - max_sign_month: last month of dateSignature in the parquet
        - max_sign_year: last year of dateSignature in the parquet

    The dictionary will be updated with new keys in update_data().
    """
    try:
        old_df = pd.read_parquet(parquet_path, filesystem=config.fs)
    except Exception:
        logger.error("Please provide a valid path to a .parquet table.")

    old_df["dateDiffusion"] = pd.to_datetime(old_df["dateDiffusion"])
    old_df["dateSignature"] = pd.to_datetime(old_df["dateSignature"])

    dates = {}

    dates["min_diff_month"] = old_df["dateDiffusion"].dt.month.max()
    dates["min_diff_year"] = old_df["dateDiffusion"].dt.year.max()
    dates["min_sign_month"] = old_df["dateSignature"].dt.month.min()
    dates["min_sign_year"] = old_df["dateSignature"].dt.year.min()
    dates["max_sign_month"] = old_df["dateSignature"].dt.month.max()
    dates["max_sign_year"] = old_df["dateSignature"].dt.year.max()

    return dates


def fetch_new_metadata(dates: dict):
    """
    Find new metadata that has been shared but not yet downloaded.
    Search for them only in the desired time interval.
    """
    max_sign_end_day = scrap_metadata.end_of_month(dates["max_sign_year"], dates["max_sign_month"])
    max_diff_end_day = scrap_metadata.end_of_month(dates["max_diff_year"], dates["max_diff_year"])

    # Date filters
    start_sign = f"{dates['min_sign_year']:04d}-{dates['min_sign_month']:02d}-01"
    end_sign = f"{dates['max_sign_year']:04d}-{dates['max_sign_month']:02d}-{max_sign_end_day:02d}"
    start_diff = f"{dates['min_diff_year']:04d}-{dates['min_diff_month']:02d}-01"
    end_diff = f"{dates['max_diff_year']:04d}-{dates['max_diff_month']:02d}-{max_diff_end_day:02d}"

    # Payload for /search
    date_filters = [
        {
            "dates": {
                "start": start_sign,
                "end": end_sign
            },
            "facette": "DATE_SIGNATURE"
        },
        {
            "dates": {
                "start": start_diff,
                "end": end_diff
                },
            "facette": "DATE_DIFFUSION"
        }
    ]

    new_metadata = scrap_metadata.download_metadata_filtered(date_filters)

    return new_metadata


def update_acco_database(new_acco, parquet_path):
    """
    Add new collected metadata to the parquet file. Remove potential duplicates.
    """
    old_df = pd.read_parquet(parquet_path, filesystem=config.fs)
    new_acco = pd.DataFrame(new_acco)
    updated_df = pd.concat([old_df, new_acco], ignore_index=True).drop_duplicates()
    updated_df.to_parquet(parquet_path, filesystem=config.fs)


def fetch_missing_doc_data(ids_to_consult: list):
    """
    Download the documents for the cids ids_to_consult in base64 format (for .docx).
    """
    payloads = [{"id": cid} for cid in ids_to_consult]
    docs = asyncio.run(scrap_texts.download_docs_by_batch(payloads))
    docs_acco = [{"id": cid, "content_b64": doc} for cid, doc in zip(ids_to_consult, docs)]

    return docs_acco


def group_ids_by_month(ids: np.ndarray, sign_dates: np.ndarray):
    """
    Create group of ids, each group corresponding to a specific month in a specific year.
    """
    ids = np.array(ids)
    unique_sign_dates = np.unique(sign_dates)
    grouped_ids = {date: ids[sign_dates == date] for date in unique_sign_dates}

    return grouped_ids


def update_data(
    max_diff_year,
    max_diff_month,
    parquet_path,
    update_docx
):
    """
    Collect new metadata (always) and documents (if update_docx) and update the databases.
    """
    dates = fetch_update_dates(parquet_path)

    dates["max_diff_year"] = max_diff_year
    dates["max_diff_month"] = max_diff_month

    assert dates["max_diff_year"] >= dates["min_diff_year"], ValueError("The metadata is already updated for that year")

    if dates["max_diff_year"] == dates["min_diff_year"]:
        assert dates["max_diff_month"] >= dates["min_diff_month"], ValueError("The metadata is already updated for that month")

    new_metadata = fetch_new_metadata(dates)

    update_acco_database(new_metadata, parquet_path)

    if update_docx:
        ids = [acco["cid"] for acco in new_metadata]

        # Group by dates (format YYYY-MM-DDT00:00:00.000+0000) for batch export
        sign_dates = [acco["dateSignature"][:7] for acco in new_metadata]
        grouped_ids = group_ids_by_month(ids, sign_dates)

        # Fetch and export
        for sign_date, ids_to_consult in grouped_ids.items():
            scrap_texts.context_vars["year"], scrap_texts.context_vars["month"] = sign_date.split("-")
            documents = fetch_missing_doc_data(ids_to_consult)
            scrap_texts.upload_batch_to_s3(documents=documents)


def main():
    parser = argparse.ArgumentParser(
        description="Scrapping of ACCO by month and year"
    )

    parser.add_argument(
        "--parquet_path",
        type=str,
        required=True,
        help="Path to the parquet containing the metadata you want to update"
    )

    parser.add_argument(
        "--max_diff_year",
        type=int,
        required=True,
        help="Maximum year desired for the update"
    )

    parser.add_argument(
        "--max_diff_month",
        type=int,
        required=True,
        help="Maximum month desired for the update"
    )

    parser.add_argument(
        "--update_docx",
        type=bool,
        required=False,
        default=True,
        help="Download the new documents in the document directory set in config"
    )

    args = parser.parse_args()

    update_data(
        max_diff_year=args.max_diff_year,
        max_diff_month=args.max_diff_month,
        parquet_path=args.parquet_path,
        update_docx=args.update_docx
        )


if __name__ == "__main__":
    main()
