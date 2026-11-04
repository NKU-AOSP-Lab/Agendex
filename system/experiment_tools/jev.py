#!/usr/bin/env python3
"""Shared Jev transport, credentials, metadata validation and durable response logs.

Policy construction and screening workflows belong to their experiment routes.
Importing this module performs no network request or credential lookup.
"""

from __future__ import annotations

from datetime import datetime, timezone
import getpass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import threading
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


# Keep recorded legacy paths readable without changing frozen experiment files.
if "agendex_workspace_paths" not in sys.modules:
    _WORKSPACE_PATHS_SPEC = importlib.util.spec_from_file_location(
        "agendex_workspace_paths", Path(__file__).resolve().parents[2] / "system" / "workspace_paths.py")
    if _WORKSPACE_PATHS_SPEC is None or _WORKSPACE_PATHS_SPEC.loader is None:
        raise ImportError("Cannot load workspace path resolver")
    WORKSPACE_PATHS = importlib.util.module_from_spec(_WORKSPACE_PATHS_SPEC)
    sys.modules[_WORKSPACE_PATHS_SPEC.name] = WORKSPACE_PATHS
    _WORKSPACE_PATHS_SPEC.loader.exec_module(WORKSPACE_PATHS)
WORKSPACE_PATHS = sys.modules["agendex_workspace_paths"]
resolve_recorded_path = WORKSPACE_PATHS.resolve_recorded_path


ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.14"
KEYCHAIN_SERVICE = "Agendex.OpenRouter"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def description_missing(repo: dict) -> bool:
    value = repo.get("description")
    if value is None:
        return True
    if not isinstance(value, str):
        raise ValueError("Description must be text or null")
    return not value.strip()


def repository_input(repo: dict) -> dict:
    """Exclude stars and activity so they cannot silently become inclusion rules."""
    return {key: repo.get(key) for key in
            ("id", "full_name", "description", "topics", "language")}


def finite_number(value, low: float, high: float = math.inf) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high)


def context_overflow_details(http_status: Optional[int], raw_body) -> Optional[dict]:
    """Recognize an explicit provider input-context rejection, without retrying.

    The caller must redact credentials before persisting the original error body.
    Ordinary validation/body-size errors, output ``max_tokens`` parameter errors,
    rate limits and server failures do not establish that README trimming helps.
    ``max_tokens_exceeded`` is TypeSafe's documented input-context error code.
    """
    if http_status not in (400, 413, 422):
        return None
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8", errors="replace")
    if isinstance(raw_body, str):
        try:
            body = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError):
            body = {"message": raw_body}
    elif isinstance(raw_body, dict):
        body = raw_body
    else:
        return None

    codes, messages = [], []

    def collect(value, key="", depth=0):
        # Inspect error fields, never reflected request/input/prompt contents.
        if depth > 6:
            return
        if isinstance(value, dict):
            for name in ("code", "type", "error", "message", "detail", "details", "metadata", "raw", "cause"):
                if name in value:
                    collect(value[name], name, depth + 1)
        elif isinstance(value, list):
            for item in value[:20]:
                collect(item, key, depth + 1)
        elif isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    nested = json.loads(stripped)
                except ValueError:
                    pass
                else:
                    collect(nested, key, depth + 1)
                    return
            if key in ("code", "type", "error"):
                codes.append(stripped.casefold())
            if key not in ("code", "type"):
                messages.append(" ".join(stripped.casefold().split()))

    collect(body)
    known_codes = {
        "context_length_exceeded", "context_window_exceeded", "max_input_tokens_exceeded",
        "too_many_input_tokens", "prompt_too_long", "input_too_long", "max_tokens_exceeded",
    }
    matched_code = next((code for code in codes if code in known_codes), None)
    # 413 means HTTP body size unless its message explicitly establishes context.
    if matched_code and http_status != 413:
        return {"category": "context_length_exceeded", "http_status": http_status,
                "evidence_kind": "provider_error_code", "error_code": matched_code}

    patterns = (
        r"(?:maximum|max) context (?:length|window).{0,180}(?:exceed|request|provided|result|input|prompt)",
        r"context (?:length|window|limit).{0,80}(?:exceed|too long|overflow)",
        r"(?:exceed\w*|too long|longer than).{0,80}(?:context (?:length|window|limit))",
        r"(?:input|prompt) tokens?.{0,80}(?:exceed\w*|above|greater than).{0,80}(?:maximum|max|limit)",
        r"(?:input|prompt).{0,30}too long.{0,80}tokens?",
        r"tokens?.{0,40}exceed\w*.{0,40}(?:max(?:imum)? input tokens?|context)",
        r"上下文.{0,40}(?:超过|超出|过长)",
    )
    if any(re.search(pattern, message) for message in messages for pattern in patterns):
        return {"category": "context_length_exceeded", "http_status": http_status,
                "evidence_kind": "explicit_context_error_message"}
    return None


