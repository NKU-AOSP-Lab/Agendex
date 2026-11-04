"""Human labels remain editable without changing model evidence or spending API calls."""

import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from agendex.db import connection
from test_support import FakeJev, create_fixture, isolated_app_factory, policy_body


create_app = isolated_app_factory()


class ManualAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "annotations.sqlite3"
        create_fixture(self.path, count=2)
        self.fake = FakeJev()
        self.app = create_app(self.path, self.fake, start_workers=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.policy = self.client.post("/api/policies", json=policy_body()).json()
        self.endpoint = "/api/repositories/1/annotations"

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def save(self, overrides, revision=None):
        return self.client.put(self.endpoint, json={"policy_id": self.policy["id"],
                               "base_revision": revision or self.policy["revision"], "overrides": overrides})

    def result(self):
        response = self.client.get("/api/policies/" + self.policy["id"] + "/results")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_unscored_annotation_is_visible_filterable_and_exported_with_provenance(self):
        saved = self.save([{"tag_id": "browser", "applied": True}])
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["tags"], [{"policy_id": self.policy["id"], "tag_id": "browser", "label": "网页浏览", "score": 1.0, "source": "manual"}])
        self.assertEqual(self.client.get("/api/repositories", params={"tag_id": "browser"}).json()["total"], 1)
        self.assertEqual(self.client.get("/api/stats").json()["tagged_repositories"], 1)
        state = self.client.get(self.endpoint).json()["items"][0]
        tags = {tag["tag_id"]: tag for tag in state["tags"]}
        self.assertIsNone(tags["coding"]["score"])
        self.assertIsNone(tags["browser"]["model_score"])
        self.assertTrue(tags["browser"]["manual_applied"])
        result = self.result()
        self.assertIsNone(result["items"][0]["model"])
        self.assertEqual(result["policy"]["result_count"], 1)
        self.assertEqual(result["tag_stats"][1]["manual_applied_count"], 1)
        self.assertEqual(result["tag_stats"][1]["distribution"], [])
        directory = list(csv.DictReader(io.StringIO(self.client.get("/api/repositories/export.csv").content.decode("utf-8-sig"))))
        row = next(row for row in directory if row["repository_id"] == "1")
        self.assertEqual(json.loads(row["tags"])[0]["source"], "manual")
        exported = self.client.get("/api/policies/" + self.policy["id"] + "/results/export.csv")
        rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        browser = next(row for row in rows if row["tag_id"] == "browser")
        self.assertEqual((browser["score"], browser["source"], browser["model_score"]), ("1.0", "manual", ""))
        self.assertEqual(self.fake.requests, [])

    def test_thresholds_and_model_reruns_preserve_manual_decisions_and_raw_history(self):
        job = self.client.post("/api/jobs", json={"policy_id": self.policy["id"], "scope": {"repository_ids": [1]}}).json()
        self.app.state.engine.run_once()
        with connection(self.path) as conn:
            original = tuple(conn.execute("SELECT request_json,response_json,results_json,cost FROM job_items WHERE job_id=?", (job["id"],)).fetchone())
        saved = self.save([{"tag_id": "coding", "applied": False}, {"tag_id": "browser", "applied": True}])
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual([tag["tag_id"] for tag in saved.json()["tags"]], ["browser"])
        self.policy = self.client.post("/api/policies/" + self.policy["id"] + "/thresholds", json={"base_revision": 1, "thresholds": {"coding": 0, "browser": 1}}).json()
        current = {tag["tag_id"]: tag for tag in self.result()["items"][0]["results"]}
        self.assertEqual((current["coding"]["score"], current["coding"]["model_score"], current["coding"]["applied"]), (0, .92, False))
        self.assertEqual((current["browser"]["score"], current["browser"]["source"]), (1, "manual"))
        old = self.client.get("/api/policies/" + self.policy["id"] + "/results", params={"revision": 1}).json()["items"][0]["results"]
        self.assertTrue(old[0]["applied"])
        self.assertFalse(old[1]["applied"])
        self.client.post("/api/jobs", json={"policy_id": self.policy["id"], "scope": {"repository_ids": [1]}, "skip_completed": False})
        self.app.state.engine.run_once()
        self.assertEqual(self.client.get("/api/repositories/1").json()["tags"][0]["source"], "manual")
        with connection(self.path) as conn:
            self.assertEqual(tuple(conn.execute("SELECT request_json,response_json,results_json,cost FROM job_items WHERE job_id=?", (job["id"],)).fetchone()), original)
        self.assertEqual(self.save([]).status_code, 200)
        self.assertEqual(self.client.get(self.endpoint).json()["items"][0]["tags"][0]["manual_applied"], None)

    def test_changed_rules_deactivate_old_manual_labels_without_deleting_them(self):
        self.assertEqual(self.save([{"tag_id": "coding", "applied": True}]).status_code, 200)
        body = policy_body(tags=self.policy["tags"], description="Different shared rule.")
        self.policy = self.client.put("/api/policies/" + self.policy["id"], json=body).json()
        self.assertEqual(self.client.get("/api/repositories/1").json()["tags"], [])
        self.assertIsNone(self.client.get(self.endpoint).json()["items"][0]["tags"][0]["manual_applied"])
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM manual_annotations").fetchone()[0], 1)
        self.assertEqual(self.fake.requests, [])

    def test_single_label_choice_suppresses_competitors_and_reset_restores_auto(self):
        self.policy = self.client.put("/api/policies/" + self.policy["id"], json=policy_body(mode="single_label", tags=self.policy["tags"])).json()
        rejected = self.save([{"tag_id": "coding", "applied": True}, {"tag_id": "browser", "applied": True}])
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.save([{"tag_id": "browser", "applied": True}]).status_code, 200)
        editor = self.client.get(self.endpoint).json()["items"][0]["tags"]
        self.assertIsNone(editor[0]["manual_applied"])
        current = self.result()["items"][0]["results"]
        self.assertFalse(current[0]["manual_applied"])
        self.assertFalse(current[0]["applied"])
        self.assertTrue(current[1]["applied"])
        self.assertEqual(self.save([]).json()["tags"], [])
        self.assertEqual(self.fake.requests, [])

    def test_stale_unknown_duplicate_and_invalid_overrides_are_rejected_atomically(self):
        self.assertEqual(self.save([{"tag_id": "coding", "applied": True}]).status_code, 200)
        self.assertEqual(self.save([{"tag_id": "unknown", "applied": True}]).status_code, 400)
        self.assertEqual(self.save([{"tag_id": "coding", "applied": "true"}]).status_code, 422)
        self.assertEqual(self.save([{"tag_id": "coding", "applied": True}] * 2).status_code, 422)
        self.assertEqual(self.save([], revision=99).status_code, 409)
        self.assertEqual(self.client.get("/api/repositories/1").json()["tags"][0]["score"], 1)
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
