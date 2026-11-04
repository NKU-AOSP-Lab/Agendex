#!/usr/bin/env python3
"""Freeze and score a candidate catalogue from repository metadata."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Optional
from urllib.error import HTTPError, URLError


DEFAULT_THRESHOLD = 0.75
MODEL = "typesafe/jev-1.14"
POLICY_PATH = Path(__file__).parent / "policies/prescreen-policy-v1.json"

# Shared transport owns credentials and request handling.
_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))
from system.experiment_tools import jev as TRANSPORT
resolve_recorded_path = TRANSPORT.resolve_recorded_path


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def file_digest(path: Path) -> str:
    path = resolve_recorded_path(path)
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def atomic_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temporary.replace(path)


def write_json(path: Path, value) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2,
                                  allow_nan=False) + "\n").encode("utf-8"))


def write_jsonl(path: Path, values) -> None:
    atomic_bytes(path, b"".join(encoded(value) + b"\n" for value in values))


def read_jsonl(path: Path) -> list:
    path = resolve_recorded_path(path)
    if not path.exists():
        return []
    values = []
    for number, line in enumerate(path.read_bytes().splitlines(), 1):
        if not line.strip():
            raise ValueError(f"{path.name}:{number} 包含空记录")
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path.name}:{number} 必须为对象")
        values.append(row)
    return values


@contextmanager
def directory_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("该试验目录已有运行进程") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def validate_repository(repo: dict) -> None:
    identifier, name = repo.get("id"), repo.get("full_name")
    if type(identifier) is not int or identifier <= 0:
        raise ValueError("仓库 ID 必须为正整数")
    if not isinstance(name, str) or len(name.split("/")) != 2 or not all(name.split("/")):
        raise ValueError("仓库 full_name 无效")
    TRANSPORT.description_missing(repo)
    if repo.get("topics") is not None and (
            not isinstance(repo["topics"], list) or not all(isinstance(t, str) for t in repo["topics"])):
        raise ValueError("仓库 Topics 必须为字符串列表")


def build_payload(repositories: list, policy: dict, model: str = MODEL) -> dict:
    questions = {}
    for repo in repositories:
        validate_repository(repo)
        if TRANSPORT.description_missing(repo):
            raise ValueError("无 Description 的仓库应本地保护保留，不送模型")
        key = "repo_" + str(repo["id"])
        if key in questions:
            raise ValueError("请求含重复仓库")
        questions[key] = {"type": "noul", "instructions": {
            "question": policy["question"],
            "repository": TRANSPORT.repository_input(repo)}}
    return {"model": model, "state": {"acquisition_policy": policy}, "questions": questions}


def batches(repositories: list, policy: dict, batch_size: int, max_bytes: int):
    if not 1 <= batch_size <= 64 or max_bytes < 1024:
        raise ValueError("批次大小必须为 1–64，字节预算至少 1024")
    batch = []
    for repo in repositories:
        candidate = batch + [repo]
        if batch and (len(candidate) > batch_size or len(encoded(build_payload(candidate, policy))) > max_bytes):
            yield batch
            candidate = [repo]
        if len(encoded(build_payload(candidate, policy))) > max_bytes:
            raise ValueError("单个仓库元数据超过请求字节预算")
        batch = candidate
    if batch:
        yield batch


def plan_all(source: Path, directory: Path, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Freeze the entire candidate frame; no random sample or control cohort is implied."""
    source, directory = resolve_recorded_path(source), resolve_recorded_path(directory)
    if not TRANSPORT.finite_number(threshold, 0, 1):
        raise ValueError("保留阈值必须为 0–1 有限数")
    repositories = read_jsonl(source)
    if not repositories:
        raise ValueError("源总体为空")
    identifiers = []
    for repo in repositories:
        validate_repository(repo)
        identifiers.append(repo["id"])
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("源文件含重复 ID")
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    source_hash = file_digest(source)
    with directory_lock(directory):
        manifest_path = directory / "manifest.json"
        if manifest_path.exists():
            manifest = load_all_plan(directory)[0]
            if (manifest["source_sha256"] != source_hash or manifest["threshold"] != threshold
                    or manifest["policy_object_hash"] != digest(policy)
                    or manifest["selected_repository_ids"] != identifiers):
                raise ValueError("全量输入、阈值或策略改变，请用新目录")
            return manifest
        if any(path.name != ".lock" for path in directory.iterdir()):
            raise ValueError("输出目录已有非本试验文件，拒绝覆盖")
        write_json(directory / "policy.json", policy)
        manifest = {"schema_version": 2, "created_at": TRANSPORT.now(),
            "source": str(source), "source_sha256": source_hash,
            "population_size": len(repositories), "selected_repositories": len(repositories),
            "selected_repository_ids": identifiers,
            "selection_method": "all_repositories_in_frozen_candidate_frame",
            "policy_version": policy["policy_version"], "policy_object_hash": digest(policy),
            "policy_sha256": file_digest(directory / "policy.json"), "requested_model": MODEL,
            "scope": "full_metadata_prescreen", "bulk_exclusion_enabled": True,
            "threshold": threshold, "threshold_comparison": "keep_if_score_greater_than_or_equal",
            "missing_description_rule": "keep_without_model_call_or_score",
            "notes": ["This is the complete frozen candidate frame, not a random sample.",
                      "Keep/exclude decisions are metadata acquisition decisions, not final agent labels."]}
        write_json(manifest_path, manifest)
        return manifest


