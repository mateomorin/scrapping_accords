"""
Heuristic recovery of document structure (headings / sections).

The agreements do not encode their titles in Word styles: they may be bold,
underlined, in CAPS, numbered (arabic, roman, decimal, letters) or a mix of
all. This module scores each paragraph with independent signals, then builds
a section tree.

Important guarantees:
- the structure is a LOSSLESS INDEX over the ordered raw content: no text is
  ever lost; a false negative simply leaves more text in a flat section;
- each section carries `confidence` (high/medium/low) and `evidence` (the
  signals that voted), so the consumer (RAG) can arbitrate.

Level assignment (see HEURES_SUPP.md, "Structure: level rule"):
    level = min(max(base + offset, 1), 5)
where `base` is the depth of the heading within its numbering family, and
`offset` (computed once per document by compute_offset) is 1 when the
document contains at least one container title (TITRE / CHAPITRE / ...), so
that articles attach one level below their container. Native format levels
(Word outlineLvl, ODF text:outline-level) are used as-is, without offset.
"""
import re
from statistics import median

RE_ARTICLE = re.compile(
    r"^(?:Article|ARTICLE|art\.|Art\.)\s*(?:1er\b|\d+(?:\.\d{1,2}){0,4})"
)
# Decimal: "11.3.2.1" (dot chains allowed) or "1." / "2)" (a dot/parenthesis
# is MANDATORY at level 1, to avoid dates like "17 Novembre 2017").
RE_DECIMAL = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,4})(?P<sep>[.)]\s*|\s)\s*\S")
RE_ROMAN = re.compile(r"^(?:I{1,3}V?|IV|VI{0,3}|IX|XI{0,3})[.:–\-]\s+\S")
RE_LETTER = re.compile(r"^[A-Z][.:–\-]\s+\S")
RE_SECTION_KW = re.compile(
    r"^(?:PREAMBULE|ANNEXE|TITRE|CHAPITRE|PARTIE|LIVRE|EXPOSÉ\s+DES\s+MOTIFS)\b", re.I
)
# CONTAINER title keywords: a TITRE/CHAPITRE has a scope (the articles that
# follow it belong to it). PREAMBULE is a sibling, not a container: it does
# not trigger the offset. (Patterns match French document content: kept as-is.)
RE_CONTAINER_KW = re.compile(
    r"^(?:TITRE|CHAPITRE|PARTIE|LIVRE|EXPOSÉ\s+DES\s+MOTIFS|ANNEXE)\b", re.I
)
RE_PREAMBULE_KW = re.compile(r"^PREAMBULE\b", re.I)
RE_FAIT_A = re.compile(r"^Fait\s+[àa]\s", re.I)
RE_PARTIES = re.compile(r"^(?:Entre\s+les\s+soussign|Soussign[ée]s\s*:?)", re.I)
# Page reference at the end ("...effectif7", "...APPLICATION 7", "...:8"):
# marker of table-of-contents entries copied into the body. Years (4 digits)
# are not affected.
RE_TOC_PAGE_TAIL = re.compile(r"[:\s]?\d{1,3}$")
# Censored party name ("EEEEEE", "XXXXXXXX"): not a heading.
RE_PLACEHOLDER = re.compile(r"^(.)\1{3,}$")
# Boilerplate preamble formulas (parties / convening): CAPS but not headings.
# The apostrophe may be ASCII (') or typographic (’).
RE_BOILERPLATE = re.compile(
    r"^(?:ENTRE[S]?\s+LES\s+SOUS|ENTRE\s*[,;:]?\s*$|D['\u2019]?\s*UNE\s+PART[,;:.]*|D['\u2019]?\s*AUTRE\s+PART[,;:.]*|"
    r"SOUSSIGN[ÉE]S?\s*:|IL\s+A\s+ET[ÉE]\s+CONVENU|CE\s+QUI\s+SUIT|"
    r"EN\s+PR[ÉE]SE\s+ANCE|Fait\s+[àa]|ET[,;:]?)$",
    re.I,
)


def _words(text: str) -> list:
    return [w for w in re.split(r"\s+", text.strip()) if w]


