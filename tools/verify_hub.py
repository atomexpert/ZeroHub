"""Run the Hub release checks without starting the server.

Modes:
  (default)        sources, node --check, git diff --check and the fast GOLDEN pytest pack
  --full           the same checks with the entire tests directory
  --staged-guard   pre-commit lock: staged paths / PWA cache-version guard, then the default checks
  --install-hooks  point core.hooksPath at the versioned tools/git_hooks directory
"""
import argparse
import fnmatch
import os
import py_compile
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_DB = ROOT / "data" / "hub.db"
HOOKS_DIR = "tools/git_hooks"
GOLDEN = tuple(
    f"tests/test_{name}.py"
    for name in (
        # Early load-bearing subsystems (v106-v130)
        "proxy_and_xray_routing_v106",
        "model_advisor_and_profile",
        "project_rename_delete",
        "project_share_unshare",
        "fast_hq_return_v126",
        "multi_account_per_tab_v127",
        "agy_stability_and_hidden_console_v130",
        # Stabilization and redesign (v132-v145)
        "frontend_contracts",
        "gemini_responsiveness_v132",
        "quotas_and_multistep_high_v134",
        "process_monitor_clear_and_ui_v135",
        "gemini_trailing_location_error_v136",
        "dynamic_activity_badges_v137",
        "mobile_voice_recovery_v139",
        "ui_foundation_v141",
        "ui_workspace_minimal_v142",
        "ui_sidebar_minimal_v143",
        "ui_modals_and_sheets_v144",
        "ui_full_geometry_gate_v145",
        # Regression guardrails, Windows launch, Git checkpoints and Google Drive mirror (v146-v152)
        "regression_guardrails_v146",
        "agy_run_command_0xc0000142_v147",
        "project_git_checkpoints_and_guardrails_v148",
        "ui_git_checkpoints_v149",
        "handoff_checkpoint_and_confirm_v150",
        "gdrive_sync_core_v151",
        "ui_gdrive_modal_v151",
        "gdrive_autosync_and_restore_v152",
        "zerohub_rebrand_and_gdrive_progress_v153",
        "dynamic_model_advisor_v154",
        "sidebar_filter_badge_contrast_v154",
        "project_env_manager_v155",
        "gdrive_ui_assets_and_advisor_backup_v156",
        "silent_empty_turn_recovery_v157",
        "release_and_updater_v158",
        "ui_updates_and_publisher_v159",
        "ui_settings_tabs_v160",
    )
)

# Live data, secrets and scratch output must never be committed.
FORBIDDEN_ROOT_DIRS = ("data", "users", "shared_projects", "tmp", "scratch")
FORBIDDEN_NAME_PATTERNS = ("*.db*", ".env*")
ALLOWED_NAMES = (".env.example",)
SW_PATH = "app/static/sw.js"
CACHE_NAME_RE = re.compile(r"""CACHE_NAME\s*=\s*["']agy-hub-v(\d+)["']""")
STATIC_ASSET_RE = re.compile(r"^app/static/.+\.(?:js|css)$", re.IGNORECASE)
# Git variables injected into hooks would redirect git calls made by tests to this repository.
GIT_HOOK_ENV_VARS = (
    "GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_PREFIX", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
)


def snapshot_db():
    try:
        stat = LIVE_DB.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns


def hidden_kwargs():
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def child_env():
    env = dict(os.environ)
    for name in GIT_HOOK_ENV_VARS:
        env.pop(name, None)
    return env


def run(*args, env=None):
    print(f"> {' '.join(args)}", flush=True)
    result = subprocess.run(
        args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, **hidden_kwargs(),
    )
    # Pass an explicit output handle to children hidden by CREATE_NO_WINDOW.
    sys.stdout.buffer.write(result.stdout)
    sys.stdout.buffer.flush()
    result.check_returncode()


def git_output(*args):
    """Return stdout of a git command (keeps hook GIT_* env so the commit's index is used) or None."""
    result = subprocess.run(
        ("git", *args), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **hidden_kwargs(),
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def normalize_path(path):
    path = str(path).strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def is_forbidden_path(path):
    path = normalize_path(path)
    if not path:
        return False
    parts = path.split("/")
    if parts[0].lower() in FORBIDDEN_ROOT_DIRS and len(parts) > 1:
        return True
    name = parts[-1].lower()
    if name in ALLOWED_NAMES:
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in FORBIDDEN_NAME_PATTERNS)


def parse_cache_version(source):
    match = CACHE_NAME_RE.search(source or "")
    return int(match.group(1)) if match else None


def staged_guard_errors(added_paths, changed_paths, staged_sw, head_sw):
    """Pure staged-commit policy.

    added_paths   -- staged paths that will exist after the commit (no deletions)
    changed_paths -- every staged path, including deletions
    staged_sw     -- staged app/static/sw.js text (None if absent)
    head_sw       -- HEAD:app/static/sw.js text (None if HEAD has no sw.js)
    """
    errors = []
    forbidden = sorted({normalize_path(p) for p in added_paths if is_forbidden_path(p)})
    if forbidden:
        errors.append(
            "staged live data or secrets (data/, users/, shared_projects/, tmp/, scratch/, *.db*, .env*): "
            + ", ".join(forbidden)
        )
    static_changed = sorted({normalize_path(p) for p in changed_paths if STATIC_ASSET_RE.match(normalize_path(p))})
    if static_changed and head_sw is not None:
        head_v = parse_cache_version(head_sw)
        current_v = parse_cache_version(staged_sw)
        if head_v is not None and (current_v is None or current_v <= head_v):
            errors.append(
                f"Cache-First PWA guard: {', '.join(static_changed)} changed but CACHE_NAME in {SW_PATH} "
                f"is v{current_v} (HEAD v{head_v}); bump ?v=N in app/static/index.html and "
                f"CACHE_NAME/SHELL_ASSETS in {SW_PATH} to at least v{head_v + 1}"
            )
    return errors


