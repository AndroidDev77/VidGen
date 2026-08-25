from __future__ import annotations

import argparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.settings import get_settings
from services.media_worker.pipeline import MediaPipeline
from vidgen.db.models import SourceVideo
from vidgen.db.session import build_engine
from vidgen.storage.blob import FilesystemBlobStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Process an uploaded VidGen source video")
    parser.add_argument("project_id", type=UUID)
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()
    settings = get_settings()
    engine = build_engine(settings.database_url)
    blob_store = FilesystemBlobStore(settings.blob_root, settings.signing_secret.encode())
    with Session(engine, expire_on_commit=False) as session:
        source = session.scalar(
            select(SourceVideo)
            .where(SourceVideo.project_id == args.project_id)
            .order_by(SourceVideo.created_at.desc(), SourceVideo.id.desc())
        )
        if source is None:
            parser.error("project has no finalized source video")
        result = MediaPipeline(session, blob_store).process(
            project_id=args.project_id,
            source_video_id=source.id,
            idempotency_key=args.idempotency_key or f"media:{source.id}:v1",
        )
        print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
