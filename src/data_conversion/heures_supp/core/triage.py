"""
Heuristic triage of images (NO LLM call).

Decides, for each image of the document, whether it is worth sending an OCR
job to the VLM (step 3), or whether it should be dropped (logo, glyph,
signature, duplicate...). Every decision is traced with its reason
(audit via the document record in logs/triage).

Rules (in order):
  1. header / footer image            -> drop  (header_footer)
  2. position in the signature zone   -> drop  (signature_zone)
  3. small WMF/EMF                    -> drop  (small_vector_logo)
  4. size below the global threshold  -> drop  (too_small)
  5. perceptual duplicate (png/jpg)   -> drop  (duplicate)
  6. otherwise                        -> keep  (inline / slide_content)
"""
from . import config
from .utils import perceptual_hash


def triage_figure(fig: dict, block_pos: int, n_blocks: int, seen_hashes: set) -> tuple:
    """
    fig      : {"part", "fmt", "size", "origin", "data": bytes|None}
    block_pos: position of the figure block in the ordered block list
    n_blocks : total number of blocks
    seen_hashes: set of phash already seen (current run); updated here
    Returns (keep: bool, reason: str)
    """
    fmt = (fig.get("fmt") or "").lower()
    size = fig.get("size") or 0

    if fig.get("origin") == "header_footer":
        return False, "header_footer"
    if n_blocks > 0 and block_pos >= n_blocks * (1 - config.SIGNATURE_ZONE_FRACTION):
        return False, "signature_zone"
    if fmt in ("wmf", "emf") and 0 < size < config.VECTOR_LOGO_MAX_BYTES:
        return False, "small_vector_logo"
    if 0 < size < config.MIN_OCR_IMAGE_BYTES:
        return False, "too_small"

    data = fig.get("data")
    if config.DEDUPE_IMAGES and data is not None and fmt in ("png", "jpg", "jpeg"):
        h = perceptual_hash(data)
        if h is not None:
            if h in seen_hashes:
                return False, "duplicate"
            seen_hashes.add(h)

    if fig.get("origin") == "slide":
        return True, "slide_content_image"
    return True, "inline_content_image"
