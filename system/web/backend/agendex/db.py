"""SQLite 数据底座：仓库、版本化证据、策略版本与任务分别保存。

原始 Search/Fetch 文件保持只读。数据库是网站的工作副本，位置可通过
AGENDEX_DB_PATH 设置；使用短事务和 WAL，允许网页查询与后台任务并行。
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from typing import Iterator, Optional


DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "agendex-keyword.sqlite3"
SCHEMA_VERSION = 7


def database_path() -> Path:
    return Path(os.environ.get("AGENDEX_DB_PATH", str(DEFAULT_DB_PATH))).expanduser().resolve()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connection(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    target = Path(path) if path is not None else database_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    url TEXT NOT NULL,
    description TEXT,
    stars INTEGER NOT NULL DEFAULT 0,
    language TEXT,
    topics_json TEXT NOT NULL DEFAULT '[]',
    license TEXT,
    default_branch TEXT,
    created_at TEXT,
    updated_at TEXT,
    pushed_at TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    agent_probability REAL,
    retention_source TEXT NOT NULL DEFAULT 'legacy_topic',
    protected_keep INTEGER NOT NULL DEFAULT 0,
    original_prescreen_decision TEXT,
    supplement_json TEXT NOT NULL DEFAULT '{}',
    catalog_visible INTEGER NOT NULL DEFAULT 1,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS repositories_stars ON repositories(stars DESC, id);
CREATE INDEX IF NOT EXISTS repositories_language ON repositories(language, stars DESC);
CREATE INDEX IF NOT EXISTS repositories_updated ON repositories(updated_at DESC);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    kind TEXT NOT NULL,
    version TEXT NOT NULL,
    source_key TEXT NOT NULL DEFAULT '',
    source_url TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    captured_at TEXT NOT NULL,
    UNIQUE(repository_id, kind, version, source_key)
);
CREATE INDEX IF NOT EXISTS evidence_repository ON evidence(repository_id, kind, captured_at DESC);

CREATE TABLE IF NOT EXISTS policies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_versions (
    id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES policies(id),
    revision INTEGER NOT NULL,
    document_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(policy_id, revision)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES policies(id),
    policy_version_id TEXT NOT NULL REFERENCES policy_versions(id),
    status TEXT NOT NULL,
    total INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    unknown_cost_count INTEGER NOT NULL DEFAULT 0,
    scope_json TEXT NOT NULL,
    model TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS job_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    status TEXT NOT NULL DEFAULT 'queued',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    request_json TEXT,
    response_json TEXT,
    results_json TEXT,
    model TEXT,
    cost REAL,
    usage_json TEXT,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    source_job_item_id INTEGER REFERENCES job_items(id),
    UNIQUE(job_id, repository_id)
);
CREATE INDEX IF NOT EXISTS job_items_queue ON job_items(job_id, status, id);
CREATE INDEX IF NOT EXISTS job_items_repository ON job_items(repository_id, id DESC);

CREATE TABLE IF NOT EXISTS tag_assignments (
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    policy_id TEXT NOT NULL REFERENCES policies(id),
    policy_revision INTEGER NOT NULL,
    tag_id TEXT NOT NULL,
    label TEXT NOT NULL,
    score REAL NOT NULL,
    job_item_id INTEGER NOT NULL REFERENCES job_items(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(repository_id, policy_id, policy_revision, tag_id)
);
CREATE INDEX IF NOT EXISTS tag_assignments_filter ON tag_assignments(policy_id, tag_id, repository_id);

-- 分数只引用一次真实模型请求；阈值版本通过 policy_results 复用来源。
CREATE TABLE IF NOT EXISTS score_cache (
    source_job_item_id INTEGER PRIMARY KEY REFERENCES job_items(id),
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    request_model TEXT NOT NULL,
    served_model TEXT NOT NULL,
    semantic_key TEXT NOT NULL,
    state_key TEXT NOT NULL,
    evidence_key TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    winner_key TEXT,
    mode TEXT NOT NULL,
    scored_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS score_cache_lookup
ON score_cache(repository_id,request_model,semantic_key,state_key,evidence_key);
CREATE TABLE IF NOT EXISTS policy_results (
    policy_version_id TEXT NOT NULL REFERENCES policy_versions(id),
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    source_job_item_id INTEGER NOT NULL REFERENCES score_cache(source_job_item_id),
    bound_at TEXT NOT NULL,
    PRIMARY KEY(policy_version_id,repository_id)
);
CREATE INDEX IF NOT EXISTS policy_results_source ON policy_results(source_job_item_id);

-- Natural-language strategy drafting is separate from repository classification.
CREATE TABLE IF NOT EXISTS policy_generations (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT,
    usage_json TEXT,
    cost REAL,
    error TEXT
);

-- Human decisions are an independent overlay; model responses stay immutable.
CREATE TABLE IF NOT EXISTS manual_annotations (
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    policy_id TEXT NOT NULL REFERENCES policies(id),
    tag_id TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    policy_mode TEXT NOT NULL,
    policy_description TEXT NOT NULL,
    tag_label TEXT NOT NULL,
    tag_description TEXT NOT NULL,
    applied INTEGER NOT NULL CHECK(applied IN (0,1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(repository_id,policy_id,tag_id,rule_key)
);
CREATE INDEX IF NOT EXISTS manual_annotations_repository ON manual_annotations(repository_id,policy_id);
CREATE VIEW IF NOT EXISTS current_policy_tags AS
SELECT p.id policy_id,p.current_revision policy_revision,
       json_extract(v.document_json,'$.mode') policy_mode,
       json_extract(v.document_json,'$.description') policy_description,
       json_extract(t.value,'$.id') tag_id,json_extract(t.value,'$.label') tag_label,
       json_extract(t.value,'$.description') tag_description
FROM policies p JOIN policy_versions v ON v.policy_id=p.id AND v.revision=p.current_revision,
     json_each(v.document_json,'$.tags') t
WHERE p.archived=0;
CREATE VIEW IF NOT EXISTS active_manual_annotations AS
SELECT m.*,t.policy_revision FROM manual_annotations m JOIN current_policy_tags t
ON t.policy_id=m.policy_id AND t.tag_id=m.tag_id AND t.policy_mode=m.policy_mode
AND t.policy_description=m.policy_description AND t.tag_label=m.tag_label
AND t.tag_description=m.tag_description;
CREATE VIEW IF NOT EXISTS effective_tag_assignments AS
SELECT a.repository_id,a.policy_id,a.policy_revision,a.tag_id,a.label,a.score,'model' source
FROM tag_assignments a JOIN policies p
ON p.id=a.policy_id AND p.current_revision=a.policy_revision AND p.archived=0
WHERE NOT EXISTS(SELECT 1 FROM active_manual_annotations m
    WHERE m.repository_id=a.repository_id AND m.policy_id=a.policy_id
    AND (m.tag_id=a.tag_id OR (m.policy_mode='single_label' AND m.applied=1)))
UNION ALL
SELECT m.repository_id,m.policy_id,m.policy_revision,m.tag_id,m.tag_label,1.0,'manual'
FROM active_manual_annotations m WHERE m.applied=1;
"""


