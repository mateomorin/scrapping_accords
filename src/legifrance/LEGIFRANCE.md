# How to use the scripts to download data from LegiFrance API ?

## 1. Set up your account on PISTE

[PISTE](https://piste.gouv.fr/) is a French-open-data API center. Follow the following steps before running the script:

1. **Create an account on `PISTE`** (admin validation should come quickly);
2. **Go to `APPLICATIONS`**;
3. **Select `Create application`** : Choose a name for it and give details for your usage;
4. **Go to your new application** in the `APPLICATIONS` menu;
5. **Select `Edit Application`**;
6. **Add `Légifrance` in the table `Select APIs`**: you need to accept the CGUs before selecting it;
7. **Go to `Authentication`**;
8. **Save your OAuth Credentials** (Client ID and Secret key).

## 2. Set up your environment

1. Clone the repository:

```{bash}
git clone https://github.com/mateomorin/scrapping_accords
```

2. Install uv and the dependancies:

```{bash}
pip install uv
uv sync
```

3. Create a `.env` file from the template `.env.example` and fill in your Client ID and your Secret key recovered from step 1.

## 3. Set up the download path

This repository was made to work easily on Insee's SSP Cloud, thus every data storage is made on S3.

### If you work with S3

1. Change the constants `METADATA_PATH` and `DOCUMENTS_PATH` in both scraping scripts. Set them to the desired output folders for metadata and texts.

2. Eventually, change your connection to S3 by modifying both boto3 and s3fs clients (nothing to do if using SSP Cloud platform).

### If you do not work with S3

1. In both scraping scripts, remove S3 clients, filesystem variables of all instances of `pd.read_parquet()`, and change `upload_single_document` and `upload_batch_to_s3` to instead just save on the desired path. Yes, it is a little bit more bothersome, sorry.

2. Change the constants `METADATA_PATH` and `DOCUMENTS_PATH` in both scraping scripts. Set them to the desired output folders for metadata and texts.

## 4. Run the scripts

The basic execution of a script is like this:

```{bash}
uv run src/legifrance/scrap_[metadata or texts].py --year [YYYY or all] --month [list of month number or all]
```

**BE SURE TO RUN scrap_metadata.py before scrap_texts.py** for the date you want to scrap.


Here are a few examples:

```{bash}
uv run src/legifrance/scrap_metadata.py --year 2017 --month 9,10,11,12
uv run src/legifrance/scrap_texts.py --year 2017 --month 9
```

```{bash}
# Costly
uv run src/legifrance/scrap_metadata.py --year all
uv run src/legifrance/scrap_texts.py --year 2018
```

```{bash}
uv run src/legifrance/scrap_metadata.py --year 2019
uv run src/legifrance/scrap_texts.py --year 2019 --month 1,4,6
```