"""Editable human labels layered over immutable model scores and responses.

An override belongs to a tag's meaning, not its threshold. Changing the shared
rule, classification mode, or tag definition makes an old decision inactive;
threshold changes and model reruns do not silently replace human decisions.
"""

import hashlib
import json
from collections import Counter

from pydantic import Field, StrictBool, model_validator

from .db import now
from .schemas import InputModel
from . import scoring


class TagOverride(InputModel):
    tag_id: str = Field(min_length=1, max_length=80)
    applied: StrictBool


class AnnotationInput(InputModel):
    policy_id: str = Field(min_length=1, max_length=80)
    base_revision: int = Field(ge=1)
    overrides: list[TagOverride] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def unique_tags(self):
        if len({item.tag_id for item in self.overrides}) != len(self.overrides):
            raise ValueError("人工标注包含重复标签")
        return self


def rule_fields(policy: dict, tag: dict) -> tuple:
    return policy["mode"], policy["description"], tag["label"], tag["description"]


def rule_key(policy: dict, tag: dict) -> str:
    encoded = json.dumps(rule_fields(policy, tag), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def current_overrides(conn, repository_id: int, policy: dict) -> dict[str, bool]:
    """Historical model-result views remain unchanged by today's annotations."""
    current = conn.execute("SELECT current_revision FROM policies WHERE id=?", (policy["id"],)).fetchone()
    if current is None or current["current_revision"] != policy["revision"]:
        return {}
    return {row["tag_id"]: bool(row["applied"]) for row in conn.execute(
        "SELECT tag_id,applied FROM active_manual_annotations WHERE repository_id=? AND policy_id=?",
        (repository_id, policy["id"]),
    )}


def overlay_results(policy: dict, model_results: list[dict], overrides: dict[str, bool]) -> list[dict]:
    models = {item["tag_id"]: item for item in model_results}
    has_single_choice = policy["mode"] == "single_label" and any(overrides.values())
    results = []
    for tag in policy["tags"]:
        model = models.get(tag["id"])
        decision = overrides.get(tag["id"])
        # A human single-label choice excludes the other automatic choices even
        # when a later threshold preview would otherwise include them.
        if decision is None and has_single_choice:
            decision = False
        score = model["score"] if model else None
        model_applied = bool(model and model["applied"])
        results.append({
            "tag_id": tag["id"], "label": tag["label"], "threshold": tag["threshold"],
            "score": (1.0 if decision else 0.0) if decision is not None else score,
            "applied": decision if decision is not None else model_applied,
            "source": "manual" if decision is not None else "model" if model else None,
            "model_score": score, "model_applied": model_applied, "manual_applied": decision,
        })
    return results


def repository_annotations(conn, repository_id: int) -> dict:
    from . import services
    if conn.execute("SELECT 1 FROM repositories WHERE id=?", (repository_id,)).fetchone() is None:
        raise services.ServiceError("仓库不存在", 404)
    items = []
    for policy in services.list_policies(conn):
        source = conn.execute(
            "SELECT c.* FROM policy_results pr JOIN policy_versions v ON v.id=pr.policy_version_id "
            "JOIN score_cache c ON c.source_job_item_id=pr.source_job_item_id "
            "WHERE pr.repository_id=? AND v.policy_id=? AND v.revision=?",
            (repository_id, policy["id"], policy["revision"]),
        ).fetchone()
        model_results = scoring.evaluate_scores(policy, source)[0] if source else []
        overrides = current_overrides(conn, repository_id, policy)
        projected = overlay_results(policy, model_results, overrides)
        definitions = {tag["id"]: tag for tag in policy["tags"]}
        tags = [{**result, "description": definitions[result["tag_id"]]["description"],
                 # Editing sends only explicit decisions back to the server.
                 "manual_applied": overrides.get(result["tag_id"])} for result in projected]
        items.append({"policy_id": policy["id"], "policy_name": policy["name"],
                      "policy_revision": policy["revision"], "mode": policy["mode"], "tags": tags})
    return {"items": items}


def replace_annotations(conn, repository_id: int, body: AnnotationInput) -> dict:
    from . import services
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    repository = conn.execute("SELECT 1 FROM repositories WHERE id=?", (repository_id,)).fetchone()
    if repository is None:
        raise services.ServiceError("仓库不存在", 404)
    policy = services.get_policy(conn, body.policy_id)
    if policy["revision"] != body.base_revision:
        raise services.ServiceError("策略已经更新，请刷新后再保存人工标注", 409)
    definitions = {tag["id"]: tag for tag in policy["tags"]}
    if any(item.tag_id not in definitions for item in body.overrides):
        raise services.ServiceError("人工标注包含不属于该策略的标签")
    if policy["mode"] == "single_label" and sum(item.applied for item in body.overrides) > 1:
        raise services.ServiceError("单标签策略只能人工选中一个标签")
    # Replace only the current meanings. Inactive decisions for older rules are
    # retained, and resetting to automatic never modifies the model's records.
    for tag in policy["tags"]:
        conn.execute("DELETE FROM manual_annotations WHERE repository_id=? AND policy_id=? AND tag_id=? AND rule_key=?",
                     (repository_id, policy["id"], tag["id"], rule_key(policy, tag)))
    for item in body.overrides:
        tag = definitions[item.tag_id]
        conn.execute(
            "INSERT INTO manual_annotations(repository_id,policy_id,tag_id,rule_key,policy_mode,policy_description,tag_label,tag_description,applied,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (repository_id, policy["id"], tag["id"], rule_key(policy, tag), *rule_fields(policy, tag), int(item.applied), now()),
        )
    return services.get_repository(conn, repository_id)


def result_rows(conn, policy: dict, model_rows: list[dict]) -> list[dict]:
    """Current effective results, including repositories annotated before scoring."""
    current = conn.execute("SELECT current_revision FROM policies WHERE id=?", (policy["id"],)).fetchone()
    if current is None or current["current_revision"] != policy["revision"]:
        return model_rows
    rows = {row["repository_id"]: {**row} for row in model_rows}
    overrides = {}
    for item in conn.execute("SELECT * FROM active_manual_annotations WHERE policy_id=?", (policy["id"],)):
        overrides.setdefault(item["repository_id"], {})[item["tag_id"]] = bool(item["applied"])
    missing = set(overrides) - set(rows)
    for repository_id in missing:
        repository = conn.execute("SELECT * FROM repositories WHERE id=?", (repository_id,)).fetchone()
        rows[repository_id] = {"repository_id": repository_id, "full_name": repository["full_name"], "url": repository["url"],
                              "stars": repository["stars"], "language": repository["language"], "model": None,
                              "scored_at": None, "source_job_id": None, "source_policy_revision": policy["revision"],
                              "evidence_ids": [], "winner_tag_id": None, "results": []}
    for repository_id, row in rows.items():
        decisions = overrides.get(repository_id, {})
        row["results"] = overlay_results(policy, row["results"], decisions)
        if policy["mode"] == "single_label" and any(decisions.values()):
            row["winner_tag_id"] = next(key for key, value in decisions.items() if value)
    return sorted(rows.values(), key=lambda row: (-row["stars"], row["repository_id"]))


def tag_statistics(policy: dict, rows: list[dict]) -> list[dict]:
    statistics = []
    for tag in policy["tags"]:
        counts = Counter()
        scored = applied = manual_applied = 0
        for row in rows:
            result = next(item for item in row["results"] if item["tag_id"] == tag["id"])
            scored += result["score"] is not None
            applied += result["applied"]
            manual_applied += result.get("manual_applied") is True
            if result.get("manual_applied") is None and result["score"] is not None:
                if policy["mode"] == "multi_label" or row["winner_tag_id"] == tag["id"]:
                    counts[result["score"]] += 1
        statistics.append({"tag_id": tag["id"], "label": tag["label"], "threshold": tag["threshold"],
                           "scored_count": scored, "applied_count": applied, "manual_applied_count": manual_applied,
                           "distribution": [{"score": score, "count": count} for score, count in sorted(counts.items())]})
    return statistics
