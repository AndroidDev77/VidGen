"""Replayable idempotency for every T18 mutation.

A record binds one owner, operation, resource and client key to the hash of the
request that first used it. Repeating the same key with an equivalent request
replays the original response; reusing it with a different request is a
structured conflict, so a duplicate browser submission can never create a second
script revision, shot workflow, render job, or approval.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vidgen.db.review_models import ApiIdempotencyRecord
from vidgen.review.errors import idempotency_key_mismatch, idempotency_key_required


def request_hash(payload: object) -> str:
    """Return the canonical hash binding a stored idempotency key to a request."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


class IdempotencyService:
    def __init__(self, session: Session, owner_subject: str) -> None:
        self._session = session
        self._owner = owner_subject

    def require_key(self, operation: str, key: str | None) -> str:
        if not key or not key.strip():
            raise idempotency_key_required(operation)
        return key.strip()[:255]

    def replay(
        self, operation: str, resource_key: str, key: str, payload: object
    ) -> dict[str, Any] | None:
        """Return the stored response when this exact request was already handled."""
        record = self._session.scalar(
            select(ApiIdempotencyRecord).where(
                ApiIdempotencyRecord.owner_subject == self._owner,
                ApiIdempotencyRecord.operation == operation,
                ApiIdempotencyRecord.resource_key == resource_key,
                ApiIdempotencyRecord.idempotency_key == key,
            )
        )
        if record is None:
            return None
        if record.request_hash != request_hash(payload):
            raise idempotency_key_mismatch(operation)
        return dict(record.response_body)

    def record(
        self,
        operation: str,
        resource_key: str,
        key: str,
        payload: object,
        status_code: int,
        response_body: dict[str, Any],
    ) -> None:
        record = ApiIdempotencyRecord(
            owner_subject=self._owner,
            operation=operation,
            resource_key=resource_key,
            idempotency_key=key,
            request_hash=request_hash(payload),
            status_code=status_code,
            response_body=response_body,
        )
        self._session.add(record)
        try:
            self._session.flush()
        except IntegrityError as error:  # concurrent duplicate submission
            self._session.rollback()
            raise idempotency_key_mismatch(operation) from error
