"""Tests for the S3 write-zone guard (heures_supp.core.utils.safe_remote_path)."""

import pytest
from heures_supp.core import config, utils


def test_allows_output_root():
    path = f"{config.SHARDS_PATH}shard_00000.parquet"
    assert utils.safe_remote_path(path) == path


def test_allows_tests_root():
    path = f"{config.TESTS_ROOT}tmp_shard.parquet"
    assert utils.safe_remote_path(path) == path


def test_refuses_source_zones():
    with pytest.raises(PermissionError):
        utils.safe_remote_path("s3://mateomorin/legifrance/metadata/acco_metadata_2024.parquet")
    with pytest.raises(PermissionError):
        utils.safe_remote_path("s3://mateomorin/legifrance/documents/2024/01/ACCOTEXT000.docx")


def test_refuses_other_buckets():
    with pytest.raises(PermissionError):
        utils.safe_remote_path("s3://some-other-bucket/data.parquet")
