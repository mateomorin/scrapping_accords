"""
Triage heuristique des images (AUCUN appel LLM).

Décide, pour chaque image du document, s'il vaut la peine d'envoyer une
vocation OCR vers le VLM (step 3), ou si elle doit être écartée (logo,
glyphe, signature, doublon...). Chaque décision est tracée avec sa raison
(audit dans logs/triage via le record du document).

Règles (dans l'ordre) :
  1. image d'en-tête / pied de page        -> drop  (header_footer)
  2. position dans la zone de signature    -> drop  (signature_zone)
  3. WMF/EMF de petite taille              -> drop  (small_vector_logo)
  4. taille sous le seuil global           -> drop  (too_small)
  5. doublon perceptuel (png/jpg)          -> drop  (duplicate_of:...)
  6. sinon                                  -> keep  (inline / slide_content)
"""
import io

import config
from utils import perceptual_hash


def triage_figure(fig: dict, block_pos: int, n_blocks: int, seen_hashes: set) -> tuple:
    """
    fig      : {"part", "fmt", "size", "origin", "data": bytes|None}
    block_pos: position du bloc figure dans la liste ordonnée des blocs
    n_blocks : nombre total de blocs
    seen_hashes: set de phash déjà vus (courant du corpus) ; mis à jour ici
    Retourne (keep: bool, reason: str)
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
