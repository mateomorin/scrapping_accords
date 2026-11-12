"""
Pipeline configuration for the "Heures supplémentaires (contingent,
majoration)" (overtime - contingent, majoration) subset.

Read-only (never written to):
    - s3://mateomorin/legifrance/metadata/   (scraped metadata parquets)
    - s3://mateomorin/legifrance/documents/  (scraped raw documents)

Write (authorized zone ONLY, enforced by utils.safe_remote_path):
    - s3://mateomorin/legifrance/data_heures_supp/data/
    - s3://mateomorin/legifrance/data_heures_supp/logs/
"""
import os

from dotenv import load_dotenv
import s3fs

load_dotenv(override=True)

# --- S3 -----------------------------------------------------------------
S3_ENDPOINT = "https://minio.lab.sspcloud.fr"
S3_REGION = "us-east-1"

fs = s3fs.S3FileSystem(
    endpoint_url=S3_ENDPOINT,
    client_kwargs={"region_name": S3_REGION},
)

# Source paths (read-only)
METADATA_PATH = "s3://mateomorin/legifrance/metadata/"
DOCUMENTS_PATH = "s3://mateomorin/legifrance/documents/"

# Output root (only writable zone)
OUTPUT_ROOT = "s3://mateomorin/legifrance/data_heures_supp/"
DATA_PATH = OUTPUT_ROOT + "data/"
LOGS_PATH = OUTPUT_ROOT + "logs/"
FIGS_PATH = DATA_PATH + "figs/"  # figures kept by the triage (for step 3 OCR)

SELECTION_FILE = LOGS_PATH + "selection.parquet"
PROFILE_FILE = LOGS_PATH + "profile_subset.parquet"

# --- Selection (step 0) ---------------------------------------------------
# Boolean columns of the metadata table; a document belongs to the subset if
# at least one of them is True. (Column names are part of the upstream data
# contract: keep them as-is.)
THEME_COLUMNS = ["Heures supplémentaires (contingent, majoration)"]

# --- LLM (OCR of the kept images, step 3) ---------------------------------
# Compatible with the existing .env variables (LLM_API_URL/KEY) or, as a
# fallback, OPENAI_BASE_URL/OPENAI_API_KEY.
LLM_API_URL = os.environ.get("LLM_API_URL") or os.environ.get("OPENAI_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_OCR_MODEL", "gemma4-26b-moe")
OCR_TIMEOUT = 120

OCR_PROMPT = (
    "Transcris intégralement le contenu de cette image en format Markdown.\n"
    "- Si l'image contient un tableau, reproduis-le sous forme de tableau Markdown, "
    "avec les cellules vides effectivement vides (jamais de chaînes de \"|\").\n"
    "- Si l'image contient du texte, transcris-le en préservant titres, listes et paragraphes.\n"
    "- Si l'image est un schéma ou un graphique sans texte, décris-le brièvement en français.\n"
    "Réponds uniquement avec le Markdown, sans commentaire."
)

# --- Concurrency / sharding ------------------------------------------------
MAX_CONCURRENCY_DOWNLOAD = 10  # parallel S3 downloads (step 2, full run)
MAX_CONCURRENCY_OCR = 4        # parallel LLM calls (step 3)
SHARD_SIZE = 500               # documents per output shard

# --- Image triage (heuristics only, step 2) --------------------------------
MIN_OCR_IMAGE_BYTES = 20_000      # smaller than this => icon/logo => drop
VECTOR_LOGO_MAX_BYTES = 300_000   # wmf/emf smaller than this => logo/glyph => drop
SIGNATURE_ZONE_FRACTION = 0.10    # the last 10% of blocks = signature zone
DEDUPE_IMAGES = True              # deduplication by perceptual hash (png/jpg)

# --- S3 write retries -------------------------------------------------------
S3_WRITE_RETRIES = 4
S3_WRITE_RETRY_BASE_DELAY = 5  # seconds; delay = base * 2**attempt
