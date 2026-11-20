"""Regression suite v148: Default project guardrails across all 3 engines + Git checkpoints, diff, rollback, pre-commit shield & RBAC.

Runs strictly inside temporary directories and temporary SQLite fixtures — never touches live `data/hub.db`,
`users/`, `shared_projects/`, `.agents/rules/hub_project.md`, or running services.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = _TESTS_DIR.parent
for _p in (str(ROOT_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _clean_git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("GIT_"):
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["ORT_DISABLE_ALL_GPU"] = "1"
    return env


async def _asgi_request(
    app: Any,
    method: str,
    url: str,
    *,
    cookie_header: str = "",
    json_body: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Invoke FastAPI ASGI app directly in-memory without sockets or external HTTP clients."""
    parsed = urlsplit(url)
    raw_body = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
    headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]
    if cookie_header:
        headers.append((b"cookie", cookie_header.encode("utf-8")))
    if json_body is not None:
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(raw_body)).encode("ascii")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": parsed.path,
        "raw_path": parsed.path.encode("utf-8"),
        "query_string": parsed.query.encode("utf-8"),
        "headers": headers,
        "client": ("127.0.0.1", 51234),
        "server": ("testserver", 80),
    }

    sent_request = False
    status_code = 500
    body_chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
        elif message["type"] == "http.response.body":
            body_chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    raw_resp = b"".join(body_chunks).decode("utf-8", errors="replace")
    data = json.loads(raw_resp) if raw_resp.strip() else {}
    return status_code, data


def test_default_project_guardrails_across_all_three_engines_and_dedup(tmp_path: Path) -> None:
    from app import agy_engine, claude_engine, codex_engine, config

    # 1. Canonical guardrail block is concise (~180-250 words) and covers all 4 pillars
    guardrails_text = config.DEFAULT_PROJECT_GUARDRAILS
    words = [w for w in guardrails_text.split() if w.strip()]
    assert 150 <= len(words) <= 260, f"Unexpected word count: {len(words)}"
    for marker in (
        "grep_search",
        "py_compile",
        "node --check",
        "verify_project.py",
        "*.session",
        "git add .",
        "!important",
    ):
        assert marker in guardrails_text

    # 2. Regular project without `.agents/rules/engineering_guardrails.md` receives default guardrails in all 3 engines
    plain_proj = tmp_path / "PlainBotProject"
    plain_proj.mkdir()

    for mode in ("write_files", "full_access"):
        agy_prompt = agy_engine._augment_prompt_for_mode(
            kind="project",
            prompt="Обнови обработчик вебхуков",
            attachments=[],
            is_first_turn=True,
            access_mode=mode,
            project_dir=plain_proj,
        )
        assert guardrails_text in agy_prompt
        assert agy_prompt.count("[Базовый инженерный стандарт проекта") == 1

        claude_rules = claude_engine._read_project_hub_rules(plain_proj, access_mode=mode)
        codex_rules = codex_engine._read_project_hub_rules(plain_proj, access_mode=mode)
        assert guardrails_text in claude_rules
        assert guardrails_text in codex_rules

    # Default 1-arg call `_read_project_hub_rules(plain_proj)` also injects default guardrails
    assert guardrails_text in claude_engine._read_project_hub_rules(plain_proj)
    assert guardrails_text in codex_engine._read_project_hub_rules(plain_proj)

    # Plan (`read_only`), regular chat, and live_voice do NOT inject write/commit guardrails
    plan_prompt = agy_engine._augment_prompt_for_mode(
        kind="project",
        prompt="Составь план рефакторинга",
        attachments=[],
        is_first_turn=True,
        access_mode="read_only",
        project_dir=plain_proj,
    )
    assert guardrails_text not in plan_prompt
    assert claude_engine._read_project_hub_rules(plain_proj, access_mode="read_only") == ""
    assert codex_engine._read_project_hub_rules(plain_proj, access_mode="read_only") == ""

    chat_prompt = agy_engine._augment_prompt_for_mode(
        kind="chat",
        prompt="Привет",
        attachments=[],
        is_first_turn=True,
        access_mode="read_only",
    )
    assert guardrails_text not in chat_prompt

    voice_prompt = agy_engine._augment_prompt_for_mode(
        kind="project",
        prompt="Ответь голосом",
        attachments=[],
        is_first_turn=True,
        access_mode="full_access",
        live_voice=True,
        project_dir=plain_proj,
    )
    assert guardrails_text not in voice_prompt

    # Project with `hub_project.md` (passport) but NO `engineering_guardrails.md` combines passport + default guardrails
    rules_dir = plain_proj / ".agents" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "hub_project.md").write_text("# Паспорт PlainBot\nСтек: FastAPI + aiogram", encoding="utf-8")
    combined_claude = claude_engine._read_project_hub_rules(plain_proj)
    combined_codex = codex_engine._read_project_hub_rules(plain_proj)
    assert combined_claude.startswith("# Паспорт PlainBot")
    assert guardrails_text in combined_claude
    assert combined_codex == combined_claude

    # 3. Smart deduplication when `engineering_guardrails.md` already exists in `.agents/rules/`
    (rules_dir / "engineering_guardrails.md").write_text("# Кастомный регламент проекта\nСТРОГИЙ ГЕЙТ", encoding="utf-8")
    agy_dedup = agy_engine._augment_prompt_for_mode(
        kind="project",
        prompt="Сделай правку",
        attachments=[],
        is_first_turn=True,
        access_mode="full_access",
        project_dir=plain_proj,
    )
    assert guardrails_text not in agy_dedup

    for reader in (claude_engine._read_project_hub_rules, codex_engine._read_project_hub_rules):
        dedup_rules = reader(plain_proj)
        assert "# Кастомный регламент проекта" in dedup_rules
        assert guardrails_text not in dedup_rules
        # Also verify live Antigravity_Hub root (`ROOT_DIR`) does not duplicate default guardrails
        hub_rules = reader(ROOT_DIR)
        assert "Инженерный регламент Antigravity Hub" in hub_rules
        assert guardrails_text not in hub_rules

    # 4. Optional per-project disable switch suppresses injection cleanly
    opt_out_proj = tmp_path / "OptOutProject"
    opt_out_proj.mkdir()
    assert (
        agy_engine._augment_prompt_for_mode(
            kind="project",
            prompt="Тест",
            attachments=[],
            is_first_turn=False,
            access_mode="write_files",
            project_dir=opt_out_proj,
            disable_default_guardrails=True,
        ).count("[Базовый инженерный стандарт проекта")
        == 0
    )
    assert claude_engine._read_project_hub_rules(opt_out_proj, disable_default_guardrails=True) == ""
    assert codex_engine._read_project_hub_rules(opt_out_proj, project_memory={"disable_default_guardrails": True}) == ""


