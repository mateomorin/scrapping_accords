# "Heures supplémentaires" pipeline — structure extraction

**Deterministic** (no LLM) extraction of the structure of the agreements in
the **"Heures supplémentaires (contingent, majoration)"** subset, from the
raw documents already scraped on S3. The LLM is used only as a last
resort: OCR of the images that the heuristic triage deems useful (tables /
charts), never on logos or signatures.

Unlike the old chain (`transform_docx_to_md.py`), which sent every page
containing an image to a VLM, this pipeline:

- **detects the real format** of the files (magic bytes — the `.docx`
  extension forced by `scrap_texts.py` is not trusted: some files are PDF,
  RTF, ODF, ...),
- **extracts text and tables directly from the XML** of the docx, of the
  embedded PowerPoint slides (the OLE objects
  `word/embeddings/Microsoft_PowerPoint_SlideN.sldx`, which are the main
  content of many agreements) and of the ODF (`content.xml`): lossless,
  no OCR,
- **recovers the structure** (sections / articles) with multi-signal
  typographic heuristics (the agreements do not use heading styles: bold,
  CAPS, underline, arabic/roman/decimal/letter numbering),
- **triages the images** with heuristics (zone, size, format, perceptual
  hash): only "content" images go to VLM OCR (1 call per image, not per
  page).

**Output**: structured data for the RAG repo (section chunks, table
objects, per-document TOC). **No RAG logic here.**

## Code layout

```
heures_supp/
├── __init__.py
├── HEURES_SUPP.md          # this document (data contract)
├── selftest.py             # local self-test (no S3, no VLM)
├── core/                   # shared library (no CLI entry points)
│   ├── config.py           # S3 paths, LLM settings, triage thresholds
│   ├── utils.py            # safe S3 writes, sniffing, OCR, hashing
│   ├── structure.py        # heading scoring + section tree (level rule)
│   ├── triage.py           # heuristic image triage (no LLM)
│   ├── doc_process.py      # per-document pipeline -> structured record
│   └── extract/            # one deterministic extractor per format
│       ├── word.py         # DOCX (zip/XML, embedded OLE objects)
│       ├── slides.py       # .sldx / .pptx packages
│       ├── odf.py          # ODF (content.xml)
│       └── pdf.py          # PDF (pdfplumber + pypdf)
└── steps/                  # CLI entry points (run directly)
    ├── step0_select.py
    ├── step1_profile.py
    ├── step2_extract.py
    ├── step3_ocr.py
    ├── step4_finalize.py
    └── step5_validate.py
```

The step scripts are run directly (no package install): each one
bootstraps `src/data_conversion` on `sys.path` and imports
`heures_supp.core.*`.

## Structure: level rule

Every paragraph accepted as a heading belongs to a **family**
(`structure.py`) that fixes its **relative depth** (`base`):

| Family | base | Examples |
|---|---|---|
| container | 0 | `TITRE I`, `CHAPITRE 2`, `PARTIE A`, `LIVRE`, `ANNEXE`, `EXPOSÉ DES MOTIFS` |
| preamble / roman | 0 | `PREAMBULE`, `I-`, `II.` |
| article | 1 (or number of dots + 1) | `Article 1`, `Article 5.1` → 2 |
| decimal | number of dots + 1 | `1.` → 1, `11.3.2.1` → 4 |
| letter | 2 | `A.`, `B)` |
| plain (strong typo, no number) | 1 | CAPS + bold title |

**Document offset**: if the document contains at least one accepted
container title, `offset = 1`, otherwise `0`. Final level:

```
level = min(max(base + offset, 1), 5)
```

Reason: a `TITRE` has a **scope** — the articles that follow it belong to
it. With the offset, `TITRE I` stays at L1 (base 0) and `Article 1` moves
to L2 under its TITRE parent. Without any container in the document,
`offset = 0` and the levels are **unchanged** compared to the base rule
(invariance guarantee tested in the selftest).

**Native levels**: if the format encodes an explicit level, it is used as
is (no offset): Word `w:outlineLvl` (directly on the paragraph or carried
by the `w:pStyle` style via `styles.xml`; 0-based in the source, stored
1-based) and ODF `text:outline-level` (directly on `<text:h>` or carried
by the paragraph style of `text:p` — the values observed in the corpus are
1-based, never 0).

## S3 safety

- **Read-only**: `s3://mateomorin/legifrance/metadata/` and
  `s3://mateomorin/legifrance/documents/` (the scraped sources).
