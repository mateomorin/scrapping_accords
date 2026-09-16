import s3fs

fs = s3fs.S3FileSystem(
    endpoint_url="https://minio.lab.sspcloud.fr",
    client_kwargs={"region_name": "us-east-1"},
)

# API calls
MAX_RETRIES = 5         # all
MAX_CONCURRENCY = 10    # async

# Storage
METADATA_PATH = "s3://mateomorin/legifrance/metadata/"
DOCUMENTS_PATH = "s3://mateomorin/legifrance/documents/"
