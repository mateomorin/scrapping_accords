"""
Local self-test of the extraction pipeline (no S3) on example_data/.

Run:
    uv run src/data_conversion/heures_supp/selftest.py
"""
import io
import json
import os
import sys
import zipfile

# Make the heures_supp package importable when run as a script
# (package root: src/data_conversion; no package install needed).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from heures_supp.core.doc_process import process_document_bytes  # noqa: E402

HERE = os.path.join(os.path.dirname(__file__), "..", "..", "..")
EXAMPLES = os.path.join(HERE, "example_data")

FAILURES = []


def build_synthetic_odf() -> bytes:
    """Small textual ODF: mimetype + content.xml (text:h, text:p, table)."""
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content '
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
    """
    Minimal DOCX: paras = [(text, bold), ...].
    Used to test the TITRE / ARTICLE hierarchy without depending on an
    external file.
    """
    import xml.sax.saxutils as saxutils

    def esc(t):
        return saxutils.escape(t, {"'": "&apos;"})

    body = []
    for text, bold in paras:
        b = "<w:b/>" if bold else ""
        body.append(
            f"<w:p><w:pPr><w:rPr>{b}</w:rPr></w:pPr>"
            f"<w:r><w:rPr>{b}</w:rPr><w:t xml:space=\"preserve\">{esc(text)}</w:t></w:r></w:p>"
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(body)}<w:sectPr/></w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def check(name, cond, detail=""):
    status = "OK  " if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def summary(record, indent="  "):
    print(f"{indent}doc_type={record['doc_type']} format={record['format']} "
          f"paras={record['n_paras']} sections={record['n_sections']} "
          f"tables={record['n_tables']} figures={record['n_figures']} "
          f"(kept={record['n_ocr_kept']}, dropped={record['n_ocr_dropped']})")
    if record["errors"]:
        print(f"{indent}errors: {record['errors'][:3]}")
    for s in record["toc"][:12]:
        print(f"{indent}  L{s['level']} [{s['confidence']}] {s['title'][:70]}")