def initialize_database(path: Optional[Path] = None) -> None:
    with connection(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError("数据库版本高于当前程序，请使用兼容版本，避免降级工作数据")
        conn.execute("PRAGMA journal_mode = WAL")
        if version == 1:
            _migrate_evidence_identity(conn)
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(job_items)")}
        if "source_job_item_id" not in columns:
            conn.execute("ALTER TABLE job_items ADD COLUMN source_job_item_id INTEGER REFERENCES job_items(id)")
        # Candidate provenance is independent of the immutable evidence and tag history.
        repository_columns = {row["name"] for row in conn.execute("PRAGMA table_info(repositories)")}
        for name, definition in (
            ("retention_source", "TEXT NOT NULL DEFAULT 'legacy_topic'"),
            ("protected_keep", "INTEGER NOT NULL DEFAULT 0"),
            ("original_prescreen_decision", "TEXT"),
            ("supplement_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("catalog_visible", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if name not in repository_columns:
                conn.execute("ALTER TABLE repositories ADD COLUMN " + name + " " + definition)
        conn.execute("PRAGMA user_version = {}".format(SCHEMA_VERSION))


def _migrate_evidence_identity(conn: sqlite3.Connection) -> None:
    """保留证据 ID，为同版本下不同路径的源码建立独立身份。

    v1 的元数据记录没有 source_key，迁移为空字符串。历史源码若带有
    provenance.path 则沿用该路径；无法唯一迁移的历史记录会整批回滚，
    不能悄悄覆盖内容。任务中引用的证据 ID 始终不变。
    """
    import json

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("""
        CREATE TABLE evidence_v2 (
            id TEXT PRIMARY KEY,
            repository_id INTEGER NOT NULL REFERENCES repositories(id),
            kind TEXT NOT NULL,
            version TEXT NOT NULL,
            source_key TEXT NOT NULL DEFAULT '',
            source_url TEXT,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            provenance_json TEXT NOT NULL DEFAULT '{}',
            captured_at TEXT NOT NULL,
            UNIQUE(repository_id, kind, version, source_key)
        )
    """)
    rows = conn.execute("SELECT * FROM evidence").fetchall()
    for row in rows:
        provenance = json.loads(row["provenance_json"])
        source_key = str(provenance.get("path") or row["source_url"] or row["id"]) if row["kind"] == "source_code" else ""
        conn.execute("""
            INSERT INTO evidence_v2
            (id,repository_id,kind,version,source_key,source_url,content,content_hash,provenance_json,captured_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (row["id"], row["repository_id"], row["kind"], row["version"], source_key,
              row["source_url"], row["content"], row["content_hash"], row["provenance_json"], row["captured_at"]))
    conn.execute("DROP TABLE evidence")
    conn.execute("ALTER TABLE evidence_v2 RENAME TO evidence")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
