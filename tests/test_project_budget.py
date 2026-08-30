"""Project budget setup for a real-provider workflow.

Every paid stage reserves against the T23 ``ProjectBudget`` before it calls a
provider. Project creation never wrote that row, so a real-provider run reached
its first paid activity and failed with ``project budget not configured``. These
tests cover the row being created with the project, the amounts surviving as
exact decimals, the validation an owner can hit, and the two ends that consume
it: ``workflow:start`` and the existing reservation/reconciliation path.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.dependencies import (
    get_blob_store,
    get_session,
    get_session_factory,
    get_workflow_controller,
)
from apps.api.main import create_app
from apps.api.settings import APISettings, get_settings
from services.costs.project_budget import (
    BUDGET_CURRENCY,
    BUDGET_POLICY_VERSION,
    BudgetDeployment,
    BudgetError,
    create_budget,
    parse_amount,
    startable,
    validate_caps,
)
from vidgen.contracts.costs import CostReservationRequest
from vidgen.db.base import Base
from vidgen.db.cost_models import ProjectBudget, ProviderAttempt
from vidgen.db.cost_repository import CostRepository
from vidgen.db.models import Project
from vidgen.review.workflow_control import FakeWorkflowController
from vidgen.storage.blob import FilesystemBlobStore

OWNER = {"X-VidGen-User": "owner-a"}

BudgetClient = tuple[TestClient, sessionmaker[Session]]

FAKE_DEPLOYMENT = BudgetDeployment(paid_provider_configured=False)
PAID_DEPLOYMENT = BudgetDeployment(paid_provider_configured=True)


@contextmanager
def budget_client(tmp_path: Path, *, paid: bool) -> Iterator[BudgetClient]:
    """A control-plane client whose deployment does or does not spend money.

    ``paid`` is expressed the way the worker itself decides: a configured
    provider credential, not a feature flag.
    """
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'budget.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    blob_store = FilesystemBlobStore(tmp_path / "blobs", b"test-secret")
    settings = APISettings(
        database_url=str(engine.url),
        blob_root=tmp_path / "blobs",
        upload_root=tmp_path / "uploads",
        signing_secret="test-secret",
        openai_api_key="sk-test" if paid else None,
        temporal_allow_fake_providers=not paid,
    )
    app = create_app()

    def session_override() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_blob_store] = lambda: blob_store
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_controller] = lambda: FakeWorkflowController()
    with TestClient(app) as client:
        yield client, factory


@pytest.fixture
def fake_client(tmp_path: Path) -> Iterator[BudgetClient]:
    with budget_client(tmp_path, paid=False) as client:
        yield client


@pytest.fixture
def paid_client(tmp_path: Path) -> Iterator[BudgetClient]:
    with budget_client(tmp_path, paid=True) as client:
        yield client


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'unit.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as opened:
        yield opened


def make_project(session: Session) -> Project:
    project = Project(
        name="Budgeted",
        owner_subject="owner-a",
        status="awaiting_upload",
        target_duration_seconds=300,
        visual_style="flat editorial cartoon",
        humor_intensity=5,
        settings={},
    )
    session.add(project)
    session.commit()
    return project


def create(client: TestClient, **overrides: object) -> object:
    payload: dict[str, object] = {
        "name": "Budgeted",
        "visual_style": "flat editorial cartoon",
        "humor_intensity": 5,
    }
    payload.update(overrides)
    return client.post("/api/v1/projects", json=payload, headers=OWNER)


# --- the budget row is created with the project ------------------------------


def test_project_and_budget_are_created_in_one_transaction(fake_client: BudgetClient) -> None:
    client, factory = fake_client
    response = create(client, budget_warning_cap="5.00", budget_hard_cap="20.00")
    assert response.status_code == 201, response.json()
    project_id = UUID(response.json()["id"])

    with factory() as session:
        budget = session.scalar(select(ProjectBudget).where(ProjectBudget.project_id == project_id))
        assert budget is not None
        assert budget.currency == BUDGET_CURRENCY == "USD"
        assert budget.policy_version == BUDGET_POLICY_VERSION
        assert budget.reserved_amount == Decimal("0")
        assert budget.committed_amount == Decimal("0")
        assert budget.released_amount == Decimal("0")
        assert budget.row_version == 1


def test_a_rejected_budget_leaves_no_project_behind(paid_client: BudgetClient) -> None:
    """The pair is transactional: a refused budget must not orphan a project."""
    client, factory = paid_client
    refused = create(client, budget_warning_cap="0", budget_hard_cap="0")
    assert refused.status_code == 422
    with factory() as session:
        assert session.scalars(select(Project)).all() == []
        assert session.scalars(select(ProjectBudget)).all() == []


def test_caps_survive_as_exact_decimals(fake_client: BudgetClient) -> None:
    """0.1 + 0.2 is the reason every amount here is a string end to end."""
    client, factory = fake_client
    response = create(client, budget_warning_cap="0.100000", budget_hard_cap="12.3456789")
    assert response.status_code == 422, "more than six decimal places is refused"

    response = create(client, budget_warning_cap="0.100000", budget_hard_cap="12.345678")
    assert response.status_code == 201
    with factory() as session:
        budget = session.scalar(select(ProjectBudget))
        assert budget is not None
        assert budget.hard_cap == Decimal("12.345678")
        assert budget.warning_cap == Decimal("0.100000")


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("warning", "hard", "field"),
    [
        ("-1.00", "10.00", "budget_warning_cap"),
        ("1.00", "-10.00", "budget_hard_cap"),
        ("10.00", "1.00", "budget_hard_cap"),
        ("not-a-number", "10.00", "budget_warning_cap"),
        ("1.00", "", "budget_hard_cap"),
        ("1.00", "NaN", "budget_hard_cap"),
        ("1.00", "Infinity", "budget_hard_cap"),
    ],
)
def test_invalid_caps_return_a_structured_validation_error(
    fake_client: BudgetClient, warning: str, hard: str, field: str
) -> None:
    client, _ = fake_client
    response = create(client, budget_warning_cap=warning, budget_hard_cap=hard)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["summary"]
    assert [item["field"] for item in body["fields"]] == [field]
    assert body["fields"][0]["code"].startswith("budget_")


def test_a_json_number_is_refused_rather_than_rounded(fake_client: BudgetClient) -> None:
    """A float has already lost the amount by the time the route sees it."""
    client, _ = fake_client
    response = create(client, budget_warning_cap="0", budget_hard_cap=20.10)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert any(item["field"] == "budget_hard_cap" for item in body["fields"])


def test_a_paid_deployment_requires_a_positive_hard_cap(paid_client: BudgetClient) -> None:
    client, _ = paid_client
    refused = create(client, budget_warning_cap="0", budget_hard_cap="0")
    assert refused.status_code == 422
    assert refused.json()["fields"][0]["code"] == "budget_hard_cap_required"
    assert create(client, budget_hard_cap="25.00").status_code == 201


def test_a_fake_deployment_accepts_a_zero_dollar_budget(fake_client: BudgetClient) -> None:
    client, factory = fake_client
    assert create(client).status_code == 201
    with factory() as session:
        budget = session.scalar(select(ProjectBudget))
        assert budget is not None and budget.hard_cap == Decimal("0")


def test_amount_parsing_rules(session: Session) -> None:
    assert parse_amount("25.00", field="x") == Decimal("25.00")
    assert parse_amount(25, field="x") == Decimal("25")
    assert parse_amount(Decimal("1.5"), field="x") == Decimal("1.5")
    for bad in (25.5, True, "1.0000001", "-0.01", "abc", None, "1" * 19):
        with pytest.raises(BudgetError):
            parse_amount(bad, field="x")


def test_validate_caps_orders_its_complaints(session: Session) -> None:
    with pytest.raises(BudgetError) as negative:
        validate_caps("-1", "5", FAKE_DEPLOYMENT)
    assert negative.value.field == "budget_warning_cap"
    with pytest.raises(BudgetError) as ordering:
        validate_caps("5", "1", FAKE_DEPLOYMENT)
    assert ordering.value.code == "budget_hard_cap_below_warning"
    assert validate_caps("5", "5", PAID_DEPLOYMENT).hard_cap == Decimal("5")


def test_a_second_budget_is_refused(session: Session) -> None:
    project = make_project(session)
    create_budget(session, project, warning_cap="1", hard_cap="2", deployment=FAKE_DEPLOYMENT)
    with pytest.raises(BudgetError, match="already has a budget"):
        create_budget(session, project, warning_cap="1", hard_cap="2", deployment=FAKE_DEPLOYMENT)


# --- workflow:start -----------------------------------------------------------


def test_startable_rules() -> None:
    exhausted = ProjectBudget(
        project_id=uuid4(),
        warning_cap=Decimal("5"),
        hard_cap=Decimal("10"),
        currency="USD",
        policy_version=BUDGET_POLICY_VERSION,
        reserved_amount=Decimal("4"),
        committed_amount=Decimal("6"),
        released_amount=Decimal("0"),
        row_version=1,
    )
    assert startable(None, FAKE_DEPLOYMENT) is True
    assert startable(None, PAID_DEPLOYMENT) is False
    assert startable(exhausted, FAKE_DEPLOYMENT) is True
    assert startable(exhausted, PAID_DEPLOYMENT) is False
    funded = ProjectBudget(
        project_id=uuid4(),
        warning_cap=Decimal("5"),
        hard_cap=Decimal("10"),
        currency="USD",
        policy_version=BUDGET_POLICY_VERSION,
        reserved_amount=Decimal("0"),
        committed_amount=Decimal("0"),
        released_amount=Decimal("0"),
        row_version=1,
    )
    assert startable(funded, PAID_DEPLOYMENT) is True


def test_a_real_workflow_is_refused_without_a_positive_budget(
    paid_client: BudgetClient, golden_video: Path
) -> None:
    """The refusal happens before Temporal, not inside a paid activity."""
    client, factory = paid_client
    project_id = _uploaded_project(client, golden_video, hard_cap="25.00")

    # Drop the ceiling to zero the way an exhausted or unfunded project looks.
    with factory() as session:
        budget = session.scalar(
            select(ProjectBudget).where(ProjectBudget.project_id == UUID(project_id))
        )
        assert budget is not None
        budget.hard_cap = Decimal("0")
        budget.warning_cap = Decimal("0")
        session.commit()

    refused = client.post(
        f"/api/v1/projects/{project_id}/workflow:start",
        json={},
        headers={**OWNER, "Idempotency-Key": "start-unfunded"},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "project_budget_required"
    assert "hard cap" in refused.json()["summary"]


def test_a_funded_real_workflow_starts(paid_client: BudgetClient, golden_video: Path) -> None:
    client, _ = paid_client
    project_id = _uploaded_project(client, golden_video, hard_cap="25.00")
    started = client.post(
        f"/api/v1/projects/{project_id}/workflow:start",
        json={},
        headers={**OWNER, "Idempotency-Key": "start-funded"},
    )
    assert started.status_code == 200, started.json()


def test_a_fake_zero_cost_workflow_still_starts(
    fake_client: BudgetClient, golden_video: Path
) -> None:
    """A fake-provider project spends nothing, so a zero-dollar budget is fine."""
    client, factory = fake_client
    project_id = _uploaded_project(client, golden_video, hard_cap="0")
    with factory() as session:
        budget = session.scalar(
            select(ProjectBudget).where(ProjectBudget.project_id == UUID(project_id))
        )
        assert budget is not None and budget.hard_cap == Decimal("0")
    started = client.post(
        f"/api/v1/projects/{project_id}/workflow:start",
        json={},
        headers={**OWNER, "Idempotency-Key": "start-fake"},
    )
    assert started.status_code == 200, started.json()


# --- the existing T23 ledger keeps working over the created row ---------------


def test_the_created_budget_reserves_and_reconciles(session: Session) -> None:
    """T23 is untouched: the row this module writes is the row it already uses."""
    project = make_project(session)
    budget = create_budget(
        session, project, warning_cap="5.00", hard_cap="10.00", deployment=PAID_DEPLOYMENT
    )
    attempt = ProviderAttempt(
        project_id=project.id,
        operation="image_generation",
        attempt_number=1,
        input_hash="a" * 64,
        idempotency_key="attempt-1",
        provider="openai",
        model="gpt-image-2-2026-04-21",
        provider_configuration_version="v1",
        currency="USD",
        usage={},
        status="SUCCEEDED",
        started_at=datetime.now(UTC),
    )
    session.add(attempt)
    session.flush()

    costs = CostRepository(session)
    reservation = costs.reserve(
        CostReservationRequest(
            project_id=project.id,
            provider_attempt_id=attempt.id,
            idempotency_key="reserve-1",
            estimated_amount=Decimal("6.00"),
            currency="USD",
        )
    )
    assert reservation.decision == "ALLOW_WITH_WARNING"
    assert reservation.reservation_id is not None
    session.refresh(budget)
    assert budget.reserved_amount == Decimal("6.00")

    costs.reconcile(reservation.reservation_id, "ledger-1", Decimal("4.25"))
    session.refresh(budget)
    assert budget.reserved_amount == Decimal("0")
    assert budget.committed_amount == Decimal("4.25")
    assert budget.released_amount == Decimal("1.75")

    denied = costs.reserve(
        CostReservationRequest(
            project_id=project.id,
            provider_attempt_id=attempt.id,
            idempotency_key="reserve-2",
            estimated_amount=Decimal("100.00"),
            currency="USD",
        )
    )
    assert denied.decision == "DENY_HARD_CAP"


def _uploaded_project(client: TestClient, golden_video: Path, *, hard_cap: str) -> str:
    """Create a project with a budget, a voice and a finalized source upload."""
    import hashlib

    created = create(client, budget_warning_cap="0", budget_hard_cap=hard_cap)
    assert created.status_code == 201, created.json()
    project_id = str(created.json()["id"])

    catalog = client.get(f"/api/v1/projects/{project_id}/voice-profiles", headers=OWNER).json()
    option = catalog["items"][0]
    assert (
        client.put(
            f"/api/v1/projects/{project_id}/voice-profile",
            json={"voice_profile_id": option["voice_profile_id"]},
            headers=OWNER,
        ).status_code
        == 200
    )

    content = golden_video.read_bytes()
    upload = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        json={
            "filename": "golden.mp4",
            "media_type": "video/mp4",
            "expected_size": len(content),
            "expected_sha256": hashlib.sha256(content).hexdigest(),
            "part_size": 8 * 1024 * 1024,
        },
        headers=OWNER,
    ).json()
    assert (
        client.put(
            f"/api/v1/uploads/{upload['id']}/parts/0", content=content, headers=OWNER
        ).status_code
        == 200
    )
    assert client.post(f"/api/v1/uploads/{upload['id']}/complete", headers=OWNER).status_code == 200
    return project_id
