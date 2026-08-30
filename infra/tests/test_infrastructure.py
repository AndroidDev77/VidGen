"""Deterministic checks on the compiled Azure infrastructure.

These are policy tests, not deployment tests. They compile the Bicep sources
locally and assert the properties that make the environment private, least
privileged, reproducible and safe to roll back. Nothing here contacts Azure, so
the whole file runs on every pull request at no cost.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from infra.tests.conftest import (
    BICEP_DIR,
    ENVIRONMENTS_DIR,
    MODULES_DIR,
    REPO_ROOT,
    compile_template,
    resolve_type,
    resources_of_type,
)

pytestmark = pytest.mark.bicep

REQUIRED_TAGS = frozenset(
    {"application", "environment", "owner", "costCenter", "managedBy", "repository"}
)


def module(name: str) -> dict[str, Any]:
    return compile_template(f"modules/{name}.bicep")


def only(resources: list[dict[str, Any]]) -> dict[str, Any]:
    assert len(resources) == 1, f"expected exactly one resource, found {len(resources)}"
    return resources[0]


def by_name(resources: list[dict[str, Any]], fragment: str) -> dict[str, Any]:
    matches = [r for r in resources if fragment in json.dumps(r.get("name", ""))]
    assert matches, f"no resource whose name contains {fragment!r}"
    return matches[0]


def copy_input(node: dict[str, Any], loop_name: str) -> Any:
    """The `input` of an ARM copy loop.

    A Bicep `[for x in xs: ...]` compiles to a `copy` block rather than to a
    literal array, so a property built that way has no key of its own.
    """
    for entry in node.get("copy", []) or []:
        if entry.get("name") == loop_name:
            return entry["input"]
    raise AssertionError(f"no copy loop named {loop_name!r} in {sorted(node)}")


def user_defined_function(template: dict[str, Any], name: str) -> dict[str, Any]:
    """One exported Bicep function, as ARM records it."""
    for namespace in template.get("functions", []) or []:
        members = namespace.get("members", {})
        if name in members:
            found: dict[str, Any] = members[name]
            return found
    raise AssertionError(f"no user-defined function named {name!r}")


def property_text(resource: dict[str, Any], *path: str) -> str:
    """The JSON text of a property, whether it is literal, an expression or a loop."""
    node: Any = resource
    for part in path[:-1]:
        node = node[part]
    last = path[-1]
    if isinstance(node, dict) and last in node:
        return json.dumps(node[last])
    return json.dumps(copy_input(node, last))


# -- compilation ---------------------------------------------------------------


def test_every_template_compiles() -> None:
    templates = sorted(BICEP_DIR.rglob("*.bicep"))
    assert templates, "no Bicep templates found"
    for template in templates:
        compiled = compile_template(str(template.relative_to(BICEP_DIR)))
        assert compiled["$schema"].endswith("deploymentTemplate.json#")


def test_main_declares_every_module_in_the_documented_layout() -> None:
    expected = {
        "identities",
        "networking",
        "private_dns",
        "container_registry",
        "container_apps_environment",
        "container_apps",
        "container_jobs",
        "postgres",
        "storage",
        "redis",
        "key_vault",
        "monitoring",
        "role_assignments",
    }
    present = {path.stem for path in MODULES_DIR.glob("*.bicep")}
    assert expected <= present


# -- naming and tags -----------------------------------------------------------


def test_resource_names_are_deterministic_functions_of_the_parameters(
    compiled: dict[str, Any],
) -> None:
    """No name may depend on time, a new GUID, or anything else non-repeatable."""
    source = json.dumps(compiled)
    assert "utcNow" not in source
    assert '"newGuid()"' not in source
    # uniqueString over the resource group id is deterministic for a given
    # resource group, which is what globally unique names need.
    assert "uniqueString" in source


def test_every_tagged_resource_carries_the_six_required_tags(compiled: dict[str, Any]) -> None:
    text = json.dumps(compiled)
    for tag in REQUIRED_TAGS:
        assert f'"{tag}"' in text, f"tag {tag} is not part of the tag object"
    tag_type = resolve_type(compiled, compiled["parameters"]["tags"])
    assert set(tag_type.get("properties", {})) == REQUIRED_TAGS


# -- privacy -------------------------------------------------------------------


def test_public_ingress_is_disabled_by_default(compiled: dict[str, Any]) -> None:
    assert compiled["parameters"]["publicIngressEnabled"]["defaultValue"] is False


def test_staging_parameters_keep_the_environment_private(
    staging_parameters: dict[str, Any],
) -> None:
    assert staging_parameters["parameters"]["publicIngressEnabled"]["value"] is False


def test_container_apps_environment_is_internal_when_ingress_is_private() -> None:
    environment = only(
        resources_of_type(module("container_apps_environment"), "Microsoft.App/managedEnvironments")
    )
    internal = environment["properties"]["vnetConfiguration"]["internal"]
    # The expression is the negation of publicIngressEnabled, so the two can
    # never disagree.
    assert "publicIngressEnabled" in json.dumps(internal)
    assert json.dumps(internal).startswith('"[not(')


def test_the_api_is_never_externally_reachable() -> None:
    apps = resources_of_type(module("container_apps"), "Microsoft.App/containerApps")
    api = by_name(apps, "-api")
    assert api["properties"]["configuration"]["ingress"]["external"] is False


def test_web_ingress_follows_the_public_ingress_parameter() -> None:
    apps = resources_of_type(module("container_apps"), "Microsoft.App/containerApps")
    web = by_name(apps, "-web")
    external = web["properties"]["configuration"]["ingress"]["external"]
    assert external == "[parameters('publicIngressEnabled')]"


def test_the_worker_has_no_ingress() -> None:
    apps = resources_of_type(module("container_apps"), "Microsoft.App/containerApps")
    worker = by_name(apps, "-worker")
    assert "ingress" not in worker["properties"]["configuration"]


def test_postgres_is_vnet_injected_and_has_no_public_endpoint() -> None:
    server = only(
        resources_of_type(module("postgres"), "Microsoft.DBforPostgreSQL/flexibleServers")
    )
    network = server["properties"]["network"]
    assert network["delegatedSubnetResourceId"] == "[parameters('delegatedSubnetId')]"
    assert network["privateDnsZoneArmResourceId"] == "[parameters('privateDnsZoneId')]"
    # A VNet-injected flexible server has no public endpoint at all, so there is
    # deliberately no firewall rule resource in this module.
    assert not resources_of_type(
        module("postgres"), "Microsoft.DBforPostgreSQL/flexibleServers/firewallRules"
    )


def test_postgres_requires_tls() -> None:
    configurations = resources_of_type(
        module("postgres"), "Microsoft.DBforPostgreSQL/flexibleServers/configurations"
    )
    names = {json.dumps(c["name"]) for c in configurations}
    assert any("require_secure_transport" in name for name in names)
    assert any("ssl_min_protocol_version" in name for name in names)


def test_storage_is_private_and_key_free() -> None:
    account = only(resources_of_type(module("storage"), "Microsoft.Storage/storageAccounts"))
    properties = account["properties"]
    assert properties["allowBlobPublicAccess"] is False
    # No account key means no key to leak and no key-based signed URL.
    assert properties["allowSharedKeyAccess"] is False
    assert properties["supportsHttpsTrafficOnly"] is True
    assert properties["minimumTlsVersion"] == "TLS1_2"
    assert "publicNetworkAccessEnabled" in json.dumps(properties["publicNetworkAccess"])
    assert "publicNetworkAccessEnabled" in json.dumps(properties["networkAcls"]["defaultAction"])


def test_storage_preserves_immutable_assets() -> None:
    service = only(
        resources_of_type(module("storage"), "Microsoft.Storage/storageAccounts/blobServices")
    )
    assert service["properties"]["isVersioningEnabled"] is True
    assert service["properties"]["deleteRetentionPolicy"]["enabled"] is True
    containers = resources_of_type(
        module("storage"), "Microsoft.Storage/storageAccounts/blobServices/containers"
    )
    assert len(containers) == 2
    for container in containers:
        assert container["properties"]["publicAccess"] == "None"


def test_storage_lifecycle_never_deletes_a_canonical_asset() -> None:
    policy = only(
        resources_of_type(module("storage"), "Microsoft.Storage/storageAccounts/managementPolicies")
    )
    rules = policy["properties"]["policy"]["rules"]
    assets_rule = next(r for r in rules if "asset" in r["name"])
    # Only superseded versions expire; the current blob has no delete action.
    assert "baseBlob" not in assets_rule["definition"]["actions"]
    scratch_rule = next(r for r in rules if "scratch" in r["name"])
    assert "delete" in scratch_rule["definition"]["actions"]["baseBlob"]


def test_key_vault_uses_rbac_and_is_private() -> None:
    vault = only(resources_of_type(module("key_vault"), "Microsoft.KeyVault/vaults"))
    properties = vault["properties"]
    assert properties["enableRbacAuthorization"] is True
    # Access policies would be a second, invisible authorisation system.
    assert "accessPolicies" not in properties
    assert properties["enableSoftDelete"] is True
    assert properties["enablePurgeProtection"] is True
    assert "publicNetworkAccessEnabled" in json.dumps(properties["publicNetworkAccess"])


def test_redis_is_the_current_managed_service_and_is_private() -> None:
    cluster = only(resources_of_type(module("redis"), "Microsoft.Cache/redisEnterprise"))
    assert cluster["properties"]["minimumTlsVersion"] == "1.2"
    database = only(resources_of_type(module("redis"), "Microsoft.Cache/redisEnterprise/databases"))
    assert database["properties"]["clientProtocol"] == "Encrypted"
    endpoints = resources_of_type(module("redis"), "Microsoft.Network/privateEndpoints")
    assert endpoints, "Azure Managed Redis must be reached through a private endpoint"


def test_every_privately_linked_service_has_an_endpoint_and_a_dns_zone_group() -> None:
    for name in ("storage", "key_vault", "container_registry", "redis"):
        compiled = module(name)
        endpoints = resources_of_type(compiled, "Microsoft.Network/privateEndpoints")
        groups = resources_of_type(
            compiled, "Microsoft.Network/privateEndpoints/privateDnsZoneGroups"
        )
        assert endpoints, f"{name} has no private endpoint"
        assert groups, f"{name} has a private endpoint with no DNS zone group"


def test_private_dns_zones_cover_every_linked_service() -> None:
    compiled = module("private_dns")
    zones = resources_of_type(compiled, "Microsoft.Network/privateDnsZones")
    links = resources_of_type(compiled, "Microsoft.Network/privateDnsZones/virtualNetworkLinks")
    # Both are copy loops over one list, so each is a single ARM resource entry.
    assert len(zones) == 1
    assert len(links) == 1
    # Every zone gets a virtual-network link; an unlinked zone resolves nothing.
    assert zones[0]["copy"]["count"] == links[0]["copy"]["count"]
    text = json.dumps(compiled)
    for suffix in (
        "privatelink.vaultcore.azure.net",
        "privatelink.azurecr.io",
        "privatelink.postgres.database.azure.com",
        "privatelink.redis.azure.net",
    ):
        assert suffix in text


def test_subnets_do_not_overlap() -> None:
    source = (MODULES_DIR / "networking.bicep").read_text()
    # Each subnet is carved from one address space with cidrSubnet(), so an
    # overlap is impossible by construction; assert the construction is used.
    assert source.count("cidrSubnet(addressPrefix") == 3
    vnet = only(resources_of_type(module("networking"), "Microsoft.Network/virtualNetworks"))
    subnets = vnet["properties"]["subnets"]
    assert [s["name"] for s in subnets] == [
        "snet-container-apps",
        "snet-private-endpoints",
        "snet-postgres",
    ]
    assert subnets[1]["properties"]["privateEndpointNetworkPolicies"] == "Disabled"


# -- identities and RBAC -------------------------------------------------------


def test_one_managed_identity_per_workload() -> None:
    identities = resources_of_type(
        module("identities"), "Microsoft.ManagedIdentity/userAssignedIdentities"
    )
    names = json.dumps([identity["name"] for identity in identities])
    for workload in ("api", "worker", "render", "migration", "smoke", "web"):
        assert f"-id-{workload}" in names
    assert len(identities) == 6


def test_no_role_assignment_grants_owner_or_contributor() -> None:
    source = (MODULES_DIR / "role_assignments.bicep").read_text()
    # Owner, Contributor and User Access Administrator role GUIDs.
    for forbidden in (
        "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
        "b24988ac-6180-42a0-ab88-20f7382dd24c",
        "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9",
    ):
        assert forbidden not in source


def test_role_assignment_names_are_deterministic() -> None:
    assignments = resources_of_type(
        module("role_assignments"), "Microsoft.Authorization/roleAssignments"
    )
    assert assignments
    for assignment in assignments:
        # guid(scope, principal, role) is stable, so a redeploy is idempotent.
        assert "guid(" in json.dumps(assignment["name"])


def test_workloads_hold_only_the_roles_they_need() -> None:
    source = (MODULES_DIR / "role_assignments.bicep").read_text()
    # The web tier holds no Key Vault or Storage role: it serves static files.
    secrets_block = source[source.index("var secretsUserPrincipals") :]
    secrets_block = secrets_block[: secrets_block.index("]")]
    assert "webPrincipalId" not in secrets_block
    blob_block = source[source.index("var blobContributorPrincipals") :]
    blob_block = blob_block[: blob_block.index("]")]
    assert "webPrincipalId" not in blob_block
    # The migration job touches only the database.
    assert "migrationPrincipalId" not in blob_block
    # Only the API mints user delegation SAS tokens.
    delegator = source[source.index("resource blobDelegator") :]
    delegator = delegator[: delegator.index("\n}")]
    assert "apiPrincipalId" in delegator


def test_no_workload_may_push_an_image_or_write_a_secret() -> None:
    source = (MODULES_DIR / "role_assignments.bicep").read_text()
    for forbidden_role_guid in (
        "8311e382-0749-4cb8-b61a-304f252e45ec",  # AcrPush
        "b86a8fe4-44ce-4948-aee5-eccb2c155cd7",  # Key Vault Secrets Officer
        "00482a5a-887f-4fb3-b363-3b7fe8e74483",  # Key Vault Administrator
    ):
        assert forbidden_role_guid not in source


# -- container apps ------------------------------------------------------------


def test_secrets_are_key_vault_references_resolved_by_managed_identity() -> None:
    compiled = module("container_apps")
    # The reference is built by one shared function, so checking it once checks
    # every workload that calls it.
    reference = user_defined_function(compiled, "secretReference")
    produced = reference["output"]["value"]
    # A Key Vault reference and nothing else: a literal secret would appear as a
    # "value" key on the object this function returns.
    assert set(produced) == {"name", "keyVaultUrl", "identity"}
    assert "secrets/" in produced["keyVaultUrl"]
    assert "vaultUri" in produced["keyVaultUrl"]

    apps = resources_of_type(compiled, "Microsoft.App/containerApps")
    for fragment, identity in (("-api", "apiIdentity"), ("-worker", "workerIdentity")):
        app = by_name(apps, fragment)
        secrets = property_text(app, "properties", "configuration", "secrets")
        assert "secretReference" in secrets
        # Resolved by the workload's own managed identity, not by a shared one.
        assert f"parameters('{identity}').id" in secrets
    web = by_name(apps, "-web")
    # The web tier holds no secret at all.
    assert web["properties"]["configuration"]["secrets"] == []


def test_no_workload_receives_a_literal_credential(compiled: dict[str, Any]) -> None:
    text = json.dumps(compiled)
    assert "AccountKey=" not in text
    assert "DefaultEndpointsProtocol" not in text
    assert not re.search(r"sk-[A-Za-z0-9]{20,}", text)


def test_api_has_liveness_and_readiness_probes() -> None:
    apps = resources_of_type(module("container_apps"), "Microsoft.App/containerApps")
    api = by_name(apps, "-api")
    probes = api["properties"]["template"]["containers"][0]["probes"]
    kinds = {probe["type"] for probe in probes}
    assert {"Liveness", "Readiness", "Startup"} <= kinds
    readiness = next(p for p in probes if p["type"] == "Readiness")
    assert readiness["httpGet"]["path"] == "/readyz"
    liveness = next(p for p in probes if p["type"] == "Liveness")
    # Liveness must not depend on the database: a blip should drain a replica,
    # not restart it.
    assert liveness["httpGet"]["path"] == "/healthz"


def test_web_has_a_health_probe_served_by_its_own_container() -> None:
    apps = resources_of_type(module("container_apps"), "Microsoft.App/containerApps")
    web = by_name(apps, "-web")
    probes = web["properties"]["template"]["containers"][0]["probes"]
    assert {p["type"] for p in probes} == {"Liveness", "Readiness"}
    assert all(p["httpGet"]["path"] == "/healthz" for p in probes)
    nginx = (REPO_ROOT / "apps" / "web" / "nginx.conf").read_text()
    assert "location = /healthz" in nginx


def test_every_container_declares_cpu_and_memory_limits() -> None:
    for name in ("container_apps", "container_jobs"):
        compiled = module(name)
        for resource_type in ("Microsoft.App/containerApps", "Microsoft.App/jobs"):
            for resource in resources_of_type(compiled, resource_type):
                for container in resource["properties"]["template"]["containers"]:
                    assert "cpu" in container["resources"]
                    assert "memory" in container["resources"]


def test_api_scales_on_http_concurrency_within_bounds() -> None:
    apps = resources_of_type(module("container_apps"), "Microsoft.App/containerApps")
    api = by_name(apps, "-api")
    scale = api["properties"]["template"]["scale"]
    assert scale["minReplicas"] == "[parameters('apiScale').minReplicas]"
    assert scale["maxReplicas"] == "[parameters('apiScale').maxReplicas]"
    assert scale["rules"][0]["http"]["metadata"]["concurrentRequests"]


def test_the_worker_never_scales_to_zero(staging_parameters: dict[str, Any]) -> None:
    worker_scale = staging_parameters["parameters"]["workerScale"]["value"]
    assert worker_scale["minReplicas"] >= 1
    assert worker_scale["maxReplicas"] >= worker_scale["minReplicas"]
    api_scale = staging_parameters["parameters"]["apiScale"]["value"]
    assert api_scale["minReplicas"] >= 1
    web_scale = staging_parameters["parameters"]["webScale"]["value"]
    assert web_scale["minReplicas"] >= 1


def test_the_worker_shuts_down_gracefully() -> None:
    apps = resources_of_type(module("container_apps"), "Microsoft.App/containerApps")
    worker = by_name(apps, "-worker")
    template = worker["properties"]["template"]
    grace = json.dumps(template["terminationGracePeriodSeconds"])
    assert "gracefulShutdownSeconds" in grace
    # The worker's environment is assembled from the shared temporalEnv list.
    compiled = module("container_apps")
    assert "workerTemporalEnv" in json.dumps(template["containers"])
    definition = json.dumps(compiled["variables"]["workerTemporalEnv"]) + json.dumps(
        user_defined_function(compiled, "temporalEnv")
    )
    for name in (
        "TEMPORAL_GRACEFUL_SHUTDOWN_SECONDS",
        "TEMPORAL_MAX_CONCURRENT_ACTIVITIES",
        "TEMPORAL_MAX_CONCURRENT_WORKFLOW_TASKS",
    ):
        assert name in definition
    source = (REPO_ROOT / "workers" / "temporal_worker" / "main.py").read_text()
    # Both workers - the project queue and the T17b render queue - are bounded.
    assert source.count("graceful_shutdown_timeout") == 2
    # An `async with` over the workers is what performs the bounded graceful
    # shutdown; Worker.run() takes no shutdown argument. A render left out of
    # that block would be killed mid-encode instead of drained.
    assert "async with worker, render_worker:" in source
    assert "shutdown_event" not in source
    assert "signal.SIGTERM" in source


# -- jobs ----------------------------------------------------------------------


def test_migration_job_runs_exactly_once() -> None:
    jobs = resources_of_type(module("container_jobs"), "Microsoft.App/jobs")
    migration = by_name(jobs, "job-migrate")
    configuration = migration["properties"]["configuration"]
    assert configuration["triggerType"] == "Manual"
    assert configuration["manualTriggerConfig"]["parallelism"] == 1
    assert configuration["manualTriggerConfig"]["replicaCompletionCount"] == 1
    # A retried migration is a retried partially applied transaction.
    assert configuration["replicaRetryLimit"] == 0
    assert configuration["replicaTimeout"]


def test_migration_job_upgrades_and_never_downgrades() -> None:
    jobs = resources_of_type(module("container_jobs"), "Microsoft.App/jobs")
    migration = by_name(jobs, "job-migrate")
    args = migration["properties"]["template"]["containers"][0]["args"]
    assert args == ["-m", "scripts.run_migrations", "--upgrade-head"]
    # Ignore prose: assert no *executable* downgrade path exists.
    source = (REPO_ROOT / "scripts" / "run_migrations.py").read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", "*"))
    )
    code = code.split('"""', 2)[-1]
    assert "command.downgrade" not in code
    assert '"downgrade"' not in code
    assert 'command.upgrade(config, "head")' in code
    state = (REPO_ROOT / "src" / "vidgen" / "db" / "alembic_state.py").read_text()
    assert "command." not in state, "the read-only helper must not be able to apply anything"