def load_all_plan(directory: Path) -> tuple:
    directory = resolve_recorded_path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != 2 or manifest.get("requested_model") != MODEL
            or manifest.get("scope") != "full_metadata_prescreen"
            or manifest.get("bulk_exclusion_enabled") is not True
            or manifest.get("selection_method") != "all_repositories_in_frozen_candidate_frame"
            or not TRANSPORT.finite_number(manifest.get("threshold"), 0, 1)
            or manifest.get("threshold_comparison") != "keep_if_score_greater_than_or_equal"
            or manifest.get("missing_description_rule") != "keep_without_model_call_or_score"):
        raise ValueError("全量初筛配置无效")
    for filename, field in (("policy.json", "policy_sha256"),):
        if file_digest(directory / filename) != manifest[field]:
            raise ValueError("冻结试验文件被修改：" + filename)
    policy = json.loads((directory / "policy.json").read_text(encoding="utf-8"))
    if digest(policy) != manifest["policy_object_hash"]:
        raise ValueError("策略对象与冻结记录不一致")
    repositories = read_jsonl(Path(manifest["source"]))
    identifiers = [repo["id"] for repo in repositories]
    if (not repositories or identifiers != manifest["selected_repository_ids"]
            or len(set(identifiers)) != len(identifiers)
            or len(repositories) != manifest["population_size"]
            or len(repositories) != manifest["selected_repositories"]):
        raise ValueError("冻结全量仓库与清单不一致")
    for repo in repositories:
        validate_repository(repo)
    if file_digest(Path(manifest["source"])) != manifest["source_sha256"]:
        raise ValueError("源总体已改变")
    return manifest, repositories, policy


def score_record(repo: dict, selection: dict, manifest: dict, probability,
                 response: Optional[dict] = None, scored_at: Optional[str] = None) -> dict:
    protected = TRANSPORT.description_missing(repo)
    if protected and probability is not None:
        raise ValueError("受保护记录不能拥有模型分数")
    if not protected and not TRANSPORT.finite_number(probability, 0, 1):
        raise ValueError("模型分数必须为 0–1 有限数")
    response = response or {}
    return {"repository_id": repo["id"], "full_name": repo["full_name"],
            "input_hash": TRANSPORT.digest(TRANSPORT.repository_input(repo)), "cohorts": selection["cohorts"],
            "prescreen_probability": probability, "protected_keep": protected,
            "decision_source": "rule" if protected else "jev",
            "reason": "missing_description_protected" if protected else "score_only_no_exclusion",
            "policy_version": manifest["policy_version"], "requested_model": MODEL,
            "served_model": response.get("model"), "response_id": response.get("id"),
            "scored_at": scored_at or TRANSPORT.now()}


class FullOutput(TRANSPORT.Output):
    """Remember only durably committed responses, avoiding a full log scan per batch."""
    def __init__(self, directory: Path):
        super().__init__(directory)
        self.response_lock = threading.Lock()
        self.responses = {}

    def append_many(self, name: str, values: list) -> None:
        super().append_many(name, values)
        if name == "responses.jsonl":
            with self.response_lock:
                for event in values:
                    self.responses[tuple(event["repository_ids"])] = event


