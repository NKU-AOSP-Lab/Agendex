"""目录分层后的入口与默认数据路径契约；不启动服务或修改数据库。"""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


WEB = Path(__file__).resolve().parents[2]
WORKSPACE = WEB.parents[1]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkspacePathTests(unittest.TestCase):
    def setUp(self):
        original_path = sys.path[:]
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), original_path))

    def test_import_defaults_use_canonical_research_data_from_both_entry_paths(self):
        for base in (WEB, WORKSPACE / "web"):
            with self.subTest(entry=base):
                module = load_script("agendex_import_path_test", base / "scripts" / "import_dataset.py")
                args = module.parser().parse_args([])
                expected = WORKSPACE / "experiment" / "02_keyword" / "output" / "metadata-prescreen" / "scoring"
                self.assertEqual(module.PROJECT_ROOT, WORKSPACE)
                self.assertEqual(args.dataset, "fine")
                self.assertEqual(module.KEYWORD_SCORING_ROOT, expected)
                self.assertEqual(module.FINE_SCREEN_ROOT, WORKSPACE / "experiment" / "02_keyword" / "output" / "fine-screen")
                self.assertIsNone(args.source)
                self.assertIsNone(args.classifications)
                self.assertEqual(args.database, WEB / "data" / "agendex-keyword.sqlite3")

    def test_legacy_launcher_resolves_the_same_workspace_and_site(self):
        canonical = load_script("agendex_run_canonical_test", WEB / "scripts" / "run.py")
        legacy = load_script("agendex_run_legacy_test", WORKSPACE / "web" / "scripts" / "run.py")
        self.assertEqual(canonical.ROOT, WORKSPACE)
        self.assertEqual(legacy.ROOT, WORKSPACE)
        self.assertEqual(canonical.WEB, legacy.WEB)
        self.assertEqual(canonical.WEB, WEB)

    def test_default_app_import_is_isolated_even_with_an_existing_database_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            protected = Path(temporary) / "must-not-be-created.sqlite3"
            environment = {**os.environ, "AGENDEX_DB_PATH": str(protected),
                           "PYTHONPATH": os.pathsep.join([str(WEB / "backend"), str(WEB / "backend" / "tests")]),
                           "PYTHONDONTWRITEBYTECODE": "1"}
            code = "from test_support import isolated_app_factory; isolated_app_factory()"
            result = subprocess.run([sys.executable, "-c", code], env=environment,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(protected.exists())

    def test_first_run_import_uses_workspace_cwd_without_starting_a_real_server(self):
        module = load_script("agendex_run_import_cwd_test", WEB / "scripts" / "run.py")
        child = Mock()
        child.poll.return_value = 0
        child.returncode = 0
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "site.sqlite3"
            with (patch.object(sys, "argv", ["run.py", "--database", str(database)]),
                  patch.object(module, "dataset_ready", return_value=False),
                  patch.object(module, "python_executable", return_value="test-python"),
                  patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
                  patch.object(module.subprocess, "Popen", return_value=child),
                  patch.object(module.shutil, "which", return_value="test-npm"),
                  patch.object(module.signal, "signal"),
                  patch("builtins.print")):
                self.assertEqual(module.main(), 0)
            import_call = run.call_args_list[1]
            self.assertEqual(import_call.args[0][1], str(WEB / "scripts" / "import_dataset.py"))
            self.assertEqual(import_call.kwargs["cwd"], str(WORKSPACE))
            self.assertEqual(import_call.kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0], str(WEB / "backend"))
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
