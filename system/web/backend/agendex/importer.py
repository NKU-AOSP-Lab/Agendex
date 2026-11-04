"""将 Search 原始快照导入网站工作库，不修改采集文件或重新调用模型。

仓库表保存便于检索的字段，metadata evidence 保存同一快照的规范化证据。
来源文件的完整 SHA256、原始行号和分数文件身份保留在 provenance 中。
不同数据快照应导入新的工作库，避免静默改写现有任务依赖的证据。
"""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from . import db


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
KEYWORD_SCORING_ROOT = WORKSPACE_ROOT / "experiment" / "02_keyword" / "output" / "metadata-prescreen" / "scoring"
FINE_SCREEN_ROOT = WORKSPACE_ROOT / "experiment" / "02_keyword" / "output" / "fine-screen"


class ImportValidationError(ValueError):
    """输入损坏、不匹配或会覆盖已有数据快照。"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ImportValidationError(message)


@dataclass
class JsonlReader:
    """读原始字节时同时累计哈希，哈希恰好对应本次解释的数据。"""

    path: Path
    sha256: Optional[str] = None
    count: int = 0

    def rows(self) -> Iterator[Tuple[int, Dict[str, Any]]]:
        digest = hashlib.sha256()
        with self.path.open("rb") as stream:
            for line_number, raw in enumerate(stream, 1):
                digest.update(raw)
                require(bool(raw.strip()), "{} 第 {} 行为空".format(self.path.name, line_number))
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ImportValidationError("{} 第 {} 行不是有效 JSON".format(self.path.name, line_number)) from exc
                require(isinstance(row, dict), "{} 第 {} 行必须是对象".format(self.path.name, line_number))
                self.count += 1
                yield line_number, row
        self.sha256 = digest.hexdigest()


def checked_id(value: Any, context: str) -> int:
    require(type(value) is int and value > 0, "{} 的仓库 ID 无效".format(context))
    return value


def optional_string(value: Any, field: str) -> Optional[str]:
    require(value is None or isinstance(value, str), "{} 必须是字符串或 null".format(field))
    return value


def read_classifications(path: Path) -> Tuple[Dict[int, Dict[str, Any]], str]:
    reader = JsonlReader(path)
    records = {}
    for line_number, row in reader.rows():
        identifier = checked_id(row.get("repository_id"), "分类文件第 {} 行".format(line_number))
        require(identifier not in records, "分类文件存在重复仓库 ID：{}".format(identifier))
        require("agent_probability" in row, "分类记录缺少 agent_probability")
        score = row["agent_probability"]
        if score is not None:
            require(type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1,
                    "仓库 {} 的分数必须是 [0,1] 内的有限数值或 null".format(identifier))
        name = row.get("full_name")
        require(isinstance(name, str) and bool(name.strip()), "分类记录缺少 full_name")
        records[identifier] = {"score": score, "full_name": name}
    require(bool(records), "分类文件为空")
    assert reader.sha256 is not None
    return records, reader.sha256


def normalize_repository(row: Mapping[str, Any]) -> Dict[str, Any]:
    """只保存网站实际使用的字段；不把 API 路径和所有嵌套对象复制一遍。"""
    identifier = checked_id(row.get("id"), "原始仓库")
    name = row.get("full_name")
    require(isinstance(name, str) and bool(name.strip()), "原始仓库缺少 full_name")
    stars = row.get("stargazers_count")
    require(type(stars) is int and stars >= 0, "仓库 {} 的 Star 数量无效".format(identifier))
    topics = row.get("topics")
    require(isinstance(topics, list) and all(isinstance(topic, str) for topic in topics),
            "仓库 {} 的 topics 必须是字符串数组".format(identifier))
    archived = row.get("archived", False)
    require(type(archived) is bool, "archived 必须是布尔值")
    license_value = row.get("license")
    if isinstance(license_value, dict):
        license_name = license_value.get("spdx_id") or license_value.get("name") or license_value.get("key")
    else:
        license_name = license_value
    url = row.get("html_url") or "https://github.com/{}".format(name)
    require(isinstance(url, str) and url.startswith("https://"), "仓库 URL 必须是 HTTPS 地址")
    normalized = {
        "id": identifier, "full_name": name, "url": url,
        "description": optional_string(row.get("description"), "description"),
        "stars": stars, "language": optional_string(row.get("language"), "language"),
        "topics": topics, "license": optional_string(license_name, "license"),
        "default_branch": optional_string(row.get("default_branch"), "default_branch"),
        "created_at": optional_string(row.get("created_at"), "created_at"),
        "updated_at": optional_string(row.get("updated_at"), "updated_at"),
        "pushed_at": optional_string(row.get("pushed_at"), "pushed_at"), "archived": archived,
    }
    return normalized


def verify_source_binding(classification_path: Path, source_hash: str) -> None:
    """若采集实验提供了冻结的源哈希，必须核对；普通导入文件可无此旁文件。"""
    config_path = classification_path.parent / "config.json"
    if not config_path.is_file():
        return
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ImportValidationError("分类 config.json 无法读取") from exc
    require(isinstance(config, dict), "分类 config.json 必须是对象")
    expected = config.get("source_sha256")
    if expected is not None:
        require(expected == source_hash, "原始仓库 SHA256 与分类实验绑定的源文件不一致")


REPOSITORY_INSERT = """
INSERT INTO repositories (
    id,full_name,url,description,stars,language,topics_json,license,default_branch,
    created_at,updated_at,pushed_at,archived,agent_probability,imported_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def import_dataset(
    source_path: Path,
    classification_path: Path,
    db_path: Path,
    threshold: float = 0.69,
) -> Dict[str, Any]:
    """导入一个完整数据快照，返回统计；同源同阈值重复执行不写入。

    不根据 Description 是否存在重新筛选；唯一筛选条件是已保存的有效
    agent_probability >= threshold。历史 null 分数在本次分数分析范围外。
    """
    require(type(threshold) in (int, float) and math.isfinite(threshold) and 0 <= threshold <= 1,
            "阈值必须是 [0,1] 内的有限数值")
    source_path = Path(source_path).expanduser().resolve()
    classification_path = Path(classification_path).expanduser().resolve()
    target = Path(db_path).expanduser().resolve()
    require(target not in (source_path, classification_path), "数据库路径不能覆盖输入文件")
    require(source_path != classification_path, "仓库文件与分类文件不能是同一个文件")
    require(source_path.is_file() and classification_path.is_file(), "仓库文件或分类文件不存在")

    classifications, classification_hash = read_classifications(classification_path)
    reader = JsonlReader(source_path)
    selected = []
    seen = set()
    for line_number, raw in reader.rows():
        identifier = checked_id(raw.get("id"), "原始仓库第 {} 行".format(line_number))
        require(identifier not in seen, "原始仓库存在重复 ID：{}".format(identifier))
        seen.add(identifier)
        require(identifier in classifications, "仓库 {} 没有对应分类记录".format(identifier))
        classification = classifications[identifier]
        require(raw.get("full_name") == classification["full_name"],
                "仓库 {} 的 full_name 与分类记录不一致".format(identifier))
        score = classification["score"]
        if score is not None and score >= threshold:
            selected.append((line_number, normalize_repository(raw), float(score)))
    require(bool(seen), "原始仓库文件为空")
    require(seen == set(classifications), "分类文件包含原始仓库文件之外的仓库")
    assert reader.sha256 is not None
    source_hash = reader.sha256
    verify_source_binding(classification_path, source_hash)

    imported_at = db.now()
    scored_count = sum(row["score"] is not None for row in classifications.values())
    dataset = {
        "source_count": reader.count, "selected_count": len(selected), "threshold": float(threshold),
        "scored_count": scored_count, "unscored_count": reader.count - scored_count,
        "source_path": str(source_path), "classification_path": str(classification_path),
        "source_sha256": source_hash, "classification_sha256": classification_hash,
        "imported_at": imported_at, "import_format_version": 1,
    }
    return _persist_dataset(source_path, classification_path, target, selected, dataset)


def _persist_dataset(source_path, classification_path, target, selected, dataset,
                     candidate_info=None, readme_documents=None):
    """Write one immutable workspace snapshot, preserving unrelated policy/job history."""
    imported_at = dataset["imported_at"]
    source_hash = dataset["source_sha256"]
    classification_hash = dataset["classification_sha256"]
    db.initialize_database(target)
    with db.connection(target) as conn:
        # 锁住“检查身份 → 写入”这段事务，防止两次并行导入相互覆盖。
        conn.execute("BEGIN IMMEDIATE")
        existing_row = conn.execute("SELECT value FROM app_meta WHERE key='dataset'").fetchone()
        existing_count = conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0]
        if existing_row is not None:
            try:
                existing = json.loads(existing_row["value"])
            except ValueError as exc:
                raise ImportValidationError("已有 dataset 元信息损坏") from exc
            identity = ("source_sha256", "classification_sha256", "threshold")
            require(isinstance(existing, dict) and all(existing.get(key) == dataset[key] for key in identity),
                    "此数据库已有不同数据快照或阈值，请使用新数据库，避免覆盖历史证据和任务")
            require(existing_count == existing.get("selected_count") == len(selected),
                    "已有仓库数量与导入记录不一致，未自动覆盖修复")
            return {**existing, "status": "unchanged"}
        require(existing_count == 0, "数据库已存在仓库但没有快照身份，拒绝覆盖")

        version = "metadata-snapshot:{}".format(source_hash)
        for line_number, repo, score in selected:
            conn.execute(REPOSITORY_INSERT, (
                repo["id"], repo["full_name"], repo["url"], repo["description"], repo["stars"],
                repo["language"], canonical_json(repo["topics"]), repo["license"], repo["default_branch"],
                repo["created_at"], repo["updated_at"], repo["pushed_at"], int(repo["archived"]), score, imported_at,
            ))
            content = canonical_json(repo)
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            evidence_id = "metadata-" + hashlib.sha256(
                "{}:{}:{}".format(repo["id"], source_hash, content_hash).encode("utf-8")
            ).hexdigest()
            provenance = {
                "source_path": str(source_path), "source_sha256": source_hash, "source_line": line_number,
                "classification_path": str(classification_path), "classification_sha256": classification_hash,
                "captured_at_basis": "import_time; source has no per-repository retrieval timestamp",
                "version_kind": "metadata_snapshot_not_commit", "normalization_version": 1,
            }
            conn.execute("""
                INSERT INTO evidence (id,repository_id,kind,version,source_url,content,content_hash,provenance_json,captured_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (evidence_id, repo["id"], "metadata", version, repo["url"], content, content_hash,
                  canonical_json(provenance), imported_at))
            if candidate_info is not None:
                candidate = candidate_info[repo["id"]]
                conn.execute("""UPDATE repositories SET retention_source=?,protected_keep=?,
                    original_prescreen_decision=?,supplement_json=? WHERE id=?""", (
                    candidate["retention_source"], int(candidate["protected_keep"]),
                    candidate["original_prescreen_decision"], canonical_json(candidate["supplement"]), repo["id"],
                ))
        # Reuse the six already fetched supplemental READMEs locally; no network work.
        if readme_documents:
            from .evidence import add_evidence
            from .schemas import EvidenceInput
            for identifier, document in readme_documents:
                add_evidence(conn, identifier, EvidenceInput(**document))
        conn.execute("INSERT INTO app_meta(key,value) VALUES ('dataset',?)", (canonical_json(dataset),))
    return {**dataset, "status": "imported"}


def import_keyword_dataset(source_path: Path, scores_path: Path, db_path: Path) -> Dict[str, Any]:
    """Import retained membership, including null-score protection and Awesome overrides.

    The augmented file already embodies the screening decision. Applying a numeric
    cutoff here would silently discard protected and deliberately supplemented rows.
    Scores remain metadata, not proof that an implementation has been verified.
    """
    source_path, scores_path, target = (Path(path).expanduser().resolve()
                                        for path in (source_path, scores_path, db_path))
    require(source_path.is_file() and scores_path.is_file(), "候选清单或初筛分数文件不存在")
    require(target not in (source_path, scores_path), "数据库路径不能覆盖输入文件")
    score_reader, scores = JsonlReader(scores_path), {}
    for _, row in score_reader.rows():
        identifier = checked_id(row.get("repository_id"), "初筛分数")
        require(identifier not in scores, "初筛分数存在重复仓库 ID")
        value = row.get("prescreen_probability")
        require(value is None or (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1),
                "初筛分数必须为 [0,1] 有限数或 null")
        require(type(row.get("protected_keep")) is bool, "初筛保护状态必须为布尔值")
        scores[identifier] = row
    require(bool(scores), "初筛分数文件为空")

    selected, candidate_info, readme_documents, seen = [], {}, [], set()
    source_reader = JsonlReader(source_path)
    for line_number, raw in source_reader.rows():
        repo = normalize_repository(raw)
        identifier = repo["id"]
        require(identifier not in seen, "候选清单存在重复仓库 ID")
        seen.add(identifier)
        origin = raw.get("retention_source")
        require(origin in {"metadata_prescreen", "awesome_readme_reference"}, "候选缺少有效保留来源")
        score_record = scores.get(identifier)
        if score_record is not None:
            require(score_record.get("full_name") == repo["full_name"], "候选与初筛记录仓库名称不一致")
        supplement = {}
        if origin == "metadata_prescreen":
            require(score_record is not None and score_record.get("decision") == "keep", "初筛保留项缺少对应keep记录")
            probability = score_record["prescreen_probability"]
            protected = score_record["protected_keep"]
            original_decision = "keep"
        else:
            reviews = raw.get("reference_reviews")
            require(isinstance(reviews, list) and bool(reviews), "Awesome补入缺少文档复核")
            review = reviews[0]
            require(review.get("include") is True and review.get("repository_id") == identifier,
                    "Awesome复核与仓库不匹配")
            original_decision = "outside_search" if score_record is None else score_record["decision"]
            require(raw.get("original_prescreen_decision") == original_decision, "Awesome原初筛状态不一致")
            probability = None if score_record is None else score_record["prescreen_probability"]
            require(raw.get("prescreen_probability") == probability, "Awesome保留分数与初筛不一致")
            protected = False
            supplement = {"reason": review.get("reason", ""), "sources": review.get("sources", []),
                          "evidence_level": review.get("evidence_level"),
                          "source_or_runtime_verified": False}
            if review.get("metadata_readme_conflict"):
                supplement["metadata_readme_conflict"] = review["metadata_readme_conflict"]
            # Current reviews use workspace-relative paths; legacy paths still resolve
            # through the common adapter rather than modifying frozen records.
            if review.get("readme_content_file"):
                import sys
                if str(WORKSPACE_ROOT) not in sys.path:
                    sys.path.insert(0, str(WORKSPACE_ROOT))
                from system.workspace_paths import resolve_recorded_path
                content_path = resolve_recorded_path(review["readme_content_file"], base=WORKSPACE_ROOT)
                require(content_path.is_file(), "补入README原文不存在")
                readme_documents.append((identifier, {
                    "kind": "readme", "version": "git-blob:" + review["readme_blob_sha"],
                    "source_url": repo["url"] + "#readme", "content": content_path.read_text(encoding="utf-8"),
                    "provenance": {"source_path": str(content_path), "fetched_at": review.get("readme_fetched_at"),
                                   "review_basis": "Awesome supplemental document review; no source/runtime verification"},
                }))
        selected.append((line_number, repo, probability))
        candidate_info[identifier] = {"retention_source": origin, "protected_keep": protected,
                                      "original_prescreen_decision": original_decision, "supplement": supplement}
    require(bool(selected), "候选清单为空")
    thresholds = {row.get("threshold") for row in scores.values()}
    require(len(thresholds) == 1, "初筛记录的阈值不一致")
    threshold = thresholds.pop()
    require(type(threshold) in (int, float) and 0 <= threshold <= 1, "初筛阈值无效")
    dataset = {
        "dataset_id": "keyword-v2-prescreen", "label": "关键词候选 · v2",
        "screening_stage": "metadata_prescreen", "fine_screened": False,
        "threshold": threshold, "selected_count": len(selected), "source_count": len(scores),
        "scored_count": sum(row["prescreen_probability"] is not None for row in scores.values()),
        "unscored_count": sum(row["prescreen_probability"] is None for row in scores.values()),
        "protected_keep_count": sum(row["protected_keep"] for row in candidate_info.values()),
        "awesome_supplement_count": sum(row["retention_source"] == "awesome_readme_reference" for row in candidate_info.values()),
        "readme_evidence_count": len(readme_documents), "source_path": str(source_path),
        "classification_path": str(scores_path), "source_sha256": source_reader.sha256,
        "classification_sha256": score_reader.sha256, "imported_at": db.now(), "import_format_version": 2,
        "selection_rule": "preselected_membership_with_protection_and_awesome_supplements",
    }
    return _persist_dataset(source_path, scores_path, target, selected, dataset, candidate_info, readme_documents)


def activate_fine_catalog(kept_path: Path, scores_path: Path, db_path: Path) -> Dict[str, Any]:
    """Switch visible membership while preserving every repository and its history.

    Fine scores are selection evidence, never replacements for metadata probabilities.
    Keeping excluded rows lets older jobs, labels and evidence retain their identities.
    """
    kept_path, scores_path, target = (Path(path).expanduser().resolve()
                                    for path in (kept_path, scores_path, db_path))
    require(target.is_file(), "请先导入关键词仓库及README，再切换精筛目录")
    summary_path = scores_path.parent / "SUMMARY.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    require(summary.get("status") == "complete" and summary.get("pending_repositories") == 0,
            "精筛尚未完整完成，不能切换目录")
    threshold = summary.get("numeric_probability_threshold")
    require(type(threshold) in (int, float) and math.isfinite(threshold) and 0 <= threshold <= 1,
            "精筛阈值无效")
    scores, included = {}, set()
    score_reader = JsonlReader(scores_path)
    for _, row in score_reader.rows():
        identifier = checked_id(row.get("repository_id"), "精筛分数")
        score = row.get("noul_score")
        require(identifier not in scores, "精筛分数包含重复仓库")
        require(type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1,
                "精筛分数缺失或无效")
        decision = "include" if score >= threshold else "exclude"
        require(row.get("decision") == decision, "精筛分数与冻结阈值判断不一致")
        scores[identifier] = row
        if decision == "include":
            included.add(identifier)
    kept, kept_reader = {}, JsonlReader(kept_path)
    for _, row in kept_reader.rows():
        identifier = checked_id(row.get("id"), "精筛保留清单")
        require(identifier not in kept, "精筛保留清单重复")
        kept[identifier] = row.get("full_name")
    require(bool(included) and set(kept) == included, "精筛分数与保留清单不一致")
    require(len(scores) == summary.get("selected_repositories") == summary.get("decided_repositories")
            and len(included) == summary.get("include_repositories"), "精筛汇总计数不一致")

    db.initialize_database(target)
    with db.connection(target) as conn:
        conn.execute("BEGIN IMMEDIATE")
        require(conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] == 0,
                "分类任务正在运行，请先暂停任务后切换目录")
        current = conn.execute("SELECT value FROM app_meta WHERE key='dataset'").fetchone()
        require(current is not None, "工作库没有数据集身份")
        previous = json.loads(current["value"])
        require(previous.get("dataset_id") in {"keyword-v2-prescreen", "keyword-v2-fine-screen"},
                "只能切换关键词工作库，不覆盖其他数据集")
        stored = {row["id"]: row["full_name"] for row in conn.execute("SELECT id,full_name FROM repositories")}
        require(set(stored) == set(scores), "精筛范围与工作库仓库不一致")
        require(all(stored[identifier] == scores[identifier].get("full_name") for identifier in scores),
                "精筛记录与工作库仓库名称不一致")
        require(all(stored[identifier] == name for identifier, name in kept.items()),
                "精筛保留清单与工作库名称不一致")
        if previous.get("dataset_id") == "keyword-v2-prescreen":
            conn.execute("INSERT OR IGNORE INTO app_meta(key,value) VALUES('prescreen_dataset',?)",
                         (canonical_json(previous),))
        base_row = conn.execute("SELECT value FROM app_meta WHERE key='prescreen_dataset'").fetchone()
        base = json.loads(base_row["value"]) if base_row else previous
        conn.execute("UPDATE repositories SET catalog_visible=0")
        conn.executemany("UPDATE repositories SET catalog_visible=1 WHERE id=?", [(identifier,) for identifier in sorted(included)])
        readme_count = conn.execute("""SELECT COUNT(*) FROM repositories r WHERE catalog_visible=1
            AND EXISTS(SELECT 1 FROM evidence e WHERE e.repository_id=r.id AND e.kind='readme')""").fetchone()[0]
        require(readme_count == len(included), "精筛保留项缺少网站README，目录切换已回滚")
        origins = conn.execute("""SELECT SUM(protected_keep),SUM(retention_source='awesome_readme_reference')
            FROM repositories WHERE catalog_visible=1""").fetchone()
        dataset = {"dataset_id": "keyword-v2-fine-screen", "label": "Agent目录",
            "screening_stage": "metadata_readme_fine_screen", "fine_screened": True,
            "threshold": float(threshold), "selected_count": len(included), "source_count": len(scores),
            "discovery_source_count": base.get("source_count"), "stored_count": len(stored),
            "readme_evidence_count": readme_count, "protected_keep_count": origins[0] or 0,
            "awesome_supplement_count": origins[1] or 0,
            "source_path": str(kept_path), "classification_path": str(scores_path),
            "source_sha256": kept_reader.sha256, "classification_sha256": score_reader.sha256,
            "imported_at": base.get("imported_at"), "activated_at": db.now(),
            "fine_policy_version": summary.get("fine_policy_version"),
            "selection_rule": "complete_metadata_readme_fine_screen_membership"}
        conn.execute("UPDATE app_meta SET value=? WHERE key='dataset'", (canonical_json(dataset),))
    return {**dataset, "status": "activated", "hidden_repositories": len(scores) - len(included)}


def import_fine_dataset(kept_path: Path, scores_path: Path, db_path: Path) -> Dict[str, Any]:
    """Bootstrap a fresh workspace locally, then activate the completed fine selection."""
    target = Path(db_path).expanduser().resolve()
    db.initialize_database(target)
    with db.connection(target) as conn:
        has_dataset = conn.execute("SELECT 1 FROM app_meta WHERE key='dataset'").fetchone() is not None
    if not has_dataset:
        import_keyword_dataset(KEYWORD_SCORING_ROOT / "kept-augmented.jsonl",
                               KEYWORD_SCORING_ROOT / "scores.jsonl", target)
        from .evidence import sync_readme_collection
        synced = sync_readme_collection(KEYWORD_SCORING_ROOT.parent.parent / "readmes", target)
        require(synced["sync_failed"] == 0, "README同步未完成，精筛目录尚未激活")
    return activate_fine_catalog(kept_path, scores_path, target)
