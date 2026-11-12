"""
Shared helpers: safe S3 writes, format sniffing, downloads, error handling,
LLM OCR of a single image, perceptual hashing.
"""
import base64
import io
import json
import logging
import re
import time
import zipfile

from . import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- S3 safety
def safe_remote_path(path: str) -> str:
    """Refuse any path outside the authorized output zone."""
    if not path.startswith(config.OUTPUT_ROOT):
        raise PermissionError(
            f"refusing write outside {config.OUTPUT_ROOT!r}: {path!r}"
        )
    return path


def s3_write_with_retry(write_fn, description: str, *args, **kwargs) -> bool:
    """Run write_fn with exponential backoff. Never raises (except refusal)."""
    last_exc = None
    for attempt in range(config.S3_WRITE_RETRIES):
        try:
            write_fn(*args, **kwargs)
            return True
        except Exception as e:
            last_exc = e
            wait = config.S3_WRITE_RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                f"S3 write failed ({description}), attempt "
                f"{attempt + 1}/{config.S3_WRITE_RETRIES}: {e}. Retrying in {wait}s."
            )
            time.sleep(wait)
    logger.error(f"S3 write definitively failed ({description}): {last_exc}")
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
            logger.warning(f"Unreadable JSON line in {path}, ignored.")
    return out


def write_jsonl(path: str, records: list) -> bool:
    safe_remote_path(path)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    return put_bytes(path, data)


# ------------------------------------------------------------- format sniff
def _zip_kind(data: bytes) -> str:
    """
    docx/pptx/xlsx from the first bytes of a zip only.

    Constraint: zipfile needs the central directory (end of archive), so it
    cannot work on a truncated head. Trick: the NAMES of the local file
    headers are stored in clear in the zip stream. We scan the PK\\x03\\x04
    signatures and read the names that follow; OOXML parts appear very early
    (word/*, ppt/*) right after [Content_Types].xml and _rels/.rels. Returns
    "zip" when no characteristic name is visible.
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
            # First entry of an ODF, stored uncompressed: its content
            # (opendocument.text / .spreadsheet / .presentation) can be read
            # right after the local header.
            method = int.from_bytes(data[i + 8 : i + 10], "little")
            if method == 0:
                # local header: csize @18, extralen @28 (standard ZIP offsets)
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
        if pos - i > 4096:  # suspiciously large header: move on
            pos = i + 4
    return "zip"


def sniff_format(data: bytes) -> str:
    """
    Detect the real format of a document from its magic bytes.
    The file extension is NEVER trusted (scrap_texts.py forces .docx).
    Returns one of: docx, pptx, xlsx, pdf, rtf, html, zip, unknown.
    """
    if data[:4] == b"PK\x03\x04":
        kind = _zip_kind(data)
        if kind != "zip":
            return kind
        # Truncated head without a characteristic name: try the full zip.
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
                f"Download failed {remote_path} "
                f"(attempt {attempt + 1}/{max_retries}): {e}"
            )
            time.sleep(2 * (attempt + 1))
    return None


def head_bytes(remote_path: str, n: int = 4096) -> bytes | None:
    """First n bytes (sniffing without downloading the whole file)."""
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
            "LLM_API_URL/LLM_API_KEY (or OPENAI_BASE_URL/OPENAI_API_KEY) not set."
        )
    if _llm_client is None:
        _llm_client = OpenAI(base_url=config.LLM_API_URL, api_key=config.LLM_API_KEY)
    return _llm_client


def llm_ocr_image(image_bytes: bytes, mime: str, model: str | None = None,
                  max_attempts: int = 3) -> str:
    """
    A single OCR/description call for one image (OpenAI-compatible endpoint).
    Internal retry on transient errors (network, 5xx); raises the last error
    after max_attempts attempts.
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
                f"OCR LLM attempt {attempt + 1}/{max_attempts} failed: {e} "
                f"-> retrying in {wait}s"
            )
            time.sleep(wait)
    raise last_err


# ------------------------------------------------------------- image prepare
_RE_MD_IMG = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_RE_URL_ONLY_LINE = re.compile(r"^\s*https?://\S+\s*$")
# Markdown links with a URL invented by the VLM (empty, example.com, or the
# replicate.delivery domain that shows up in hallucinations): keep the text,
# drop the URL.
_RE_FAKE_LINK = re.compile(
    r"\[([^\]]*)\]\((?:https?://(?:[^\)]*example[^\)]*|[^\)]*replicate\.delivery[^\)]*|))\)"
)


def clean_ocr_md(md: str) -> str:
    """
    Deterministic cleaning of the Markdown produced by the VLM:
    - image references `![alt](url)` (the model renders logos with invented
      URLs) -> quote `> [Image : alt]` without URL;
    - lines containing only a URL: removed (URLs in the middle of text are
      kept, cautiously);
    - Markdown link `[text](invented_url)` (empty URL, example.com,
      replicate.delivery): keep the text, drop the URL.
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
    Deterministic image preparation before sending to the VLM:
    - resizes very large images (VLMs cap image size),
    - re-encodes (JPEG q85 / PNG) to stay under a few MB,
    - flattens transparency onto a white background.
    Returns (bytes, mime); on error, returns the original image.
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
        # transparency -> white background
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
    """Perceptual hash (pHash) of a PIL-openable image, or None if unreadable."""
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