def test_git_service_lifecycle_filtering_syntax_diff_and_rollback(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git CLI is not installed")

    from app import config, git_service

    proj = tmp_path / "DemoService"
    proj.mkdir()

    # Uninitialized status (0 commits, no .git)
    st0 = git_service.get_project_git_status(proj)
    assert st0["initialized"] is False
    assert st0["checkpoints"] == []
    assert st0["shield_installed"] is False
    assert st0["shield_kind"] == "none"

    # 1. Init creates `.git` and safe `.gitignore` without failing on 0 commits
    init_res = git_service.ensure_project_git_ready(proj)
    assert init_res["ok"] is True
    assert init_res["initialized"] is True
    assert init_res["gitignore_created"] is True
    gitignore_file = proj / ".gitignore"
    assert gitignore_file.is_file()
    orig_gitignore = gitignore_file.read_text(encoding="utf-8")
    assert ".env" in orig_gitignore and "!.env.example" in orig_gitignore and "*.session" in orig_gitignore

    # Empty repo status (0 commits) works without error
    st_empty = git_service.get_project_git_status(proj)
    assert st_empty["initialized"] is True
    assert st_empty["head_sha"] == ""
    assert st_empty["checkpoints"] == []
    assert any(item["path"] == ".gitignore" for item in st_empty["changed_files"])

    # Existing custom `.gitignore` is never overwritten
    custom_gi = orig_gitignore + "\n# custom-user-rule\ncustom_cache/\n"
    gitignore_file.write_text(custom_gi, encoding="utf-8")
    init_again = git_service.ensure_project_git_ready(proj)
    assert init_again["gitignore_created"] is False
    assert gitignore_file.read_text(encoding="utf-8") == custom_gi

    # 2. Populate allowed code files AND forbidden secret/DB/session files
    (proj / "main.py").write_text("def greet(name: str) -> str:\n    return f'Hello, {name}!'\n", encoding="utf-8")
    (proj / "utils.js").write_text("function sum(a, b) { return a + b; }\n", encoding="utf-8")
    (proj / ".env.example").write_text("API_TOKEN=changeme\n", encoding="utf-8")

    # Forbidden files that must NEVER enter checkpoints or diffs
    (proj / ".env").write_text("SUPER_SECRET_TOKEN=top_secret_value_123\n", encoding="utf-8")
    (proj / ".env.local").write_text("LOCAL_KEY=abc\n", encoding="utf-8")
    (proj / "secret.db").write_bytes(b"SQLite format 3\x00secret")
    (proj / "bot.session").write_bytes(b"telethon_session_payload")
    (proj / "data").mkdir()
    (proj / "data" / "runtime.json").write_text('{"live": true}\n', encoding="utf-8")
    (proj / ".agents" / "rules").mkdir(parents=True)
    (proj / ".agents" / "rules" / "hub_project.md").write_text("# Auto passport\n", encoding="utf-8")

    # Even if someone force-staged `.env` manually, `create_project_checkpoint` must unstage and exclude it
    subprocess.run(
        ["git", "add", "-f", "--", ".env"],
        cwd=proj,
        env=_clean_git_env(),
        check=True,
        **config.subprocess_hidden_kwargs(),
    )

    st_dirty = git_service.get_project_git_status(proj)
    changed_paths = {f["path"] for f in st_dirty["changed_files"]}
    assert changed_paths == {".gitignore", ".env.example", "main.py", "utils.js"}
    assert st_dirty["ignored_or_blocked_count"] >= 4

    # Working tree diff previews safe files and never leaks `.env`
    wt_diff = git_service.get_project_git_diff(proj)
    assert "def greet" in wt_diff["diff"]
    assert "top_secret_value_123" not in wt_diff["diff"]
    blocked_diff = git_service.get_project_git_diff(proj, path=".env")
    assert blocked_diff["diff"] == ""
    assert blocked_diff.get("blocked") is True

    # Create first checkpoint (auto-generated Russian commit message)
    cp1 = git_service.create_project_checkpoint(proj, message="")
    assert cp1["ok"] is True and cp1["created"] is True
    assert "Точка сохранения:" in cp1["message"]
    assert set(cp1["files"]) == {".gitignore", ".env.example", "main.py", "utils.js"}

    # Verify forbidden files are absent from HEAD commit tree, while still intact on disk
    ls_tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=proj,
        env=_clean_git_env(),
        capture_output=True,
        text=True,
        check=True,
        **config.subprocess_hidden_kwargs(),
    ).stdout.splitlines()
    assert set(ls_tree) == {".gitignore", ".env.example", "main.py", "utils.js"}
    assert (proj / ".env").is_file()
    assert (proj / "secret.db").is_file()
    assert (proj / "bot.session").is_file()

    # Calling checkpoint on clean tree is a safe no-op
    cp_noop = git_service.create_project_checkpoint(proj)
    assert cp_noop == {"ok": True, "created": False, "reason": "clean", "files_count": 0, "files": []}

    # Verify fallback committer identity when parent Git hook exports GIT_AUTHOR_* without GIT_COMMITTER_*
    old_auth_env = {k: os.environ.get(k) for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_CONFIG_GLOBAL")}
    try:
        os.environ["GIT_CONFIG_GLOBAL"] = str(tmp_path / "empty_gitconfig")
        os.environ["GIT_AUTHOR_NAME"] = "ParentHook"
        os.environ["GIT_AUTHOR_EMAIL"] = "parent@hook.local"
        os.environ.pop("GIT_COMMITTER_NAME", None)
        os.environ.pop("GIT_COMMITTER_EMAIL", None)
        env_built = git_service._build_git_env(proj, author_fallback=True)
        assert env_built.get("GIT_COMMITTER_NAME") == "ParentHook"
        assert env_built.get("GIT_COMMITTER_EMAIL") == "parent@hook.local"
    finally:
        for k, v in old_auth_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 3. Syntax error in `.py` or `.js` blocks checkpoint creation (`run_verify=True`)
    (proj / "main.py").write_text("def broken_syntax(:\n    return 1\n", encoding="utf-8")
    with pytest.raises(git_service.GitCheckpointVerifyError) as exc_info:
        git_service.create_project_checkpoint(proj, message="Битый коммит")
    assert "main.py" in str(exc_info.value)
    assert git_service.get_project_git_status(proj)["head_sha"] == cp1["sha"]

    if config._find_node_bin():
        (proj / "main.py").write_text("def greet(name: str) -> str:\n    return f'Hello, {name}!'\n", encoding="utf-8")
        (proj / "utils.js").write_text("function brokenJs( { return 1; }\n", encoding="utf-8")
        with pytest.raises(git_service.GitCheckpointVerifyError) as js_exc:
            git_service.create_project_checkpoint(proj, message="Битый JS")
        assert "utils.js" in str(js_exc.value)
        (proj / "utils.js").write_text("function sum(a, b) { return a + b; }\n", encoding="utf-8")

    # Fix syntax + add `feature.py` -> create checkpoint #2
    (proj / "main.py").write_text(
        "def greet(name: str) -> str:\n    return f'Welcome, {name}!'\n",
        encoding="utf-8",
    )
    (proj / "feature.py").write_text("FEATURE_VERSION = 2\n" + ("# line\n" * 120), encoding="utf-8")
    cp2 = git_service.create_project_checkpoint(proj, message="Добавлен модуль feature.py")
    assert cp2["created"] is True
    assert cp2["sha"] != cp1["sha"]

    # Commit diff + bounded truncation
    cp2_diff = git_service.get_project_git_diff(proj, commit=cp2["sha"])
    assert "FEATURE_VERSION = 2" in cp2_diff["diff"]
    assert cp2_diff["truncated"] is False
    cp2_trunc = git_service.get_project_git_diff(proj, commit=cp2["sha"], max_chars=512)
    assert cp2_trunc["truncated"] is True

    # 4. Safe rollback: `discard_uncommitted` stashes dirty work without losing code or touching `.env`
    (proj / "main.py").write_text("DIRTY_EXPERIMENT = 'save_me_in_stash'\n", encoding="utf-8")
    (proj / "draft_notes.py").write_text("DRAFT = True\n", encoding="utf-8")
    rb_discard = git_service.rollback_project_to_checkpoint(proj, mode="discard_uncommitted")
    assert rb_discard["ok"] is True
    assert rb_discard["backup_stash_created"] is True
    assert rb_discard["head_sha"] == cp2["sha"]
    assert not (proj / "draft_notes.py").exists()
    assert "Welcome," in (proj / "main.py").read_text(encoding="utf-8")
    assert (proj / ".env").read_text(encoding="utf-8") == "SUPER_SECRET_TOKEN=top_secret_value_123\n"

    stash_patch = subprocess.run(
        ["git", "stash", "show", "-p", "stash@{0}"],
        cwd=proj,
        env=_clean_git_env(),
        capture_output=True,
        text=True,
        check=True,
        **config.subprocess_hidden_kwargs(),
    ).stdout
    assert "save_me_in_stash" in stash_patch

    # 5. Safe rollback: `restore_checkpoint` stashes dirty edits, restores exact tree of `cp1`
    # (removing `feature.py` added in `cp2`), and records a NEW forward commit preserving history!
    (proj / "main.py").write_text("SECOND_DIRTY_EDIT = 42\n", encoding="utf-8")
    rb_restore = git_service.rollback_project_to_checkpoint(
        proj,
        mode="restore_checkpoint",
        target_sha=cp1["sha"],
    )
    assert rb_restore["ok"] is True
    assert rb_restore["commit_created"] is True
    assert rb_restore["backup_stash_created"] is True
    assert f"Возврат к точке сохранения {cp1['short_sha']}" == rb_restore["message"]
    assert "Hello," in (proj / "main.py").read_text(encoding="utf-8")
    assert not (proj / "feature.py").exists()
    assert (proj / ".env").read_text(encoding="utf-8") == "SUPER_SECRET_TOKEN=top_secret_value_123\n"

    st_final = git_service.get_project_git_status(proj)
    assert st_final["is_clean"] is True
    assert st_final["stashes_count"] == 2
    checkpoint_shas = [c["sha"] for c in st_final["checkpoints"]]
    assert checkpoint_shas == [rb_restore["head_sha"], cp2["sha"], cp1["sha"]]


def test_install_project_shield_and_real_pre_commit_hook_enforcement(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git CLI is not installed")

    from app import config, git_service

    proj = tmp_path / "ShieldedApp"
    proj.mkdir()

    res = git_service.install_project_shield(proj)
    assert res["ok"] is True
    assert res["shield_installed"] is True
    assert res["shield_kind"] == "project"

    hook_file = proj / "tools" / "git_hooks" / "pre-commit"
    verifier_file = proj / "tools" / "verify_project.py"
    assert hook_file.is_file() and verifier_file.is_file()
    assert b"\r\n" not in hook_file.read_bytes()

    st = git_service.get_project_git_status(proj)
    assert st["shield_installed"] is True
    assert st["shield_kind"] == "project"

    env = _clean_git_env()
    git_base = ["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com"]

    def run_git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*git_base, *args],
            cwd=proj,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **config.subprocess_hidden_kwargs(),
        )

    # 1. Pre-commit hook blocks force-staged `.env`
    (proj / ".env").write_text("BOT_TOKEN=secret\n", encoding="utf-8")
    assert run_git("add", "-f", "--", ".env").returncode == 0
    blocked_env = run_git("commit", "-m", "Attempt to commit .env")
    assert blocked_env.returncode != 0
    assert "VERIFY_PROJECT_BLOCK" in (blocked_env.stderr + blocked_env.stdout)
    run_git("rm", "--cached", "-q", "--", ".env")

    # 2. Pre-commit hook blocks broken Python syntax
    (proj / "app.py").write_text("def broken(\n", encoding="utf-8")
    assert run_git("add", "--", "app.py").returncode == 0
    blocked_py = run_git("commit", "-m", "Attempt to commit broken Python")
    assert blocked_py.returncode != 0
    assert "VERIFY_PROJECT_BLOCK" in (blocked_py.stderr + blocked_py.stdout)

    # 3. Pre-commit hook allows valid clean code
    (proj / "app.py").write_text("def compute() -> int:\n    return 2 + 2\n", encoding="utf-8")
    assert run_git("add", "--", ".gitignore", "tools/verify_project.py", "tools/git_hooks/pre-commit", "app.py").returncode == 0
    clean_commit = run_git("commit", "-m", "Initial verified commit")
    assert clean_commit.returncode == 0, clean_commit.stderr + clean_commit.stdout
    assert "VERIFY_PROJECT_OK" in (clean_commit.stdout + clean_commit.stderr)

    # 4. Self-Hub (`ROOT_DIR`) reports `shield_kind == "hub"` without modifying `verify_hub.py`
    hub_st = git_service.get_project_git_status(ROOT_DIR, limit=3)
    assert hub_st["shield_kind"] == "hub"
    assert hub_st["shield_installed"] is True