class FullJevClient(TRANSPORT.JevClient):
    """One transport attempt per batch; uncertain billing is never retried silently."""
    def call(self, payload: dict) -> dict:
        identifiers = [int(key.removeprefix("repo_")) for key in payload["questions"]]
        with self.lock:
            allowed = self.cost < self.max_cost and not self.unknown_cost
        if self.stop.is_set() or not allowed:
            raise RuntimeError("Stopped, spending limit reached, or cost unavailable")
        event = {"time": TRANSPORT.now(), "repository_ids": identifiers, "attempt": 1}
        self.output.append("requests.jsonl", {**event, "status": "started"})
        started = time.monotonic()
        try:
            request = TRANSPORT.Request(TRANSPORT.ENDPOINT, data=TRANSPORT.encoded(payload),
                method="POST", headers={"Authorization": "Bearer " + self.key,
                    "Content-Type": "application/json", "User-Agent": "Agendex-Jev-Screening/0.1"})
            with TRANSPORT.build_opener(TRANSPORT.NoRedirect()).open(request, timeout=45) as response:
                body = response.read()
            try:
                data = json.loads(body)
            except ValueError:
                self.output.append("responses.jsonl", {**event,
                    "body": body.decode("utf-8", "replace").replace(self.key, "[REDACTED]")})
                self.account(None)
                raise ValueError("Response is not JSON; original response saved")
            self.output.append("responses.jsonl", {**event, "response": data})
            self.account(data)
            TRANSPORT.validate_response(data, identifiers)
        except HTTPError as error:
            body = error.read().decode("utf-8", "replace").replace(self.key, "[REDACTED]")
            self.output.append("requests.jsonl", {**event, "status": "http_error",
                "http_status": error.code, "body": body,
                "elapsed_seconds": round(time.monotonic() - started, 3)})
            raise RuntimeError("Jev HTTP request failed; no automatic retry") from None
        except (URLError, TimeoutError, ConnectionError, OSError) as error:
            with self.lock:
                self.network_error_attempts += 1
            self.output.append("requests.jsonl", {**event, "status": "network_error",
                "error_type": type(error).__name__, "billing_uncertain": True,
                "elapsed_seconds": round(time.monotonic() - started, 3)})
            raise RuntimeError("Jev transport receipt uncertain; no automatic retry") from None
        except ValueError as error:
            self.output.append("requests.jsonl", {**event, "status": "invalid_response",
                "error": str(error), "elapsed_seconds": round(time.monotonic() - started, 3)})
            raise RuntimeError("Invalid Jev response; no automatic retry") from None
        self.output.append("requests.jsonl", {**event, "status": "ok",
            "elapsed_seconds": round(time.monotonic() - started, 3)})
        return data


def full_score_record(repo: dict, manifest: dict, probability, response: Optional[dict] = None,
                      scored_at: Optional[str] = None) -> dict:
    row = score_record(repo, {"cohorts": []}, manifest, probability, response, scored_at)
    row.pop("cohorts")
    keep = row["protected_keep"] or probability >= manifest["threshold"]
    row.update(decision="keep" if keep else "exclude", threshold=manifest["threshold"],
               reason=("missing_description_protected" if row["protected_keep"] else
                       "score_at_or_above_threshold" if keep else "score_below_threshold"))
    return row


