"""S3StorageService credential selection (storage keys vs. shared AWS keys)."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.storage_service import S3StorageService


@pytest.fixture
def s3_settings(monkeypatch):
    monkeypatch.setattr(settings, "S3_BUCKET_NAME", "crisismap")
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setattr(settings, "AWS_REGION", "eu-west-1")
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA-REKOGNITION")
    monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setattr(settings, "S3_ACCESS_KEY_ID", None)
    monkeypatch.setattr(settings, "S3_SECRET_ACCESS_KEY", None)
    return monkeypatch


def test_falls_back_to_aws_keys_when_storage_keys_unset(s3_settings):
    kwargs = S3StorageService()._client_kwargs()

    assert kwargs["aws_access_key_id"] == "AKIA-REKOGNITION"
    assert kwargs["aws_secret_access_key"] == "aws-secret"
    assert kwargs["endpoint_url"] == "http://minio:9000"
    assert kwargs["region_name"] == "eu-west-1"


def test_storage_keys_take_precedence_over_aws_keys(s3_settings):
    s3_settings.setattr(settings, "S3_ACCESS_KEY_ID", "minio-user")
    s3_settings.setattr(settings, "S3_SECRET_ACCESS_KEY", "minio-pass")

    kwargs = S3StorageService()._client_kwargs()

    assert kwargs["aws_access_key_id"] == "minio-user"
    assert kwargs["aws_secret_access_key"] == "minio-pass"


def test_no_keys_leaves_credentials_to_the_default_chain(s3_settings):
    s3_settings.setattr(settings, "AWS_ACCESS_KEY_ID", None)
    s3_settings.setattr(settings, "AWS_SECRET_ACCESS_KEY", None)

    kwargs = S3StorageService()._client_kwargs()

    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs
