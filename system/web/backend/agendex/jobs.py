"""持久化分类队列。网络请求在事务外执行，结果与标签在同一事务提交。"""

import json
from pathlib import Path
import threading
import uuid

from .db import connection, now
from .evidence import encode, latest_evidence_ids, load_bundle
from . import jev
from .jev import JevClient, JevError, response_usage, validate_response
from .glm import GLMClient
from .services import ServiceError, get_job, get_policy, repository_filters
from . import scoring


def refresh_job(conn, identifier: str) -> None:
    counts = conn.execute(
        "SELECT COUNT(*) total,SUM(status='completed') completed,SUM(status='failed') failed,SUM(status='skipped') skipped,SUM(status IN ('queued','running')) remaining,COALESCE(SUM(cost),0) cost,SUM(status IN ('completed','failed') AND request_json IS NOT NULL AND cost IS NULL) unknown_cost_count FROM job_items WHERE job_id=?", (identifier,),
    ).fetchone()
    status = conn.execute("SELECT status FROM jobs WHERE id=?", (identifier,)).fetchone()["status"]
    if not counts["remaining"] and status != "cancelled":
        status = "completed_with_errors" if counts["failed"] else "completed"
    conn.execute(
        "UPDATE jobs SET status=?,completed=?,failed=?,skipped=?,cost=?,unknown_cost_count=?,updated_at=? WHERE id=?",
        (status, counts["completed"] or 0, counts["failed"] or 0, counts["skipped"] or 0,
         counts["cost"], counts["unknown_cost_count"] or 0, now(), identifier),
    )


def apply_results(conn, item, results: list[dict], served_model: str) -> None:
    """一次事务更新当前版本标签及历史结果；可复用于已保存响应的恢复。"""
    timestamp = now()
    conn.execute("UPDATE job_items SET status='completed',results_json=?,model=?,error=NULL,finished_at=? WHERE id=?", (encode(results), served_model, timestamp, item["id"]))
    scoring.record_completed_score(conn, item["id"])


def reuse_item(conn, item, policy: dict, version_id: str, cached) -> None:
    """引用已验证的源评分；复用项没有请求、响应或新增费用。"""
    results = scoring.evaluate_scores(policy, cached)[0]
    scoring.bind_result(conn, version_id, cached, overwrite=True)
    conn.execute(
        "UPDATE job_items SET status='skipped',source_job_item_id=?,results_json=?,model=?,cost=0,error=NULL,finished_at=? WHERE id=?",
        (cached["source_job_item_id"], encode(results), cached["served_model"], now(), item["id"]),
    )


def request_identity(conn, item) -> dict:
    """比较实际请求内容及冻结证据；标签ID和阈值不改变模型问题。"""
    if item["source_job_item_id"] is not None and not item["request_json"]:
        source = conn.execute(scoring.SOURCE_QUERY, (item["source_job_item_id"],)).fetchone()
        if source is None:
            raise ValueError("复用评分的原始请求不存在")
        return request_identity(conn, source)
    policy = scoring.task_policy(json.loads(item["document_json"]), json.loads(item["scope_json"]))
    bundle = load_bundle(conn, item["repository_id"], json.loads(item["evidence_ids_json"]))
    payload = json.loads(item["request_json"]) if item["request_json"] else jev.build_payload(policy, bundle, item["request_model"])
    return scoring.request_keys(payload, policy, bundle)