- **Write** only under `s3://mateomorin/legifrance/data_heures_supp/`
  (`data/` and `logs/`). Any write outside is refused by
  `utils.safe_remote_path`.

## Prerequisites

```bash
pip install uv
uv sync            # adds lxml, pdfplumber, pypdf, imagehash, striprtf
```

Environment (`.env` or variables):

| Variable | Usage |
|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | S3 (auto-detected) |
| `LLM_API_URL` / `LLM_API_KEY` (or `OPENAI_BASE_URL` / `OPENAI_API_KEY`) | image OCR (step 3) |
| `LLM_OCR_MODEL` (default `gemma4-26b-moe`) | OCR VLM model |

## The 5 steps

### Step 0 — Selection

```bash
uv run src/data_conversion/heures_supp/steps/step0_select.py
```

Reads every parquet in `metadata/`, deduplicates by `cid`, filters on the
boolean column `Heures supplémentaires (contingent, majoration)`, and
writes `logs/selection.parquet` (cid, reference, dateSignature,
`docx_path`, ...).

### Step 1 — Inventory / profiling (0 LLM)

```bash
uv run src/data_conversion/heures_supp/steps/step1_profile.py            # sample 300
uv run src/data_conversion/heures_supp/steps/step1_profile.py --sample 500 --seed 7
```

- **all** documents: real format (magic bytes on 64 KiB — the zip part
  names are stored in clear and allow identifying docx/pptx/ODF even on a
  truncated head) + existence;
- **the sample**: full XML profiling (OLE objects, tables, images, PDF
  pages, text chars...).

Output: `logs/profile_subset.parquet` + printed distribution. This is the
step that quantifies the expected OCR cost before converting anything.

### Step 2 — Structure extraction (0 LLM)

```bash
uv run src/data_conversion/heures_supp/steps/step2_extract.py             # whole subset
uv run src/data_conversion/heures_supp/steps/step2_extract.py --limit 30  # quick test
uv run src/data_conversion/heures_supp/steps/step2_extract.py --force     # reprocess complete shards
```

For each document: download, sniffing, extraction (docx — including
namespace variants and non-standard main parts — / embedded PowerPoint
slides / ODF / pdf / rtf / html), structure, image triage. The kept images
are uploaded to `data/figs/{cid}/`.

Outputs: `data/shard_XXXXX.parquet` (500 docs/shard, checkpoint: a shard is
reprocessed only if incomplete — a `--limit` test does not block later
runs) + `data/figs/`.

### Step 3 — OCR of the kept images

```bash
uv run src/data_conversion/heures_supp/steps/step3_ocr.py                 # whole queue
uv run src/data_conversion/heures_supp/steps/step3_ocr.py --max-images 20 # test
uv run src/data_conversion/heures_supp/steps/step3_ocr.py --ocr-scanned   # + scanned PDFs (Docling VLM, heavy)
```

One VLM call **per image** (not per page) via the OpenAI-compatible
endpoint. Before sending, each image goes through a deterministic
preparation (`utils.prepare_image_for_ocr`): resize beyond 3200 px,
JPEG/PNG re-encoding, flattening of transparency onto a white background —
VLMs cap the image size.

After sending, the Markdown is cleaned (`utils.clean_ocr_md`): the VLM
tends to render logos as Markdown image references with invented URLs; the
description (alt text) is kept as a quote, without the URL.

Resume via `data/ocr_results.jsonl`: only **successful** results count as
done; a re-run therefore retries the failures (internal 3× retry on
transient errors before declaring a failure). EMF/WMF are rasterized via
ImageMagick if available, otherwise marked
`emf_wmf_rasterizer_unavailable` (traced failure, not a crash).

### Step 4 — Finalization (RAG outputs)

```bash
uv run src/data_conversion/heures_supp/steps/step4_finalize.py
```

OCR merge + writing of the consumable outputs:

| File | Content |
|---|---|
| `data/final_shard_XXXXX.parquet` | full record per document (JSON) |
| `data/chunks.jsonl` | 1 chunk per section (+ TOC, preamble, signature per doc) |
| `data/tables.jsonl` | 1 object per table (structured grid + md rendering) |
| `logs/summary.json` | global statistics |

### Step 5 — Validation

```bash
uv run src/data_conversion/heures_supp/steps/step5_validate.py --limit 3
uv run src/data_conversion/heures_supp/steps/step5_validate.py --limit 5 --compare
```

Per-document report for the sampled documents (tree with confidences,
tables, triage). `--compare` computes the token-overlap F1 between our
reconstructed text and the old LLM output (`full_data/data/`, read-only).

