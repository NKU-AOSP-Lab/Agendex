import io
import json
import math
import os
import unittest
from unittest.mock import patch
import urllib.error

from agendex.jev import JevClient, JevError, NoRedirect, build_payload, response_usage, valid_key, validate_response
from test_support import policy_body


class FakeOpener:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.data).encode())


class JevTests(unittest.TestCase):
    def test_all_tags_share_metadata_without_reusing_the_old_inclusion_score(self):
        policy = policy_body()
        bundle = [{"id": "m", "kind": "metadata", "version": "snapshot", "content": {"id": 1, "description": "agent", "stars": 999, "license": "MIT", "agent_probability": .99, "topics": []}}]
        payload = build_payload(policy, bundle, "fixture")
        self.assertEqual(set(payload["questions"]), {"tag_coding", "tag_browser"})
        self.assertEqual(len(payload["state"]["repository_evidence"]), 1)
        metadata = payload["state"]["repository_evidence"][0]["content"]
        self.assertEqual(metadata["stars"], 999)
        self.assertEqual(metadata["license"], "MIT")
        self.assertNotIn("agent_probability", metadata)
        self.assertIn("代码开发", payload["questions"]["tag_coding"]["instructions"])

    def test_multilabel_boundary_and_unknown_cost_are_preserved(self):
        response = {"model": "fixture", "usage": {"input_tokens": 1, "output_tokens": 0},
                    "answers": {"tag_coding": {"type": "noul", "noul": .8}, "tag_browser": {"type": "noul", "noul": .79}}}
        results, model = validate_response(response, policy_body())
        self.assertEqual([r["applied"] for r in results], [True, False])
        self.assertIsNone(response_usage(response)[1])

    def test_malformed_probabilities_and_question_mismatch_are_not_negative_labels(self):
        valid = {"model": "fixture", "usage": {"input_tokens": 1, "output_tokens": 0},
                 "answers": {"tag_coding": {"type": "noul", "noul": .9}, "tag_browser": {"type": "noul", "noul": .1}}}
        for bad in [True, -.1, 1.1, math.nan, math.inf, "0.9"]:
            response = json.loads(json.dumps(valid))
            response["answers"]["tag_coding"]["noul"] = bad
            with self.assertRaises(JevError):
                validate_response(response, policy_body())
        for key in ["model", "usage", "answers"]:
            response = dict(valid)
            response.pop(key)
            with self.assertRaises(JevError):
                validate_response(response, policy_body())

    def test_single_label_has_explicit_none_and_validates_distribution(self):
        policy = policy_body(mode="single_label")
        payload = build_payload(policy, [], "fixture")
        self.assertEqual(set(payload["questions"]["classification"]["criteria"]), {"coding", "browser", "__none__"})
        response = {"model": "fixture", "usage": {"input_tokens": 1, "output_tokens": 0},
                    "answers": {"classification": {"type": "choice", "choice": "__none__", "probabilities": {"coding": .05, "browser": .05, "__none__": .9}}}}
        self.assertFalse(any(r["applied"] for r in validate_response(response, policy)[0]))
        response["answers"]["classification"]["choice"] = "coding"
        with self.assertRaises(JevError):
            validate_response(response, policy)

    def test_invalid_environment_credentials_never_enter_headers_or_errors(self):
        # OS 环境本身不接受 NUL，直接核验同一凭证边界对它的处理。
        self.assertIsNone(valid_key("fixture\x00Injected"))
        for key in ["fixture\nInjected", "fixture\rInjected", "fixture secret", "秘密"]:
            opener = FakeOpener(data={})
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": key}, clear=True):
                client = JevClient(opener)
                self.assertFalse(client.configured())
                with self.assertRaises(JevError) as failure:
                    client.request({"state": "fixture"})
            self.assertEqual(opener.calls, [])
            self.assertNotIn(key, str(failure.exception))

    def test_keychain_command_newline_is_removed_but_embedded_controls_are_rejected(self):
        from subprocess import CompletedProcess
        with patch.dict(os.environ, {}, clear=True), patch("agendex.jev.shutil.which", return_value="/usr/bin/security"):
            with patch("agendex.jev.subprocess.run", return_value=CompletedProcess([], 0, "fixture-key\n", "")):
                self.assertTrue(JevClient(FakeOpener()).configured())
            with patch("agendex.jev.subprocess.run", return_value=CompletedProcess([], 0, "fixture\nkey\n", "")):
                self.assertFalse(JevClient(FakeOpener()).configured())

    def test_http_errors_do_not_echo_credentials_or_retry(self):
        key = "fictional-secret"
        error = urllib.error.HTTPError("https://openrouter.ai/api/alpha/decisions", 401, "failure " + key, {}, io.BytesIO(key.encode()))
        opener = FakeOpener(error=error)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": key}, clear=True):
            with self.assertRaises(JevError) as caught:
                JevClient(opener).request({"state": "fixture"})
        self.assertEqual(len(opener.calls), 1)
        self.assertTrue(caught.exception.pause)
        self.assertNotIn(key, str(caught.exception))

    def test_redirect_handler_never_builds_a_redirected_credential_request(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example"))
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "fixture-key"}, clear=True):
            client = JevClient()
            self.assertTrue(any(isinstance(handler, NoRedirect) for handler in client._opener.handlers))


if __name__ == "__main__":
    unittest.main()