def next_context_prefix(raw_utf8_bytes: bytes, current_limit: int, error_details: dict) -> int:
    """Choose a shorter UTF-8-safe prefix only after a confirmed context error.

    This is a byte-prefix backoff, not a tokenizer or a bytes-to-tokens estimate.
    A return value of zero cannot remove metadata/policy overhead; the caller must
    stop if an empty README was already rejected, retaining that item as pending.
    The caller owns request logging, coverage records, accounting and retry limits.
    """
    if not isinstance(raw_utf8_bytes, bytes):
        raise ValueError("README input must be UTF-8 bytes")
    if (type(current_limit) is not int or not 0 <= current_limit <= len(raw_utf8_bytes)):
        raise ValueError("Current README limit must be within the original byte range")
    if not isinstance(error_details, dict) or error_details.get("category") != "context_length_exceeded":
        raise ValueError("README context backoff requires a confirmed context error")
    raw_utf8_bytes.decode("utf-8")
    if current_limit == 0:
        return 0
    current_prefix = len(raw_utf8_bytes[:current_limit].decode("utf-8", errors="ignore").encode("utf-8"))
    target = current_prefix * 3 // 4
    return len(raw_utf8_bytes[:target].decode("utf-8", errors="ignore").encode("utf-8"))


def validate_response(data: dict, expected_ids) -> tuple:
    """Reject missing/invalid predictions instead of turning failures into negatives."""
    if not isinstance(data, dict) or not isinstance(data.get("model"), str) or not data["model"].strip():
        raise ValueError("Response is missing the served model")
    expected = {f"repo_{identifier}": identifier for identifier in expected_ids}
    answers = data.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(expected):
        raise ValueError("Response question IDs do not match the request")
    probabilities = {}
    for key, identifier in expected.items():
        answer = answers[key]
        if (not isinstance(answer, dict) or answer.get("type") != "noul"
                or not finite_number(answer.get("noul"), 0, 1)):
            raise ValueError(f"Invalid probability for {key}")
        probabilities[identifier] = answer["noul"]
    usage = data.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("Response is missing usage")
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"Invalid {key}")
    cost = usage.get("cost")
    if cost is not None and not finite_number(cost, 0):
        raise ValueError("Invalid cost")
    return probabilities, {**usage, "cost": cost}


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_records(path: Path):
    """Repair only an interrupted final line; corruption within a log is an error."""
    if not path.exists():
        return
    with path.open("r+b") as stream:
        while True:
            start = stream.tell()
            line = stream.readline()
            if not line:
                return
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if not line.endswith(b"\n") and not stream.read(1):
                    stream.truncate(start)
                    return
                raise ValueError(f"Corrupt log: {path.name}, byte {start}")
            if not line.endswith(b"\n"):
                stream.seek(0, 2)
                stream.write(b"\n")
            yield record


class Output:
    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = threading.Lock()

    def append(self, name: str, value) -> None:
        self.append_many(name, [value])

    def append_many(self, name: str, values: list) -> None:
        """Commit a batch together; paid responses are already durable before labels."""
        if not values:
            return
        block = b"".join(encoded(value) + b"\n" for value in values)
        with self.lock:
            with (self.directory / name).open("ab") as stream:
                stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Keep the credential on the explicitly authorized OpenRouter endpoint.
        return None


class TransportFailure(RuntimeError):
    """A network failure; local persistence failures must never trigger paid retries."""