def test_migration_job_uses_its_own_database_role() -> None:
    jobs = resources_of_type(module("container_jobs"), "Microsoft.App/jobs")
    compiled = module("container_jobs")
    jobs = resources_of_type(compiled, "Microsoft.App/jobs")
    migration = by_name(jobs, "job-migrate")
    secrets = property_text(migration, "properties", "configuration", "secrets")
    assert "variables('migrationSecretNames')" in secrets

    # The variable is built from the migration role's secret and no other.
    migration_names = json.dumps(compiled["variables"]["migrationSecretNames"])
    assert "postgres-migration-url" in migration_names
    assert "postgres-app-url" not in migration_names

    # Every other job connects as the least-privileged application role.
    for fragment in ("job-render", "job-smoke", "job-admin"):
        other = by_name(jobs, fragment)
        other_secrets = property_text(other, "properties", "configuration", "secrets")
        assert "migrationSecretNames" not in other_secrets


def test_render_job_executes_a_render_rather_than_queueing_one() -> None:
    """The Container Apps Job must perform the render, not insert a queue row.

    T24 originally pointed this job at a command that created a render-job row
    and exited, which made a green job execution mean nothing about whether a
    render happened. The job now runs the T17b worker against an existing render
    job id supplied per execution, so its exit status is the render result.
    """
    jobs = resources_of_type(module("container_jobs"), "Microsoft.App/jobs")
    render = by_name(jobs, "job-render")
    container = render["properties"]["template"]["containers"][0]
    assert container["command"] == ["python"]
    assert container["args"] == ["-m", "workers.render_job.main", "--from-env"]

    # `--from-env` reads exactly one variable, and it is supplied per execution
    # rather than baked into the deployed definition.
    worker = (REPO_ROOT / "workers" / "render_job" / "main.py").read_text()
    assert "VIDGEN_RENDER_JOB_ID" in worker
    environment = property_text(render["properties"]["template"]["containers"][0], "env")
    assert "VIDGEN_RENDER_JOB_ID" not in environment
    # Rendering needs writable scratch, and it must be the TMPDIR the image
    # creates for the non-root runtime user.
    assert "VIDGEN_RENDER_WORK_ROOT" in environment
    assert "/tmp/vidgen/render" in environment


