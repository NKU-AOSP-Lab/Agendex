#!/usr/bin/env python3
"""采集 GitHub README 原文；仅使用 Python 3.9+ 标准库，不进行 Agent 分类。"""

from __future__ import annotations

import argparse
import base64
import binascii
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
from http.client import HTTPException
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request


# Keep discovery and README collection credentials consistent.
_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))
from system.experiment_tools import github as GITHUB
from system.experiment_tools.github import github_token, utc_now


# 独立 CLI 与测试共用路径迁移规则，不改写冻结文件中的历史路径。
if "agendex_workspace_paths" not in sys.modules:
    _PATHS_SPEC = importlib.util.spec_from_file_location(
        "agendex_workspace_paths", Path(__file__).resolve().parents[2] / "system/workspace_paths.py")
    _PATHS_MODULE = importlib.util.module_from_spec(_PATHS_SPEC)
    sys.modules[_PATHS_SPEC.name] = _PATHS_MODULE
    _PATHS_SPEC.loader.exec_module(_PATHS_MODULE)
resolve_recorded_path = sys.modules["agendex_workspace_paths"].resolve_recorded_path


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
SCHEMA_VERSION = 1
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 60
MAX_REDIRECTS = 5
MAX_CONSECUTIVE_NETWORK_FAILURES = 5
MAX_INLINE_README_BYTES = 1024 * 1024
MAX_README_BYTES = 100 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * MAX_INLINE_README_BYTES + 65536
RATE_LIMIT_HEADERS = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
                      "X-RateLimit-Resource", "Retry-After")
LOGGER = logging.getLogger("readme_fetch")
NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
# 这些状态已经获得明确的采集结果；缺少原文仍需在摘要中单列。
TERMINAL = {"ok", "repository_unavailable", "repository_not_public",
            "readme_not_found", "too_large", "content_unavailable"}
