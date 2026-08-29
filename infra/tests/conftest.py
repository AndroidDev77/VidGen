"""Compile the Bicep sources once per session and expose them to the tests.

Every assertion in this directory is made against the *compiled ARM template*
rather than against the Bicep text, so a refactor that preserves behaviour keeps
the tests green and a change that silently alters a deployed property does not.
No Azure subscription, credential or deployment is involved: `bicep build` is a
local, free, offline operation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BICEP_DIR = REPO_ROOT / "infra" / "bicep"
MODULES_DIR = BICEP_DIR / "modules"
ENVIRONMENTS_DIR = BICEP_DIR / "environments"

#: Obviously-fake values so `readEnvironmentVariable` can be evaluated. They are
#: never used for a deployment and are asserted against in the parameter tests.
PLACEHOLDER_ENVIRONMENT = {
    "VIDGEN_AZURE_LOCATION": "eastus2",
    "VIDGEN_APP_IMAGE": "placeholder.azurecr.io/vidgen-app@sha256:" + "0" * 64,
    "VIDGEN_WEB_IMAGE": "placeholder.azurecr.io/vidgen-web@sha256:" + "0" * 64,
    "VIDGEN_DEPLOY_PRINCIPAL_ID": "00000000-0000-0000-0000-000000000000",
    "VIDGEN_DEPLOY_PRINCIPAL_NAME": "placeholder-deployer",
    "VIDGEN_TEMPORAL_ADDRESS": "placeholder.tmprl.cloud:7233",
    "VIDGEN_TEMPORAL_NAMESPACE": "placeholder",
    "VIDGEN_OWNER": "placeholder-owner",
    "VIDGEN_COST_CENTER": "placeholder-cost-centre",
}


@lru_cache(maxsize=1)
def bicep_command() -> tuple[str, ...] | None:
    """The available Bicep CLI, or None when neither form is installed."""
    explicit = os.environ.get("BICEP_BIN")
    if explicit and shutil.which(explicit.split()[0]):
        return tuple(explicit.split())
    if shutil.which("bicep"):
        return ("bicep",)
    if shutil.which("az"):
        return ("az", "bicep")
    return None


def _run_bicep(*arguments: str) -> str:
    command = bicep_command()
    assert command is not None
    completed = subprocess.run(
        [*command, *arguments],
        capture_output=True,
        check=False,
        text=True,
        env={**os.environ, **PLACEHOLDER_ENVIRONMENT},
        timeout=600,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"bicep {' '.join(arguments)} failed:\n{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout


@lru_cache(maxsize=32)
def compile_template(relative_path: str) -> dict[str, Any]:
    output = _run_bicep("build", "--stdout", str(BICEP_DIR / relative_path))
    parsed: dict[str, Any] = json.loads(output)
    return parsed


@lru_cache(maxsize=8)
def compile_parameters(relative_path: str) -> dict[str, Any]:
    output = _run_bicep("build-params", "--stdout", str(BICEP_DIR / relative_path))
    envelope: dict[str, Any] = json.loads(output)
    # `build-params --stdout` wraps the JSON parameter file in a small envelope.
    if "parametersJson" in envelope:
        inner: dict[str, Any] = json.loads(envelope["parametersJson"])
        return inner
    return envelope


def _resource_entries(node: dict[str, Any]) -> list[dict[str, Any]]:
    """The resources of one template.

    Bicep emits ARM language version 2.0, where `resources` is an object keyed
    by symbolic name rather than an array. Both shapes are accepted so the tests
    do not depend on which one the CLI produces.
    """
    resources = node.get("resources") or []
    if isinstance(resources, dict):
        return [value for value in resources.values() if isinstance(value, dict)]
    return [value for value in resources if isinstance(value, dict)]


def resources_of_type(template: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    """Every resource of a type, including those inside nested module templates."""
    found: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        for resource in _resource_entries(node):
            if resource.get("type") == resource_type:
                found.append(resource)
            nested = resource.get("properties", {}).get("template")
            if isinstance(nested, dict):
                walk(nested)

    walk(template)
    return found


def resolve_type(template: dict[str, Any], declaration: dict[str, Any]) -> dict[str, Any]:
    """Follow a parameter's `$ref` into the template's type definitions."""
    reference = declaration.get("$ref")
    if not reference:
        return declaration
    node: Any = template
    for part in reference.lstrip("#/").split("/"):
        node = node[part]
    assert isinstance(node, dict)
    return node


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if bicep_command() is not None:
        return
    skip = pytest.mark.skip(
        reason="the Bicep CLI is not installed; CI installs it and always runs these tests"
    )
    for item in items:
        if "compiled" in getattr(item, "fixturenames", ()) or item.get_closest_marker("bicep"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def compiled() -> Iterator[dict[str, Any]]:
    """The compiled main template, with every module inlined."""
    yield compile_template("main.bicep")


@pytest.fixture(scope="session")
def staging_parameters() -> dict[str, Any]:
    return compile_parameters("environments/staging.bicepparam")


@pytest.fixture(scope="session")
def production_parameters() -> dict[str, Any]:
    return compile_parameters("environments/production.example.bicepparam")
