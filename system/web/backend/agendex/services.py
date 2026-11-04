"""仓库、策略及任务的数据库查询；HTTP 与后台任务复用同一语义。"""

import hashlib
import json
import uuid

from .db import now
from .evidence import encode, evidence_document
from . import annotations, scoring
from .jev import DEFAULT_MODEL
from .schemas import validate_filter_tag_ids


class ServiceError(ValueError):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.status_code = status_code


def repository_filters(filters) -> tuple[str, list, str]:
    filters = filters or {}
    where = ["r.catalog_visible = 1", "r.stars >= ?"]
    params = [int(filters.get("min_stars", 0))]
    q = str(filters.get("q") or "").strip()
    if q:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(r.full_name LIKE ? ESCAPE '\\' OR COALESCE(r.description,'') LIKE ? ESCAPE '\\' OR r.topics_json LIKE ? ESCAPE '\\')")
        params.extend([pattern, pattern, pattern])
    if filters.get("language"):
        where.append("r.language = ?")
        params.append(filters["language"])
    if filters.get("retention_source"):
        if filters["retention_source"] not in {"metadata_prescreen", "awesome_readme_reference", "legacy_topic"}:
            raise ServiceError("不支持的候选来源")
        where.append("r.retention_source = ?")
        params.append(filters["retention_source"])
    try:
        tag_ids = validate_filter_tag_ids(filters.get("tag_ids") or [])
    except ValueError as exc:
        raise ServiceError(str(exc), 422) from None
    # A nonempty multi-selection replaces the old single-tag parameter.
    # One EXISTS / IN clause implements ANY semantics without duplicate rows.
    selected_tags = tag_ids or ([filters["tag_id"]] if filters.get("tag_id") else [])
    if filters.get("policy_id") or selected_tags:
        extra = []
        if filters.get("policy_id"):
            extra.append("ta.policy_id=?")
            params.append(filters["policy_id"])
        if selected_tags:
            extra.append("ta.tag_id IN (" + ",".join("?" for _ in selected_tags) + ")")
            params.extend(selected_tags)
        where.append("EXISTS(SELECT 1 FROM effective_tag_assignments ta WHERE ta.repository_id=r.id AND " + " AND ".join(extra) + ")")
    sort = {"stars": "r.stars DESC,r.id", "updated": "r.updated_at DESC,r.id", "name": "r.full_name COLLATE NOCASE,r.id"}.get(filters.get("sort", "stars"))
    if sort is None:
        raise ServiceError("不支持的排序")
    return " AND ".join(where), params, sort


def repository_document(row) -> dict:
    result = dict(row)
    result["topics"] = json.loads(result.pop("topics_json"))
    result["archived"] = bool(result["archived"])
    result["prescreen_probability"] = result["agent_probability"]
    result["protected_keep"] = bool(result["protected_keep"])
    result["supplement"] = json.loads(result.pop("supplement_json")) or None
    result.pop("imported_at", None)
    result.pop("catalog_visible", None)
    result["tags"] = []
    return result


def attach_tags(conn, repositories: list[dict]) -> None:
    if not repositories:
        return
    by_id = {row["id"]: row for row in repositories}
    placeholders = ",".join("?" for _ in repositories)
    rows = conn.execute(
        "SELECT ta.* FROM effective_tag_assignments ta WHERE ta.repository_id IN (" + placeholders + ") ORDER BY ta.label",
        list(by_id),
    ).fetchall()
    for row in rows:
        by_id[row["repository_id"]]["tags"].append({key: row[key] for key in ("policy_id", "tag_id", "label", "score", "source")})


def list_repositories(conn, filters: dict, page: int = 1, page_size: int = 25) -> dict:
    where, params, sort = repository_filters(filters)
    total = conn.execute("SELECT COUNT(*) FROM repositories r WHERE " + where, params).fetchone()[0]
    rows = conn.execute("SELECT r.* FROM repositories r WHERE " + where + " ORDER BY " + sort + " LIMIT ? OFFSET ?", params + [page_size, (page - 1) * page_size]).fetchall()
    items = [repository_document(row) for row in rows]
    attach_tags(conn, items)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_repository(conn, identifier: int) -> dict:
    row = conn.execute("SELECT * FROM repositories WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise ServiceError("仓库不存在", 404)
    document = repository_document(row)
    attach_tags(conn, [document])
    document["evidence"] = [evidence_document(item) for item in conn.execute("SELECT * FROM evidence WHERE repository_id=? ORDER BY captured_at DESC,rowid DESC", (identifier,))]
    rows = conn.execute(
        "SELECT c.*,i.job_id,i.evidence_ids_json,v.policy_id,v.revision,v.document_json,pr.bound_at FROM policy_results pr JOIN policy_versions v ON v.id=pr.policy_version_id JOIN score_cache c ON c.source_job_item_id=pr.source_job_item_id JOIN job_items i ON i.id=c.source_job_item_id WHERE pr.repository_id=? ORDER BY pr.bound_at DESC,v.revision DESC LIMIT 50", (identifier,),
    ).fetchall()
    document["classifications"] = [{
        "policy_id": item["policy_id"], "policy_name": json.loads(item["document_json"])["name"],
        "policy_revision": item["revision"], "job_id": item["job_id"], "model": item["served_model"],
        "created_at": item["bound_at"], "evidence_ids": json.loads(item["evidence_ids_json"]),
        "results": scoring.evaluate_scores(json.loads(item["document_json"]), item)[0],
    } for item in rows]
    return document


def get_stats(conn) -> dict:
    dataset = conn.execute("SELECT value FROM app_meta WHERE key='dataset'").fetchone()
    return {
        "total_repositories": conn.execute("SELECT COUNT(*) FROM repositories WHERE catalog_visible=1").fetchone()[0],
        "tagged_repositories": conn.execute("SELECT COUNT(DISTINCT ta.repository_id) FROM effective_tag_assignments ta JOIN repositories r ON r.id=ta.repository_id WHERE r.catalog_visible=1").fetchone()[0],
        "policy_count": conn.execute("SELECT COUNT(*) FROM policies WHERE archived=0").fetchone()[0],
        "active_jobs": conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0],
        "languages": [{"name": row["language"], "count": row["n"]} for row in conn.execute("SELECT language,COUNT(*) n FROM repositories WHERE catalog_visible=1 AND language IS NOT NULL GROUP BY language ORDER BY n DESC,language")],
        "dataset": json.loads(dataset["value"]) if dataset else {"source_count": 0, "selected_count": 0, "threshold": 0.69, "imported_at": None},
    }


