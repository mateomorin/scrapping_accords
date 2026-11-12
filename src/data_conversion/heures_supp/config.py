"""
Configuration du pipeline d'extraction de structure pour le sous-ensemble
"Heures supplémentaires (contingent, majoration)".

Lecture seule (jamais d'écriture) :
    - s3://mateomorin/legifrance/metadata/   (parquets de metadata scrapés)
    - s3://mateomorin/legifrance/documents/  (documents bruts scrapés)

Écriture (zone autorisée UNIQUEMENT, vérifiée par utils.safe_remote_path) :
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

# Chemins sources (lecture seule)
METADATA_PATH = "s3://mateomorin/legifrance/metadata/"
DOCUMENTS_PATH = "s3://mateomorin/legifrance/documents/"

# Racine de sortie (seule zone d'écriture)
OUTPUT_ROOT = "s3://mateomorin/legifrance/data_heures_supp/"
DATA_PATH = OUTPUT_ROOT + "data/"
LOGS_PATH = OUTPUT_ROOT + "logs/"
FIGS_PATH = DATA_PATH + "figs/"  # images conservées par le triage (pour l'OCR step 3)

SELECTION_FILE = LOGS_PATH + "selection.parquet"
PROFILE_FILE = LOGS_PATH + "profile_subset.parquet"

# --- Sélection (step 0) ---------------------------------------------------
# Colonnes booléennes de la table de metadata ; le document appartient à la
# catégorie si l'une d'elles est True.
THEME_COLUMNS = ["Heures supplémentaires (contingent, majoration)"]

# --- LLM (OCR des images conservées, step 3) ------------------------------
# Compatible avec les variables du .env existant (LLM_API_URL/KEY) ou, en
# secours, OPENAI_BASE_URL/OPENAI_API_KEY.
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
MAX_CONCURRENCY_DOWNLOAD = 10  # téléchargements S3 parallèles (step 2, run complet)
MAX_CONCURRENCY_OCR = 4        # appels LLM parallèles (step 3)
SHARD_SIZE = 500               # documents par shard de sortie

# --- Triage des images (heuristiques uniquement, step 2) -------------------
MIN_OCR_IMAGE_BYTES = 20_000      # plus petit que ça => icône/logo => drop
VECTOR_LOGO_MAX_BYTES = 300_000   # wmf/emf plus petits que ça => logo/glyphe => drop
SIGNATURE_ZONE_FRACTION = 0.10    # les 10 % de blocs finaux = zone de signature
DEDUPE_IMAGES = True              # dédoublonnage par hash perceptuel (png/jpg)

# --- Retries d'écriture S3 -------------------------------------------------
S3_WRITE_RETRIES = 4
S3_WRITE_RETRY_BASE_DELAY = 5  # secondes ; délai = base * 2**attempt
