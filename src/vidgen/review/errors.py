"""Structured control-plane failures.

Every T18 failure is raised as a :class:`ReviewError` carrying an
:class:`~vidgen.contracts.review.ApiError`. The API layer renders it verbatim,
so Python tracebacks, SQL text, provider payloads and FFmpeg logs never reach a
browser.
"""

from __future__ import annotations

from vidgen.contracts.review import ApiError, ApiErrorCode, ApiErrorField


class ReviewError(Exception):
    """A structured, renderable control-plane failure."""

    def __init__(self, status_code: int, error: ApiError) -> None:
        super().__init__(error.summary)
        self.status_code = status_code
        self.error = error


def not_found(resource: str) -> ReviewError:
    """Return an indistinguishable 404 for missing, foreign, and cross-project IDs."""
    return ReviewError(
        404,
        ApiError(
            code=ApiErrorCode.NOT_FOUND,
            summary=f"The requested {resource} was not found.",
            retryable=False,
        ),
    )


def validation_failed(summary: str, fields: list[ApiErrorField] | None = None) -> ReviewError:
    return ReviewError(
        422,
        ApiError(
            code=ApiErrorCode.VALIDATION_FAILED,
            summary=summary,
            retryable=False,
            fields=fields or [],
        ),
    )


def precondition_required(resource: str, current_version: int) -> ReviewError:
    return ReviewError(
        428,
        ApiError(
            code=ApiErrorCode.PRECONDITION_REQUIRED,
            summary=(
                f"This change to the {resource} requires an If-Match header carrying the "
                "row version you last read."
            ),
            retryable=False,
            current_version=current_version,
        ),
    )


def version_conflict(resource: str, current_version: int) -> ReviewError:
    return ReviewError(
        409,
        ApiError(
            code=ApiErrorCode.VERSION_CONFLICT,
            summary=(
                f"The {resource} changed since you loaded it. Refresh to compare your "
                "unsaved edits against the current version."
            ),
            retryable=False,
            current_version=current_version,
        ),
    )


def idempotency_key_required(operation: str) -> ReviewError:
    return ReviewError(
        428,
        ApiError(
            code=ApiErrorCode.IDEMPOTENCY_KEY_REQUIRED,
            summary=f"{operation} requires an Idempotency-Key header.",
            retryable=False,
        ),
    )


def idempotency_key_mismatch(operation: str) -> ReviewError:
    return ReviewError(
        409,
        ApiError(
            code=ApiErrorCode.IDEMPOTENCY_KEY_MISMATCH,
            summary=(f"This Idempotency-Key was already used for a different {operation} request."),
            retryable=False,
        ),
    )


def conflict(code: ApiErrorCode, summary: str, **extra: object) -> ReviewError:
    return ReviewError(
        409,
        ApiError(code=code, summary=summary, retryable=False, **extra),  # type: ignore[arg-type]
    )