def score_all(directory: Path, max_cost: float = 1.0, batch_size: int = 32,
              max_request_bytes: int = 160000, max_api_items: Optional[int] = None,
              workers: int = 4, retry_failed: bool = False) -> dict:
    """Score all frozen metadata, with durable receipts and bounded concurrent spending."""
    directory = resolve_recorded_path(directory)
    if not TRANSPORT.finite_number(max_cost, 0.000001):
        raise ValueError("费用上限必须为正有限数")
    if type(workers) is not int or not 1 <= workers <= 32:
        raise ValueError("并发数必须为 1–32")
    if max_api_items is not None and (type(max_api_items) is not int or max_api_items < 0):
        raise ValueError("调用数量上限必须为非负整数")
    if not 1 <= batch_size <= 64 or max_request_bytes < 1024:
        raise ValueError("请求批次或字节预算无效")
    with directory_lock(directory):
        manifest, repositories, policy = load_all_plan(directory)
        if max_api_items is not None and max_api_items > len(repositories):
            raise ValueError("调用数量上限不得超过冻结总体总数")
        scoring = directory / "scoring"
        scoring.mkdir(exist_ok=True)
        output, stop = FullOutput(scoring), threading.Event()
        client = FullJevClient("", output, stop, max_cost)
        by_id = {repo["id"]: repo for repo in repositories}
        config = {"manifest_sha256": file_digest(directory / "manifest.json"),
            "source_sha256": manifest["source_sha256"],
            "policy_sha256": manifest["policy_sha256"], "requested_model": MODEL,
            "input_fields": list(TRANSPORT.repository_input(repositories[0])),
            "scope": manifest["scope"], "bulk_exclusion_enabled": True,
            "threshold": manifest["threshold"], "automatic_request_retries": False}
        config_path = scoring / "config.json"
        if config_path.exists() and json.loads(config_path.read_text(encoding="utf-8")) != config:
            raise ValueError("评分配置已改变，拒绝复用付费结果")
        if not config_path.exists():
            write_json(config_path, config)
        payload_keys = set()
        for record in TRANSPORT.read_records(scoring / "payloads.jsonl"):
            ids = record["repository_ids"]
            if len(set(ids)) != len(ids) or not ids or not set(ids).issubset(by_id):
                raise ValueError("付费请求含未知或重复仓库")
            expected = build_payload([by_id[i] for i in ids], policy)
            if record.get("payload") != expected or record.get("payload_sha256") != digest(expected):
                raise ValueError("已保存付费请求与冻结总体/政策不匹配")
            payload_keys.add(tuple(ids))
        results, started_ids = {}, set()
        for event in TRANSPORT.read_records(scoring / "responses.jsonl"):
            data, ids = event.get("response"), event.get("repository_ids")
            client.account(data)
            if not isinstance(ids, list) or tuple(ids) not in payload_keys:
                raise ValueError("响应找不到对应的已保存请求")
            started_ids.update(ids)
            try:
                probabilities, _ = TRANSPORT.validate_response(data, ids)
            except ValueError:
                continue
            for identifier, probability in probabilities.items():
                if identifier in results and results[identifier]["prescreen_probability"] != probability:
                    raise ValueError("同一仓库出现相互冲突的付费响应")
                results[identifier] = full_score_record(by_id[identifier], manifest,
                    probability, data, event["time"])
        for event in TRANSPORT.read_records(scoring / "requests.jsonl"):
            ids = event.get("repository_ids")
            if not isinstance(ids, list) or tuple(ids) not in payload_keys:
                raise ValueError("请求记录找不到对应的已保存请求")
            started_ids.update(ids)
            client.network_error_attempts += event.get("status") == "network_error"
        for repo in repositories:
            if TRANSPORT.description_missing(repo):
                results[repo["id"]] = full_score_record(repo, manifest, None,
                                                       scored_at=manifest["created_at"])
        unresolved = started_ids - set(results)
        pending = [repo for repo in repositories if repo["id"] not in results]
        if max_api_items is not None:
            pending = pending[:max_api_items]
        summary_path = scoring / "SUMMARY.json"
        previous = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        invocations = previous.get("invocations", [])
        started_at, monotonic_start = TRANSPORT.now(), time.monotonic()
        invocation = {"started_at": started_at, "finished_at": None, "wall_elapsed_seconds": 0,
                      "workers": workers, "retry_failed": retry_failed,
                      "batch_size": batch_size, "max_request_bytes": max_request_bytes}
        invocations.append(invocation)
        status, reason, error_type = "running", "running", None

        def checkpoint(final: bool = False) -> dict:
            invocation["wall_elapsed_seconds"] = round(time.monotonic() - monotonic_start, 3)
            if final:
                invocation["finished_at"] = TRANSPORT.now()
                # Durable responses are the recovery authority. Export the large raw-metadata
                # subsets only when leaving an invocation, not once per paid batch.
                rows = []
                for repo in repositories:
                    row = results.get(repo["id"])
                    if row is None:
                        row = {"repository_id": repo["id"], "full_name": repo["full_name"],
                            "input_hash": TRANSPORT.digest(TRANSPORT.repository_input(repo)),
                            "prescreen_probability": None, "protected_keep": False,
                            "decision": "pending", "decision_source": None,
                            "reason": "request_failed_or_uncertain" if repo["id"] in unresolved else "not_scored",
                            "threshold": manifest["threshold"]}
                    rows.append(row)
                write_jsonl(scoring / "scores.jsonl", rows)
                for decision, filename in (("keep", "kept.jsonl"), ("exclude", "excluded.jsonl"),
                                           ("pending", "pending.jsonl")):
                    write_jsonl(scoring / filename, [repo for repo, row in zip(repositories, rows)
                                                    if row["decision"] == decision])
            summary = {"updated_at": TRANSPORT.now(), "status": status, "reason": reason,
                "scope": manifest["scope"], "bulk_exclusion_enabled": True,
                "population_size": len(repositories), "selected_repositories": len(repositories),
                "scored_or_protected_repositories": len(results), "pending_repositories": len(repositories)-len(results),
                "kept_repositories": sum(row["decision"] == "keep" for row in results.values()),
                "excluded_repositories": sum(row["decision"] == "exclude" for row in results.values()),
                "protected_missing_description": sum(row["protected_keep"] for row in results.values()),
                "jev_scored_repositories": sum(row["decision_source"] == "jev" for row in results.values()),
                "failed_or_uncertain_repositories": len(unresolved),
                "requested_model": MODEL, "threshold": manifest["threshold"],
                "reported_cost_usd": client.cost, "cost_source": "sum_of_response_usage.cost",
                "responses_with_unknown_cost": client.unknown_cost,
                "transport_attempts_with_uncertain_billing": client.network_error_attempts,
                "max_cost_usd": max_cost, "workers_last_invocation": workers,
                "cost_limit_type": "reported-cost stop threshold; up to workers in-flight responses may exceed it",
                "started_at": invocations[0]["started_at"], "finished_at": invocation["finished_at"],
                "wall_elapsed_seconds": round(sum(item["wall_elapsed_seconds"] for item in invocations), 3),
                "end_to_end_elapsed_seconds": round((datetime.fromisoformat(
                    invocation["finished_at"] or TRANSPORT.now()) - datetime.fromisoformat(
                    invocations[0]["started_at"])).total_seconds(), 3),
                "wall_time_source": "sum_of_monotonic_invocation_elapsed_seconds",
                "invocations": invocations, "error_type": error_type}
            write_json(summary_path, summary)
            return summary

        def run_batch(batch: list) -> tuple:
            ids = [repo["id"] for repo in batch]
            payload = build_payload(batch, policy)
            wire_body = TRANSPORT.encoded(payload)
            output.append("payloads.jsonl", {"time": TRANSPORT.now(), "repository_ids": ids,
                "payload_sha256": digest(payload), "payload": payload,
                "request_body_sha256": hashlib.sha256(wire_body).hexdigest(),
                "request_body_utf8": wire_body.decode("utf-8")})
            data = client.call(payload)
            probabilities, _ = TRANSPORT.validate_response(data, ids)
            with output.response_lock:
                received = output.responses.get(tuple(ids))
            if received is None or received.get("response") != data:
                raise ValueError("返回分数与持久保存的付费响应不匹配")
            return ids, probabilities, data, received["time"]

        def can_submit() -> bool:
            return (not stop.is_set() and client.cost < max_cost and not client.unknown_cost
                    and not (directory / "STOP").exists())

        checkpoint()
        old_handlers = {}
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGINT, signal.SIGTERM):
                old_handlers[number] = signal.signal(number, lambda *_: stop.set())
        try:
            if unresolved and not retry_failed:
                status, reason = "paused", "prior_failed_or_uncertain_request_requires_explicit_retry"
            elif pending and can_submit():
                # Validate every local input/batch before any paid request. A single
                # oversized metadata object must not fail midway through a paid run.
                prepared_batches = list(batches(pending, policy, batch_size, max_request_bytes))
                client.key = TRANSPORT.api_key()
                iterator = iter(prepared_batches)
                exhausted = False
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    active = {}
                    while active or not exhausted:
                        while len(active) < workers and not exhausted and can_submit():
                            batch = next(iterator, None)
                            if batch is None:
                                exhausted = True
                                break
                            active[executor.submit(run_batch, batch)] = [repo["id"] for repo in batch]
                        if not active:
                            break
                        done, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                        if (directory / "STOP").exists():
                            stop.set()
                        for future in done:
                            ids = active.pop(future)
                            try:
                                _, probabilities, data, received_at = future.result()
                            except Exception as error:
                                unresolved.update(ids)
                                error_type = type(error).__name__
                                reason = "request_failed_or_billing_uncertain"
                                stop.set()
                            else:
                                for identifier, probability in probabilities.items():
                                    results[identifier] = full_score_record(by_id[identifier], manifest,
                                        probability, data, received_at)
                                    unresolved.discard(identifier)
                            checkpoint()
                            print(f"Jev full: {len(results)}/{len(repositories)}; reported cost ${client.cost:.6f}", flush=True)
            if len(results) == len(repositories):
                status, reason = "complete", "all_candidate_records_scored_or_protected"
            else:
                status = "paused"
                if reason == "running":
                    reason = "budget_unknown_cost_or_stop" if not can_submit() else "selected_item_limit"
        except (Exception, KeyboardInterrupt) as error:
            # Executor shutdown waits for in-flight calls. Recover any responses that
            # became durable before a local exception interrupted result collection.
            with output.response_lock:
                durable_events = list(output.responses.values())
            for event in durable_events:
                ids, data = event["repository_ids"], event.get("response")
                try:
                    probabilities, _ = TRANSPORT.validate_response(data, ids)
                except ValueError:
                    continue
                for identifier, probability in probabilities.items():
                    results[identifier] = full_score_record(by_id[identifier], manifest,
                        probability, data, event["time"])
                    unresolved.discard(identifier)
            status, reason, error_type = "failed", "request_or_persistence_failed", type(error).__name__
            raise
        finally:
            summary = checkpoint(final=True)
            for number, handler in old_handlers.items():
                signal.signal(number, handler)
        return summary


