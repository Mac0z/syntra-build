"""Reusable, per-operation Git askpass authentication."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from syntra_build.infrastructure.config import SecretValue

_ASKPASS_PROGRAM = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' "$SYNTRA_GIT_USERNAME" ;;
  *Password*) printf '%s\\n' "$SYNTRA_GIT_PASSWORD" ;;
  *) exit 1 ;;
esac
"""


@contextmanager
def git_authentication_environment(
    temporary_root: Path, username: str, token: SecretValue
) -> Iterator[dict[str, str]]:
    """Yield an environment whose credential material disappears on exit."""
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".git-askpass-", dir=temporary_root) as raw:
        askpass = Path(raw) / "askpass"
        askpass.write_text(_ASKPASS_PROGRAM, encoding="utf-8")
        askpass.chmod(0o700)
        environment = dict(os.environ)
        environment.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS=str(askpass),
            SYNTRA_GIT_USERNAME=username,
            SYNTRA_GIT_PASSWORD=token.value,
        )
        yield environment
