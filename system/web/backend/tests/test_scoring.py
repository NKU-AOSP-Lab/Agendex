"""评分来源复用与阈值版本的离线契约测试，不发出真实模型请求。"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agendex.app import create_app
from agendex.db import connection
from agendex import jev
from agendex.schemas import JobInput
from test_support import FakeJev, create_fixture, policy_body


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "scoring.sqlite3"
        create_fixture(self.path, count=3)
        self.fake = FakeJev()
        self.app = create_app(self.path, self.fake, start_workers=False)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def policy(self, **overrides):
        response = self.client.post("/api/policies", json=policy_body(**overrides))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def classify(self, policy, ids=None, reuse=True):
        response = self.client.post("/api/jobs", json={"policy_id": policy["id"], "scope": {"repository_ids": ids or [1]}, "skip_completed": reuse})
        self.assertEqual(response.status_code, 201, response.text)
        while self.app.state.engine.run_once():
            pass
        return self.client.get("/api/jobs/" + response.json()["id"]).json()

    def results(self, policy, revision=None, **params):
        if revision is not None:
            params["revision"] = revision
        response = self.client.get("/api/policies/" + policy["id"] + "/results", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def threshold(self, policy, values):
        response = self.client.post("/api/policies/" + policy["id"] + "/thresholds", json={"base_revision": policy["revision"], "thresholds": values})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def editable(policy):
        return {key: policy[key] for key in ("name", "description", "mode", "threshold", "tags", "evidence_kinds")}

    def test_tag_thresholds_resolve_legacy_default_and_allow_zero(self):
        tags = policy_body()["tags"]
        tags[1]["threshold"] = .1
        policy = self.policy(threshold=.7, tags=tags)
        self.assertEqual([tag["threshold"] for tag in policy["tags"]], [.7, .1])
        self.classify(policy)
        updated = self.threshold(policy, {"coding": 1, "browser": 0})
        result = self.results(updated)["items"][0]["results"]
        self.assertEqual([tag["applied"] for tag in result], [False, True])
        self.assertEqual(len(self.fake.requests), 1)

    def test_policy_display_name_changes_reuse_but_tag_rules_do_not(self):
        policy = self.policy()
        self.classify(policy)
        renamed_body = self.editable(policy)
        renamed_body["name"] = "名称变化"
        renamed = self.client.put("/api/policies/" + policy["id"], json=renamed_body).json()
        self.assertEqual(renamed["result_count"], 1)
        replay = self.classify(renamed)
        self.assertEqual((replay["skipped"], replay["cost"]), (1, 0))
        changed = self.editable(renamed)
        changed["tags"][0]["description"] = "Only code review agents qualify."
        changed_policy = self.client.put("/api/policies/" + policy["id"], json=changed).json()
        self.assertEqual(changed_policy["result_count"], 0)
        self.classify(changed_policy)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(self.results(policy, 1)["policy"]["result_count"], 1)

    def test_changed_shared_instructions_require_new_scores(self):
        policy = self.policy()
        self.classify(policy)
        document = self.editable(policy)
        document["description"] = "Only independently implemented loops qualify."
        changed = self.client.put("/api/policies/" + policy["id"], json=document).json()
        self.assertEqual(changed["result_count"], 0)
        self.classify(changed)
        self.assertEqual(len(self.fake.requests), 2)

    def test_new_evidence_version_does_not_reuse_old_scores(self):
        policy = self.policy()
        self.classify(policy)
        evidence = self.client.get("/api/repositories/1").json()["evidence"][0]
        response = self.client.post("/api/repositories/1/evidence", json={"kind": "metadata", "version": "new-snapshot", "content": evidence["content"]})
        self.assertEqual(response.status_code, 201)
        self.classify(policy)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(self.results(policy)["items"][0]["evidence_ids"], [response.json()["id"]])

    def test_threshold_and_restore_keep_frozen_evidence_after_new_snapshot_arrives(self):
        policy = self.policy()
        original_job = self.classify(policy)
        original = self.results(policy)["items"][0]
        evidence = self.client.get("/api/repositories/1").json()["evidence"][0]
        response = self.client.post("/api/repositories/1/evidence", json={"kind": "metadata", "version": "later-snapshot", "content": evidence["content"]})
        self.assertEqual(response.status_code, 201)
        updated = self.threshold(policy, {"coding": 1})
        self.assertEqual(self.results(updated)["items"][0]["evidence_ids"], original["evidence_ids"])
        restored_response = self.client.post("/api/policies/" + policy["id"] + "/restore/1")
        self.assertEqual(restored_response.status_code, 200)
        restored = restored_response.json()
        restored_item = self.results(restored)["items"][0]
        self.assertEqual(restored_item["source_job_id"], original_job["id"])
        self.assertEqual(restored_item["evidence_ids"], original["evidence_ids"])
        self.assertEqual(len(self.fake.requests), 1)
        replay_job = self.classify(restored)
        self.assertEqual(replay_job["skipped"], 0)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(self.results(restored)["items"][0]["evidence_ids"], [response.json()["id"]])
        self.assertEqual(self.results(policy, 1)["items"][0]["evidence_ids"], original["evidence_ids"])

    def test_changed_request_model_misses_but_threshold_replay_preserves_original_model(self):
        policy = self.policy()
        job = self.classify(policy)
        self.fake.model = "fixture/new-requested-model"
        updated = self.threshold(policy, {"coding": .5})
        result = self.results(updated)["items"][0]
        self.assertEqual(result["source_job_id"], job["id"])
        self.assertEqual(result["model"], "fixture/served-model")
        self.assertEqual(len(self.fake.requests), 1)
        self.classify(updated)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(self.fake.requests[-1]["model"], self.fake.model)

    def test_actual_question_template_changes_invalidate_reuse(self):
        policy = self.policy()
        self.classify(policy)
        original = jev.build_payload

        def changed(*args):
            payload = original(*args)
            for question in payload["questions"].values():
                question["instructions"] += " Additional strict instruction."
            return payload

        with patch("agendex.jev.build_payload", changed):
            job = self.classify(policy)
        self.assertEqual(job["skipped"], 0)
        self.assertEqual(len(self.fake.requests), 2)

    def test_actual_transmitted_state_changes_invalidate_reuse(self):
        policy = self.policy()
        self.classify(policy)
        original = jev.build_payload

        def changed(*args):
            payload = original(*args)
            payload["state"]["context_version"] = "new-state-template"
            return payload

        with patch("agendex.jev.build_payload", changed):
            job = self.classify(policy)
        self.assertEqual(job["skipped"], 0)
        self.assertEqual(len(self.fake.requests), 2)

    def test_completed_all_reused_task_still_checks_its_original_request_template(self):
        policy = self.policy()
        self.classify(policy)
        copy = self.policy(name="相同标签的另一策略")
        reused = self.classify(copy)
        self.assertEqual(reused["skipped"], 1)
        original = jev.build_payload

        def changed(*args):
            payload = original(*args)
            payload["state"]["context_version"] = "updated-after-reuse"
            return payload

        with patch("agendex.jev.build_payload", changed):
            next_job = self.classify(copy)
        self.assertNotEqual(next_job["id"], reused["id"])
        self.assertEqual(len(self.fake.requests), 2)

    def test_template_upgrade_preserves_bound_threshold_replay_but_new_jobs_miss(self):
        policy = self.policy()
        source_job = self.classify(policy)
        original = jev.build_payload

        def upgraded(*args):
            payload = original(*args)
            payload["state"]["template_version"] = "v2"
            for question in payload["questions"].values():
                question["instructions"] += " Updated evaluation template."
            return payload

        with patch("agendex.jev.build_payload", upgraded):
            updated = self.threshold(policy, {"coding": .95})
            self.assertEqual(self.results(updated)["items"][0]["source_job_id"], source_job["id"])
            restored = self.client.post("/api/policies/" + policy["id"] + "/restore/1").json()
            self.assertEqual(self.results(restored)["items"][0]["source_job_id"], source_job["id"])
            copied = self.policy(name="升级模板后的新策略")
            self.assertEqual(copied["result_count"], 0)
            self.assertEqual(len(self.fake.requests), 1)
            rerun = self.classify(restored)
        self.assertEqual(rerun["skipped"], 0)
        self.assertEqual(len(self.fake.requests), 2)

    def single_handler(self, choice, probabilities):
        return lambda payload: {"model": "fixture/single", "usage": {"input_tokens": 100, "output_tokens": 5, "cost": .001},
                                "answers": {"classification": {"type": "choice", "choice": choice, "probabilities": probabilities}}}

    def test_single_none_stays_none_when_all_thresholds_are_zero(self):
        self.fake.handler = self.single_handler("__none__", {"coding": .05, "browser": .05, "__none__": .9})
        policy = self.policy(mode="single_label")
        self.classify(policy)
        updated = self.threshold(policy, {"coding": 0, "browser": 0})
        result = self.results(updated)
        self.assertIsNone(result["items"][0]["winner_tag_id"])
        self.assertFalse(any(tag["applied"] for tag in result["items"][0]["results"]))
        self.assertTrue(all(tag["distribution"] == [] for tag in result["tag_stats"]))
        self.assertEqual(len(self.fake.requests), 1)

    def test_single_threshold_only_applies_original_winner_and_maps_copied_ids(self):
        self.fake.handler = self.single_handler("coding", {"coding": .65, "browser": .25, "__none__": .1})
        policy = self.policy(mode="single_label")
        job = self.classify(policy)
        copied_tags = [{**tag, "id": "copy-" + tag["id"], "threshold": 0} for tag in policy["tags"]]
        copied = self.policy(name="复制", mode="single_label", tags=copied_tags)
        result = self.results(copied)["items"][0]
        self.assertEqual(result["winner_tag_id"], "copy-coding")
        self.assertEqual([tag["applied"] for tag in result["results"]], [True, False])
        self.assertEqual(result["source_job_id"], job["id"])
        self.assertEqual(len(self.fake.requests), 1)

    def test_old_inflight_job_adds_to_compatible_current_threshold_version(self):
        policy = self.policy()
        job = self.app.state.engine.create_job(JobInput(policy_id=policy["id"], scope={"repository_ids": [1]}))
        item = self.app.state.engine._claim()
        updated = self.threshold(policy, {"coding": 1, "browser": .1})
        self.app.state.engine._execute(item)
        current = self.results(updated)
        self.assertEqual(current["total"], 1)
        self.assertEqual([tag["applied"] for tag in current["items"][0]["results"]], [False, True])
        self.assertEqual([tag["applied"] for tag in self.results(policy, 1)["items"][0]["results"]], [True, False])
        self.assertEqual(current["items"][0]["source_job_id"], job["id"])

    def test_old_inflight_job_does_not_bind_incompatible_current_rules(self):
        policy = self.policy()
        self.client.post("/api/jobs", json={"policy_id": policy["id"], "scope": {"repository_ids": [1]}})
        item = self.app.state.engine._claim()
        changed = self.editable(policy)
        changed["description"] = "A different shared policy."
        updated = self.client.put("/api/policies/" + policy["id"], json=changed).json()
        self.app.state.engine._execute(item)
        self.assertEqual(self.results(updated)["total"], 0)
        self.assertEqual(self.results(policy, 1)["total"], 1)

    def test_result_statistics_cover_full_version_not_search_or_pagination(self):
        policy = self.policy()
        self.classify(policy, ids=[1, 2, 3])
        data = self.results(policy, q="repo1", page_size=1)
        self.assertEqual((data["total"], len(data["items"]), data["policy"]["result_count"]), (1, 1, 3))
        self.assertEqual(data["tag_stats"][0]["scored_count"], 3)
        self.assertEqual(sum(point["count"] for point in data["tag_stats"][0]["distribution"]), 3)

    def test_completed_legacy_responses_backfill_without_http_or_source_mutation(self):
        policy = self.policy()
        job = self.classify(policy)
        with connection(self.path) as conn:
            original = tuple(conn.execute("SELECT request_json,response_json,results_json,cost FROM job_items WHERE job_id=?", (job["id"],)).fetchone())
            conn.execute("DELETE FROM tag_assignments")
            conn.execute("DELETE FROM policy_results")
            conn.execute("DELETE FROM score_cache")
        restored = create_app(self.path, self.fake, start_workers=False)
        with TestClient(restored) as client:
            response = client.get("/api/policies/" + policy["id"] + "/results").json()
            self.assertEqual(response["total"], 1)
        self.assertEqual(len(self.fake.requests), 1)
        with connection(self.path) as conn:
            self.assertEqual(tuple(conn.execute("SELECT request_json,response_json,results_json,cost FROM job_items WHERE job_id=?", (job["id"],)).fetchone()), original)

    def test_legacy_backfill_preserves_materialized_latest_scoring_source(self):
        policy = self.policy()
        self.classify(policy)
        self.fake.handler = lambda payload: {"model": "fixture/served-model", "usage": {"input_tokens": 5, "output_tokens": 2, "cost": .002},
                                             "answers": {key: {"type": "noul", "noul": .4 if "coding" in key else .9} for key in payload["questions"]}}
        latest_job = self.classify(policy, reuse=False)
        with connection(self.path) as conn:
            existing = [row[0] for row in conn.execute("SELECT label FROM tag_assignments")]
            self.assertEqual(existing, ["网页浏览"])
            conn.execute("DELETE FROM policy_results")
            conn.execute("DELETE FROM score_cache")
        restored_app = create_app(self.path, self.fake, start_workers=False)
        with TestClient(restored_app) as client:
            result = client.get("/api/policies/" + policy["id"] + "/results").json()
            self.assertEqual(result["items"][0]["source_job_id"], latest_job["id"])
            self.assertEqual([tag["applied"] for tag in result["items"][0]["results"]], [False, True])
            self.assertEqual([tag["label"] for tag in client.get("/api/repositories/1").json()["tags"]], existing)
        self.assertEqual(len(self.fake.requests), 2)

    def test_legacy_backfill_uses_latest_complete_source_when_latest_has_no_tags(self):
        policy = self.policy()
        self.classify(policy)
        self.fake.handler = lambda payload: {"model": "fixture/served-model", "usage": {"input_tokens": 5, "output_tokens": 2, "cost": .002},
                                             "answers": {key: {"type": "noul", "noul": .1} for key in payload["questions"]}}
        latest = self.classify(policy, reuse=False)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tag_assignments").fetchone()[0], 0)
            conn.execute("DELETE FROM policy_results")
            conn.execute("DELETE FROM score_cache")
        restored_app = create_app(self.path, self.fake, start_workers=False)
        with TestClient(restored_app) as client:
            result = client.get("/api/policies/" + policy["id"] + "/results").json()
            self.assertEqual(result["items"][0]["source_job_id"], latest["id"])
            self.assertFalse(any(tag["applied"] for tag in result["items"][0]["results"]))
        self.assertEqual(len(self.fake.requests), 2)

    def test_reuse_does_not_create_a_second_raw_response_or_cost(self):
        policy = self.policy()
        original = self.classify(policy)
        replay = self.classify(policy)
        with connection(self.path) as conn:
            item = conn.execute("SELECT * FROM job_items WHERE job_id=?", (replay["id"],)).fetchone()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(replay["id"], original["id"])
        self.assertIsNone(item["source_job_item_id"])
        self.assertIsNotNone(item["request_json"])
        self.assertIsNotNone(item["response_json"])
        self.assertIsNotNone(item["usage_json"])
        self.assertEqual(item["cost"], original["cost"])
        self.assertEqual(len(self.fake.requests), 1)


if __name__ == "__main__":
    unittest.main()
