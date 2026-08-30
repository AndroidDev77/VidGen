"""Credential-free deployment smoke test, run from inside the private environment.

This runs as a Container Apps Job in the same virtual network as everything it
checks. That is deliberate: the API has internal ingress, and PostgreSQL, Redis,
Blob Storage and Key Vault are reachable only through private endpoints, so a
GitHub-hosted runner cannot see any of them. Running the test here means the
deployment is verified over exactly the network path the application uses.

No credential is passed to this job. Every Azure call is made with the job's own
managed identity, and every application credential is resolved from Key Vault by
the platform, so there is nothing to leak and nothing to rotate.

Two phases:

* **Connectivity** proves each dependency is reachable and correctly private.
* **End to end** creates a clearly tagged throwaway project, selects a
  deterministic fake narration voice through the supported product API, uploads
  a small synthetic fixture, runs the workflow with the deterministic fake
  providers, issues one real control command, and asserts the T22 completion
  gate, a downloadable final render, that no command was left waiting for a
  worker, and a zero provider spend. No paid provider is enabled for this job,
  in the job template itself, so it cannot spend money even if a parameter file
  is wrong.

  The voice selection and the control command are deliberately part of the
  end-to-end path rather than set up out of band: both were places where the
  product looked complete and was not, and a smoke test that skips them proves
  nothing about what a browser user can actually do.

Exit code 0 means every check passed. Any other code fails the deployment and
the workflow rolls application traffic back to the previous revision.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

_LOGGER = logging.getLogger("vidgen.smoke")

#: The owner subject the throwaway project belongs to. Every project this job
#: creates is owned by it and named with the prefix below, so a staging database
#: can be swept for smoke artefacts with one predicate.
SMOKE_SUBJECT = "vidgen-smoke-test"
SMOKE_PROJECT_PREFIX = "smoke-test"


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class SmokeReport:
    results: list[CheckResult] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        self.results.append(CheckResult(name=name, passed=passed, detail=detail))
        _LOGGER.info(
            "check %s: %s%s", name, "PASS" if passed else "FAIL", f" ({detail})" if detail else ""
        )
        return passed

    @property
    def failed(self) -> list[CheckResult]:
        return [result for result in self.results if not result.passed]

    def as_json(self) -> str:
        return json.dumps(
            {
                "passed": not self.failed,
                "checks": [
                    {"name": r.name, "passed": r.passed, "detail": r.detail} for r in self.results
                ],
            },
            indent=2,
            sort_keys=True,
        )


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def _host_of(value: str) -> str:
    if "://" in value:
        return urlparse(value).hostname or value
    return value.split(":", 1)[0]


def _is_private_address(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_private
    except ValueError:
        return False


# -- connectivity phase -------------------------------------------------------


def check_media_tools(report: SmokeReport) -> None:
    """T17 rendering and the T20/T22 media measurements both shell out to these."""
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if path is None:
            report.record(f"media-tool-{tool}", False, "not found on PATH")
            continue
        try:
            completed = subprocess.run(
                [path, "-version"], capture_output=True, check=False, timeout=30
            )
        except (OSError, subprocess.SubprocessError) as error:
            report.record(f"media-tool-{tool}", False, type(error).__name__)
            continue
        first_line = completed.stdout.decode("utf-8", "replace").splitlines()[:1]
        report.record(
            f"media-tool-{tool}",
            completed.returncode == 0,
            first_line[0] if first_line else "",
        )


def check_telemetry(report: SmokeReport) -> None:
    """The T23 stack has to initialise before anything else reports through it."""
    try:
        from vidgen.telemetry.bootstrap import initialize_telemetry

        tracer = initialize_telemetry(service_name="vidgen-smoke")
        with tracer.start_as_current_span("smoke.telemetry"):
            pass
    except Exception as error:
        report.record("telemetry-initialisation", False, type(error).__name__)
        return
    report.record("telemetry-initialisation", True)


def check_private_dns(report: SmokeReport) -> None:
    """Every privately linked name must resolve, and resolve to a private address.

    A public address here means the private DNS zone is missing or unlinked and
    the workload would silently reach the service's public front end instead.
    """
    targets = {
        "postgres": _host_of(_require_env("VIDGEN_SMOKE_POSTGRES_HOST")),
        "redis": _host_of(_require_env("VIDGEN_SMOKE_REDIS_HOST")),
        "blob": _host_of(_require_env("VIDGEN_SMOKE_BLOB_ACCOUNT_URL")),
        "key-vault": f"{_require_env('VIDGEN_SMOKE_KEY_VAULT_NAME')}.vault.azure.net",
    }
    for label, host in targets.items():
        try:
            infos = socket.getaddrinfo(host, None, family=socket.AF_INET)
        except OSError as error:
            report.record(f"private-dns-{label}", False, f"{host}: {error.strerror}")
            continue
        addresses = sorted({str(info[4][0]) for info in infos})
        private = all(_is_private_address(address) for address in addresses)
        report.record(
            f"private-dns-{label}",
            private,
            f"{host} -> {', '.join(addresses)}" if not private else host,
        )


def check_key_vault(report: SmokeReport) -> None:
    """Proves the platform resolved a Key Vault reference with the job identity.

    The Container App secret named below is a Key Vault reference: the platform
    resolves it at replica start using this job's managed identity, over the
    vault's private endpoint, against the vault's RBAC assignment. If any of
    those three is wrong the replica does not start, and if the reference itself
    is wrong the variable is absent. Its *value* is never read into a log, a
    report field or an exception message.
    """
    resolved = bool(os.getenv("VIDGEN_APPLICATIONINSIGHTS_CONNECTION_STRING"))
    report.record(
        "key-vault-resolution",
        resolved,
        "" if resolved else "the platform did not resolve the Key Vault reference",
    )


def check_postgres(report: SmokeReport) -> None:
    """Connectivity, TLS, and exactly one applied Alembic head."""
    try:
        from sqlalchemy import text

        from vidgen.db.alembic_state import alembic_config, database_revisions, script_heads
        from vidgen.db.session import build_engine

        engine = build_engine()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            ssl_in_use = connection.exec_driver_sql(
                "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
            ).scalar()
            heads = script_heads(alembic_config())
            applied = database_revisions(connection)
    except Exception as error:
        report.record("postgres-connectivity", False, type(error).__name__)
        return
    report.record("postgres-connectivity", True)
    report.record(
        "postgres-tls", bool(ssl_in_use), "connection is not encrypted" if not ssl_in_use else ""
    )
    report.record(
        "postgres-single-alembic-head",
        len(heads) == 1 and applied == heads,
        f"heads={','.join(heads)} applied={','.join(applied) or 'base'}",
    )


def check_redis(report: SmokeReport) -> None:
    """A TLS PING over the RESP protocol.

    The repository has no Redis client dependency, and adding one just to prove
    reachability would be worse than speaking sixteen bytes of the protocol.
    """
    url = os.getenv("VIDGEN_REDIS_URL")
    if not url:
        report.record("redis-connectivity", False, "VIDGEN_REDIS_URL is not configured")
        return
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 10000
    password = parsed.password
    use_tls = parsed.scheme in {"rediss", "redis+tls"}
    try:
        raw = socket.create_connection((host, port), timeout=15)
        sock: socket.socket = raw
        if use_tls:
            context = ssl.create_default_context()
            sock = context.wrap_socket(raw, server_hostname=host)
        with sock:
            sock.settimeout(15)
            if password:
                sock.sendall(f"*2\r\n$4\r\nAUTH\r\n${len(password)}\r\n{password}\r\n".encode())
                auth_reply = sock.recv(256)
                if not auth_reply.startswith(b"+OK"):
                    report.record("redis-connectivity", False, "authentication rejected")
                    return
            sock.sendall(b"*1\r\n$4\r\nPING\r\n")
            reply = sock.recv(64)
    except (OSError, ssl.SSLError) as error:
        report.record("redis-connectivity", False, type(error).__name__)
        return
    report.record("redis-connectivity", reply.startswith(b"+PONG"))
    report.record("redis-tls", use_tls, "configured URL is not TLS" if not use_tls else "")


def check_blob_storage(report: SmokeReport) -> None:
    """Write, read back and confirm a blob, with the job's managed identity."""
    try:
        from apps.api.settings import APISettings
        from vidgen.storage.factory import build_blob_store

        settings = APISettings()
        store = build_blob_store(settings)
        payload = f"vidgen-smoke {time.time_ns()}".encode()
        key = f"smoke/{hashlib.sha256(payload).hexdigest()}"
        created = store.put_if_absent(key, payload)
        roundtrip = store.read(key)
        exists = store.exists(key)
    except Exception as error:
        report.record("blob-connectivity", False, type(error).__name__)
        return
    report.record("blob-connectivity", created and exists and roundtrip == payload)


