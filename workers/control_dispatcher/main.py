"""The durable control-command dispatcher.

    uv run python -m workers.control_dispatcher.main            # poll until stopped
    uv run python -m workers.control_dispatcher.main --once     # one bounded pass

This is the process that makes every ``202 Accepted`` in the API true. It claims
pending control commands under a lease, starts or signals the real Temporal
workflow each one names, writes that workflow's actual identity back, and later
settles the command from the workflow's own durable state.

It is deliberately a small, bounded, restartable poller:

* concurrency is the batch size, so it cannot start an unbounded number of
  workflows or hold an unbounded number of database connections;
* every claim is a lease, so a killed replica strands nothing - the next one
  recovers the command once the lease expires;
* SIGTERM and SIGINT finish the current pass and exit, which is what a Container
  Apps stop signal should mean for a worker that is mid-dispatch.

Exit codes: ``0`` for a clean stop, ``2`` for a usage error.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from types import FrameType

from sqlalchemy.orm import sessionmaker

from apps.api.settings import APISettings, get_settings
from services.control_plane.dispatcher import ControlCommandDispatcher, default_dispatcher_id
from vidgen.db.session import build_engine
from vidgen.review.workflow_control import (
    FakeWorkflowController,
    TemporalWorkflowController,
    WorkflowController,
)
from vidgen.telemetry.bootstrap import initialize_telemetry

logger = logging.getLogger("vidgen.control_dispatcher")

EXIT_OK = 0
EXIT_USAGE = 2


class _Shutdown:
    """Cooperative stop: the current pass finishes, the next one does not start."""

    def __init__(self) -> None:
        self.requested = False

    def request(self, _signum: int, _frame: FrameType | None) -> None:
        self.requested = True

    def __call__(self) -> bool:
        return self.requested


def build_controller(settings: APISettings) -> WorkflowController:
    """The real Temporal controller, unless the deployment has none configured.

    A local run without a Temporal endpoint gets the deterministic in-memory
    controller so the dispatcher can still be exercised end to end - it just
    cannot start a real workflow, and says so by never leaving ``running``.
    """
    host = os.getenv("TEMPORAL_ADDRESS") or settings.temporal_target_host
    if not host:
        logger.warning("no Temporal endpoint configured; using the in-memory controller")
        return FakeWorkflowController()
    return TemporalWorkflowController(
        host,
        namespace=os.getenv("TEMPORAL_NAMESPACE", settings.temporal_namespace),
        api_key=os.getenv("TEMPORAL_API_KEY") or None,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="run a single bounded pass and exit")
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=float(os.getenv("VIDGEN_CONTROL_POLL_INTERVAL_SECONDS", "2")),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.getenv("VIDGEN_CONTROL_BATCH_SIZE", "8")),
        help="maximum commands claimed in one pass; bounds dispatch concurrency",
    )
    parser.add_argument("--max-passes", type=int, default=None)
    return parser


def run(arguments: argparse.Namespace) -> int:
    initialize_telemetry(service_name="vidgen-control-dispatcher")
    settings = get_settings()
    engine = build_engine(settings.database_url)
    dispatcher = ControlCommandDispatcher(
        sessionmaker(bind=engine, expire_on_commit=False),
        build_controller(settings),
        image_provider_name=settings.image_provider_name,
        image_model=settings.image_model,
        video_provider_name=settings.video_provider_name,
        visual_capability_profile=settings.visual_capability_profile,
        batch_size=max(1, arguments.batch_size),
        dispatcher_id=default_dispatcher_id(),
    )
    shutdown = _Shutdown()
    for received in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(received, shutdown.request)
        except ValueError:  # pragma: no cover - not the main thread
            pass
    logger.info("control dispatcher starting", extra={"dispatcherId": dispatcher.dispatcher_id})
    if arguments.once:
        report = dispatcher.run_once()
        print(
            f"claimed={report.claimed} dispatched={report.dispatched} "
            f"completed={report.completed} failed={report.failed}"
        )
        return EXIT_OK
    dispatcher.run_forever(
        poll_seconds=max(arguments.poll_interval_seconds, 0.1),
        should_stop=shutdown,
        max_passes=arguments.max_passes,
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
