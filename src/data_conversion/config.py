import os

from dotenv import load_dotenv
from markitdown import MarkItDown
from openai import OpenAI
import s3fs

load_dotenv(override=True)

LLM_MODEL = "gemma4-26b-moe"

llm_client = OpenAI(
    base_url=os.environ["LLM_API_URL"],
    api_key=os.environ["LLM_API_KEY"],
)

llm_converter = MarkItDown(
    enable_plugins=True,
    llm_client=llm_client,
    llm_model=LLM_MODEL,
    llm_prompt="Ecris tout le texte que tu vois sur l'image, en préservant la structure des paragraphes et des titres. Veille à respecter l'ortographe, la syntaxe et la police de caractère (gras, itallique, ...). TOUT DOIT ÊTRE ECRIT EN FORMAT MARKDOWN",
)

basic_converter = MarkItDown(
    enable_plugins=False
)

fs = s3fs.S3FileSystem(
    endpoint_url="https://minio.lab.sspcloud.fr",
    client_kwargs={"region_name": "us-east-1"},
)

# Storage
METADATA_PATH = "s3://mateomorin/legifrance/metadata/"
DOCUMENTS_PATH = "s3://mateomorin/legifrance/documents/"
