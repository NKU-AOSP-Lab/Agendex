"""可复用评分与版本化判定。

真实模型请求只保存在源 job_item。score_cache 是其完整、已验证分数的索引；
policy_results 只引用源记录。改变阈值不会制造模型响应或复制原任务费用。
"""

from collections import Counter
import hashlib
import json

from .db import now
from .evidence import encode, latest_evidence_ids, load_bundle
from . import jev
from .jev import NONE_OPTION, JevError, validate_response


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def normalize_policy(document: dict) -> dict:
    """读取旧策略时显式补齐逐标签阈值，不修改冻结的原 JSON。"""
    document = {**document, "tags": [dict(tag) for tag in document.get("tags", [])]}
    for tag in document["tags"]:
        if tag.get("threshold") is None:
            tag["threshold"] = document.get("threshold", .8)
    return document


def task_policy(document: dict, scope: dict) -> dict:
    """Overlay the task's frozen sources without editing a saved policy version.

    Legacy jobs did not store this setting in scope_json, so their original
    policy document remains the authoritative fallback.
    """
    return {**document, "evidence_kinds": list(scope.get("evidence_kinds") or document["evidence_kinds"])}


def tag_key(tag: dict) -> str:
    # ID 只是持久化身份；同一标签定义在另一个策略中可使用不同 ID。
    return digest({"label": tag["label"], "description": tag["description"]})


def policy_semantic_key(policy: dict) -> str:
    """已绑定分数的重判只比较用户规则，不冒充一次新模板的推理。

    与新请求缓存不同：阈值调整和恢复是在原证据/原模型/原模板上重放
    判定，不能因服务端模板升级或后来补充证据而丢掉历史评分来源。
    """
    return digest({"description": policy.get("description", ""), "mode": policy["mode"],
                   "evidence_kinds": sorted(policy["evidence_kinds"]),
                   "tags": sorted(tag_key(tag) for tag in policy["tags"])})


def request_keys(payload: dict, policy: dict, bundle: list[dict]) -> dict:
    """从实际传输 payload 生成签名，不能另造一份过时的 prompt 模板。

    只移除 API 明确不作为语义的 question/option ID。问题文本、criteria、
    state、模型和其余 payload 字段均参与匹配；新模板或输入变化会失配。
    """
    questions = payload.get("questions")
    if not isinstance(questions, dict) or "state" not in payload or not isinstance(payload.get("model"), str):
        raise ValueError("保存的模型请求不完整")
    id_keys = {tag["id"]: tag_key(tag) for tag in policy["tags"]}
    if policy["mode"] == "multi_label":
        expected = {"tag_" + identifier for identifier in id_keys}
        if set(questions) != expected:
            raise ValueError("保存的问题与标签定义不匹配")
        normalized = {id_keys[identifier]: questions["tag_" + identifier] for identifier in id_keys}
    else:
        if set(questions) != {"classification"}:
            raise ValueError("保存的单选问题不完整")
        question = questions["classification"]
        criteria = question.get("criteria")
        if not isinstance(criteria, dict) or set(criteria) != set(id_keys) | {NONE_OPTION}:
            raise ValueError("保存的单选选项不完整")
        normalized = {"classification": {**question, "criteria": {
            **{id_keys[identifier]: criteria[identifier] for identifier in id_keys},
            NONE_OPTION: criteria[NONE_OPTION],
        }}}
    semantic = {key: value for key, value in payload.items() if key not in {"state", "model", "questions"}}
    semantic.update({"mode": policy["mode"], "questions": normalized})
    identity = [{key: item[key] for key in ("id", "repository_id", "kind", "version", "source_key", "content_hash")} for item in bundle]
    return {
        "semantic_key": digest(semantic), "state_key": digest(payload["state"]),
        "evidence_key": digest(sorted(identity, key=lambda item: item["id"])),
        "request_model": payload["model"],
    }


SOURCE_QUERY = """SELECT i.*,j.model request_model,j.policy_id source_policy_id,
    j.policy_version_id source_policy_version_id,j.updated_at source_updated_at,
    j.scope_json,v.revision source_policy_revision,v.document_json
    FROM job_items i JOIN jobs j ON j.id=i.job_id
    JOIN policy_versions v ON v.id=j.policy_version_id WHERE i.id=?"""


def index_completed_item(conn, source_id: int):
    """只从完整成功的真实请求回填索引；不信任旧的 applied 字段。"""
    source = conn.execute(SOURCE_QUERY, (source_id,)).fetchone()
    if source is None or source["status"] != "completed" or source["source_job_item_id"] is not None:
        return None
    if not source["request_json"] or not source["response_json"]:
        return None
    try:
        policy = normalize_policy(task_policy(json.loads(source["document_json"]), json.loads(source["scope_json"])))
        payload = json.loads(source["request_json"])
        response = json.loads(source["response_json"])
        results, served_model = validate_response(response, policy)
        if payload.get("model") != source["request_model"] or source["model"] != served_model:
            return None
        bundle = load_bundle(conn, source["repository_id"], json.loads(source["evidence_ids_json"]))
        if {item["kind"] for item in bundle} != set(policy["evidence_kinds"]):
            return None
        keys = request_keys(payload, policy, bundle)
        tags = {tag["id"]: tag_key(tag) for tag in policy["tags"]}
        scores = {tags[result["tag_id"]]: result["score"] for result in results}
        winner = None
        if policy["mode"] == "single_label":
            chosen = response["answers"]["classification"]["choice"]
            winner = tags[chosen] if chosen != NONE_OPTION else None
        values = {
            "source_job_item_id": source_id, "repository_id": source["repository_id"],
            "served_model": served_model, **keys, "scores_json": encode(scores),
            "winner_key": winner, "mode": policy["mode"],
            "scored_at": source["finished_at"] or source["source_updated_at"],
        }
    except (ValueError, KeyError, TypeError, JevError):
        return None
    existing = conn.execute("SELECT * FROM score_cache WHERE source_job_item_id=?", (source_id,)).fetchone()
    if existing is not None:
        # 来源及索引不可悄悄变更，否则历史版本的评分会被改写。
        return existing if all(existing[key] == value for key, value in values.items()) else None
    columns = list(values)
    conn.execute("INSERT INTO score_cache(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", [values[key] for key in columns])
    return conn.execute("SELECT * FROM score_cache WHERE source_job_item_id=?", (source_id,)).fetchone()


def evaluate_scores(policy: dict, cached) -> tuple[list[dict], str | None]:
    policy = normalize_policy(policy)
    scores = json.loads(cached["scores_json"])
    expected = {tag_key(tag) for tag in policy["tags"]}
    if set(scores) != expected or cached["mode"] != policy["mode"]:
        raise ValueError("标签定义与保存分数不兼容")
    results, winner_id = [], None
    for tag in policy["tags"]:
        key = tag_key(tag)
        score = scores[key]
        is_winner = cached["winner_key"] == key
        applied = score >= tag["threshold"] and (policy["mode"] == "multi_label" or is_winner)
        results.append({"tag_id": tag["id"], "label": tag["label"], "score": score,
                        "threshold": tag["threshold"], "applied": applied})
        if policy["mode"] == "single_label" and is_winner:
            winner_id = tag["id"]
    return results, winner_id


