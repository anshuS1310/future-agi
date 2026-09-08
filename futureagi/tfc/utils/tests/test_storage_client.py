import importlib
import sys


STORAGE_ENV_KEYS = (
    "STORAGE_BACKEND",
    "S3_ENDPOINT",
    "S3_ENDPOINT_URL",
    "S3_SECURE",
    "MINIO_URL",
    "MINIO_REGION",
    "GCS_HMAC_ACCESS_KEY",
    "GCS_HMAC_SECRET_KEY",
    "AWS_DEFAULT_REGION",
)


def reload_storage_client(monkeypatch, **env):
    for key in STORAGE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    sys.modules.pop("tfc.utils.storage_client", None)
    return importlib.import_module("tfc.utils.storage_client")


def test_minio_object_url_uses_public_minio_url(monkeypatch):
    storage_client = reload_storage_client(
        monkeypatch,
        STORAGE_BACKEND="minio",
        S3_ENDPOINT_URL="http://minio:9000",
        MINIO_URL="http://localhost:9005",
    )

    assert (
        storage_client.get_object_url("futureagi", "tempcust/image.png")
        == "http://localhost:9005/futureagi/tempcust/image.png"
    )


def test_minio_object_url_falls_back_to_default_when_minio_url_unset(monkeypatch):
    storage_client = reload_storage_client(
        monkeypatch,
        STORAGE_BACKEND="minio",
        S3_ENDPOINT_URL="http://minio:9000",
    )

    assert (
        storage_client.get_object_url("futureagi", "tempcust/image.png")
        == "http://localhost:9005/futureagi/tempcust/image.png"
    )


def test_s3_object_url_ignores_minio_url(monkeypatch):
    storage_client = reload_storage_client(
        monkeypatch,
        STORAGE_BACKEND="s3",
        MINIO_URL="http://localhost:9005",
        AWS_DEFAULT_REGION="us-east-1",
    )

    assert (
        storage_client.get_object_url("futureagi", "tempcust/image.png")
        == "https://futureagi.s3.us-east-1.amazonaws.com/tempcust/image.png"
    )


def test_gcs_object_url_ignores_s3_and_minio_urls(monkeypatch):
    storage_client = reload_storage_client(
        monkeypatch,
        STORAGE_BACKEND="gcs",
        S3_ENDPOINT_URL="http://minio:9000",
        MINIO_URL="http://localhost:9005",
    )

    assert (
        storage_client.get_object_url("futureagi", "tempcust/image.png")
        == "https://storage.googleapis.com/futureagi/tempcust/image.png"
    )


def test_gcs_client_uses_minio_region(monkeypatch):
    storage_client = reload_storage_client(
        monkeypatch,
        STORAGE_BACKEND="gcs",
        MINIO_REGION="europe-west3",
        GCS_HMAC_ACCESS_KEY="access",
        GCS_HMAC_SECRET_KEY="secret",
    )
    captured = {}

    def fake_minio(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(storage_client, "Minio", fake_minio)
    storage_client.reset_storage_client()
    storage_client.get_storage_client()

    assert captured["args"][0] == "storage.googleapis.com"
    assert captured["kwargs"]["region"] == "europe-west3"


def test_gcs_client_falls_back_to_auto_region(monkeypatch):
    storage_client = reload_storage_client(
        monkeypatch,
        STORAGE_BACKEND="gcs",
        GCS_HMAC_ACCESS_KEY="access",
        GCS_HMAC_SECRET_KEY="secret",
    )
    captured = {}

    def fake_minio(*args, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(storage_client, "Minio", fake_minio)
    storage_client.reset_storage_client()
    storage_client.get_storage_client()

    assert captured["kwargs"]["region"] == "auto"


def test_minio_urls_are_recognised_as_our_own_object(monkeypatch):
    """On MinIO the server-reachable endpoint is a private address by construction, so an eval can
    only read our recordings by bucket and key rather than over a guarded HTTP fetch."""
    storage_client = reload_storage_client(
        monkeypatch,
        STORAGE_BACKEND="minio",
        MINIO_URL="http://localhost:9005",
        S3_ENDPOINT_URL="http://minio:9000",
    )

    expected = ("fi-content-dev", "alk-harness/run/call/object")
    assert (
        storage_client.own_storage_object(
            "http://localhost:9005/fi-content-dev/alk-harness/run/call/object"
        )
        == expected
    )
    assert (
        storage_client.own_storage_object(
            "http://minio:9000/fi-content-dev/alk-harness/run/call/object"
        )
        == expected
    )


def test_a_provider_recording_url_is_not_our_own_object(monkeypatch):
    """The SSRF guard must keep protecting genuinely remote audio."""
    storage_client = reload_storage_client(
        monkeypatch,
        STORAGE_BACKEND="minio",
        MINIO_URL="http://localhost:9005",
        S3_ENDPOINT_URL="http://minio:9000",
    )

    assert storage_client.own_storage_object("https://api.vapi.ai/recording/abc.mp3") is None
    assert storage_client.own_storage_object("http://169.254.169.254/latest/meta-data") is None
    assert storage_client.own_storage_object("") is None
    assert storage_client.own_storage_object("http://localhost:9005/") is None
    assert storage_client.own_storage_object("http://localhost:9005/bucket-only") is None


def test_s3_object_urls_are_recognised_in_both_addressing_forms(monkeypatch):
    storage_client = reload_storage_client(monkeypatch, STORAGE_BACKEND="s3")

    assert storage_client.own_storage_object(
        "https://fi-content.s3.us-east-2.amazonaws.com/calls/one.mp3"
    ) == ("fi-content", "calls/one.mp3")
    assert storage_client.own_storage_object(
        "https://s3.us-east-2.amazonaws.com/fi-content/calls/one.mp3"
    ) == ("fi-content", "calls/one.mp3")
    assert storage_client.own_storage_object("https://example.com/fi-content/one.mp3") is None


def test_gcs_object_urls_are_recognised(monkeypatch):
    storage_client = reload_storage_client(monkeypatch, STORAGE_BACKEND="gcs")

    assert storage_client.own_storage_object(
        "https://storage.googleapis.com/fi-content/calls/one.mp3"
    ) == ("fi-content", "calls/one.mp3")
    assert storage_client.own_storage_object("https://storage.example.com/fi-content/one.mp3") is None
