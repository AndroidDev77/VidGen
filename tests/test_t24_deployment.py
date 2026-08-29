"""T24: the application-side changes a private Azure deployment depends on.

Everything here runs offline. The Azure adapter is exercised against a fake
service client, so the tests prove the adapter's contract - conditional writes,
streaming, signed reads - without an account, a credential or a network.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from azure.core.exceptions import ResourceExistsError
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from apps.api.settings import APISettings
from vidgen.storage.azure_blob import AzureBlobStore
from vidgen.storage.blob import FilesystemBlobStore
from vidgen.storage.factory import build_blob_store

# -- the blob backend factory --------------------------------------------------


def test_the_default_backend_is_the_filesystem_store(tmp_path: Path) -> None:
    settings = APISettings(blob_root=tmp_path, signing_secret="x")
    assert settings.blob_backend == "filesystem"
    assert isinstance(build_blob_store(settings), FilesystemBlobStore)


def test_an_unknown_backend_is_rejected() -> None:
    with pytest.raises(ValueError, match="blob_backend"):
        APISettings(blob_backend="s3")


def test_the_azure_backend_requires_an_account_url(tmp_path: Path) -> None:
    settings = APISettings(blob_backend="azure", blob_root=tmp_path, signing_secret="x")
    with pytest.raises(ValueError, match="VIDGEN_BLOB_ACCOUNT_URL"):
        build_blob_store(settings)


def test_the_azure_backend_needs_no_key_or_connection_string() -> None:
    """The settings surface offers nowhere to put an account key.

    Access is by managed identity only; a key in configuration would be a key
    that can be copied out of configuration.
    """
    fields = set(APISettings.model_fields)
    assert "blob_account_key" not in fields
    assert "blob_connection_string" not in fields
    assert {"blob_account_url", "blob_container", "blob_backend"} <= fields


# -- the Azure adapter ---------------------------------------------------------


class _FakeBlobClient:
    def __init__(self, store: dict[str, bytes], key: str) -> None:
        self._store = store
        self._key = key

    def upload_blob(self, data: Any, *, length: int, overwrite: bool) -> None:
        assert overwrite is False, "an immutable asset must never be overwritten"
        if self._key in self._store:
            # What the service returns for a failed If-None-Match precondition.
            raise ResourceExistsError("blob already exists")
        payload = data if isinstance(data, bytes) else data.read()
        assert len(payload) == length
        self._store[self._key] = payload

    def exists(self) -> bool:
        return self._key in self._store

    def download_blob(self) -> _FakeDownloader:
        if self._key not in self._store:
            raise FileNotFoundError(self._key)
        return _FakeDownloader(self._store[self._key])


class _FakeDownloader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def readall(self) -> bytes:
        return self._payload

    def readinto(self, stream: io.BufferedWriter) -> int:
        return stream.write(self._payload)


class _FakeContainerClient:
    url = "https://account.blob.core.windows.net/assets"

    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def get_blob_client(self, key: str) -> _FakeBlobClient:
        return _FakeBlobClient(self._store, key)


class _FakeServiceClient:
    account_name = "account"

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.delegation_requests = 0

    def get_container_client(self, name: str) -> _FakeContainerClient:
        return _FakeContainerClient(self.store)

    def get_user_delegation_key(self, *, key_start_time: Any, key_expiry_time: Any) -> Any:
        self.delegation_requests += 1

        class _Key:
            signed_oid = "oid"
            signed_tid = "tid"
            signed_start = key_start_time.isoformat()
            signed_expiry = key_expiry_time.isoformat()
            signed_service = "b"
            signed_version = "2023-11-03"
            signed_delegated_user_tid = None
            value = "ZmFrZS1kZWxlZ2F0aW9uLWtleQ=="

        return _Key()


@pytest.fixture
def azure_store() -> tuple[AzureBlobStore, _FakeServiceClient]:
    service = _FakeServiceClient()
    store = AzureBlobStore(
        account_url="https://account.blob.core.windows.net",
        container="assets",
        service_client=service,  # type: ignore[arg-type]
    )
    return store, service


def test_a_content_addressed_write_is_created_once(
    azure_store: tuple[AzureBlobStore, _FakeServiceClient],
) -> None:
    store, _ = azure_store
    key = "sha256/aa/bb"
    assert store.put_if_absent(key, b"payload") is True
    # The second writer learns it did not create the blob, which is what lets a
    # retried activity deduplicate instead of rewriting immutable provenance.
    assert store.put_if_absent(key, b"payload") is False
    assert store.read(key) == b"payload"
    assert store.exists(key) is True


def test_a_file_write_streams_and_is_also_conditional(
    azure_store: tuple[AzureBlobStore, _FakeServiceClient], tmp_path: Path
) -> None:
    store, service = azure_store
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 4096)
    assert store.put_file_if_absent("sha256/cc/dd", source) is True
    assert store.put_file_if_absent("sha256/cc/dd", source) is False
    assert service.store["sha256/cc/dd"] == b"x" * 4096


def test_a_download_never_leaves_a_truncated_file(
    azure_store: tuple[AzureBlobStore, _FakeServiceClient], tmp_path: Path
) -> None:
    store, _ = azure_store
    store.put_if_absent("sha256/ee/ff", b"canonical")
    destination = tmp_path / "nested" / "out.bin"
    store.copy_to("sha256/ee/ff", destination)
    assert destination.read_bytes() == b"canonical"
    # The temporary file it downloads through is always removed.
    assert list(destination.parent.glob(".*.download")) == []


def test_a_signed_url_is_a_user_delegation_sas(
    azure_store: tuple[AzureBlobStore, _FakeServiceClient],
) -> None:
    store, service = azure_store
    store.put_if_absent("sha256/11/22", b"payload")
    url = store.signed_read_url("sha256/11/22", expires_in_seconds=300)
    assert url.startswith("https://account.blob.core.windows.net/assets/sha256/11/22?")
    assert "sig=" in url
    # Derived from the caller's identity, not from an account key.
    assert service.delegation_requests == 1
    # The key is cached, so listing a project's assets does not issue one
    # request per asset.
    store.signed_read_url("sha256/11/22", expires_in_seconds=300)
    assert service.delegation_requests == 1


def test_a_signed_url_expiry_is_bounded(
    azure_store: tuple[AzureBlobStore, _FakeServiceClient],
) -> None:
    store, _ = azure_store
    store.put_if_absent("sha256/33/44", b"payload")
    with pytest.raises(ValueError, match="positive"):
        store.signed_read_url("sha256/33/44", expires_in_seconds=0)
    with pytest.raises(ValueError, match="maximum"):
        store.signed_read_url("sha256/33/44", expires_in_seconds=100_000)


def test_a_signed_url_for_a_missing_blob_is_refused(
    azure_store: tuple[AzureBlobStore, _FakeServiceClient],
) -> None:
    store, _ = azure_store
    with pytest.raises(FileNotFoundError):
        store.signed_read_url("sha256/absent", expires_in_seconds=60)


# -- health endpoints ----------------------------------------------------------


def test_liveness_does_not_touch_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.api import main as api_main

    def explode() -> Any:
        raise AssertionError("liveness must not open a database connection")

    monkeypatch.setattr(api_main, "get_engine", explode)
    with TestClient(api_main.create_app()) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_unavailable_when_the_database_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.api import main as api_main

    def failing_engine() -> Any:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(api_main, "get_engine", failing_engine)
    with TestClient(api_main.create_app()) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    # The driver error can carry the connection string, so it is never returned.
    assert "connection refused" not in response.text


# -- no migration at application startup ---------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "apps/api/main.py",
        "apps/api/dependencies.py",
        "workers/temporal_worker/main.py",
        "workers/temporal_worker/production_handlers.py",
    ],
)
def test_no_application_entry_point_can_run_a_migration(path: str) -> None:
    """A schema change must be a deliberate, single-writer step, not a side
    effect of a replica starting."""
    source = Path(path).read_text()
    # Prose is allowed to explain why; executable statements are not.
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert "import alembic" not in code
    assert "from alembic" not in code
    assert "command.upgrade" not in code
    assert "create_all" not in code


def test_the_migration_runner_has_no_downgrade_path() -> None:
    source = Path("scripts/run_migrations.py").read_text()
    code = source.split('"""', 2)[-1]
    assert "command.downgrade" not in code
    assert 'command.upgrade(config, "head")' in code


