"""证据适配层。采集器只需写入 Evidence，分类任务不依赖采集实现。"""

import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

from .db import connection, now


SUPPORTED_KINDS = {"metadata", "readme", "source_code"}


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def evidence_document(row) -> dict:
    content = row["content"]
    if row["kind"] == "metadata":
        content = json.loads(content)
    return {
        "id": row["id"], "repository_id": row["repository_id"], "kind": row["kind"],
        "version": row["version"], "source_key": row["source_key"], "source_url": row["source_url"],
        "content_hash": row["content_hash"], "captured_at": row["captured_at"],
        "content": content, "provenance": json.loads(row["provenance_json"]),
    }


def latest_evidence_ids(conn, repository_id: int, kinds: list[str]) -> list[str]:
    """同一来源取最新版本；源码同版本可有多个文件，不混合不同版本。"""
    identifiers = []
    for kind in kinds:
        if kind not in SUPPORTED_KINDS:
            raise ValueError("不支持的证据类型")
        latest = conn.execute(
            "SELECT version FROM evidence WHERE repository_id=? AND kind=? ORDER BY captured_at DESC,rowid DESC LIMIT 1",
            (repository_id, kind),
        ).fetchone()
        if latest is None:
            raise ValueError("仓库缺少 {} 证据".format(kind))
        rows = conn.execute(
            "SELECT id FROM evidence WHERE repository_id=? AND kind=? AND version=? ORDER BY id",
            (repository_id, kind, latest["version"]),
        ).fetchall()
        identifiers.extend(row["id"] for row in rows)
    return identifiers


def load_bundle(conn, repository_id: int, identifiers: list[str]) -> list[dict]:
    documents = []
    for identifier in identifiers:
        row = conn.execute("SELECT * FROM evidence WHERE id=? AND repository_id=?", (identifier, repository_id)).fetchone()
        if row is None:
            raise ValueError("任务引用的证据不存在")
        if hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != row["content_hash"]:
            raise ValueError("任务引用的证据校验失败")
        documents.append(evidence_document(row))
    if not documents:
        raise ValueError("任务没有可用证据")
    return documents


def add_evidence(conn, repository_id: int, body) -> dict:
    content = encode(body.content) if body.kind == "metadata" else body.content
    if len(content.encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("单份证据超过 4 MiB，请由采集器拆分文件")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        "SELECT * FROM evidence WHERE repository_id=? AND kind=? AND version=? AND source_key=?",
        (repository_id, body.kind, body.version, body.source_key),
    ).fetchone()
    if existing is not None:
        if existing["content_hash"] != digest:
            raise ValueError("同一版本和文件标识已有不同内容，请提供新版本")
        return evidence_document(existing)
    identifier = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO evidence(id,repository_id,kind,version,source_key,source_url,content,content_hash,provenance_json,captured_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (identifier, repository_id, body.kind, body.version, body.source_key, body.source_url, content, digest, encode(body.provenance), now()),
    )
    return evidence_document(conn.execute("SELECT * FROM evidence WHERE id=?", (identifier,)).fetchone())


def sync_readme_collection(collection_path: Path, db_path: Path) -> dict:
    """Append complete local README records to an existing website database.

    Collection journals are append-only and may still be growing. Only the last
    complete record for each repository is considered; an unfinished final line
    waits for the next sync. Neither collection files nor saved labels change.
    """
    from .schemas import EvidenceInput

    workspace = Path(__file__).resolve().parents[4]
    if str(workspace) not in sys.path:
        sys.path.insert(0, str(workspace))
    from system.workspace_paths import resolve_recorded_path

    source = resolve_recorded_path(collection_path)
    journal = source / "readmes.jsonl" if source.is_dir() else source
    target = Path(db_path).expanduser().resolve()
    if not journal.is_file() or not target.is_file():
        raise ValueError("README台账与已有网站数据库必须存在；同步不会初始化或重导网站")
    latest, tail_pending = {}, False
    with journal.open("rb") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError) as error:
                if not line.endswith(b"\n"):
                    tail_pending = True
                    break
                raise ValueError(f"README台账第{line_number}行无效") from error
            if not isinstance(record, dict) or type(record.get("repository_id")) is not int:
                raise ValueError(f"README台账第{line_number}行缺少仓库数字ID")
            latest[record["repository_id"]] = record

    summary = {"status": "complete", "collection_journal": str(journal),
               "database": str(target), "recorded_repositories": len(latest),
               "added": 0, "unchanged": 0, "outside_dataset": 0,
               "not_ready": 0, "sync_failed": 0, "failures": [],
               "journal_tail_pending": tail_pending}
    with connection(target) as conn:
        repositories = {row["id"]: row["url"] for row in conn.execute("SELECT id,url FROM repositories")}
        for identifier, record in latest.items():
            if identifier not in repositories:
                summary["outside_dataset"] += 1
                continue
            if record.get("status") != "ok" or record.get("content_complete") is not True:
                summary["not_ready"] += 1
                continue
            try:
                blob, readme_path = record.get("blob_sha"), record.get("readme_path")
                if not isinstance(blob, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", blob):
                    raise ValueError("README缺少有效Git blob版本")
                if not isinstance(readme_path, str) or not readme_path.strip():
                    raise ValueError("README缺少实际文件路径")
                content_file = record.get("content_file")
                if not isinstance(content_file, str) or not content_file:
                    raise ValueError("README缺少原文文件引用")
                content_path = resolve_recorded_path(content_file, base=journal.parent)
                if content_path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("README超过网站单份证据4 MiB上限；采集原文保留，未截断")
                # Decode exact bytes: read_text() would normalize CRLF and alter evidence.
                content = content_path.read_bytes().decode("utf-8")
                if len(content.encode("utf-8")) > 4 * 1024 * 1024:
                    raise ValueError("README超过网站单份证据4 MiB上限；采集原文保留，未截断")
                version = "git-blob:" + blob
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                existing = conn.execute(
                    "SELECT id,content_hash FROM evidence WHERE repository_id=? AND kind='readme' AND version=?",
                    (identifier, version),
                ).fetchall()
                if any(row["content_hash"] == digest for row in existing):
                    # Earlier supplemental imports used an empty source_key. Do not
                    # create a duplicate just because this journal knows its path.
                    summary["unchanged"] += 1
                    continue
                if existing:
                    raise ValueError("同一README blob版本已有不同内容；保留现场，不覆盖旧证据")
                document = EvidenceInput(
                    kind="readme", version=version, source_key=readme_path,
                    source_url=record.get("readme_html_url") or repositories[identifier] + "#readme",
                    content=content,
                    provenance={"collection_journal": str(journal), "source_path": str(content_path),
                                "fetched_at": record.get("fetched_at"), "readme_path": readme_path,
                                "blob_sha": blob, "collection_status": "ok", "content_complete": True},
                )
                added = add_evidence(conn, identifier, document)
                if record.get("fetched_at"):
                    conn.execute("UPDATE evidence SET captured_at=? WHERE id=?", (record["fetched_at"], added["id"]))
                summary["added"] += 1
            except (OSError, UnicodeDecodeError, ValueError) as error:
                summary["sync_failed"] += 1
                summary["failures"].append({"repository_id": identifier, "reason": str(error)})
        summary["readme_repositories_total"] = conn.execute(
            "SELECT COUNT(DISTINCT repository_id) FROM evidence WHERE kind='readme'").fetchone()[0]
    if summary["sync_failed"]:
        summary["status"] = "completed_with_errors"
    return summary