def get_policy(conn, identifier: str, include_archived: bool = False, revision: int | None = None) -> dict:
    revision_filter = "v.revision=p.current_revision" if revision is None else "v.revision=?"
    parameters = [identifier] if revision is None else [revision, identifier]
    row = conn.execute(
        "SELECT p.*,v.id version_id,v.revision selected_revision,v.created_at version_created_at,v.document_json FROM policies p JOIN policy_versions v ON v.policy_id=p.id AND " + revision_filter + " WHERE p.id=?" + ("" if include_archived else " AND p.archived=0"), parameters,
    ).fetchone()
    if row is None:
        raise ServiceError("策略不存在", 404)
    counts = scoring.result_counts(conn, row["version_id"])
    if row["selected_revision"] == row["current_revision"]:
        counts = (
            conn.execute("SELECT COUNT(*) FROM (SELECT repository_id FROM policy_results WHERE policy_version_id=? UNION SELECT repository_id FROM active_manual_annotations WHERE policy_id=?)", (row["version_id"], identifier)).fetchone()[0],
            conn.execute("SELECT COUNT(DISTINCT repository_id) FROM effective_tag_assignments WHERE policy_id=?", (identifier,)).fetchone()[0],
        )
    return {**scoring.normalize_policy(json.loads(row["document_json"])), "id": identifier,
            "revision": row["selected_revision"], "created_at": row["created_at"],
            "updated_at": row["version_created_at"], "result_count": counts[0], "applied_count": counts[1]}


def list_policies(conn) -> list[dict]:
    return [get_policy(conn, row["id"]) for row in conn.execute("SELECT id FROM policies WHERE archived=0 ORDER BY updated_at DESC,rowid DESC")]


def policy_save_identity(document: dict) -> dict:
    """Compare editable content; fresh tag IDs do not create a new policy.

    Label order remains part of the saved layout. Policy names and thresholds
    also matter here, unlike the semantic matching used to reuse model scores.
    """
    document = scoring.normalize_policy(document)
    return {**{key: document[key] for key in ("name", "description", "mode", "threshold")},
            "tags": [{key: tag[key] for key in ("label", "description", "threshold")} for tag in document["tags"]]}


def save_policy(conn, body, identifier: str | None = None, request_model: str = DEFAULT_MODEL,
                preferred_version_id=None, *, deduplicate: bool = True) -> dict:
    # Direct service callers need the same atomic check-and-save as HTTP callers.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    document = body.model_dump()
    for tag in document["tags"]:
        tag["id"] = tag["id"] or uuid.uuid4().hex
        if tag["id"] == "__none__":
            raise ServiceError("标签 ID 使用了保留值")
    document = scoring.normalize_policy(document)
    previous = get_policy(conn, identifier) if identifier else None
    if deduplicate:
        identity = policy_save_identity(document)
        if previous is not None:
            if policy_save_identity(previous) == identity:
                return previous
        else:
            rows = conn.execute(
                "SELECT p.id,v.document_json FROM policies p JOIN policy_versions v ON v.policy_id=p.id AND v.revision=p.current_revision WHERE p.archived=0 AND p.name=? ORDER BY p.updated_at DESC,p.rowid DESC",
                (document["name"],),
            )
            for row in rows:
                if policy_save_identity(json.loads(row["document_json"])) == identity:
                    return get_policy(conn, row["id"])
    if previous is not None and preferred_version_id is None:
        preferred_version_id = conn.execute("SELECT id FROM policy_versions WHERE policy_id=? AND revision=?", (identifier, previous["revision"])).fetchone()["id"]
    identifier = identifier or uuid.uuid4().hex
    revision = previous["revision"] + 1 if previous else 1
    timestamp = now()
    if previous:
        conn.execute("UPDATE policies SET name=?,current_revision=?,updated_at=? WHERE id=?", (document["name"], revision, timestamp, identifier))
    else:
        conn.execute("INSERT INTO policies(id,name,current_revision,created_at,updated_at) VALUES(?,?,?,?,?)", (identifier, document["name"], revision, timestamp, timestamp))
    payload = encode(document)
    version_id = uuid.uuid4().hex
    conn.execute("INSERT INTO policy_versions(id,policy_id,revision,document_json,content_hash,created_at) VALUES(?,?,?,?,?,?)", (version_id, identifier, revision, payload, hashlib.sha256(payload.encode("utf-8")).hexdigest(), timestamp))
    scoring.bind_available_scores(conn, version_id, request_model, preferred_version_id)
    return get_policy(conn, identifier)


