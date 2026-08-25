from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.models import Asset, Project
from vidgen.db.upload_models import UploadPart, UploadSession


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, project: Project) -> Project:
        self.session.add(project)
        self.session.flush()
        return project

    def get(self, project_id: UUID) -> Project | None:
        return self.session.get(Project, project_id)

    def list_for_owner(self, owner_subject: str) -> list[Project]:
        return list(
            self.session.scalars(
                select(Project)
                .where(Project.owner_subject == owner_subject)
                .order_by(Project.created_at.desc())
            )
        )


class AssetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_hash(self, sha256: str) -> Asset | None:
        return self.session.scalar(select(Asset).where(Asset.sha256 == sha256))

    def get_by_idempotency(self, project_id: UUID | None, idempotency_key: str) -> Asset | None:
        return self.session.scalar(
            select(Asset).where(
                Asset.project_id == project_id,
                Asset.idempotency_key == idempotency_key,
            )
        )

    def add(self, asset: Asset) -> Asset:
        self.session.add(asset)
        self.session.flush()
        return asset


class UploadRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, upload: UploadSession) -> UploadSession:
        self.session.add(upload)
        self.session.flush()
        return upload

    def get(self, upload_id: UUID) -> UploadSession | None:
        return self.session.get(UploadSession, upload_id)

    def get_part(self, upload_id: UUID, part_number: int) -> UploadPart | None:
        return self.session.scalar(
            select(UploadPart).where(
                UploadPart.upload_id == upload_id,
                UploadPart.part_number == part_number,
            )
        )

    def list_parts(self, upload_id: UUID) -> list[UploadPart]:
        return list(
            self.session.scalars(
                select(UploadPart)
                .where(UploadPart.upload_id == upload_id)
                .order_by(UploadPart.part_number)
            )
        )

    def add_part(self, part: UploadPart) -> UploadPart:
        self.session.add(part)
        self.session.flush()
        return part
