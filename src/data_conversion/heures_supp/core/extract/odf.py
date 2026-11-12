"""
Deterministic extraction of blocks from an ODF (OpenDocument, .odt) file.

Some agreements are ODF (mimetype + content.xml) saved with a .docx
extension forced by the scraper. ODF is flat, readable XML:
- text:h      : native heading with text:outline-level (strong signal)
- text:p      : paragraph (text:span with styles -> bold/underline)
- table:table : table with number-columns/rows-spanned + covered-table-cell
- text:list   : lists (items marked is_list)
- draw:frame  : images (draw:image @xlink:href = media/...)

Returns the same block model as extract word / slides.
"""
import io
import zipfile
from lxml import etree

OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
STYLE = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
FO = "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
XLINK = "http://www.w3.org/1999/xlink"


class _Ns:
    __slots__ = ("office", "style", "text", "table", "draw", "fo", "xlink")

    def __init__(self, nsmap: dict):
        self.office = nsmap.get("office") or OFFICE
        self.style = nsmap.get("style") or STYLE
        self.text = nsmap.get("text") or TEXT
        self.table = nsmap.get("table") or TABLE
        self.draw = nsmap.get("draw") or DRAW
        self.fo = nsmap.get("fo") or FO
        self.xlink = nsmap.get("xlink") or XLINK

    def q(self, which: str, tag: str) -> str:
        return f"{{{getattr(self, which)}}}{tag}"


def _style_map(zf: zipfile.ZipFile, content_root: etree._Element, ns: _Ns,
               parser: etree.XMLParser) -> dict:
    """
    style-name -> {'bold': bool, 'underline': bool, 'size_pt': float|None,
    'outline': int|None} for text styles (content.xml automatic-styles
    + styles.xml).
    """
    out = {}

    def _scan(el):
        if el is None:
            return
        for st in el.iter(ns.q("style", "style")):
            name = st.get(ns.q("style", "name"))
            if name is None:
                continue
            entry = out.setdefault(name, {"bold": False, "underline": False, "size_pt": None, "outline": None})
            fw = st.find(ns.q("fo", "font-weight"))
            if fw is not None and fw.get("text") in ("bold", "bolder"):
                entry["bold"] = True
            td = st.find(ns.q("fo", "text-decoration"))
            if td is not None and "underline" in (td.get("text") or ""):
                entry["underline"] = True
            fs = st.find(ns.q("fo", "font-size"))
            if fs is not None and fs.get("text"):
                v = fs.get("text")
                try:
                    entry["size_pt"] = float(v[:-2]) if v.endswith("pt") else float(v) / 20.0
                except ValueError:
                    pass
            pp = st.find(ns.q("style", "paragraph-properties"))
            if pp is not None and pp.get(ns.q("text", "outline-level")):
                try:
                    entry["outline"] = int(pp.get(ns.q("text", "outline-level")))
                except ValueError:
                    pass
        # ODF text styles are <text:style> (not style:style)
        for st in el.iter(ns.q("text", "style")):
            name = st.get(ns.q("style", "name"))
            if name is None:
                continue
            entry = out.setdefault(name, {"bold": False, "underline": False, "size_pt": None, "outline": None})
            fw = st.find(ns.q("fo", "font-weight"))
            if fw is not None and fw.get("text") in ("bold", "bolder"):
                entry["bold"] = True
            td = st.find(ns.q("fo", "text-decoration"))
            if td is not None and "underline" in (td.get("text") or ""):
                entry["underline"] = True
            fs = st.find(ns.q("fo", "font-size"))
            if fs is not None and fs.get("text"):
                v = fs.get("text")
                try:
                    entry["size_pt"] = float(v[:-2]) if v.endswith("pt") else float(v) / 20.0
                except ValueError:
                    pass

    _scan(content_root.find(ns.q("office", "automatic-styles")))
    try:
        styles_root = etree.fromstring(zf.read("styles.xml"), parser)
        _scan(styles_root.find(ns.q("office", "styles")))
        _scan(styles_root.find(ns.q("office", "automatic-styles")))
    except (KeyError, etree.XMLSyntaxError):
        pass
    return out


