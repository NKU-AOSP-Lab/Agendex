from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest

from agendex.db import connection
from agendex.evidence import add_evidence, load_bundle
from agendex.jev import build_payload
from agendex.jobs import JobEngine
from agendex.jev import JevError
from agendex.schemas import EvidenceInput, JobInput, PolicyInput, ThresholdInput
from agendex.services import ServiceError, get_job, get_repository, policy_results, save_policy, update_thresholds
from test_support import FakeJev, create_fixture, policy_body


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        create_fixture(self.path)
        self.fake = FakeJev()
        self.engine = JobEngine(self.path, self.fake)
        with connection(self.path) as conn:
            self.policy = save_policy(conn, PolicyInput(**policy_body()))

    def tearDown(self):
        self.temp.cleanup()

    def job(self, ids=None, **overrides):
        return self.engine.create_job(JobInput(policy_id=self.policy["id"], scope={"repository_ids": ids or [1]}, **overrides))

    def fetch(self, identifier):
        with connection(self.path) as conn:
            return get_job(conn, identifier, detail=True)

    def test_one_request_classifies_all_tags_and_records_cost_and_evidence(self):
        job = self.job()
        self.assertEqual(self.fake.requests, [])
        self.assertTrue(self.engine.run_once())
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(len(self.fake.requests[0]["questions"]), 2)
        completed = self.fetch(job["id"])
        self.assertEqual((completed["status"], completed["completed"], completed["failed"]), ("completed", 1, 0))
        self.assertAlmostEqual(completed["cost"], .001)
        with connection(self.path) as conn:
            detail = get_repository(conn, 1)
            self.assertEqual([tag["label"] for tag in detail["tags"]], ["代码开发"])
            self.assertEqual(detail["classifications"][0]["evidence_ids"], ["evidence-1"])
            item = conn.execute("SELECT * FROM job_items").fetchone()
            self.assertIsNotNone(item["request_json"])
            self.assertIsNotNone(item["response_json"])

    def test_completed_matching_revision_and_evidence_reuse_the_existing_task(self):
        first = self.job()
        self.engine.run_once()
        second = self.job()
        self.assertEqual(second["id"], first["id"])
        self.assertEqual((second["status"], second["completed"], second["skipped"]), ("completed", 1, 0))
        self.assertFalse(self.engine.run_once())
        self.assertEqual(len(self.fake.requests), 1)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_task_sources_are_frozen_and_do_not_change_the_saved_policy(self):
        with connection(self.path) as conn:
            add_evidence(conn, 1, EvidenceInput(kind="readme", version="readme-A", content="An agent implementation."))
        job = self.job(evidence_kinds=["metadata", "readme"])
        self.assertEqual(job["evidence_kinds"], ["metadata", "readme"])
        with connection(self.path) as conn:
            add_evidence(conn, 1, EvidenceInput(kind="readme", version="readme-B", content="Later README content."))
            saved = conn.execute("SELECT document_json FROM policy_versions WHERE policy_id=?", (self.policy["id"],)).fetchone()[0]
            self.assertEqual(json.loads(saved)["evidence_kinds"], ["metadata"])
            scope = json.loads(conn.execute("SELECT scope_json FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])
            self.assertEqual(scope["evidence_kinds"], ["metadata", "readme"])
        self.assertTrue(self.engine.run_once())
        documents = self.fake.requests[0]["state"]["repository_evidence"]
        self.assertEqual({item["kind"] for item in documents}, {"metadata", "readme"})
        self.assertEqual(next(item for item in documents if item["kind"] == "readme")["version"], "readme-A")
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM score_cache").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM policy_results").fetchone()[0], 1)

    def test_task_sources_separate_inflight_and_completed_reuse(self):
        with connection(self.path) as conn:
            add_evidence(conn, 1, EvidenceInput(kind="readme", version="readme-A", content="An agent implementation."))
        metadata = self.job()
        running = self.engine._claim()
        combined = self.job(evidence_kinds=["metadata", "readme"])
        self.assertNotEqual(combined["id"], metadata["id"])
        self.assertEqual(self.job(evidence_kinds=["readme", "metadata"])["id"], combined["id"])
        # Distinct evidence can run while the first request is still in flight.
        self.assertTrue(self.engine.run_once())
        self.engine._execute(running)
        self.assertEqual(len(self.fake.requests), 2)
        for kinds, original in [(["metadata"], metadata), (["metadata", "readme"], combined)]:
            reused = self.job(evidence_kinds=kinds)
            self.assertEqual(reused["id"], original["id"])
            self.assertEqual((reused["completed"], reused["cost"]), (1, .001))
        self.assertFalse(self.engine.run_once())
        self.assertEqual(len(self.fake.requests), 2)

    def test_legacy_jobs_without_frozen_sources_keep_the_policy_sources(self):
        job = self.job()
        with connection(self.path) as conn:
            scope = json.loads(conn.execute("SELECT scope_json FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])
            del scope["evidence_kinds"]
            conn.execute("UPDATE jobs SET scope_json=? WHERE id=?", (json.dumps(scope), job["id"]))
        self.assertEqual(self.fetch(job["id"])["evidence_kinds"], ["metadata"])
        self.assertEqual(self.job()["id"], job["id"])
        self.engine.run_once()
        self.assertEqual(self.job(evidence_kinds=["metadata"])["id"], job["id"])
        with connection(self.path) as conn:
            # Reading and completing a legacy task must not rewrite its scope.
            scope = json.loads(conn.execute("SELECT scope_json FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])
            self.assertNotIn("evidence_kinds", scope)

    def test_task_source_scores_remain_available_for_threshold_replay(self):
        with connection(self.path) as conn:
            add_evidence(conn, 1, EvidenceInput(kind="readme", version="readme-A", content="An agent implementation."))
        original = self.job(evidence_kinds=["metadata", "readme"])
        self.engine.run_once()
        with connection(self.path) as conn:
            changed = update_thresholds(conn, self.policy["id"], ThresholdInput(base_revision=1, thresholds={"coding": .99}), self.fake.model)
            rows = policy_results(conn, changed["id"])["items"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_job_id"], original["id"])
            self.assertFalse(rows[0]["results"][0]["applied"])
        self.assertEqual(len(self.fake.requests), 1)

    def test_repeating_a_failed_exact_task_preserves_its_error_and_cost(self):
        self.fake.handler = lambda payload: {"model": "fixture", "usage": {"cost": .002, "input_tokens": 10, "output_tokens": 1}, "answers": {}}
        first = self.job()
        self.engine.run_once()
        repeated = self.job()
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual((repeated["status"], repeated["failed"], repeated["cost"]), ("completed_with_errors", 1, .002))
        self.assertFalse(self.engine.run_once())
        self.assertEqual(len(self.fake.requests), 1)

    def test_larger_followup_scope_reuses_completed_items(self):
        first = self.job([1, 2])
        self.engine.run_once()
        self.engine.run_once()
        larger = self.job([1, 2, 3, 4])
        self.assertNotEqual(larger["id"], first["id"])
        self.assertEqual((larger["total"], larger["skipped"], larger["completed"]), (4, 2, 0))
        self.engine.run_once()
        self.engine.run_once()
        self.assertEqual(len(self.fake.requests), 4)
        self.assertEqual(self.job([1, 2, 3, 4])["id"], larger["id"])

    def test_job_keeps_original_policy_and_current_revision_hides_old_tags(self):
        job = self.job()
        with connection(self.path) as conn:
            save_policy(conn, PolicyInput(**policy_body(name="新策略", threshold=.95)), self.policy["id"])
        self.engine.run_once()
        self.assertEqual(self.fetch(job["id"])["policy_revision"], 1)
        with connection(self.path) as conn:
            detail = get_repository(conn, 1)
        self.assertEqual(detail["tags"], [])
        self.assertEqual(next(row for row in detail["classifications"] if row["policy_revision"] == 1)["policy_name"], "用途")
        self.assertEqual(detail["classifications"][0]["policy_name"], "新策略")

    def test_pause_resume_and_cancel_stop_new_dispatches(self):
        job = self.job([1, 2])
        self.engine.control(job["id"], "pause")
        self.assertFalse(self.engine.run_once())
        self.engine.control(job["id"], "resume")
        self.engine.run_once()
        self.engine.control(job["id"], "cancel")
        self.assertFalse(self.engine.run_once())
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.fetch(job["id"])["status"], "cancelled")

    def test_network_failure_pauses_without_false_negative_or_retry(self):
        def fail(payload):
            raise JevError("fixture network failure", pause=True)
        self.fake.handler = fail
        job = self.job([1, 2])
        self.engine.run_once()
        self.assertEqual(self.fetch(job["id"])["status"], "paused")
        self.assertFalse(self.engine.run_once())
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tag_assignments").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM score_cache").fetchone()[0], 0)
            self.assertEqual([r[0] for r in conn.execute("SELECT status FROM job_items ORDER BY id")], ["failed", "queued"])
        self.assertEqual(self.fetch(job["id"])["unknown_cost_count"], 1)

    def test_invalid_paid_response_is_preserved_and_five_failures_trip_circuit(self):
        self.fake.handler = lambda payload: {"model": "fixture", "usage": {"cost": .002, "input_tokens": 10, "output_tokens": 1}, "answers": {}}
        job = self.job(list(range(1, 9)))
        for _ in range(5):
            self.assertTrue(self.engine.run_once())
        self.assertFalse(self.engine.run_once())
        result = self.fetch(job["id"])
        self.assertEqual((result["status"], result["failed"], result["completed"]), ("paused", 5, 0))
        self.assertAlmostEqual(result["cost"], .01)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_items WHERE response_json IS NOT NULL").fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tag_assignments").fetchone()[0], 0)

    def test_restart_never_reissues_an_indeterminate_paid_request(self):
        job = self.job([1, 2])
        item = self.engine._claim()
        with connection(self.path) as conn:
            conn.execute("UPDATE job_items SET request_json='{}' WHERE id=?", (item["id"],))
        self.engine.recover()
        self.assertEqual(self.fetch(job["id"])["status"], "paused")
        self.engine.control(job["id"], "resume")
        self.engine.run_once()
        self.assertEqual(len(self.fake.requests), 1)
        result = self.fetch(job["id"])
        self.assertEqual((result["completed"], result["failed"]), (1, 1))

    def test_restart_reuses_persisted_response_without_another_model_call(self):
        job = self.job()
        item = self.engine._claim()
        response = {"model": "fixture/saved", "usage": {"cost": .02, "input_tokens": 8, "output_tokens": 2},
                    "answers": {"tag_coding": {"type": "noul", "noul": .95}, "tag_browser": {"type": "noul", "noul": .1}}}
        with connection(self.path) as conn:
            bundle = load_bundle(conn, 1, ["evidence-1"])
            request = build_payload(json.loads(item["document_json"]), bundle, self.fake.model)
            conn.execute("UPDATE job_items SET request_json=?,response_json=?,cost=.02 WHERE id=?", (json.dumps(request), json.dumps(response), item["id"]))
        self.engine.recover()
        self.assertEqual(self.fake.requests, [])
        result = self.fetch(job["id"])
        self.assertEqual((result["status"], result["completed"]), ("completed", 1))
        self.assertAlmostEqual(result["cost"], .02)
        with connection(self.path) as conn:
            self.assertEqual(get_repository(conn, 1)["tags"][0]["label"], "代码开发")

    def test_future_source_files_keep_one_version_and_job_evidence_is_frozen(self):
        with connection(self.path) as conn:
            for path in ["agent.py", "tools.py"]:
                add_evidence(conn, 1, EvidenceInput(kind="source_code", version="commit-A", content=path, provenance={"path": path}))
            # Seed a historical source policy explicitly: source differences no
            # longer distinguish new user-facing policy definitions.
            self.policy = save_policy(conn, PolicyInput(**policy_body(evidence_kinds=["metadata", "source_code"])), deduplicate=False)
        self.job()
        with connection(self.path) as conn:
            add_evidence(conn, 1, EvidenceInput(kind="source_code", version="commit-B", content="new version", provenance={"path": "agent.py"}))
        self.engine.run_once()
        sources = [item for item in self.fake.requests[0]["state"]["repository_evidence"] if item["kind"] == "source_code"]
        self.assertEqual({item["version"] for item in sources}, {"commit-A"})
        self.assertEqual({item["path"] for item in sources}, {"agent.py", "tools.py"})
        self.assertEqual({item["source_key"] for item in sources}, {"agent.py", "tools.py"})

    def test_concurrent_workers_claim_each_repository_once(self):
        self.job(list(range(1, 9)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.engine.run_once(), range(8)))
        self.assertEqual(len(self.fake.requests), 8)
        ids = [payload["state"]["repository_evidence"][0]["content"]["id"] for payload in self.fake.requests]
        self.assertEqual(sorted(ids), list(range(1, 9)))

    def test_concurrent_duplicate_submissions_reuse_the_existing_paused_job(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(lambda _: self.job([1, 2]), range(8)))
        self.assertEqual(len({job["id"] for job in jobs}), 1)
        original = jobs[0]
        self.engine.control(original["id"], "pause")
        duplicate = self.job([2, 1])
        self.assertEqual((duplicate["id"], duplicate["status"]), (original["id"], "paused"))
        self.assertFalse(self.engine.run_once())
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_items").fetchone()[0], 2)
        self.assertEqual(self.fake.requests, [])

    def test_overlapping_jobs_share_inflight_work_and_reuse_at_claim(self):
        first = self.job([1, 2, 3, 4])
        with connection(self.path) as conn:
            copied = save_policy(conn, PolicyInput(**policy_body(name="相同规则的另一策略")))
        second = self.engine.create_job(JobInput(policy_id=copied["id"], scope={"repository_ids": [3, 4, 5, 6, 7, 8]}))
        barrier = threading.Barrier(9, timeout=10)

        def blocked_response(payload):
            barrier.wait()
            return {"model": "fixture/served-model", "usage": {"input_tokens": 100, "output_tokens": 5, "cost": .001},
                    "answers": {key: {"type": "noul", "noul": .1} for key in payload["questions"]}}

        self.fake.handler = blocked_response
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.engine.run_once) for _ in range(8)]
            barrier.wait()
            self.assertEqual(len(self.fake.requests), 8)
            for future in futures:
                self.assertTrue(future.result(timeout=10))
        # Both tasks existed before the first score; dispatch rechecks the cache.
        self.assertFalse(self.engine.run_once())
        ids = [payload["state"]["repository_evidence"][0]["content"]["id"] for payload in self.fake.requests]
        self.assertEqual(sorted(ids), list(range(1, 9)))
        finished = [self.fetch(first["id"]), self.fetch(second["id"])]
        self.assertEqual(sum(job["completed"] for job in finished), 8)
        self.assertEqual(sum(job["skipped"] for job in finished), 2)
        self.assertAlmostEqual(sum(job["cost"] for job in finished), .008)
        with connection(self.path) as conn:
            reused = conn.execute("SELECT * FROM job_items WHERE status='skipped'").fetchall()
            self.assertEqual(len(reused), 2)
            self.assertTrue(all(row["source_job_item_id"] is not None and row["cost"] == 0 and row["request_json"] is None for row in reused))
            # A valid all-negative result is reusable, too.
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tag_assignments").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM policy_results WHERE policy_version_id=(SELECT id FROM policy_versions WHERE policy_id=?)", (copied["id"],)).fetchone()[0], 6)

    def test_paused_or_cancelled_parent_does_not_release_an_inflight_request(self):
        for action, repository_id in [("pause", 1), ("cancel", 3)]:
            with self.subTest(action=action):
                original = self.job([repository_id])
                running = self.engine._claim()
                self.engine.control(original["id"], action)
                following = self.job([repository_id, repository_id + 1])
                unrelated = self.engine._claim()
                self.assertEqual(unrelated["repository_id"], repository_id + 1)
                self.engine._execute(unrelated)
                self.assertIsNone(self.engine._claim())
                self.engine._execute(running)
                self.assertIsNone(self.engine._claim())
                result = self.fetch(following["id"])
                self.assertEqual((result["status"], result["completed"], result["skipped"]), ("completed", 1, 1))
        ids = [payload["state"]["repository_evidence"][0]["content"]["id"] for payload in self.fake.requests]
        self.assertEqual(sorted(ids), [1, 2, 3, 4])

    def test_changed_evidence_rules_or_request_model_are_not_deduplicated(self):
        first = self.job([1])
        with connection(self.path) as conn:
            add_evidence(conn, 1, EvidenceInput(kind="metadata", version="snapshot-B", content={"id": 1, "description": "Updated evidence"}))
        second = self.job([1])
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(self.engine.run_once())
        self.assertTrue(self.engine.run_once())
        with connection(self.path) as conn:
            body = policy_body()
            body["tags"][0]["description"] = "Writes software for a documented user task."
            self.policy = save_policy(conn, PolicyInput(**body), self.policy["id"])
        third = self.job([1])
        self.assertNotEqual(second["id"], third["id"])
        self.assertTrue(self.engine.run_once())
        self.fake.model = "fixture/another-request-model"
        fourth = self.job([1])
        self.assertNotEqual(third["id"], fourth["id"])
        self.assertTrue(self.engine.run_once())
        self.assertEqual(len(self.fake.requests), 4)

    def test_explicit_force_waits_for_inflight_work_but_remains_a_rerun(self):
        self.job([1])
        running = self.engine._claim()
        forced = self.job([1], skip_completed=False)
        self.assertIsNone(self.engine._claim())
        self.engine._execute(running)
        self.assertTrue(self.engine.run_once())
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual((self.fetch(forced["id"])["completed"], self.fetch(forced["id"])["skipped"]), (1, 0))

    def test_missing_future_evidence_fails_without_request(self):
        with connection(self.path) as conn:
            self.policy = save_policy(conn, PolicyInput(**policy_body(evidence_kinds=["readme"])), deduplicate=False)
        result = self.job()
        self.assertEqual((result["status"], result["failed"]), ("completed_with_errors", 1))
        self.assertFalse(self.engine.run_once())
        self.assertEqual(self.fake.requests, [])

    def test_modified_evidence_is_rejected_before_model_request(self):
        job = self.job()
        with connection(self.path) as conn:
            conn.execute("UPDATE evidence SET content='{}' WHERE id='evidence-1'")
        self.engine.run_once()
        self.assertEqual(self.fake.requests, [])
        result = self.fetch(job["id"])
        self.assertEqual((result["failed"], result["completed"], result["unknown_cost_count"]), (1, 0, 0))

    def test_selected_model_routes_requests_and_reuses_only_its_own_scores(self):
        glm = FakeJev()
        glm.model = "glm-5.3"
        engine = JobEngine(self.path, self.fake, glm_client=glm)
        first = engine.create_job(JobInput(policy_id=self.policy["id"], model="typesafe/jev-1.14", scope={"repository_ids": [1]}))
        second = engine.create_job(JobInput(policy_id=self.policy["id"], model="glm-5.3", scope={"repository_ids": [1]}))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["model"], "glm-5.3")
        self.assertTrue(engine.run_once())
        self.assertTrue(engine.run_once())
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(len(glm.requests), 1)
        self.assertNotIn("glm_protocol", self.fake.requests[0])
        self.assertIn("glm_protocol", glm.requests[0])
        reused = engine.create_job(JobInput(policy_id=self.policy["id"], model="glm-5.3", scope={"repository_ids": [1]}))
        self.assertEqual(reused["id"], second["id"])
        self.assertEqual((reused["completed"], reused["cost"]), (1, .001))
        self.assertFalse(engine.run_once())
        self.assertEqual(len(glm.requests), 1)
        # Returning to Jev must find its former score, not use the newer GLM result.
        jev_again = engine.create_job(JobInput(policy_id=self.policy["id"], model="typesafe/jev-1.14", scope={"repository_ids": [1]}))
        self.assertEqual(jev_again["id"], first["id"])
        self.assertEqual((jev_again["completed"], jev_again["cost"]), (1, .001))
        self.assertEqual(len(self.fake.requests), 1)

    def test_selected_glm_credentials_are_required_without_falling_back_to_jev(self):
        glm = FakeJev()
        glm.model = "glm-5.3"
        glm.configured = lambda: False
        engine = JobEngine(self.path, self.fake, glm_client=glm)
        with self.assertRaises(ServiceError):
            engine.create_job(JobInput(policy_id=self.policy["id"], model="glm-5.3", scope={"repository_ids": [1]}))
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(glm.requests, [])

    def test_glm_failure_pauses_that_job_and_resume_checks_its_selected_client(self):
        def fail(_payload):
            raise JevError("GLM fixture network failure", pause=True)
        glm = FakeJev(fail)
        glm.model = "glm-5.3"
        engine = JobEngine(self.path, self.fake, glm_client=glm)
        job = engine.create_job(JobInput(policy_id=self.policy["id"], model="glm-5.3", scope={"repository_ids": [1, 2]}))
        self.assertTrue(engine.run_once())
        self.assertEqual(self.fetch(job["id"])["status"], "paused")
        glm.configured = lambda: False
        with self.assertRaises(ServiceError):
            engine.control(job["id"], "resume")
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(len(glm.requests), 1)


if __name__ == "__main__":
    unittest.main()
