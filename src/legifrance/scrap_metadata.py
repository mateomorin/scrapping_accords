import argparse
import logging
import time

import httpx
import numpy as np
import pandas as pd
from tqdm import tqdm

import config
from legifrance_api_client import LegiFranceClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_months(value):
    """
    Convert the argument --month to "all" or to a list of int (1-12).
    """
    if value.lower() == "all":
        return "all"

    try:
        months = [int(m.strip()) for m in value.replace(',', ' ').split()]
        for m in months:
            if not 1 <= m <= 12:
                raise argparse.ArgumentTypeError(f"Month {m} must be set between 1 and 12.")
        return months
    except ValueError:
        raise argparse.ArgumentTypeError("Months must be 'all' or integers split by commas.")


def parse_year(value):
    """
    Convert the argument --year to "all" or to an int.
    """
    if value.lower() == "all":
        return "all"

    try:
        year = int(value)
        return year
    except ValueError:
        raise argparse.ArgumentTypeError("The year must be 'all' are an integer.")


def retrieve_data(accos: list):
    """
    Extracts the useful information from the dict 'results' of the json.
    """
    metadata_accos = []

    for acco in accos:
        title_1 = acco["titles"][0]
        if len(acco["titles"]) > 1:
            logger.warning("Several titles have been detected for a single page call.")
        metadata_to_keep = {}
        metadata_to_keep["cid"] = title_1["cid"]
        metadata_to_keep["id"] = title_1["id"]
        metadata_to_keep["title"] = title_1["title"]
        metadata_to_keep["legalStatus"] = title_1["legalStatus"]
        metadata_to_keep["startDate"] = title_1["startDate"]
        metadata_to_keep["endDate"] = title_1["endDate"]
        metadata_to_keep["nature"] = title_1["nature"]
        metadata_to_keep["dateSignature"] = acco["dateSignature"]
        metadata_to_keep["dateDiffusion"] = acco["dateDiffusion"]
        metadata_to_keep["conforme"] = acco["conforme"]
        for i, theme in enumerate(acco["themes"]):
            metadata_to_keep[f"theme_{i+1}"] = theme
        metadata_accos.append(metadata_to_keep)

    return metadata_accos


def end_of_month(year: int, month: int):
    """
    Returns the final day of the month for any month in any year.
    """
    assert month in range(1, 13), ValueError("month is not valid")
    if month in [1, 3, 5, 7, 8, 10, 12]:
        return 31
    elif month != 2:
        return 30
    else:
        if (year % 4) == 0 and (year % 100 != 0 or year % 400 == 0):
            return 29
        else:
            return 28


def download_metadata_with_retry(client, payload, pageNumber):
    """
    Trying to download metadata for a specific payload, with retries.
    401 errors often happen for some filters, so this allows multiple tries.
    Waiting time grows exponentially with each retry.
    """
    status_code_errors = 0
    request_errors = 0
    for attempt in range(config.MAX_RETRIES):
        # In case of httpx errors
        try:
            response = client.search(payload=payload)

            # In case of unexpected status code
            if response.status_code == 200:
                return response, attempt, status_code_errors, request_errors
            else:
                status_code_errors += 1
                time.sleep(2 * (attempt + 1))

        except httpx.RequestError:
            request_errors += 1
            time.sleep(2 * (attempt + 1))

    logger.warning(f"Too many unsuccessful trials at page {pageNumber}. Stopping...")
    return None, attempt, status_code_errors, request_errors


def check_data_length(metadata_list, theoretical_length):
    """
    Check for data length and for data id uniqueness.
    """

    # Validation
    if len(metadata_list) != theoretical_length:
        logger.warning("The displayed number of elements is not the same after scrapping.")
        logger.info(f"Length of the scrapped data: {len(metadata_list)}")
        logger.info(f"Expected number of elements {theoretical_length}")
        logger.info(f"{metadata_list[-1]}")

        return False

    ids = [data["cid"] for data in metadata_list]

    if len(set(ids)) != len(ids):
        logger.warning("The data that has been retrieved contains duplicates.")
        logger.info(f"Unique cids : {len(set(ids))}")
        logger.info(f"Length of the data : {len(ids)}")
        return False

    return True


def create_sign_date_filter(year, month):
    """
    Create simple sign date filters for payloads.
    """
    end_day = end_of_month(year, month)

    sign_date_filter = [
        {
            "dates": {
                "start": f"{year:04d}-{month:02d}-01",
                "end": f"{year:04d}-{month:02d}-{end_day:02d}"
            },
            "facette": "DATE_SIGNATURE"
        }
    ]

    return sign_date_filter