def test_render_job_is_resourced_for_ffmpeg(
    staging_parameters: dict[str, Any], production_parameters: dict[str, Any]
) -> None:
    """FFmpeg is CPU and memory bound; a starved replica is a failed render."""
    for parameters in (staging_parameters, production_parameters):
        limits = parameters["parameters"]["renderLimits"]["value"]
        assert float(limits["resources"]["cpu"]) >= 2.0
        assert int(limits["resources"]["memory"].removesuffix("Gi")) >= 4
        # A long encode must not be killed mid-render by the replica timeout.
        assert limits["timeoutSeconds"] >= 3600
        # Platform retry is bounded: the executor is idempotent, but an
        # unbounded retry of a deterministic failure is just wasted compute.
        assert 0 <= limits["retryLimit"] <= 2


def test_render_concurrency_is_bounded(
    staging_parameters: dict[str, Any], production_parameters: dict[str, Any]
) -> None:
    for parameters in (staging_parameters, production_parameters):
        limits = parameters["parameters"]["renderLimits"]["value"]
        assert 1 <= limits["parallelism"] <= 8
        assert limits["timeoutSeconds"] > 0


def test_every_job_is_bounded() -> None:
    for job in resources_of_type(module("container_jobs"), "Microsoft.App/jobs"):
        configuration = job["properties"]["configuration"]
        assert configuration["replicaTimeout"]
        assert "replicaRetryLimit" in configuration
        assert configuration["manualTriggerConfig"]["parallelism"]
        assert configuration["manualTriggerConfig"]["replicaCompletionCount"]


