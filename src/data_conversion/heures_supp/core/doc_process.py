"""
Processing of a single document:
  raw bytes -> sniffing -> deterministic extraction (docx/pptx/odf/pdf/rtf/html)
  -> structure (sections) -> image triage -> structured record (JSON).

Used by steps/step2_extract.py (on S3) and selftest.py (local files).
"""
import json
import logging

from . import structure, triage
from .extract import odf as extract_odf
from .extract import pdf as extract_pdf
from .extract import slides as extract_slides
from .extract import word as extract_word
from .utils import sniff_format

logger = logging.getLogger(__name__)

PPT_EXT = (".sldx", ".pptx")


# ------------------------------------------------------------------ helpers
def _esc_cell(c) -> str:
    return (c or "").replace("|", "\\|").replace("\n", " ")


def render_table_md(grid) -> str:
    """Markdown rendering of a grid (empty cells really empty)."""
    if not grid:
        return ""
    lines = []
    for ri, row in enumerate(grid):
        lines.append("| " + " | ".join(_esc_cell(c) for c in row) + " |")
        if ri == 0:
            lines.append("|" + "|".join(" --- " for _ in row) + "|")
    return "\n".join(lines)


def _empty_table(grid) -> bool:
    return not any((c or "").strip() for row in grid for c in row)


def _make_figure(fid, spec, origin, data, extra=None) -> dict:
    fig = {
        "id": fid,
        "origin": origin,
        "fmt": (spec.get("fmt") or "").lower(),
        "size": spec.get("size") or (len(data) if data else 0),
        "part": spec.get("part"),
        "data": data,
    }
    if extra:
        fig.update(extra)
    return fig


def _process_embedded(entry, zf, blocks, errors, stats, seen_hashes_holder) -> None:
    """Expands an OLE object (PPT slides) or flags it as unreadable."""
    part = entry.get("part") or ""
    low = part.lower()
    if low.endswith(PPT_EXT):
        try:
            emb = zf.read(part)
            ppt = extract_ppt_lazy(emb)
            stats["n_ppt"] += 1
            for slide_blocks in ppt["slides"]:
                for sb in slide_blocks:
                    if sb["type"] == "para":
                        blocks.append(sb)
                    elif sb["type"] == "table":
                        blocks.append(sb)
                    elif sb["type"] == "figure":
                        img = sb["image"]
                        data = None
                        try:
                            data = ppt["zf"].read(img["part"])
                        except KeyError:
                            pass
                        blocks.append(
                            {"kind": "figure", "origin": "slide", "spec": img, "data": data}
                        )
                    elif sb["type"] == "slide_error":
                        errors.append(f"slide {sb['part']}: {sb['error']}")
        except Exception as e:
            errors.append(f"embedded object {part}: {e}")
            stats["unreadable"].append(part)
    elif low.endswith((".ppt", ".bin", ".doc", ".xls", ".docx")):
        # Binary OLE not readable as XML: the only exploitable representation
        # is the preview image (EMF/WMF) -> queue it for OCR.
        if entry.get("preview"):
            blocks.append(
                {"kind": "figure", "origin": "ole_preview", "spec": entry["preview"],
                 "data": _read_media(zf, entry["preview"])}
            )
        stats["unreadable"].append(part)
    else:
        stats["unreadable"].append(part)


def _read_media(zf, spec) -> bytes | None:
    try:
        return zf.read(spec["part"])
    except KeyError:
        return None


def extract_ppt_lazy(data: bytes) -> dict:
    return extract_slides.extract_ppt(data)


def _process_odf(data: bytes) -> dict:
    doc = extract_odf.extract_odf(data)
    zf = doc["zf"]
    raw_blocks = []
    for b in doc["blocks"]:
        if b["type"] == "para":
            raw_blocks.append(b)
        elif b["type"] == "table":
            raw_blocks.append(b)
    # images (position not reliable in the stream: appended at the end)
    for spec in doc["figures"]:
        raw_blocks.append(
            {"kind": "figure", "origin": "inline", "spec": spec,
             "data": _read_media(zf, spec)}
        )
    return {
        "format": "odt",
        "doc_type": "odf",
        "raw_blocks": raw_blocks,
        "errors": [],
        "stats": {"n_figures": doc["info"]["n_figures"]},
        "info": {},
        "extra": {},
        "data": data,
    }