def score_paragraph(text: str, feat: dict) -> tuple:
    """
    Returns (score, evidence, is_heading, kind, base, number, outline_lvl).

    kind  : "container" | "preamble" | "article" | "decimal" | "letter"
            | "roman" | "plain"
    base  : depth of the heading within its numbering family (>= 0).
            The final level is computed later (compute_offset /
            build_structure) to account for the container titles of the
            document (TITRE/CHAPITRE...): an article under a TITRE is one
            level lower.
    number / outline_lvl are meaningful only if is_heading is True.
    """
    t = text.strip()
    if not t:
        return 0, [], False, None, None, None, None

    n = len(_words(t))
    s = 0
    ev = []
    outline_lvl = feat.get("outline_lvl")

    # --- strong / explicit signals ---
    if outline_lvl is not None:
        s += 3
        ev.append(f"outline_lvl={outline_lvl}")
    m_art = RE_ARTICLE.match(t)
    m_dec = RE_DECIMAL.match(t)
    m_rom = RE_ROMAN.match(t)
    m_let = RE_LETTER.match(t)
    m_kw = RE_SECTION_KW.match(t)
    # depth of decimal-numbered articles: "Article 5.1" -> 2
    art_depth = None
    if m_art:
        num = m_art.group(0).split()[-1]
        if "." in num:
            art_depth = num.count(".") + 1
    dec_depth = None
    if m_dec:
        num = m_dec.group("num")
        if "." in num:
            dec_depth = num.count(".") + 1
        elif m_dec.group("sep").startswith((".", ")")):
            dec_depth = 1
    if m_art:
        s += 3
        ev.append("kw-article")
    if m_rom:
        s += 2
        ev.append("roman-num")
    if dec_depth is not None and n <= 15:
        s += 2
        ev.append(f"decimal-num(L{dec_depth})")
    if m_let and n <= 12:
        s += 1
        ev.append("letter-level")
    if m_kw and n <= 12:
        s += 3
        ev.append("kw-section")
    # Hard veto: boilerplate formulas and censored names are NEVER headings,
    # whatever the typography (including CAPS + bold).
    is_boiler = bool(RE_BOILERPLATE.match(t))
    is_placeholder = bool(RE_PLACEHOLDER.match(re.sub(r"\s+", "", t)))
    hard_veto = is_boiler or is_placeholder
    # A real heading almost never ends with a bare 1-3 digit number: that is
    # the marker of TOC entries.
    toc_like = n >= 4 and bool(RE_TOC_PAGE_TAIL.search(t))

    # --- typographic signals ---
    caps = feat.get("caps", 0.0)
    bold = feat.get("bold_frac", 0.0)
    u = feat.get("u_frac", 0.0)
    caps_short = caps >= 0.8 and n <= 10
    bold_short = bold >= 0.8 and n <= 12
    if caps_short:
        s += 2
        ev.append("caps-short")
    if bold_short:
        s += 2
        ev.append("bold-short")
    if u >= 0.8 and n <= 12:
        s += 1
        ev.append("underline-short")
    if feat.get("size_pt") and feat.get("doc_median_pt") and feat["size_pt"] >= feat["doc_median_pt"] * 1.15:
        s += 1
        ev.append("size-large")

    # --- penalties ---
    if hard_veto:
        s -= 4
        ev.append("boilerplate" if is_boiler else "placeholder")
    if toc_like:
        s -= 3
        ev.append("toc-page-ref")
    if feat.get("is_list") and not (caps_short and bold_short):
        s -= 3
        ev.append("list-item")
    if n > 15:
        s -= 2
        ev.append("long")
    if t.endswith(".") and n > 8:
        s -= 1
        ev.append("sentence-end")

    strong_typo = caps_short and bold_short
    has_numbering = any(x is not None for x in (m_art, m_rom, m_let)) or dec_depth is not None
    is_heading = (
        not hard_veto
        and (s >= 3 or (s >= 1 and not toc_like and (has_numbering or strong_typo or "kw-section" in ev)))
    )

    # --- family / depth ---
    # base = relative depth of the heading (>= 0). Final level:
    #   level = min(max(base + offset, 1), 5)
    # with offset = 1 when the document contains container titles.
    # Containers (TITRE/CHAPITRE...) and their "siblings" (PREAMBULE, roman
    # sections) have depth 0: they stay at level 1 even with offset (a TITRE
    # is the root of the hierarchy).
    kind, base, number = None, None, None
    if is_heading:
        if m_kw:
            kind = "container" if RE_CONTAINER_KW.match(t) else "preamble"
            base, number = 0, None
        elif m_art:
            kind = "article"
            base = art_depth if art_depth is not None else 1
            number = m_art.group(0).strip()
        elif m_rom:
            kind, base, number = "roman", 0, m_rom.group(0).rstrip(" .:–-")
        elif dec_depth is not None:
            kind, base = "decimal", dec_depth
            number = m_dec.group("num")
        elif m_let:
            kind, base, number = "letter", 2, m_let.group(0).rstrip(" .:–-")
        else:
            kind, base = "plain", 1
    return s, ev, is_heading, kind, base, number, outline_lvl


def compute_offset(scored: dict) -> int:
    """
    Document-wide level offset: if the document contains at least one
    accepted container title (TITRE / CHAPITRE / PARTIE / LIVRE /
    EXPOSÉ DES MOTIFS / ANNEXE), articles (and their sub-hierarchy) are one
    level lower: they belong to the container that precedes them. Otherwise
    (no container in the document) offset = 0 and levels are identical to
    the base rule.
    """
    return 1 if any(
        res[2] and res[3] == "container" for res in scored.values()
    ) else 0