class JevClient:
    def __init__(self, key: str, output: Output, stop: threading.Event, max_cost: float):
        self.key, self.output, self.stop = key, output, stop
        self.max_cost = max_cost
        self.cost = 0.0
        self.unknown_cost = 0
        self.network_error_attempts = 0
        self.lock = threading.Lock()

    def account(self, response: dict) -> None:
        usage = response.get("usage") if isinstance(response, dict) else None
        cost = usage.get("cost") if isinstance(usage, dict) else None
        with self.lock:
            if finite_number(cost, 0):
                self.cost += cost
            else:
                self.unknown_cost += 1

    def call(self, payload: dict) -> dict:
        identifiers = [int(key.removeprefix("repo_")) for key in payload["questions"]]
        for attempt in range(1, 5):
            with self.lock:
                allowed = self.cost < self.max_cost and not self.unknown_cost
            if self.stop.is_set() or not allowed:
                raise RuntimeError("Stopped, spending limit reached, or cost unavailable")
            event = {"time": now(), "repository_ids": identifiers, "attempt": attempt}
            started = time.monotonic()
            delay, retry = min(2 ** attempt, 30), False
            try:
                request = Request(ENDPOINT, data=encoded(payload), method="POST", headers={
                    "Authorization": "Bearer " + self.key,
                    "Content-Type": "application/json", "User-Agent": "Agendex-Jev-Screening/0.1",
                })
                try:
                    with build_opener(NoRedirect()).open(request, timeout=45) as response:
                        body = response.read()
                except HTTPError:
                    raise
                except (URLError, TimeoutError, ConnectionError, OSError) as error:
                    raise TransportFailure(type(error).__name__) from error
                try:
                    data = json.loads(body)
                except ValueError:
                    self.output.append("responses.jsonl", {**event, "body": body.decode("utf-8", "replace").replace(self.key, "[REDACTED]")})
                    self.account(None)
                    raise ValueError("Response is not JSON; original response saved")
                self.output.append("responses.jsonl", {**event, "response": data})
                self.account(data)
                validate_response(data, identifiers)
                event.update(status="ok", elapsed_seconds=round(time.monotonic() - started, 3))
                self.output.append("requests.jsonl", event)
                return data
            except HTTPError as error:
                body = error.read().decode("utf-8", "replace").replace(self.key, "[REDACTED]")
                try:
                    details = json.loads(body)
                except ValueError:
                    details = {}
                error_info = details.get("error") if isinstance(details, dict) else None
                metadata = error_info.get("metadata") if isinstance(error_info, dict) else None
                limit_source = metadata.get("limit_source") if isinstance(metadata, dict) else None
                retry = error.code == 429 or error.code >= 500 or (error.code == 402 and limit_source == "openrouter_in_flight_budget")
                event.update(status="http_error", http_status=error.code, body=body)
                after = error.headers.get("Retry-After", "")
                if after.isdigit():
                    delay = min(max(delay, int(after)), 60)
            except TransportFailure as error:
                retry = True
                with self.lock:
                    self.network_error_attempts += 1
                event.update(status="network_error", error_type=str(error))
            except ValueError as error:
                event.update(status="invalid_response", error=str(error))
            self.output.append("requests.jsonl", event)
            if not retry or attempt == 4:
                raise RuntimeError(f"Jev request failed: {event['status']} {event.get('http_status', '')}")
            if self.stop.wait(delay + random.random()):
                raise RuntimeError("Interrupted")
        raise RuntimeError("Retry limit reached")


def api_key() -> str:
    value = os.environ.get("OPENROUTER_API_KEY")
    if not value and sys.platform == "darwin":
        result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                                 "-a", "agendex", "-w"], capture_output=True, text=True)
        if result.returncode == 0:
            value = result.stdout.strip()
    if not value:
        raise ValueError("Set OPENROUTER_API_KEY or run --configure-key")
    return value


def configure_key() -> None:
    if sys.platform != "darwin" or not sys.stdin.isatty():
        raise ValueError("Keychain configuration needs an interactive macOS terminal")
    key = getpass.getpass("OpenRouter API key (hidden): ")
    if not re.fullmatch(r"sk-or-v1-[A-Za-z0-9]+", key):
        raise ValueError("Unexpected key format")
    # Send the credential over stdin, never as a process argument or project file.
    command = f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a "agendex" -w "{key}"\n'
    result = subprocess.run(["/usr/bin/security", "-i"], input=command,
                            capture_output=True, text=True)
    if result.returncode != 0 or api_key() != key:
        raise ValueError("Could not save and verify the Keychain credential")
    print("OpenRouter credential saved in macOS Keychain.")


def load_input(path: Path) -> tuple:
    rows, identifiers, checksum = [], set(), hashlib.sha256()
    with path.open("rb") as stream:
        for line in stream:
            checksum.update(line)
            row = json.loads(line)
            identifier = row["id"]
            if identifier in identifiers:
                raise ValueError("Duplicate repository ID in input")
            identifiers.add(identifier)
            description_missing(row)
            rows.append(repository_input(row))
    return rows, checksum.hexdigest()
