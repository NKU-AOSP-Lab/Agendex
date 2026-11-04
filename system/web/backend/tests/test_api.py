import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agendex.db import connection
from test_support import FakeJev, create_fixture, isolated_app_factory, policy_body


create_app = isolated_app_factory()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        create_fixture(self.path)
        self.fake = FakeJev()
        self.app = create_app(self.path, self.fake, start_workers=False)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_empty_policies_and_real_repository_pagination(self):
        self.assertEqual(self.client.get("/api/policies").json(), {"items": []})
        result = self.client.get("/api/repositories", params={"min_stars": 30, "language": "Python", "page_size": 2}).json()
        self.assertEqual(result["total"], 3)
        self.assertEqual([item["id"] for item in result["items"]], [7, 5])
        detail = self.client.get("/api/repositories/1").json()
        self.assertEqual(detail["full_name"], "example/repo1")
        self.assertEqual(detail["evidence"][0]["kind"], "metadata")
        self.assertEqual(detail["classifications"], [])
        self.assertEqual(self.fake.requests, [])

    def test_task_source_validation_does_not_create_jobs_or_model_calls(self):
        policy = self.client.post("/api/policies", json=policy_body()).json()
        for kinds in [[], ["metadata", "metadata"], ["source_code"], ["metadata", "readme", "source_code"]]:
            response = self.client.post("/api/jobs", json={"policy_id": policy["id"], "evidence_kinds": kinds})
            self.assertEqual(response.status_code, 422, response.text)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.fake.requests, [])

    def test_search_treats_sql_wildcards_as_literal_text(self):
        for q in ["%", "_", "' OR 1=1 --"]:
            self.assertEqual(self.client.get("/api/repositories", params={"q": q}).json()["total"], 0)
        self.assertEqual(self.client.get("/api/repositories", params={"q": "repo1"}).json()["total"], 1)

    def test_policy_revisions_are_immutable_and_delete_is_soft(self):
        first = self.client.post("/api/policies", json=policy_body()).json()
        body = policy_body(name="新用途", tags=first["tags"])
        second = self.client.put("/api/policies/" + first["id"], json=body).json()
        self.assertEqual((first["revision"], second["revision"]), (1, 2))
        self.assertEqual(first["tags"][0]["id"], second["tags"][0]["id"])
        with connection(self.path) as conn:
            rows = conn.execute("SELECT document_json FROM policy_versions WHERE policy_id=? ORDER BY revision", (first["id"],)).fetchall()
            self.assertEqual([json.loads(row[0])["name"] for row in rows], ["用途", "新用途"])
        self.assertEqual(self.client.delete("/api/policies/" + first["id"]).status_code, 200)
        self.assertEqual(self.client.get("/api/policies").json(), {"items": []})
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM policy_versions").fetchone()[0], 2)
        self.assertEqual(self.fake.requests, [])

    def test_external_origin_and_host_cannot_create_paid_jobs_or_policies(self):
        for headers in [{"Origin": "https://evil.example"}, {"Host": "evil.example"}, {"Origin": "null"}]:
            response = self.client.post("/api/policies", json=policy_body(), headers=headers)
            self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/policies", json=policy_body(), headers={"Origin": "http://localhost:5173"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(self.client.get("/api/policies").json()["items"]), 1)

    def test_custom_local_ports_follow_server_configuration(self):
        with patch.dict(os.environ, {"AGENDEX_LOCAL_PORTS": "8123,6123"}):
            allowed = self.client.post("/api/policies", json=policy_body(), headers={"Origin": "http://127.0.0.1:6123"})
            rejected = self.client.post("/api/policies", json=policy_body(), headers={"Origin": "http://127.0.0.1:5173"})
            self.assertEqual(allowed.status_code, 201)
            self.assertEqual(rejected.status_code, 403)

    def test_future_evidence_upload_is_versioned_and_idempotent(self):
        body = {"kind": "source_code", "version": "commit-fixture", "source_url": "https://github.com/example/repo1", "content": "def loop(): pass", "provenance": {"path": "agent.py"}}
        first = self.client.post("/api/repositories/1/evidence", json=body).json()
        again = self.client.post("/api/repositories/1/evidence", json=body).json()
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["source_key"], "agent.py")
        conflict = self.client.post("/api/repositories/1/evidence", json={**body, "content": "def loop(): return True"})
        self.assertEqual(conflict.status_code, 400)
        changed = self.client.post("/api/repositories/1/evidence", json={**body, "version": "commit-next", "content": "def loop(): return True"}).json()
        self.assertNotEqual(changed["content_hash"], first["content_hash"])
        self.assertEqual(len(self.client.get("/api/repositories/1").json()["evidence"]), 3)

    def test_equal_content_in_distinct_source_files_is_preserved(self):
        body = {"kind": "source_code", "version": "same-commit", "content": "pass", "provenance": {}}
        first = self.client.post("/api/repositories/1/evidence", json={**body, "source_key": "a.py"}).json()
        second = self.client.post("/api/repositories/1/evidence", json={**body, "source_key": "b.py"}).json()
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual((first["source_key"], second["source_key"]), ("a.py", "b.py"))
        self.assertEqual(self.client.post("/api/repositories/1/evidence", json=body).status_code, 422)
        self.assertEqual(self.client.post("/api/repositories/1/evidence", json={**body, "source_key": " ", "provenance": {}}).status_code, 422)

    def test_validation_and_missing_records_have_readable_errors(self):
        self.assertEqual(self.client.get("/api/repositories?page_size=101").status_code, 422)
        self.assertEqual(self.client.get("/api/repositories/999").status_code, 404)
        self.assertEqual(self.client.get("/api/repositories?sort=arbitrary_sql").status_code, 400)
        for body in [policy_body(name=" "), policy_body(tags=[]), policy_body(threshold=-.1), policy_body(tags=[policy_body()["tags"][0]]*2)]:
            self.assertEqual(self.client.post("/api/policies", json=body).status_code, 422)

    def test_browser_workflow_creates_a_job_and_exposes_result_tags(self):
        policy = self.client.post("/api/policies", json=policy_body()).json()
        response = self.client.post("/api/jobs", json={"policy_id": policy["id"], "scope": {"repository_ids": [1]}, "skip_completed": True})
        self.assertEqual(response.status_code, 201)
        job = response.json()
        self.app.state.engine.run_once()
        detail = self.client.get("/api/jobs/" + job["id"]).json()
        self.assertEqual(detail["completed"], 1)
        filtered = self.client.get("/api/repositories", params={"policy_id": policy["id"], "tag_id": "coding"}).json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(self.client.get("/api/stats").json()["tagged_repositories"], 1)

    def test_multiple_tag_filters_match_any_effective_tag_and_freeze_the_same_job_scope(self):
        policy = self.client.post("/api/policies", json=policy_body()).json()
        other = self.client.post("/api/policies", json=policy_body(name="其他策略")).json()
        for repository_id, selected, tags in [
            (1, policy, ["coding"]), (2, policy, ["browser"]),
            (3, policy, ["coding", "browser"]), (4, other, ["coding"]),
        ]:
            response = self.client.put(f"/api/repositories/{repository_id}/annotations", json={
                "policy_id": selected["id"], "base_revision": selected["revision"],
                "overrides": [{"tag_id": tag, "applied": True} for tag in tags],
            })
            self.assertEqual(response.status_code, 200, response.text)
        filters = {"policy_id": policy["id"], "tag_ids": "coding,browser"}
        result = self.client.get("/api/repositories", params=filters).json()
        self.assertEqual(result["total"], 3)
        self.assertEqual([row["id"] for row in result["items"]], [3, 2, 1])
        self.assertEqual({tag["source"] for row in result["items"] for tag in row["tags"]}, {"manual"})
        narrowed = self.client.get("/api/repositories", params={**filters, "language": "Python", "min_stars": 20}).json()
        self.assertEqual([row["id"] for row in narrowed["items"]], [3])
        legacy = self.client.get("/api/repositories", params={"policy_id": policy["id"], "tag_id": "coding"}).json()
        precedence = self.client.get("/api/repositories", params={**filters, "tag_id": "browser", "tag_ids": "coding"}).json()
        self.assertEqual([row["id"] for row in legacy["items"]], [3, 1])
        self.assertEqual([row["id"] for row in precedence["items"]], [3, 1])
        scope = {"filters": {"policy_id": policy["id"], "tag_ids": ["coding", "browser"]}}
        job = self.client.post("/api/jobs", json={"policy_id": policy["id"], "scope": scope})
        self.assertEqual(job.status_code, 201, job.text)
        self.assertEqual(job.json()["total"], 3)
        with connection(self.path) as conn:
            frozen = json.loads(conn.execute("SELECT scope_json FROM jobs WHERE id=?", (job.json()["id"],)).fetchone()[0])
            self.assertEqual(frozen["filters"]["tag_ids"], ["coding", "browser"])
            ids = [row[0] for row in conn.execute("SELECT repository_id FROM job_items WHERE job_id=? ORDER BY repository_id", (job.json()["id"],))]
            self.assertEqual(ids, [1, 2, 3])
        self.assertEqual(self.fake.requests, [])

    def test_multiple_tag_filters_reject_invalid_or_unbounded_selections(self):
        policy = self.client.post("/api/policies", json=policy_body()).json()
        invalid = [["coding", "coding"], ["coding", ""], ["x" * 81], [f"t{i}" for i in range(41)], "coding"]
        for tags in invalid:
            response = self.client.post("/api/jobs", json={"policy_id": policy["id"], "scope": {"filters": {"tag_ids": tags}}})
            self.assertEqual(response.status_code, 422, response.text)
            if isinstance(tags, list):
                response = self.client.get("/api/repositories", params={"tag_ids": ",".join(tags)})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/api/repositories", params={"tag_ids": ""}).json()["total"], 8)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.fake.requests, [])

    def test_model_selection_is_exposed_and_routes_the_frozen_http_job(self):
        glm = FakeJev()
        glm.model = "glm-5.3"
        app = create_app(self.path, self.fake, start_workers=False, glm_client=glm)
        with TestClient(app) as client:
            health = client.get("/api/health").json()
            self.assertEqual([item["id"] for item in health["models"]], ["typesafe/jev-1.14", "glm-5.3"])
            self.assertTrue(health["glm_configured"])
            policy = client.post("/api/policies", json=policy_body()).json()
            job = client.post("/api/jobs", json={"policy_id": policy["id"], "model": "glm-5.3", "scope": {"repository_ids": [1]}}).json()
            self.assertEqual(job["model"], "glm-5.3")
            app.state.engine.run_once()
            self.assertEqual(client.get("/api/jobs/" + job["id"]).json()["completed"], 1)
            self.assertEqual(len(glm.requests), 1)
            self.assertEqual(self.fake.requests, [])
            self.assertEqual(client.post("/api/jobs", json={"policy_id": policy["id"], "model": "arbitrary-provider"}).status_code, 422)

    def test_natural_language_generation_returns_a_draft_without_saving_or_classifying(self):
        from agendex.policy_generation import POLICY_GENERATION_EXAMPLE
        glm = FakeJev()
        glm.model = glm.provider_model = "glm-5.3"
        chats = []
        def chat(payload):
            chats.append(payload)
            return {"model": "glm-5.3", "usage": {"prompt_tokens": 150, "completion_tokens": 50},
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(POLICY_GENERATION_EXAMPLE["assistant"])}}]}
        glm.chat = chat
        app = create_app(self.path, self.fake, start_workers=False, glm_client=glm)
        with TestClient(app) as client:
            result = client.post("/api/policies/generate", json={"messages": [{"role": "user", "content": "按代码、渗透测试和网页操作分类"}], "draft": {}})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["policy"]["evidence_kinds"], ["metadata", "readme"])
            self.assertIsNone(result.json()["cost"])
            self.assertEqual(client.get("/api/policies").json(), {"items": []})
            self.assertEqual(client.get("/api/jobs").json(), {"items": []})
        with connection(self.path) as conn:
            record = conn.execute("SELECT * FROM policy_generations").fetchone()
            self.assertEqual(record["status"], "completed")
            self.assertIsNone(record["cost"])
            self.assertIsNotNone(record["response_json"])
        self.assertEqual(len(chats), 1)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(glm.requests, [])


if __name__ == "__main__":
    unittest.main()