def test_the_smoke_test_job_cannot_call_a_paid_provider() -> None:
    source = (MODULES_DIR / "container_jobs.bicep").read_text()
    block = source[source.index("var smokeProviders ProviderToggles = {") :]
    block = block[: block.index("}")]
    for line in block.splitlines()[1:]:
        if ":" in line:
            assert line.strip().endswith("false"), line
    jobs = resources_of_type(module("container_jobs"), "Microsoft.App/jobs")
    smoke = by_name(jobs, "job-smoke")
    secrets = property_text(smoke, "properties", "configuration", "secrets")
    container = json.dumps(smoke["properties"]["template"]["containers"])
    for provider_secret in (
        "openai-api-key",
        "runway-api-secret",
        "elevenlabs-api-key",
        "google-credentials-json",
        "voicestudio-api-key",
    ):
        assert provider_secret not in secrets
        assert provider_secret not in container


def test_the_smoke_test_runs_inside_the_private_environment() -> None:
    jobs = resources_of_type(module("container_jobs"), "Microsoft.App/jobs")
    smoke = by_name(jobs, "job-smoke")
    assert smoke["properties"]["environmentId"] == "[parameters('environmentId')]"
    container = json.dumps(smoke["properties"]["template"]["containers"])
    # It is pointed at the internal ingress FQDN and the private endpoints,
    # which only resolve inside the virtual network.
    for name in (
        "VIDGEN_SMOKE_API_URL",
        "VIDGEN_SMOKE_WEB_URL",
        "VIDGEN_SMOKE_POSTGRES_HOST",
        "VIDGEN_SMOKE_REDIS_HOST",
        "VIDGEN_SMOKE_KEY_VAULT_NAME",
    ):
        assert name in container
    assert smoke["properties"]["template"]["containers"][0]["args"] == [
        "-m",
        "scripts.staging_smoke_test",
    ]


