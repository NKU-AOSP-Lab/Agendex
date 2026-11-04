"""README 采集的离线测试；所有 HTTP 与凭据入口均使用本地替身。"""

import base64
from collections import deque
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError


SCRIPT = Path(__file__).resolve().parents[1] / "fetch_readmes.py"
SPEC = importlib.util.spec_from_file_location("agendex_readme_fetch", SCRIPT)
fetch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fetch
SPEC.loader.exec_module(fetch)


def source_row(identifier, description="说明"):
    return {"id": identifier, "full_name": "old/repository-" + str(identifier),
            "description": description}


def canonical(identifier):
    return {"id": identifier, "full_name": "current/repository-" + str(identifier),
            "private": False, "default_branch": "main"}


def readme(raw=b"# Agent\n"):
    sha = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    return {"type": "file", "path": "README.md", "size": len(raw), "sha": sha,
            "encoding": "base64", "content": base64.b64encode(raw).decode("ascii") + "\n",
            "html_url": "https://github.com/current/repository/blob/main/README.md"}


def http_error(code, headers=None, message=b"failure"):
    return HTTPError(fetch.API_ROOT + "/test", code, "模拟 HTTP 错误", headers or {}, io.BytesIO(message))


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode("utf-8"))
        self.headers = {"X-RateLimit-Remaining": "5000", "X-RateLimit-Resource": "core"}


