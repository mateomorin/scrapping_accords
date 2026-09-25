# Scraping Accords

This repository is made of scripts and tools to scrap the French "Accords d'entreprises" (Company-level agreements), which are documents written and signed by unions and company executives to change employees rights on top of the French "Code du Travail" (Labour Code).

Nearly half of the data is publicly available on [LégiFrance](https://www.legifrance.gouv.fr/), stored with metadata in `.docx` documents.

This repository aims to scrap data from LégiFrance and treat it for different purposes (from simple general statistics to advanced NLP on text). It is composed of 2 main parts (for now):

1. **Legifrance scraping** (`src/legifrance`): using LegiFrance API to get all the data
2. **Data conversion** (`src/data_conversion`): converting Word documents (scraped from LégiFrance) to Markdown thanks to different tools (and LLMs for OCR).

Each subproject has its own documentation, and is meant to be independant in the running process. Nonetheless, it is paramount to keep in mind that the order of the list makes sense. For instance, if you first created your dataset using LegiFrance scraping, it is easier to use data conversion afterwards because data structure is already good.