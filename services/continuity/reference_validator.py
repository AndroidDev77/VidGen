"""Technical validation delegated to the existing T14 image validator."""

from __future__ import annotations

from services.image_generation.validation import validate_base64_image
from vidgen.contracts.continuity import ReferenceValidationDiagnostic, ReferenceValidationReport
from vidgen.contracts.image_generation import ImageFormat


def validate_reference(
    encoded_image: str, *, expected_format: ImageFormat, width: int, height: int
) -> ReferenceValidationReport:
    report = validate_base64_image(
        encoded_image, expected_format=expected_format, width=width, height=height
    ).report
    return ReferenceValidationReport(
        valid=report.valid,
        sha256=report.sha256,
        width=report.width,
        height=report.height,
        media_type=report.mime_type,
        diagnostics=[
            ReferenceValidationDiagnostic(
                code=item.code, severity=item.severity, message=item.message
            )
            for item in report.diagnostics
        ],
    )
