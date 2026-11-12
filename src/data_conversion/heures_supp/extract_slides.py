"""
Extraction de texte / tableaux / images d'un package PowerPoint embarqué :
- .sldx  : package à diapositive unique (cas typique des objets OLE dans les
           docx ACCO : word/embeddings/Microsoft_PowerPoint_SlideN.sldx)
- .pptx  : présentation classique multi-diapositives

Le texte est récupéré directement du XML (a:t), sans OCR : c'est sans perte
et gratuit. Les tableaux PowerPoint (a:tbl) sont de vraies grilles avec
merges (gridSpan / rowSpan).
"""
import io
import zipfile
from lxml import etree

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# réutilise parse_rels de extract_word (même structure de rels OOXML)
from extract_word import parse_rels


def _q(nsmap: dict, prefix: str, tag: str) -> str:
    uri = nsmap.get(prefix) or {
        "a": A, "p": P, "r": R,
    }[prefix]
    return f"{{{uri}}}{tag}"


def _slide_order(zf: zipfile.ZipFile) -> list:
    """Ordre des parties de diapositives dans le package."""
    names = set(zf.namelist())
    if "ppt/presentation.xml" in names:
        pres = etree.fromstring(zf.read("ppt/presentation.xml"))
        rels = parse_rels(zf, "ppt/_rels/presentation.xml.rels")
        order = []
        for sld in pres.iter(_q(pres.nsmap or {}, "p", "sldId")):
            rid = sld.get(f"{{{pres.nsmap.get('r') or R}}}id")
            if rid and rid in rels:
                target = rels[rid]["target"].lstrip("/")
                order.append(target if target.startswith("ppt/") else "ppt/" + target)
        return order
    # package à diapositive unique (.sldx) : ppt/slides/slide1.xml
    cands = sorted(
        n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")
    )
    return cands


def _slide_para(ap: etree._Element) -> dict:
    nsmap = ap.nsmap or {}
    at_tag = _q(nsmap, "a", "t")
    text = ""
    bold_chars = u_chars = 0
    total = 0
    size_pts = []
    for ar in ap.iter(_q(nsmap, "a", "r")):
        at = "".join(t.text or "" for t in ar.iter(at_tag))
        if not at:
            continue
        text += at
        total += len(at)
        rpr = ar.find(_q(nsmap, "a", "rPr"))
        b = rpr is not None and rpr.get("b") == "1"
        u = rpr is not None and rpr.get("u") not in (None, "none")
        if b:
            bold_chars += len(at)
        if u:
            u_chars += len(at)
        if rpr is not None and rpr.get("sz"):
            try:
                size_pts.append(int(rpr.get("sz")) / 100)  # 1/100 pt -> pt
            except ValueError:
                pass
    letters = [c for c in text if c.isalpha()]
    caps_frac = sum(1 for c in letters if c.isupper()) / len(letters) if letters else 0.0
    return {
        "type": "para",
        "text": text,
        "src": "slide",
        "style": None,
        "feat": {
            "caps": caps_frac,
            "bold_frac": bold_chars / total if total else 0.0,
            "u_frac": u_chars / total if total else 0.0,
            "size_pt": max(size_pts) if size_pts else None,
            "is_list": False,
            "outline_lvl": None,
        },
        "images": [],
        "objects": [],
    }


def _slide_table(tbl: etree._Element) -> dict:
    nsmap = tbl.nsmap or {}
    grid, spans, rowspans = [], [], []
    for tr in tbl.findall(_q(nsmap, "a", "tr")):
        row_text, row_span, row_rspan = [], [], []
        for tc in tr.findall(_q(nsmap, "a", "tc")):
            gs = tc.get("gridSpan")
            rs = tc.get("rowSpan")
            text = "\n".join(
                "".join(t.text or "" for t in ap.iter(_q(nsmap, "a", "t")))
                for ap in tc.iter(_q(nsmap, "a", "p"))
            ).strip()
            row_text.append(text)
            try:
                row_span.append(int(gs) if gs else 1)
            except ValueError:
                row_span.append(1)
            try:
                row_rspan.append(int(rs) if rs else 1)
            except ValueError:
                row_rspan.append(1)
        grid.append(row_text)
        spans.append(row_span)
        rowspans.append(row_rspan)
    n_cols = max((sum(row) for row in spans), default=0)
    return {
        "type": "table",
        "grid": grid,
        "spans": spans,
        "vmerge": [row[:] for row in rowspans],
        "n_rows": len(grid),
        "n_cols": n_cols,
    }


def _parse_slide(zf: zipfile.ZipFile, part: str) -> list:
    fname = part.split("/")[-1]
    rels = parse_rels(zf, f"ppt/slides/_rels/{fname}.rels")
    root = etree.fromstring(zf.read(part))
    nsmap = root.nsmap or {}
    r_uri = nsmap.get("r") or R
    blocks = []
    for el in root.iter():
        if el.tag == _q(nsmap, "p", "sp"):
            for ap in el.findall(f".//{_q(nsmap, 'a', 'p')}"):
                blocks.append(_slide_para(ap))
        elif el.tag == _q(nsmap, "p", "graphicFrame"):
            tbl = el.find(f".//{_q(nsmap, 'a', 'tbl')}")
            if tbl is not None:
                blocks.append(_slide_table(tbl))
        elif el.tag == _q(nsmap, "p", "pic"):
            blip = el.find(f".//{_q(nsmap, 'a', 'blip')}")
            rid = blip.get(f"{{{r_uri}}}embed") if blip is not None else None
            if rid and rid in rels and rels[rid]["type"] == "image":
                target = rels[rid]["target"].lstrip("/")
                media_part = target if target.startswith("ppt/") else "ppt/" + target
                try:
                    size = zf.getinfo(media_part).file_size
                except KeyError:
                    size = 0
                blocks.append(
                    {
                        "type": "figure",
                        "origin": "slide",
                        "image": {
                            "part": media_part,
                            "fmt": media_part.rsplit(".", 1)[-1].lower(),
                            "size": size,
                            "container": part,
                        },
                    }
                )
    return blocks


def extract_ppt(data: bytes) -> dict:
    """
    Retourne {"slides": [ [blocs] ... ], "media": {part: {size, fmt}}}.
    media couvre ppt/media/* de TOUT le package (les diapositives référencent
    des parties partagées).
    """
    zf = zipfile.ZipFile(io.BytesIO(data))
    media = {}
    for name in zf.namelist():
        if name.startswith("ppt/media/"):
            base = name.split("/")[-1]
            try:
                size = zf.getinfo(name).file_size
            except KeyError:
                size = 0
            media[name] = {
                "size": size,
                "fmt": base.rsplit(".", 1)[-1].lower() if "." in base else "",
            }
    slides = []
    for part in _slide_order(zf):
        try:
            slides.append(_parse_slide(zf, part))
        except Exception as e:
            slides.append([{"type": "slide_error", "part": part, "error": str(e)}])
    return {"slides": slides, "media": media, "zf": zf}
