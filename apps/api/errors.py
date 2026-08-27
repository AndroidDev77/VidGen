"""Structured error rendering for the T18 control plane.

The API returns the :class:`~vidgen.contracts.review.ApiError` shape for every
failure it owns, and never leaks a traceback, SQL text, provider payload or
FFmpeg log to a browser.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi import HTTPException as FastAPIHTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from vidgen.contracts.review import ApiError, ApiErrorCode, ApiErrorField
from vidgen.review.errors import ReviewError

CORRELATION_HEADER = "x-vidgen-correlation-id"

# Readable summaries for the short domain codes the pre-T18 routes raise.
SUMMARIES = {
    "project not found": "The requested project was not found.",
    "upload not found": "The requested upload was not found.",
    "asset not found": "The requested asset was not found.",
    "source video not found": "This project has no finalized source video yet.",
    "conflicting_part": "A different body was already uploaded for this part.",
    "invalid_video_container": "That file is not a supported video container.",
    "unsupported_media_type": "That media type is not accepted for source video.",
    "file_too_large": "That file exceeds the configured maximum upload size.",
    "checksum_mismatch": "The uploaded bytes do not match the checksum you declared.",
    "incomplete_upload": "Some parts are still missing. Finish uploading them first.",
}


def correlation_id(request: Request) -> str | None:
    header = request.headers.get(CORRELATION_HEADER)
    if header:
        return header[:128]
    trace = request.headers.get("traceparent")
    if trace and trace.count("-") >= 2:
        return trace.split("-")[1][:128]
    return None


def error_response(request: Request, status_code: int, error: ApiError) -> JSONResponse:
    body = error.model_copy(
        update={"correlation_id": error.correlation_id or correlation_id(request)}
    )
    headers = {}
    if body.current_version is not None:
        headers["ETag"] = f'"{body.current_version}"'
    return JSONResponse(
        status_code=status_code, content=body.model_dump(mode="json"), headers=headers
    )


def register_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(ReviewError)
    async def _review_error(request: Request, exc: ReviewError) -> JSONResponse:
        return error_response(request, exc.status_code, exc.error)

    @application.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            ApiErrorField(
                field=".".join(str(part) for part in item.get("loc", ())[1:]) or "body",
                code=str(item.get("type", "invalid"))[:64],
                message=str(item.get("msg", "Invalid value"))[:500],
            )
            for item in exc.errors()[:64]
        ]
        return error_response(
            request,
            422,
            ApiError(
                code=ApiErrorCode.VALIDATION_FAILED,
                summary="The request could not be validated.",
                retryable=False,
                fields=fields,
            ),
        )

    @application.exception_handler(StarletteHTTPException)
    @application.exception_handler(FastAPIHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if not str(request.url.path).startswith("/api/v1/"):
            response = await http_exception_handler(request, exc)
            assert isinstance(response, JSONResponse)
            return response
        code = {
            404: ApiErrorCode.NOT_FOUND,
            409: ApiErrorCode.VERSION_CONFLICT,
            412: ApiErrorCode.VERSION_CONFLICT,
            422: ApiErrorCode.VALIDATION_FAILED,
            429: ApiErrorCode.RATE_LIMITED,
        }.get(exc.status_code, ApiErrorCode.INTERNAL_ERROR)
        detail = (
            exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
        )
        return error_response(
            request,
            exc.status_code,
            ApiError(
                code=code,
                summary=SUMMARIES.get(detail, detail)[:500],
                retryable=exc.status_code >= 500 or exc.status_code == 429,
                detail_code=detail[:128],
            ),
        )
