#!/bin/bash
# Prepare a Claude Code on the web session so `make verify` and `make verify-web`
# can run: the FFmpeg binaries the media, narration, render and QA suites shell
# out to, the Python environment, and the web workspace's node modules.
#
# Local sessions are left alone: the README's prerequisites cover them.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# FFmpeg and ffprobe ship together and must be the same build; the tests
# refuse to fabricate media when either is missing.
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends ffmpeg
fi

# The same install CI performs (`make install`), including the optional Azure
# extras so mypy sees every module it checks. The cache dir matches the
# Makefile's default so a later `make` reuses what this download fetched.
export UV_CACHE_DIR="${UV_CACHE_DIR:-.uv-cache}"
uv sync --all-groups --all-extras

# The pnpm workspace: the web app and the shared TypeScript contracts.
# Playwright's browsers are pre-installed in the remote image, so the install
# must not try to fetch them.
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
pnpm install

{
  echo 'export UV_CACHE_DIR=".uv-cache"'
  echo 'export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1'
} >> "$CLAUDE_ENV_FILE"
