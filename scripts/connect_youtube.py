"""Connect a YouTube channel to a local VidGen owner.

Local development uses the loopback redirect Google permits for the web-server
flow. The script prints an authorization URL, waits for the browser to hit the
local callback, exchanges the code on the backend, verifies the channel identity
with YouTube and seals the refresh credential:

    uv run python scripts/connect_youtube.py --local

The fake provider needs no Google project at all and is what local pipeline runs
and the tests use:

    uv run python scripts/connect_youtube.py --provider fake

Nothing this script prints is a credential: not the access token, not the
refresh token, not the authorization code. The URL it prints carries only the
public client ID, the scopes, the one-time state and the PKCE challenge.
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import os
import threading
import urllib.parse
import webbrowser
from typing import ClassVar

from services.publisher.commands import (
    PublisherCommandOptions,
    build_publisher_provider,
    keyring_from_settings,
    oauth_settings_from_environment,
)
from services.publisher.oauth import OAuthFlowError, YouTubeOAuthService
from services.publisher.providers import FAKE_PROVIDER, YOUTUBE_PROVIDER
from vidgen.db.publication_repository import PublicationRepository
from vidgen.db.session import build_engine, session_factory

_CALLBACK_TIMEOUT_SECONDS = 300


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """A single-shot loopback listener for the OAuth redirect."""

    received: ClassVar[dict[str, str]] = {}
    done: ClassVar[threading.Event] = threading.Event()

    def do_GET(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).received = {key: values[0] for key, values in query.items()}
        body = b"VidGen received the authorization. You can close this tab."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Never cached: this URL carries an authorization code.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        type(self).done.set()

    def log_message(self, format: str, *args: object) -> None:
        """Silenced: the request line contains the authorization code."""


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider", choices=(FAKE_PROVIDER, YOUTUBE_PROVIDER), default=YOUTUBE_PROVIDER
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="listen on the configured loopback redirect and complete the flow",
    )
    parser.add_argument("--owner", default=os.getenv("VIDGEN_LOCAL_OWNER", "local-user"))
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()

    settings = oauth_settings_from_environment()
    provider = build_publisher_provider(PublisherCommandOptions(provider=arguments.provider))
    keyring = keyring_from_settings(
        allow_development_key=arguments.provider == FAKE_PROVIDER or None
    )
    if keyring.is_development_only:
        print(
            "warning: sealing credentials with the shared development key. "
            "It is in this repository and is never suitable for a shared or "
            "deployed environment."
        )

    engine = build_engine()
    with session_factory(engine)() as session:
        service = YouTubeOAuthService(PublicationRepository(session, keyring), provider, settings)
        authorization, state = service.start(owner_subject=arguments.owner)
        session.commit()
        print("open this URL to authorize VidGen:")
        print(f"  {authorization.authorization_url}")
        print(f"  state expires at {authorization.expires_at.isoformat()}")

        if arguments.provider == FAKE_PROVIDER:
            connection, _ = await service.complete(
                state=state, code="fake-authorization-code", owner_subject=arguments.owner
            )
            session.commit()
            _print(connection)
            return 0

        if not arguments.local:
            print("\nre-run with --local to complete the flow automatically.")
            return 0

        parsed = urllib.parse.urlparse(settings.redirect_uri)
        if parsed.hostname not in {"localhost", "127.0.0.1"}:
            parser.error("--local requires VIDGEN_YOUTUBE_OAUTH_REDIRECT_URI to be a loopback URI")
        server = http.server.HTTPServer(
            (parsed.hostname or "127.0.0.1", parsed.port or 80), _CallbackHandler
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        if not arguments.no_browser:
            webbrowser.open(authorization.authorization_url)
        try:
            if not _CallbackHandler.done.wait(_CALLBACK_TIMEOUT_SECONDS):
                parser.error("timed out waiting for the OAuth callback")
            received = _CallbackHandler.received
        finally:
            server.shutdown()
        if "error" in received:
            parser.error(f"authorization was refused: {received['error']}")
        try:
            connection, _ = await service.complete(
                state=received.get("state", ""),
                code=received.get("code", ""),
                owner_subject=arguments.owner,
            )
        except OAuthFlowError as error:
            parser.error(str(error))
        session.commit()
        _print(connection)
    return 0


def _print(connection: object) -> None:
    channel_id = getattr(connection, "channel_id", "")
    print("\nconnected:")
    print(f"  connection_id={getattr(connection, 'id', '')}")
    print(f"  channel_id={channel_id}")
    print(f"  channel_title={getattr(connection, 'channel_title', '')}")
    print(f"  status={getattr(connection, 'status', '')}")
    print(f"  scopes={','.join(getattr(connection, 'granted_scopes', []) or [])}")
    print(f"  encryption_key_version={getattr(connection, 'encryption_key_version', '')}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