INCOMPLETE = {"too_large", "content_unavailable", "request_failed",
              "content_invalid", "identity_mismatch", "request_deferred"}


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def append_jsonl(path: Path, value: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_journal(path: Path):
    """只修复被中断的最后一行；文件中部损坏必须保留并报错。"""
    with path.open("r+b") as stream:
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                return
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("记录不是对象")
            except (ValueError, UnicodeDecodeError) as error:
                if not line.endswith(b"\n") and not stream.read(1):
                    stream.seek(offset)
                    stream.truncate()
                    LOGGER.warning("已移除 %s 中断留下的不完整末行", path.name)
                    return
                raise ValueError(f"{path.name} 中部存在损坏记录") from error
            if not line.endswith(b"\n"):
                stream.write(b"\n")
            yield value


@contextmanager
def output_lock(directory: Path):
    """操作系统释放锁，进程意外退出后无需手动删除遗留锁文件。"""
    with (directory / ".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("输出目录正在被另一个采集进程使用") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def valid_id(value: Any) -> bool:
    return type(value) is int and value > 0


def valid_name(value: Any) -> bool:
    return (isinstance(value, str) and NAME_PATTERN.fullmatch(value) is not None
            and all(part not in (".", "..") for part in value.split("/")))


def load_plan(source: Path, selection: Optional[Path], limit: Optional[int],
              max_readme_bytes: int = MAX_README_BYTES,
              reuse_source_metadata: bool = False) -> dict:
    """冻结源文件和选择清单；原清单顺序决定 pilot 顺序，不根据旧分数排除。"""
    source = resolve_recorded_path(source)
    selection = resolve_recorded_path(selection) if selection is not None else None
    if type(max_readme_bytes) is not int or not 0 < max_readme_bytes <= MAX_README_BYTES:
        raise ValueError("README 大小上限必须为 1 至 100 MiB 的字节数")
    if type(reuse_source_metadata) is not bool:
        raise ValueError("源元数据复用选项必须是布尔值")
    checksum = hashlib.sha256()
    repositories = {}
    with source.open("rb") as stream:
        for line in stream:
            checksum.update(line)
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("源文件记录必须是对象")
            identifier, name = row.get("id"), row.get("full_name")
            description = row.get("description")
            if not valid_id(identifier) or not valid_name(name):
                raise ValueError("源文件包含无效仓库 ID 或 full_name")
            if description is not None and not isinstance(description, str):
                raise ValueError("源文件 Description 必须是字符串或 null")
            item = {"repository_id": identifier, "source_full_name": name,
                    "has_description": bool(description and description.strip())}
            if reuse_source_metadata:
                private, branch = row.get("private"), row.get("default_branch")
                if private is not None and type(private) is not bool:
                    raise ValueError("源文件 private 字段必须是布尔值或缺失")
                if private is True:
                    raise ValueError("只允许复用公开仓库的源元数据")
                if branch is not None and (not isinstance(branch, str) or not branch
                        or any(ord(char) < 33 for char in branch)):
                    raise ValueError("源文件 default_branch 字段无效")
                # 缺字段仍可安全采集，但该仓库退回原来的 ID 身份核对流程。
                item.update(source_private=private, source_default_branch=branch)
            if identifier in repositories and repositories[identifier] != item:
                raise ValueError("同一仓库 ID 的源记录存在冲突")
            repositories[identifier] = item
    selection_hash, selected_ids = None, None
    if selection:
        selection_bytes = selection.read_bytes()
        selection_hash = hashlib.sha256(selection_bytes).hexdigest()
        names = {row["source_full_name"].casefold(): identifier
                 for identifier, row in repositories.items()}
        selected_ids = set()
        for line in selection_bytes.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            identifier, name = None, None
            if isinstance(value, dict):
                identifier = value.get("repository_id", value.get("id"))
                name = value.get("full_name")
            elif valid_id(value):
                identifier = value
            elif isinstance(value, str):
                name = value
            if identifier is None and valid_name(name):
                identifier = names.get(name.casefold())
            if not valid_id(identifier) or identifier not in repositories:
                raise ValueError("选择清单含未知或无效仓库 ID/name")
            if name is not None and (not valid_name(name) or
                    name.casefold() != repositories[identifier]["source_full_name"].casefold()):
                raise ValueError("选择清单的仓库 ID 与名称不一致")
            selected_ids.add(identifier)
    eligible = [row for identifier, row in repositories.items()
                if selected_ids is None or identifier in selected_ids]
    chosen = eligible if limit is None else eligible[:limit]
    plan = {"schema_version": SCHEMA_VERSION, "source": str(source),
            "source_sha256": checksum.hexdigest(), "selection": str(selection) if selection else None,
            "selection_sha256": selection_hash, "limit": limit,
            "scope": "limited_selection" if limit is not None or selection else "all_source_repositories",
            "selection_limited": limit is not None or selection is not None,
            "source_repositories": len(repositories),
            "source_without_description": sum(not row["has_description"] for row in repositories.values()),
            "eligible_before_limit": len(eligible), "selected_repositories": len(chosen),
            "missing_description_policy": "include", "max_readme_bytes": max_readme_bytes,
            "repositories": chosen}
    # 关闭时不增加配置键，使历史冻结配置能够逐字段恢复，旧任务不会切换模式。
    if reuse_source_metadata:
        plan["reuse_source_metadata"] = True
    return plan


def matches_frozen_plan(frozen: dict, current: dict) -> bool:
    """目录重组可改变路径拼写；选择范围、内容哈希及采集设置仍须完全一致。"""
    normalized = dict(current)
    for key in ("source", "selection"):
        previous, supplied = frozen.get(key), current.get(key)
        if previous is None or supplied is None:
            if previous is not supplied:
                return False
        elif (not isinstance(previous, str) or not isinstance(supplied, str)
              or resolve_recorded_path(previous) != resolve_recorded_path(supplied)):
            return False
        normalized[key] = previous
    return frozen == normalized


class RequestFailure(Exception):
    def __init__(self, category: str, http_status: Optional[int] = None):
        self.category, self.http_status = category, http_status
        super().__init__(category)


class PauseFetch(RequestFailure):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def safe_api_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname == "api.github.com"
                and parts.port in (None, 443) and not parts.username and not parts.password
                and not parts.fragment and not any(ord(char) < 33 for char in url))
    except ValueError:
        return False


def pinned_blob_url(url: str, sha: str) -> bool:
    """重定向后仍须指向同一不可变 blob，不能退回默认分支内容地址。"""
    if not safe_api_url(url):
        return False
    parts = urlsplit(url)
    return (not parts.query and parts.path.endswith("/git/blobs/" + sha))


class GitHubClient:
    """共享请求预算与全局间隔；HTTP 在锁外进行，日志逐条串行落盘。"""
    def __init__(self, token: Optional[str], log_path: Path,
                 max_requests: Optional[int], interval: float):
        self.token, self.log_path = token, log_path
        self.max_requests, self.interval = max_requests, interval
        self.request_count, self.last_request = 0, None
        self.opener = build_opener(NoRedirect())
        self._default_opener = self.opener
        self._thread_local = threading.local()
        self._request_lock, self._log_lock = threading.RLock(), threading.Lock()
        self._repository_requests = Counter()
        self._pause = None
        self.parallel = False

    def pause(self, category: str, http_status: Optional[int] = None) -> None:
        """Stop new attempts across workers, while allowing in-flight GETs to finish."""
        with self._request_lock:
            if self._pause is None:
                self._pause = PauseFetch(category, http_status)

    def check_pause(self) -> None:
        with self._request_lock:
            if (self.log_path.parent / "STOP").exists() and self._pause is None:
                self._pause = PauseFetch("stop_file")
            if self._pause is not None:
                raise self._pause

    def repository_requests(self, identifier: int) -> int:
        with self._request_lock:
            return self._repository_requests[identifier]

    def reserve_request(self, identifier: int) -> int:
        """Reserve one attempt before I/O; parallel workers cannot overspend a budget."""
        with self._request_lock:
            self.check_pause()
            if self.max_requests is not None and self.request_count >= self.max_requests:
                self.pause("request_budget")
                self.check_pause()
            if self.last_request is not None:
                remaining = self.interval - (time.monotonic() - self.last_request)
                while remaining > 0:
                    # A STOP file remains responsive even with a deliberately long interval.
                    time.sleep(min(remaining, .2))
                    self.check_pause()
                    remaining = self.interval - (time.monotonic() - self.last_request)
            self.last_request = time.monotonic()
            self.request_count += 1
            self._repository_requests[identifier] += 1
            return self.request_count

    def log_event(self, event: dict) -> None:
        with self._log_lock:
            append_jsonl(self.log_path, event)

    def request_opener(self):
        # Keep the sequential/injected opener contract used by existing callers/tests.
        if not self.parallel or self.opener is not self._default_opener:
            return self.opener
        if not hasattr(self._thread_local, "opener"):
            self._thread_local.opener = build_opener(NoRedirect())
        return self._thread_local.opener

    def get_json(self, url: str, identifier: int, stage: str):
        return self._get(url, identifier, stage, "application/vnd.github+json",
                         MAX_RESPONSE_BYTES, parse_json=True)

    def get_raw(self, url: str, identifier: int, stage: str, max_bytes: int,
                blob_sha: Optional[str] = None):
        """只向 API 同域发送认证；原文保持 bytes，不经过 JSON 或文本解码。"""
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_README_BYTES:
            raise ValueError("原文响应大小上限无效")
        if blob_sha is not None and (not isinstance(blob_sha, str)
                or not re.fullmatch(r"[0-9a-f]{40}", blob_sha)
                or not pinned_blob_url(url, blob_sha)):
            raise RequestFailure("invalid_pinned_blob_url")
        return self._get(url, identifier, stage, "application/vnd.github.raw+json",
                         max_bytes, parse_json=False, pinned_blob_sha=blob_sha)

    def _get(self, url: str, identifier: int, stage: str, accept: str,
             max_bytes: int, parse_json: bool, pinned_blob_sha: Optional[str] = None):
        current, redirects, attempt = url, 0, 0
        while True:
            if not safe_api_url(current):
                raise RequestFailure("unsafe_redirect")
            request_number = self.reserve_request(identifier)
            attempt += 1
            event = {"time": utc_now(), "repository_id": identifier, "stage": stage,
                     "url": current, "attempt": attempt,
                     "request_this_invocation": request_number}
            headers = {"Accept": accept,
                       "X-GitHub-Api-Version": API_VERSION,
                       "User-Agent": "Agendex-README-Fetch/0.1"}
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            delay, retry = 2 ** (attempt - 1), False
            try:
                with self.request_opener().open(Request(current, headers=headers), timeout=30) as response:
                    for key in RATE_LIMIT_HEADERS:
                        if response.headers.get(key) is not None:
                            event[key.lower()] = response.headers[key]
                    body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise RequestFailure("response_too_large")
                data = json.loads(body) if parse_json else body
                if parse_json and not isinstance(data, dict):
                    raise ValueError("响应不是对象")
                event.update(status="ok", http_status=200)
                self.log_event(event)
                return data, current
            except HTTPError as error:
                event.update(status="http_error", http_status=error.code)
                for key in RATE_LIMIT_HEADERS:
                    if error.headers.get(key) is not None:
                        event[key.lower()] = error.headers[key]
                if error.code in (301, 302, 303, 307, 308):
                    location = error.headers.get("Location")
                    target = urljoin(current, location) if location else ""
                    if (not safe_api_url(target) or redirects >= MAX_REDIRECTS
                            or (pinned_blob_sha is not None
                                and not pinned_blob_url(target, pinned_blob_sha))):
                        event["error_type"] = "unsafe_or_excessive_redirect"
                        self.log_event(event)
                        raise RequestFailure("unsafe_or_excessive_redirect", error.code)
                    self.log_event(event)
                    current, redirects, attempt = target, redirects + 1, 0
                    continue
                # 仅在内存中识别限流原因，不把响应正文或异常字符串写入日志。
                message = error.read(65536).decode("utf-8", errors="replace").lower()
                limited = error.code == 429 or (error.code == 403 and (
                    error.headers.get("X-RateLimit-Remaining") == "0"
                    or error.headers.get("Retry-After") is not None or "rate limit" in message))
                category = "rate_limited" if limited else "http_error"
                if limited:
                    try:
                        retry_after = float(error.headers.get("Retry-After", "0"))
                        reset = float(error.headers.get("X-RateLimit-Reset", "0")) - time.time() + 1
                        delay = max(1, retry_after, reset if error.headers.get(
                            "X-RateLimit-Remaining") == "0" else 0)
                        if not math.isfinite(delay):
                            delay = MAX_RETRY_WAIT + 1
                    except ValueError:
                        delay = MAX_RETRY_WAIT + 1
                    if delay <= 1 and not error.headers.get("Retry-After"):
                        delay = MAX_RETRY_WAIT
                event["error_type"] = category
                self.log_event(event)
                if error.code in (401, 403) and not limited:
                    self.pause("authentication_or_permission", error.code)
                    raise PauseFetch("authentication_or_permission", error.code)
                if limited and (self.parallel or delay > MAX_RETRY_WAIT or attempt >= MAX_ATTEMPTS):
                    self.pause("rate_limited", error.code)
                    raise PauseFetch("rate_limited", error.code)
                retry = limited or 500 <= error.code < 600
                failure = RequestFailure(category, error.code)
            except RequestFailure as error:
                event.update(status=error.category, error_type=error.category)
                self.log_event(event)
                raise
            except (URLError, TimeoutError, OSError, HTTPException) as error:
                event.update(status="network_error", error_type=type(error).__name__)
                self.log_event(event)
                retry, failure = True, RequestFailure("network_error")
            except (ValueError, UnicodeDecodeError) as error:
                event.update(status="invalid_response", error_type=type(error).__name__)
                self.log_event(event)
                retry, failure = True, RequestFailure("invalid_response")
            if not retry or attempt >= MAX_ATTEMPTS:
                raise failure
            if self.max_requests is not None and self.request_count >= self.max_requests:
                raise PauseFetch("request_budget")
            time.sleep(delay)


def validate_readme_metadata(data: dict, max_bytes: int) -> None:
    size, sha, path = data.get("size"), data.get("sha"), data.get("path")
    if type(size) is not int or size < 0 or not isinstance(path, str) or not path:
        raise RequestFailure("invalid_readme_metadata")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RequestFailure("invalid_blob_sha")
    if data.get("type") != "file":
        raise RequestFailure("invalid_readme_type")
    if size > max_bytes or size > MAX_README_BYTES:
        raise RequestFailure("readme_too_large")


def verify_readme_bytes(data: dict, raw: bytes, max_bytes: int) -> bytes:
    """校验完整内容和 Git blob SHA；blob SHA 不是分支或 commit SHA。"""
    validate_readme_metadata(data, max_bytes)
    if len(raw) > max_bytes or len(raw) > MAX_README_BYTES:
        raise RequestFailure("readme_too_large")
    if len(raw) != data["size"]:
        raise RequestFailure("content_length_mismatch")
    expected = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if expected != data["sha"]:
        raise RequestFailure("blob_sha_mismatch")
    return raw


def decode_readme(data: dict, max_bytes: int = MAX_README_BYTES):
    validate_readme_metadata(data, max_bytes)
    if (data.get("encoding") != "base64" or not isinstance(data.get("content"), str)
            or (data["size"] > 0 and not data["content"].strip())):
        raise RequestFailure("content_unavailable")
    try:
        raw = base64.b64decode("".join(data["content"].split()).encode("ascii"), validate=True)
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise RequestFailure("invalid_base64") from error
    return verify_readme_bytes(data, raw, max_bytes)


class ReadmeFetcher:
    def __init__(self, directory: Path, config: dict, client: GitHubClient, workers: int = 1):
        if type(workers) is not int or not 1 <= workers <= 32:
            raise ValueError("并发数必须为 1 至 32")
        self.directory, self.config, self.client = resolve_recorded_path(directory), config, client
        self.workers = workers
        self.client.parallel = workers > 1
        self._results_lock = threading.RLock()
        self.rows = {row["repository_id"]: row for row in config["repositories"]}
        self.results = {}
        self.previous_requests = sum(1 for _ in read_journal(directory / "requests.jsonl"))
        for record in read_journal(directory / "readmes.jsonl"):
            identifier = record.get("repository_id")
            if not valid_id(identifier) or identifier not in self.rows or not isinstance(record.get("status"), str):
                raise ValueError("README 结果中存在未知仓库或无效状态")
            self.results[identifier] = record
        for record in self.results.values():
            if record["status"] == "ok":
                self.verify_saved(record)
        self.counts = Counter(record["status"] for record in self.results.values())

    def verify_saved(self, record: dict) -> None:
        relative = record.get("content_file")
        if not isinstance(relative, str):
            raise ValueError("已保存 README 缺少原文路径")
        path = (self.directory / relative).resolve()
        if self.directory.resolve() not in path.parents:
            raise ValueError("已保存 README 路径超出输出目录")
        try:
            raw = path.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
            if sha != record["sha256"] or blob != record["blob_sha"] or len(raw) != record["size_bytes"]:
                raise ValueError("原文哈希或长度不匹配")
        except (OSError, KeyError, ValueError) as error:
            raise ValueError("已保存 README 原文缺失或损坏；请保留现场检查") from error

    def save(self, record: dict) -> None:
        with self._results_lock:
            append_jsonl(self.directory / "readmes.jsonl", record)
            previous = self.results.get(record["repository_id"])
            if previous:
                self.counts[previous["status"]] -= 1
            self.results[record["repository_id"]] = record
            self.counts[record["status"]] += 1

    def checkpoint(self, status: str, reason: Optional[str] = None) -> None:
        with self._results_lock:
            counts = self.counts.copy()
            pending = len(self.rows) - sum(counts[status] for status in TERMINAL)
            recorded = len(self.results)
        summary = {"schema_version": SCHEMA_VERSION, "status": status, "reason": reason,
                   "updated_at": utc_now(), "scope": self.config["scope"],
                   "selection_limited": self.config["selection_limited"],
                   "selected_repositories": len(self.rows), "recorded_repositories": recorded,
                   "pending_repositories": pending, "status_counts": dict(counts),
                   "requests_this_invocation": self.client.request_count,
                   "workers": self.workers,
                   "requests_total": self.previous_requests + self.client.request_count,
                   "all_selected_processed": pending == 0,
                   "all_selected_content_retrieved": counts["ok"] == len(self.rows),
                   "full_source_scope_complete": status == "plan_complete" and not self.config["selection_limited"],
                   "note": "状态仅描述 README 采集；缺失、不可用和失败均不表示不是 Agent。"}
        atomic_json(self.directory / "state.json", summary)
        atomic_json(self.directory / "summary.json", summary)

    def checked_repository(self, identifier: int, record: dict) -> dict:
        """需要时以稳定 ID 重查身份；只有这条路径可以记录本次身份核对时间。"""
        url = API_ROOT + "/repositories/" + str(identifier)
        record["repository_api_url"] = url
        repository, resolved = self.client.get_json(url, identifier, "repository")
        record["repository_resolved_url"] = resolved
        if type(repository.get("id")) is not int or repository["id"] != identifier:
            raise RequestFailure("canonical_repository_id_mismatch")
        name = repository.get("full_name")
        if not valid_name(name) or type(repository.get("private")) is not bool:
            raise RequestFailure("invalid_repository_metadata")
        record.update(canonical_full_name=name, repository_checked_at=utc_now(),
                      repository_html_url="https://github.com/" + name,
                      repository_identity_source="repository_api",
                      repository_identity_verified_this_run=True)
        return repository

    def get_readme_metadata(self, identifier: int, repository: dict, record: dict):
        name = repository["full_name"]
        url = API_ROOT + "/repos/" + quote(name, safe="/") + "/readme"
        record.update(readme_api_url=url, requested_ref=None,
                      default_branch=repository.get("default_branch"))
        return self.client.get_json(url, identifier, "readme")

    @staticmethod
    def readme_matches_name(data: dict, resolved: str, name: str) -> bool:
        """探测改名等身份疑点；仅检查地址归属，不把 README 内容当指令。"""
        expected = name.casefold()
        if (not safe_api_url(resolved) or unquote(urlsplit(resolved).path).casefold()
                != "/repos/" + expected + "/readme"):
            return False
        for key, hostname in (("url", "api.github.com"), ("git_url", "api.github.com"),
                              ("html_url", "github.com")):
            value = data.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                return False
            try:
                parts = urlsplit(value)
            except ValueError:
                return False
            segments = unquote(parts.path).strip("/").split("/")
            offset = 1 if hostname == "api.github.com" else 0
            if (parts.scheme != "https" or parts.hostname != hostname
                    or parts.username or parts.password
                    or len(segments) < offset + 2
                    or (offset and segments[0] != "repos")
                    or "/".join(segments[offset:offset + 2]).casefold() != expected):
                return False
        return True

    def fetch_one(self, row: dict) -> None:
        identifier = row["repository_id"]
        record = {"schema_version": SCHEMA_VERSION, "repository_id": identifier,
                  "source_full_name": row["source_full_name"], "fetched_at": utc_now()}
        requests_before = self.client.repository_requests(identifier)

        def save_record():
            record["requests_this_repository"] = self.client.repository_requests(identifier) - requests_before
            self.save(record)

        stage = "repository"
        try:
            reuse_enabled = self.config.get("reuse_source_metadata", False)
            use_snapshot = (reuse_enabled and row.get("source_private") is False
                            and isinstance(row.get("source_default_branch"), str)
                            and bool(row["source_default_branch"]))
            if use_snapshot:
                repository = {"id": identifier, "full_name": row["source_full_name"],
                              "private": False, "default_branch": row["source_default_branch"]}
                record.update(canonical_full_name=repository["full_name"],
                              repository_html_url="https://github.com/" + repository["full_name"],
                              repository_identity_source="source_snapshot",
                              repository_identity_verified_this_run=False,
                              source_metadata_sha256=self.config["source_sha256"])
            else:
                if reuse_enabled:
                    record["metadata_fallback_reason"] = "source_metadata_incomplete"
                repository = self.checked_repository(identifier, record)
            if repository["private"]:
                record.update(status="repository_not_public", reason="public_dataset_scope")
                save_record()
                return
            stage = "readme"
            fallback = None
            try:
                data, resolved = self.get_readme_metadata(identifier, repository, record)
            except PauseFetch:
                raise
            except RequestFailure as error:
                if not use_snapshot or error.http_status != 404:
                    raise
                fallback = "readme_not_found_at_snapshot_name"
            else:
                if use_snapshot and not self.readme_matches_name(data, resolved, repository["full_name"]):
                    fallback = "readme_repository_redirect_or_mismatch"
            if fallback:
                # 改名/404 不直接记为 README 缺失；先按 ID 找回正确仓库，再取原文。
                record["metadata_fallback_reason"] = fallback
                stage = "repository"
                repository = self.checked_repository(identifier, record)
                if repository["private"]:
                    record.update(status="repository_not_public", reason="public_dataset_scope")
                    save_record()
                    return
                stage = "readme"
                data, resolved = self.get_readme_metadata(identifier, repository, record)
                if not self.readme_matches_name(data, resolved, repository["full_name"]):
                    raise RequestFailure("readme_repository_mismatch")
            record.update(readme_resolved_url=resolved, readme_path=data.get("path"),
                          blob_sha=data.get("sha"), reported_size_bytes=data.get("size"),
                          readme_html_url=data.get("html_url"))
            max_bytes = self.config["max_readme_bytes"]
            try:
                raw = decode_readme(data, max_bytes)
                record["content_transfer"] = "base64"
            except RequestFailure as error:
                if error.category != "content_unavailable":
                    raise
                stage = "readme_raw"
                # 元数据和正文之间，默认分支可能更新。按已经验证的 blob SHA
                # 获取不可变对象，避免把不同版本的正文与先前长度/SHA 进行比较。
                blob_url = (API_ROOT + "/repos/" + quote(repository["full_name"], safe="/")
                            + "/git/blobs/" + data["sha"])
                record.update(readme_raw_api_url=blob_url, readme_raw_ref_kind="git_blob_sha")
                raw, raw_resolved = self.client.get_raw(
                    blob_url, identifier, stage, max_bytes, blob_sha=data["sha"])
                record["readme_raw_resolved_url"] = raw_resolved
                raw = verify_readme_bytes(data, raw, max_bytes)
                record["content_transfer"] = "api_raw"
            checksum = hashlib.sha256(raw).hexdigest()
            relative = Path("content") / str(identifier) / (checksum + ".bin")
            path = self.directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if self.directory.resolve() not in path.resolve().parents:
                raise ValueError("README 原文路径超出输出目录")
            temporary = path.with_suffix(".tmp")
            with temporary.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            try:
                raw.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                encoding = "non_utf8"
            record.update(status="ok", content_complete=True, content_file=relative.as_posix(),
                          size_bytes=len(raw), sha256=checksum, text_encoding=encoding,
                          fetched_at=utc_now())
        except PauseFetch as error:
            record.update(status="request_deferred", stage=stage, reason=error.category,
                          http_status=error.http_status, content_complete=False)
            save_record()
            raise
        except RequestFailure as error:
            status = "request_failed"
            if error.http_status == 404:
                status = "repository_unavailable" if stage == "repository" else "readme_not_found"
            elif error.category in ("response_too_large", "readme_too_large"):
                status = "too_large"
            elif error.category == "content_unavailable":
                status = "content_unavailable"
            elif error.category in ("canonical_repository_id_mismatch", "readme_repository_mismatch"):
                status = "identity_mismatch"
            elif error.category.startswith(("invalid_readme", "invalid_blob", "invalid_base64", "content_length", "blob_sha")):
                status = "content_invalid"
            record.update(status=status, stage=stage, reason=error.category,
                          http_status=error.http_status, content_complete=False)
        save_record()

    def run_parallel(self, rows) -> None:
        """Keep at most workers repositories in flight; never queue the whole dataset.

        Worker writes use save()'s journal lock. The coordinator alone checkpoints,
        stops dispatching on a pause, and drains in-flight GETs before final status.
        """
        iterator, pending, failure = iter(rows), {}, None
        consecutive_network_failures = 0
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            def submit_next() -> bool:
                self.client.check_pause()
                row = next(iterator, None)
                if row is None:
                    return False
                pending[executor.submit(self.fetch_one, row)] = row["repository_id"]
                return True

            try:
                for _ in range(self.workers):
                    if not submit_next():
                        break
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        identifier = pending.pop(future)
                        try:
                            future.result()
                        except Exception as error:
                            if failure is None:
                                failure = error
                            category = error.category if isinstance(error, PauseFetch) else "worker_failure"
                            self.client.pause(category)
                        result = self.results.get(identifier, {})
                        if result.get("status") == "request_failed" and result.get("reason") == "network_error":
                            consecutive_network_failures += 1
                        else:
                            consecutive_network_failures = 0
                        if consecutive_network_failures >= MAX_CONSECUTIVE_NETWORK_FAILURES and failure is None:
                            failure = PauseFetch("network_unavailable")
                            self.client.pause("network_unavailable")
                    self.checkpoint("running")
                    if failure is None:
                        for _ in done:
                            try:
                                if not submit_next():
                                    break
                            except PauseFetch as error:
                                failure = error
                                break
            except BaseException:
                self.client.pause("user_interrupt")
                for future in pending:
                    future.cancel()
                raise
        if failure is not None:
            raise failure

    def run(self) -> int:
        status, reason, code = "running", None, 0
        consecutive_network_failures = 0
        self.checkpoint(status)
        try:
            remaining = [row for identifier, row in self.rows.items()
                         if self.results.get(identifier, {}).get("status") not in TERMINAL]
            if self.workers > 1:
                self.run_parallel(remaining)
            else:
                for row in remaining:
                    self.client.check_pause()
                    self.fetch_one(row)
                    result = self.results[row["repository_id"]]
                    if result["status"] == "request_failed" and result.get("reason") == "network_error":
                        consecutive_network_failures += 1
                    else:
                        consecutive_network_failures = 0
                    if consecutive_network_failures >= MAX_CONSECUTIVE_NETWORK_FAILURES:
                        raise PauseFetch("network_unavailable")
                    self.checkpoint("running")
            unfinished = any(record["status"] in INCOMPLETE for record in self.results.values())
            status = "incomplete" if unfinished else (
                "selection_complete" if self.config["selection_limited"] else "plan_complete")
            code = 2 if unfinished else 0
        except PauseFetch as error:
            status, reason, code = "paused", error.category, 2
        except KeyboardInterrupt:
            status, reason, code = "paused", "user_interrupt", 130
        except Exception as error:
            status, reason, code = "failed", type(error).__name__, 1
            raise
        finally:
            self.checkpoint(status, reason)
        LOGGER.info("状态 %s；已取得完整 README %s/%s；本次请求 %s",
                    status, sum(row["status"] == "ok" for row in self.results.values()),
                    len(self.rows), self.client.request_count)
        return code


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def interval_value(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("必须是大于等于 0 的有限秒数")
    return number


def readme_size_value(value: str) -> int:
    number = positive_int(value)
    if number > MAX_README_BYTES:
        raise argparse.ArgumentTypeError("不能超过 100 MiB")
    return number


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="原始 repositories.jsonl 文件")
    parser.add_argument("--selection", type=Path, help="可选 ID/name JSONL 清单，按仓库 ID 回连源文件")
    parser.add_argument("--limit", type=positive_int, help="冻结前 N 个符合输入范围的仓库，明确标为受限试采集")
    parser.add_argument("--output-dir", type=Path, help="输出目录，默认源文件旁的 readmes 目录")
    parser.add_argument("--max-requests", type=positive_int, help="本次 HTTP 尝试预算，包含元数据、重定向和重试")
    parser.add_argument("--max-readme-bytes", type=readme_size_value,
                        help="README 原文字节上限，默认 100 MiB；恢复时默认沿用冻结配置")
    identity_mode = parser.add_mutually_exclusive_group()
    identity_mode.add_argument("--reuse-source-metadata", dest="reuse_source_metadata",
                               action="store_true", default=None,
                               help="复用源文件公开性和默认分支，正常每库只请求 README；异常时补查 ID")
    identity_mode.add_argument("--no-reuse-source-metadata", dest="reuse_source_metadata",
                               action="store_false", help="始终先按仓库 ID 核对身份（历史默认模式）")
    parser.add_argument("--interval", type=interval_value, default=1.0, help="相邻请求的最小间隔秒数，默认 1")
    parser.add_argument("--workers", type=positive_int, default=1,
                        help="在途仓库数，1至32；共享请求间隔和预算，默认1；并发模式遇限流即暂停")
    parser.add_argument("--resume", action="store_true", help="验证输入和原文哈希后恢复；需沿用原 selection/limit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        source = resolve_recorded_path(args.source)
        selection = resolve_recorded_path(args.selection) if args.selection else None
        directory = resolve_recorded_path(args.output_dir or source.parent / "readmes")
        directory.mkdir(parents=True, exist_ok=True)
        with output_lock(directory):
            config_path = directory / "config.json"
            frozen = json.loads(config_path.read_text(encoding="utf-8")) if args.resume else None
            max_bytes = args.max_readme_bytes
            if max_bytes is None:
                max_bytes = frozen["max_readme_bytes"] if frozen is not None else MAX_README_BYTES
            reuse_metadata = args.reuse_source_metadata
            if reuse_metadata is None:
                reuse_metadata = frozen.get("reuse_source_metadata", False) if frozen is not None else False
            config = load_plan(source, selection, args.limit, max_bytes, reuse_metadata)
            if args.resume:
                if not matches_frozen_plan(frozen, config):
                    raise ValueError("源文件、选择清单、limit 或采集模式与冻结配置不同，请沿用原配置或使用新目录")
                if not all((directory / name).is_file() for name in ("readmes.jsonl", "requests.jsonl")):
                    raise ValueError("恢复所需的结果或请求记录缺失")
                # 恢复沿用原始来源记录，不为目录迁移改写已冻结配置。
                config = frozen
            else:
                if any(path.name != ".lock" for path in directory.iterdir()):
                    raise ValueError("输出目录已有内容，请指定新目录或使用 --resume")
                (directory / "readmes.jsonl").touch()
                (directory / "requests.jsonl").touch()
                atomic_json(config_path, config)
            # 完成所有本地输入和恢复校验后才读取凭据。
            client = GitHubClient(None, directory / "requests.jsonl", args.max_requests, args.interval)
            fetcher = ReadmeFetcher(directory, config, client, workers=args.workers)
            client.token = github_token()
            return fetcher.run()
    except (OSError, ValueError, KeyError) as error:
        LOGGER.error("无法安全采集：%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
