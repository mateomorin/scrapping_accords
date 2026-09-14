import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

import boto3
from botocore.config import Config
import pandas as pd
import s3fs
from tqdm.asyncio import tqdm_asyncio

from legifrance_api_async import LegiFranceAPIClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

fs = s3fs.S3FileSystem(
    endpoint_url="https://minio.lab.sspcloud.fr",
    client_kwargs={"region_name": "us-east-1"},
)

boto3_config = Config(
        max_pool_connections=50,
        retries={'max_attempts': 3, 'mode': 'standard'}
    )
boto_client = boto3.client(
    "s3",
    endpoint_url="https://minio.lab.sspcloud.fr",
    region_name="us-east-1",
    config=boto3_config
)

MAX_RETRIES = 5
CONCURRENCY_LIMIT = 10
METADATA_PATH = "s3://mateomorin/legifrance/metadata/"
DOCUMENTS_PATH = "s3://mateomorin/legifrance/documents/"

# Change year/month before running exports
context_vars = {
    "year": "YYYY",
    "month": "MM"
}


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


def retrieve_table_url(year, month):
    """
    Either the file is in format 'acco_metadata_YYYY.parquet' if it contains every month of the year.
    Or it is in format 'acco_metadata_YYYY_MM_MM_MM.parquet', each 'MM' standing for a particular month.
    'MM' can also be 'M' if the month is < 10.
    """
    existing_files = fs.ls(METADATA_PATH)

    # Filter .parquet
    existing_tables = [file for file in existing_files if file.endswith(".parquet")]

    # Filter year
    existing_year = [table for table in existing_tables if str(year) in table]

    for file_name in existing_year:
        if f"acco_metadata_{year}.parquet" in file_name:
            return f"acco_metadata_{year}.parquet"

    # Filter month
    existing_month = [table for table in existing_year if f"_{month}" in table]

    if len(existing_month) > 0:
        return existing_month[0].split("/")[-1]

    logger.error(f"No table found corresponding to the month {year}/{month:02d}")


def retrieve_ids(year, month):
    path_to_table = METADATA_PATH + retrieve_table_url(year, month)

    df_table = pd.read_parquet(path_to_table, filesystem=fs)

    df_table["dateSignature"] = pd.to_datetime(df_table["dateSignature"])

    ids = df_table[df_table["dateSignature"].dt.month == 11]["cid"].to_list()

    return ids


async def fetch_with_retry(client, payload, semaphore):
    """
    Usually, 401 errors happen for some filters, so this allows multiple tries.
    Cannot refresh client for the page because it might reset the order.
    """
    async with semaphore:
        for attempt in range(MAX_RETRIES):
            response = await client.download_acco(payload=payload)

            if response.status_code == 200:
                return response.json()["acco"]["data"]

            # Unexpected error
            else:
                logger.warning(f"Error {response.status_code} at id {payload['id']} (Trial {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(2 * (attempt + 1))

        logger.warning(f"Too many unsuccessful trials for id {payload['id']}. Stopping...")
        return None


async def run_batch(payloads):
    async with LegiFranceAPIClient() as client:
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        tasks = [fetch_with_retry(client, payload, semaphore) for payload in payloads]

        results = await tqdm_asyncio.gather(*tasks)
        return results


def consult_month(year: int, month: int):
    ids_to_consult = retrieve_ids(year, month)

    payloads = [{"id": cid} for cid in ids_to_consult]

    docs = asyncio.run(run_batch(payloads))

    docs_acco = [{"id": cid, "content_b64": doc} for cid, doc in zip(ids_to_consult, docs)]

    return docs_acco


def upload_single_document(item):
    doc_id = item['id']
    content_b64 = item['content_b64']

    try:
        binary_data = base64.b64decode(content_b64)

        boto_client.put_object(
            Bucket="mateomorin",
            Key=f"legifrance/documents/{context_vars['year']}/{context_vars['month']}/{doc_id}.docx",
            Body=binary_data,
            ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        return doc_id, True, None

    except Exception as e:
        return doc_id, False, str(e)


def upload_batch_to_s3(documents, max_workers=30):
    """
    For massive export to S3.
    """
    results = {"success": 0, "failed": 0, "errors": []}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(upload_single_document, doc) for doc in documents]

        for future in as_completed(futures):
            doc_id, success, err = future.result()
            if success:
                results["success"] += 1
            else:
                results["failed"] += 1
                results["errors"].append((doc_id, err))

    return results


def scrap_all_acco():

    logger.info("=========================== YEAR 2017 ============================")
    context_vars["year"] = 2017
    for month in range(9, 13):
        logger.info(f"------------------------ MONTH {month:02d} ----------------------------")
        documents = consult_month(2017, month)
        context_vars["month"] = f"{month:02d}"
        upload_batch_to_s3(documents=documents)

    for year in range(2018, 2026):
        logger.info(f"=========================== YEAR {year} ============================")
        context_vars["year"] = year
        for month in range(1, 13):
            logger.info(f"------------------------ MONTH {month:02d} ----------------------------")
            documents = consult_month(year, month)
            context_vars["month"] = f"{month:02d}"
            logger.info("Exportation...")
            upload_batch_to_s3(documents=documents)


def scrap_specific_months(year, months):
    if months == "all":
        months = range(1, 13)
    else:
        assert isinstance(months, list)
    context_vars["year"] = year
    for month in months:
        logger.info(f"------------------------ {year}/{month:02d} ----------------------------")
        documents = consult_month(year, month)
        context_vars["month"] = f"{month:02d}"
        logger.info("Exportation...")
        upload_batch_to_s3(documents=documents)


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
