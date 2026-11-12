# Pipeline "Heures supplémentaires" — extraction de structure

Extraction **déterministe** (sans LLM) de la structure des accords du
sous-ensemble **"Heures supplémentaires (contingent, majoration)"**, à partir
des documents bruts déjà scrapés sur S3. Le LLM n'est utilisé que comme
dernier recours : l'OCR des seules images que le triage heuristique juge
utiles (tableaux / schémas), jamais sur les logos ou signatures.

Contrairement à l'ancienne chaîne (`transform_docx_to_md.py`), qui envoyait
chaque page contenant une image dans un VLM, ce pipeline :

- **détecte le format réel** des fichiers (magic bytes — l'extension `.docx`
  posée par `scrap_texts.py` n'est pas trustée : certains fichiers sont des
  PDF, RTF, ODF, ...),
- **extrait le texte et les tableaux directement du XML** des docx, des
  diapositives PowerPoint embarquées (les objets OLE
  `word/embeddings/Microsoft_PowerPoint_SlideN.sldx`, qui sont le contenu
  principal de beaucoup d'accords) et des ODF (`content.xml`) : sans perte,
  sans OCR,
- **récupère la structure** (sections / articles) par heuristiques
  typographiques multi-signaux (les accords n'utilisent pas de styles de
  titre : gras, CAPITALES, souligné, numérotation arabe/romaine/décimale/
  lettres),
- **triage les images** par heuristiques (zone, taille, format, hash
  perceptuel) : seules les images "contenu" passent en OCR VLM (1 appel par
  image, pas par page).

**Sortie** : données structurées destinées au repo RAG (chunks de sections,
objets tableaux, TOC par document). **Aucune logique de RAG ici.**

## Sécurité S3

- **Lecture seule** : `s3://mateomorin/legifrance/metadata/` et
  `s3://mateomorin/legifrance/documents/` (sources scrapées).
- **Écriture** uniquement sous `s3://mateomorin/legifrance/data_heures_supp/`
  (`data/` et `logs/`). Tout écriture en dehors est refusée par
  `utils.safe_remote_path`.

## Prérequis

```bash
pip install uv
uv sync            # ajoute lxml, pdfplumber, pypdf, imagehash, striprtf
```

Environnement (`.env` ou variables) :

| Variable | Usage |
|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | S3 (auto-détectées) |
| `LLM_API_URL` / `LLM_API_KEY` (ou `OPENAI_BASE_URL` / `OPENAI_API_KEY`) | OCR des images (step 3) |
| `LLM_OCR_MODEL` (défaut `gemma4-26b-moe`) | modèle VLM d'OCR |

## Les 5 steps

### Step 0 — Sélection

```bash
uv run src/data_conversion/heures_supp/step0_select.py
```

Lit tous les parquets de `metadata/`, déduplique par `cid`, filtre sur la
colonne booléenne `Heures supplémentaires (contingent, majoration)`, et écrit
`logs/selection.parquet` (cid, reference, dateSignature, `docx_path`, ...).

### Step 1 — Inventaire / profilage (0 LLM)

```bash
uv run src/data_conversion/heures_supp/step1_profile.py            # échantillon 300
uv run src/data_conversion/heures_supp/step1_profile.py --sample 500 --seed 7
```

- **tous** les documents : format réel (magic bytes sur 64 Ko — les noms de
  parties du zip sont en clair et permettent d'identifier docx/pptx/ODF même
  sur un extrait tronqué) + existence ;
- **l'échantillon** : profilage XML complet (objets OLE, tableaux, images,
  pages PDF, chars texte...).

Sortie : `logs/profile_subset.parquet` + distribution affichée. C'est l'étape
qui quantifie le coût OCR attendu avant de convertir quoi que ce soit.

### Step 2 — Extraction de structure (0 LLM)

```bash
uv run src/data_conversion/heures_supp/step2_extract.py             # tout le sous-ensemble
uv run src/data_conversion/heures_supp/step2_extract.py --limit 30  # essai rapide
```

Pour chaque document : téléchargement, sniffing, extraction (docx — y
compris les variantes d'espaces de noms et parties principales non
standard — / diapositives PowerPoint embarquées / ODF / pdf / rtf / html),
structure, triage des images. Les images conservées sont uploadées dans
`data/figs/{cid}/`.

Sorties : `data/shard_XXXXX.parquet` (500 docs/shard, checkpoint : un shard
n'est repris que s'il est incomplet — un essai `--limit` ne bloque pas la
suite) + `data/figs/`.

### Step 3 — OCR des images conservées

```bash
uv run src/data_conversion/heures_supp/step3_ocr.py                 # toute la file
uv run src/data_conversion/heures_supp/step3_ocr.py --max-images 20 # essai
uv run src/data_conversion/heures_supp/step3_ocr.py --ocr-scanned   # + PDF scannés (Docling VLM, lourd)
```

