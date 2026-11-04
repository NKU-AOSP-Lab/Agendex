"""Both historical layouts remain readable without recreating old directories."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "workspace_paths.py"
SPEC = importlib.util.spec_from_file_location("agendex_workspace_paths_tests", MODULE)
paths = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(paths)


class RecordedPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.patch = patch.object(paths, "WORKSPACE_ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def assert_legacy_variants(self, former, current):
        """Check absolute and relative forms from both previous layouts."""
        expected = self.root / current
        for prefix in ("", "experiment/"):
            recorded = prefix + former
            with self.subTest(recorded=recorded):
                self.assertEqual(paths.resolve_recorded_path(self.root / recorded), expected)
                self.assertEqual(paths.resolve_recorded_path(recorded, base=self.root), expected)
                with patch("os.getcwd", return_value=str(self.root)):
                    self.assertEqual(paths.resolve_recorded_path(recorded), expected)

    def test_output_roots_follow_their_research_route(self):
        for former, current in (
            ("search/output/ai-agents-demo/repositories.jsonl", "experiment/01_topic/output/ai-agents-demo/repositories.jsonl"),
            ("fetch/output/ai-agents-readmes-20261031/readmes.jsonl", "experiment/01_topic/output/ai-agents-readmes-20261031/readmes.jsonl"),
            ("search/output/v2/record.json", "experiment/02_keyword/output/v2/record.json"),
            ("search/output/awesome-seeds-20261031/manifest.json", "experiment/02_keyword/output/awesome-seeds-20261031/manifest.json"),
            ("fetch/output/pilot/content/readme.bin", "experiment/02_keyword/output/pilot/content/readme.bin"),
            ("search/other.json", "experiment/01_topic/other.json"),
            ("fetch/other.json", "experiment/02_keyword/other.json"),
        ):
            self.assert_legacy_variants(former, current)

    def test_renamed_code_and_method_paths_are_exact(self):
        for former, current in (
            ("search/01_collect_topic_repositories.py", "experiment/01_topic/01_collect_repositories.py"),
            ("search/02_classify_agent_repositories.py", "experiment/01_topic/02_classify_metadata.py"),
            ("search/03_calibrate_recall_threshold.py", "experiment/01_topic/03_calibrate_recall.py"),
            ("search/04_compare_threshold_confidence.py", "experiment/01_topic/04_compare_thresholds.py"),
            ("search/plot_jev_score_ecdf.py", "experiment/01_topic/05_plot_scores.py"),
            ("search/plot_repository_stars.py", "experiment/01_topic/06_plot_stars.py"),
            ("search/plot_repository_timeline.py", "experiment/01_topic/07_plot_timeline.py"),
            ("search/05_extract_awesome_candidates.py", "experiment/02_keyword/10_extract_awesome_candidates.py"),
            ("search/06_analyze_awesome_metadata.py", "experiment/02_keyword/11_analyze_awesome_metadata.py"),
            ("search/07_collect_readme_repositories.py", "experiment/02_keyword/01_collect_repositories.py"),
            ("search/08_verify_readme_collection.py", "experiment/02_keyword/02_verify_collection.py"),
            ("search/METHOD.md", "experiment/02_keyword/METHOD.md"),
            ("search/discovery-v2.md", "experiment/02_keyword/METHOD.md"),
            ("fetch/01_fetch_repository_readmes.py", "system/experiment_tools/fetch_readmes.py"),
            ("fetch/02_run_readme_collection.py", "system/experiment_tools/run_readme_collection.py"),
            ("fetch/03_pilot_readme_prescreen.py", "experiment/02_keyword/03_prescreen_metadata.py"),
            ("fetch/04_evaluate_readme_prescreen.py", "experiment/02_keyword/05_evaluate_prescreen.py"),
            ("fetch/05_plot_prescreen_risk_heatmap.py", "experiment/02_keyword/08_plot_prescreen_risk.py"),
            ("fetch/06_fine_screen_readmes.py", "experiment/02_keyword/04_fine_screen.py"),
            ("fetch/07_evaluate_jev_cascade.py", "experiment/02_keyword/06_evaluate_cascade.py"),
            ("fetch/08_evaluate_fine_screen_audit.py", "experiment/02_keyword/07_evaluate_reference.py"),
            ("fetch/plot_screening_tradeoff.py", "experiment/02_keyword/09_plot_screening_tradeoff.py"),
        ):
            self.assert_legacy_variants(former, current)
        self.assert_legacy_variants(
            "fetch/output/example/06_fine_screen_readmes.py",
            "experiment/02_keyword/output/example/06_fine_screen_readmes.py")
        self.assert_legacy_variants(
            "search/output/05_extract_awesome_candidates.py",
            "experiment/01_topic/output/05_extract_awesome_candidates.py")

    def test_only_fetch_root_policy_json_files_move_into_policies(self):
        for name in ("agent-policy-v2.json", "fine-screen-policy-v3-noul.json", "future-policy.json"):
            self.assert_legacy_variants("fetch/" + name, "experiment/02_keyword/policies/" + name)
        for former, current in (
            ("fetch/output/agent-policy-v2.json", "experiment/02_keyword/output/agent-policy-v2.json"),
            ("fetch/nested/agent-policy-v2.json", "experiment/02_keyword/nested/agent-policy-v2.json"),
            ("fetch/agent-policy-v2.json.backup", "experiment/02_keyword/agent-policy-v2.json.backup"),
            ("search/agent-policy-v2.json", "experiment/01_topic/agent-policy-v2.json"),
        ):
            self.assert_legacy_variants(former, current)

    def test_old_records_read_identical_bytes_without_legacy_directories(self):
        current = self.root / "experiment/01_topic/output/ai-agents-readmes-20261031/content/readme.bin"
        current.parent.mkdir(parents=True)
        current.write_bytes(b"frozen evidence")
        for prefix in ("fetch", "experiment/fetch"):
            recorded = str(self.root / prefix / "output/ai-agents-readmes-20261031/content/readme.bin")
            resolved = paths.resolve_recorded_path(recorded)
            self.assertEqual(resolved, current)
            self.assertEqual(resolved.read_bytes(), b"frozen evidence")
            self.assertIn(prefix, recorded)
        for prefix in ("search", "fetch", "experiment/search", "experiment/fetch"):
            self.assertFalse((self.root / prefix).exists())

    def test_relative_readme_uses_explicit_historical_record_directory(self):
        for base in ("fetch/output/pilot", "experiment/fetch/output/pilot"):
            result = paths.resolve_recorded_path("content/readme.bin", self.root / base)
            self.assertEqual(result, self.root / "experiment/02_keyword/output/pilot/content/readme.bin")
        result = paths.resolve_recorded_path(
            "content/readme.bin", self.root / "experiment/fetch/output/ai-agents-readmes-20261031")
        self.assertEqual(result, self.root / "experiment/01_topic/output/ai-agents-readmes-20261031/content/readme.bin")

    def test_canonical_and_unrelated_paths_are_not_relocated(self):
        for value in (self.root,
                      self.root / "experiment",
                      self.root / "experiment/01_topic/output/x.json",
                      self.root / "experiment/02_keyword/policies/agent-policy-v2.json",
                      self.root / "system/experiment_tools/fetch_readmes.py",
                      self.root / "search-results/x.json",
                      self.root / "experiment/fetch-results/x.json",
                      self.root.parent / "other-checkout/search/output/v2/x.json"):
            self.assertEqual(paths.resolve_recorded_path(value), value.resolve())

    def test_temporary_relative_paths_retain_working_directory_and_base_semantics(self):
        outside = self.root.parent / "outside-checkout"
        for former in ("search/output/v2/x.json", "experiment/fetch/agent-policy-v2.json"):
            with patch("os.getcwd", return_value=str(outside)):
                self.assertEqual(paths.resolve_recorded_path(former), outside / former)
            self.assertEqual(paths.resolve_recorded_path(former, base=outside), outside / former)

    def test_longest_prefix_matches_whole_path_components(self):
        self.assert_legacy_variants("search/output/v2/record.json", "experiment/02_keyword/output/v2/record.json")
        self.assert_legacy_variants("search/output/v2-other/record.json", "experiment/01_topic/output/v2-other/record.json")
        self.assert_legacy_variants("fetch/output/ai-agents-readmes-20261031-other/record.json", "experiment/02_keyword/output/ai-agents-readmes-20261031-other/record.json")

    def test_missing_files_stay_missing_without_destination_guessing(self):
        resolved = paths.resolve_recorded_path(self.root / "fetch/missing.json")
        self.assertEqual(resolved, self.root / "experiment/02_keyword/missing.json")
        self.assertFalse(resolved.exists())
        self.assertFalse(resolved.parent.exists())

    def test_workspace_alias_resolves_before_matching_missing_legacy_leaf(self):
        alias = self.root / "checkout-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertFalse((alias / "fetch").exists())
        for prefix in ("fetch", "experiment/fetch"):
            self.assertEqual(
                paths.resolve_recorded_path(alias / prefix / "output/ai-agents-readmes-20261031/readmes.jsonl"),
                self.root / "experiment/01_topic/output/ai-agents-readmes-20261031/readmes.jsonl")


class ActualWorkspacePathTests(unittest.TestCase):
    def test_canonical_evidence_and_code_exist_and_both_historical_layouts_resolve(self):
        workspace = MODULE.parents[1]
        for former, current in (
            ("search/output/ai-agents-demo/repositories.jsonl", "experiment/01_topic/output/ai-agents-demo/repositories.jsonl"),
            ("fetch/output/ai-agents-readmes-20261031/readmes.jsonl", "experiment/01_topic/output/ai-agents-readmes-20261031/readmes.jsonl"),
            ("fetch/fine-screen-policy-v3-noul.json", "experiment/02_keyword/policies/fine-screen-policy-v3-noul.json"),
            ("fetch/01_fetch_repository_readmes.py", "system/experiment_tools/fetch_readmes.py"),
            ("search/07_collect_readme_repositories.py", "experiment/02_keyword/01_collect_repositories.py"),
        ):
            expected = workspace / current
            with self.subTest(current=current):
                self.assertTrue(expected.is_file())
                self.assertEqual(paths.resolve_recorded_path(expected), expected)
                self.assertEqual(paths.resolve_recorded_path(workspace / former), expected)
                self.assertEqual(paths.resolve_recorded_path(workspace / "experiment" / former), expected)
        for former in ("search", "fetch", "experiment/search", "experiment/fetch"):
            self.assertFalse((workspace / former).exists())


if __name__ == "__main__":
    unittest.main()