def check_temporal(report: SmokeReport) -> None:
    """Connect to Temporal Cloud and describe the configured namespace."""
    import asyncio

    async def connect() -> str:
        from temporalio.client import Client, TLSConfig

        api_key = os.getenv("TEMPORAL_API_KEY") or None
        tls_enabled = (
            os.getenv("TEMPORAL_TLS_ENABLED", "true" if api_key else "false").lower() == "true"
        )
        namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
        client = await Client.connect(
            os.getenv("TEMPORAL_ADDRESS", _require_env("VIDGEN_TEMPORAL_TARGET_HOST")),
            namespace=namespace,
            api_key=api_key,
            tls=TLSConfig() if tls_enabled else False,
        )
        return client.namespace

    try:
        namespace = asyncio.run(asyncio.wait_for(connect(), timeout=60))
    except Exception as error:
        report.record("temporal-connectivity", False, type(error).__name__)
        return
    report.record("temporal-connectivity", True, namespace)


def check_http_endpoints(report: SmokeReport) -> None:
    """Internal ingress: the API's probes and the web tier's health endpoint."""
    api_url = _require_env("VIDGEN_SMOKE_API_URL").rstrip("/")
    web_url = _require_env("VIDGEN_SMOKE_WEB_URL").rstrip("/")
    with httpx.Client(timeout=30) as client:
        for label, url in (
            ("api-liveness", f"{api_url}/healthz"),
            ("api-readiness", f"{api_url}/readyz"),
            ("web-health", f"{web_url}/healthz"),
        ):
            try:
                response = client.get(url)
            except httpx.HTTPError as error:
                report.record(label, False, type(error).__name__)
                continue
            report.record(label, response.status_code == 200, f"HTTP {response.status_code}")


