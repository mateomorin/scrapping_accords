import os

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    VlmConvertOptions,
    VlmPipelineOptions,
)
from docling.datamodel.vlm_engine_options import ApiVlmEngineOptions, VlmEngineType
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.vlm_pipeline import VlmPipeline
from dotenv import load_dotenv
from markitdown import MarkItDown
from openai import OpenAI
import s3fs

load_dotenv(override=True)

# Storage
fs = s3fs.S3FileSystem(
    endpoint_url="https://minio.lab.sspcloud.fr",
    client_kwargs={"region_name": "us-east-1"},
)

METADATA_PATH = "s3://mateomorin/legifrance/metadata/"
DOCUMENTS_PATH = "s3://mateomorin/legifrance/documents/"
FULL_DATA_PATH = "s3://mateomorin/legifrance/data/"
LOGS_PATH = "s3://mateomorin/legifrance/logs/"

# LLM Model
LLM_MODEL = "gemma4-26b-moe"
# Only for markitdown
LLM_PROMPT = """
Ecris tout le texte que tu vois sur l'image, en préservant la structure des paragraphes, des titres, et des potentiels tableaux.

Veille à respecter l'ortographe, la syntaxe et la police de caractère (gras, itallique, ...).

Si l'image comporte un schéma, décris ce schéma.

TOUT DOIT ÊTRE ECRIT EN FORMAT MARKDOWN
"""

llm_client = OpenAI(
    base_url=os.environ["LLM_API_URL"],
    api_key=os.environ["LLM_API_KEY"],
)

# MarkItDown converters
llm_converter = MarkItDown(
    enable_plugins=True,
    llm_client=llm_client,
    llm_model=LLM_MODEL,
    llm_prompt=LLM_PROMPT,
)

basic_converter = MarkItDown(
    enable_plugins=False
)

# Docling LLM converter
engine_options = ApiVlmEngineOptions(
    runtime_type=VlmEngineType.API,
    url=f"{os.environ['LLM_API_URL'].rstrip('/')}/chat/completions",
    headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
    params={
        "model": "gemma4-26b-moe",
        "temperature": 0.0,
    },
    timeout=120,
    concurrency=10
)

vlm_options = VlmConvertOptions.from_preset(
    "gemma_27b",
    engine_options=engine_options,
)

pipeline_options = VlmPipelineOptions(
    vlm_options=vlm_options,
    enable_remote_services=True,
)

docling_llm_converter = DocumentConverter(
    format_options={
        InputFormat.PDF: PdfFormatOption(
            pipeline_cls=VlmPipeline,
            pipeline_options=pipeline_options,
        )
    }
)

# Async
MAX_CONCURRENCY_DOC = 20

# --- Sharding / checkpointing ---
# Nombre de documents traités par "shard" : c'est à la fois la fréquence de
# sauvegarde intermédiaire sur S3 (checkpoint) ET la taille des fichiers de
# sortie. À ajuster après un premier run en observant la taille réelle d'un
# fichier shard_*.parquet (viser << 1 Go pour rester large sous la limite).
SHARD_SIZE = 2000

# --- Retry écriture S3 ---
# En cas d'échec d'écriture (shard ou log d'erreurs) sur S3, on retente avec
# un backoff exponentiel avant d'abandonner (ce qui fait échouer le job pour
# que Argo puisse le relancer proprement grâce à la reprise par shard).
S3_WRITE_RETRIES = 4
S3_WRITE_RETRY_BASE_DELAY = 5  # secondes ; délai = BASE_DELAY * 2**tentative