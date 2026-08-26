from __future__ import annotations

import argparse
import json
from uuid import UUID

from sqlalchemy import select

from apps.api.auth import Principal
from apps.api.routes.costs import costs
from vidgen.db.cost_models import ProviderAttempt
from vidgen.db.session import build_engine, session_factory


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    with session_factory(build_engine())() as session:
        report = costs(
            args.project_id,
            session,
            Principal(
                subject=session.execute(
                    select(
                        __import__("vidgen.db.models", fromlist=["Project"]).Project.owner_subject
                    ).where(
                        __import__("vidgen.db.models", fromlist=["Project"]).Project.id
                        == args.project_id
                    )
                ).scalar_one()
            ),
        )
        attempts = session.scalars(
            select(ProviderAttempt)
            .where(ProviderAttempt.project_id == args.project_id)
            .order_by(ProviderAttempt.started_at.desc())
            .limit(10)
        ).all()
        report["recentAttempts"] = [
            {
                "provider": r.provider,
                "model": r.model,
                "operation": r.operation,
                "latencyMs": r.latency_ms,
                "failureClass": r.failure_class,
            }
            for r in attempts
        ]
    if args.json:
        print(json.dumps(report, default=str, sort_keys=True))
    else:
        print(
            f"Project {report['projectId']}\n"
            f"Warning/hard caps: {report['warningCap']} / {report['hardCap']}\n"
            "Reserved/committed/released/remaining: "
            f"{report['reservedAmount']} / {report['committedAmount']} / "
            f"{report['releasedAmount']} / {report['remainingAmount']}"
        )
        for key in ("byProvider", "byModel", "byOperation", "byReason"):
            print(f"{key}: {report[key]}")


if __name__ == "__main__":
    main()