## Data schema

Record per document (`doc_json`):

```jsonc
{
  "cid": "ACCOTEXT...", "reference": "...",
  "format": "docx", "doc_type": "word+ppt",
  "n_paras": 1150, "n_sections": 69, "n_tables": 4,
  "n_figures": 3, "n_ocr_kept": 1, "n_ocr_dropped": 2,
  "toc": [{"id","level","number","title","confidence"}],
  "preamble_md": "...", "preamble_table_ids": [], "preamble_figure_ids": [],
  "sections": [
    {"id":"S3","level":2,"number":"11.3","title":"...","confidence":"high",
     "evidence":["decimal-num(L2)","bold-short"],"parent":"S2",
     "body_md":"...","n_paras":12,
     "table_ids":["T1"],"figure_ids":["F2"]}
  ],
  "signature_md": "...",
  "tables": [{"id":"T1","section":"S3","context":"...","n_rows":5,"n_cols":4,
              "empty":false,"grid":[["..."]],
              "spans":[[1,1,1,1],...],"vmerge":[[null,null,null,null],...],
              "md":"| ... |"}],
  "figures": [{"id":"F1","origin":"slide","fmt":"png","size":123,
               "section":"S5","triage":{"keep":true,"reason":"slide_content_image"},
               "fig_s3":"s3://.../data/figs/CID/F1.png",
               "ocr_md":"...","ocr_ok":true}],
  "stats": {"n_ole":42,"n_ppt":42}, "errors": []
}
```

Chunk (`chunks.jsonl`): `chunk_id` (`{cid}:TOC`, `{cid}:PREAMBLE`,
`{cid}:S3`, `{cid}:SIGNATURE`), `cid`, `reference`, `doc_type`,
`chunk_type`, + for sections: `level`, `number`, `title`, `confidence`,
`parent`, `body_md`, `table_ids`, `figure_ids`, `n_chars`.

Table (`tables.jsonl`): `table_id` (`{cid}:T1`), `section_id`,
`section_title`, `context` (previous sentence), `header`, `rows`
(structured grid), `spans` (column/row merges), `md`.

## Parser robustness

The raw documents are heterogeneous; the extractor handles:

- **OOXML namespace variants**: the URNs are resolved per document from
  the root (`extract/word.py._Ns`) — some editors emit `purl.oclc.org`
  instead of `schemas.openxmlformats.org`.
- **Non-standard main part**: discovered via `_rels/.rels` (type
  `officeDocument`) — some files have `word/document2.xml`.
- **Malformed XML**: parsing in `recover` mode (invalid PCDATA chars /
  control characters).
- **ODF (OpenDocument)**: `mimetype` + `content.xml` saved as `.docx`.
  The native headings `<text:h text:outline-level="N">` are used as a
  strong signal; the `table:table` tables and `draw:frame` images are
  extracted.
- **Levels carried by styles**: when the document does not use `text:h` /
  `w:outlineLvl` directly, the level is read from the paragraph style
  (ODF `style:paragraph-properties text:outline-level` in `styles.xml` /
  `automatic-styles`; Word `w:outlineLvl` in the `w:pStyle` of
  `styles.xml`). The ODF values in the corpus are 1-based; the Word ones
  (0-based) are converted to 1-based.
- **Corrupted / empty files**: reported in `error` (doc_type `error`),
  without blocking the shard.

## Known limitations

- **Titles**: detection is heuristic (see `structure.py` and the
  "Structure: level rule" section). Each section carries `confidence` and
  `evidence`; possible false positives are limited by the rules (boilerplate
  preamble formulas, dates, repeated banners...). No text is lost: the
  structure is an index over the ordered content.
- **EMF/WMF** without a rasterizer (ImageMagick): the image is not OCR'd
  (the error is traced). For **binary** OLE objects (legacy `.ppt`, not
  `.sldx`), only the EMF preview is available: it is queued for OCR under
  `origin=ole_preview`.
- **PDF**: born-digital → text + tables via pdfplumber; scanned →
  `doc_type=pdf_scanned`, to be processed with `step3 --ocr-scanned`
  (Docling VLM, same pipeline as the old chain).
- PDF images are placed at the end of the document (no reliable position
  in the text stream).

## Local self-test (no S3)

```bash
uv run src/data_conversion/heures_supp/selftest.py
```

Validates the extractor on `example_data/` (classic Word, Word + 42
embedded PPT slides, corrupted docx, PDF) plus synthetic documents for the
TITRE/ARTICLE level hierarchy.