# -- diagnostics ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "postgres",
        "storage",
        "key_vault",
        "redis",
        "container_registry",
        "container_apps_environment",
    ],
)
def test_diagnostic_settings_are_enabled(name: str) -> None:
    settings = resources_of_type(module(name), "Microsoft.Insights/diagnosticSettings")
    assert settings, f"{name} has no diagnostic setting"
    for setting in settings:
        assert setting["properties"]["workspaceId"]
        assert setting["properties"]["logs"]


def test_log_retention_is_configurable_and_capped() -> None:
    workspace = only(
        resources_of_type(module("monitoring"), "Microsoft.OperationalInsights/workspaces")
    )
    assert workspace["properties"]["retentionInDays"] == "[parameters('retentionInDays')]"
    assert workspace["properties"]["workspaceCapping"]["dailyQuotaGb"]


def test_application_insights_is_workspace_based() -> None:
    component = only(resources_of_type(module("monitoring"), "Microsoft.Insights/components"))
    assert component["properties"]["WorkspaceResourceId"]
    assert component["properties"]["IngestionMode"] == "LogAnalytics"


# -- parameter files -----------------------------------------------------------


def test_parameter_files_contain_no_secret_and_no_subscription_identity() -> None:
    for path in sorted(ENVIRONMENTS_DIR.glob("*.bicepparam")):
        source = path.read_text()
        assert "sk-" not in source
        assert "AccountKey=" not in source
        assert "BEGIN " not in source
        # No subscription id, tenant id, resource group or public hostname.
        assert not re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", source
        )
        assert "subscriptions/" not in source
        assert "azurecontainerapps.io" not in source