def policy_versions(conn, identifier: str) -> list[dict]:
    get_policy(conn, identifier)
    return [get_policy(conn, identifier, revision=row["revision"]) for row in conn.execute("SELECT revision FROM policy_versions WHERE policy_id=? ORDER BY revision DESC", (identifier,))]


def policy_results(conn, identifier: str, revision: int | None = None, q: str = "", page: int = 1, page_size: int = 25) -> dict:
    policy = get_policy(conn, identifier, revision=revision)
    version = conn.execute("SELECT id FROM policy_versions WHERE policy_id=? AND revision=?", (identifier, policy["revision"])).fetchone()
    rows = annotations.result_rows(conn, policy, scoring.version_result_rows(conn, version["id"]))
    stats = annotations.tag_statistics(policy, rows)
    if q.strip():
        query = q.strip().casefold()
        rows = [row for row in rows if query in row["full_name"].casefold()]
    total = len(rows)
    return {"policy": policy, "items": rows[(page - 1) * page_size:page * page_size],
            "total": total, "page": page, "page_size": page_size, "tag_stats": stats}


def update_thresholds(conn, identifier: str, body, request_model: str) -> dict:
    from .schemas import PolicyInput
    policy = get_policy(conn, identifier)
    if policy["revision"] != body.base_revision:
        raise ServiceError("策略已经更新，请刷新后再应用阈值", 409)
    allowed = {tag["id"] for tag in policy["tags"]}
    if not set(body.thresholds) <= allowed:
        raise ServiceError("阈值包含不属于该策略的标签")
    document = {key: policy[key] for key in ("name", "description", "mode", "threshold", "tags", "evidence_kinds")}
    for tag in document["tags"]:
        if tag["id"] in body.thresholds:
            tag["threshold"] = body.thresholds[tag["id"]]
    return save_policy(conn, PolicyInput(**document), identifier, request_model=request_model, deduplicate=False)


def restore_policy(conn, identifier: str, revision: int, request_model: str) -> dict:
    from .schemas import PolicyInput
    policy = get_policy(conn, identifier, revision=revision)
    document = {key: policy[key] for key in ("name", "description", "mode", "threshold", "tags", "evidence_kinds")}
    version_id = conn.execute("SELECT id FROM policy_versions WHERE policy_id=? AND revision=?", (identifier, revision)).fetchone()["id"]
    return save_policy(conn, PolicyInput(**document), identifier, request_model=request_model, preferred_version_id=version_id, deduplicate=False)


def archive_policy(conn, identifier: str) -> None:
    get_policy(conn, identifier)
    conn.execute("UPDATE policies SET archived=1,updated_at=? WHERE id=?", (now(), identifier))


JOB_JOIN = "SELECT j.*,v.revision,v.document_json FROM jobs j JOIN policy_versions v ON v.id=j.policy_version_id"


def job_document(row) -> dict:
    policy = scoring.task_policy(json.loads(row["document_json"]), json.loads(row["scope_json"]))
    return {**{key: row[key] for key in ("id", "policy_id", "status", "total", "completed", "failed", "skipped", "cost", "unknown_cost_count", "created_at", "updated_at", "error", "model")},
            "policy_name": policy["name"], "policy_revision": row["revision"], "evidence_kinds": policy["evidence_kinds"]}


def get_job(conn, identifier: str, detail: bool = False) -> dict:
    row = conn.execute(JOB_JOIN + " WHERE j.id=?", (identifier,)).fetchone()
    if row is None:
        raise ServiceError("任务不存在", 404)
    result = job_document(row)
    if detail:
        result["items"] = [{
            "id": item["id"], "repository_id": item["repository_id"], "full_name": item["full_name"],
            "status": item["status"], "error": item["error"], "cost": item["cost"],
            "model": item["model"], "started_at": item["started_at"], "finished_at": item["finished_at"],
        } for item in conn.execute("SELECT i.*,r.full_name FROM job_items i JOIN repositories r ON r.id=i.repository_id WHERE i.job_id=? ORDER BY i.id DESC LIMIT 100", (identifier,))]
    return result


def list_jobs(conn) -> list[dict]:
    return [job_document(row) for row in conn.execute(JOB_JOIN + " ORDER BY j.created_at DESC,j.rowid DESC LIMIT 200")]
