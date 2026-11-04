"""已有工作数据库的迁移必须保留证据身份，遇冲突完整回滚。"""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agendex.db import SCHEMA_VERSION, connection, initialize_database


OLD_EVIDENCE = """
CREATE TABLE evidence (
    id TEXT PRIMARY KEY, repository_id INTEGER NOT NULL REFERENCES repositories(id),
    kind TEXT NOT NULL, version TEXT NOT NULL, source_url TEXT,
    content TEXT NOT NULL, content_hash TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}', captured_at TEXT NOT NULL,
    UNIQUE(repository_id,kind,version,content_hash)
);
PRAGMA user_version = 1;
"""


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "working.sqlite3"
        initialize_database(self.path)
        with connection(self.path) as conn:
            conn.execute("INSERT INTO repositories(id,full_name,url,agent_probability,imported_at) VALUES(1,'fixture/repo','https://github.com/fixture/repo',0.9,'2026-10-31')")
            conn.executescript("DROP TABLE evidence;" + OLD_EVIDENCE)

    def tearDown(self):
        self.temp.cleanup()

    def add_old(self, identifier, kind="metadata", digest="hash", provenance=None):
        with connection(self.path) as conn:
            conn.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?)", (
                identifier, 1, kind, "frozen-version", "https://github.com/fixture/repo", "unchanged-content",
                digest, json.dumps(provenance or {}), "2026-10-31T00:00:00Z"))

    def test_migration_preserves_ids_content_and_source_paths_and_is_idempotent(self):
        self.add_old("metadata-id")
        self.add_old("source-id", kind="source_code", provenance={"path": "src/agent.py"})
        initialize_database(self.path)
        initialize_database(self.path)
        with connection(self.path) as conn:
            rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM evidence")}
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows["metadata-id"]["source_key"], "")
            self.assertEqual(rows["source-id"]["source_key"], "src/agent.py")
            self.assertEqual(rows["source-id"]["content"], "unchanged-content")
            self.assertEqual(rows["source-id"]["content_hash"], "hash")
            self.assertEqual(rows["source-id"]["version"], "frozen-version")

    def test_conflicting_old_file_identity_rolls_back_without_losing_evidence(self):
        for identifier, digest in [("first", "first-hash"), ("second", "second-hash")]:
            self.add_old(identifier, kind="source_code", digest=digest, provenance={"path": "agent.py"})
        with self.assertRaises(sqlite3.IntegrityError):
            initialize_database(self.path)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 2)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='evidence_v2'").fetchone())

    def test_future_schema_is_rejected_without_downgrading(self):
        with connection(self.path) as conn:
            conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(ValueError):
            initialize_database(self.path)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 99)


if __name__ == "__main__":
    unittest.main()
