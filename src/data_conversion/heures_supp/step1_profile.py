"""
Step 1 - Inventaire et profilage du sous-ensemble (AUCUN LLM).

- TOUS les documents : détection du format réel par magic bytes (lecture des
  4 premiers Ko seulement) + vérification d'existence du fichier.
- Un ÉCHANTILLON (par défaut 300, --sample) : téléchargement complet et
  profilage XML (objets OLE, tableaux, images, pages PDF, ...).

Écrit (zone autorisée) : logs/profile_subset.parquet

Lancement :
    uv run src/data_conversion/heures_supp/step1_profile.py
    uv run src/data_conversion/heures_supp/step1_profile.py --sample 500 --seed 7
"""
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import config
import utils
from utils import download_bytes, head_bytes, sniff_format

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def quick_format(row) -> dict:
    """Format réel (magic bytes sur 64 Ko) + existence."""
    head = head_bytes(row["docx_path"], 65536)
    if head is None or len(head) == 0:
        return {"exists": False, "format": "missing", "size_head": 0}
    return {"exists": True, "format": sniff_format(head), "size_head": len(head)}


def deep_profile(row) -> dict:
    """Profilage XML complet (nécessite le téléchargement du fichier)."""
    out = {"exists": True}
    data = download_bytes(row["docx_path"])
    if data is None:
        return {"exists": False, "format": "missing"}
    fmt = sniff_format(data)
    out["format"] = fmt
    out["size_bytes"] = len(data)
    try:
        if fmt == "docx":
            import extract_word

            doc = extract_word.extract_docx(data)
            blocks = doc["blocks"]
            n_paras = sum(1 for b in blocks if b["type"] == "para")
            n_tables = sum(1 for b in blocks if b["type"] == "table")
            media = doc["info"]["media"]
            out.update({
                "n_paras": n_paras,
                "n_tables": n_tables,
                "n_ole": len(doc["info"]["embedded_parts"]),
                "n_ppt_embed": int(doc["info"]["has_ppt_embed"]),
                "n_media": len(media),
                "media_bytes": sum(m["size"] for m in media.values()),
                "n_hf_images": doc["info"]["n_header_footer_images"],
            })
        elif fmt == "pptx":
            import extract_slides

            ppt = extract_slides.extract_ppt(data)
            n_paras = sum(
                1 for s in ppt["slides"] for b in s if b["type"] == "para"
            )
            n_tables = sum(
                1 for s in ppt["slides"] for b in s if b["type"] == "table"
            )
            n_figs = sum(
                1 for s in ppt["slides"] for b in s if b["type"] == "figure"
            )
            out.update({
                "n_slides": len(ppt["slides"]),
                "n_paras": n_paras,
                "n_tables": n_tables,
                "n_media": len(ppt["media"]),
                "media_bytes": sum(m["size"] for m in ppt["media"].values()),
                "n_slide_figs": n_figs,
            })
        elif fmt == "pdf":
            import extract_pdf

            pdf = extract_pdf.extract_pdf(data)
            out.update({
                "n_pages": pdf["n_pages"],
                "total_chars": pdf["total_chars"],
                "scanned": int(pdf["scanned"]),
                "n_tables": sum(len(p["tables"]) for p in pdf["pages"]),
                "n_images": len(pdf["images"]),
            })
        elif fmt in ("rtf", "html"):
            out["n_paras"] = len(data) // 100  # indicatif seulement
    except Exception as e:
        out["profile_error"] = f"{type(e).__name__}: {e}"
    return out


def main():
    parser = argparse.ArgumentParser(description="Step 1 : profilage du sous-ensemble")
    parser.add_argument("--sample", type=int, default=300,
                        help="Taille de l'échantillon pour le profilage complet (défaut 300)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    sel = pd.read_parquet(config.SELECTION_FILE, filesystem=config.fs)
    logger.info(f"{len(sel)} documents à inventorier")

    # 1) Format réel + existence (tous les documents, 4 Ko chacun)
    logger.info("Détection des formats (magic bytes)...")
    with ThreadPoolExecutor(max_workers=16) as ex:
        quick = list(ex.map(quick_format, sel.to_dict("records")))
    qdf = pd.DataFrame(quick)
    df = pd.concat([sel[["cid", "docx_path"]].reset_index(drop=True), qdf], axis=1)
    logger.info("Distribution des formats :")
    logger.info(df["format"].value_counts().to_string())
    n_missing = int((df["format"] == "missing").sum())
    if n_missing:
        logger.warning(f"{n_missing} fichiers introuvables dans {config.DOCUMENTS_PATH}")

    # 2) Profilage complet d'un échantillon
    n_sample = min(args.sample, len(df))
    sample = df.sample(n=n_sample, random_state=args.seed).reset_index(drop=True)
    logger.info(f"Profilage complet de l'échantillon ({n_sample} documents)...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        deep = list(ex.map(deep_profile, sample.to_dict("records")))
    ddf = pd.DataFrame(deep)
    df_sample = pd.concat(
        [sample[["cid", "docx_path", "format"]].reset_index(drop=True), ddf], axis=1
    )

    out = pd.concat(
        [df, df_sample.drop(columns=["cid", "docx_path", "format", "exists"])], axis=1
    )
    ok = utils.put_parquet(out, config.PROFILE_FILE)
    if not ok:
        raise SystemExit(f"Écriture impossible : {config.PROFILE_FILE}")
    logger.info(f"Profil écrit : {config.PROFILE_FILE}")

    # Résumé de l'échantillon
    for col in ["n_ole", "n_ppt_embed", "n_tables", "n_media", "n_pages", "scanned"]:
        if col in out.columns and out[col].notna().any():
            logger.info(f"  {col}: moyenne={out[col].mean():.2f} max={out[col].max()}")


if __name__ == "__main__":
    main()