# ------------------------------------------------------------------ formats
def _process_docx(data: bytes) -> dict:
    doc = extract_word.extract_docx(data)
    zf = doc["zf"]
    errors = []
    stats = {"n_ole": 0, "n_ppt": 0, "unreadable": []}

    raw_blocks = []
    for b in doc["blocks"]:
        if b["type"] == "para":
            raw_blocks.append(b)
            for entry in b.get("objects", []):
                stats["n_ole"] += 1
                _process_embedded(entry, zf, raw_blocks, errors, stats, None)
            for img in b.get("images", []):
                raw_blocks.append(
                    {"kind": "figure", "origin": "inline", "spec": img,
                     "data": _read_media(zf, img)}
                )
        elif b["type"] == "table":
            raw_blocks.append(b)

    return {
        "format": "docx",
        "doc_type": "word+ppt" if stats["n_ppt"] > 0 else "word",
        "raw_blocks": raw_blocks,
        "errors": errors,
        "stats": stats,
        "info": doc["info"],
        "extra": {"n_header_footer_images": doc["info"]["n_header_footer_images"]},
        "data": data,
    }


def _process_pptx(data: bytes) -> dict:
    errors = []
    ppt = extract_slides.extract_ppt(data)
    raw_blocks = []
    for slide_idx, slide_blocks in enumerate(ppt["slides"], start=1):
        for sb in slide_blocks:
            if sb["type"] == "para":
                raw_blocks.append(sb)
            elif sb["type"] == "table":
                raw_blocks.append(sb)
            elif sb["type"] == "figure":
                img = sb["image"]
                try:
                    d = ppt["zf"].read(img["part"])
                except KeyError:
                    d = None
                raw_blocks.append(
                    {"kind": "figure", "origin": "slide", "spec": img, "data": d,
                     "extra": {"slide": slide_idx}}
                )
            elif sb["type"] == "slide_error":
                errors.append(f"slide {sb['part']}: {sb['error']}")
    return {
        "format": "pptx",
        "doc_type": "pptx",
        "raw_blocks": raw_blocks,
        "errors": errors,
        "stats": {"n_slides": len(ppt["slides"])},
        "info": {},
        "extra": {},
        "data": data,
    }


def _process_pdf(data: bytes) -> dict:
    pdf = extract_pdf.extract_pdf(data)
    raw_blocks = []
    for page in pdf["pages"]:
        for line in page["lines"]:
            raw_blocks.append(
                {"type": "para", "text": line["text"], "src": "pdf", "style": None,
                 "feat": line["feat"], "images": [], "objects": [],
                 "page": page["page"]}
            )
        for t in page["tables"]:
            t = dict(t)
            t["page"] = page["page"]
            raw_blocks.append(t)
    # Images (pypdf) have no reliable position in the text stream:
    # they are appended at the end of the document.
    for img in pdf["images"]:
        raw_blocks.append(
            {"kind": "figure", "origin": "pdf",
             "spec": {"part": None, "fmt": img["fmt"], "size": img["size"]},
             "data": img["data"], "extra": {"page": img["page"], "name": img["name"]}}
        )
    scanned = pdf["scanned"]
    return {
        "format": "pdf",
        "doc_type": "pdf_scanned" if scanned else "pdf",
        "raw_blocks": raw_blocks,
        "errors": [] if not scanned else ["pdf_scanned: no text layer"],
        "stats": {"n_pages": pdf["n_pages"], "total_chars": pdf["total_chars"]},
        "info": {},
        "extra": {},
        "data": data,
    }