def test_the_migration_runner_serialises_on_an_advisory_lock() -> None:
    from vidgen.db.alembic_state import MIGRATION_ADVISORY_LOCK_KEY

    source = Path("scripts/run_migrations.py").read_text()
    assert "pg_try_advisory_lock" in source
    assert "pg_advisory_unlock" in source
    assert isinstance(MIGRATION_ADVISORY_LOCK_KEY, int)


def test_the_migration_runner_refuses_a_branched_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_migrations as runner

    monkeypatch.setattr(runner, "script_heads", lambda config: ("aaaa", "bbbb"))
    # Non-zero exit blocks the traffic switch. Nothing is applied.
    assert runner.upgrade_to_head() == 2
    assert runner.verify_single_head() == 2


def test_the_repository_has_exactly_one_alembic_head() -> None:
    from vidgen.db.alembic_state import alembic_config, script_heads

    assert len(script_heads(alembic_config())) == 1


# -- telemetry bootstrap -------------------------------------------------------


def test_telemetry_installs_no_exporter_when_none_is_configured() -> None:
    from vidgen.telemetry.bootstrap import build_span_processor
    from vidgen.telemetry.config import TelemetrySettings

    settings = TelemetrySettings(
        applicationinsights_connection_string=None, otel_exporter_otlp_endpoint=None
    )
    assert build_span_processor(settings) is None


