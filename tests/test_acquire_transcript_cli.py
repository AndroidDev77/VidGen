from __future__ import annotations

from uuid import UUID

from scripts.acquire_transcript import build_parser


def test_cli_accepts_repeated_sidecar_asset_ids() -> None:
    project_id = UUID("00000000-0000-0000-0000-000000000001")
    first = UUID("00000000-0000-0000-0000-000000000002")
    second = UUID("00000000-0000-0000-0000-000000000003")
    args = build_parser().parse_args(
        [
            str(project_id),
            "--sidecar-asset-id",
            str(first),
            "--sidecar-asset-id",
            str(second),
        ]
    )
    assert args.project_id == project_id
    assert args.sidecar_asset_id == [first, second]
