#!/usr/bin/env python3
"""Discover candidate repositories through GitHub README keyword searches.

Split searches by repository creation time, record the raw responses, and
export a deduplicated metadata list when every search shard is complete.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from http.client import HTTPException
from uuid import uuid4


DEFAULT_QUERIES = (
    "agent in:readme stars:>=1000 fork:true is:public",
    "assistant in:readme stars:>=1000 fork:true is:public",
)
PAGE_SIZE = 100
SEARCH_LIMIT = 1000
STATE_VERSION = 1
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 60
API_URL = "https://api.github.com/search/repositories"
LOGGER = logging.getLogger("readme_search_v2")

# Shared GitHub credentials and timestamp helpers.
_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))
from system.experiment_tools.github import github_token, format_timestamp, utc_now


class CollectionError(Exception):
    """数据或网络异常：保留尚未完成的分片供恢复。"""


class RequestBudgetReached(Exception):
    """本次 HTTP 尝试次数达到预算。"""


class PauseRequested(Exception):
    """用户 STOP 或需要稍后恢复的限流。"""


def atomic_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def write_json(path: Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def append_jsonl(path: Path, value: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def journal_records(path: Path):
    """仅修复被中断的最后一行；中间损坏属于完整性错误。"""
    if not path.exists():
        return
    with path.open("r+b") as stream:
        while True:
            position = stream.tell()
            line = stream.readline()
            if not line:
                return
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("journal item must be an object")
            except (ValueError, TypeError) as error:
                if not line.endswith(b"\n") and not stream.read(1):
                    atomic_bytes(path.with_name(path.name + ".interrupted-" + uuid4().hex), line)
                    stream.seek(position)
                    stream.truncate()
                    return
                raise CollectionError("请求日志存在损坏，无法安全恢复") from error
            if not line.endswith(b"\n"):
                stream.seek(0, 2)
                stream.write(b"\n")
            yield value


@contextmanager
def directory_lock(directory: Path):
    """锁由操作系统持有；进程退出会释放，残留锁文件不代表仍在运行。"""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".collector.lock").open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise CollectionError("该输出目录已有采集进程，请勿同时写入") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class TimeWindow:
    """秒级闭区间；拆分后既不重叠，也不遗漏边界。"""

    start: int
    end: int

    def split(self) -> Optional[tuple[TimeWindow, TimeWindow]]:
        if self.start == self.end:
            return None
        middle = (self.start + self.end) // 2
        return TimeWindow(self.start, middle), TimeWindow(middle + 1, self.end)

    def query(self, base_query: str) -> str:
        return (base_query + " created:" + format_timestamp(self.start)
                + ".." + format_timestamp(self.end))


def initial_state(end: Optional[int] = None) -> dict:
    window = {"start": 0, "end": int(time.time()) if end is None else end}
    return {
        "version": STATE_VERSION, "dataset_version": "v2", "started_at": utc_now(),
        "creation_range": window.copy(), "queries": [
            {"base_query": query, "pending": [window.copy()], "completed": [],
             "unresolved": [], "root_total_count": None}
            for query in DEFAULT_QUERIES],
    }


def validate_response(data: Any) -> None:
    if not isinstance(data, dict):
        raise ValueError("响应必须为对象")
    if type(data.get("total_count")) is not int or data["total_count"] < 0:
        raise ValueError("缺少有效 total_count")
    if type(data.get("incomplete_results")) is not bool:
        raise ValueError("缺少 incomplete_results")
    if not isinstance(data.get("items"), list) or len(data["items"]) > PAGE_SIZE:
        raise ValueError("仓库列表无效")


def query_parts(query: str) -> tuple[str, TimeWindow]:
    match = re.fullmatch(r"(.+) created:(\S+)\.\.(\S+)", query)
    if not match:
        raise CollectionError("日志中的查询缺少创建时间范围")
    try:
        bounds = [int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
                  for value in match.groups()[1:]]
    except (ValueError, OverflowError) as error:
        raise CollectionError("查询时间格式无效") from error
    return match.group(1), TimeWindow(*bounds)


def validate_items(items: list, window: TimeWindow) -> None:
    """索引匹配内容不在元数据中，故不以 Description 替代 README 关键词验证。"""
    for item in items:
        try:
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
                raise ValueError("仓库 ID 无效")
            if item.get("private") is not False:
                raise ValueError("非公开仓库")
            stars = item.get("stargazers_count")
            if type(stars) is not int or stars < 1000:
                raise ValueError("Star 数不满足查询")
            created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            if created.tzinfo is None or not window.start <= created.timestamp() <= window.end:
                raise ValueError("创建时间不在分片内")
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
            raise CollectionError("仓库元数据无法与公开性、Star 或创建时间条件核对") from error


class NoRedirect(HTTPRedirectHandler):
    """搜索端点不需要重定向，拒绝将认证头交给其他地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubClient:
    """串行请求、限流与原始响应落盘；不负责分片调度。"""

    def __init__(self, token: Optional[str], log_path: Path,
                 max_requests: Optional[int] = None, interval: Optional[float] = None) -> None:
        self.token, self.log_path, self.max_requests = token, log_path, max_requests
        self.interval = interval if interval is not None else (2.1 if token else 6.1)
        self.request_count, self.last_request = 0, 0.0
        self.last_event: Optional[dict] = None
        self.opener = build_opener(NoRedirect())
        (log_path.parent / "responses").mkdir(parents=True, exist_ok=True)

    def _check_stop(self) -> None:
        if (self.log_path.parent / "STOP").exists():
            raise PauseRequested("检测到 STOP 文件")

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check_stop()
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    def _save_body(self, content: bytes, suffix: str = ".json") -> dict:
        relative = "responses/" + uuid4().hex + suffix
        atomic_bytes(self.log_path.parent / relative, content)
        return {"response_path": relative, "sha256": hashlib.sha256(content).hexdigest()}

    def search(self, query: str, page: int) -> dict:
        url = API_URL + "?" + urlencode({"q": query, "per_page": PAGE_SIZE,
            "page": page, "sort": "updated", "order": "asc"})
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Agendex-Search-v2/1.0"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        for attempt in range(MAX_ATTEMPTS):
            self._check_stop()
            if self.max_requests is not None and self.request_count >= self.max_requests:
                raise RequestBudgetReached()
            self._wait(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.request_count += 1
            event = {"time": utc_now(), "query": query, "page": page, "attempt": attempt + 1}
            delay, retryable, limited = 2 ** attempt, True, False
            try:
                with self.opener.open(Request(url, headers=headers), timeout=30) as response:
                    raw = response.read()
                    event.update(http_status=response.status, **self._save_body(raw))
                data = json.loads(raw)
                validate_response(data)
                _, window = query_parts(query)
                validate_items(data["items"], window)
                event.update(status="ok", total_count=data["total_count"],
                             incomplete_results=data["incomplete_results"])
                append_jsonl(self.log_path, event)
                self.last_event = event
                return data
            except HTTPError as error:
                # 错误正文可能反射凭据，必须先脱敏。请求头从不写入日志。
                body = error.read().decode("utf-8", errors="replace")
                if self.token:
                    body = body.replace(self.token, "[REDACTED]")
                body = re.sub(r"(?:github_pat_|ghp_)[A-Za-z0-9_]+", "[REDACTED]", body)
                event.update(status="http_error", http_status=error.code,
                             **self._save_body(body.encode("utf-8"), ".error.txt"))
                limited = error.code == 429 or (error.code == 403 and (
                    error.headers.get("X-RateLimit-Remaining") == "0"
                    or error.headers.get("Retry-After") is not None or "rate limit" in body.lower()))
                retryable = limited or 500 <= error.code < 600
                if limited:
                    try:
                        after = float(error.headers.get("Retry-After", "0"))
                        reset = (float(error.headers.get("X-RateLimit-Reset", "0")) - time.time() + 1
                                 if error.headers.get("X-RateLimit-Remaining") == "0" else 0)
                        delay = max(after, reset, 1) if after > 0 or reset > 0 else 60
                    except ValueError:
                        delay = 60
                reason = "GitHub 返回 HTTP " + str(error.code)
            except (URLError, TimeoutError, OSError, HTTPException) as error:
                event.update(status="network_error", error_type=type(error).__name__)
                reason = "连接 GitHub 失败"
            except (ValueError, TypeError, CollectionError) as error:
                event.update(status="invalid_response", error_type=type(error).__name__)
                reason = "GitHub 响应未通过验证"
            append_jsonl(self.log_path, event)
            if limited and delay > MAX_RETRY_WAIT:
                raise PauseRequested(reason + "，需稍后恢复")
            if not retryable or attempt + 1 == MAX_ATTEMPTS:
                raise CollectionError(reason + "；进度已保留")
            LOGGER.warning("%s，%.0f 秒后重试", reason, delay)
            self._wait(delay)
        raise AssertionError("unreachable")


class ReadmeCollector:
    """调度两条查询；审计源先落盘，再导出对象，最后提交分片检查点。"""

    def __init__(self, directory: Path, state: dict, client: GitHubClient) -> None:
        self.directory, self.state, self.client = directory, state, client
        self.saved_ids: set[int] = set()
        self.provenance_keys: set[tuple[str, int]] = set()
        self.cache: dict[tuple[str, int], dict] = {}
        self.response_events: dict[str, dict] = {}

    def _validate_state(self) -> None:
        if self.state.get("version") != STATE_VERSION or self.state.get("dataset_version") != "v2":
            raise CollectionError("检查点版本不支持")
        queries = self.state.get("queries")
        if not isinstance(queries, list) or [q.get("base_query") for q in queries] != list(DEFAULT_QUERIES):
            raise CollectionError("已有查询配置与 Search v2 不一致")
        root = self.state["creation_range"]
        for query in queries:
            # 完成、未完成和异常叶子必须共同覆盖完整初始范围，防止丢失任务后误报完成。
            windows = query["pending"] + query["completed"] + query["unresolved"]
            expected = root["start"]
            for item in sorted(windows, key=lambda value: value["start"]):
                if (type(item.get("start")) is not int or type(item.get("end")) is not int
                        or item["start"] != expected or item["end"] < item["start"]):
                    raise CollectionError("检查点分片存在缺口、重叠或无效范围")
                expected = item["end"] + 1
            if expected != root["end"] + 1:
                raise CollectionError("检查点未覆盖完整采集时间范围")

    def _load_response(self, event: dict) -> dict:
        relative = event.get("response_path", "")
        path = (self.directory / relative).resolve()
        if not relative.startswith("responses/") or self.directory.resolve() not in path.parents:
            raise CollectionError("响应路径不在采集目录内")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != event.get("sha256"):
            raise CollectionError("原始响应哈希不匹配")
        data = json.loads(raw)
        validate_response(data)
        base, window = query_parts(event["query"])
        if base not in DEFAULT_QUERIES:
            raise CollectionError("原始响应属于其他采集配置")
        validate_items(data["items"], window)
        if (event.get("total_count") != data["total_count"]
                or event.get("incomplete_results") != data["incomplete_results"]):
            raise CollectionError("请求记录与原始响应不一致")
        return data

    def _write_items(self, data: dict, event: dict, repositories, provenance) -> None:
        base, _ = query_parts(event["query"])
        for item in data["items"]:
            identifier = item["id"]
            if identifier not in self.saved_ids:
                repositories.write(json.dumps(item, ensure_ascii=False) + "\n")
                self.saved_ids.add(identifier)
            key = (base, identifier)
            if key not in self.provenance_keys:
                provenance.write(json.dumps({"repository_id": identifier, "base_query": base,
                    "query": event["query"], "page": event["page"],
                    "response_path": event["response_path"], "response_sha256": event["sha256"],
                    "observed_at": event["time"]}, ensure_ascii=False) + "\n")
                self.provenance_keys.add(key)

    def _recover_exports(self) -> None:
        """从已提交的响应日志重建导出，修复任意导出/检查点间的中断。"""
        self.saved_ids.clear()
        self.provenance_keys.clear()
        self.cache.clear()
        self.response_events.clear()
        repository_tmp = self.directory / "repositories.jsonl.tmp"
        provenance_tmp = self.directory / "provenance.jsonl.tmp"
        with repository_tmp.open("w", encoding="utf-8") as repositories, \
                provenance_tmp.open("w", encoding="utf-8") as provenance:
            for event in journal_records(self.client.log_path):
                if event.get("status") != "ok":
                    continue
                data = self._load_response(event)
                self._write_items(data, event, repositories, provenance)
                self.cache[(event["query"], event["page"])] = event
                self.response_events[event["response_path"]] = event
        for query in self.state["queries"]:
            for completed in query["completed"]:
                sources = completed.get("responses", [])
                if not sources:
                    raise CollectionError("已完成分片缺少响应依据")
                seen = set()
                expected_query = TimeWindow(completed["start"], completed["end"]).query(query["base_query"])
                for page, relative in enumerate(sources, 1):
                    event = self.response_events.get(relative)
                    if not event or event["query"] != expected_query or event["page"] != page:
                        raise CollectionError("已完成分片的响应依据缺失或不匹配")
                    data = self._load_response(event)
                    ids = [item["id"] for item in data["items"]]
                    if (data["incomplete_results"] or data["total_count"] != completed["total_count"]
                            or seen.intersection(ids) or len(set(ids)) != len(ids)):
                        raise CollectionError("已完成分片未通过恢复验证")
                    seen.update(ids)
                if len(seen) != completed["total_count"]:
                    raise CollectionError("已完成分片的计数与原始响应不一致")
        # 所有完成证据验证后才替换导出；失败时保留此前可供排查的文件。
        # 两个替换之间若中断，下一次仍会从原始响应整体重建。
        repository_tmp.replace(self.directory / "repositories.jsonl")
        provenance_tmp.replace(self.directory / "provenance.jsonl")

    def _record_recovery_failure(self, error: Exception) -> None:
        """恢复失败也撤销旧的完成状态，同时保留此前计数与分片证据。"""
        reason = "恢复验证失败：" + str(error)
        self.state.update(status="failed", reason=reason, updated_at=utc_now())
        write_json(self.directory / "state.json", self.state)
        try:
            summary = json.loads((self.directory / "summary.json").read_text(encoding="utf-8"))
            if not isinstance(summary, dict):
                summary = {}
        except (OSError, ValueError):
            summary = {}
        summary.update(dataset_version="v2", status="failed", reason=reason,
                       updated_at=self.state["updated_at"], recovery_validation_failed=True)
        write_json(self.directory / "summary.json", summary)

    def _search(self, query: str, page: int) -> tuple[dict, dict]:
        cached = self.cache.get((query, page))
        if cached:
            return self._load_response(cached), cached
        data = self.client.search(query, page)
        validate_response(data)
        _, window = query_parts(query)
        validate_items(data["items"], window)
        event = getattr(self.client, "last_event", None)
        if not event or event.get("query") != query or event.get("page") != page:
            # 注入离线客户端时也保存完整字典，使恢复路径与生产采集一致。
            raw = (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")
            relative = "responses/" + uuid4().hex + ".json"
            atomic_bytes(self.directory / relative, raw)
            event = {"time": utc_now(), "query": query, "page": page, "status": "ok",
                     "response_path": relative, "sha256": hashlib.sha256(raw).hexdigest(),
                     "total_count": data["total_count"], "incomplete_results": data["incomplete_results"]}
            append_jsonl(self.client.log_path, event)
        with (self.directory / "repositories.jsonl").open("a", encoding="utf-8") as repositories, \
                (self.directory / "provenance.jsonl").open("a", encoding="utf-8") as provenance:
            self._write_items(data, event, repositories, provenance)
        self.cache[(query, page)] = event
        self.response_events[event["response_path"]] = event
        self.checkpoint("running")
        return data, event

    def _collect_window(self, entry: dict, window: TimeWindow) -> tuple[Optional[str], dict]:
        query = window.query(entry["base_query"])
        first, event = self._search(query, 1)
        if asdict(window) == self.state["creation_range"] and entry["root_total_count"] is None:
            entry["root_total_count"] = first["total_count"]
        total, seen, sources = first["total_count"], set(), []
        for page in range(1, max(1, math.ceil(min(total, SEARCH_LIMIT) / PAGE_SIZE)) + 1):
            self._check_stop()
            data, source = (first, event) if page == 1 else self._search(query, page)
            sources.append(source["response_path"])
            if data["incomplete_results"]:
                return "GitHub 报告结果不完整", {}
            if data["total_count"] > SEARCH_LIMIT:
                return "超过单查询 1,000 条返回上限", {}
            if data["total_count"] != total:
                return "分页期间命中数量变化", {}
            ids = [item["id"] for item in data["items"]]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                return "分页重复，可能存在索引漂移", {}
            seen.update(ids)
        if len(seen) != total:
            return "实际 ID 数与接口报告数量不符", {}
        return None, {**asdict(window), "total_count": total, "actual_count": len(seen), "responses": sources,
                      "completed_at": utc_now()}

    def _check_stop(self) -> None:
        if (self.directory / "STOP").exists():
            raise PauseRequested("检测到 STOP 文件")

    def checkpoint(self, status: str, reason: Optional[str] = None) -> None:
        self.state.update(status=status, reason=reason, updated_at=utc_now(),
                          repositories_saved=len(self.saved_ids))
        write_json(self.directory / "state.json", self.state)
        rows = []
        for query in self.state["queries"]:
            rows.append({"base_query": query["base_query"],
                "initial_reported_total": query["root_total_count"],
                "unique_repositories_observed": sum(base == query["base_query"] for base, _ in self.provenance_keys),
                "validated_leaf_total": sum(item["total_count"] for item in query["completed"]),
                "completed_windows": len(query["completed"]), "pending_windows": len(query["pending"]),
                "unresolved_windows": query["unresolved"]})
        write_json(self.directory / "summary.json", {
            "dataset_version": "v2", "status": status, "reason": reason,
            "started_at": self.state["started_at"], "updated_at": self.state["updated_at"],
            "creation_range": self.state["creation_range"], "queries": rows,
            "repositories_saved": len(self.saved_ids), "provenance_relations": len(self.provenance_keys),
            "successful_responses": len(self.response_events),
            "requests_this_invocation": self.client.request_count,
            "note": "仅两条英文关键词；不限制 README 语言。完成指计划分片验证完成，不是严格快照或全部 Agent。"})

    def run(self) -> int:
        self.directory.mkdir(parents=True, exist_ok=True)
        with directory_lock(self.directory):
            (self.directory / "responses").mkdir(exist_ok=True)
            try:
                self._validate_state()
                self._recover_exports()
            except (CollectionError, OSError, ValueError, KeyError, TypeError) as error:
                self._record_recovery_failure(error)
                raise
            self.checkpoint("running")
            status, reason, exit_code = "running", None, 0
            try:
                self._check_stop()
                for entry in self.state["queries"]:
                    # 已无更小时间窗口的异常分片，在显式恢复时重新尝试，不复用旧异常页。
                    for item in entry["unresolved"]:
                        window = TimeWindow(item["start"], item["end"])
                        prefix = window.query(entry["base_query"])
                        for key in list(self.cache):
                            if key[0] == prefix:
                                self.cache.pop(key)
                        entry["pending"].append(asdict(window))
                    entry["unresolved"] = []
                    while entry["pending"]:
                        self._check_stop()
                        window = TimeWindow(**entry["pending"][-1])
                        LOGGER.info("%s；%s 至 %s；已保存 %s 个仓库", entry["base_query"].split()[0],
                                    format_timestamp(window.start), format_timestamp(window.end), len(self.saved_ids))
                        problem, completed = self._collect_window(entry, window)
                        pending = entry["pending"][:-1]
                        if problem:
                            children = window.split()
                            if children:
                                pending.extend(asdict(child) for child in reversed(children))
                            else:
                                entry["unresolved"].append({**asdict(window), "reason": problem})
                                LOGGER.warning("单秒分片未解决：%s", problem)
                        else:
                            entry["completed"].append(completed)
                        entry["pending"] = pending
                        self.checkpoint("running")
                unresolved = any(entry["unresolved"] for entry in self.state["queries"])
                status, exit_code = ("incomplete", 2) if unresolved else ("plan_complete", 0)
            except RequestBudgetReached:
                status, reason, exit_code = "paused", "达到本次请求预算", 2
            except PauseRequested as error:
                status, reason, exit_code = "paused", str(error), 2
            except KeyboardInterrupt:
                status, reason, exit_code = "paused", "用户中断", 130
            except CollectionError as error:
                status, reason, exit_code = "failed", str(error), 1
            except (OSError, ValueError, KeyError, TypeError) as error:
                status, reason, exit_code = "failed", "读写或数据错误：" + type(error).__name__, 1
            finally:
                self.checkpoint(status, reason)
            LOGGER.info("状态 %s；保存 %s 个仓库；输出 %s", status, len(self.saved_ids), self.directory)
            return exit_code


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return result


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).resolve().parent / "output" / "v2")
    parser.add_argument("--resume", action="store_true", help="恢复同一目录内的查询")
    parser.add_argument("--max-requests", type=positive_int, help="本次最多 HTTP 尝试数，含重试")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    directory = _TOPIC.resolve_recorded_path(args.output_dir)
    try:
        if args.resume:
            state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        else:
            if directory.exists() and any(directory.iterdir()):
                parser.error("输出目录已有内容；请指定新目录或使用 --resume")
            state = initial_state()
        client = GitHubClient(github_token(), directory / "requests.jsonl", args.max_requests)
        return ReadmeCollector(directory, state, client).run()
    except (CollectionError, OSError, ValueError, KeyError, TypeError) as error:
        LOGGER.error("无法安全运行采集：%s", str(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
