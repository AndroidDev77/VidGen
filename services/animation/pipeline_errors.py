"""Shared T15 failure types without adapter/pipeline import cycles."""


class AmbiguousVideoSubmission(RuntimeError):
    """Submission may have succeeded but no remote ID was received."""
