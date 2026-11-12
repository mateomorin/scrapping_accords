"""
Extraction déterministe des blocs (paragraphes, tableaux, images, objets
embarqués) d'un fichier DOCX, au niveau zip/XML (lxml).

Pourquoi pas python-docx ? Il n'expose pas les objets OLE embarqués
(p. ex. des diapositives PowerPoint intégrées), qui sont pourtant le
contenu principal de certains accords.

Le document est modélisé comme une liste ordonnée de blocs :
    {"type": "para",     "text", "feat", "images": [figspec], "objects": [part]}
    {"type": "table",    "grid", "spans", "vmerge", "n_rows", "n_cols"}

figspec = {"part": "word/media/image3.emf", "fmt": "emf", "size": 12345}

Robustesse :
- les espaces de noms sont résolus par DOCUMENT à partir de la racine
  (certains éditeurs émettent la variante purl.oclc.org au lieu de
  schemas.openxmlformats.org),
- la partie principale du document est découverte via _rels/.rels
  (type officeDocument) : certains fichiers ont word/document2.xml,
- le XML est parsé en mode "recover" pour tolérer les caractères
  PCDATA invalides (char. de contrôle).
"""
import io
import os
import zipfile
from lxml import etree

# Valeurs par défaut (variantes standards OOXML)
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
V = "urn:schemas-microsoft-com:vml"
O = "urn:schemas-microsoft-com:office:office"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"


class _Ns:
    """Espaces de noms résolus pour un document donné."""

    __slots__ = ("W", "R", "V", "O", "A", "WP")

    def __init__(self, nsmap: dict):
        self.W = nsmap.get("w") or W
        self.R = nsmap.get("r") or R
        self.V = nsmap.get("v") or V
        self.O = nsmap.get("o") or O
        self.A = nsmap.get("a") or A
        self.WP = nsmap.get("wp") or WP

    def q(self, which: str, tag: str) -> str:
        return f"{{{getattr(self, which)}}}{tag}"


def parse_rels(zf: zipfile.ZipFile, rels_path: str) -> dict:
    """rId -> {'type': str (dernier segment), 'target': str}."""
    out = {}
    try:
        xml = zf.read(rels_path)
    except KeyError:
        return out
    root = etree.fromstring(xml)
    for rel in root:
        if not (rel.tag.endswith("}Relationship") or rel.tag == "Relationship"):
            continue
        out[rel.get("Id")] = {
            "type": (rel.get("Type") or "").rsplit("/", 1)[-1],
            "target": rel.get("Target") or "",
        }
    return out


def _find_main_part(zf: zipfile.ZipFile) -> str:
    """
    Partie principale du document (normalement word/document.xml).
    Découverte via la relation officeDocument de _rels/.rels ; certains
    fichiers ont word/document2.xml ou autre.
    """
    names = set(zf.namelist())
    for rel in parse_rels(zf, "_rels/.rels").values():
        if rel["type"] == "officeDocument":
            part = rel["target"].lstrip("/")
            if part in names:
                return part
    if "word/document.xml" in names:
        return "word/document.xml"
    # dernier recours : toute partie word/document*.xml
    for n in sorted(names):
        base = os.path.basename(n)
        if n.startswith("word/") and base.startswith("document") and n.endswith(".xml"):
            return n
    raise KeyError("partie principale du document introuvable")


def _doc_rels_path(main_part: str) -> str:
    d = os.path.dirname(main_part)
    b = os.path.basename(main_part)
    return f"{d}/_rels/{b}.rels" if d else f"_rels/{b}.rels"


def _resolve_part(target: str, base_dir: str) -> str:
    """Target relatif aux rels d'une partie -> nom complet dans le zip."""
    if target.startswith("/"):
        return target.lstrip("/")
    if base_dir:
        return f"{base_dir}/{target}"
    return target


def _media_index(zf: zipfile.ZipFile, prefix: str) -> dict:
    """part -> {'size', 'fmt'} pour les parties media/ d'un zip."""
    out = {}
    for name in zf.namelist():
        if name.startswith(prefix):
            base = name[len(prefix):]
            fmt = base.rsplit(".", 1)[-1].lower() if "." in base else ""
            try:
                size = zf.getinfo(name).file_size
            except KeyError:
                size = 0
            out[f"{prefix}{base}"] = {"size": size, "fmt": fmt}
    return out