def test_the_region_is_never_hardcoded() -> None:
    for path in sorted(ENVIRONMENTS_DIR.glob("*.bicepparam")):
        source = path.read_text()
        assert "readEnvironmentVariable('VIDGEN_AZURE_LOCATION')" in source
    main_source = (BICEP_DIR / "main.bicep").read_text()
    assert "param location string\n" in main_source, "location must have no default"


def test_optional_providers_are_off_in_staging(staging_parameters: dict[str, Any]) -> None:
    providers = staging_parameters["parameters"]["providers"]["value"]
    assert providers == {
        "openai": False,
        "runway": False,
        "veo": False,
        "elevenlabs": False,
        "voicestudio": False,
        "opensubtitles": False,
    }
    features = staging_parameters["parameters"]["features"]["value"]
    # A disabled Veo provider must not leave the repair route pointing at it.
    assert features["repairAlternateProvider"] == "none"


def test_production_template_stays_private_and_veo_stays_disabled(
    production_parameters: dict[str, Any],
) -> None:
    assert production_parameters["parameters"]["publicIngressEnabled"]["value"] is False
    providers = production_parameters["parameters"]["providers"]["value"]
    assert providers["veo"] is False
    features = production_parameters["parameters"]["features"]["value"]
    assert features["repairAlternateProvider"] == "none"


