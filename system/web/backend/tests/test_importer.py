"""导入器的离线证据/筛选契约测试；只使用临时文件和临时 SQLite。"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from agendex import db
from agendex.importer import ImportValidationError, import_dataset, import_keyword_dataset


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "repositories.jsonl"
        self.classifications = self.root / "classifications.jsonl"
        self.database = self.root / "data" / "test.sqlite3"
        self.repos = [
            {"id": i, "full_name": "example/repo{}".format(i), "html_url": "https://github.com/example/repo{}".format(i),
             "description": None if i == 2 else "Agent {}".format(i), "stargazers_count": i * 7,
             "language": "Python", "topics": ["ai-agents", "测试"], "license": {"spdx_id": "MIT"},
             "default_branch": "main", "created_at": "2025-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
             "pushed_at": "2025-12-31T00:00:00Z", "archived": i == 2}
            for i in range(1, 5)
        ]
        self.classes = [
            {"repository_id": i, "full_name": "example/repo{}".format(i), "agent_probability": score,
             "is_agent": False, "threshold": 0.99}
            for i, score in enumerate([0.69, 0.8, 0.68, None], 1)
        ]
        self.save()

    def save(self):
        write_jsonl(self.source, self.repos)
        write_jsonl(self.classifications, self.classes)

    def run_import(self, threshold=0.69):
        return import_dataset(self.source, self.classifications, self.database, threshold)

    def test_inclusive_threshold_uses_saved_score_and_keeps_missing_description(self):
        result = self.run_import()
        self.assertEqual(result["selected_count"], 2)
        self.assertEqual(result["source_count"], 4)
        self.assertEqual(result["scored_count"], 3)
        self.assertEqual(result["unscored_count"], 1)
        with db.connection(self.database) as conn:
            rows = conn.execute("SELECT * FROM repositories ORDER BY id").fetchall()
            self.assertEqual([r["id"] for r in rows], [1, 2])
            self.assertEqual(rows[0]["agent_probability"], 0.69)
            self.assertIsNone(rows[1]["description"])
            self.assertEqual(rows[1]["archived"], 1)
            self.assertEqual(rows[1]["stars"], 14)
            self.assertEqual(rows[1]["license"], "MIT")
            self.assertEqual(rows[1]["default_branch"], "main")
            self.assertEqual(rows[1]["pushed_at"], "2025-12-31T00:00:00Z")

    def test_evidence_content_and_provenance_bind_exact_source_without_claiming_commit(self):
        original = self.source.read_bytes()
        result = self.run_import()
        with db.connection(self.database) as conn:
            evidence = conn.execute("SELECT * FROM evidence WHERE repository_id=1").fetchone()
            content = json.loads(evidence["content"])
            provenance = json.loads(evidence["provenance_json"])
            self.assertEqual(content["topics"], ["ai-agents", "测试"])
            self.assertEqual(content["stars"], 7)
            self.assertNotIn("agent_probability", content)
            self.assertEqual(evidence["content_hash"], hashlib.sha256(evidence["content"].encode()).hexdigest())
            self.assertEqual(provenance["source_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(provenance["source_line"], 1)
            self.assertEqual(provenance["version_kind"], "metadata_snapshot_not_commit")
            self.assertIn("no per-repository retrieval timestamp", provenance["captured_at_basis"])
            self.assertEqual(evidence["captured_at"], result["imported_at"])
        self.assertEqual(self.source.read_bytes(), original)

    def test_same_snapshot_is_idempotent_and_preserves_existing_evidence_and_policy(self):
        first = self.run_import()
        with db.connection(self.database) as conn:
            conn.execute("INSERT INTO policies VALUES ('p','User policy',1,0,'before','before')")
            conn.execute("INSERT INTO policy_versions VALUES ('pv','p',1,'{}','hash','before')")
            conn.execute("""INSERT INTO jobs (id,policy_id,policy_version_id,status,total,scope_json,model,created_at,updated_at)
                            VALUES ('j','p','pv','completed',1,'{}','test','before','before')""")
            conn.execute("INSERT INTO job_items(job_id,repository_id,status) VALUES ('j',1,'completed')")
            conn.execute("""INSERT INTO evidence(id,repository_id,kind,version,source_url,content,content_hash,provenance_json,captured_at)
                            VALUES ('readme',1,'readme','blob:test','https://example.com','content','hash','{}','before')""")
        second = self.run_import()
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(first["imported_at"], second["imported_at"])
        with db.connection(self.database) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_items").fetchone()[0], 1)

    def test_different_snapshot_or_threshold_is_rejected_without_mutating_existing_rows(self):
        self.run_import()
        with self.assertRaises(ImportValidationError):
            self.run_import(threshold=0.8)
        self.repos[0]["description"] = "Changed metadata"
        self.save()
        with self.assertRaises(ImportValidationError):
            self.run_import()
        with db.connection(self.database) as conn:
            self.assertEqual(conn.execute("SELECT description FROM repositories WHERE id=1").fetchone()[0], "Agent 1")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 2)

    def test_stored_source_hash_binding_is_verified_before_database_creation(self):
        (self.root / "config.json").write_text(json.dumps({"source_sha256": "0" * 64}))
        with self.assertRaises(ImportValidationError):
            self.run_import()
        self.assertFalse(self.database.exists())
        (self.root / "config.json").write_text(json.dumps({"source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest()}))
        self.assertEqual(self.run_import()["status"], "imported")

    def test_duplicate_and_unmatched_identities_are_rejected_before_database_creation(self):
        for broken_repos, broken_classes in [
            (self.repos + [self.repos[0]], self.classes),
            (self.repos, self.classes + [self.classes[0]]),
            (self.repos[:-1], self.classes),
            (self.repos, self.classes[:-1]),
        ]:
            with self.subTest(lengths=(len(broken_repos), len(broken_classes))):
                write_jsonl(self.source, broken_repos)
                write_jsonl(self.classifications, broken_classes)
                with self.assertRaises(ImportValidationError):
                    self.run_import()
                self.assertFalse(self.database.exists())

    def test_invalid_scores_are_not_silently_treated_as_negative(self):
        for value in [True, -0.01, 1.01, "0.8", float("nan"), float("inf")]:
            with self.subTest(score=value):
                self.classes[0]["agent_probability"] = value
                self.save()
                with self.assertRaises(ImportValidationError):
                    self.run_import()
                self.assertFalse(self.database.exists())

    def test_database_cannot_overwrite_source_file(self):
        original = self.source.read_bytes()
        with self.assertRaises(ImportValidationError):
            import_dataset(self.source, self.classifications, self.source)
        self.assertEqual(self.source.read_bytes(), original)

    def test_keyword_membership_keeps_protection_and_supplements_without_fabricated_scores(self):
        """Membership, rather than another cutoff, governs the new website dataset."""
        scores = [
            {"repository_id": i, "full_name": self.repos[i - 1]["full_name"],
             "prescreen_probability": value, "protected_keep": i == 2,
             "decision": "exclude" if i == 3 else "keep", "threshold": .75}
            for i, value in [(1, .8), (2, None), (3, .68)]
        ]
        candidates = []
        for repo in self.repos:
            row = {**repo, "retention_source": "metadata_prescreen"}
            if repo["id"] in (3, 4):
                row.update(retention_source="awesome_readme_reference",
                           original_prescreen_decision="exclude" if repo["id"] == 3 else "outside_search",
                           prescreen_probability=.68 if repo["id"] == 3 else None,
                           reference_reviews=[{"repository_id": repo["id"], "include": True,
                                               "reason": "Document-supported path", "sources": []}])
            candidates.append(row)
        write_jsonl(self.source, candidates)
        write_jsonl(self.classifications, scores)
        result = import_keyword_dataset(self.source, self.classifications, self.database)
        self.assertEqual((result["selected_count"], result["protected_keep_count"], result["awesome_supplement_count"]), (4, 1, 2))
        with db.connection(self.database) as conn:
            rows = conn.execute("SELECT * FROM repositories ORDER BY id").fetchall()
            self.assertEqual([row["agent_probability"] for row in rows], [.8, None, .68, None])
            self.assertEqual(rows[1]["protected_keep"], 1)
            self.assertEqual(rows[2]["original_prescreen_decision"], "exclude")
        self.assertEqual(import_keyword_dataset(self.source, self.classifications, self.database)["status"], "unchanged")


if __name__ == "__main__":
    unittest.main()
