# How to use data conversion?

To be able to run the data conversion script `transform_docx_to_md.py`, you need several requirements:

- proper data storage
- LibreOffice installed on your device
- time

## Data storage

The first main requirement is data storage. This project was built using Insee's [SSP Cloud](https://datalab.sspcloud.fr/), which storage relies on MinIO (S3-like storage system). Thus, if you do not use S3, you need to change everything around `s3fs.S3FileSystem()`. Good luck (still possible though).

Then, you need to make sure of your storage organization:

- All your metadata must be stored in a parquet file located in folder `METADATA_PATH` (from `config.py`). Then you will just need to give the name of the file as input. The metadata must have at least these fields:
- 1. Column `cid`, unique reference made to the document taken from Legifrance,
- 2. Column `dateSignature`, chains of caracters in datetime format such that conversion is easy.

- All your documents to convert must be located in folder `DOCUMENTS_PATH` (from `config.py`). The subfolders MUST be organized this way (or change the code if not): a document with `dateSignature` `YYYY-MM-DDTXX:XX:XX.XXX+XXXX` and `cid` `CID` in metadata will be stored in `DOCUMENT_PATH/YYYY/MM/CID.docx`. Be aware that months must be written with 2 digits (potential leading zeros).

**NB**: if you have already used the legifrance scraping script in this repository, everything should be fine (just change the bucket's names for S3 configuration). 

## LibreOffice

LibreOffice is the must-have for data conversion using linux os: the main script converts documents from `.docx` to `.pdf` format if their bodies contain images.

Make sure to install LibreOffice:

```{bash}
sudo apt update
sudo apt install libreoffice
```

If you work on Windows/Mac os with Word installed, you can use `docx2pdf` library instead and change the script:

```{cmd}
uv add docx2pdf
```

## Running the main script

The main script is `src/data_conversion/transform_docx_to_md.py`. It relies on a config file `src/data_conversion/config.py` that is meant to be changed depending on your set up and your needs.

A usual set up for a new user would be like this:

- Clone repository:

```{bash}
git clone https://github.com/mateomorin/scrapping_accords.git
cd scrapping_accords
```

- Install dependencies with uv:

```{bash}
pip install uv
uv sync
```

- Configure the `src/data_conversion/config.py` according to your needs. Especially, make sure to change all the variables linked to paths.

- Configure your `.env` file to set `LLM_API_URL` and `LLM_API_KEY` (or use `export` command in bash terminal). You can use `.env.example` as a template (no need to configure the Legifrance API variables).

- Then run the script to convert your files:

```{bash}
uv run src/data_conversion/transform_docx_to_md.py --metadata_name metadata.parquet --output_name default
```

- - `metadata_name`: name of the parquet file in the folder `METADATA_PATH`, e.g. `acco_metadata_2018.parquet`,
- - `output_name` (optional): name of the ouput parquet file in the folder `FULL_DATA_PATH`, by default set to `acco_data_{YEAR}` is `metadata_name` is `acco_metadata_{YEAR}`.

## Using Argo Workflows

If you want to go faster in the process, you can use argo workflow. Just be careful of the concurrency management in the config file, especially because you might spend a lot of tokens (and I mean really a LOT of tokens) quickly. On the other hand, if your server that hosts a LLM is too small, you might receive errors. Thus, you need to find compromises between speed/cost monitoring/server capabilities.

The script that almost fits you is located in `src/data_conversion/argo_transform.yaml`, just make sure to create your own secrets so that everything works well. Change the number of parallel pods if necessary.