"""
Text / table / image extraction from PDF.

- pdfplumber: text line by line (with fontname/size for the title-detection
  features) + native tables ("born digital" PDFs).
- pypdf     : image bytes (for the triage / potential OCR).

A "scanned" PDF (no text layer) is detected (scanned=True): in that case
only the full LLM pipeline (step 3, --ocr-scanned) can exploit it.
"""
import io

import pdfplumber
from pypdf import PdfReader


def _line_features(page) -> list:
    if not page.chars:
        return []
    lines = {}
    for c in page.chars:
        lines.setdefault(round(c["top"], 1), []).append(c)
    out = []
    for key in sorted(lines):
        cs = sorted(lines[key], key=lambda c: c["x0"])
        text = "".join(c["text"] for c in cs)
        if not text.strip():
            continue
        bold = sum(
            1
            for c in cs
            if any(k in c.get("fontname", "") for k in ("Bold", "Black", "Heavy", "Demi", "Semi"))
        )
        size = max(c["size"] for c in cs)
        letters = [ch for ch in text if ch.isalpha()]
        caps = sum(1 for ch in letters if ch.isupper())
        out.append(
            {
                "text": text,
                "feat": {
                    "caps": caps / len(letters) if letters else 0.0,
                    "bold_frac": bold / len(cs) if cs else 0.0,
                    "u_frac": 0.0,
                    "size_pt": size,
                    "is_list": False,
                    "outline_lvl": None,
                },
            }
        )
    return out


def extract_pdf(data: bytes) -> dict:
    pdf = pdfplumber.open(io.BytesIO(data))
    pages = []
    total_chars = 0
    for pageno, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        total_chars += len(text)
        tables = []
        try:
            for t in page.extract_tables():
                grid = [[(c or "").strip() for c in row] for row in t]
                if grid:
                    tables.append(
                        {
                            "type": "table",
                            "grid": grid,
                            "spans": [[1] * len(row) for row in grid],
                            "vmerge": [[None] * len(row) for row in grid],
                            "n_rows": len(grid),
                            "n_cols": len(grid[0]),
                        }
                    )
        except Exception:
            pass
        pages.append(
            {
                "page": pageno + 1,
                "lines": _line_features(page),
                "tables": tables,
                "n_images": len(page.images or []),
            }
        )
    n_pages = len(pdf.pages)
    pdf.close()

    images = []
    try:
        reader = PdfReader(io.BytesIO(data))
        for pageno, page in enumerate(reader.pages):
            try:
                imgs = list(page.images)
            except Exception:
                continue
            for img in imgs:
                name = img.name or "image.png"
                images.append(
                    {
                        "page": pageno + 1,
                        "name": name,
                        "data": img.data,
                        "fmt": name.rsplit(".", 1)[-1].lower() if "." in name else "png",
                        "size": len(img.data),
                    }
                )
    except Exception:
        pass

    return {
        "pages": pages,
        "images": images,
        "scanned": total_chars < 50,
        "n_pages": n_pages,
        "total_chars": total_chars,
    }