def _fig_spec(rid: str, rels: dict, zf: zipfile.ZipFile, base_dir: str) -> dict | None:
    rel = rels.get(rid)
    if rel is None or rel["type"] != "image":
        return None
    part = _resolve_part(rel["target"], base_dir)
    info = {"part": part, "fmt": part.rsplit(".", 1)[-1].lower(), "size": 0}
    try:
        info["size"] = zf.getinfo(part).file_size
    except KeyError:
        pass
    return info


def _run_features(ns: _Ns, r: etree._Element) -> tuple:
    rpr = r.find(ns.q("W", "rPr"))
    text = "".join(t.text or "" for t in r.iter(ns.q("W", "t")))
    feat = {"b": False, "u": False, "caps": False, "sz": None}
    if rpr is not None:
        b = rpr.find(ns.q("W", "b"))
        feat["b"] = b is not None and b.get(ns.q("W", "val")) not in ("0", "false")
        u = rpr.find(ns.q("W", "u"))
        feat["u"] = u is not None and u.get(ns.q("W", "val")) not in (None, "none")
        c = rpr.find(ns.q("W", "caps"))
        feat["caps"] = c is not None and c.get(ns.q("W", "val")) not in ("0", "false")
        sz = rpr.find(ns.q("W", "sz"))
        if sz is not None:
            try:
                feat["sz"] = int(sz.get(ns.q("W", "val")))  # demi-points
            except (TypeError, ValueError):
                pass
    return text, feat


def parse_para(ns: _Ns, p: etree._Element, rels: dict, zf: zipfile.ZipFile,
               base_dir: str) -> dict:
    text_parts = []
    bold_chars = u_chars = caps_chars = 0
    total = 0
    size_pts = []
    for r in p.iter(ns.q("W", "r")):
        text, feat = _run_features(ns, r)
        if not text:
            continue
        text_parts.append(text)
        total += len(text)
        if feat["b"]:
            bold_chars += len(text)
        if feat["u"]:
            u_chars += len(text)
        if feat["caps"]:
            caps_chars += len(text)
        if feat["sz"]:
            size_pts.append(feat["sz"] / 2)  # demi-points -> points

    text = "".join(text_parts)
    ppr = p.find(ns.q("W", "pPr"))
    style, is_list, outline = None, False, None
    if ppr is not None:
        ps = ppr.find(ns.q("W", "pStyle"))
        if ps is not None:
            style = ps.get(ns.q("W", "val"))
        if ppr.find(ns.q("W", "numPr")) is not None:
            is_list = True
        ol = ppr.find(ns.q("W", "outlineLvl"))
        if ol is not None:
            try:
                outline = int(ol.get(ns.q("W", "val")))
            except (TypeError, ValueError):
                pass

    letters = [c for c in text if c.isalpha()]
    caps_frac = sum(1 for c in letters if c.isupper()) / len(letters) if letters else 0.0

    # --- objets OLE embarqués (partie + image de prévisualisation) ---
    objects = []
    obj_preview_rids = set()
    for obj in p.iter(ns.q("W", "object")):
        entry = None
        ole = obj.find(f".//{ns.q('O', 'OLEObject')}")
        rid = ole.get(ns.q("R", "id")) if ole is not None else None
        if rid and rid in rels:
            entry = {"part": _resolve_part(rels[rid]["target"], base_dir), "preview": None}
        for im in obj.iter(f"{ns.q('V', 'imagedata')}"):
            prid = im.get(ns.q("R", "id"))
            if not prid:
                continue
            obj_preview_rids.add(prid)
            spec = _fig_spec(prid, rels, zf, base_dir)
            if entry is None:
                entry = {"part": None, "preview": spec}
            elif entry["preview"] is None:
                entry["preview"] = spec
        if entry is not None:
            objects.append(entry)

    # --- images inline (hors prévisualisations d'objets) ---
    images = []
    seen = set()
    for im in p.iter(f"{ns.q('V', 'imagedata')}"):
        rid = im.get(ns.q("R", "id"))
        if not rid or rid in seen or rid in obj_preview_rids:
            continue
        seen.add(rid)
        spec = _fig_spec(rid, rels, zf, base_dir)
        if spec:
            images.append(spec)
    for blip in p.iter(f"{ns.q('A', 'blip')}"):
        rid = blip.get(f"{{{ns.R}}}embed")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        spec = _fig_spec(rid, rels, zf, base_dir)
        if spec:
            images.append(spec)

    return {
        "type": "para",
        "text": text,
        "src": "word",
        "style": style,
        "feat": {
            "caps": caps_frac,
            "bold_frac": bold_chars / total if total else 0.0,
            "u_frac": u_chars / total if total else 0.0,
            "size_pt": max(size_pts) if size_pts else None,
            "is_list": is_list,
            "outline_lvl": outline,
        },
        "images": images,
        "objects": objects,
    }


