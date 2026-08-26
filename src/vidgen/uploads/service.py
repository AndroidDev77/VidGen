from __future__ import annotations

import fcntl
import hashlib
import math
import os
import shutil
from collections.abc import AsyncIterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.models import Project, SourceVideo
from vidgen.db.repositories import UploadRepository
from vidgen.db.upload_models import UploadPart, UploadSession
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore
from vidgen.storage.content_address import sha256_file


class UploadError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PartWriteResult:
    part: UploadPart
    duplicate: bool


@dataclass(frozen=True, slots=True)
class FinalizedUpload:
    upload: UploadSession
    source_video: SourceVideo


class UploadService:
    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        upload_root: Path,
        max_upload_bytes: int,
        allowed_media_types: tuple[str, ...],
    ) -> None:
        self.session = session
        self.blob_store = blob_store
        self.upload_root = upload_root.resolve()
        self.max_upload_bytes = max_upload_bytes
        self.allowed_media_types = allowed_media_types
        self.uploads = UploadRepository(session)
        self.assets = AssetService(session, blob_store)
        self.upload_root.mkdir(parents=True, exist_ok=True)

    def initialize(
        self,
        *,
        project: Project,
        owner_subject: str,
        filename: str,
        media_type: str,
        expected_size: int,
        expected_sha256: str,
        part_size: int,
    ) -> UploadSession:
        if media_type not in self.allowed_media_types:
            raise UploadError("unsupported_media_type", f"unsupported media type: {media_type}")
        if expected_size > self.max_upload_bytes:
            raise UploadError("upload_too_large", "upload exceeds configured maximum")
        upload = self.uploads.add(
            UploadSession(
                project_id=project.id,
                owner_subject=owner_subject,
                filename=Path(filename).name,
                media_type=media_type,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                part_size=part_size,
            )
        )
        project.status = "uploading"
        self.session.commit()
        return upload

    async def write_part(
        self,
        upload: UploadSession,
        part_number: int,
        chunks: AsyncIterable[bytes],
    ) -> PartWriteResult:
        if upload.status not in {"uploading", "finalizing"}:
            raise UploadError("upload_closed", "upload no longer accepts parts")
        expected_parts = math.ceil(upload.expected_size / upload.part_size)
        if part_number < 0 or part_number >= expected_parts:
            raise UploadError("invalid_part_number", "part number is outside the upload range")

        directory = self._upload_directory(upload.id)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".{part_number}.{os.getpid()}.{uuid4().hex}.upload"
        digest = hashlib.sha256()
        byte_size = 0
        try:
            with temporary.open("xb") as stream:
                async for chunk in chunks:
                    byte_size += len(chunk)
                    if byte_size > upload.part_size:
                        raise UploadError("part_too_large", "part exceeds configured part size")
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if byte_size == 0:
                raise UploadError("empty_part", "upload part cannot be empty")
            actual_hash = digest.hexdigest()
            with self._part_lock(directory, part_number):
                existing = self.uploads.get_part(upload.id, part_number)
                if existing is not None:
                    if existing.sha256 != actual_hash or existing.byte_size != byte_size:
                        raise UploadError("conflicting_part", "part retry has different content")
                    # A matching retry is also the recovery mechanism for a part file
                    # lost after its database row was committed. Replacing it is atomic
                    # and harmless when the existing file is already healthy.
                    destination = Path(existing.storage_path)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(temporary, destination)
                    return PartWriteResult(existing, duplicate=True)

                destination = directory / f"part-{part_number:08d}"
                os.replace(temporary, destination)
                part = self.uploads.add_part(
                    UploadPart(
                        upload_id=upload.id,
                        part_number=part_number,
                        byte_size=byte_size,
                        sha256=actual_hash,
                        storage_path=str(destination),
                    )
                )
                self.session.commit()
                return PartWriteResult(part, duplicate=False)
        finally:
            temporary.unlink(missing_ok=True)

    def finalize(self, upload: UploadSession) -> FinalizedUpload:
        directory = self._upload_directory(upload.id)
        directory.mkdir(parents=True, exist_ok=True)
        with self._file_lock(directory / "finalize.lock"):
            # The upload may have completed in another request while this one
            # waited for the filesystem lock.
            self.session.refresh(upload)
            return self._finalize_locked(upload, directory)

    def _finalize_locked(
        self, upload: UploadSession, directory: Path
    ) -> FinalizedUpload:
        if upload.status == "complete" and upload.completed_asset_id is not None:
            source = self.session.scalar(
                select(SourceVideo).where(SourceVideo.asset_id == upload.completed_asset_id)
            )
            if source is None:
                raise UploadError("inconsistent_upload", "completed source video is missing")
            return FinalizedUpload(upload, source)
        if upload.status not in {"uploading", "finalizing"}:
            raise UploadError("upload_closed", "upload cannot be finalized")

        parts = self.uploads.list_parts(upload.id)
        expected_parts = math.ceil(upload.expected_size / upload.part_size)
        if [part.part_number for part in parts] != list(range(expected_parts)):
            raise UploadError("missing_parts", "upload does not contain every required part")

        upload.status = "finalizing"
        upload.error_code = None
        self.session.commit()
        assembled = directory / f"assembled.{uuid4().hex}.mp4"
        with assembled.open("wb") as output:
            for part in parts:
                with Path(part.storage_path).open("rb") as input_stream:
                    shutil.copyfileobj(input_stream, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())

        actual_hash, actual_size = sha256_file(assembled)
        if actual_size != upload.expected_size:
            self._return_to_uploading(upload, "size_mismatch")
            raise UploadError("size_mismatch", "assembled upload size does not match")
        if actual_hash != upload.expected_sha256:
            self._return_to_uploading(upload, "hash_mismatch")
            raise UploadError("hash_mismatch", "assembled upload hash does not match")
        with assembled.open("rb") as stream:
            if b"ftyp" not in stream.read(16):
                self._return_to_uploading(upload, "invalid_video_container")
                raise UploadError("invalid_video_container", "file is not an MP4-family container")

        stored = self.assets.store_file(
            path=assembled,
            kind="source_video",
            media_type=upload.media_type,
            project_id=upload.project_id,
            idempotency_key=f"upload:{upload.id}:source",
            generation_parameters={
                "filename": upload.filename,
                "upload_id": str(upload.id),
                "part_count": expected_parts,
            },
            metadata={"original_filename": upload.filename},
        )
        source = self.session.scalar(select(SourceVideo).where(SourceVideo.asset_id == stored.id))
        if source is None:
            source = SourceVideo(
                project_id=upload.project_id,
                asset_id=stored.id,
                filename=upload.filename,
                probe={},
            )
            self.session.add(source)
        upload.completed_asset_id = stored.id
        upload.status = "complete"
        project = self.session.get(Project, upload.project_id)
        if project is not None:
            project.status = "uploaded"
        self.session.commit()
        shutil.rmtree(directory, ignore_errors=True)
        return FinalizedUpload(upload, source)

    def _return_to_uploading(self, upload: UploadSession, error_code: str) -> None:
        upload.status = "uploading"
        upload.error_code = error_code
        self.session.commit()

    def _upload_directory(self, upload_id: UUID) -> Path:
        path = (self.upload_root / str(upload_id)).resolve()
        if not path.is_relative_to(self.upload_root):
            raise ValueError("upload path escapes configured root")
        return path

    @contextmanager
    def _part_lock(self, directory: Path, part_number: int) -> Iterator[None]:
        with self._file_lock(directory / f"part-{part_number:08d}.lock"):
            yield

    @contextmanager
    def _file_lock(self, lock_path: Path) -> Iterator[None]:
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
