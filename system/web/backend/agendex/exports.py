"""只读 CSV 导出：完整目录和指定策略版本的已保存分类结果。"""

import csv
import io
import json
import math

from . import annotations, scoring, services


def csv_bytes(columns: tuple[str, ...], rows) -> bytes:
    """UTF-8 BOM 方便表格软件读取中文；外部文本作为文本单元格处理。"""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in rows:
        cells = []
        for value in row:
            # CSV 引号本身不能阻止电子表格执行以这些字符开头的公式。
            if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))):
                value = "'" + value
            cells.append(value)
        writer.writerow(cells)
    return output.getvalue().encode("utf-8-sig")


def repositories_csv(conn) -> bytes:
    """导出完整可见目录，与网页的搜索、筛选和分页相互独立。"""
    repositories = [services.repository_document(row) for row in conn.execute(
        "SELECT * FROM repositories WHERE catalog_visible=1 ORDER BY stars DESC,id"
    )]
    # 每批限制绑定参数数量，避免目录增长后触及 SQLite 的变量数量限制。
    for offset in range(0, len(repositories), 500):
        services.attach_tags(conn, repositories[offset:offset + 500])
    policy_names = {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM policies")}
    columns = ("repository_id", "full_name", "url", "description", "stars", "language", "topics", "license",
               "default_branch", "created_at", "updated_at", "pushed_at", "archived", "tags")
    rows = []
    for repository in repositories:
        tags = [{"policy": policy_names[tag["policy_id"]], "label": tag["label"], "score": tag["score"], "source": tag["source"]}
                for tag in repository["tags"]]
        rows.append((repository["id"], repository["full_name"], repository["url"], repository["description"],
                     repository["stars"], repository["language"], json.dumps(repository["topics"], ensure_ascii=False),
                     repository["license"], repository["default_branch"], repository["created_at"],
                     repository["updated_at"], repository["pushed_at"], repository["archived"],
                     json.dumps(tags, ensure_ascii=False)))
    return csv_bytes(columns, rows)


def preview_policy(policy: dict, thresholds: str | None) -> dict:
    """严格验证完整阈值映射，防止导出与页面预览使用不同的判定条件。"""
    if thresholds is None:
        return policy
    try:
        values = json.loads(thresholds)
    except (ValueError, TypeError):
        raise services.ServiceError("阈值必须是有效的 JSON 对象") from None
    if not isinstance(values, dict) or set(values) != {tag["id"] for tag in policy["tags"]}:
        raise services.ServiceError("导出阈值必须包含该策略的全部标签，且不能包含其他标签")
    if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in values.values()):
        raise services.ServiceError("每个标签阈值必须在 [0,1] 内")
    return {**policy, "tags": [{**tag, "threshold": values[tag["id"]]} for tag in policy["tags"]]}


def policy_results_csv(conn, identifier: str, revision: int | None = None, q: str = "",
                       thresholds: str | None = None) -> tuple[bytes, int]:
    """导出一整个版本的匹配结果；阈值预览仍使用原评分和单选赢家。"""
    policy = preview_policy(services.get_policy(conn, identifier, revision=revision), thresholds)
    version = conn.execute("SELECT id FROM policy_versions WHERE policy_id=? AND revision=?",
                           (identifier, policy["revision"])).fetchone()
    results = annotations.result_rows(conn, policy, scoring.version_result_rows(conn, version["id"], policy=policy))
    query = q.strip().casefold()
    columns = ("repository_id", "full_name", "url", "stars", "language", "policy_name", "policy_revision",
               "mode", "tag_id", "tag_label", "score", "threshold", "applied", "threshold_source", "model", "scored_at",
               "source", "model_score", "manual_applied")
    rows = (
        (row["repository_id"], row["full_name"], row["url"], row["stars"], row["language"], policy["name"],
         policy["revision"], policy["mode"], tag["tag_id"], tag["label"], tag["score"], tag["threshold"],
         tag["applied"], "preview" if thresholds is not None else "saved", row["model"], row["scored_at"],
         tag.get("source", "model"), tag.get("model_score", tag["score"]), tag.get("manual_applied"))
        for row in results if not query or query in row["full_name"].casefold()
        for tag in row["results"]
    )
    return csv_bytes(columns, rows), policy["revision"]
