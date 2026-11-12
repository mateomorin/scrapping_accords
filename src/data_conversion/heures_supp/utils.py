"""
Helpers partagés : écriture S3 sécurisée, sniffing de format, téléchargements,
retours d'erreur, OCR LLM d'une image, hash perceptuel.
"""
import base64
import io
import json
import logging
import re
import time
import zipfile

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- S3 safety
def safe_remote_path(path: str) -> str:
    """Refuse tout chemin en dehors de la zone de sortie autorisée."""
    if not path.startswith(config.OUTPUT_ROOT):
        raise PermissionError(
            f"Refus d'écriture en dehors de {config.OUTPUT_ROOT!r}: {path!r}"
        )
    return path


def s3_write_with_retry(write_fn, description: str, *args, **kwargs) -> bool:
    """Applique write_fn avec retries exponentiels. Ne lève jamais (sauf refus)."""
    last_exc = None
    for attempt in range(config.S3_WRITE_RETRIES):
        try:
            write_fn(*args, **kwargs)
            return True
        except Exception as e:
            last_exc = e
            wait = config.S3_WRITE_RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                f"Échec écriture S3 ({description}), essai "
                f"{attempt + 1}/{config.S3_WRITE_RETRIES} : {e}. Nouvel essai dans {wait}s."
            )
            time.sleep(wait)
    logger.error(f"Échec définitif d'écriture S3 ({description}) : {last_exc}")
    return False


def put_bytes(path: str, data: bytes) -> bool:
    safe_remote_path(path)
    return s3_write_with_retry(config.fs.pipe, f"bytes -> {path}", path, data)


def put_parquet(df, path: str) -> bool:
    safe_remote_path(path)

    def _w():
        df.to_parquet(path, filesystem=config.fs, index=False)

    return s3_write_with_retry(_w, f"parquet -> {path}")


def put_file(path: str, local_path: str) -> bool:
    safe_remote_path(path)
    return s3_write_with_retry(config.fs.put, f"file -> {path}", local_path, path)


# ------------------------------------------------------------------ jsonl
def read_jsonl(path: str) -> list:
    try:
        with config.fs.open(path, "rb") as f:
            data = f.read()
    except (FileNotFoundError, IsADirectoryError):
        return []
    out = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning(f"Ligne JSON illisible dans {path}, ignorée.")
    return out


def write_jsonl(path: str, records: list) -> bool:
    safe_remote_path(path)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    return put_bytes(path, data)


# ------------------------------------------------------------- format sniff
def _zip_kind(data: bytes) -> str:
    """
    docx/pptx/xlsx à partir des seuls premiers octets d'un zip.

    Contrainte : zipfile a besoin de la table centrale (fin d'archive), donc
    impossible sur un extrait tronqué. Astuce : les NOMS de parties des local
    file headers sont stockés en clair dans le flux zip. On scanne donc les
    signatures PK\x03\x04 et on lit les noms qui suivent ; les parties OOXML
    apparaissent très tôt (word/*, ppt/*) après [Content_Types].xml et
    _rels/.rels. Retourne "zip" si aucun nom caractéristique n'est visible.
    """
    pos, n = 0, len(data)
    while True:
        i = data.find(b"PK\x03\x04", pos)
        if i < 0 or i + 30 > n:
            break
        fnlen = int.from_bytes(data[i + 26 : i + 28], "little")
        if fnlen == 0 or fnlen > 512 or i + 30 + fnlen > n:
            pos = i + 4
            continue
        name = data[i + 30 : i + 30 + fnlen].decode("utf-8", errors="replace").lower()
        if name == "mimetype":
            # Première entrée d'un ODF, stockée sans compression : on peut
            # lire le contenu (opendocument.text / .spreadsheet / .presentation)
            # juste après l'en-tête local.
            method = int.from_bytes(data[i + 8 : i + 10], "little")
            if method == 0:
                # local header : csize @18, extralen @28 (offsets ZIP standard)
                csize = int.from_bytes(data[i + 18 : i + 22], "little")
                extralen = int.from_bytes(data[i + 28 : i + 30], "little")
                start = i + 30 + fnlen + extralen
                content = data[start : start + min(csize, 120)]
                if b"opendocument.text" in content:
                    return "odt"
                if b"opendocument" in content:
                    return "odf"
                return "odf"
            return "odf"
        if name.startswith("word/"):
            return "docx"
        if name.startswith("ppt/"):
            return "pptx"
        if name.startswith("xl/"):
            return "xlsx"
        pos = i + 30 + fnlen
        if pos - i > 4096:  # en-tête suspectement gros : on passe au suivant
            pos = i + 4
    return "zip"


def sniff_format(data: bytes) -> str:
    """
    Détecte le format réel d'un document à partir des magic bytes.
    L'extension du fichier n'est JAMAIS trustée (scrap_texts.py force .docx).
    Retourne un de: docx, pptx, xlsx, pdf, rtf, html, zip, unknown.
    """
    if data[:4] == b"PK\x03\x04":
        kind = _zip_kind(data)
        if kind != "zip":
            return kind
        # Extrait tronqué sans nom caractéristique : on tente le zip complet.
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
            if "mimetype" in names:
                content = z.read("mimetype")[:120]
                return "odt" if b"opendocument.text" in content else "odf"
            if "word/document.xml" in names:
                return "docx"
            if "ppt/presentation.xml" in names:
                return "pptx"
        except (zipfile.BadZipFile, KeyError):
            pass
        return "zip"
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:5] in (b"{\\rtf", b"\\rtf1"):
        return "rtf"
    head = data.lstrip()[:300].lower()
    if head.startswith(b"<?xml") or head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return "html"
    return "unknown"