def _para_block(ns: _Ns, el: etree._Element, style_map: dict,
                is_list: bool, outline_lvl: int | None) -> dict:
    text = "".join(el.itertext())
    bold_chars = u_chars = 0
    for span in el.iter(ns.q("text", "span")):
        st = style_map.get(span.get(ns.q("style", "name")) or "")
        if not st:
            continue
        n = len("".join(span.itertext()))
        if st["bold"]:
            bold_chars += n
        if st["underline"]:
            u_chars += n

    pstyle = el.get(ns.q("text", "style-name")) or ""
    total = max(len(text), 1)
    letters = [c for c in text if c.isalpha()]
    caps_frac = sum(1 for c in letters if c.isupper()) / len(letters) if letters else 0.0

    # size: paragraph style, otherwise the span median is not available here
    size_pt = None
    st = style_map.get(pstyle)
    if st and st["size_pt"]:
        size_pt = st["size_pt"]

    return {
        "type": "para",
        "text": text,
        "src": "odf",
        "style": pstyle or None,
        "feat": {
            "caps": caps_frac,
            "bold_frac": bold_chars / total,
            "u_frac": u_chars / total,
            "size_pt": size_pt,
            "is_list": is_list,
            "outline_lvl": outline_lvl,
        },
        "images": [],
        "objects": [],
    }


def _table_block(ns: _Ns, tbl: etree._Element) -> dict:
    grid, spans, vmerge = [], [], []
    for tr in tbl.findall(ns.q("table", "table-row")):
        row_text, row_span, row_rs = [], [], []
        for cell in tr:
            if cell.tag == ns.q("table", "table-cell"):
                try:
                    nc = int(cell.get(ns.q("table", "number-columns-spanned")) or 1)
                except ValueError:
                    nc = 1
                try:
                    nr = int(cell.get(ns.q("table", "number-rows-spanned")) or 1)
                except ValueError:
                    nr = 1
                txt = "\n".join(
                    "".join(p.itertext()) for p in cell.iter(ns.q("text", "p"))
                ).strip()
                row_text.append(txt)
                row_span.append(nc)
                row_rs.append(nr)
            elif cell.tag == ns.q("table", "covered-table-cell"):
                row_text.append("")
                row_span.append(1)
                row_rs.append(1)
        grid.append(row_text)
        spans.append(row_span)
        vmerge.append(row_rs)
    # n_cols: max number of row elements (covered cells occupy a column)
    n_cols = max((len(r) for r in grid), default=0)
    return {
        "type": "table",
        "grid": grid,
        "spans": spans,
        "vmerge": vmerge,
        "n_rows": len(grid),
        "n_cols": n_cols,
    }


def _figures_in(ns: _Ns, el: etree._Element, zf: zipfile.ZipFile) -> list:
    out = []
    for frame in el.iter(ns.q("draw", "frame")):
        img = frame.find(ns.q("draw", "image"))
        if img is None:
            continue
        href = img.get(f"{{{ns.xlink}}}href") or ""
        if not href:
            continue
        part = href.lstrip("/")
        fmt = part.rsplit(".", 1)[-1].lower() if "." in part else ""
        size = 0
        try:
            size = zf.getinfo(part).file_size
        except KeyError:
            pass
        out.append({"part": part, "fmt": fmt, "size": size})
    return out


def extract_odf(data: bytes) -> dict:
    """
    Returns {"blocks", "info", "zf"} (same shape as extract_docx).
    """
    zf = zipfile.ZipFile(io.BytesIO(data))
    # Tolerant parsing: some ODF files have invalid xml:id (NCName) that
    # make a strict parse fail.
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(zf.read("content.xml"), parser)
    ns = _Ns(root.nsmap or {})
    style_map = _style_map(zf, root, ns, parser)

    body = root.find(f"{ns.q('office','body')}/{ns.q('office','text')}")
    if body is None:
        raise ValueError("content.xml has no office:text (non-textual ODF file?)")

    blocks = []
    n_figs = 0

    def walk(el, in_list: bool):
        nonlocal n_figs
        for child in el:
            tag = child.tag
            if tag == ns.q("text", "h"):
                try:
                    lvl = int(child.get(ns.q("text", "outline-level")) or 1)
                except ValueError:
                    lvl = 1
                blocks.append(_para_block(ns, child, style_map, False, lvl))
            elif tag == ns.q("text", "p"):
                # some documents do not use text:h at all: the level is
                # carried by the paragraph style (paragraph-properties
                # text:outline-level in styles.xml / automatic-styles).
                st = style_map.get(child.get(ns.q("text", "style-name")) or "")
                blocks.append(_para_block(ns, child, style_map, in_list, st.get("outline") if st else None))
            elif tag == ns.q("text", "list"):
                walk(child, True)
            elif tag == ns.q("text", "list-item"):
                walk(child, in_list or True)
            elif tag == ns.q("table", "table"):
                blocks.append(_table_block(ns, child))
            elif tag == ns.q("draw", "page"):
                walk(child, in_list)
            # draw:frame and others: ignored here (images collected globally)

    walk(body, False)

    # document images (unreliable position: appended at the end, like for PDF)
    figures = _figures_in(ns, body, zf)

    return {
        "blocks": blocks,
        "figures": figures,
        "info": {"n_figures": len(figures)},
        "zf": zf,
    }
