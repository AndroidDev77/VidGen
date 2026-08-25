from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import Header


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str


class Authenticator(Protocol):
    async def authenticate(self, supplied_subject: str | None) -> Principal: ...


class LocalAuthenticator:
    async def authenticate(self, supplied_subject: str | None) -> Principal:
        return Principal(subject=supplied_subject or "local-user")


async def get_current_user(
    x_vidgen_user: Annotated[str | None, Header()] = None,
) -> Principal:
    return await LocalAuthenticator().authenticate(x_vidgen_user)