def _process_rtf(data: bytes) -> dict:
    try:
        from striprtf.striprtf import rtf_to_text

        text = rtf_to_text(data.decode("utf-8", errors="ignore"))
    except Exception as e:
        return {
            "format": "rtf", "doc_type": "unknown", "raw_blocks": [],
            "errors": [f"rtf: {e}"], "stats": {}, "info": {}, "extra": {}, "data": data,
        }
    raw_blocks = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        letters = [c for c in line if c.isalpha()]
        raw_blocks.append(
            {
                "type": "para", "text": line, "src": "rtf", "style": None,
                "feat": {
                    "caps": sum(1 for c in letters if c.isupper()) / len(letters) if letters else 0.0,
                    "bold_frac": 0.0, "u_frac": 0.0, "size_pt": None,
                    "is_list": False, "outline_lvl": None,
                },
                "images": [], "objects": [],
            }
        )
    return {
        "format": "rtf", "doc_type": "rtf", "raw_blocks": raw_blocks,
        "errors": [], "stats": {}, "info": {}, "extra": {}, "data": data,
    }


def _process_html(data: bytes) -> dict:
    import html as _html
    import re as _re

    text = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", data.decode("utf-8", errors="ignore"))
    text = _re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = _re.sub(r"(?i)</p>|</div>|</li>", "\n", text)
    text = _re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    raw_blocks = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        letters = [c for c in line if c.isalpha()]
        raw_blocks.append(
            {
                "type": "para", "text": line, "src": "html", "style": None,
                "feat": {
                    "caps": sum(1 for c in letters if c.isupper()) / len(letters) if letters else 0.0,
                    "bold_frac": 0.0, "u_frac": 0.0, "size_pt": None,
                    "is_list": False, "outline_lvl": None,
                },
                "images": [], "objects": [],
            }
        )
    return {
        "format": "html", "doc_type": "html", "raw_blocks": raw_blocks,
        "errors": [], "stats": {}, "info": {}, "extra": {}, "data": data,
    }


