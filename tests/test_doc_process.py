"""
Deterministic document processing tests (no S3, no LLM, no network).

Runs the extractor on the example documents in example_data/ plus synthetic
in-memory docx/odt files. Ported from heures_supp/selftest.py.
"""

import io
import xml.sax.saxutils as saxutils
import zipfile
from pathlib import Path

from heures_supp.core.doc_process import process_document_bytes
from heures_supp.core.structure import RE_TOC_GLUED

EXAMPLES = Path(__file__).resolve().parents[1] / "example_data"


def build_synthetic_odf() -> bytes:
    """Small textual ODF: mimetype + content.xml (text:h, text:p, table)."""
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<office:document-content "
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        "<office:automatic-styles>"
        '<style:style style:name="B" style:family="paragraph">'
        '<style:text-properties fo:font-weight="bold"/>'
        "</style:style>"
        "</office:automatic-styles>"
        "<office:body><office:text>"
        '<text:h text:outline-level="1">OBJET DE L\'ACCORD</text:h>'
        "<text:p>Le présent accord définit les heures supplémentaires.</text:p>"
        "<text:list>"
        "<text:list-item><text:p>Taux de majoration :</text:p></text:list-item>"
        "</text:list>"
        '<table:table table:name="T1">'
        '<table:table-row><table:table-cell office:value-type="string">'
        "<text:p>Taux</text:p></table:table-cell>"
        '<table:table-cell office:value-type="string">'
        "<text:p>Heures</text:p></table:table-cell></table:table-row>"
        '<table:table-row><table:table-cell office:value-type="string">'
        "<text:p>25%</text:p></table:table-cell>"
        '<table:table-cell office:value-type="string">'
        "<text:p>44h</text:p></table:table-cell></table:table-row>"
        "</table:table>"
        "</office:text></office:body>"
        "</office:document-content>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", content)
    return buf.getvalue()