# -- end-to-end phase ---------------------------------------------------------


def _synthetic_fixture(directory: Path) -> Path:
    """A tiny, deterministic MP4 built locally with the image's own FFmpeg."""
    path = directory / "smoke-fixture.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=24:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )
    return path


def run_end_to_end(report: SmokeReport, workflow_timeout_seconds: int) -> None:
    api_url = _require_env("VIDGEN_SMOKE_API_URL").rstrip("/")
    headers = {"X-Vidgen-User": SMOKE_SUBJECT}
    commit = os.getenv("VIDGEN_COMMIT_SHA", "unknown")[:12]
    project_name = f"{SMOKE_PROJECT_PREFIX} {commit} {int(time.time())}"

    with (
        httpx.Client(base_url=api_url, headers=headers, timeout=120) as client,
        tempfile.TemporaryDirectory() as scratch,
    ):
        response = client.post(
            "/api/v1/projects",
            json={"name": project_name, "target_duration_seconds": 60},
        )
        if response.status_code != 201:
            report.record("e2e-create-project", False, f"HTTP {response.status_code}")
            return
        project_id = response.json()["id"]
        report.record("e2e-create-project", True, project_id)

        # T12 narration resolves the project's voice profile, and the workflow
        # refuses to start without one. Selecting it through the supported API -
        # never by writing a row directly - is what proves the product path a
        # browser user follows actually works in this deployment.
        response = client.get(f"/api/v1/projects/{project_id}/voice-profiles")
        catalog = response.json().get("items", []) if response.status_code == 200 else []
        fake_voices = [item for item in catalog if item.get("provider") == "fake"]
        if not fake_voices:
            report.record(
                "e2e-voice-profile",
                False,
                f"no deterministic fake voice offered (HTTP {response.status_code})",
            )
            return
        response = client.put(
            f"/api/v1/projects/{project_id}/voice-profile",
            json={"voice_profile_id": fake_voices[0]["voice_profile_id"]},
        )
        selected = response.json().get("profile", {}) if response.status_code == 200 else {}
        if not selected.get("selected"):
            report.record("e2e-voice-profile", False, f"HTTP {response.status_code}")
            return
        report.record("e2e-voice-profile", True, str(selected.get("provider_voice_id", "")))

        fixture = _synthetic_fixture(Path(scratch))
        payload = fixture.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        response = client.post(
            f"/api/v1/projects/{project_id}/uploads",
            json={
                "filename": fixture.name,
                "media_type": "video/mp4",
                "expected_size": len(payload),
                "expected_sha256": digest,
                "part_size": 1024 * 1024,
            },
        )
        if response.status_code != 201:
            report.record("e2e-upload-init", False, f"HTTP {response.status_code}")
            return
        upload_id = response.json()["id"]

        response = client.put(
            f"/api/v1/uploads/{upload_id}/parts/1",
            content=payload,
            headers={"Content-Type": "application/octet-stream"},
        )
        if response.status_code >= 400:
            report.record("e2e-upload-part", False, f"HTTP {response.status_code}")
            return
        response = client.post(f"/api/v1/uploads/{upload_id}/complete")
        if response.status_code >= 400:
            report.record("e2e-upload-complete", False, f"HTTP {response.status_code}")
            return
        asset_id = response.json()["asset_id"]
        report.record("e2e-upload-complete", True, asset_id)

        # Asset retrieval: proves the API can mint a signed read URL for a blob
        # it just stored, which exercises the user delegation key path.
        response = client.get(f"/api/v1/assets/{asset_id}/download-url")
        report.record(
            "e2e-asset-retrieval",
            response.status_code == 200 and bool(response.json().get("url")),
            f"HTTP {response.status_code}",
        )

        response = client.post(f"/api/v1/projects/{project_id}/workflow:start")
        if response.status_code >= 400:
            report.record("e2e-workflow-start", False, f"HTTP {response.status_code}")
            return
        report.record("e2e-workflow-start", True, response.json().get("workflow_id", ""))

        # A real control command, dispatched by the real dispatcher, against a
        # project that spends nothing. This is the check that would have caught
        # the whole class of bug T18b fixes: an accepted command that no worker
        # ever consumes.
        _check_control_command(report, client, project_id, workflow_timeout_seconds)

        status_payload = _await_workflow(client, project_id, workflow_timeout_seconds)
        if status_payload is None:
            report.record(
                "e2e-worker-activity-execution",
                False,
                f"no terminal workflow state within {workflow_timeout_seconds}s",
            )
            return
        completed_stages = status_payload.get("completed_stages") or []
        report.record(
            "e2e-worker-activity-execution",
            bool(completed_stages),
            f"stages: {len(completed_stages)}",
        )
        terminal = str(status_payload.get("status", ""))
        report.record("e2e-workflow-terminal-state", terminal.lower() == "completed", terminal)

        response = client.get(f"/api/v1/projects/{project_id}/final-qa/gate")
        gate = response.json() if response.status_code == 200 else {}
        report.record(
            "e2e-t22-completion-gate",
            bool(gate.get("allowed")),
            str(gate.get("reason", f"HTTP {response.status_code}")),
        )

        # The render T22 inspected must be downloadable, not merely recorded.
        response = client.get(f"/api/v1/projects/{project_id}/render")
        render = response.json() if response.status_code == 200 else {}
        asset_id = render.get("final_video_asset_id")
        if asset_id:
            download = client.get(f"/api/v1/assets/{asset_id}/download-url")
            report.record(
                "e2e-final-render-downloadable",
                download.status_code == 200 and bool(download.json().get("url")),
                f"HTTP {download.status_code}",
            )
        else:
            report.record(
                "e2e-final-render-downloadable", False, "the project has no final render asset"
            )

        _check_no_stranded_commands(report, client, project_id)

        response = client.get(f"/api/v1/projects/{project_id}/costs")
        costs = response.json() if response.status_code == 200 else {}
        committed = str(costs.get("committedCost", costs.get("committed", "0")))
        report.record(
            "e2e-zero-provider-cost",
            _is_zero(committed),
            f"committed={committed}",
        )
        _LOGGER.info(
            "smoke project retained for inspection",
            extra={"projectId": project_id},
        )