def supplement(directory: Path, reviews_path: Optional[Path] = None) -> dict:
    """Merge positive README reference reviews into a separate candidate list, offline."""
    directory = resolve_recorded_path(directory)
    reviews_path = resolve_recorded_path(reviews_path) if reviews_path is not None else None
    scoring = directory / "scoring"
    base_path, augmented_path = scoring / "kept.jsonl", scoring / "kept-augmented.jsonl"
    summary_path = scoring / "AUGMENTATION.json"
    if not base_path.is_file() or (reviews_path is not None and not reviews_path.is_file()):
        raise ValueError("The retained candidate list and any supplied reviews file must exist")
    if reviews_path in (augmented_path, summary_path):
        raise ValueError("复核输入不能使用本命令的派生输出路径")
    with directory_lock(directory):
        base = {}
        for row in read_jsonl(base_path):
            identifier = row.get("repository_id", row.get("id"))
            if type(identifier) is not int or identifier <= 0:
                raise ValueError("基线仓库 ID 必须为正整数")
            if row.get("id", identifier) != identifier:
                raise ValueError("基线仓库 ID 字段不一致")
            validate_repository(row)
            if identifier in base and base[identifier]["full_name"] != row["full_name"]:
                raise ValueError("同一基线 ID 出现不同仓库名称")
            base.setdefault(identifier, {**row, "retention_source": "metadata_prescreen"})
        grouped = {}
        review_records = read_jsonl(reviews_path) if reviews_path is not None else []
        for review in review_records:
            identifier = review.get("repository_id", review.get("id"))
            if type(identifier) is not int or identifier <= 0:
                raise ValueError("复核仓库 ID 必须为正整数")
            if type(review.get("include")) is not bool:
                raise ValueError("复核 include 必须为布尔值")
            if review.get("reference_label") != ("include" if review["include"] else "exclude"):
                raise ValueError("文档参考标签与 include 不一致")
            if review.get("original_prescreen_decision") not in ("keep", "exclude", "outside_search"):
                raise ValueError("原始初筛状态无效")
            probability = review.get("prescreen_probability")
            if probability is not None and not TRANSPORT.finite_number(probability, 0, 1):
                raise ValueError("原始初筛分数必须为 0–1 有限数或 null")
            name = review.get("full_name")
            if not isinstance(name, str) or len(name.split("/")) != 2 or not all(name.split("/")):
                raise ValueError("复核仓库 full_name 无效")
            if not all(isinstance(review.get(field), list) for field in ("sources", "source_repos", "evidence")):
                raise ValueError("复核来源与证据必须为列表")
            entries = grouped.setdefault(identifier, [])
            if entries and any(review[field] != entries[0][field] for field in
                               ("include", "original_prescreen_decision", "prescreen_probability")):
                raise ValueError("同一仓库存在相互冲突的复核或原始分数")
            entries.append(review)
        augmented = dict(base)
        added, discovery, overrides, already_kept = [], [], [], []
        positive_ids = [identifier for identifier, entries in grouped.items() if entries[0]["include"] is True]
        for identifier in positive_ids:
            entries = grouped[identifier]
            review = entries[0]
            if identifier in base:
                if base[identifier]["full_name"].casefold() != review["full_name"].casefold():
                    raise ValueError("基线与复核仓库名称不一致")
                already_kept.append(identifier)
                continue
            metadata_reference = review.get("metadata_record")
            if not isinstance(metadata_reference, str) or not metadata_reference:
                raise ValueError("补充仓库必须具有原始元数据文件引用")
            metadata_path = resolve_recorded_path(metadata_reference, base=_WORKSPACE_ROOT)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("原始仓库元数据必须为对象")
            validate_repository(metadata)
            if metadata["id"] != identifier or metadata["full_name"].casefold() != review["full_name"].casefold():
                raise ValueError("复核与原始元数据的仓库 ID 或名称不一致")
            original_decision = review["original_prescreen_decision"]
            if original_decision == "keep":
                raise ValueError("复核标记为原始 keep，但基线保留清单未包含该仓库")
            augmented[identifier] = {**metadata, "retention_source": "awesome_readme_reference",
                "decision": "keep", "original_prescreen_decision": original_decision,
                "prescreen_probability": review["prescreen_probability"],
                "metadata_record": metadata_reference,
                "reference_reviews": entries}
            added.append(identifier)
            (discovery if original_decision == "outside_search" else overrides).append(identifier)
        summary = {"schema_version": 1, "updated_at": TRANSPORT.now(),
            "scope": "awesome_readme_reference_candidate_augmentation",
            "base_input": str(base_path), "reviews_input": str(reviews_path) if reviews_path is not None else None,
            "augmented_output": str(augmented_path), "base_count": len(base),
            "reviewed_count": len(grouped), "review_record_count": len(review_records),
            "positive_review_count": len(positive_ids), "added_count": len(added),
            "augmented_count": len(augmented), "discovery_additions": len(discovery),
            "prescreen_overrides": len(overrides), "reviewed_repository_ids": list(grouped),
            "positive_review_repository_ids": positive_ids, "added_repository_ids": added,
            "discovery_addition_repository_ids": discovery, "prescreen_override_repository_ids": overrides,
            "already_kept_positive_repository_ids": already_kept,
            "evidence_class": "README_documentation_reference_review",
            "notes": ["Reference inclusion retains candidates for further review; it is not source-code or runtime verification.",
                      "Original prescreen decisions and scores remain unchanged."]}
        write_jsonl(augmented_path, list(augmented.values()))
        write_json(summary_path, summary)
        return summary


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan", help="Freeze repository metadata and the screening policy locally")
    planning.add_argument("source", type=Path)
    planning.add_argument("--output-dir", type=Path, required=True)
    planning.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    scoring = commands.add_parser("score", help="Score the frozen candidates, resuming previous progress")
    scoring.add_argument("--output-dir", type=Path, required=True)
    scoring.add_argument("--max-cost", type=float, default=1.0)
    scoring.add_argument("--batch-size", type=int, default=32)
    scoring.add_argument("--max-request-bytes", type=int, default=160000)
    scoring.add_argument("--max-api-items", type=int)
    scoring.add_argument("--workers", type=int, default=4)
    scoring.add_argument("--retry-failed", action="store_true", help="Allow retries after failures or uncertain billing")
    augmentation = commands.add_parser("supplement", help="Prepare the candidate list and merge any optional README reviews")
    augmentation.add_argument("--output-dir", type=Path, required=True)
    augmentation.add_argument("--reviews", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan_all(args.source, args.output_dir, args.threshold)
            fields = ("scope", "population_size", "selected_repositories", "threshold")
            print(json.dumps({key: result[key] for key in fields}, ensure_ascii=False))
            return 0
        if args.command == "supplement":
            result = supplement(args.output_dir, args.reviews)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        result = score_all(args.output_dir, args.max_cost, args.batch_size, args.max_request_bytes,
                           args.max_api_items, args.workers, args.retry_failed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "complete" else 2
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
        print("Prescreen failed: " + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
