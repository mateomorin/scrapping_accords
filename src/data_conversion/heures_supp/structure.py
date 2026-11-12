"""
Récupération heuristique de la structure des documents (titres / sections).

Les accords n'encodent pas leurs titres dans les styles Word : ils peuvent
être en gras, soulignés, en CAPITALES, numérotés (arabe, romain, décimal,
lettres) ou un mélange. Ce module score chaque paragraphe avec des signaux
indépendants, puis construit un arbre de sections.

Garanties importantes :
- la structure est un INDEX par-dessus le contenu ordonné brut : aucun texte
  n'est jamais perdu ; un faux négatif laisse simplement plus de texte dans
  une section plate ;
- chaque section porte `confidence` (high/medium/low) et `evidence` (les
  signaux qui ont voté), pour que le consommateur (RAG) puisse arbitrer.
"""
import re
from statistics import median

RE_ARTICLE = re.compile(r"^(?:Article|ARTICLE|art\.|Art\.)\s*(?:1er\b|\d+)")
# Décimal : "11.3.2.1" (pointillés autorisés) ou "1." / "2)" (point/parenthèse
# OBLIGATOIRE au niveau 1, pour éviter les dates comme "17 Novembre 2017").
RE_DECIMAL = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,4})(?P<sep>[.)]\s*|\s)\s*\S")
RE_ROMAN = re.compile(r"^(?:I{1,3}V?|IV|VI{0,3}|IX|XI{0,3})[.:–\-]\s+\S")
RE_LETTER = re.compile(r"^[A-Z][.:–\-]\s+\S")
RE_SECTION_KW = re.compile(
    r"^(?:PREAMBULE|ANNEXE|TITRE|CHAPITRE|EXPOSÉ\s+DES\s+MOTIFS)\b", re.I
)
RE_FAIT_A = re.compile(r"^Fait\s+[àa]\s", re.I)
RE_PARTIES = re.compile(r"^(?:Entre\s+les\s+soussign|Soussign[ée]s\s*:?)", re.I)
# Référence de page à la fin ("...effectif7", "...APPLICATION 7", "...:8") :
# signe des entrées de table des matières (sommaire) reprises dans le corps.
# Les années (4 chiffres) ne sont pas concernées.
RE_TOC_PAGE_TAIL = re.compile(r"[:\s]?\d{1,3}$")
# Nom de partie censuré ("EEEEEE", "XXXXXXXX") : pas un titre.
RE_PLACEHOLDER = re.compile(r"^(.)\1{3,}$")
# Formules de chapeau (parties / convocation) : CAPITALES mais pas des titres.
# L'apostrophe peut être ASCII (') ou typographique (’).
RE_BOILERPLATE = re.compile(
    r"^(?:ENTRE[S]?\s+LES\s+SOUS|ENTRE\s*[,;:]?\s*$|D['\u2019]?\s*UNE\s+PART|D['\u2019]?\s*AUTRE\s+PART|"
    r"SOUSSIGN[ÉE]S?\s*:|IL\s+A\s+ET[ÉE]\s+CONVENU|CE\s+QUI\s+SUIT|"
    r"EN\s+PR[ÉE]SE\s+ANCE|Fait\s+[àa]|ET[,;:]?)$",
    re.I,
)


def _words(text: str) -> list:
    return [w for w in re.split(r"\s+", text.strip()) if w]


def score_paragraph(text: str, feat: dict) -> tuple:
    """
    Retourne (score, evidence, is_heading, level, number).
    level/number ne sont significatifs que si is_heading est True.
    """
    t = text.strip()
    if not t:
        return 0, [], False, None, None

    n = len(_words(t))
    s = 0
    ev = []

    # --- signaux forts / explicites ---
    if feat.get("outline_lvl") is not None:
        s += 3
        ev.append(f"outline_lvl={feat['outline_lvl']}")
    m_art = RE_ARTICLE.match(t)
    m_dec = RE_DECIMAL.match(t)
    m_rom = RE_ROMAN.match(t)
    m_let = RE_LETTER.match(t)
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
    if RE_SECTION_KW.match(t) and n <= 12:
        s += 3
        ev.append("kw-section")
    # Veto dur : formules de chapeau et noms censurés ne sont JAMAIS des
    # titres, quelle que soit la typographie (CAPS + gras compris).
    is_boiler = bool(RE_BOILERPLATE.match(t))
    is_placeholder = bool(RE_PLACEHOLDER.match(re.sub(r"\s+", "", t)))
    hard_veto = is_boiler or is_placeholder
    # Un vrai titre ne se termine pratiquement jamais par un simple numéro
    # de 1-3 chiffres : c'est le marqueur des entrées de sommaire.
    toc_like = n >= 4 and bool(RE_TOC_PAGE_TAIL.search(t))

    # --- signaux typographiques ---
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

    # --- pénalités ---
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

    # --- niveau / numéro ---
    level, number = None, None
    if is_heading:
        if m_art:
            level = 1
            number = m_art.group(0).strip()
        elif m_rom:
            level, number = 1, m_rom.group(0).rstrip(" .:–-")
        elif dec_depth is not None:
            level = min(dec_depth, 5)
            number = m_dec.group("num")
        elif m_let:
            level, number = 2, m_let.group(0).rstrip(" .:–-")
        else:
            level = 1
    return s, ev, is_heading, level, number


def find_signature_start(para_units: list) -> int | None:
    """
    Index du premier paragraphe 'Fait à ...' situé après la moitié du document
    (zone de signature). None si introuvable.
    """
    n = len(para_units)
    for i in range(n // 2, n):
        if RE_FAIT_A.match(para_units[i]["text"].strip()):
            return i
    return None


def build_structure(items: list, scored: dict, para_units: list, signature_start: int | None) -> dict:
    """
    Construit l'arbre de sections en une seule passe sur les items du document.

    items     : liste ordonnée ; chaque item :
                  {"kind": "para",   "idx": para_idx, "u": {"text","feat","src"}}
                  {"kind": "table",  "tbl": bloc_table, "rattach": para_idx|None, "id": "T.."}
                  {"kind": "figure", "fig": dict, "rattach": para_idx|None, "id": "F.."}
    scored    : {para_idx: (score, ev, is_heading, level, number)}
    para_units: list de {"text","feat","src"} (les items "para" projetés).

    Retourne {"root", "sections", "signature"} ; chaque section :
      {id, level, number, title, confidence, evidence, parent,
       paras: [para_idx...], table_ids: [], figure_ids: [], body_md, n_paras}
    Les items table/figure reçoivent leur clé "sec_id" (section, "root" ou "signature").
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
    last_by_title = {}  # (level, titre_normalisé) -> section (détection des bannières répétées)
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
                _, ev, _, level, number = res
                level = level or 1
                title_norm = re.sub(r"\s+", " ", u["text"].strip()).casefold()
                key = (level, title_norm)
                if key in last_by_title:
                    # Titre répété (bannière de slide, en-tête répété) :
                    # pas de nouvelle section ; la ligne répétée est écartée.
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
