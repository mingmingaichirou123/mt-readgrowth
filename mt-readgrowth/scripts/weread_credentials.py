#!/usr/bin/env python3
"""Resolve the WeRead API key without exposing it in logs or artifacts."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


ENVIRONMENT_VARIABLE = "WEREAD_API_KEY"
KEY_FILE_NAME = ".weread-api-key"
MAX_KEY_FILE_BYTES = 4096
MISSING_KEY_MESSAGE = (
    "WeRead API key is not configured; set WEREAD_API_KEY or add one line to "
    ".weread-api-key in the reading workspace"
)
INVALID_KEY_FILE_MESSAGE = "WeRead API key file must contain exactly one non-empty UTF-8 line"


class CredentialFailure(RuntimeError):
    """A safe, user-facing credential resolution failure."""


def key_file_path(workspace: Path) -> Path:
    return Path(workspace) / KEY_FILE_NAME


def resolve_weread_api_key(
    workspace: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the environment key first, then the workspace-local one-line key."""
    source = os.environ if environ is None else environ
    environment_key = str(source.get(ENVIRONMENT_VARIABLE, "")).strip()
    if environment_key:
        return environment_key

    path = key_file_path(workspace)
    if not path.exists():
        raise CredentialFailure(MISSING_KEY_MESSAGE)
    try:
        if not path.is_file() or path.stat().st_size > MAX_KEY_FILE_BYTES:
            raise CredentialFailure(INVALID_KEY_FILE_MESSAGE)
        key = path.read_text(encoding="utf-8-sig").strip()
    except CredentialFailure:
        raise
    except (OSError, UnicodeError) as exc:
        raise CredentialFailure(INVALID_KEY_FILE_MESSAGE) from exc
    if not key or "\n" in key or "\r" in key or any(character.isspace() for character in key):
        raise CredentialFailure(INVALID_KEY_FILE_MESSAGE)
    return key