def test_telemetry_export_is_batched_not_synchronous() -> None:
    """A synchronous exporter would block an activity on Azure Monitor."""
    source = Path("src/vidgen/telemetry/bootstrap.py").read_text()
    assert "BatchSpanProcessor" in source
    assert "SimpleSpanProcessor" not in source


# -- images --------------------------------------------------------------------


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "apps/web/Dockerfile"])
def test_images_pin_their_bases_by_digest_and_drop_root(dockerfile: str) -> None:
    source = Path(dockerfile).read_text()
    for line in source.splitlines():
        if line.startswith("ARG ") and "_IMAGE=" in line:
            assert "@sha256:" in line, f"{dockerfile}: {line} is not digest pinned"
    assert ":latest" not in source
    assert "\nUSER " in source
    assert "org.opencontainers.image.revision" in source
    assert "org.opencontainers.image.created" in source


def test_the_application_image_provides_ffmpeg_and_an_explicit_temp_directory() -> None:
    source = Path("Dockerfile").read_text()
    # ffmpeg carries both binaries; T17, T20 and T22 all need them on PATH.
    assert "ffmpeg" in source
    assert "ca-certificates" in source
    assert "TMPDIR=/tmp/vidgen" in source
    assert "uv sync --frozen --no-dev --extra azure" in source


def test_the_build_context_excludes_credentials() -> None:
    ignored = Path(".dockerignore").read_text().splitlines()
    for entry in (".env", ".git", ".local-data"):
        assert entry in ignored


def test_the_web_image_keeps_the_api_endpoint_a_runtime_setting() -> None:
    source = Path("apps/web/Dockerfile").read_text()
    # An empty build-time base URL means the bundle calls the same-origin /api
    # path, and nginx substitutes the real upstream at container start.
    assert 'ARG VITE_VIDGEN_API_BASE_URL=""' in source
    assert 'ENV VIDGEN_API_UPSTREAM="http://api:8000"' in source
    assert "location = /healthz" in Path("apps/web/nginx.conf").read_text()


# -- regressions ---------------------------------------------------------------


def test_the_worker_uses_a_real_shutdown_api() -> None:
    """`Worker.run()` takes no arguments in temporalio; passing one would raise
    a TypeError on every container start."""
    import inspect

    from temporalio.worker import Worker

    assert list(inspect.signature(Worker.run).parameters) == ["self"]
    assert hasattr(Worker, "__aenter__") and hasattr(Worker, "__aexit__")

    source = Path("workers/temporal_worker/main.py").read_text()
    assert "async with worker:" in source
    assert "shutdown_event" not in source
    assert "graceful_shutdown_timeout" in source


def test_the_api_authenticates_to_temporal_cloud() -> None:
    """A control plane that connects without an API key or TLS cannot reach
    Temporal Cloud at all."""
    import inspect

    from vidgen.review.workflow_control import TemporalWorkflowController

    parameters = inspect.signature(TemporalWorkflowController.__init__).parameters
    assert "api_key" in parameters
    assert "tls_enabled" in parameters

    source = inspect.getsource(TemporalWorkflowController._client)
    assert "api_key=self._api_key" in source
    assert "TLSConfig()" in source

    assert {"temporal_api_key", "temporal_tls_enabled"} <= set(APISettings.model_fields)


def test_temporal_tls_follows_the_api_key_by_default() -> None:
    """A configured key means Temporal Cloud, which is always TLS; a bare local
    dev server has neither."""
    from vidgen.review.workflow_control import TemporalWorkflowController

    assert TemporalWorkflowController("host:7233", "ns")._tls_enabled is False
    assert TemporalWorkflowController("host:7233", "ns", api_key="k")._tls_enabled is True
    # An explicit choice still wins.
    assert (
        TemporalWorkflowController("host:7233", "ns", api_key="k", tls_enabled=False)._tls_enabled
        is False
    )


def test_the_worker_refuses_to_start_without_a_provider_or_permission_to_fake() -> None:
    """This is why an all-providers-off staging environment has to set
    VIDGEN_TEMPORAL_ALLOW_FAKE_PROVIDERS explicitly."""
    from workers.temporal_worker.production_handlers import build_shot_production_handlers

    settings = APISettings(
        openai_api_key=None,
        runway_api_secret=None,
        temporal_allow_fake_providers=False,
    )
    with pytest.raises(ValueError, match="not configured"):
        build_shot_production_handlers(settings)

    permitted = settings.model_copy(update={"temporal_allow_fake_providers": True})
    assert build_shot_production_handlers(permitted)