class FixtureOpener:
    def __init__(self, values):
        self.values = deque(values)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if not self.values:
            raise AssertionError("出现未计划的 HTTP 请求")
        value = self.values.popleft()
        if isinstance(value, Exception):
            raise value
        return Response(value)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "repositories.jsonl"
        self.directory = self.root / "output"
        self.write_source([source_row(1), source_row(2, None)])
        self.key_patch = patch.object(fetch, "github_token", return_value=None)
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.sleep_patch = patch.object(fetch.time, "sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.log_patch = patch.object(fetch.LOGGER, "disabled", True)
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    def write_source(self, rows):
        self.source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def runner(self, values, limit=None, max_requests=None, token="private-token",
               max_readme_bytes=fetch.MAX_README_BYTES, reuse_source_metadata=False):
        self.directory.mkdir(exist_ok=True)
        for name in ("readmes.jsonl", "requests.jsonl"):
            (self.directory / name).touch()
        config = fetch.load_plan(self.source, None, limit, max_readme_bytes, reuse_source_metadata)
        client = fetch.GitHubClient(token, self.directory / "requests.jsonl", max_requests, 0)
        client.opener = FixtureOpener(values)
        return fetch.ReadmeFetcher(self.directory, config, client)

    def records(self):
        return [json.loads(line) for line in (self.directory / "readmes.jsonl").read_text().splitlines()]

    def summary(self):
        return json.loads((self.directory / "summary.json").read_text())

    def test_plan_includes_missing_description_and_deduplicates_ids(self):
        self.write_source([source_row(1), source_row(2, None), source_row(1)])
        config = fetch.load_plan(self.source, None, None)
        self.assertEqual(config["selected_repositories"], 2)
        self.assertEqual(config["source_without_description"], 1)
        self.assertEqual(config["missing_description_policy"], "include")
        self.assertFalse(config["selection_limited"])

    def test_resume_plan_accepts_moved_paths_without_rewriting_frozen_provenance(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        source = legacy / "repositories.jsonl"
        selection = legacy / "selection.jsonl"
        source.write_bytes(self.source.read_bytes())
        selection.write_text("1\n")
        frozen = fetch.load_plan(source, selection, None)
        original = json.dumps(frozen, sort_keys=True)
        canonical = self.root / "experiment" / "fetch-data"
        canonical.parent.mkdir()
        legacy.rename(canonical)
        legacy.symlink_to(canonical, target_is_directory=True)

        current = fetch.load_plan(source.resolve(), selection.resolve(), None)
        self.assertNotEqual(frozen["source"], current["source"])
        self.assertTrue(fetch.matches_frozen_plan(frozen, current))
        self.assertEqual(json.dumps(frozen, sort_keys=True), original)

        copied = self.root / "copied.jsonl"
        copied.write_bytes(source.read_bytes())
        self.assertFalse(fetch.matches_frozen_plan(
            frozen, fetch.load_plan(copied, selection.resolve(), None)))
        source.write_text("".join(json.dumps(row) + "\n" for row in
                                  [source_row(1, "changed description"), source_row(2)]))
        changed = fetch.load_plan(source.resolve(), selection.resolve(), None)
        self.assertFalse(fetch.matches_frozen_plan(frozen, changed))

    def test_selection_joins_ids_and_names_without_duplicates(self):
        selection = self.root / "selection.jsonl"
        selection.write_text('2\n{"repository_id": 2, "full_name": "old/repository-2"}\n'
                             '"OLD/REPOSITORY-1"\n', encoding="utf-8")
        config = fetch.load_plan(self.source, selection, 1)
        self.assertEqual([row["repository_id"] for row in config["repositories"]], [1])
        self.assertEqual(config["eligible_before_limit"], 2)
        self.assertTrue(config["selection_limited"])
        self.assertEqual(len(config["selection_sha256"]), 64)

    def test_plan_recovers_recorded_paths_after_legacy_directories_are_removed(self):
        self.root = self.root.resolve()
        legacy_source = self.root / "search/repositories.jsonl"
        legacy_selection = self.root / "fetch/selection.jsonl"
        legacy_source.parent.mkdir()
        legacy_selection.parent.mkdir()
        legacy_source.write_bytes(self.source.read_bytes())
        legacy_selection.write_text("1\n")
        frozen = fetch.load_plan(legacy_source, legacy_selection, None)
        original = json.dumps(frozen, sort_keys=True)
        (self.root / "experiment").mkdir()
        legacy_source.parent.rename(self.root / "experiment/01_topic")
        legacy_selection.parent.rename(self.root / "experiment/02_keyword")

        with patch.object(sys.modules["agendex_workspace_paths"], "WORKSPACE_ROOT", self.root):
            current = fetch.load_plan(legacy_source, legacy_selection, None)
            self.assertTrue(fetch.matches_frozen_plan(frozen, current))
            self.assertEqual(current["source"], str(self.root / "experiment/01_topic/repositories.jsonl"))
            self.assertEqual(current["selection"], str(self.root / "experiment/02_keyword/selection.jsonl"))
            self.assertEqual(json.dumps(frozen, sort_keys=True), original)
            (self.root / "experiment/02_keyword/selection.jsonl").write_text("2\n")
            self.assertFalse(fetch.matches_frozen_plan(
                frozen, fetch.load_plan(legacy_source, legacy_selection, None)))
        self.assertFalse((self.root / "search").exists())
        self.assertFalse((self.root / "fetch").exists())

    def test_selection_rejects_unknown_id_and_id_name_conflict(self):
        selection = self.root / "selection.jsonl"
        for content in ('{"id": 99}\n', '{"id": 1, "full_name": "other/repo"}\n'):
            selection.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                fetch.load_plan(self.source, selection, None)

    def test_source_rejects_conflicting_duplicate_or_invalid_identity(self):
        for rows in ([source_row(1), dict(source_row(1), full_name="different/repo")],
                     [dict(source_row(1), id=True)], [dict(source_row(1), full_name="../repo")]):
            self.write_source(rows)
            with self.assertRaises(ValueError):
                fetch.load_plan(self.source, None, None)

    def test_success_checks_canonical_name_and_saves_exact_bytes(self):
        raw = "# 自主 Agent\n".encode("utf-8")
        runner = self.runner([canonical(1), readme(raw), canonical(2), readme(b"")])
        self.assertEqual(runner.run(), 0)
        self.assertEqual(len(self.records()), 2)
        record = self.records()[0]
        self.assertEqual(record["canonical_full_name"], "current/repository-1")
        self.assertEqual((self.directory / record["content_file"]).read_bytes(), raw)
        self.assertEqual(record["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(record["text_encoding"], "utf-8")
        self.assertNotIn("commit_sha", record)
        urls = [request.full_url for request in runner.client.opener.requests]
        self.assertEqual(urls[:2], [fetch.API_ROOT + "/repositories/1",
                                   fetch.API_ROOT + "/repos/current/repository-1/readme"])
        self.assertEqual(self.summary()["status"], "plan_complete")
        self.assertTrue(self.summary()["all_selected_content_retrieved"])

    def test_non_utf8_bytes_are_preserved_and_marked(self):
        runner = self.runner([canonical(1), readme(b"\xff\xfe\x00")], limit=1)
        self.assertEqual(runner.run(), 0)
        record = self.records()[0]
        self.assertEqual(record["text_encoding"], "non_utf8")
        self.assertEqual((self.directory / record["content_file"]).read_bytes(), b"\xff\xfe\x00")
        self.assertEqual(self.summary()["status"], "selection_complete")
        self.assertFalse(self.summary()["full_source_scope_complete"])

    def test_wrong_canonical_id_never_downloads_readme(self):
        runner = self.runner([canonical(999)], limit=1)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.records()[0]["status"], "identity_mismatch")
        self.assertEqual(runner.client.request_count, 1)

    def test_private_repository_is_outside_public_dataset(self):
        metadata = dict(canonical(1), private=True)
        runner = self.runner([metadata], limit=1)
        self.assertEqual(runner.run(), 0)
        self.assertEqual(self.records()[0]["status"], "repository_not_public")
        self.assertEqual(runner.client.request_count, 1)

    def test_repository_and_readme_404_are_distinct(self):
        runner = self.runner([http_error(404), canonical(2), http_error(404)])
        self.assertEqual(runner.run(), 0)
        self.assertEqual([row["status"] for row in self.records()],
                         ["repository_unavailable", "readme_not_found"])
        self.assertFalse(self.summary()["all_selected_content_retrieved"])
        self.assertNotIn("is_agent", (self.directory / "readmes.jsonl").read_text())

    def test_unavailable_raw_content_and_oversize_do_not_claim_success(self):
        missing = readme()
        missing.pop("content")
        large = dict(readme(), size=fetch.MAX_README_BYTES + 1)
        runner = self.runner([canonical(1), missing, http_error(406), canonical(2), large])
        self.assertEqual(runner.run(), 2)
        self.assertEqual([row["status"] for row in self.records()], ["request_failed", "too_large"])
        self.assertFalse(any(row["content_complete"] for row in self.records()))
        self.assertEqual(self.summary()["pending_repositories"], 1)
        self.assertEqual(self.summary()["status"], "incomplete")

    def test_large_readme_uses_api_raw_and_verifies_exact_bytes(self):
        raw = b"# Large README\n" + b"x" * fetch.MAX_INLINE_README_BYTES
        metadata = dict(readme(raw), encoding="none", content="")
        runner = self.runner([canonical(1), metadata, raw], limit=1)
        self.assertEqual(runner.run(), 0)
        record = self.records()[0]
        self.assertEqual(record["content_transfer"], "api_raw")
        self.assertEqual(record["reported_size_bytes"], len(raw))
        self.assertEqual(record["blob_sha"], metadata["sha"])
        self.assertEqual((self.directory / record["content_file"]).read_bytes(), raw)
        request = runner.client.opener.requests[-1]
        self.assertEqual(request.full_url, fetch.API_ROOT + "/repos/current/repository-1/git/blobs/" + metadata["sha"])
        self.assertEqual(record["readme_raw_api_url"], request.full_url)
        self.assertEqual(record["readme_raw_ref_kind"], "git_blob_sha")
        self.assertNotEqual(request.full_url, record["readme_resolved_url"])
        self.assertEqual(request.get_header("Accept"), "application/vnd.github.raw+json")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
        self.assertEqual(runner.client.request_count, 3)
        events = [json.loads(line) for line in (self.directory / "requests.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["stage"], "readme_raw")

    def test_missing_base64_falls_back_to_raw_without_text_or_json_conversion(self):
        for index, metadata_change in enumerate(({"encoding": "none"}, {"content": None}, {"content": ""})):
            with self.subTest(metadata_change=metadata_change):
                self.directory = self.root / ("raw-" + str(index))
                raw = b"\xff\xfe\x00{raw is not JSON}\r\n"
                metadata = dict(readme(raw), **metadata_change)
                runner = self.runner([canonical(1), metadata, raw], limit=1)
                self.assertEqual(runner.run(), 0)
                record = self.records()[0]
                self.assertEqual(record["text_encoding"], "non_utf8")
                self.assertEqual((self.directory / record["content_file"]).read_bytes(), raw)

    def test_raw_length_or_blob_sha_mismatch_never_saves_content(self):
        for index, (raw, reason) in enumerate(((b"short", "content_length_mismatch"),
                                               (b"# Other\n", "blob_sha_mismatch"))):
            with self.subTest(reason=reason):
                self.directory = self.root / ("invalid-raw-" + str(index))
                metadata = dict(readme(b"# Agent\n"), encoding="none", content="")
                runner = self.runner([canonical(1), metadata, raw], limit=1)
                self.assertEqual(runner.run(), 2)
                record = self.records()[0]
                self.assertEqual(record["status"], "content_invalid")
                self.assertEqual(record["reason"], reason)
                self.assertEqual(record["stage"], "readme_raw")
                self.assertFalse((self.directory / "content").exists())

    def test_raw_response_and_reported_size_both_respect_frozen_limit(self):
        metadata = dict(readme(b"abcd"), encoding="none", content="")
        runner = self.runner([canonical(1), metadata, b"abcde"], limit=1, max_readme_bytes=4)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.records()[0]["status"], "too_large")
        self.assertEqual(self.records()[0]["reason"], "response_too_large")
        self.assertFalse((self.directory / "content").exists())
        self.directory = self.root / "reported-too-large"
        runner = self.runner([canonical(1), metadata], limit=1, max_readme_bytes=3)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.records()[0]["reason"], "readme_too_large")
        self.assertEqual(runner.client.request_count, 2)

    def test_raw_fallback_is_included_in_request_budget(self):
        metadata = dict(readme(), encoding="none", content="")
        runner = self.runner([canonical(1), metadata], limit=1, max_requests=2)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.summary()["reason"], "request_budget")
        self.assertEqual(self.records()[0]["stage"], "readme_raw")
        self.assertEqual(runner.client.request_count, 2)
        self.assertFalse((self.directory / "content").exists())

    def test_raw_retry_and_same_api_redirect_use_shared_request_policy(self):
        raw = b"# Agent\n"
        metadata = dict(readme(raw), encoding="none", content="")
        destination = fetch.API_ROOT + "/repos/renamed/repository-1/git/blobs/" + metadata["sha"]
        runner = self.runner([canonical(1), metadata, http_error(503),
                              http_error(302, {"Location": destination}), raw], limit=1)
        self.assertEqual(runner.run(), 0)
        self.assertEqual(runner.client.request_count, 5)
        self.assertEqual(runner.client.opener.requests[-1].full_url, destination)
        self.assertTrue(all(request.get_header("Accept") == "application/vnd.github.raw+json"
                            for request in runner.client.opener.requests[2:]))
        self.assertTrue(all(request.get_header("Authorization") == "Bearer private-token"
                            for request in runner.client.opener.requests))

    def test_raw_redirect_never_forwards_authentication_to_another_host(self):
        for index, destination in enumerate(("https://raw.githubusercontent.com/current/repo/main/README.md",
                                             "https://evil.example/README.md")):
            with self.subTest(destination=destination):
                self.directory = self.root / ("raw-redirect-" + str(index))
                metadata = dict(readme(), encoding="none", content="")
                runner = self.runner([canonical(1), metadata,
                                      http_error(302, {"Location": destination})], limit=1)
                self.assertEqual(runner.run(), 2)
                self.assertEqual(runner.client.request_count, 3)
                self.assertEqual(self.records()[0]["reason"], "unsafe_or_excessive_redirect")
                self.assertTrue(all(fetch.safe_api_url(request.full_url)
                                    for request in runner.client.opener.requests))

    def test_pinned_raw_redirect_cannot_change_blob_or_return_to_mutable_readme(self):
        raw = b"# Stable blob\n"
        metadata = dict(readme(raw), encoding="none", content="")
        destinations = (
            fetch.API_ROOT + "/repos/current/repository-1/readme",
            fetch.API_ROOT + "/repos/current/repository-1/contents/README.md?ref=main",
            fetch.API_ROOT + "/repos/current/repository-1/git/blobs/" + "0" * 40,
        )
        for index, destination in enumerate(destinations):
            with self.subTest(destination=destination):
                self.directory = self.root / ("changed-blob-" + str(index))
                runner = self.runner([canonical(1), metadata,
                                      http_error(302, {"Location": destination})], limit=1)
                self.assertEqual(runner.run(), 2)
                self.assertEqual(runner.client.request_count, 3)
                self.assertEqual(self.records()[0]["reason"], "unsafe_or_excessive_redirect")
                self.assertFalse((self.directory / "content").exists())

    def test_raw_blob_address_is_built_from_verified_name_and_sha_not_download_url(self):
        raw = b"# Stable blob\n"
        metadata = dict(readme(raw), encoding="none", content="",
                        download_url="https://evil.example/stolen-readme",
                        git_url="https://evil.example/git/blobs/" + "0" * 40)
        runner = self.runner([canonical(1), metadata, raw], limit=1)
        self.assertEqual(runner.run(), 0)
        expected = fetch.API_ROOT + "/repos/current/repository-1/git/blobs/" + metadata["sha"]
        self.assertEqual(runner.client.opener.requests[-1].full_url, expected)
        self.assertTrue(all(fetch.safe_api_url(request.full_url)
                            for request in runner.client.opener.requests))

    def test_blob_length_and_base64_validation(self):
        cases = [(dict(readme(), sha="0" * 40), "blob_sha_mismatch"),
                 (dict(readme(), size=999), "content_length_mismatch"),
                 (dict(readme(), content="@@"), "invalid_base64")]
        for value, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(fetch.RequestFailure) as captured:
                    fetch.decode_readme(value)
                self.assertEqual(captured.exception.category, reason)

    def test_bad_content_is_saved_as_incomplete_and_retried_on_resume(self):
        runner = self.runner([canonical(1), dict(readme(), sha="0" * 40)], limit=1)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.records()[0]["status"], "content_invalid")
        resumed = self.runner([canonical(1), readme()], limit=1)
        self.assertEqual(resumed.run(), 0)
        self.assertEqual(self.records()[-1]["status"], "ok")
        self.assertEqual(self.summary()["status_counts"]["content_invalid"], 0)

    def test_budget_includes_metadata_and_resume_skips_finished_repository(self):
        runner = self.runner([canonical(1), readme()], max_requests=2)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.summary()["reason"], "request_budget")
        self.assertEqual(self.summary()["requests_this_invocation"], 2)
        resumed = self.runner([canonical(2), readme()])
        self.assertEqual(resumed.run(), 0)
        self.assertEqual(resumed.client.request_count, 2)
        self.assertEqual([row["repository_id"] for row in self.records() if row["status"] == "ok"], [1, 2])
        self.assertEqual(self.summary()["requests_total"], 4)

    def test_401_or_ordinary_403_pauses_entire_queue_and_hides_response(self):
        for code in (401, 403):
            with self.subTest(code=code):
                directory = self.root / str(code)
                self.directory = directory
                runner = self.runner([http_error(code, message=b"private-token should not be logged")])
                self.assertEqual(runner.run(), 2)
                self.assertEqual(runner.client.request_count, 1)
                self.assertEqual(self.summary()["reason"], "authentication_or_permission")
                self.assertEqual(len(self.records()), 1)
                self.assertNotIn("private-token", (directory / "requests.jsonl").read_text())

    def test_long_rate_limit_pauses_instead_of_sleeping(self):
        runner = self.runner([http_error(429, {"Retry-After": "120"})], limit=1)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.summary()["reason"], "rate_limited")
        fetch.time.sleep.assert_not_called()

    def test_http_error_logs_only_allowlisted_rate_limit_headers(self):
        headers = {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "0",
                   "X-RateLimit-Reset": "2000", "X-RateLimit-Resource": "core",
                   "Retry-After": "120", "Authorization": "private-token",
                   "Set-Cookie": "private-token"}
        with patch.object(fetch.time, "time", return_value=1000):
            runner = self.runner([http_error(403, headers, b"private-token rate limit")], limit=1)
            self.assertEqual(runner.run(), 2)
        log = (self.directory / "requests.jsonl").read_text()
        event = json.loads(log)
        self.assertEqual(event["error_type"], "rate_limited")
        for key in fetch.RATE_LIMIT_HEADERS:
            self.assertEqual(event[key.lower()], headers[key])
        self.assertNotIn("private-token", log)
        self.assertNotIn("authorization", log)
        self.assertNotIn("set-cookie", log)
        fetch.time.sleep.assert_not_called()

    def test_short_rate_limit_and_5xx_retry_within_budget(self):
        runner = self.runner([http_error(403, {"Retry-After": "2"}), canonical(1),
                              http_error(503), readme()], limit=1)
        self.assertEqual(runner.run(), 0)
        self.assertEqual(runner.client.request_count, 4)
        self.assertIn(unittest.mock.call(2.0), fetch.time.sleep.call_args_list)

    def test_network_failure_is_bounded_and_not_a_negative_agent_label(self):
        runner = self.runner([URLError("private-token") for _ in range(fetch.MAX_ATTEMPTS)], limit=1)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(runner.client.request_count, fetch.MAX_ATTEMPTS)
        self.assertEqual(self.records()[0]["reason"], "network_error")
        self.assertEqual(self.records()[0]["status"], "request_failed")
        self.assertNotIn("private-token", (self.directory / "requests.jsonl").read_text())

    def test_continuous_network_failure_pauses_before_scanning_entire_queue(self):
        threshold = fetch.MAX_CONSECUTIVE_NETWORK_FAILURES
        self.write_source([source_row(identifier) for identifier in range(1, threshold + 4)])
        failures = [URLError("offline") for _ in range(threshold * fetch.MAX_ATTEMPTS)]
        runner = self.runner(failures)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.summary()["status"], "paused")
        self.assertEqual(self.summary()["reason"], "network_unavailable")
        self.assertEqual(len(self.records()), threshold)
        self.assertEqual(runner.client.request_count, threshold * fetch.MAX_ATTEMPTS)
        self.assertTrue(all(row["status"] == "request_failed" and row["reason"] == "network_error"
                            for row in self.records()))

    def test_success_404_and_5xx_reset_network_failure_streak(self):
        self.write_source([source_row(identifier) for identifier in range(1, 8)])
        network = lambda: [URLError("offline") for _ in range(fetch.MAX_ATTEMPTS)]
        values = (network() + [http_error(503) for _ in range(fetch.MAX_ATTEMPTS)]
                  + network() + [http_error(404)] + network() + [canonical(6), readme()] + network())
        with patch.object(fetch, "MAX_CONSECUTIVE_NETWORK_FAILURES", 2):
            runner = self.runner(values)
            self.assertEqual(runner.run(), 2)
        self.assertEqual(self.summary()["status"], "incomplete")
        self.assertIsNone(self.summary()["reason"])
        self.assertEqual(len(self.records()), 7)

    def test_invalid_json_retries_and_response_size_is_bounded(self):
        runner = self.runner([b"not-json"] * fetch.MAX_ATTEMPTS, limit=1)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.records()[0]["reason"], "invalid_response")
        self.directory = self.root / "oversize"
        runner = self.runner([b"x" * (fetch.MAX_RESPONSE_BYTES + 1)], limit=1)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.records()[0]["status"], "too_large")

    def test_safe_api_redirect_is_counted_and_preserves_auth_only_inside_api(self):
        location = fetch.API_ROOT + "/repositories/1?canonical=1"
        runner = self.runner([http_error(301, {"Location": location}), canonical(1), readme()], limit=1)
        self.assertEqual(runner.run(), 0)
        self.assertEqual(runner.client.request_count, 3)
        self.assertEqual(runner.client.opener.requests[1].full_url, location)
        self.assertTrue(all(request.get_header("Authorization") == "Bearer private-token"
                            for request in runner.client.opener.requests))
        self.assertNotIn("private-token", (self.directory / "requests.jsonl").read_text())

    def test_unsafe_redirect_never_sends_a_second_request(self):
        locations = ["https://evil.example/readme", "http://api.github.com/readme",
                     "https://api.github.com.evil.example/readme", "https://user@api.github.com/readme"]
        for number, location in enumerate(locations):
            with self.subTest(location=location):
                self.directory = self.root / ("redirect-" + str(number))
                runner = self.runner([http_error(302, {"Location": location})], limit=1)
                self.assertEqual(runner.run(), 2)
                self.assertEqual(runner.client.request_count, 1)
                self.assertEqual(len(runner.client.opener.requests), 1)

    def test_redirects_are_part_of_request_budget(self):
        runner = self.runner([http_error(301, {"Location": fetch.API_ROOT + "/canonical"})],
                             limit=1, max_requests=1)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.summary()["reason"], "request_budget")
        self.assertEqual(runner.client.request_count, 1)

    def test_resume_verifies_saved_file_and_never_refetches_success(self):
        runner = self.runner([canonical(1), readme()], limit=1)
        self.assertEqual(runner.run(), 0)
        resumed = self.runner([], limit=1)
        self.assertEqual(resumed.run(), 0)
        self.assertEqual(resumed.client.request_count, 0)
        record = self.records()[0]
        (self.directory / record["content_file"]).write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "原文缺失或损坏"):
            self.runner([], limit=1)

    def test_resume_repairs_only_incomplete_final_journal_line(self):
        runner = self.runner([canonical(1), readme()], limit=1)
        self.assertEqual(runner.run(), 0)
        with (self.directory / "readmes.jsonl").open("ab") as stream:
            stream.write(b'{"repository_id":')
        resumed = self.runner([], limit=1)
        self.assertEqual(resumed.run(), 0)
        self.assertEqual(len(self.records()), 1)
        with (self.directory / "readmes.jsonl").open("ab") as stream:
            stream.write(b"broken\n{}\n")
        with self.assertRaisesRegex(ValueError, "中部"):
            self.runner([], limit=1)

    def test_resume_rejects_missing_content_and_escape_path(self):
        runner = self.runner([canonical(1), readme()], limit=1)
        self.assertEqual(runner.run(), 0)
        record = self.records()[0]
        (self.directory / record["content_file"]).unlink()
        with self.assertRaises(ValueError):
            self.runner([], limit=1)
        record["content_file"] = "../outside.bin"
        (self.directory / "readmes.jsonl").write_text(json.dumps(record) + "\n")
        with self.assertRaisesRegex(ValueError, "超出"):
            self.runner([], limit=1)

    def test_lock_prevents_two_writers(self):
        self.directory.mkdir()
        with fetch.output_lock(self.directory):
            with self.assertRaisesRegex(ValueError, "另一个"):
                with fetch.output_lock(self.directory):
                    pass
        with fetch.output_lock(self.directory):
            pass

    def test_main_freezes_input_and_does_not_read_credentials_for_invalid_resume(self):
        opener = FixtureOpener([canonical(1), readme()])
        arguments = [str(self.source), "--limit", "1", "--output-dir", str(self.directory), "--interval", "0"]
        with patch.object(fetch, "build_opener", return_value=opener):
            self.assertEqual(fetch.main(arguments), 0)
        self.write_source([source_row(1, "源输入变化")])
        with patch.object(fetch, "github_token", side_effect=AssertionError("不应读取凭据")):
            self.assertEqual(fetch.main(arguments + ["--resume"]), 1)

    def test_main_frozen_selection_and_limit_must_match_on_resume(self):
        selection = self.root / "selected.jsonl"
        selection.write_text('1\n')
        arguments = [str(self.source), "--selection", str(selection), "--limit", "1",
                     "--output-dir", str(self.directory), "--interval", "0"]
        with patch.object(fetch, "build_opener", return_value=FixtureOpener([canonical(1), readme()])):
            self.assertEqual(fetch.main(arguments), 0)
        selection.write_text('2\n')
        self.assertEqual(fetch.main(arguments + ["--resume"]), 1)

    def test_main_resume_preserves_legacy_one_mib_limit(self):
        arguments = [str(self.source), "--limit", "1", "--output-dir", str(self.directory), "--interval", "0"]
        with patch.object(fetch, "build_opener", return_value=FixtureOpener([canonical(1), readme()])):
            self.assertEqual(fetch.main(arguments + ["--max-readme-bytes", str(1024 * 1024)]), 0)
        config_path = self.directory / "config.json"
        frozen = config_path.read_bytes()
        self.assertEqual(json.loads(frozen)["max_readme_bytes"], 1024 * 1024)
        with patch.object(fetch, "build_opener", return_value=FixtureOpener([])):
            self.assertEqual(fetch.main(arguments + ["--resume"]), 0)
        self.assertEqual(config_path.read_bytes(), frozen)
        with patch.object(fetch, "github_token", side_effect=AssertionError("不应读取凭据")):
            self.assertEqual(fetch.main(arguments + ["--resume", "--max-readme-bytes",
                                                     str(fetch.MAX_README_BYTES)]), 1)

    def test_new_plan_defaults_to_100_mib_and_rejects_larger_limit(self):
        self.assertEqual(fetch.load_plan(self.source, None, None)["max_readme_bytes"], 100 * 1024 * 1024)
        for limit in (True, 0, fetch.MAX_README_BYTES + 1):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    fetch.load_plan(self.source, None, None, limit)

    def complete_source(self, identifier=1):
        return dict(source_row(identifier), private=False, default_branch="main")

    def named_readme(self, name="old/repository-1", raw=b"# Agent\n"):
        data = readme(raw)
        data.update(html_url="https://github.com/" + name + "/blob/main/README.md",
                    url=fetch.API_ROOT + "/repos/" + name + "/contents/README.md",
                    git_url=fetch.API_ROOT + "/repos/" + name + "/git/blobs/" + data["sha"])
        return data

    def test_fast_path_fetches_exact_readme_with_one_request_and_snapshot_identity(self):
        self.write_source([self.complete_source()])
        raw = "# 自有 Agent\r\n".encode("utf-8")
        runner = self.runner([self.named_readme(raw=raw)], reuse_source_metadata=True)
        self.assertEqual(runner.run(), 0)
        self.assertEqual(runner.client.request_count, 1)
        record = self.records()[0]
        self.assertEqual(record["requests_this_repository"], 1)
        self.assertEqual(record["repository_identity_source"], "source_snapshot")
        self.assertFalse(record["repository_identity_verified_this_run"])
        self.assertEqual(record["source_metadata_sha256"], runner.config["source_sha256"])
        self.assertNotIn("repository_checked_at", record)
        self.assertNotIn("repository_api_url", record)
        self.assertEqual((self.directory / record["content_file"]).read_bytes(), raw)
        self.assertEqual(record["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(record["blob_sha"], self.named_readme(raw=raw)["sha"])
        self.assertEqual(runner.client.opener.requests[0].full_url,
                         fetch.API_ROOT + "/repos/old/repository-1/readme")

    def test_fast_path_404_checks_id_then_recovers_renamed_repository(self):
        self.write_source([self.complete_source()])
        runner = self.runner([http_error(404), canonical(1), self.named_readme("current/repository-1")],
                             reuse_source_metadata=True)
        self.assertEqual(runner.run(), 0)
        record = self.records()[0]
        self.assertEqual(record["metadata_fallback_reason"], "readme_not_found_at_snapshot_name")
        self.assertEqual(record["repository_identity_source"], "repository_api")
        self.assertTrue(record["repository_identity_verified_this_run"])
        self.assertEqual(record["canonical_full_name"], "current/repository-1")
        self.assertIn("repository_checked_at", record)
        self.assertEqual(record["requests_this_repository"], 3)
        self.assertEqual([r.full_url for r in runner.client.opener.requests], [
            fetch.API_ROOT + "/repos/old/repository-1/readme",
            fetch.API_ROOT + "/repositories/1",
            fetch.API_ROOT + "/repos/current/repository-1/readme"])

    def test_fast_path_cross_name_redirect_triggers_identity_check_and_refetch(self):
        self.write_source([self.complete_source()])
        target = fetch.API_ROOT + "/repos/current/repository-1/readme"
        runner = self.runner([http_error(301, {"Location": target}),
                              self.named_readme("current/repository-1"), canonical(1),
                              self.named_readme("current/repository-1")], reuse_source_metadata=True)
        self.assertEqual(runner.run(), 0)
        record = self.records()[0]
        self.assertEqual(record["requests_this_repository"], 4)
        self.assertEqual(record["metadata_fallback_reason"], "readme_repository_redirect_or_mismatch")
        self.assertTrue(record["repository_identity_verified_this_run"])

    def test_fast_path_metadata_address_mismatch_is_not_saved_under_unchecked_id(self):
        self.write_source([self.complete_source()])
        runner = self.runner([self.named_readme("unrelated/repo"), canonical(1),
                              self.named_readme("unrelated/repo")], reuse_source_metadata=True)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.records()[0]["status"], "identity_mismatch")
        self.assertEqual(self.records()[0]["requests_this_repository"], 3)
        self.assertFalse((self.directory / "content").exists())

    def test_fast_path_id_mismatch_during_404_fallback_never_saves_content(self):
        self.write_source([self.complete_source()])
        runner = self.runner([http_error(404), canonical(99)], reuse_source_metadata=True)
        self.assertEqual(runner.run(), 2)
        record = self.records()[0]
        self.assertEqual(record["status"], "identity_mismatch")
        self.assertEqual(record["reason"], "canonical_repository_id_mismatch")
        self.assertEqual(record["requests_this_repository"], 2)
        self.assertFalse((self.directory / "content").exists())

    def test_fast_path_confirmed_readme_missing_is_distinct_from_repository_unavailable(self):
        for index, (values, expected_status) in enumerate((
                ([http_error(404), canonical(1), http_error(404)], "readme_not_found"),
                ([http_error(404), http_error(404)], "repository_unavailable"))):
            with self.subTest(status=expected_status):
                self.directory = self.root / ("fast-missing-" + str(index))
                self.write_source([self.complete_source()])
                runner = self.runner(values, reuse_source_metadata=True)
                self.assertEqual(runner.run(), 0)
                record = self.records()[0]
                self.assertEqual(record["status"], expected_status)
                self.assertNotIn("is_agent", record)
                self.assertNotIn("content_file", record)
                self.assertEqual(record["requests_this_repository"], len(values))

    def test_fast_path_private_repository_discovered_on_fallback_is_not_fetched(self):
        self.write_source([self.complete_source()])
        repository = dict(canonical(1), private=True)
        runner = self.runner([http_error(404), repository], reuse_source_metadata=True)
        self.assertEqual(runner.run(), 0)
        self.assertEqual(self.records()[0]["status"], "repository_not_public")
        self.assertEqual(runner.client.request_count, 2)
        self.assertFalse((self.directory / "content").exists())

    def test_fast_path_missing_source_fields_safely_uses_legacy_identity_lookup(self):
        for index, source in enumerate((source_row(1), dict(source_row(1), private=False))):
            with self.subTest(source=source):
                self.directory = self.root / ("fast-incomplete-" + str(index))
                self.write_source([source])
                runner = self.runner([canonical(1), readme()], reuse_source_metadata=True)
                self.assertEqual(runner.run(), 0)
                record = self.records()[0]
                self.assertEqual(record["metadata_fallback_reason"], "source_metadata_incomplete")
                self.assertEqual(record["repository_identity_source"], "repository_api")
                self.assertEqual(record["requests_this_repository"], 2)

    def test_fast_path_rejects_private_or_invalid_source_metadata_before_network(self):
        cases = ({"private": True}, {"private": "false"}, {"default_branch": "main\n"},
                 {"default_branch": False})
        for change in cases:
            with self.subTest(change=change):
                self.write_source([dict(self.complete_source(), **change)])
                with self.assertRaises(ValueError):
                    fetch.load_plan(self.source, None, None, reuse_source_metadata=True)

    def test_fast_path_large_readme_fallback_preserves_bytes_in_two_requests(self):
        self.write_source([self.complete_source()])
        raw = b"# Complete README\r\n"
        metadata = dict(self.named_readme(raw=raw), encoding="none", content="")
        runner = self.runner([metadata, raw], reuse_source_metadata=True)
        self.assertEqual(runner.run(), 0)
        record = self.records()[0]
        self.assertEqual(record["requests_this_repository"], 2)
        self.assertEqual(record["content_transfer"], "api_raw")
        self.assertEqual((self.directory / record["content_file"]).read_bytes(), raw)
        self.assertEqual(runner.client.opener.requests[-1].full_url,
                         fetch.API_ROOT + "/repos/old/repository-1/git/blobs/" + metadata["sha"])

    def test_fast_mode_is_frozen_and_resume_inherits_it_without_refetching(self):
        self.write_source([self.complete_source()])
        arguments = [str(self.source), "--output-dir", str(self.directory), "--interval", "0"]
        opener = FixtureOpener([self.named_readme()])
        with patch.object(fetch, "build_opener", return_value=opener):
            self.assertEqual(fetch.main(arguments + ["--reuse-source-metadata"]), 0)
        config_path = self.directory / "config.json"
        frozen = config_path.read_bytes()
        self.assertTrue(json.loads(frozen)["reuse_source_metadata"])
        with patch.object(fetch, "build_opener", return_value=FixtureOpener([])):
            self.assertEqual(fetch.main(arguments + ["--resume"]), 0)
        self.assertEqual(config_path.read_bytes(), frozen)
        self.assertEqual(len(self.records()), 1)
        with patch.object(fetch, "github_token", side_effect=AssertionError("不应读取凭据")):
            self.assertEqual(fetch.main(arguments + ["--resume", "--no-reuse-source-metadata"]), 1)

    def test_legacy_config_without_mode_stays_unchanged_and_rejects_fast_resume(self):
        arguments = [str(self.source), "--limit", "1", "--output-dir", str(self.directory), "--interval", "0"]
        with patch.object(fetch, "build_opener", return_value=FixtureOpener([canonical(1), readme()])):
            self.assertEqual(fetch.main(arguments), 0)
        config_path = self.directory / "config.json"
        frozen = config_path.read_bytes()
        self.assertNotIn("reuse_source_metadata", json.loads(frozen))
        with patch.object(fetch, "build_opener", return_value=FixtureOpener([])):
            self.assertEqual(fetch.main(arguments + ["--resume"]), 0)
        self.assertEqual(config_path.read_bytes(), frozen)
        with patch.object(fetch, "github_token", side_effect=AssertionError("不应读取凭据")):
            self.assertEqual(fetch.main(arguments + ["--resume", "--reuse-source-metadata"]), 1)


class TokenTests(unittest.TestCase):
    def test_environment_priority_does_not_touch_keychain(self):
        with patch.dict(fetch.os.environ, {"GITHUB_TOKEN": "first", "GH_TOKEN": "second"}, clear=True), \
                patch.object(fetch.GITHUB.subprocess, "run", side_effect=AssertionError("不应读取钥匙串")):
            self.assertEqual(fetch.github_token(), "first")

    def test_keychain_token_is_not_in_argv(self):
        with patch.dict(fetch.os.environ, {}, clear=True), patch.object(fetch.sys, "platform", "darwin"), \
                patch.object(fetch.GITHUB.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout="private-token\n")) as call:
            self.assertEqual(fetch.github_token(), "private-token")
        self.assertNotIn("private-token", repr(call.call_args))
        self.assertEqual(call.call_args.args[0][-1], "-w")

    def test_keychain_timeout_is_not_a_secret_error(self):
        with patch.dict(fetch.os.environ, {}, clear=True), patch.object(fetch.sys, "platform", "darwin"), \
                patch.object(fetch.GITHUB.subprocess, "run", side_effect=subprocess.TimeoutExpired("security", 30)):
            self.assertIsNone(fetch.github_token())


if __name__ == "__main__":
    unittest.main()