#: Command statuses that will not change again.
_TERMINAL_COMMANDS = {"completed", "failed", "cancelled", "superseded"}
#: Statuses that mean a worker has genuinely taken the command on.
_DISPATCHED_COMMANDS = {"running", "awaiting_review", *_TERMINAL_COMMANDS}


def _check_control_command(
    report: SmokeReport, client: httpx.Client, project_id: str, timeout_seconds: int
) -> None:
    """Issue one real, free control command and prove a worker consumed it.

    A project continuation is the right probe: it costs nothing, it exercises
    the whole path - durable row, claim, dispatch, real workflow identity - and
    its outcome is visible through the ordinary command API.
    """
    response = client.post(
        f"/api/v1/projects/{project_id}/workflow:continue",
        json={"entry_stage": "final_editorial_qa", "reason": "operator_request"},
        headers={"Idempotency-Key": f"smoke-continue-{project_id}"},
    )
    if response.status_code != 202:
        report.record("e2e-control-command-accepted", False, f"HTTP {response.status_code}")
        return
    command = response.json()["command"]
    report.record("e2e-control-command-accepted", True, command["command_id"])
    # An accepted command must never name a workflow it has not started.
    report.record(
        "e2e-control-command-truthful-acceptance",
        command["workflow_id"] is None and command["status"] == "pending",
        f"status={command['status']} workflow={command['workflow_id']}",
    )
    deadline = time.monotonic() + min(timeout_seconds, 300)
    latest = command
    while time.monotonic() < deadline:
        probe = client.get(
            f"/api/v1/projects/{project_id}/commands/{command['command_id']}"
        )
        if probe.status_code == 200:
            latest = probe.json()["command"]
            if latest["status"] in _DISPATCHED_COMMANDS:
                break
        time.sleep(5)
    dispatched = latest["status"] in _DISPATCHED_COMMANDS
    report.record(
        "e2e-control-command-dispatched",
        dispatched and bool(latest["workflow_id"]),
        f"status={latest['status']} workflow={latest['workflow_id']}",
    )


