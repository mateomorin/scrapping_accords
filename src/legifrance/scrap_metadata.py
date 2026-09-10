import argparse
import logging
import time

import numpy as np
import pandas as pd
import s3fs
from tqdm import tqdm

from legifrance_api_client import LegiFranceAPIClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

fs = s3fs.S3FileSystem(
    endpoint_url="https://minio.lab.sspcloud.fr",
    client_kwargs={"region_name": "us-east-1"},
)

MAX_RETRIES = 5


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
    Convert the argument --year to "all" or to an int between 2017 and 2025.
    """
    if value.lower() == "all":
        return "all"

    try:
        year = int(value)
        if not 2017 <= year <= 2025:
            raise argparse.ArgumentTypeError("The year must be set between 2017 and 2025.")
        return year
    except ValueError:
        raise argparse.ArgumentTypeError("The year must be 'all' are an integer.")


def retrieve_data(accos: list):
    """
    Extracts the useful information from the dict 'results' of the json.
    """
    data_accos = []

    for acco in accos:
        title_1 = acco["titles"][0]
        if len(acco["titles"]) > 1:
            logger.warning("Several titles here")
        data_to_keep = {}
        data_to_keep["cid"] = title_1["cid"]
        data_to_keep["id"] = title_1["id"]
        data_to_keep["title"] = title_1["title"]
        data_to_keep["legalStatus"] = title_1["legalStatus"]
        data_to_keep["startDate"] = title_1["startDate"]
        data_to_keep["endDate"] = title_1["endDate"]
        data_to_keep["nature"] = title_1["nature"]
        data_to_keep["dateSignature"] = acco["dateSignature"]
        data_to_keep["dateDiffusion"] = acco["dateDiffusion"]
        data_to_keep["conforme"] = acco["conforme"]
        for i, theme in enumerate(acco["themes"]):
            data_to_keep[f"theme_{i+1}"] = theme
        data_accos.append(data_to_keep)

    return data_accos


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


def multiple_tries(client, payload, pageNumber):
    """
    Usually, 401 errors happen for some filters, so this allows multiple tries.
    Cannot refresh client for the page because it might reset the order.
    """
    for attempt in range(MAX_RETRIES):
        response = client.search(payload=payload)
        match response.status_code:
            case 200:
                return response
            case 401:
                logger.warning(f"Error 401 at page {pageNumber} (Trial {attempt + 1}/{MAX_RETRIES})")

                # Stop the process if too many retries
                if attempt + 1 == MAX_RETRIES:
                    logger.warning("Too many trials. Stopping...")
                    response = 401
                    break

                time.sleep(2 * (attempt + 1))
            case 503:
                logger.warning("No more pages (Error 503). Skipping...")
                response = None
                break
            case _:
                logger.error(f"Unusual error: {response}")
                response = None
                break

    return response


def search_month(year: int, month: int):
    client = LegiFranceAPIClient()
    data_accos = []
    pageNumber = 1
    end_day = end_of_month(year, month)

    payload = {
        "recherche": {
            "filtres": [{
                "dates": {
                    "start": f"{year:04d}-{month:02d}-01",
                    "end": f"{year:04d}-{month:02d}-{end_day:02d}"
                },
                "facette": "DATE_SIGNATURE"
            }],
            "sort": "ID",
            "fromAdvancedRecherche": False,
            "pageSize": 100,
            "typePagination": "DEFAUT",
            "pageNumber": 1
        },
        "fond": "ACCO"
    }

    # Retrieve the number of elements to validate at the end
    response = multiple_tries(client, payload, pageNumber)
    if (not response) or (response == 401) or (response.status_code != 200):
        raise Exception("Requests for the number of results were unsuccesful")

    totalResultNumber = response.json()['totalResultNumber']

    max_pages = int(np.ceil(totalResultNumber/100))

    for pageNumber in tqdm(range(1, max_pages + 1)):
        payload["recherche"]["pageNumber"] = pageNumber
        response = multiple_tries(client, payload, pageNumber)

        if response is None:
            break

        accos = response.json().get("results")
        if len(accos) == 0:
            break

        new_data = retrieve_data(accos)
        data_accos += new_data

    # Validation
    if len(data_accos) != totalResultNumber:
        logger.warning("The displayed number of elements is not the same after scrapping.")
        logger.info(f"Length of the scrapped data: {len(data_accos)}")
        logger.info(f"Expected number of elements {totalResultNumber}")
        logger.info(f"{data_accos[-1]}")

    return data_accos


def save_acco_to_parquet(acco, file_name):
    df_acco = pd.DataFrame(acco)
    df_acco.to_parquet(f"s3://mateomorin/legifrance/{file_name}.parquet", filesystem=fs)


def scrap_all_acco():
    all_acco = []

    logger.info("=========================== YEAR 2017 ============================")
    for month in range(9, 13):
        logger.info(f"------------------------ MONTH {month:02d} ----------------------------")
        all_acco += search_month(2017, month)

    for year in range(2018, 2026):
        logger.info(f"=========================== YEAR {year} ============================")
        for month in range(1, 13):
            logger.info(f"------------------------ MONTH {month:02d} ----------------------------")
            all_acco += search_month(year, month)

    save_acco_to_parquet("acco_metadata_2017_2025_new")

    return all_acco


def scrap_specific_months(year, months):
    acco = []

    if months == "all":
        months = range(1, 13)
        filename = f"acco_metadata_{year}"
    else:
        assert isinstance(months, list)
        filename = f"acco_metadata_{year}_{"_".join(map(str, months))}"

    for month in months:
        logger.info(f"------------------------ {year}/{month:02d} ----------------------------")
        acco += search_month(year, month)

    save_acco_to_parquet(acco, filename)


def main():
    parser = argparse.ArgumentParser(
        description="Scrapping of ACCO by month and year"
    )

    parser.add_argument(
        "--year",
        type=parse_year,
        required=True,
        help="Specific year (2017-2025) or 'all'"
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