def build_synthetic_docx(paras: list) -> bytes:
    """Minimal DOCX: paras = [(text, bold), ...], without any external file."""

    def esc(t):
        return saxutils.escape(t, {"'": "&apos;"})

    body = []
    for text, bold in paras:
        b = "<w:b/>" if bold else ""
        body.append(
            f"<w:p><w:pPr><w:rPr>{b}</w:rPr></w:pPr>"
            f'<w:r><w:rPr>{b}</w:rPr><w:t xml:space="preserve">{esc(text)}</w:t></w:r></w:p>'
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'    # noqa: E501
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'   # noqa: E501
        'wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'    # noqa: E501
        'officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def test_ex1_classic_word():
    with open(EXAMPLES / "example_1.docx", "rb") as f:
        rec = process_document_bytes(f.read(), "EX1", "ref1")
    titles = [s["title"] for s in rec["toc"]]
    assert any("PREAMBULE" in t.upper() for t in titles), "PREAMBULE not detected"
    assert any(t.strip().startswith("I-") for t in titles), "section I (roman) not detected"
    assert any(t.strip().startswith("II") for t in titles), "section II not detected"
    assert any(t.strip().startswith("III") for t in titles), "section III not detected"
    assert rec["n_tables"] >= 1, "no table found"
    assert rec["signature_md"], "signature_md missing"


def test_ex2_word_with_embedded_ppt():
    with open(EXAMPLES / "ACCOTEXT000036508884.docx", "rb") as f:
        rec = process_document_bytes(f.read(), "EX2", "ref2")
    assert rec["doc_type"] == "word+ppt"
    assert rec["stats"].get("n_ole") == 42, f"n_ole={rec['stats'].get('n_ole')}"
    assert rec["n_paras"] > 100, f"n_paras={rec['n_paras']}"
    assert any(s.get("number") == "11.3.2.1" for s in rec["toc"]), "section 11.3.2.1 not detected"
    assert not rec["errors"], str(rec["errors"][:2])
    assert "body_md" in rec["sections"][0], "sections not JSON-serializable shape"


def test_ex3_corrupted_docx():
    # Extraction must not crash on a [trash]-corrupted docx.
    with open(EXAMPLES / "example_corrupted.docx", "rb") as f:
        rec = process_document_bytes(f.read(), "EX3", "ref3")
    assert isinstance(rec["n_paras"], int)


def test_ex4_pdf():
    with open(EXAMPLES / "example_1.pdf", "rb") as f:
        rec = process_document_bytes(f.read(), "EX4", "ref4")
    assert rec["format"] == "pdf"
    assert rec["n_paras"] > 20, f"n_paras={rec['n_paras']}"


def test_ex5_synthetic_odf():
    rec = process_document_bytes(build_synthetic_odf(), "EX5", "ref5")
    assert rec["format"] == "odt"
    assert rec["doc_type"] == "odf"
    assert any("OBJET" in s["title"].upper() for s in rec["toc"]), "text:h title not detected"
    assert rec["n_tables"] >= 1, "table not extracted"
    t = rec["tables"][0]
    assert (t["n_rows"], t["n_cols"]) == (2, 2), f"{t['n_rows']}x{t['n_cols']}"
    assert "| Taux | Heures |" in t["md"], t["md"][:60]


def test_ex6_titre_article_hierarchy():
    docx = build_synthetic_docx(
        [
            ("ACCORD SUR LES HEURES SUPPLEMENTAIRES", True),
            ("TITRE I - DISPOSITIONS GENERALES", True),
            ("Article 1 - Objet", True),
            ("Le present article determine l'objet de l'accord.", False),
            ("Article 2 - Duree", True),
            ("L'accord est conclu pour une duree de trois ans.", False),
            ("TITRE II - MODALITES D APPLICATION", True),
            ("Article 3 - Taux de majoration", True),
            ("Les heures supplementaires sont majorees de 25 pour cent.", False),
            ("Fait a Paris, le 1er janvier 2024", False),
        ]
    )
    rec = process_document_bytes(docx, "EX6", "ref6")
    by_title = {}
    # "TITRE I " keeps the trailing space: avoids "TITRE II" overwriting the
    # "TITRE I" key.
    for s in rec["sections"]:  # sections (not toc): carries the parent field
        for k in ("TITRE I ", "TITRE II", "Article 1", "Article 2", "Article 3"):
            if s["title"].startswith(k):
                by_title[k.rstrip()] = s
    assert (
        by_title.get("TITRE I", {}).get("level") == 1
        and by_title.get("TITRE I", {}).get("parent") is None
    ), "TITRE I not at L1 root"
    assert by_title.get("Article 1", {}).get("level") == 2 and by_title.get("Article 1", {}).get(
        "parent"
    ) == by_title.get("TITRE I", {}).get("id"), "Article 1 not attached under TITRE I (L2)"
    assert by_title.get("Article 2", {}).get("parent") == by_title.get("TITRE I", {}).get("id"), (
        "Article 2 not attached under TITRE I"
    )
    assert (
        by_title.get("TITRE II", {}).get("level") == 1
        and by_title.get("TITRE II", {}).get("parent") is None
    ), "TITRE II not at L1 root"
    assert by_title.get("Article 3", {}).get("level") == 2 and by_title.get("Article 3", {}).get(
        "parent"
    ) == by_title.get("TITRE II", {}).get("id"), "Article 3 not attached under TITRE II (L2)"
    assert any(s["level"] == 1 and s["title"].startswith("ACCORD") for s in rec["toc"]), (
        "document title not at L1"
    )


def test_ex7_no_titre_level_invariance():
    docx = build_synthetic_docx(
        [
            ("PREAMBULE", True),
            ("Les parties se sont reunies afin de negocier les heures supplementaires.", False),
            ("Article 1 - Objet", True),
            ("L'objet est de definir les heures supplementaires.", False),
            ("Article 2 - Taux", True),
            ("Majoration de 25 pour cent au dela de 35 heures.", False),
        ]
    )
    rec = process_document_bytes(docx, "EX7", "ref7")
    by_title = {}
    for s in rec["sections"]:
        for k in ("PREAMBULE", "Article 1", "Article 2"):
            if s["title"].startswith(k):
                by_title[k] = s
    assert by_title.get("PREAMBULE", {}).get("level") == 1, "PREAMBULE not at L1"
    assert (
        by_title.get("Article 1", {}).get("level") == 1
        and by_title.get("Article 1", {}).get("parent") is None
    ), "Article 1 not at L1 root (invariance)"
    assert (
        by_title.get("Article 2", {}).get("level") == 1
        and by_title.get("Article 2", {}).get("parent") is None
    ), "Article 2 not at L1 root (invariance)"


def test_ex8_long_numbered_headings():
    # Regression for the "long numbered heading" scoring bug: a numbered
    # heading that doubles as a long enumeration (e.g. a 4.2 / 4.3 banner
    # listing every job category) used to be demoted by the "long" penalty
    # and swallowed into the previous section's body. Now: the decimal bonus
    # applies to fully-bold long lines and the "long" penalty is skipped
    # whenever an explicit numbering signal is present. The 4.4 line is the
    # control: long AND numbered but NOT bold -> must stay body text.
    docx = build_synthetic_docx(
        [
            ("Article 4 - DUREE ET AMENAGEMENT DU TEMPS DE TRAVAIL", True),
            ("4.1. Personnel d'exploitation : ouvriers, employs et techniciens", True),
            ("4.1.1. Principes generaux", True),
            (
                "4.2. Agents de maitrise\u00a0d'exploitation, agents de maitrise "
                "d'etudes et developpement, agents administratifs ou d'etudes "
                "lies a l'exploitation",
                True,
            ),
            ("4.2.1. Principes generaux", True),
            (
                "4.3. Personnels administratifs, comptables, etudes et autres "
                "emplois non directement lies a l'exploitation : employs, "
                "techniciens, agents de maitrise",
                True,
            ),
            ("4.3.1. Principes generaux", True),
            ("4.3.2. Samedis et jours feries", True),
            (
                "4.4. Les agents de maitrise d'exploitation ainsi que les employs "
                "et techniciens lies a l'exploitation sont concernes par la "
                "presente clause",
                False,
            ),
        ]
    )
    rec = process_document_bytes(docx, "EX8", "ref8")
    by_number = {s["number"]: s for s in rec["sections"] if s.get("number")}
    art4 = by_number.get("Article 4", {})
    h42 = by_number.get("4.2", {})
    h421 = by_number.get("4.2.1", {})
    h43 = by_number.get("4.3", {})
    h432 = by_number.get("4.3.2", {})
    assert art4.get("level") == 1 and art4.get("parent") is None, "Article 4 not at L1 root"
    assert h42.get("level") == 2 and h42.get("parent") == art4.get("id"), (
        "long bold numbered 4.2 not a heading at L2 under Article 4"
    )
    assert h421.get("parent") == h42.get("id"), "4.2.1 not attached under 4.2"
    assert h43.get("level") == 2 and h43.get("parent") == art4.get("id"), (
        "long bold numbered 4.3 not a heading at L2 under Article 4"
    )
    assert h432.get("parent") == h43.get("id"), "4.3.2 not attached under 4.3"
    assert "4.4" not in by_number and not any(
        s["title"].startswith("4.4.") for s in rec["sections"]
    ), "long NON-bold numbered 4.4 must not be a heading"
    assert "presente clause" in (h432.get("body_md") or ""), (
        "4.4 control line lost from the body of 4.3.2"
    )
    assert all("\u00a0" not in s["title"] for s in rec["sections"]) and all(
        "\u00a0" not in t["title"] for t in rec["toc"]
    ), "NBSP found in a title"


def test_ex9_sommaire_toc():
    # Sommaire / table of contents: entries are references, not real
    # headings. TOC entries carry a page number GLUED to the last word
    # ("1.3.1. Substitution.11", "1.4 - Adhésion11"); the glued form is a
    # HARD veto. The real content headings below share the same titles but
    # WITHOUT the glued page number and must be detected.
    docx = build_synthetic_docx(
        [
            ("SOMMAIRE", True),
            ("PREAMBULE2", False),
            ("TITRE 1 : DISPOSITIONS GENERALES10", False),
            ("1.3.1. Substitution.11", False),  # short glued TOC entry -> body
            ("1.4 - Adhésion11", False),  # short glued TOC entry -> body
            ("ARTICLE 1 – OBJET, CHAMP D'APPLICATION5", True),  # bold CAPS+ARTICLE glued -> body
            ("2.1 - Définitions13", False),  # short glued TOC entry -> body
            # ---- real content (same titles, clean, bold) ----
            ("TITRE 1 : DISPOSITIONS GENERALES", True),
            ("1.3.1. Substitution.", True),
            ("La substitution est prevue par le present accord.", False),
            ("1.4 - Adhésion", True),
            ("L'adhesion des nouveaux employeurs est libre.", False),
            ("ARTICLE 1 – OBJET, CHAMP D'APPLICATION", True),
            ("Le present article definit l'objet de l'accord.", False),
            ("2.1 - Définitions", True),
            (
                "Sont consideres comme heures supplementaires les heures effectuees "
                "au dela du contingent.",
                False,
            ),
        ]
    )
    rec = process_document_bytes(docx, "EX9", "ref9")
    by_number = {s["number"]: s for s in rec["sections"] if s.get("number")}
    assert any("SOMMAIRE" in s["title"].upper() for s in rec["sections"]), "SOMMAIRE not a section"
    sommaire = next((s for s in rec["sections"] if "SOMMAIRE" in s["title"].upper()), {})
    assert sommaire.get("is_toc") is True, "SOMMAIRE section not flagged is_toc=True"
    assert all(
        not s.get("is_toc") for s in rec["sections"] if "SOMMAIRE" not in s["title"].upper()
    ), "real sections flagged is_toc"
    assert any(t.get("is_toc") is True for t in rec["toc"]) and all(
        t.get("is_toc") is not None for t in rec["toc"]
    ), "is_toc not consistently exposed on the flat toc list"
    # Core invariant: no section title carries a glued page number.
    assert not any(RE_TOC_GLUED.search(s["title"]) for s in rec["sections"]), (
        "a section title has a glued page number (TOC entry became a heading)"
    )
    # The real 1.4 heading wins the "1.4" slot and is clean (the fake
    # "1.4 - Adhésion11" did not become a section).
    assert by_number.get("1.4", {}).get("title") == "1.4 - Adhésion", "real 1.4 heading not clean"
    assert by_number.get("1.3.1", {}).get("title", "").endswith("Substitution."), (
        "real 1.3.1 heading missing or not clean"
    )
    assert by_number.get("2.1", {}).get("n_paras", 0) >= 1, "real 2.1 heading missing its body"
    # The strong-typo (bold + CAPS + ARTICLE) TOC entry must be sunk too; the
    # real clean "ARTICLE 1" heading is the one that survives.
    assert not any(s["title"].endswith("APPLICATION5") for s in rec["sections"]), (
        "bold CAPS+ARTICLE glued TOC entry became a heading"
    )
    assert (
        by_number.get("ARTICLE 1", {}).get("n_paras", 0) >= 1
        and by_number.get("ARTICLE 1", {}).get("title") == "ARTICLE 1 – OBJET, CHAMP D'APPLICATION"
    ), "real ARTICLE 1 heading missing or not clean"
    assert any(
        s["title"].upper().startswith("TITRE 1") and "10" not in s["title"] for s in rec["sections"]
    ), "real TITRE 1 container missing"
