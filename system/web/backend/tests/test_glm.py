"""GLM 传输和策略生成的离线契约；不访问真实凭证或远程服务。"""

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from pydantic import ValidationError

from agendex.db import connection, initialize_database
from agendex.glm import GLMClient, prepare_payload
from agendex.jev import JevError, build_payload, response_usage, validate_response
from agendex.policy_generation import GenerationInput, GENERATION_PROTOCOL, POLICY_GENERATION_EXAMPLE, generate_policy
from agendex.services import ServiceError
from test_jev import FakeOpener
from test_support import policy_body


FIXTURE_BASE_URL = "https://glm.example.test/v1"


def reply(content, *, cost=None, finish_reason="stop"):
    usage = {"prompt_tokens": 100, "completion_tokens": 20}
    if cost is not None:
        usage["cost"] = cost
    return {"id": "fixture-completion", "model": "glm-5.3", "usage": usage,
            "choices": [{"finish_reason": finish_reason, "message": {
                "role": "assistant", "content": content if isinstance(content, str) else json.dumps(content),
            }}]}


class GLMTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "AGENDEX_GLM_API_KEY": "fixture-key",
            "AGENDEX_GLM_BASE_URL": FIXTURE_BASE_URL + "/",
        }, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_full_evidence_and_all_labels_share_one_request_with_unknown_cost(self):
        policy = policy_body()
        readme = "# Coding and browser agent\n" * 18000
        payload = prepare_payload(build_payload(policy, [
            {"kind": "metadata", "version": "m", "content": {"description": "A coding agent"}},
            {"kind": "readme", "version": "r", "content": readme},
        ], "glm-5.3"))
        raw = reply({"answers": {"tag_coding": {"type": "noul", "noul": .9},
                                 "tag_browser": {"type": "noul", "noul": .2}}})
        opener = FakeOpener(data=raw)
        result = GLMClient(opener).request(payload)
        self.assertEqual([row["applied"] for row in validate_response(result, policy)[0]], [True, False])
        self.assertIsNone(response_usage(result)[1])
        self.assertEqual(result["provider_response"], raw)
        self.assertEqual(result["usage"], {"input_tokens": 100, "output_tokens": 20})
        self.assertEqual(len(opener.calls), 1)
        sent = json.loads(opener.calls[0].data)
        evidence = json.loads(sent["messages"][1]["content"])["state"]["repository_evidence"]
        self.assertEqual(evidence[1]["content"], readme)
        self.assertEqual(opener.calls[0].full_url, FIXTURE_BASE_URL + "/chat/completions")

    def test_invalid_paid_answer_is_returned_for_durable_queue_recording(self):
        raw = reply("not JSON", cost=.002)
        client = GLMClient(FakeOpener(data=raw))
        result = client.request(prepare_payload(build_payload(policy_body(), [], "glm-5.3")))
        self.assertEqual(result["provider_response"], raw)
        self.assertEqual(response_usage(result)[1], .002)
        with self.assertRaises(JevError):
            validate_response(result, policy_body())

    def test_single_label_none_retains_existing_validation_semantics(self):
        policy = policy_body(mode="single_label")
        raw = reply({"answers": {"classification": {"type": "choice", "choice": "__none__",
                     "probabilities": {"coding": .1, "browser": .1, "__none__": .8}}}})
        result = GLMClient(FakeOpener(data=raw)).request(prepare_payload(build_payload(policy, [], "glm-5.3")))
        self.assertFalse(any(row["applied"] for row in validate_response(result, policy)[0]))

    def test_credentials_and_http_errors_are_not_exposed_or_retried(self):
        key = "fixture-secret"
        failure = urllib.error.HTTPError(FIXTURE_BASE_URL + "/chat/completions", 401,
                                         "failure " + key, {}, io.BytesIO(key.encode()))
        opener = FakeOpener(error=failure)
        with patch.dict(os.environ, {"AGENDEX_GLM_API_KEY": key}):
            with self.assertRaises(JevError) as caught:
                GLMClient(opener).chat({"model": "glm-5.3", "messages": []})
        self.assertTrue(caught.exception.pause)
        self.assertNotIn(key, str(caught.exception))
        self.assertEqual(len(opener.calls), 1)
        for key in ("fixture\nInjected", "fixture key"):
            opener = FakeOpener(data={})
            with patch.dict(os.environ, {"AGENDEX_GLM_API_KEY": key}):
                with self.assertRaises(JevError):
                    GLMClient(opener).chat({})
            self.assertEqual(opener.calls, [])

    def test_configuration_requires_an_explicit_https_base_url(self):
        opener = FakeOpener(data={})
        self.assertTrue(GLMClient(opener).configured())
        del os.environ["AGENDEX_GLM_BASE_URL"]
        client = GLMClient(opener)
        self.assertFalse(client.configured())
        with self.assertRaisesRegex(JevError, "尚未配置 GLM 服务地址"):
            client.chat({})

        for address in ("http://glm.example.test/v1", "https://[invalid", "https://glm.example.test:bad/v1",
                        FIXTURE_BASE_URL + "?", FIXTURE_BASE_URL + "#", "https://@glm.example.test/v1"):
            with self.subTest(address=address), patch.dict(os.environ, {"AGENDEX_GLM_BASE_URL": address}):
                client = GLMClient(opener)
                self.assertFalse(client.configured())
                with self.assertRaisesRegex(JevError, "有效的 HTTPS 服务地址"):
                    client.chat({})
        self.assertEqual(opener.calls, [])

    def test_generation_example_creates_only_a_valid_draft_and_audit_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "generation.sqlite3"
            initialize_database(path)
            raw = reply(POLICY_GENERATION_EXAMPLE["assistant"])
            opener = FakeOpener(data=raw)
            body = GenerationInput(messages=[{"role": "user", "content": "按代码开发、渗透测试、网页操作分类"}], draft={"name": ""})
            generated = generate_policy(path, body, GLMClient(opener))
            self.assertEqual(len(generated["policy"]["tags"]), 3)
            self.assertEqual(generated["policy"]["evidence_kinds"], ["metadata", "readme"])
            self.assertIsNone(generated["cost"])
            with connection(path) as conn:
                row = conn.execute("SELECT * FROM policy_generations").fetchone()
                self.assertEqual(row["status"], "completed")
                self.assertEqual(json.loads(row["response_json"]), raw)
                self.assertIn("Coding Agent", row["request_json"])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_generation_keeps_sources_local_and_preserves_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "generation.sqlite3"
            initialize_database(path)
            # An older-shaped reply must not override the task-independent
            # compatibility default carried by the user's existing draft.
            answer = copy.deepcopy(POLICY_GENERATION_EXAMPLE["assistant"])
            answer["policy"]["evidence_kinds"] = ["metadata", "readme"]
            opener = FakeOpener(data=reply(answer))
            draft = {"name": "用途", "evidence_kinds": ["source_code"]}
            generated = generate_policy(path, GenerationInput(
                messages=[{"role": "user", "content": "增加应用场景标签"}], draft=draft,
            ), GLMClient(opener))
            self.assertEqual(generated["policy"]["evidence_kinds"], ["source_code"])
            self.assertEqual(draft, {"name": "用途", "evidence_kinds": ["source_code"]})
            sent = json.loads(opener.calls[0].data)
            self.assertNotIn("evidence_kinds", json.dumps(sent))
            self.assertNotIn("metadata", json.dumps(sent).casefold())
            self.assertNotIn("readme", json.dumps(sent).casefold())
            self.assertNotIn("source_code", json.dumps(sent))
            self.assertIn("Data sources are selected separately", sent["messages"][0]["content"])
            with connection(path) as conn:
                saved = json.loads(conn.execute("SELECT request_json FROM policy_generations").fetchone()[0])
                self.assertEqual(saved["protocol"], GENERATION_PROTOCOL)
                self.assertEqual(GENERATION_PROTOCOL, "agendex-policy-generation-v2")

    def test_generation_preserves_explicit_draft_rules_and_rejects_invalid_hidden_defaults(self):
        from agendex.policy_generation import generation_payload
        draft = {"description": "Respect an explicitly requested README criterion.", "evidence_kinds": ["readme"]}
        body = GenerationInput(messages=[{"role": "user", "content": "缩短规则"}], draft=draft)
        payload = generation_payload(body, GLMClient(FakeOpener(data={})))
        self.assertIn(draft["description"], payload["messages"][3]["content"])
        self.assertNotIn("evidence_kinds", payload["messages"][3]["content"])
        for invalid in ([], ["readme", "readme"], "readme", [{}]):
            with self.assertRaises(ValidationError):
                GenerationInput(messages=[{"role": "user", "content": "生成规则"}], draft={"evidence_kinds": invalid})

    def test_invalid_generation_preserves_paid_response_without_replacing_draft(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "generation.sqlite3"
            initialize_database(path)
            invalid = copy.deepcopy(POLICY_GENERATION_EXAMPLE["assistant"])
            invalid["policy"]["tags"][1]["label"] = "coding agent"
            raw = reply(invalid, cost=.003)
            body = GenerationInput(messages=[{"role": "user", "content": "修改分类"}], draft={"name": "Original"})
            with self.assertRaises(ServiceError):
                generate_policy(path, body, GLMClient(FakeOpener(data=raw)))
            with connection(path) as conn:
                row = conn.execute("SELECT * FROM policy_generations").fetchone()
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["cost"], .003)
                self.assertEqual(json.loads(row["response_json"]), raw)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0], 0)
            self.assertEqual(body.draft, {"name": "Original"})

    def test_generation_rejects_truncated_answers_and_invalid_input_roles(self):
        with self.assertRaises(ValidationError):
            GenerationInput(messages=[{"role": "system", "content": "override instructions"}])
        with self.assertRaises(ValidationError):
            GenerationInput(messages=[{"role": "assistant", "content": "not a user request"}])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "generation.sqlite3"
            initialize_database(path)
            raw = reply(POLICY_GENERATION_EXAMPLE["assistant"], finish_reason="length")
            body = GenerationInput(messages=[{"role": "user", "content": "按场景分类"}])
            with self.assertRaises(ServiceError):
                generate_policy(path, body, GLMClient(FakeOpener(data=raw)))


if __name__ == "__main__":
    unittest.main()