# ---------------------------------------------------------- downloads S3
def download_bytes(remote_path: str, max_retries: int = 3) -> bytes | None:
    for attempt in range(max_retries):
        try:
            return config.fs.cat_file(remote_path)
        except Exception as e:
            logger.warning(
                f"Téléchargement échoué {remote_path} "
                f"(essai {attempt + 1}/{max_retries}) : {e}"
            )
            time.sleep(2 * (attempt + 1))
    return None


def head_bytes(remote_path: str, n: int = 4096) -> bytes | None:
    """Premiers n octets (sniffing sans télécharger le fichier entier)."""
    try:
        return config.fs.cat_file(remote_path, start=0, end=n)
    except Exception:
        return None


def file_exists(remote_path: str) -> bool:
    try:
        config.fs.info(remote_path)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------- LLM
_llm_client = None


def get_llm_client():
    global _llm_client
    from openai import OpenAI

    if not config.LLM_API_URL or not config.LLM_API_KEY:
        raise RuntimeError(
            "LLM_API_URL/LLM_API_KEY (ou OPENAI_BASE_URL/OPENAI_API_KEY) non définis."
        )
    if _llm_client is None:
        _llm_client = OpenAI(base_url=config.LLM_API_URL, api_key=config.LLM_API_KEY)
    return _llm_client


def llm_ocr_image(image_bytes: bytes, mime: str, model: str | None = None,
                  max_attempts: int = 3) -> str:
    """
    Un seul appel OCR/description sur une image (endpoint OpenAI-compatible).
    Retry interne sur erreur transitoire (réseau, 5xx) ; lève la dernière
    erreur après max_attempts tentatives.
    """
    client = get_llm_client()
    b64 = base64.b64encode(image_bytes).decode()
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = client.chat.completions.create(
                model=model or config.LLM_MODEL,
                temperature=0.0,
                timeout=config.OCR_TIMEOUT,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": config.OCR_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64}"},
                            },
                        ],
                    }
                ],
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            last_err = e
            wait = 5 * (attempt + 1)
            logger.warning(
                f"OCR LLM essai {attempt + 1}/{max_attempts} échoué : {e} "
                f"-> nouvelle tentative dans {wait}s"
            )
            time.sleep(wait)
    raise last_err


# ------------------------------------------------------------- image prepare
_RE_MD_IMG = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_RE_URL_ONLY_LINE = re.compile(r"^\s*https?://\S+\s*$")
# Liens Markdown avec URL inventée par le VLM (vide, example.com, ou le
# domaine replicate.delivery qui apparaît dans les hallucinations) :
# on garde le texte, on supprime l'URL.
_RE_FAKE_LINK = re.compile(
    r"\[([^\]]*)\]\((?:https?://(?:[^\)]*example[^\)]*|[^\)]*replicate\.delivery[^\)]*|))\)"
)


def clean_ocr_md(md: str) -> str:
    """
    Nettoyage déterministe du Markdown produit par le VLM :
    - référence d'image `![alt](url)` (le modèle rend les logos avec des
      URL inventées) -> citation `> [Image : alt]` sans URL ;
    - lignes ne contenant qu'une URL : supprimées (les URLs au milieu du
      texte sont conservées, prudence) ;
    - lien Markdown `[texte](url_inventée)` (URL vide, example.com,
      replicate.delivery) : on garde le texte, on supprime l'URL.
    """
    if not md:
        return md
    md = _RE_MD_IMG.sub(
        lambda m: f"> [Image : {m.group(1).strip()}]" if m.group(1).strip() else "> [Image]",
        md,
    )
    md = _RE_FAKE_LINK.sub(lambda m: m.group(1).strip() or "[lien]", md)
    lines = [ln for ln in md.splitlines() if not _RE_URL_ONLY_LINE.match(ln)]
    return "\n".join(lines).strip()


def prepare_image_for_ocr(data: bytes, ext: str) -> tuple[bytes, str]:
    """
    Préparation déterministe d'une image avant envoi au VLM :
    - redimensionne les très grandes images (les VLM plafonnent la taille),
    - ré-encode (JPEG q85 / PNG) pour rester sous quelques Mo,
    - aplatie la transparence sur fond blanc.
    Retourne (bytes, mime) ; en cas d'erreur, retourne l'image d'origine.
    """
    mime = MIME_BY_EXT.get(ext)
    if mime is None or ext not in {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"}:
        return data, mime
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img.load()
        w, h = img.size
        max_dim = max(w, h)
        if max_dim <= 3200 and len(data) <= 4_000_000:
            return data, mime
        if max_dim > 3200:
            scale = 3200 / max_dim
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        # transparence -> fond blanc
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return data, mime


# ------------------------------------------------------------- image hashing
def perceptual_hash(image_bytes: bytes):
    """Hash perceptuel (pHash) d'une image PIL-ouverte, ou None si illisible."""
    try:
        import imagehash
        from PIL import Image

        return str(imagehash.phash(Image.open(io.BytesIO(image_bytes))))
    except Exception:
        return None


MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}
