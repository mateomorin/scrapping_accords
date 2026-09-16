import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

import boto3
from botocore.config import Config
import httpx
import pandas as pd
from tqdm.asyncio import tqdm_asyncio

import config
from legifrance_api_client import AsyncLegiFranceClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


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
        return year
    except ValueError:
        raise argparse.ArgumentTypeError("The year must be 'all' are an integer.")


def retrieve_table_url(year: int, month: int):
    """
    Either the file is in format 'acco_metadata_YYYY.parquet' if it contains every month of the year.
    Or it is in format 'acco_metadata_YYYY_MM_MM_MM.parquet', each 'MM' standing for a particular month.
    'MM' can also be 'M' if the month is < 10.
    """
    existing_files = config.fs.ls(config.METADATA_PATH)

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


def retrieve_ids(year: int, month: int):
    """
    Retrieve all ids from metadata corresponding to a specific month in the year.
    """
    # Find url
    path_to_table = config.METADATA_PATH + retrieve_table_url(year, month)

    # Treat data 
    df_table = pd.read_parquet(path_to_table, filesystem=config.fs)
    df_table["dateSignature"] = pd.to_datetime(df_table["dateSignature"])

    # Fetch ids
    ids = df_table[df_table["dateSignature"].dt.month == month]["cid"].to_list()
    return ids


async def download_doc_with_retry(client, payload, semaphore):
    """
    Trying to download an acco for a specific payload, with retries.
    401 errors often happen for some filters, so this allows multiple tries.
    Waiting time grows exponentially with each retry.
    """
    async with semaphore:
        status_code_errors = 0
        request_errors = 0
        for attempt in range(config.MAX_RETRIES):
            # In case of httpx errors
            try:
                response = await client.download_acco(payload=payload)

                # In case of unexpected status code
                if response.status_code == 200:
                    return response.json()["acco"]["data"], attempt, status_code_errors, request_errors
                else:
                    status_code_errors += 1
                    await asyncio.sleep(2 * (attempt + 1))

            except httpx.RequestError:
                request_errors += 1
                await asyncio.sleep(2 * (attempt + 1))

        logger.warning(f"Too many unsuccessful trials for id {payload['id']}. Stopping...")
        return None, attempt, status_code_errors, request_errors


async def download_docs_by_batch(payloads):
    """
    Use a semaphore to send several requests in parallel and shorten runtime.
    """
    async with AsyncLegiFranceClient() as client:
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY)
        tasks = [download_doc_with_retry(client, payload, semaphore) for payload in payloads]
        results = await tqdm_asyncio.gather(*tasks)

        # Show stats
        total_missed_attempts = 0
        total_status_code_errors = 0
        total_request_errors = 0
        data_list = []

        for res in results:
            data, attempt, status_errors, req_errors = res

            if data is not None:
                data_list.append(data)

            total_missed_attempts += attempt
            total_status_code_errors += status_errors
            total_request_errors += req_errors

        logger.info(
            f"Total missed attempts : {total_missed_attempts} | "
            f"Total HTTP status errors : {total_status_code_errors} | "
            f"Total httpx request errors : {total_request_errors}"
        )

        return data_list


def download_doc_by_month(year: int, month: int):
    """
    Use legifrance consult API to retrieve all documents for a specific month in the year.
    """
    # Prepare payloads to call API consult/acco/
    ids_to_consult = retrieve_ids(year, month)
    payloads = [{"id": cid} for cid in ids_to_consult]

    # Download and treat data
    docs = asyncio.run(download_docs_by_batch(payloads))
    docs_acco = [{"id": cid, "content_b64": doc} for cid, doc in zip(ids_to_consult, docs)]

    return docs_acco


def upload_single_document(item):
    """
    Upload a document to s3 in format docx.
    The item should contain keys:
        - id: cid of the document (unique)
        - content_b64: binary data of the document in format base64
    """
    doc_id = item['id']
    content_b64 = item['content_b64']

    try:
        # Decode
        binary_data = base64.b64decode(content_b64)

        # Data upload
        split_path = config.DOCUMENTS_PATH.split("/")
        bucket_name = split_path[2]
        directory = "/".join(split_path[3:])
        boto_client.put_object(
            Bucket=bucket_name,
            Key=f"{directory}{context_vars['year']}/{context_vars['month']}/{doc_id}.docx",
            Body=binary_data,
            ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        return doc_id, True, None

    except Exception as e:
        logger.info(f"Document {doc_id} has not be exported due to unexpected error.")
        return doc_id, False, str(e)


def upload_batch_to_s3(documents, max_workers=30):
    """
    For fast massive export to S3.
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

    logger.info(f"{results['success']} documents uploaded! | "
                f"{results['failed']} fails")

    if results['failed'] >= 1:
        logger.info(f"Errors: \n{'\n'.join(results['errors'])}")


def scrap_all_acco():
    """
    Scrap all ACCO documents from 2017/09/01 (ACCO creation) to 2025/12/31.
    """

    logger.info("=========================== YEAR 2017 ============================")
    context_vars["year"] = 2017
    for month in range(9, 13):
        logger.info(f"------------------------ MONTH {month:02d} ----------------------------")
        documents = download_doc_by_month(2017, month)
        context_vars["month"] = f"{month:02d}"
        upload_batch_to_s3(documents=documents)

    for year in range(2018, 2026):
        logger.info(f"=========================== YEAR {year} ============================")
        context_vars["year"] = year
        for month in range(1, 13):
            logger.info(f"------------------------ MONTH {month:02d} ----------------------------")
            documents = download_doc_by_month(year, month)
            context_vars["month"] = f"{month:02d}"
            logger.info("Exportation...")
            upload_batch_to_s3(documents=documents)


def scrap_specific_months(year, months):
    """
    Scrap ACCO documents for specific/all month(s) in a specific year.
    """
    if months == "all":
        months = range(1, 13)
    context_vars["year"] = year
    for month in months:
        logger.info(f"------------------------ {year}/{month:02d} ----------------------------")
        documents = download_doc_by_month(year, month)
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
