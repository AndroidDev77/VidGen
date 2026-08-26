"""Database models and repositories."""

from vidgen.db.base import Base

__all__ = ["Base"]
from . import narration_models as narration_models