def find_signature_start(para_units: list) -> int | None:
    """
    Index of the first 'Fait à ...' paragraph located after half the document
    (signature zone). None if not found.
    """
    n = len(para_units)
    for i in range(n // 2, n):
        if RE_FAIT_A.match(para_units[i]["text"].strip()):
            return i
    return None


def build_structure(items: list, scored: dict, para_units: list,
                    signature_start: int | None, offset: int = 0) -> dict:
    """
    Builds the section tree in a single pass over the document items.

    items     : ordered list; each item:
                  {"kind": "para",   "idx": para_idx, "u": {"text","feat","src"}}
                  {"kind": "table",  "tbl": table_block, "rattach": para_idx|None, "id": "T.."}
                  {"kind": "figure", "fig": dict, "rattach": para_idx|None, "id": "F.."}
    scored    : {para_idx: (score, ev, is_heading, kind, base, number, outline_lvl)}
    para_units: list of {"text","feat","src"} (the projected "para" items).
    offset    : 1 if the document contains container titles (TITRE /
                CHAPITRE / ...): articles are then attached one level lower
                (the scope of the title). 0 otherwise.

    Returns {"root", "sections", "signature"}; each section:
      {id, level, number, title, confidence, evidence, parent,
       paras: [para_idx...], table_ids: [], figure_ids: [], body_md, n_paras}
    Table/figure items get a "sec_id" key (section, "root" or "signature").
    """
    root = {
        "id": "root",
        "level": 0,
        "number": None,
        "title": "",
        "confidence": None,
        "evidence": [],
        "parent": None,
        "paras": [],
        "table_ids": [],
        "figure_ids": [],
    }
    sections = []
    stack = []
    last_by_title = {}  # (level, normalized_title) -> section (repeated banner detection)
    signature = {"paras": [], "table_ids": [], "figure_ids": [], "start_idx": signature_start}

    def in_signature(para_idx) -> bool:
        return signature_start is not None and para_idx is not None and para_idx >= signature_start

    for it in items:
        kind = it["kind"]
        if kind == "para":
            i = it["idx"]
            u = it["u"]
            if in_signature(i):
                if u["text"].strip():
                    signature["paras"].append(i)
                continue
            res = scored.get(i)
            if res and res[2]:
                _, ev, _, hkind, base, number, outline_lvl = res
                if outline_lvl is not None:
                    # native level from the format (ODF text:h / Word outlineLvl):
                    # already an absolute level, no offset.
                    level = min(max(int(outline_lvl), 1), 5)
                else:
                    level = min(max(base + offset, 1), 5)
                    # unnumbered heading at the very start of the document:
                    # stays L1 even if containers exist further down.
                    if hkind == "plain" and not stack:
                        level = 1
                title_norm = re.sub(r"\s+", " ", u["text"].strip()).casefold()
                key = (level, title_norm)
                if key in last_by_title:
                    # Repeated title (slide banner, repeated header):
                    # no new section; the repeated line is dropped.
                    continue
                while stack and stack[-1]["level"] >= level:
                    stack.pop()
                conf = "high" if res[0] >= 4 else ("medium" if res[0] >= 3 else "low")
                sec = {
                    "id": f"S{len(sections) + 1}",
                    "level": level,
                    "number": number,
                    "title": u["text"].strip(),
                    "confidence": conf,
                    "evidence": ev,
                    "parent": stack[-1]["id"] if stack else None,
                    "paras": [],
                    "table_ids": [],
                    "figure_ids": [],
                }
                sections.append(sec)
                stack.append(sec)
                last_by_title[key] = sec
            cur = stack[-1] if stack else root
            cur["paras"].append(i)
        else:  # table / figure
            rattach = it.get("rattach")
            if in_signature(rattach):
                it["sec_id"] = "signature"
                (signature["table_ids"] if kind == "table" else signature["figure_ids"]).append(it["id"])
            else:
                cur = stack[-1] if stack else root
                it["sec_id"] = cur["id"]
                (cur["table_ids"] if kind == "table" else cur["figure_ids"]).append(it["id"])

    for sec in [root] + sections:
        sec["body_md"] = "\n\n".join(
            para_units[i]["text"].strip() for i in sec["paras"] if para_units[i]["text"].strip()
        )
        sec["n_paras"] = len(sec["paras"])

    if signature_start is not None:
        signature["text_md"] = "\n\n".join(
            para_units[i]["text"].strip() for i in signature["paras"]
        )

    return {"root": root, "sections": sections, "signature": signature}


def doc_median_size_pt(para_units: list) -> float | None:
    sizes = [u["feat"].get("size_pt") for u in para_units if u["feat"].get("size_pt")]
    if not sizes:
        return None
    return float(median(sizes))
