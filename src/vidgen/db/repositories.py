from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.models import Asset, Project


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, project: Project) -> Project:
        self.session.add(project)
        self.session.flush()
        return project

    def get(self, project_id: UUID) -> Project | None:
        return self.session.get(Project, project_id)


class AssetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_hash(self, sha256: str) -> Asset | None:
        return self.session.scalar(select(Asset).where(Asset.sha256 == sha256))

    def add(self, asset: Asset) -> Asset:
        self.session.add(asset)
        self.session.flush()
        return asset
