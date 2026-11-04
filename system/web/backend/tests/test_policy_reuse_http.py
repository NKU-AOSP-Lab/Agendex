"""从网页 API 验证结果复用；所有模型响应均由临时测试客户端提供。"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from agendex.db import connection
from agendex.schemas import PolicyInput
from agendex.services import save_policy
from test_support import FakeJev, create_fixture, isolated_app_factory, policy_body


create_app = isolated_app_factory()


class PolicyReuseHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "policy-history.sqlite3"
        create_fixture(self.path, count=2)
        self.fake = FakeJev()
        self.app = create_app(self.path, self.fake, start_workers=False)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def create_policy(self, **overrides):
        response = self.client.post("/api/policies", json=policy_body(**overrides))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def classify(self, policy_id, *, reuse=True):
        response = self.client.post("/api/jobs", json={
            "policy_id": policy_id, "scope": {"repository_ids": [1]}, "skip_completed": reuse,
        })
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json()
        while self.app.state.engine.run_once():
            pass
        return self.client.get("/api/jobs/" + job["id"]).json()

    def results(self, policy_id, revision=None):
        response = self.client.get("/api/policies/" + policy_id + "/results", params={} if revision is None else {"revision": revision})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def verdicts(results):
        return {row["label"]: row["applied"] for row in results["items"][0]["results"]}

    def test_independent_threshold_changes_replay_scores_without_changing_history_or_billing(self):
        policy = self.create_policy()
        original_job = self.classify(policy["id"])
        original = self.results(policy["id"], 1)
        with connection(self.path) as conn:
            original_bytes = tuple(conn.execute("SELECT request_json,response_json,results_json,cost FROM job_items WHERE job_id=?", (original_job["id"],)).fetchone())
        response = self.client.post("/api/policies/" + policy["id"] + "/thresholds", json={
            "base_revision": 1, "thresholds": {"coding": 1.0, "browser": 0.1},
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["revision"], 2)
        changed = self.results(policy["id"], 2)
        self.assertEqual(self.verdicts(original), {"代码开发": True, "网页浏览": False})
        self.assertEqual(self.verdicts(changed), {"代码开发": False, "网页浏览": True})
        self.assertEqual(self.verdicts(self.results(policy["id"], 1)), self.verdicts(original))
        self.assertEqual(len(self.fake.requests), 1)
        with connection(self.path) as conn:
            self.assertEqual(tuple(conn.execute("SELECT request_json,response_json,results_json,cost FROM job_items WHERE job_id=?", (original_job["id"],)).fetchone()), original_bytes)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        tags = self.client.get("/api/repositories/1").json()["tags"]
        self.assertEqual([row["label"] for row in tags], ["网页浏览"])

    def test_identical_policy_with_new_tag_ids_reuses_scores_and_preserves_original_source(self):
        original = self.create_policy()
        original_job = self.classify(original["id"])
        tags = [{**row, "id": "copied-" + row["id"], "threshold": .8} for row in original["tags"]]
        copied = self.create_policy(name="另一份相同规则", tags=tags)
        results = self.results(copied["id"])
        self.assertEqual(results["total"], 1)
        self.assertEqual({row["tag_id"] for row in results["items"][0]["results"]}, {"copied-coding", "copied-browser"})
        self.assertEqual(results["items"][0]["source_job_id"], original_job["id"])
        replay_job = self.classify(copied["id"])
        self.assertEqual(replay_job["cost"], 0)
        self.assertEqual(replay_job["skipped"], 1)
        self.assertEqual(len(self.fake.requests), 1)

    def test_restoring_history_reuses_that_versions_scores_not_a_newer_forced_run(self):
        policy = self.create_policy()
        first_job = self.classify(policy["id"])
        first = self.results(policy["id"], 1)
        response = self.client.post("/api/policies/" + policy["id"] + "/thresholds", json={
            "base_revision": 1, "thresholds": {"coding": .8, "browser": .8},
        })
        self.assertEqual(response.status_code, 200, response.text)

        def newer_scores(payload):
            return {"model": "fixture/served-model", "usage": {"input_tokens": 100, "output_tokens": 5, "cost": .001},
                    "answers": {key: {"type": "noul", "noul": .4 if "coding" in key else .9} for key in payload["questions"]}}

        self.fake.handler = newer_scores
        self.classify(policy["id"], reuse=False)
        self.assertEqual(self.verdicts(self.results(policy["id"], 2)), {"代码开发": False, "网页浏览": True})
        restored = self.client.post("/api/policies/" + policy["id"] + "/restore/1")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["revision"], 3)
        third = self.results(policy["id"], 3)
        self.assertEqual(self.verdicts(third), self.verdicts(first))
        self.assertEqual(third["items"][0]["source_job_id"], first_job["id"])
        self.assertEqual(self.verdicts(self.results(policy["id"], 1)), self.verdicts(first))
        self.assertEqual(len(self.fake.requests), 2)

    def test_stale_threshold_update_is_atomic_and_cannot_mutate_another_revision(self):
        policy = self.create_policy()
        endpoint = "/api/policies/" + policy["id"] + "/thresholds"
        first = self.client.post(endpoint, json={"base_revision": 1, "thresholds": {"coding": .7}})
        self.assertEqual(first.status_code, 200, first.text)
        rejected = self.client.post(endpoint, json={"base_revision": 1, "thresholds": {"browser": .1}})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        versions = self.client.get("/api/policies/" + policy["id"] + "/versions").json()["items"]
        self.assertEqual(len(versions), 2)
        current = self.client.get("/api/policies/" + policy["id"]).json()
        thresholds = {row["id"]: row["threshold"] for row in current["tags"]}
        self.assertEqual(thresholds, {"coding": .7, "browser": .8})
        self.assertEqual(self.fake.requests, [])

    def test_repeated_template_save_with_fresh_tag_ids_returns_existing_policy_and_results(self):
        original = self.create_policy()
        self.classify(original["id"])
        tags = [{**tag, "id": "fresh-" + tag["id"]} for tag in original["tags"]]
        repeated = self.create_policy(tags=tags)
        self.assertEqual(repeated["id"], original["id"])
        self.assertEqual(repeated["revision"], 1)
        self.assertEqual([tag["id"] for tag in repeated["tags"]], [tag["id"] for tag in original["tags"]])
        self.assertEqual(repeated["result_count"], 1)
        self.assertEqual(len(self.fake.requests), 1)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM policy_versions").fetchone()[0], 1)

    def test_noop_update_does_not_create_a_revision_or_replace_persistent_tag_ids(self):
        original = self.create_policy()
        body = policy_body(tags=[{**tag, "id": "fresh-" + tag["id"]} for tag in original["tags"]])
        response = self.client.put("/api/policies/" + original["id"], json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), original)
        self.assertEqual(len(self.client.get("/api/policies/" + original["id"] + "/versions").json()["items"]), 1)
        self.assertEqual(self.fake.requests, [])

    def test_legacy_source_defaults_do_not_duplicate_the_same_visible_policy(self):
        original = self.create_policy()
        repeated = self.create_policy(evidence_kinds=["metadata", "readme"])
        self.assertEqual(repeated["id"], original["id"])
        self.assertEqual(repeated["evidence_kinds"], ["metadata"])
        response = self.client.put("/api/policies/" + original["id"], json=policy_body(evidence_kinds=["metadata", "readme"]))
        self.assertEqual(response.json()["revision"], 1)
        self.assertEqual(self.fake.requests, [])

    def test_changed_content_and_archived_policies_are_not_overmerged(self):
        original = self.create_policy()
        variants = [
            {"name": "另一份用途"}, {"description": "Different shared instructions."},
            {"mode": "single_label"}, {"threshold": .7},
            {"tags": list(reversed(original["tags"]))},
            {"tags": [{**tag, "threshold": .7} for tag in original["tags"]]},
            {"tags": [{**tag, "description": tag["description"] + " Changed."} for tag in original["tags"]]},
        ]
        identifiers = {original["id"]}
        for variant in variants:
            with self.subTest(variant=variant):
                changed = self.create_policy(**variant)
                self.assertNotIn(changed["id"], identifiers)
                identifiers.add(changed["id"])
        self.assertEqual(self.client.delete("/api/policies/" + original["id"]).status_code, 200)
        recreated = self.create_policy()
        self.assertNotIn(recreated["id"], identifiers)
        self.assertEqual(self.fake.requests, [])

    def test_concurrent_direct_saves_are_atomic_without_an_outer_transaction(self):
        def save(index):
            tags = [{**tag, "id": f"fresh-{index}-{tag['id']}"} for tag in policy_body()["tags"]]
            with connection(self.path) as conn:
                return save_policy(conn, PolicyInput(**policy_body(tags=tags)))["id"]

        with ThreadPoolExecutor(max_workers=8) as workers:
            identifiers = list(workers.map(save, range(8)))
        self.assertEqual(len(set(identifiers)), 1)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM policy_versions").fetchone()[0], 1)
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