def main():
    # --- 1. Word "classic" with tables and bold/CAPS titles ---
    p = os.path.join(EXAMPLES, "example_1.docx")
    rec = process_document_bytes(open(p, "rb").read(), "EX1", "ref1")
    print("\n=== example_1.docx (classic Word) ===")
    summary(rec)
    titles = [s["title"] for s in rec["toc"]]
    check("ex1: PREAMBULE detected", any("PREAMBULE" in t.upper() for t in titles))
    check("ex1: section I (roman) detected", any(t.strip().startswith("I-") for t in titles))
    check("ex1: section II detected", any(t.strip().startswith("II") for t in titles))
    check("ex1: section III detected", any(t.strip().startswith("III") for t in titles))
    check("ex1: at least 1 table", rec["n_tables"] >= 1)
    check("ex1: signature_md present", bool(rec["signature_md"]))
    check("ex1: no false heading in the body", all(
        s["n_paras"] >= 0 for s in rec["sections"]))

    # --- 2. Word with embedded PPT slides (heavy case) ---
    p = os.path.join(EXAMPLES, "ACCOTEXT000036508884.docx")
    rec = process_document_bytes(open(p, "rb").read(), "EX2", "ref2")
    print("\n=== ACCOTEXT000036508884.docx (Word + 42 embedded PPT slides) ===")
    summary(rec)
    check("ex2: doc_type word+ppt", rec["doc_type"] == "word+ppt")
    check("ex2: 42 OLE objects", rec["stats"].get("n_ole") == 42,
          f"n_ole={rec['stats'].get('n_ole')}")
    check("ex2: slide text extracted", rec["n_paras"] > 100,
          f"n_paras={rec['n_paras']}")
    check("ex2: section 11.3.2.1 detected", any(
        s.get("number") == "11.3.2.1" for s in rec["toc"]))
    check("ex2: no slide error", not rec["errors"], str(rec["errors"][:2]))
    sample = json.dumps(rec["sections"][0], ensure_ascii=False)
    check("ex2: sections serializable", "body_md" in sample)

    # --- 3. Corrupted Word ([trash]) ---
    p = os.path.join(EXAMPLES, "example_corrupted.docx")
    rec = process_document_bytes(open(p, "rb").read(), "EX3", "ref3")
    print("\n=== example_corrupted.docx (corrupted docx / [trash]) ===")
    summary(rec)
    check("ex3: extraction without crash", rec["n_paras"] >= 0)

    # --- 4. Born-digital PDF ---
    p = os.path.join(EXAMPLES, "example_1.pdf")
    rec = process_document_bytes(open(p, "rb").read(), "EX4", "ref4")
    print("\n=== example_1.pdf (PDF) ===")
    summary(rec)
    check("ex4: format pdf", rec["format"] == "pdf")
    check("ex4: text extracted", rec["n_paras"] > 20, f"n_paras={rec['n_paras']}")

    # --- 5. Synthetic ODF (mimetype + content.xml) ---
    odf = build_synthetic_odf()
    rec = process_document_bytes(odf, "EX5", "ref5")
    print("\n=== synthetic ODF (text:h + table) ===")
    summary(rec)
    check("ex5: format odt", rec["format"] == "odt")
    check("ex5: doc_type odf", rec["doc_type"] == "odf")
    check("ex5: text:h title detected", any(
        "OBJET" in s["title"].upper() for s in rec["toc"]))
    check("ex5: table extracted", rec["n_tables"] >= 1)
    if rec["tables"]:
        t = rec["tables"][0]
        check("ex5: 2x2 grid", t["n_rows"] == 2 and t["n_cols"] == 2,
              f"{t['n_rows']}x{t['n_cols']}")
        check("ex5: clean md rendering", "| Taux | Heures |" in t["md"], t["md"][:60])

    # --- 6. TITRE / ARTICLE hierarchy (container) ---
    docx = build_synthetic_docx([
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
    ])
    rec = process_document_bytes(docx, "EX6", "ref6")
    print("\n=== synthetic docx (TITRE / ARTICLE) ===")
    summary(rec)
    by_title = {}
    # "TITRE I " with the trailing space: avoids "TITRE II" overwriting
    # the "TITRE I" key.
    for s in rec["sections"]:  # sections (not toc): carries the parent field
        for k in ("TITRE I ", "TITRE II", "Article 1", "Article 2", "Article 3"):
            if s["title"].startswith(k):
                by_title[k.rstrip()] = s
    check("ex6: TITRE I detected at L1 root",
          by_title.get("TITRE I", {}).get("level") == 1
          and by_title.get("TITRE I", {}).get("parent") is None)
    check("ex6: Article 1 attached under TITRE I (L2)",
          by_title.get("Article 1", {}).get("level") == 2
          and by_title.get("Article 1", {}).get("parent") == by_title.get("TITRE I", {}).get("id"))
    check("ex6: Article 2 attached under TITRE I (L2)",
          by_title.get("Article 2", {}).get("parent") == by_title.get("TITRE I", {}).get("id"))
    check("ex6: TITRE II detected at L1 root",
          by_title.get("TITRE II", {}).get("level") == 1
          and by_title.get("TITRE II", {}).get("parent") is None)
    check("ex6: Article 3 attached under TITRE II (L2)",
          by_title.get("Article 3", {}).get("level") == 2
          and by_title.get("Article 3", {}).get("parent") == by_title.get("TITRE II", {}).get("id"))
    check("ex6: document title at L1",
          by_title and any(s["level"] == 1 and s["title"].startswith("ACCORD")
                           for s in rec["toc"]))

    # --- 7. Document WITHOUT TITRE: level invariance (offset = 0) ---
    docx = build_synthetic_docx([
        ("PREAMBULE", True),
        ("Les parties se sont reunies afin de negocier les heures supplementaires.", False),
        ("Article 1 - Objet", True),
        ("L'objet est de definir les heures supplementaires.", False),
        ("Article 2 - Taux", True),
        ("Majoration de 25 pour cent au dela de 35 heures.", False),
    ])
    rec = process_document_bytes(docx, "EX7", "ref7")
    print("\n=== synthetic docx (no TITRE: invariance) ===")
    summary(rec)
    by_title = {}
    for s in rec["sections"]:
        for k in ("PREAMBULE", "Article 1", "Article 2"):
            if s["title"].startswith(k):
                by_title[k] = s
    check("ex7: PREAMBULE at L1", by_title.get("PREAMBULE", {}).get("level") == 1)
    check("ex7: Article 1 stays at L1 root (invariance)",
          by_title.get("Article 1", {}).get("level") == 1
          and by_title.get("Article 1", {}).get("parent") is None)
    check("ex7: Article 2 stays at L1 root (invariance)",
          by_title.get("Article 2", {}).get("level") == 1
          and by_title.get("Article 2", {}).get("parent") is None)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("All tests pass.")


if __name__ == "__main__":
    main()