Un appel VLM **par image** (pas par page) via l'endpoint OpenAI-compatible.
Avant l'envoi, chaque image passe une préparation déterministe
(`utils.prepare_image_for_ocr`) : redimensionnement au-delà de 3200 px,
ré-encodage JPEG/PNG, aplatissement de la transparence sur fond blanc —
les VLM plafonnent la taille des images.

Après l'envoi, le Markdown est nettoyé (`utils.clean_ocr_md`) : le VLM a
tendance à rendre les logos en référence d'image Markdown avec des URL
inventées ; on conserve la description (alt text) en citation, sans URL.

Reprise par `data/ocr_results.jsonl` : seuls les résultats **réussis**
comptent comme faits ; une relance retente donc les échecs (retry interne
3× sur erreur transitoire avant de déclarer l'échec). Les EMF/WMF sont
rasterisés via ImageMagick si disponible, sinon marqués
`rasterisation_emf_wmf_indisponible` (échec tracé, pas un crash).

### Step 4 — Finalisation (sorties pour le RAG)

```bash
uv run src/data_conversion/heures_supp/step4_finalize.py
```

Fusion OCR + écriture des sorties consommables :

| Fichier | Contenu |
|---|---|
| `data/final_shard_XXXXX.parquet` | record complet par document (JSON) |
| `data/chunks.jsonl` | 1 chunk par section (+ TOC, préambule, signature par doc) |
| `data/tables.jsonl` | 1 objet par tableau (grille structurée + rendu md) |
| `logs/summary.json` | statistiques globales |

### Step 5 — Validation

```bash
uv run src/data_conversion/heures_supp/step5_validate.py --limit 3
uv run src/data_conversion/heures_supp/step5_validate.py --limit 5 --compare
```

Rapport par document échantillonné (arborescence avec confiances, tableaux,
triage). `--compare` calcule le F1 de recouvrement token entre notre texte
reconstruit et l'ancienne sortie LLM (`full_data/data/`, lecture seule).

## Schéma des données

Record par document (`doc_json`) :

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

Chunk (`chunks.jsonl`) : `chunk_id` (`{cid}:TOC`, `{cid}:PREAMBLE`,
`{cid}:S3`, `{cid}:SIGNATURE`), `cid`, `reference`, `doc_type`, `chunk_type`,
+ pour les sections : `level`, `number`, `title`, `confidence`, `parent`,
`body_md`, `table_ids`, `figure_ids`, `n_chars`.

Tableau (`tables.jsonl`) : `table_id` (`{cid}:T1`), `section_id`,
`section_title`, `context` (phrase précédente), `header`, `rows` (grille
structurée), `spans` (merges colonnes/lignes), `md`.

## Robustesse du parseur

Les documents bruts sont hétérogènes ; l'extractor gère :

- **Variantes d'espaces de noms OOXML** : les URN sont résolues par
  document à partir de la racine (`extract_word._Ns`) — certains éditeurs
  émettent `purl.oclc.org` au lieu de `schemas.openxmlformats.org`.
- **Partie principale non standard** : découverte via `_rels/.rels`
  (type `officeDocument`) — certains fichiers ont `word/document2.xml`.
- **XML malformé** : parsing en mode `recover` (caractères PCDATA invalides /
  char. de contrôle).
- **ODF (OpenDocument)** : `mimetype` + `content.xml` enregistrés en `.docx`.
  Les titres natifs `<text:h text:outline-level="N">` sont utilisés comme
  signal fort ; les tableaux `table:table` et images `draw:frame` sont extraits.
- **Fichiers corrompus / vides** : signalés dans `error` (doc_type `error`),
  sans bloquer le shard.

## Limites connues

- **Titres** : la détection est heuristique (cf. `structure.py`). Chaque
  section porte `confidence` et `evidence` ; les faux positifs possibles sont
  limités par les règles (formules de chapeau, dates, bannières répétées...).
  Aucun texte n'est perdu : la structure est un index au-dessus du contenu
  ordonné.
- **EMF/WMF** sans rasteriseur (ImageMagick) : l'image n'est pas OCR'de
  (l'erreur est tracée). Pour les objets OLE **binaires** (`.ppt` legacy,
  non `.sldx`), seule la prévisualisation EMF est disponible : elle est
  mise en file d'OCR sous `origin=ole_preview`.
- **PDF** : born-digital → texte + tables via pdfplumber ; scannés →
  `doc_type=pdf_scanned`, à traiter avec `step3 --ocr-scanned` (Docling VLM,
  même pipeline que l'ancienne chaîne).
- Les images de PDF sont rangées en fin de document (pas de position fiable
  dans le flux texte).

## Auto-test local (sans S3)

```bash
uv run src/data_conversion/heures_supp/selftest.py
```

Valide l'extractor sur `example_data/` (Word classique, Word + 42 slides PPT
embarquées, docx corrompu, PDF).