# ------------------------------------------------------------------ pipeline
def process_document_bytes(data: bytes, cid: str, reference: str,
                           upload_figure=None, figs_base: str | None = None) -> dict:
    """
    Processes the raw bytes of a document and returns the structured record.

    upload_figure(fig_s3, bytes) is called for each image kept by the triage
    (None locally). figs_base: S3 prefix of the figures.
    """
    fmt = sniff_format(data)

    if fmt == "docx":
        proc = _process_docx(data)
    elif fmt == "pptx":
        proc = _process_pptx(data)
    elif fmt in ("odt", "odf"):
        proc = _process_odf(data)
    elif fmt == "pdf":
        proc = _process_pdf(data)
    elif fmt == "rtf":
        proc = _process_rtf(data)
    elif fmt == "html":
        proc = _process_html(data)
    else:
        proc = {
            "format": fmt, "doc_type": "unknown", "raw_blocks": [],
            "errors": [f"unrecognized format: {fmt}"], "stats": {}, "info": {},
            "extra": {}, "data": data,
        }

    raw_blocks = proc["raw_blocks"]

    # --- unified items (paragraphs + tables + figures, in order) ---
    items = []
    para_units = []
    last_para_idx = None
    table_n = 0
    figure_n = 0
    seen_hashes = set()
    for b in raw_blocks:
        if b.get("type") == "para":
            para_idx = len(para_units)
            para_units.append({"text": b["text"], "feat": b["feat"], "src": b.get("src")})
            last_para_idx = para_idx
            items.append({"kind": "para", "idx": para_idx, "u": para_units[-1]})
        elif b.get("type") == "table":
            table_n += 1
            tbl_id = f"T{table_n}"
            grid = b.get("grid") or []
            items.append({
                "kind": "table", "id": tbl_id, "rattach": last_para_idx,
                "grid": grid,
                "spans": b.get("spans"),
                "vmerge": b.get("vmerge"),
                "n_rows": b.get("n_rows") or len(grid),
                "n_cols": b.get("n_cols") or (len(grid[0]) if grid else 0),
                "empty": _empty_table(grid),
                "md": render_table_md(grid),
                "context": (para_units[last_para_idx]["text"].strip()[:200]
                            if last_para_idx is not None and para_units[last_para_idx]["text"].strip()
                            else ""),
                **({k: b[k] for k in ("page",) if k in b}),
            })
        elif b.get("kind") == "figure":
            figure_n += 1
            fid = f"F{figure_n}"
            data_img = b.get("data")
            fig = _make_figure(fid, b["spec"], b.get("origin") or "inline", data_img,
                               extra=b.get("extra"))
            # triage (heuristics only)
            keep, reason = triage.triage_figure(fig, len(items), len(raw_blocks), seen_hashes)
            fig["triage"] = {"keep": keep, "reason": reason}
            if keep and upload_figure is not None and figs_base:
                ext = fig["fmt"] if fig["fmt"] in ("png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp") else "bin"
                fig_s3 = f"{figs_base}{cid}/{fid}.{ext}"
                ok = upload_figure(fig_s3, data_img)
                fig["fig_s3"] = fig_s3 if ok else None
                if not ok:
                    fig["triage"]["reason"] = reason + "|upload_failed"
            elif not keep:
                fig["fig_s3"] = None
            items.append({"kind": "figure", "id": fid, "rattach": last_para_idx, "fig": fig})

    # --- structure ---
    median_pt = structure.doc_median_size_pt(para_units)
    for u in para_units:
        u["feat"]["doc_median_pt"] = median_pt
    scored = {}
    for i, u in enumerate(para_units):
        scored[i] = structure.score_paragraph(u["text"], u["feat"])
    sig_start = structure.find_signature_start(para_units)
    # offset = 1 when the document has TITRE/CHAPITRE containers: articles
    # are then attached one level lower (scope of the title).
    offset = structure.compute_offset(scored)
    tree = structure.build_structure(items, scored, para_units, sig_start, offset)

    # --- record assembly ---
    toc = [
        {"id": s["id"], "level": s["level"], "number": s["number"],
         "title": s["title"], "confidence": s["confidence"]}
        for s in tree["sections"]
    ]
    sections_out = []
    for s in tree["sections"]:
        sections_out.append({
            "id": s["id"], "level": s["level"], "number": s["number"],
            "title": s["title"], "confidence": s["confidence"],
            "evidence": s["evidence"], "parent": s["parent"],
            "body_md": s["body_md"], "n_paras": s["n_paras"],
            "table_ids": s["table_ids"], "figure_ids": s["figure_ids"],
        })
    root = tree["root"]
    tables_out = []
    figures_out = []
    for it in items:
        if it["kind"] == "table":
            tables_out.append({
                "id": it["id"], "section": it["sec_id"], "context": it["context"],
                "n_rows": it["n_rows"], "n_cols": it["n_cols"], "empty": it["empty"],
                "grid": it["grid"], "spans": it.get("spans"), "vmerge": it.get("vmerge"),
                "md": it["md"], **({k: it[k] for k in ("page",) if k in it}),
            })
        elif it["kind"] == "figure":
            f = it["fig"]
            f = {k: v for k, v in f.items() if k != "data"}
            f["section"] = it["sec_id"]
            f["ocr_md"] = None
            f["ocr_ok"] = None
            figures_out.append(f)

    signature = tree["signature"]
    n_kept = sum(1 for f in figures_out if f["triage"]["keep"])
    n_dropped = len(figures_out) - n_kept

    record = {
        "cid": cid,
        "reference": reference,
        "format": proc["format"],
        "doc_type": proc["doc_type"],
        "n_paras": len(para_units),
        "n_sections": len(sections_out),
        "n_tables": len(tables_out),
        "n_figures": len(figures_out),
        "n_ocr_kept": n_kept,
        "n_ocr_dropped": n_dropped,
        "toc": toc,
        "preamble_md": root["body_md"],
        "preamble_table_ids": root["table_ids"],
        "preamble_figure_ids": root["figure_ids"],
        "sections": sections_out,
        "signature_md": signature.get("text_md"),
        "tables": tables_out,
        "figures": figures_out,
        "stats": proc["stats"],
        "errors": proc["errors"],
    }
    if proc.get("extra"):
        record["extra"] = proc["extra"]
    return record


def record_to_json(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False)


def json_to_record(s: str) -> dict:
    return json.loads(s)
