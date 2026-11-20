"""Regression guardrails v146: project rules for every engine, staged-commit lock and GOLDEN coverage.

Runs only on temporary directories and pure functions: never touches live `data/hub.db`,
the running server or the real Git index.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

GUARDRAILS = ROOT_DIR / ".agents" / "rules" / "engineering_guardrails.md"
HOOK = ROOT_DIR / "tools" / "git_hooks" / "pre-commit"
VERIFY = ROOT_DIR / "tools" / "verify_hub.py"
HIDDEN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
SW_TEMPLATE = 'const CACHE_NAME = "agy-hub-v{v}";\nconst SHELL_ASSETS = ["/static/app.js?v={v}"];\n'


def _load_verify_hub():
    spec = importlib.util.spec_from_file_location("verify_hub_under_test", VERIFY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _clean_git_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("GIT_"):
            env.pop(name)
    return env


class TestProjectRulesForAllEngines(unittest.TestCase):
    def setUp(self) -> None:
        import app.claude_engine as claude_engine
        import app.codex_engine as codex_engine

        self.readers = {"claude": claude_engine._read_project_hub_rules, "codex": codex_engine._read_project_hub_rules}
        self._tmp = tempfile.TemporaryDirectory(prefix="agy_test_v146_rules_", ignore_cleanup_errors=True)
        self.proj = Path(self._tmp.name) / "proj"
        self.rules = self.proj / ".agents" / "rules"
        self.rules.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_hub_project_first_then_other_md_alphabetically(self) -> None:
        (self.rules / "hub_project.md").write_text("# PASSPORT\n", encoding="utf-8")
        (self.rules / "engineering_guardrails.md").write_text("# GUARDRAILS\n", encoding="utf-8")
        (self.rules / "a_style.md").write_text("# STYLE\n", encoding="utf-8")
        (self.rules / "notes.txt").write_text("IGNORED", encoding="utf-8")
        (self.rules / "empty.md").write_text("   \n", encoding="utf-8")
        for name, reader in self.readers.items():
            with self.subTest(engine=name):
                self.assertEqual(reader(self.proj), "# PASSPORT\n\n# STYLE\n\n# GUARDRAILS")

    def test_guardrails_without_passport_and_missing_dirs(self) -> None:
        (self.rules / "engineering_guardrails.md").write_text("# GUARDRAILS", encoding="utf-8")
        for name, reader in self.readers.items():
            with self.subTest(engine=name):
                self.assertEqual(reader(self.proj), "# GUARDRAILS")
                self.assertEqual(reader(None), "")
                self.assertEqual(reader(self.proj / "missing"), "")

    def test_combined_cap_stays_26000_and_passport_only_is_unchanged(self) -> None:
        (self.rules / "hub_project.md").write_text("R" * 30_000, encoding="utf-8")
        for reader in self.readers.values():
            self.assertEqual(reader(self.proj), "R" * 26_000)
        (self.rules / "hub_project.md").write_text("P" * 25_990, encoding="utf-8")
        (self.rules / "engineering_guardrails.md").write_text("G" * 100, encoding="utf-8")
        for reader in self.readers.values():
            text = reader(self.proj)
            self.assertEqual(len(text), 26_000)
            self.assertTrue(text.startswith("P" * 25_990 + "\n\nG"))

    def test_repository_guardrails_reach_codex_and_claude(self) -> None:
        raw = GUARDRAILS.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8")
        for marker in (
            "VERIFY_HUB_OK", "--no-verify", "GOLDEN", "Gemini High", "test_frontend_contracts.py",
            "@layer", "window.AgyUI", "CACHE_NAME", "SHELL_ASSETS", "--staged-guard", "data/hub.db",
        ):
            self.assertIn(marker, text)
        for name, reader in self.readers.items():
            with self.subTest(engine=name):
                self.assertIn("Инженерный регламент Antigravity Hub", reader(ROOT_DIR))


class TestStagedGuardPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.vh = _load_verify_hub()

    def errors(self, paths, staged_v=145, head_v=145, deleted=()):
        staged = None if staged_v is None else SW_TEMPLATE.format(v=staged_v)
        head = None if head_v is None else SW_TEMPLATE.format(v=head_v)
        return self.vh.staged_guard_errors(list(paths), list(paths) + list(deleted), staged, head)

    def test_blocks_live_data_and_secrets(self) -> None:
        for path in (
            "data/hub.db", "data/cli_homes/admin/token.json", "users/admin/uploads/a.png",
            "shared_projects/demo/x.py", "tmp/out.txt", "scratch/probe.py", "app/cache.db",
            "backup/hub.db-wal", "hub.db-journal", ".env", ".env.local", "deploy/.env.production",
            "Data\\hub.sqlite",
        ):
            with self.subTest(path=path):
                errors = self.errors([path])
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("live data or secrets", errors[0])

    def test_allows_regular_sources_example_env_and_deletions(self) -> None:
        self.assertEqual(
            self.errors(
                [".env.example", "app/database.py", "tests/test_data_paths.py", "tools/git_hooks/pre-commit",
                 ".agents/rules/engineering_guardrails.md", "docs/database.md"],
                deleted=["data/old_dump.db", ".env"],
            ),
            [],
        )

    def test_static_change_requires_cache_bump(self) -> None:
        for path in ("app/static/app.js", "app/static/styles.css", "app/static/ui-system.css",
                     "app/static/vendor/marked.min.js", "app/static/sw.js"):
            with self.subTest(path=path):
                errors = self.errors([path], staged_v=145, head_v=145)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("Cache-First PWA guard", errors[0])
                self.assertIn("v146", errors[0])
        self.assertTrue(self.errors(["app/static/app.js"], staged_v=144, head_v=145))
        self.assertTrue(self.errors(["app/static/app.js"], staged_v=None, head_v=145))

    def test_static_change_with_bump_and_backend_only_pass(self) -> None:
        self.assertEqual(self.errors(["app/static/app.js", "app/static/sw.js", "app/static/index.html"],
                                     staged_v=146, head_v=145), [])
        self.assertEqual(self.errors(["app/main.py", "tools/verify_hub.py", "tests/test_x.py"],
                                     staged_v=145, head_v=145), [])
        self.assertEqual(self.errors(["app/static/index.html", "app/static/icon.svg"],
                                     staged_v=145, head_v=145), [])
        self.assertEqual(self.errors(["app/static/app.js"], staged_v=1, head_v=None), [])

    def test_deleted_static_asset_also_requires_bump(self) -> None:
        self.assertTrue(self.errors([], deleted=["app/static/old.css"]))

    def test_golden_covers_early_subsystems_and_files_exist(self) -> None:
        golden = set(self.vh.GOLDEN)
        for name in (
            "agy_stability_and_hidden_console_v130", "multi_account_per_tab_v127", "fast_hq_return_v126",
            "project_rename_delete", "project_share_unshare", "model_advisor_and_profile",
            "proxy_and_xray_routing_v106", "frontend_contracts", "ui_full_geometry_gate_v145",
            "regression_guardrails_v146",
        ):
            self.assertIn(f"tests/test_{name}.py", golden)
        self.assertEqual(len(golden), len(self.vh.GOLDEN))
        for rel in self.vh.GOLDEN:
            self.assertTrue((ROOT_DIR / rel).is_file(), rel)

    def test_child_env_drops_hook_git_variables(self) -> None:
        old = {k: os.environ.get(k) for k in ("GIT_INDEX_FILE", "GIT_DIR")}
        os.environ["GIT_INDEX_FILE"] = "x/index.lock"
        os.environ["GIT_DIR"] = "x/.git"
        try:
            env = self.vh.child_env()
            self.assertNotIn("GIT_INDEX_FILE", env)
            self.assertNotIn("GIT_DIR", env)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class TestPreCommitHookFile(unittest.TestCase):
    def test_hook_is_portable_sh_and_calls_staged_guard(self) -> None:
        raw = HOOK.read_bytes()
        self.assertTrue(raw.startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r\n", raw)
        text = raw.decode("utf-8")
        self.assertIn(".venv/Scripts/python.exe", text)
        self.assertIn("tools/verify_hub.py --staged-guard", text)
        self.assertNotIn("--no-verify ", text.replace("`git commit --no-verify`", ""))

    def test_staged_guard_against_real_git_index(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        with tempfile.TemporaryDirectory(prefix="agy_test_v146_git_", ignore_cleanup_errors=True) as tmp:
            repo = Path(tmp)
            (repo / "tools").mkdir()
            shutil.copy2(VERIFY, repo / "tools" / "verify_hub.py")
            static = repo / "app" / "static"
            static.mkdir(parents=True)
            (static / "sw.js").write_text(SW_TEMPLATE.format(v=145), encoding="utf-8")
            (static / "app.js").write_text("console.log(1);\n", encoding="utf-8")
            env = _clean_git_env()
            git = ("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.autocrlf=false",
                   "-c", "core.hooksPath=.no-hooks")

            def sh(*args: str) -> subprocess.CompletedProcess:
                return subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", **HIDDEN)

            self.assertEqual(sh("git", "init", "-q").returncode, 0)
            sh(*git, "add", "--", "tools/verify_hub.py", "app/static/sw.js", "app/static/app.js")
            self.assertEqual(sh(*git, "commit", "-q", "-m", "init").returncode, 0)

            def guard() -> subprocess.CompletedProcess:
                return sh(sys.executable, "tools/verify_hub.py", "--staged-guard")

            (static / "app.js").write_text("console.log(2);\n", encoding="utf-8")
            sh(*git, "add", "--", "app/static/app.js")
            blocked = guard()
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("STAGED_GUARD_BLOCK: Cache-First PWA guard", blocked.stderr)

            (static / "sw.js").write_text(SW_TEMPLATE.format(v=146), encoding="utf-8")
            sh(*git, "add", "--", "app/static/sw.js")
            self.assertIn("STAGED_GUARD_OK: cache v146 (HEAD v145)", guard().stdout)

            (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
            sh(*git, "add", "-f", "--", ".env")
            secret = guard()
            self.assertNotEqual(secret.returncode, 0)
            self.assertIn("live data or secrets", secret.stderr)
            self.assertIn("HUB_DB_BEFORE: None", secret.stdout)


if __name__ == "__main__":
    unittest.main()