def download_metadata_filtered(filters: list):
    """
    Download all metadata respecting filters.
    """
    client = LegiFranceClient()

    # Payload for search/
    payload = {
        "recherche": {
            "filtres": filters,
            "sort": "DATE_ASC",
            "secondSort": "ID_ASC",
            "fromAdvancedRecherche": False,
            "pageSize": 100,
            "typePagination": "DEFAUT",
            "pageNumber": 1
        },
        "fond": "ACCO"
    }

    # Retrieve the number of elements to validate at the end and compute max pages
    response = download_metadata_with_retry(client, payload, 1)[0]
    if not response:
        raise Exception("Requests for the number of results were unsuccesful")
    totalResultNumber = response.json()['totalResultNumber']
    max_pages = int(np.ceil(totalResultNumber/100))

    total_missed_attempts = 0
    total_status_code_errors = 0
    total_request_errors = 0
    metadata_list = []

    # Iterate through pages
    for pageNumber in tqdm(range(1, max_pages + 1)):

        # Request a specific page
        payload["recherche"]["pageNumber"] = pageNumber
        response, attempt, status_errors, req_errors = download_metadata_with_retry(client, payload, pageNumber)
        metadata = response.json().get("results", [])

        # Update stats
        total_missed_attempts += attempt
        total_status_code_errors += status_errors
        total_request_errors += req_errors

        # If too many unsuccesful tries or missing data, skip
        if metadata is None:
            logger.warning(f"Response is invalid for page {pageNumber}. Skipping...")
            continue
        if len(metadata) == 0:
            logger.warning(f"No metadata found at page {pageNumber}. Skipping...")
            continue

        new_data = retrieve_data(metadata)
        metadata_list += new_data

    check_data_length(metadata_list=metadata_list, theoretical_length=totalResultNumber)

    return metadata_list


def save_metadata_to_parquet(metadata, file_name):
    """
    Save metadata to parquet.
    """
    df_metadata = pd.DataFrame(metadata)
    df_metadata.to_parquet(f"{config.METADATA_PATH}{file_name}.parquet", filesystem=config.fs)


def scrap_all_acco():
    """
    Scrap all ACCO metadata from 2017/09/01 (ACCO creation) to 2025/12/31.
    Be careful, the save only arrives at the end, a simple error might make you lose all data.
    """
    all_acco_metadata = []

    logger.info("=========================== YEAR 2017 ============================")
    for month in range(9, 13):
        logger.info(f"------------------------ MONTH {month:02d} ----------------------------")
        sign_date_filter = create_sign_date_filter(2017, month)
        all_acco_metadata += download_metadata_filtered(sign_date_filter)

    for year in range(2018, 2026):
        logger.info(f"=========================== YEAR {year} ============================")
        for month in range(1, 13):
            logger.info(f"------------------------ MONTH {month:02d} ----------------------------")
            sign_date_filter = create_sign_date_filter(year, month)
            all_acco_metadata += download_metadata_filtered(sign_date_filter)

    save_metadata_to_parquet(all_acco_metadata, "acco_metadata_2017_2025")

    return all_acco_metadata


def scrap_specific_months(year, months):
    """
    Scrap ACCO metadata for specific/all month(s) in a specific year.
    """
    acco_metadata = []

    if months == "all":
        months = range(1, 13)
        filename = f"acco_metadata_{year}"
    else:
        assert isinstance(months, list)
        filename = f"acco_metadata_{year}_{"_".join(map(str, months))}"

    for month in months:
        logger.info(f"------------------------ {year}/{month:02d} ----------------------------")
        sign_date_filter = create_sign_date_filter(year, month)
        acco_metadata += download_metadata_filtered(sign_date_filter)

    save_metadata_to_parquet(acco_metadata, filename)


def main():
    parser = argparse.ArgumentParser(
        description="Scrapping of ACCO by month and year"
    )

    parser.add_argument(
        "--year",
        type=parse_year,
        required=True,
        help="Specific year or 'all'"
    )

    parser.add_argument(
        "--month",
        type=parse_months,
        default="all",
        help="Month list (ex: '1,2,3' ou '1 2 3') or 'all' (default: 'all')"
    )

    args = parser.parse_args()

    if args.year == "all":
        logger.info("Start of full scraping (2017-2025)...")
        scrap_all_acco()
    else:
        logger.info(f"Start of scraping for year {args.year}...")
        scrap_specific_months(args.year, args.month)


if __name__ == "__main__":
    main()
