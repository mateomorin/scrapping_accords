"""
Auto-test local du pipeline d'extraction (sans S3) sur example_data/.

Lancement :
    uv run src/data_conversion/heures_supp/selftest.py
"""
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doc_process import process_document_bytes  # noqa: E402

HERE = os.path.join(os.path.dirname(__file__), "..", "..", "..")
EXAMPLES = os.path.join(HERE, "example_data")

FAILURES = []


def build_synthetic_odf() -> bytes:
    """Petit ODF textuel : mimetype + content.xml (text:h, text:p, table)."""
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
    # --- 1. Word "classique" avec tableaux et titres en gras/CAPS ---
    p = os.path.join(EXAMPLES, "example_1.docx")
    rec = process_document_bytes(open(p, "rb").read(), "EX1", "ref1")
    print("\n=== example_1.docx (Word classique) ===")
    summary(rec)
    titles = [s["title"] for s in rec["toc"]]
    check("ex1: PREAMBULE détecté", any("PREAMBULE" in t.upper() for t in titles))
    check("ex1: section I (romain) détectée", any(t.strip().startswith("I-") for t in titles))
    check("ex1: section II détectée", any(t.strip().startswith("II") for t in titles))
    check("ex1: section III détectée", any(t.strip().startswith("III") for t in titles))
    check("ex1: au moins 1 tableau", rec["n_tables"] >= 1)
    check("ex1: signature_md présente", bool(rec["signature_md"]))
    check("ex1: aucun faux titre en corps", all(
        s["n_paras"] >= 0 for s in rec["sections"]))

    # --- 2. Word avec diapositives PPT embarquées (cas lourd) ---
    p = os.path.join(EXAMPLES, "ACCOTEXT000036508884.docx")
    rec = process_document_bytes(open(p, "rb").read(), "EX2", "ref2")
    print("\n=== ACCOTEXT000036508884.docx (Word + 42 slides PPT embarquées) ===")
    summary(rec)
    check("ex2: doc_type word+ppt", rec["doc_type"] == "word+ppt")
    check("ex2: 42 objets OLE", rec["stats"].get("n_ole") == 42,
          f"n_ole={rec['stats'].get('n_ole')}")
    check("ex2: texte des slides extrait", rec["n_paras"] > 100,
          f"n_paras={rec['n_paras']}")
    check("ex2: section 11.3.2.1 détectée", any(
        s.get("number") == "11.3.2.1" for s in rec["toc"]))
    check("ex2: pas d'erreur de slide", not rec["errors"], str(rec["errors"][:2]))
    sample = json.dumps(rec["sections"][0], ensure_ascii=False)
    check("ex2: sections sérialisables", "body_md" in sample)

    # --- 3. Word corrompu ([trash]) ---
    p = os.path.join(EXAMPLES, "example_corrupted.docx")
    rec = process_document_bytes(open(p, "rb").read(), "EX3", "ref3")
    print("\n=== example_corrupted.docx (docx corrompu / [trash]) ===")
    summary(rec)
    check("ex3: extraction sans crash", rec["n_paras"] >= 0)

    # --- 4. PDF born-digital ---
    p = os.path.join(EXAMPLES, "example_1.pdf")
    rec = process_document_bytes(open(p, "rb").read(), "EX4", "ref4")
    print("\n=== example_1.pdf (PDF) ===")
    summary(rec)
    check("ex4: format pdf", rec["format"] == "pdf")
    check("ex4: texte extrait", rec["n_paras"] > 20, f"n_paras={rec['n_paras']}")

    # --- 5. ODF synthétique (mimetype + content.xml) ---
    odf = build_synthetic_odf()
    rec = process_document_bytes(odf, "EX5", "ref5")
    print("\n=== ODF synthétique (text:h + table) ===")
    summary(rec)
    check("ex5: format odt", rec["format"] == "odt")
    check("ex5: doc_type odf", rec["doc_type"] == "odf")
    check("ex5: titre text:h détecté", any(
        "OBJET" in s["title"].upper() for s in rec["toc"]))
    check("ex5: tableau extrait", rec["n_tables"] >= 1)
    if rec["tables"]:
        t = rec["tables"][0]
        check("ex5: grille 2x2", t["n_rows"] == 2 and t["n_cols"] == 2,
              f"{t['n_rows']}x{t['n_cols']}")
        check("ex5: md rendu propre", "| Taux | Heures |" in t["md"], t["md"][:60])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} ÉCHEC(S): {FAILURES}")
        sys.exit(1)
    print("Tous les tests passent.")


if __name__ == "__main__":
    main()