def _check_no_stranded_commands(
    report: SmokeReport, client: httpx.Client, project_id: str
) -> None:
    """No command may still be waiting for a worker that never came."""
    response = client.get(f"/api/v1/projects/{project_id}/commands")
    if response.status_code != 200:
        report.record("e2e-no-stranded-commands", False, f"HTTP {response.status_code}")
        return
    items = response.json().get("items", [])
    stranded = [
        item["command_id"] for item in items if item["status"] in {"pending", "claimed"}
    ]
    report.record(
        "e2e-no-stranded-commands",
        not stranded,
        f"{len(stranded)} command(s) never reached a worker" if stranded else f"{len(items)} total",
    )


def _is_zero(value: str) -> bool:
    try:
        return float(value) == 0.0
    except ValueError:
        return False


def _await_workflow(
    client: httpx.Client, project_id: str, timeout_seconds: int
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    terminal = {"completed", "failed", "cancelled", "terminated", "timed_out"}
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/projects/{project_id}/workflow")
        if response.status_code == 200:
            last = response.json()
            if str(last.get("status", "")).lower() in terminal:
                return last
        time.sleep(10)
    return last if last and str(last.get("status", "")).lower() in terminal else None


# -- entry point --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VidGen private deployment smoke test")
    parser.add_argument(
        "--mode",
        choices=("connectivity", "e2e"),
        default=os.getenv("VIDGEN_SMOKE_MODE", "e2e"),
        help="connectivity runs the dependency checks only; e2e adds the fake-provider workflow",
    )
    parser.add_argument(
        "--workflow-timeout-seconds",
        type=int,
        default=int(os.getenv("VIDGEN_SMOKE_WORKFLOW_TIMEOUT_SECONDS", "900")),
    )
    arguments = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = SmokeReport()

    check_telemetry(report)
    check_media_tools(report)
    check_private_dns(report)
    check_key_vault(report)
    check_postgres(report)
    check_redis(report)
    check_blob_storage(report)
    check_temporal(report)
    check_http_endpoints(report)

    if arguments.mode == "e2e" and not report.failed:
        run_end_to_end(report, arguments.workflow_timeout_seconds)

    print(report.as_json())
    if report.failed:
        _LOGGER.error("smoke test failed: %s", ", ".join(r.name for r in report.failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