def bind_result(conn, version_id: str, cached, *, overwrite: bool = False) -> bool:
    """绑定分数来源并更新该版本的派生标签；绝不写入源响应或源费用。"""
    version = conn.execute("SELECT * FROM policy_versions WHERE id=?", (version_id,)).fetchone()
    if version is None:
        return False
    existing = conn.execute("SELECT source_job_item_id FROM policy_results WHERE policy_version_id=? AND repository_id=?", (version_id, cached["repository_id"])).fetchone()
    if existing is not None and not overwrite:
        return False
    results, _ = evaluate_scores(json.loads(version["document_json"]), cached)
    conn.execute("INSERT INTO policy_results(policy_version_id,repository_id,source_job_item_id,bound_at) VALUES(?,?,?,?) ON CONFLICT(policy_version_id,repository_id) DO UPDATE SET source_job_item_id=excluded.source_job_item_id,bound_at=excluded.bound_at",
                 (version_id, cached["repository_id"], cached["source_job_item_id"], now()))
    conn.execute("DELETE FROM tag_assignments WHERE repository_id=? AND policy_id=? AND policy_revision=?", (cached["repository_id"], version["policy_id"], version["revision"]))
    for result in results:
        if result["applied"]:
            conn.execute("INSERT INTO tag_assignments(repository_id,policy_id,policy_revision,tag_id,label,score,job_item_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (cached["repository_id"], version["policy_id"], version["revision"], result["tag_id"], result["label"], result["score"], cached["source_job_item_id"], now()))
    return True


def find_compatible_score(conn, repository_id: int, policy: dict, evidence_ids: list[str], request_model: str, preferred_source_id=None):
    bundle = load_bundle(conn, repository_id, evidence_ids)
    if {item["kind"] for item in bundle} != set(policy["evidence_kinds"]):
        return None
    wanted = request_keys(jev.build_payload(policy, bundle, request_model), policy, bundle)
    rows = conn.execute("SELECT * FROM score_cache WHERE repository_id=? AND request_model=? AND semantic_key=? AND state_key=? AND evidence_key=? ORDER BY source_job_item_id DESC",
                        (repository_id, wanted["request_model"], wanted["semantic_key"], wanted["state_key"], wanted["evidence_key"])).fetchall()
    if preferred_source_id is not None:
        rows.sort(key=lambda row: row["source_job_item_id"] != preferred_source_id)
    for candidate in rows:
        verified = index_completed_item(conn, candidate["source_job_item_id"])
        if verified is not None:
            return verified
    return None


def bind_available_scores(conn, version_id: str, request_model: str, preferred_version_id=None) -> int:
    """新版本优先继承指定历史版本，再补入其余兼容来源。

    restore 指定 v1 时不能让全局较新的 v2 评分覆盖 v1 原来的来源。
    """
    version = conn.execute("SELECT document_json FROM policy_versions WHERE id=?", (version_id,)).fetchone()
    policy = normalize_policy(json.loads(version["document_json"]))
    semantic = request_keys(jev.build_payload(policy, [], request_model), policy, [])
    candidates = {row["repository_id"] for row in conn.execute("SELECT DISTINCT repository_id FROM score_cache WHERE request_model=? AND semantic_key=?", (request_model, semantic["semantic_key"]))}
    preferred = {}
    if preferred_version_id:
        preferred = {row["repository_id"]: row["source_job_item_id"] for row in conn.execute("SELECT repository_id,source_job_item_id FROM policy_results WHERE policy_version_id=?", (preferred_version_id,))}
        candidates.update(preferred)
    count = 0
    for repository_id in sorted(candidates):
        try:
            cached = None
            if repository_id in preferred:
                origin = index_completed_item(conn, preferred[repository_id])
                if origin is not None:
                    source = conn.execute(SOURCE_QUERY, (preferred[repository_id],)).fetchone()
                    if policy_semantic_key(json.loads(source["document_json"])) == policy_semantic_key(policy):
                        cached = origin
            if cached is None:
                evidence_ids = latest_evidence_ids(conn, repository_id, policy["evidence_kinds"])
                cached = find_compatible_score(conn, repository_id, policy, evidence_ids, request_model)
            if cached is not None and bind_result(conn, version_id, cached):
                count += 1
        except (ValueError, KeyError, TypeError):
            continue
    return count


def record_completed_score(conn, source_id: int) -> None:
    cached = index_completed_item(conn, source_id)
    if cached is None:
        return
    source = conn.execute(SOURCE_QUERY, (source_id,)).fetchone()
    bind_result(conn, source["source_policy_version_id"], cached, overwrite=True)
    current = conn.execute("SELECT p.current_revision,v.* FROM policies p JOIN policy_versions v ON v.policy_id=p.id AND v.revision=p.current_revision WHERE p.id=? AND p.archived=0", (source["source_policy_id"],)).fetchone()
    if current is not None and current["id"] != source["source_policy_version_id"]:
        policy = normalize_policy(json.loads(current["document_json"]))
        try:
            if policy_semantic_key(json.loads(source["document_json"])) == policy_semantic_key(policy):
                bind_result(conn, current["id"], cached)
        except (ValueError, KeyError, TypeError):
            pass


def backfill_scores(conn) -> None:
    """为旧的完成记录建立索引和原版本结果，不产生任何 HTTP 请求。"""
    # v2 的当前标签已保存实际使用的源 item。先捕获它们，不能在回填时
    # 让升序遇到的最旧响应覆盖用户最后一次分类的结果。
    preferred = {}
    for row in conn.execute("SELECT policy_id,policy_revision,repository_id,job_item_id FROM tag_assignments"):
        key = (row["policy_id"], row["policy_revision"], row["repository_id"])
        preferred.setdefault(key, set()).add(row["job_item_id"])
    rows = conn.execute("SELECT i.id,j.policy_version_id,j.policy_id,v.revision,i.repository_id FROM job_items i JOIN jobs j ON j.id=i.job_id JOIN policy_versions v ON v.id=j.policy_version_id WHERE i.status='completed' AND i.source_job_item_id IS NULL ORDER BY i.finished_at DESC,i.id DESC").fetchall()
    choices = {}
    for row in rows:
        cached = index_completed_item(conn, row["id"])
        if cached is not None:
            identity = (row["policy_version_id"], row["repository_id"])
            materialized = preferred.get((row["policy_id"], row["revision"], row["repository_id"]), set())
            # 无明确旧来源时，按最新完成记录选取；明确来源仅在其响应有效时优先。
            if identity not in choices or materialized == {row["id"]}:
                choices[identity] = cached
    for (version_id, _), cached in choices.items():
        # 新版已经绑定的历史结果绝不因后续启动/回填而改变。
        bind_result(conn, version_id, cached)


def result_document(conn, version, cached, repository, *, policy: dict | None = None) -> dict:
    source = conn.execute(SOURCE_QUERY, (cached["source_job_item_id"],)).fetchone()
    results, winner = evaluate_scores(policy if policy is not None else json.loads(version["document_json"]), cached)
    return {
        "repository_id": repository["id"], "full_name": repository["full_name"], "url": repository["url"],
        "stars": repository["stars"], "language": repository["language"],
        "model": cached["served_model"], "scored_at": cached["scored_at"],
        "source_job_id": source["job_id"], "source_policy_revision": source["source_policy_revision"],
        "evidence_ids": json.loads(source["evidence_ids_json"]), "winner_tag_id": winner, "results": results,
    }


def version_result_rows(conn, version_id: str, *, policy: dict | None = None) -> list[dict]:
    """读取保存分数；可用临时阈值预览判定，不修改策略或历史结果。"""
    version = conn.execute("SELECT * FROM policy_versions WHERE id=?", (version_id,)).fetchone()
    rows = conn.execute("SELECT c.*,r.full_name,r.url,r.stars,r.language,r.id repository_identity FROM policy_results pr JOIN score_cache c ON c.source_job_item_id=pr.source_job_item_id JOIN repositories r ON r.id=pr.repository_id WHERE pr.policy_version_id=? ORDER BY r.stars DESC,r.id", (version_id,)).fetchall()
    result = []
    for row in rows:
        repository = {"id": row["repository_identity"], "full_name": row["full_name"], "url": row["url"], "stars": row["stars"], "language": row["language"]}
        result.append(result_document(conn, version, row, repository, policy=policy))
    return result


def result_counts(conn, version_id: str) -> tuple[int, int]:
    version = conn.execute("SELECT policy_id,revision FROM policy_versions WHERE id=?", (version_id,)).fetchone()
    total = conn.execute("SELECT COUNT(*) FROM policy_results WHERE policy_version_id=?", (version_id,)).fetchone()[0]
    applied = conn.execute("SELECT COUNT(DISTINCT ta.repository_id) FROM tag_assignments ta JOIN policy_results pr ON pr.repository_id=ta.repository_id AND pr.policy_version_id=? WHERE ta.policy_id=? AND ta.policy_revision=?", (version_id, version["policy_id"], version["revision"])).fetchone()[0]
    return total, applied


def tag_statistics(policy: dict, rows: list[dict]) -> list[dict]:
    statistics = []
    for tag in policy["tags"]:
        counts = Counter()
        scored = applied = 0
        for row in rows:
            result = next(item for item in row["results"] if item["tag_id"] == tag["id"])
            scored += 1
            applied += result["applied"]
            if policy["mode"] == "multi_label" or row["winner_tag_id"] == tag["id"]:
                counts[result["score"]] += 1
        statistics.append({"tag_id": tag["id"], "label": tag["label"], "threshold": tag["threshold"],
                           "scored_count": scored, "applied_count": applied,
                           "distribution": [{"score": score, "count": count} for score, count in sorted(counts.items())]})
    return statistics