def test_production_and_staging_networks_do_not_overlap(
    staging_parameters: dict[str, Any], production_parameters: dict[str, Any]
) -> None:
    assert (
        staging_parameters["parameters"]["vnetAddressPrefix"]["value"]
        != production_parameters["parameters"]["vnetAddressPrefix"]["value"]
    )


def test_replica_ceilings_fit_the_postgres_connection_budget(
    staging_parameters: dict[str, Any],
) -> None:
    """Every ceiling together must stay inside max_connections with headroom.

    SQLAlchemy's default pool is 5 connections with 10 overflow, so 15 per
    process is the worst case. The assertion is what keeps a scaling change
    from quietly exhausting the server.
    """
    parameters = staging_parameters["parameters"]
    worst_case_per_process = 15
    processes = (
        parameters["apiScale"]["value"]["maxReplicas"]
        + parameters["workerScale"]["value"]["maxReplicas"]
        + parameters["renderLimits"]["value"]["parallelism"]
        + parameters["migrationLimits"]["value"]["parallelism"]
        + parameters["smokeLimits"]["value"]["parallelism"]
    )
    budget = parameters["postgresMaxConnections"]["value"]
    # Azure reserves a handful of connections for its own monitoring.
    assert processes * worst_case_per_process <= budget - 10


# -- regressions -----------------------------------------------------------------


def test_every_workload_can_authenticate_to_temporal_cloud() -> None:
    """The API starts and queries workflows and the smoke test proves the
    endpoint is reachable, so the connection cannot live only on the worker."""
    compiled = module("container_apps")
    shared = json.dumps(user_defined_function(compiled, "commonEnv"))
    for name in ("TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE", "TEMPORAL_TLS_ENABLED"):
        assert name in shared
    # The API key is a secret reference, never a literal.
    assert "temporal-api-key" in shared
    assert "TEMPORAL_API_KEY" in shared
    assert "VIDGEN_TEMPORAL_API_KEY" in shared
    # Every workload mounts the secret the reference resolves.
    for name in ("container_apps", "container_jobs"):
        assert "temporal-api-key" in json.dumps(
            user_defined_function(module(name), "applicationSecretNames")
        )