def parse_table(ns: _Ns, tbl: etree._Element) -> dict:
    grid, spans, vmerge = [], [], []
    for tr in tbl.findall(ns.q("W", "tr")):
        row_text, row_span, row_vm = [], [], []
        for tc in tr.findall(ns.q("W", "tc")):
            tcpr = tc.find(ns.q("W", "tcPr"))
            span, vm = 1, None
            if tcpr is not None:
                gs = tcpr.find(ns.q("W", "gridSpan"))
                if gs is not None:
                    try:
                        span = int(gs.get(ns.q("W", "val")))
                    except (TypeError, ValueError):
                        span = 1
                vme = tcpr.find(ns.q("W", "vMerge"))
                if vme is not None:
                    vm = vme.get(ns.q("W", "val")) or "continue"
            cell_text = "\n".join(
                "".join(t.text or "" for t in p.iter(ns.q("W", "t")))
                for p in tc.iter(ns.q("W", "p"))
            ).strip()
            row_text.append(cell_text)
            row_span.append(span)
            row_vm.append(vm)
        grid.append(row_text)
        spans.append(row_span)
        vmerge.append(row_vm)
    n_cols = max((sum(row) for row in spans), default=0)
    return {
        "type": "table",
        "grid": grid,
        "spans": spans,
        "vmerge": vmerge,
        "n_rows": len(grid),
        "n_cols": n_cols,
    }


def _xml_recover(data: bytes) -> etree._Element:
    """Parsage tolérant (char. PCDATA invalides : char. de contrôle)."""
    parser = etree.XMLParser(recover=True, huge_tree=True)
    return etree.fromstring(data, parser)


def extract_docx(data: bytes) -> dict:
    """
    Retourne {"blocks", "info", "zf"}.
    zf est conservé ouvert : les parties embarquées (.sldx...) s'y lisent ensuite.
    """
    zf = zipfile.ZipFile(io.BytesIO(data))
    main_part = _find_main_part(zf)
    base_dir = os.path.dirname(main_part)
    rels = parse_rels(zf, _doc_rels_path(main_part))
    root = _xml_recover(zf.read(main_part))
    ns = _Ns(root.nsmap or {})
    body = root.find(ns.q("W", "body"))

    blocks = []
    if body is None:
        raise ValueError(f"document principal {main_part!r} sans <body>")
    for el in body:
        if el.tag == ns.q("W", "p"):
            blocks.append(parse_para(ns, el, rels, zf, base_dir))
        elif el.tag == ns.q("W", "tbl"):
            blocks.append(parse_table(ns, el))
        # w:sectPr et autres : ignorés

    # --- inventaire des images d'en-tête/pied de page (jamais OCR'd) ---
    hf_parts = set()
    for name in zf.namelist():
        if (name.startswith(f"{base_dir}/_rels/header") or name.startswith(f"{base_dir}/_rels/footer")) \
                and name.endswith(".rels"):
            for rel in parse_rels(zf, name).values():
                if rel["type"] == "image":
                    hf_parts.add(_resolve_part(rel["target"], base_dir))

    embedded_parts = []
    for b in blocks:
        if b["type"] == "para":
            for o in b["objects"]:
                if o.get("part"):
                    embedded_parts.append(o["part"])

    doc_info = {
        "n_header_footer_images": len(hf_parts),
        "media": _media_index(zf, f"{base_dir}/media/"),
        "embedded_parts": embedded_parts,
        "has_ppt_embed": any(
            p.lower().endswith((".sldx", ".pptx")) for p in embedded_parts
        ),
        "main_part": main_part,
    }
    return {"blocks": blocks, "info": doc_info, "zf": zf}