class JobEngine:
    def __init__(self, db_path: Path, client=None, workers: int = 8, glm_client=None):
        self.db_path = Path(db_path)
        self.client = client or JevClient()
        self.glm_client = glm_client or GLMClient()
        self.worker_count = workers
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads = []

    def client_for_model(self, model: str | None = None):
        """Route each frozen job to its selected provider; never fall back."""
        if model is None or model in {"typesafe/jev-1.14", self.client.model}:
            return self.client
        if model == self.glm_client.model:
            return self.glm_client
        raise ServiceError("不支持的分类模型")

    def model_options(self) -> list[dict]:
        return [
            {"id": "typesafe/jev-1.14", "label": "Jev 1.14", "configured": self.client.configured()},
            {"id": "glm-5.3", "label": "GLM 5.3", "configured": self.glm_client.configured()},
        ]

    def recover(self) -> None:
        """请求可能已计费但无响应时，标失败并暂停，不自动重发。"""
        with connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            affected = {row["job_id"] for row in conn.execute("SELECT DISTINCT job_id FROM job_items WHERE status='running'")}
            for item in conn.execute("SELECT i.*,j.scope_json,v.document_json FROM job_items i JOIN jobs j ON j.id=i.job_id JOIN policy_versions v ON v.id=j.policy_version_id WHERE i.status='running'").fetchall():
                if item["response_json"] is not None:
                    try:
                        policy = scoring.task_policy(json.loads(item["document_json"]), json.loads(item["scope_json"]))
                        results, model = validate_response(json.loads(item["response_json"]), policy)
                        apply_results(conn, item, results, model)
                        continue
                    except (ValueError, JevError, KeyError, TypeError):
                        message = "已保存的响应无效，未自动重试"
                else:
                    message = "服务重启时请求状态不确定，未自动重试"
                conn.execute("UPDATE job_items SET status='failed',error=?,finished_at=? WHERE id=?", (message, now(), item["id"]))
            conn.execute("UPDATE jobs SET status='paused',error=?,updated_at=? WHERE status IN ('queued','running')",
                         ("服务已重启，请检查记录后继续", now()))
            affected.update(row["id"] for row in conn.execute("SELECT id FROM jobs WHERE status='paused'"))
            for identifier in affected:
                refresh_job(conn, identifier)

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for index in range(self.worker_count):
            thread = threading.Thread(target=self._loop, name="agendex-classifier-{}".format(index), daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with connection(self.db_path) as conn:
            conn.execute("UPDATE jobs SET status='paused',updated_at=? WHERE status IN ('queued','running')", (now(),))
        for thread in self._threads:
            thread.join(timeout=1)
        self._threads.clear()

    def create_job(self, body) -> dict:
        request_client = self.client_for_model(body.model)
        request_model = request_client.model
        with connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            saved_policy = get_policy(conn, body.policy_id)
            policy = scoring.task_policy(saved_policy, {"evidence_kinds": body.evidence_kinds})
            version = conn.execute("SELECT id FROM policy_versions WHERE policy_id=? AND revision=?", (policy["id"], policy["revision"])).fetchone()
            if body.scope.repository_ids is not None:
                requested = set(body.scope.repository_ids)
                # 避免数万个 SQLite 占位符超过宿主的变量限制。
                identifiers = [row["id"] for row in conn.execute("SELECT id FROM repositories WHERE catalog_visible=1 ORDER BY stars DESC,id") if row["id"] in requested]
                if len(identifiers) != len(requested):
                    raise ServiceError("选择中存在不属于当前目录的仓库")
            else:
                where, params, sort = repository_filters(body.scope.filters.model_dump() if body.scope.filters else {})
                identifiers = [row["id"] for row in conn.execute("SELECT r.id FROM repositories r WHERE " + where + " ORDER BY " + sort, params)]
            if body.scope.limit:
                identifiers = identifiers[:body.scope.limit]
            if not identifiers:
                raise ServiceError("当前范围没有仓库")
            # 先解析实际仓库与证据快照，再检查任务；筛选条件文本相同并不
            # 意味着其结果或README仍相同。事务锁让并发提交只能建一份任务。
            prepared = {}
            for repository_id in identifiers:
                try:
                    prepared[repository_id] = (latest_evidence_ids(conn, repository_id, policy["evidence_kinds"]), None)
                except ValueError as exc:
                    prepared[repository_id] = ([], str(exc))
            if body.skip_completed:
                existing = conn.execute(
                    "SELECT id,scope_json FROM jobs WHERE policy_version_id=? AND model=? AND total=? AND status IN ('queued','running','paused','completed','completed_with_errors') ORDER BY created_at,rowid",
                    (version["id"], request_model, len(identifiers)),
                ).fetchall()
                wanted = {key: sorted(value[0]) for key, value in prepared.items()}
                for candidate in existing:
                    frozen_policy = scoring.task_policy(saved_policy, json.loads(candidate["scope_json"]))
                    if set(frozen_policy["evidence_kinds"]) != set(policy["evidence_kinds"]):
                        continue
                    candidate_items = conn.execute("SELECT * FROM job_items WHERE job_id=?", (candidate["id"],)).fetchall()
                    frozen = {row["repository_id"]: sorted(json.loads(row["evidence_ids_json"])) for row in candidate_items}
                    if frozen != wanted:
                        continue
                    # A completed task also has to match the currently generated
                    # request. Provider prompt/state updates must not silently
                    # return an older task just because its evidence IDs match.
                    matches = True
                    for row in candidate_items:
                        previous = {**dict(row), "request_model": request_model,
                                    "document_json": encode(saved_policy), "scope_json": candidate["scope_json"]}
                        expected = {**previous, "request_json": None, "source_job_item_id": None,
                                    "scope_json": encode({"evidence_kinds": policy["evidence_kinds"]})}
                        try:
                            if request_identity(conn, previous) != request_identity(conn, expected):
                                matches = False
                                break
                        except (ValueError, KeyError, TypeError):
                            matches = False
                            break
                    if matches:
                        return get_job(conn, candidate["id"])
            identifier = uuid.uuid4().hex
            timestamp = now()
            conn.execute("INSERT INTO jobs(id,policy_id,policy_version_id,status,total,scope_json,model,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (identifier, policy["id"], version["id"], "queued", len(identifiers), encode({**body.scope.model_dump(), "skip_completed": body.skip_completed, "evidence_kinds": policy["evidence_kinds"]}), request_model, timestamp, timestamp))
            for repository_id in identifiers:
                evidence_ids, error = prepared[repository_id]
                status = "failed" if error else "queued"
                cached = None
                if body.skip_completed and status == "queued":
                    previous = conn.execute("SELECT source_job_item_id FROM policy_results WHERE policy_version_id=? AND repository_id=?", (version["id"], repository_id)).fetchone()
                    cached = scoring.find_compatible_score(conn, repository_id, policy, evidence_ids, request_model,
                                                           previous["source_job_item_id"] if previous else None)
                    if cached is not None:
                        status = "skipped"
                        scoring.bind_result(conn, version["id"], cached, overwrite=True)
                reused_results = scoring.evaluate_scores(policy, cached)[0] if cached is not None else None
                conn.execute("INSERT INTO job_items(job_id,repository_id,status,evidence_ids_json,error,finished_at,source_job_item_id,results_json,model,cost) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (identifier, repository_id, status, encode(evidence_ids), error, timestamp if status != "queued" else None,
                              cached["source_job_item_id"] if cached is not None else None,
                              encode(reused_results) if reused_results is not None else None,
                              cached["served_model"] if cached is not None else None, 0 if cached is not None else None))
            pending = conn.execute("SELECT COUNT(*) FROM job_items WHERE job_id=? AND status='queued'", (identifier,)).fetchone()[0]
            if pending and not request_client.configured():
                raise ServiceError("所选模型尚未配置，请检查服务端设置")
            refresh_job(conn, identifier)
            result = get_job(conn, identifier)
        self._wake.set()
        return result

    def control(self, identifier: str, action: str) -> dict:
        with connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = get_job(conn, identifier)
            if action == "pause":
                if job["status"] not in {"queued", "running", "paused"}:
                    raise ServiceError("任务已结束")
                conn.execute("UPDATE jobs SET status='paused',updated_at=? WHERE id=?", (now(), identifier))
            elif action == "resume":
                if job["status"] != "paused":
                    raise ServiceError("只能继续已暂停的任务")
                if not self.client_for_model(job["model"]).configured():
                    raise ServiceError("所选模型尚未配置，请检查服务端设置")
                pending = conn.execute("SELECT COUNT(*) FROM job_items WHERE job_id=? AND status='queued'", (identifier,)).fetchone()[0]
                if not pending:
                    raise ServiceError("没有待处理仓库；失败记录不会自动重复计费")
                conn.execute("UPDATE jobs SET status='queued',error=NULL,updated_at=? WHERE id=?", (now(), identifier))
            elif action == "cancel":
                if job["status"] in {"completed", "completed_with_errors", "cancelled"}:
                    raise ServiceError("任务已结束")
                conn.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=?", (now(), identifier))
                conn.execute("UPDATE job_items SET status='cancelled',finished_at=? WHERE job_id=? AND status='queued'", (now(), identifier))
            else:
                raise ServiceError("不支持的任务操作")
            result = get_job(conn, identifier)
        self._wake.set()
        return result

    def _claim(self):
        with connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT i.*,j.model request_model,j.scope_json,j.policy_version_id,v.document_json FROM job_items i JOIN jobs j ON j.id=i.job_id JOIN policy_versions v ON v.id=j.policy_version_id WHERE i.status='queued' AND j.status IN ('queued','running') ORDER BY j.created_at,j.rowid,i.id"
            ).fetchall()
            identities = {}
            for row in rows:
                policy = scoring.task_policy(json.loads(row["document_json"]), json.loads(row["scope_json"]))
                try:
                    # Another task may have finished after this item was queued.
                    # Old jobs without this persisted option use normal reuse.
                    if json.loads(row["scope_json"]).get("skip_completed", True):
                        previous = conn.execute("SELECT source_job_item_id FROM policy_results WHERE policy_version_id=? AND repository_id=?",
                                                (row["policy_version_id"], row["repository_id"])).fetchone()
                        cached = scoring.find_compatible_score(conn, row["repository_id"], policy,
                                                               json.loads(row["evidence_ids_json"]), row["request_model"],
                                                               previous["source_job_item_id"] if previous else None)
                        if cached is not None:
                            reuse_item(conn, row, policy, row["policy_version_id"], cached)
                            refresh_job(conn, row["job_id"])
                            continue
                    wanted = request_identity(conn, row)
                    running = conn.execute(
                        "SELECT i.*,j.model request_model,j.scope_json,v.document_json FROM job_items i JOIN jobs j ON j.id=i.job_id JOIN policy_versions v ON v.id=j.policy_version_id WHERE i.repository_id=? AND j.model=? AND i.status='running'",
                        (row["repository_id"], row["request_model"]),
                    ).fetchall()
                    blocked = False
                    for active in running:
                        try:
                            if active["id"] not in identities:
                                identities[active["id"]] = request_identity(conn, active)
                        except (ValueError, KeyError, TypeError):
                            continue
                        if identities[active["id"]] == wanted:
                            blocked = True
                            break
                    if blocked:
                        # Parent pause/cancel does not stop a request already sent.
                        # Look past this item so unrelated repositories can run.
                        continue
                except (ValueError, KeyError, TypeError):
                    # _execute records malformed evidence as a failure before
                    # sending anything; do not silently drop the queued item.
                    pass
                conn.execute("UPDATE job_items SET status='running',started_at=? WHERE id=?", (now(), row["id"]))
                conn.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now(), row["job_id"]))
                return dict(row)
            return None

    def _fail(self, item: dict, message: str, pause: bool = False) -> None:
        with connection(self.db_path) as conn:
            conn.execute("UPDATE job_items SET status='failed',error=?,finished_at=? WHERE id=?", (message, now(), item["id"]))
            recent = conn.execute("SELECT status FROM job_items WHERE job_id=? AND status IN ('completed','failed') ORDER BY finished_at DESC,id DESC LIMIT 5", (item["job_id"],)).fetchall()
            if len(recent) == 5 and all(row["status"] == "failed" for row in recent):
                pause = True
                message = "连续 5 条处理失败，任务已暂停；请检查策略与响应记录"
            if pause:
                conn.execute("UPDATE jobs SET status='paused',error=?,updated_at=? WHERE id=? AND status!='cancelled'", (message, now(), item["job_id"]))
            refresh_job(conn, item["job_id"])

    def _execute(self, item: dict) -> None:
        policy = scoring.task_policy(json.loads(item["document_json"]), json.loads(item["scope_json"]))
        try:
            with connection(self.db_path) as conn:
                bundle = load_bundle(conn, item["repository_id"], json.loads(item["evidence_ids_json"]))
            if {document["kind"] for document in bundle} != set(policy["evidence_kinds"]):
                raise ValueError("冻结证据与任务数据来源不匹配")
            request = jev.build_payload(policy, bundle, item["request_model"])
            with connection(self.db_path) as conn:
                conn.execute("UPDATE job_items SET request_json=? WHERE id=?", (encode(request), item["id"]))
            response = self.client_for_model(item["request_model"]).request(request)
            usage, cost = response_usage(response)
            # 即使结果类型错误，也先保存实际返回内容与费用；不自动再次付费。
            with connection(self.db_path) as conn:
                conn.execute("UPDATE job_items SET response_json=?,usage_json=?,cost=?,model=? WHERE id=?",
                             (json.dumps(response, ensure_ascii=False), json.dumps(usage, ensure_ascii=False), cost,
                              response.get("model") if isinstance(response.get("model"), str) else None, item["id"]))
            results, served_model = validate_response(response, policy)
            with connection(self.db_path) as conn:
                apply_results(conn, item, results, served_model)
                refresh_job(conn, item["job_id"])
        except JevError as exc:
            self._fail(item, str(exc), pause=exc.pause)
        except (ValueError, KeyError, TypeError):
            self._fail(item, "证据或响应格式无效，未应用标签")
        except Exception:
            self._fail(item, "分类处理失败，未自动重试", pause=True)

    def run_once(self) -> bool:
        if self._stop.is_set():
            return False
        item = self._claim()
        if item is None:
            return False
        self._execute(item)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.run_once():
                    continue
            except Exception:
                # 数据库暂时占用时不让守护线程永久退出；已派发项由恢复协议处理。
                pass
            self._wake.wait(timeout=1)
            self._wake.clear()