def test_the_worker_is_told_which_providers_it_may_fake(
    staging_parameters: dict[str, Any], production_parameters: dict[str, Any]
) -> None:
    """The worker refuses to start with an unconfigured provider, so an
    all-providers-off environment must say so explicitly."""
    shared = json.dumps(user_defined_function(module("container_apps"), "commonEnv"))
    assert "VIDGEN_TEMPORAL_ALLOW_FAKE_PROVIDERS" in shared

    staging_providers = staging_parameters["parameters"]["providers"]["value"]
    staging_features = staging_parameters["parameters"]["features"]["value"]
    assert not any(staging_providers.values())
    assert staging_features["allowFakeProviders"] is True

    # The invariant that matters: fakes are never allowed where a paid provider
    # is enabled, or a missing credential would silently produce fake output.
    production_providers = production_parameters["parameters"]["providers"]["value"]
    production_features = production_parameters["parameters"]["features"]["value"]
    if any(production_providers.values()):
        assert production_features["allowFakeProviders"] is False


def test_internal_ingress_is_addressed_over_https() -> None:
    """Both ingresses set allowInsecure:false, so a plaintext request is
    answered with a redirect that neither the smoke client nor a proxy_pass
    follows."""
    apps = resources_of_type(module("container_apps"), "Microsoft.App/containerApps")
    web = by_name(apps, "-web")
    upstream = json.dumps(web["properties"]["template"]["containers"])
    assert "https://" in upstream
    assert "'http://'" not in upstream

    jobs = resources_of_type(module("container_jobs"), "Microsoft.App/jobs")
    smoke = json.dumps(by_name(jobs, "job-smoke")["properties"]["template"]["containers"])
    assert "https://" in smoke
    assert "http://" not in smoke.replace("https://", "")

    nginx = (REPO_ROOT / "apps" / "web" / "nginx.conf").read_text()
    # Container Apps routes an internal request by Host and terminates TLS with
    # a certificate for that name, so the browser's host must not be forwarded.
    assert "proxy_set_header Host $proxy_host;" in nginx
    assert "proxy_ssl_server_name on;" in nginx


def test_multi_replica_uploads_keep_their_parts_on_one_replica() -> None:
    """Resumable upload parts are staged on local disk, so the part PUT and the
    completing POST have to reach the same replica."""
    apps = resources_of_type(module("container_apps"), "Microsoft.App/containerApps")
    api = by_name(apps, "-api")
    ingress = api["properties"]["configuration"]["ingress"]
    assert ingress["stickySessions"]["affinity"] == "sticky"


def test_the_deny_all_outbound_rule_spares_the_platform_dns_service() -> None:
    """168.63.129.16 is AzurePlatformDNS, not AzureCloud and not Internet.
    Without an explicit allow, the deny below it breaks every private endpoint
    name lookup in the environment."""
    nsg = by_name(
        resources_of_type(module("networking"), "Microsoft.Network/networkSecurityGroups"),
        "nsg-apps",
    )
    rules = {rule["name"]: rule["properties"] for rule in nsg["properties"]["securityRules"]}
    platform = rules["AllowOutboundAzurePlatformDns"]
    assert platform["destinationAddressPrefix"] == "AzurePlatformDNS"
    assert platform["access"] == "Allow"
    deny = rules["DenyOtherInternetOutbound"]
    assert platform["priority"] < deny["priority"]


def test_image_immutability_is_enforced_where_it_can_stop_a_deployment() -> None:
    """Bicep cannot fail on a computed condition, so the check has to live in
    the script that runs before anything reaches Azure."""
    common = (REPO_ROOT / "infra" / "scripts" / "_common.sh").read_text()
    assert "assert_immutable_image()" in common
    assert "refusing to deploy the 'latest' tag" in common
    deploy = (REPO_ROOT / "infra" / "scripts" / "deploy_staging.sh").read_text()
    assert 'assert_immutable_image "${VIDGEN_APP_IMAGE}"' in deploy
    assert 'assert_immutable_image "${VIDGEN_WEB_IMAGE}"' in deploy


def test_the_smoke_mode_override_cannot_drop_the_secret_environment() -> None:
    """`--env-vars` would replace the whole environment list and unmount every
    Key Vault reference; only the arguments may be overridden."""
    script = (REPO_ROOT / "infra" / "scripts" / "run_smoke_test.sh").read_text()
    # Comments are allowed to explain why; the executable lines are the claim.
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert "--env-vars" not in code
    assert "--args" in code


def test_the_deploy_workflow_makes_no_key_vault_data_plane_call_from_a_runner() -> None:
    """The vault has public network access disabled, so a hosted runner would
    get a 403 and report every secret as missing."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "deploy-staging.yml").read_text()
    code = "\n".join(line for line in workflow.splitlines() if not line.lstrip().startswith("#"))
    assert "bootstrap_secrets.sh" not in code
    assert "az keyvault" not in code
