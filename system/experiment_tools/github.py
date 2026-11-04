#!/usr/bin/env python3
"""Shared GitHub credentials and UTC formatting for offline-safe CLI imports."""

from datetime import datetime, timezone
import os
import subprocess
import sys
from typing import Optional


KEYCHAIN_SERVICE = "Agendex.GitHub"
KEYCHAIN_ACCOUNT = "agendex"


def keychain_token() -> Optional[str]:
    """Read this project's macOS credential without exposing command output."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def github_token() -> Optional[str]:
    """Explicit environment configuration takes priority over the local keychain."""
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or keychain_token()


def utc_now() -> str:
    """Use UTC consistently so local time zones do not affect query ranges."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_timestamp(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