class ProjectGitRestAndRbacV148Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git CLI is not installed")

        from app import auth, brain_reader, config, database, git_service, main as main_mod

        self.auth = auth
        self.brain_reader = brain_reader
        self.config = config
        self.database = database
        self.git_service = git_service
        self.main_mod = main_mod

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v148_api_", ignore_cleanup_errors=True)
        iso_root = Path(self._tmp.name).resolve() / "iso_hub"
        data_dir = iso_root / "data"
        users_dir = iso_root / "users"
        projects_root = users_dir / "admin" / "projects"
        shared_dir = iso_root / "shared_projects"
        shared_uploads = shared_dir / "_uploads"
        shared_ctx = shared_dir / "_context"
        cli_homes = data_dir / "cli_homes"
        uploads_dir = data_dir / "uploads"
        db_path = data_dir / "test_v148.db"
        for d in (
            iso_root,
            iso_root / "app",
            iso_root / ".venv",
            iso_root / ".git",
            data_dir,
            users_dir,
            projects_root,
            shared_dir,
            shared_uploads,
            shared_ctx,
            cli_homes,
            uploads_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        await database.close_db_pool()
        patch_map = {
            "BASE_DIR": iso_root,
            "DATA_DIR": data_dir,
            "DB_PATH": db_path,
            "USERS_DIR": users_dir,
            "USER_WORKSPACES_DIR": users_dir,
            "PROJECTS_ROOT": projects_root,
            "SHARED_PROJECTS_DIR": shared_dir,
            "SHARED_UPLOADS_DIR": shared_uploads,
            "SHARED_CONTEXT_DIR": shared_ctx,
            "CLI_HOMES_DIR": cli_homes,
            "UPLOADS_DIR": uploads_dir,
        }
        self._saved_attrs: list[tuple[Any, str, Any]] = []
        for mod in (config, database, brain_reader, main_mod):
            for attr, val in patch_map.items():
                if hasattr(mod, attr):
                    self._saved_attrs.append((mod, attr, getattr(mod, attr)))
                    setattr(mod, attr, val)

        self.users_dir = users_dir
        self.shared_dir = shared_dir
        brain_reader.invalidate_projects_list_cache()
        await database.init_db()

    async def asyncTearDown(self) -> None:
        await self.database.close_db_pool()
        for mod, attr, orig in reversed(self._saved_attrs):
            setattr(mod, attr, orig)
        self.brain_reader.invalidate_projects_list_cache()
        self._tmp.cleanup()

    async def test_http_rest_endpoints_and_rbac_enforcement(self) -> None:
        writer = await self.database.create_user(
            username="writer_dev",
            password="Password123!",
            display_name="Writer Dev",
            max_access_mode="write_files",
        )
        readonly_guest = await self.database.create_user(
            username="readonly_viewer",
            password="Password123!",
            display_name="ReadOnly Viewer",
            max_access_mode="read_only",
        )

        writer_proj_dir = self.users_dir / "writer_dev" / "projects" / "WriterProj"
        writer_proj_dir.mkdir(parents=True, exist_ok=True)
        (writer_proj_dir / "service.py").write_text("VALUE = 10\n", encoding="utf-8")
        (writer_proj_dir / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

        guest_proj_dir = self.users_dir / "readonly_viewer" / "projects" / "GuestProj"
        guest_proj_dir.mkdir(parents=True, exist_ok=True)
        (guest_proj_dir / "readme.py").write_text("A = 1\n", encoding="utf-8")

        shared_proj_dir = self.shared_dir / "SharedTeamProj"
        shared_proj_dir.mkdir(parents=True, exist_ok=True)
        (shared_proj_dir / "shared_mod.py").write_text("TEAM = True\n", encoding="utf-8")
        await self.database.create_shared_project(
            name="SharedTeamProj",
            member_ids=[writer["id"], readonly_guest["id"]],
            execution_mode="docker",
            storage_location="shared_dir",
        )
        self.brain_reader.invalidate_projects_list_cache()

        cname = self.auth.COOKIE_NAME
        admin_cookie = f"{cname}={self.auth.create_user_session_token('admin', 1)}"
        writer_cookie = f"{cname}={self.auth.create_user_session_token(writer['id'], int(writer.get('session_version') or 1))}"
        guest_cookie = f"{cname}={self.auth.create_user_session_token(readonly_guest['id'], int(readonly_guest.get('session_version') or 1))}"

        app = self.main_mod.app

        # 1. Writer owner full HTTP lifecycle on personal `WriterProj`
        code, d_init = await _asgi_request(app, "POST", "/api/projects/WriterProj/git/init", cookie_header=writer_cookie)
        self.assertEqual(code, 200, d_init)
        self.assertTrue(d_init["gitignore_exists"])

        code, d_st = await _asgi_request(app, "GET", "/api/projects/WriterProj/git/status", cookie_header=writer_cookie)
        self.assertEqual(code, 200)
        self.assertEqual({f["path"] for f in d_st["changed_files"]}, {".gitignore", "service.py"})

        code, d_cp1 = await _asgi_request(
            app,
            "POST",
            "/api/projects/WriterProj/git/checkpoint",
            cookie_header=writer_cookie,
            json_body={"message": "Первая точка WriterProj"},
        )
        self.assertEqual(code, 200, d_cp1)
        self.assertTrue(d_cp1["created"])
        cp1_sha = d_cp1["sha"]

        (writer_proj_dir / "service.py").write_text("VALUE = 20\n", encoding="utf-8")
        code, d_diff = await _asgi_request(app, "GET", "/api/projects/WriterProj/git/diff", cookie_header=writer_cookie)
        self.assertEqual(code, 200)
        self.assertIn("+VALUE = 20", d_diff["diff"])

        code, d_shield = await _asgi_request(
            app,
            "POST",
            "/api/projects/WriterProj/git/install-shield",
            cookie_header=writer_cookie,
        )
        self.assertEqual(code, 200, d_shield)
        self.assertEqual(d_shield["shield_kind"], "project")

        code, d_rb = await _asgi_request(
            app,
            "POST",
            "/api/projects/WriterProj/git/rollback",
            cookie_header=writer_cookie,
            json_body={"mode": "discard_uncommitted"},
        )
        self.assertEqual(code, 200, d_rb)
        self.assertTrue(d_rb["backup_stash_created"])
        self.assertEqual(d_rb["head_sha"], cp1_sha)

        # 2. Read-only user (`max_access_mode == "read_only"`): GET allowed (200), all POST mutations return 403!
        for target_proj in ("GuestProj", "SharedTeamProj"):
            c_st, _ = await _asgi_request(app, "GET", f"/api/projects/{target_proj}/git/status", cookie_header=guest_cookie)
            self.assertEqual(c_st, 200)
            c_df, _ = await _asgi_request(app, "GET", f"/api/projects/{target_proj}/git/diff", cookie_header=guest_cookie)
            self.assertEqual(c_df, 200)

            for ep, payload in (
                ("init", None),
                ("checkpoint", {"message": "nope"}),
                ("rollback", {"mode": "discard_uncommitted"}),
                ("install-shield", None),
            ):
                c_mut, d_mut = await _asgi_request(
                    app,
                    "POST",
                    f"/api/projects/{target_proj}/git/{ep}",
                    cookie_header=guest_cookie,
                    json_body=payload,
                )
                self.assertEqual(c_mut, 403, f"Expected 403 on {target_proj}/{ep}, got {c_mut}: {d_mut}")

        # Non-admin writer member on shared project: read 200, write 403 (admin-only on shared projects)
        c_sh_get, _ = await _asgi_request(app, "GET", "/api/projects/SharedTeamProj/git/status", cookie_header=writer_cookie)
        self.assertEqual(c_sh_get, 200)
        c_sh_post, _ = await _asgi_request(
            app,
            "POST",
            "/api/projects/SharedTeamProj/git/checkpoint",
            cookie_header=writer_cookie,
            json_body={},
        )
        self.assertEqual(c_sh_post, 403)

        # Admin can create checkpoint on shared project
        c_adm_sp, d_adm_sp = await _asgi_request(
            app,
            "POST",
            "/api/projects/SharedTeamProj/git/checkpoint",
            cookie_header=admin_cookie,
            json_body={"message": "Admin shared checkpoint"},
        )
        self.assertEqual(c_adm_sp, 200, d_adm_sp)
        self.assertTrue(d_adm_sp["created"])

        # 3. Self-Hub (`Antigravity_Hub`) rollback is strictly forbidden (400) even for admin!
        c_self_rb, d_self_rb = await _asgi_request(
            app,
            "POST",
            "/api/projects/Antigravity_Hub/git/rollback",
            cookie_header=admin_cookie,
            json_body={"mode": "discard_uncommitted"},
        )
        self.assertEqual(c_self_rb, 400, d_self_rb)
        self.assertIn("Antigravity Hub", d_self_rb["detail"])

        with pytest.raises(self.git_service.GitServiceError) as hub_rb_exc:
            self.git_service.rollback_project_to_checkpoint(self.config.BASE_DIR, mode="discard_uncommitted")
        self.assertEqual(hub_rb_exc.value.status_code, 400)

        # 4. Project memory `disable_default_guardrails` SQLite & HTTP roundtrip
        c_mem, d_mem = await _asgi_request(
            app,
            "PUT",
            "/api/projects/WriterProj/memory",
            cookie_header=writer_cookie,
            json_body={"disable_default_guardrails": True},
        )
        self.assertEqual(c_mem, 200, d_mem)
        self.assertTrue(d_mem["memory"]["disable_default_guardrails"])


if __name__ == "__main__":
    unittest.main()