def staged_guard():
    """Collect the real staged state from Git and apply staged_guard_errors."""
    print("Checking staged paths and the PWA cache version", flush=True)
    added = git_output("diff", "--cached", "--name-only", "--diff-filter=d", "-z")
    changed = git_output("diff", "--cached", "--name-only", "-z")
    if added is None or changed is None:
        raise ValueError("cannot read staged paths from git")
    added_paths = [p for p in added.split("\0") if p]
    changed_paths = [p for p in changed.split("\0") if p]
    print(f"STAGED_PATHS: {len(changed_paths)}", flush=True)
    staged_sw = git_output("show", f":{SW_PATH}")
    head_sw = git_output("show", f"HEAD:{SW_PATH}")
    errors = staged_guard_errors(added_paths, changed_paths, staged_sw, head_sw)
    for error in errors:
        print(f"STAGED_GUARD_BLOCK: {error}", file=sys.stderr, flush=True)
    if errors:
        raise ValueError("staged guard blocked the commit")
    print(
        f"STAGED_GUARD_OK: cache v{parse_cache_version(staged_sw)} (HEAD v{parse_cache_version(head_sw)})",
        flush=True,
    )


def install_hooks():
    hook = ROOT / HOOKS_DIR / "pre-commit"
    if not hook.is_file():
        print(f"INSTALL_HOOKS_FAILED: {hook.relative_to(ROOT)} is missing", file=sys.stderr)
        return 1
    if os.name != "nt":
        hook.chmod(hook.stat().st_mode | 0o111)
    subprocess.run(("git", "config", "core.hooksPath", HOOKS_DIR), cwd=ROOT, check=True, **hidden_kwargs())
    print(f"HOOKS_INSTALLED: core.hooksPath={git_output('config', '--get', 'core.hooksPath').strip()}")
    return 0


def check_sources():
    python_files = sorted((*ROOT.glob("app/*.py"), *ROOT.glob("tools/*.py")))
    static_files = sorted(
        path for path in ROOT.glob("app/static/*")
        if path.suffix in {".html", ".css", ".js"}
    )
    extra_files = [
        path for path in (ROOT / HOOKS_DIR / "pre-commit", *ROOT.glob(".agents/rules/*.md"))
        if path.is_file()
    ]
    shell_files = sorted(ROOT.glob("*.sh"))
    public_files = (*python_files, *static_files, *ROOT.glob("*.md"), *shell_files,
                    *ROOT.glob("*.ps1"), ROOT / ".env.example")
    secret_pattern = re.compile(
        r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
        r"|GOCSPX-[A-Za-z0-9_-]{18,}|AIzaSy[A-Za-z0-9_-]{25,}"
    )
    print("Checking UTF-8, shell line endings and embedded secrets, then compiling Python", flush=True)
    for path in (*public_files, *(ROOT / name for name in GOLDEN), *extra_files):
        source = path.read_bytes().decode("utf-8")
        if source.startswith("\ufeff") or "\ufffd" in source:
            raise ValueError(f"{path.relative_to(ROOT)} contains a BOM or replacement character")
        if path in shell_files and "\r" in source:
            raise ValueError(f"{path.relative_to(ROOT)} must use LF line endings")
        if path in public_files and (match := secret_pattern.search(source)):
            line = source.count("\n", 0, match.start()) + 1
            raise ValueError(f"{path.relative_to(ROOT)}:{line}: embedded secret")
    with tempfile.TemporaryDirectory(prefix="hub_verify_") as directory:
        for index, path in enumerate(python_files):
            py_compile.compile(str(path), str(Path(directory) / f"{index}.pyc"), doraise=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--full", action="store_true", help="Run the entire tests directory")
    parser.add_argument("--staged-guard", action="store_true", help="Pre-commit mode: check staged paths first")
    parser.add_argument("--install-hooks", action="store_true", help="Set core.hooksPath to tools/git_hooks")
    options = parser.parse_args()
    if options.install_hooks:
        return install_hooks()
    before = snapshot_db()
    print(f"HUB_DB_BEFORE: {before}", flush=True)
    status = 0
    try:
        if options.staged_guard:
            staged_guard()
        check_sources()
        env = child_env()
        for asset in ("ui-shell.js", "app.js", "sw.js"):
            run("node", "--check", f"app/static/{asset}", env=env)
        # The repository tracks both LF and CRLF sources.
        git_diff = ("git", "-c", "core.whitespace=cr-at-eol", "diff")
        run(*git_diff, "--check")
        run(*git_diff, "--cached", "--check")
        if options.staged_guard:
            env["HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY"] = "1"
        else:
            env.pop("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY", None)
        run(sys.executable, "-m", "pytest", "-q", *(("tests",) if options.full else GOLDEN), env=env)
    except (OSError, ValueError, py_compile.PyCompileError, subprocess.CalledProcessError) as exc:
        print(f"VERIFY_HUB_FAILED: {exc}", file=sys.stderr, flush=True)
        status = 1
    finally:
        after = snapshot_db()
        print(f"HUB_DB_AFTER: {after}", flush=True)
        unchanged = before == after
        print(f"HUB_DB_UNCHANGED: {unchanged}", flush=True)
        if not unchanged and options.staged_guard:
            print("HUB_DB_EXTERNAL_ACTIVITY: live DB snapshot changed during staged checks", flush=True)
    if status or (not unchanged and not options.staged_guard):
        return 1
    print("VERIFY_HUB_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
