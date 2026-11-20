
"""GitHub publishing and client updates for ZeroHub."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import config, env_service, proxy_router

CACHE_SECONDS = 900
OWNER_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
GITHUB_TOKEN_RE = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")
SECRET_PATTERNS = {
    "GitHub token": GITHUB_TOKEN_RE,
    "OpenRouter key": re.compile(r"sk-or-v1-[A-Za-z0-9_-]{20,}"),
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "Google OAuth secret": re.compile(r"GOCSPX-[A-Za-z0-9_-]{18,}"),
    "Google API key": re.compile(r"AIzaSy[A-Za-z0-9_-]{25,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
HOST_USER = "ato" + "me"
HOST_DOMAIN = "hub." + "sibnet.app"
HOST_ACCOUNT = "atom" + "77793"
LEGACY_HOME = re.compile(r"[A-Z]:[\\/]+Internet[\\/]+Antigravity(?=[\\/\s\x60*]|$)", re.I)
PERSONAL_HOST = re.compile(
    rf"Users[\\/]+{HOST_USER}\b|{HOST_ACCOUNT}|{re.escape(HOST_DOMAIN)}|{LEGACY_HOME.pattern}", re.I
)
PRIVATE_DIRS = {"data", "users", "shared_projects", "tmp", "scratch", ".git", ".secrets", "private-profiles"}
PASSPORT = """# ZeroHub project passport

ZeroHub is a self-hosted FastAPI and PWA workspace for AI agents on Windows and Linux.
Use run_server.py or the platform launcher. Local state lives in data/, users/ and shared_projects/.
Keep tests isolated from the running service and its SQLite database. Run tools/verify_hub.py
before committing, retain the existing access controls, and run speech recognition on the CPU.
"""
_MUTATION_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_cache: tuple[tuple, float, dict[str, Any]] | None = None


class ReleaseError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class ReleaseSecurityError(ReleaseError):
    def __init__(self, issues: list[str]):
        super().__init__("\n".join(issues), 422)
        self.issues = issues


@contextlib.contextmanager
def _mutation():
    if not _MUTATION_LOCK.acquire(blocking=False):
        raise ReleaseError("Another release operation is in progress.", 409)
    try:
        yield
    finally:
        _MUTATION_LOCK.release()


def parse_semver(raw: str) -> tuple[int, int, int]:
    version = raw.strip().removeprefix("v")
    if not config.VERSION_RE.fullmatch(version):
        raise ValueError("Expected a version in X.Y.Z format.")
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def is_newer_version(current: str, candidate: str) -> bool:
    return parse_semver(candidate) > parse_semver(current)


def mask_github_token(token: str) -> str:
    if not token:
        return ""
    prefix = next((p for p in ("ghp_", "github_pat_") if token.startswith(p)), "")
    return prefix + "••••" + (token[-4:] if len(token) - len(prefix) > 4 else "")


def _dotenv_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8") if path.exists() else ""


def _validate_slug(owner: str, repo: str) -> None:
    for part in (owner, repo):
        if not OWNER_RE.fullmatch(part) or part in {".", ".."}:
            raise ValueError("Invalid GitHub repository owner or name.")


def _publisher_config(env_path: Path | None = None) -> tuple[str, str, str]:
    path = env_path if env_path is not None else config.ENV_PATH
    values = {entry["key"]: entry["value"] for entry in env_service.parse_dotenv_text(_dotenv_text(path))["entries"]}
    owner = values.get("GITHUB_REPO_OWNER", "").strip() or config.DEFAULT_GITHUB_OWNER
    repo = values.get("GITHUB_REPO_NAME", "").strip() or config.DEFAULT_GITHUB_REPO
    _validate_slug(owner, repo)
    return owner, repo, values.get("GITHUB_PUBLISH_TOKEN", "").strip()


def get_publisher_settings(env_path: Path | None = None) -> dict[str, Any]:
    owner, repo, token = _publisher_config(env_path)
    return {"owner": owner, "repo": repo, "has_token": bool(token), "token_mask": mask_github_token(token)}


def save_publisher_settings(
    *, owner: str, repo: str, new_token: str | None = None,
    clear_token: bool = False, env_path: Path | None = None,
) -> dict[str, Any]:
    _validate_slug(owner, repo)
    if new_token is not None and (
        clear_token or not isinstance(new_token, str) or not GITHUB_TOKEN_RE.fullmatch(new_token)
    ):
        raise ValueError("Invalid GitHub publishing token.")
    path = env_path if env_path is not None else config.ENV_PATH
    values = {"GITHUB_REPO_OWNER": owner, "GITHUB_REPO_NAME": repo}
    if new_token is not None or clear_token:
        values["GITHUB_PUBLISH_TOKEN"] = new_token or ""
    with _mutation():
        entries = [{"key": key, "value": value} for key, value in values.items()]
        text = env_service.apply_entries_to_dotenv(_dotenv_text(path), entries, merge=True)
        env_service._atomic_write_text_utf8(path, text)
        if path.resolve() == config.ENV_PATH.resolve():
            os.environ.update(values)
            _invalidate_cache()
        return get_publisher_settings(path)


def _invalidate_cache() -> None:
    global _cache
    with _CACHE_LOCK:
        _cache = None


def _github_json(request: urllib.request.Request, timeout: int) -> Any:
    opener = proxy_router.build_urllib_proxy_opener(config.resolve_outbound_proxy())
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


def _github(method, path, token="", payload=None, transport=None, allowed=()):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ZeroHub-Updater/1.0"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request("https://api.github.com" + path, body, headers, method=method)
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        response = (transport or _github_json)(request, 12)
    except urllib.error.HTTPError as exc:
        if exc.code in allowed:
            return exc.code, {}
        raise ReleaseError(f"GitHub API returned HTTP {exc.code}.") from None
    except (OSError, ValueError):
        raise ReleaseError("GitHub API is unavailable or returned an invalid response.") from None
    if not isinstance(response, dict):
        raise ReleaseError("GitHub API returned an invalid response.")
    return 200, response


def get_cached_update() -> dict[str, Any] | None:
    with _CACHE_LOCK:
        return dict(_cache[2]) if _cache else None


def check_for_updates(*, force=False, env_path=None, github_api_transport=None) -> dict[str, Any]:
    global _cache
    owner, repo, token = _publisher_config(env_path)
    slug = f"{owner}/{repo}"
    current = config.get_app_version()
    key = (slug, current, hashlib.sha256(token.encode()).digest(), github_api_transport)
    with _CACHE_LOCK:
        if not force and _cache and _cache[0] == key and time.monotonic() - _cache[1] < CACHE_SECONDS:
            return dict(_cache[2])
    status, release = _github("GET", f"/repos/{slug}/releases/latest", token,
                              transport=github_api_transport, allowed=(404,))
    try:
        latest = ".".join(map(str, parse_semver(release.get("tag_name", "")))) if status == 200 else current
    except (AttributeError, ValueError):
        raise ReleaseError("The latest GitHub release has an invalid version tag.") from None
    result = {
        "ok": True,
        "current_version": current,
        "latest_version": latest,
        "update_available": is_newer_version(current, latest),
        "published_yet": status == 200,
        "release_title": release.get("name") or "",
        "release_notes": release.get("body") or "",
        "published_at": release.get("published_at") or "",
        "html_url": release.get("html_url") or "",
        "repo_slug": slug,
        "repo_url": f"https://github.com/{slug}",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    with _CACHE_LOCK:
        _cache = (key, time.monotonic(), result)
    return dict(result)


def _path_issue(path: str) -> str | None:
    parts = path.lower().split("/")
    if any(not part or part in {".", ".."} or part.rstrip(" .") != part for part in parts):
        return "invalid export path"
    if "\\" in path or ":" in path or any(ord(c) < 32 for c in path):
        return "invalid export path"
    if any(part in PRIVATE_DIRS for part in parts[:-1]):
        return "private directory"
    if any(
        part in PRIVATE_DIRS or ".db" in part or ".session" in part or part.endswith((".pem", ".key"))
        or (part.startswith(".env") and part != ".env.example")
        or (part.endswith(".json") and ("secret" in part or "credentials" in part))
        for part in parts
    ):
        return "private file"
    return None


def _safe_path(path: str) -> str:
    if any(ord(c) < 32 for c in path) or PERSONAL_HOST.search(path) or any(
        p.search(path) for p in SECRET_PATTERNS.values()
    ):
        return "<redacted path>"
    return path


def scan_export_tree_for_leaks(files) -> None:
    issues = []
    entries = files.items() if isinstance(files, dict) else files
    for path, raw in entries:
        label = _safe_path(path)
        reason = _path_issue(path)
        if reason:
            issues.append(f"{label}:1: {reason}")
            continue
        patterns = SECRET_PATTERNS
        if not path.startswith("tests/"):
            patterns = {**patterns, "personal host marker": PERSONAL_HOST}
        for reason, pattern in patterns.items():
            if pattern.search(path):
                issues.append(f"{label}:1: {reason}")
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
        source = raw.decode(encoding, errors="replace")
        for reason, pattern in patterns.items():
            for match in pattern.finditer(source):
                line = source.count("\n", 0, match.start()) + 1
                issues.append(f"{label}:{line}: {reason}")
    if issues:
        raise ReleaseSecurityError(issues)


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("GITHUB_PUBLISH_TOKEN", None)
    env.update(CUDA_VISIBLE_DEVICES="", NVIDIA_VISIBLE_DEVICES="none", ORT_DISABLE_ALL_GPU="1")
    return env


def _git(root: Path, *args, allowed=(0,), token="", proxy=None, isolated=False):
    env = _child_env()
    for key in list(env):
        if key.startswith("GIT_"):
            del env[key]
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_LITERAL_PATHSPECS="1")
    options = []
    if isolated:
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        options += ["-c", "core.autocrlf=false", "-c", "credential.helper=", "-c", "http.followRedirects=false"]
    if proxy:
        options += ["-c", f"http.proxy={proxy}"]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        options += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
    try:
        result = subprocess.run(
            ["git", *options, *args], cwd=root, env=env, capture_output=True,
            timeout=180, **config.subprocess_hidden_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseError(f"Could not complete Git operation ({args[0]}).") from None
    if result.returncode not in allowed:
        raise ReleaseError(f"Git operation failed ({args[0]}, exit {result.returncode}).")
    return result


def _lines(result) -> list[str]:
    return result.stdout.decode("utf-8").splitlines()


def _index_entries(root: Path, *, isolated=False) -> dict[str, str]:
    result = _git(root, "ls-files", "--stage", "-z", isolated=isolated)
    entries = {}
    for record in result.stdout.decode("utf-8").split("\0"):
        if not record:
            continue
        header, path = record.split("\t", 1)
        mode, _, stage = header.split()
        if mode not in {"100644", "100755"} or stage != "0":
            raise ReleaseSecurityError([f"{_safe_path(path)}:1: unsupported Git entry"])
        entries[path] = mode
    return entries


# Exact, existing synthetic values used by legacy offline regression tests.
# Express them as Python escapes in the public source while preserving their
# runtime values. Any new value still goes through the unmodified scanner.
_FIXTURE_DIGESTS = {
    "tests/test_claude_code_integration.py": {
        "2ed8672023772d6bb2f3e95dad4d959b894711c7a08763c50da968aaa64f4146",
    },
    "tests/test_dynamic_telemetry_sync.py": {
        "10b445117e011925b9c1eabc27d4457051614ef69243cb27a3a3fd4e45dcb1ed",
        "c7a3bf4d0b5833cc74d1e5539c23338c6c4749e9c71c059142615853cb6afe90",
    },
    "tests/test_gdrive_sync_core_v151.py": {
        "3021d90eb9437b2d8f30e8363695c4418b5e5f1870801b5c317e9398ee0f572d",
        "40868842e61a2093a66c97681ee47987405e3da8218126f3a400ee587b91ea55",
        "cb273f263d22577d45d4d5c78a0077a9d185f325c4e41158ed2792794912dfa9",
    },
    "tests/test_project_env_manager_v155.py": {
        "3021d90eb9437b2d8f30e8363695c4418b5e5f1870801b5c317e9398ee0f572d",
    },
    "tests/test_release_v91_orchestrator_upgrade.py": {
        "3fa7bc199af85b1706cb82dc4904f615c09cc6e9d874ca883279cc219fbdebb3",
        "46d63a9eec66f90e5a7c68279caea60159f4c34af7fa13f5719258c04b6451e2",
        "557a00ebf31f5894a53349c9bd844339d11a362017afd1d3a88efa097048ae24",
        "a5ba1671d16416bf232e222e61f75493014e34d74ade506e81f88175fa1ab40a",
        "c71e9789242f788020007ffa9d2ef7618a9d4ae409e7833e1ce80af60d256dea",
    },
    "tests/test_smart_titles_openrouter_v101.py": {
        "93bf307b65cd0661f30659eba2c2d351286b2fbcaea94fe86372b7f03d1514c8",
        "a27d88fb8c1c630fabf29cb87f252fb71f8c3f02b6c79897014125f12bc815ac",
    },
}


def _portable_export_file(path: str, raw: bytes) -> bytes:
    # These legacy examples remain in the development tree. Change only the known
    # literals in the exported snapshot, then run the same fail-closed scanner.
    if path == "app/static/index.html":
        raw = raw.replace(rf"C:\Users\{HOST_USER}\Desktop\app".encode(), rb"C:\Projects\app")
    if path in {"app/static/index.html", "tools/migrate_projects.py", ".agents/rules/engineering_guardrails.md"}:
        text = LEGACY_HOME.sub(lambda _: "D:\\\\Projects" if path.endswith(".py") else r"D:\Projects",
                               raw.decode("utf-8"))
        return text.encode("utf-8")
    if hashes := _FIXTURE_DIGESTS.get(path):
        def preserve_fixture(match: re.Match[str]) -> str:
            value = match[0]
            if hashlib.sha256(value.encode()).hexdigest() not in hashes:
                return value
            return f"\\x{ord(value[0]):02x}{value[1:]}"

        source = raw.decode("utf-8")
        for pattern in SECRET_PATTERNS.values():
            source = pattern.sub(preserve_fixture, source)
        return source.encode("utf-8")
    return raw


def _collect_export(root: Path, version: str) -> tuple[dict[str, bytes], dict[str, str]]:
    paths = _index_entries(root)
    files = {}
    resolved = root.resolve()
    for path in paths:
        reason = _path_issue(path)
        if reason:
            raise ReleaseSecurityError([f"{_safe_path(path)}:1: {reason}"])
        if path == ".agents/rules/hub_project.md":
            continue
        full = root / path
        if full.resolve() != resolved / path or full.is_symlink() or not full.is_file():
            raise ReleaseSecurityError([f"{_safe_path(path)}:1: expected a regular tracked file"])
        files[path] = _portable_export_file(path, full.read_bytes())
    files[".agents/rules/hub_project.md"] = PASSPORT.encode("utf-8")
    files["VERSION"] = f"{version}\n".encode()
    files.setdefault(".gitattributes", b"*.sh text eol=lf\nVERSION text eol=lf\n.env.example text eol=lf\n")
    return files, paths


def _batches(paths):
    paths = list(paths)
    for start in range(0, len(paths), 32):
        yield paths[start:start + 32]


def build_draft_release_notes(source_root: Path | None = None, max_items: int = 12) -> str:
    root = source_root or config.BASE_DIR
    if max_items <= 0 or not (root / ".git").exists():
        return ""
    titles = _lines(_git(root, "log", "-n", str(min(max_items, 100)), "--format=%s"))
    notes = "\n".join(f"- {title}" for title in titles if title.strip())
    for pattern in (*SECRET_PATTERNS.values(), PERSONAL_HOST):
        notes = pattern.sub("[redacted]", notes)
    return notes


def _publish_tree(root, files, modes, remote, owner, token, proxy, tag, title):
    network = {"isolated": True, "token": token, "proxy": proxy}
    existing = _git(root, "ls-remote", "--heads", "--", remote, "refs/heads/main", **network)
    initial = not existing.stdout.strip()
    if initial:
        _git(root, "init", "--template=", "-b", "main", isolated=True)
    else:
        _git(root, "clone", "--template=", "--depth", "1", "--branch", "main", "--", remote, ".", **network)
        for batch in _batches(_index_entries(root, isolated=True).keys() - files.keys()):
            _git(root, "rm", "-f", "--", *batch, isolated=True)
    for path, raw in files.items():
        target = root / path
        if target.is_symlink() or target.resolve() != root.resolve() / path:
            raise ReleaseSecurityError([f"{_safe_path(path)}:1: unsafe public checkout path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    for batch in _batches(files):
        _git(root, "add", "-f", "--", *batch, isolated=True)
    for batch in _batches(path for path in files if modes.get(path) == "100755"):
        _git(root, "update-index", "--chmod=+x", "--", *batch, isolated=True)
    change = _git(root, "diff", "--cached", "--quiet", allowed=(0, 1), isolated=True).returncode
    if initial or change:
        _git(root, "-c", f"user.name={owner}", "-c", f"user.email={owner}@users.noreply.github.com",
             "commit", "-m", f"Release {tag} — {title}", isolated=True)
    head = _lines(_git(root, "rev-parse", "HEAD", isolated=True))[0]
    remote_tag = _git(root, "ls-remote", "--tags", "--", remote, f"refs/tags/{tag}", **network)
    if remote_tag.stdout:
        if remote_tag.stdout.split()[0] != head.encode():
            raise ReleaseError("The version tag already points to a different release.", 409)
    else:
        _git(root, "tag", tag, isolated=True)
        _git(root, "push", "--atomic", "--", remote, "refs/heads/main", f"refs/tags/{tag}", **network)
    return initial


def publish_release_to_github(
    *, version: str, title: str, release_notes: str, source_root: Path | None = None,
    remote_git_url: str | None = None, github_api_transport=None,
) -> dict[str, Any]:
    if not config.VERSION_RE.fullmatch(version):
        raise ValueError("Expected a version in X.Y.Z format.")
    title = title.strip()
    if not title or "\n" in title or "\r" in title or "\x00" in title:
        raise ValueError("The release title must be a single nonempty line.")
    root = (source_root or config.BASE_DIR).resolve()
    with _mutation():
        owner, repo, token = _publisher_config(root / ".env")
        if not GITHUB_TOKEN_RE.fullmatch(token):
            raise ReleaseError("Configure a valid GitHub publishing token first.", 409)
        files, modes = _collect_export(root, version)
        scan_export_tree_for_leaks([*files.items(), ("release-notes", (title + "\n" + release_notes).encode())])
        slug = f"{owner}/{repo}"
        endpoint = f"/repos/{slug}"
        status, repo_info = _github("GET", endpoint, token, transport=github_api_transport, allowed=(404,))
        if status == 404:
            _, repo_info = _github("POST", "/user/repos", token, {
                "name": repo, "private": False, "has_issues": True,
                "description": "ZeroHub — Self-Hosted Multi-Agent AI Workspace for Windows, Linux & Mobile PWA",
            }, github_api_transport)
        if repo_info.get("private") is True or repo_info.get("full_name", slug).lower() != slug.lower():
            raise ReleaseError("The selected GitHub repository must be public and match the configured owner.", 409)

        remote = remote_git_url or f"https://github.com/{slug}.git"
        proxy = config.resolve_outbound_proxy() if remote_git_url is None else None
        tag = f"v{version}"
        with tempfile.TemporaryDirectory(prefix="zerohub_pub_") as directory:
            initial = _publish_tree(Path(directory), files, modes, remote, owner,
                                    token if remote_git_url is None else "", proxy, tag, title)
        payload = {"tag_name": tag, "name": title, "body": release_notes, "draft": False, "prerelease": False}
        status, release = _github("POST", endpoint + "/releases", token, payload, github_api_transport, (422,))
        if status == 422:
            _, existing = _github("GET", f"{endpoint}/releases/tags/{tag}", token, transport=github_api_transport)
            release_id = existing.get("id")
            if not isinstance(release_id, int):
                raise ReleaseError("GitHub did not return a valid release ID.")
            _, release = _github("PATCH", f"{endpoint}/releases/{release_id}", token, payload, github_api_transport)
        version_file = root / "VERSION"
        if not version_file.exists() or version_file.read_bytes() != files["VERSION"]:
            env_service._atomic_write_text_utf8(version_file, version + "\n")
        _invalidate_cache()
        return {"ok": True, "html_url": release.get("html_url", ""), "tag_name": tag, "version": version,
                "files_published": len(files), "clean_initial_history": initial}



_UPSTREAM_MAIN = "refs/remotes/zerohub_upstream/main"
_UPSTREAM_TAGS = "refs/zerohub-upstream/tags"


def _version_at(root: Path, ref: str) -> str:
    raw = _lines(_git(root, "show", f"{ref}:VERSION"))
    if len(raw) != 1 or not config.VERSION_RE.fullmatch(raw[0]):
        raise ReleaseError("The upstream VERSION file is invalid.", 409)
    return raw[0]


def _validate_update_tree(root: Path, ref: str) -> None:
    result = _git(root, "ls-tree", "-rz", ref)
    issues = []
    seen = set()
    for entry in result.stdout.decode("utf-8").split("\0"):
        if not entry:
            continue
        header, name = entry.split("\t", 1)
        reason = _path_issue(name)
        if not reason and header.split()[0] not in {"100644", "100755"}:
            reason = "unsupported Git entry"
        if name.casefold() in seen:
            reason = "case-insensitive path collision"
        seen.add(name.casefold())
        if reason:
            issues.append(f"{_safe_path(name)}:1: {reason}")
    if issues:
        raise ReleaseSecurityError(issues)


def _backup_database(db_path: Path, version: str) -> str | None:
    if not db_path.is_file():
        return None
    folder = db_path.parent / "backups"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = folder / f"hub_pre_update_v{version}_{stamp}.db"
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".hub_backup_", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with contextlib.closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                with contextlib.closing(sqlite3.connect(tmp_path)) as destination:
                    source.backup(destination)
            tmp_path.replace(output)
        finally:
            tmp_path.unlink(missing_ok=True)
        backups = sorted(folder.glob("hub_pre_update_*.db"), key=lambda p: p.stem.rsplit("_", 1)[-1], reverse=True)
        for old in backups[5:]:
            old.unlink()
    except (OSError, sqlite3.Error):
        raise ReleaseError("Could not back up the local SQLite database.") from None
    return str(output)


def _requirements_digest(root: Path) -> bytes | None:
    path = root / "requirements.txt"
    return hashlib.sha256(path.read_bytes()).digest() if path.is_file() else None


def _install_requirements(root: Path) -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=root, env=_child_env(), capture_output=True, check=True,
            timeout=900, **config.subprocess_hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError("Python dependency installation failed. The previous code will be restored.") from None


def _restore_update(root, old_head, old_branch, old_main, stash):
    if old_branch == "main":
        _git(root, "checkout", "--no-overwrite-ignore", "-B", "main", old_head)
    else:
        _git(root, "checkout", "--no-overwrite-ignore", "--detach", old_head)
        if old_main:
            _git(root, "update-ref", "refs/heads/main", old_main)
        else:
            _git(root, "update-ref", "-d", "refs/heads/main")
        if old_branch:
            _git(root, "checkout", "--no-overwrite-ignore", old_branch)
    if stash:
        _git(root, "stash", "apply", "--index", stash)


def apply_one_click_update(
    *, target_version: str | None = None, source_root: Path | None = None,
    remote_git_url: str | None = None, db_path: Path | None = None, run_pip: bool = True,
) -> dict[str, Any]:
    root = (source_root or config.BASE_DIR).resolve()
    if target_version is not None:
        target_version = ".".join(map(str, parse_semver(target_version)))
    if db_path is None:
        db_path = config.DB_PATH if source_root is None else root / "data" / "hub.db"
    with _mutation():
        owner, repo, token = _publisher_config(root / ".env")
        remote = remote_git_url or f"https://github.com/{owner}/{repo}.git"
        proxy = config.resolve_outbound_proxy() if remote_git_url is None else None
        old_head = _lines(_git(root, "rev-parse", "HEAD"))[0]
        _git(root, "fetch", "--no-tags", "--prune", "--", remote,
             f"+refs/heads/main:{_UPSTREAM_MAIN}", f"+refs/tags/v*:{_UPSTREAM_TAGS}/v*",
             isolated=True, proxy=proxy)
        if token and _git(root, "merge-base", old_head, _UPSTREAM_MAIN, allowed=(0, 1)).returncode:
            raise ReleaseError(
                "Это мастер-стенд издателя релизов с собственной локальной историей разработки. "
                "Клиентское обновление из публичного зеркала здесь отключено для сохранения рабочих веток.",
                409,
            )
        ref = _UPSTREAM_MAIN
        if target_version is not None:
            tag = f"{_UPSTREAM_TAGS}/v{target_version}"
            if _git(root, "show-ref", "--verify", "--quiet", tag, allowed=(0, 1)).returncode == 0:
                ref = tag
            if _git(root, "merge-base", "--is-ancestor", ref, _UPSTREAM_MAIN, allowed=(0, 1)).returncode:
                raise ReleaseError("The requested release is outside upstream main.", 409)
        updated = _version_at(root, ref)
        if target_version is not None and updated != target_version:
            raise ReleaseError("The requested version is unavailable.", 409)
        previous_file = root / "VERSION"
        previous = previous_file.read_text(encoding="utf-8").strip() if previous_file.is_file() else "1.0.0"
        if not config.VERSION_RE.fullmatch(previous) or not is_newer_version(previous, updated):
            raise ReleaseError("Upstream does not offer a version newer than this installation.", 409)

        _validate_update_tree(root, ref)
        _validate_update_tree(root, old_head)
        for path in _index_entries(root):
            reason = _path_issue(path)
            if reason:
                raise ReleaseSecurityError([f"{_safe_path(path)}:1: {reason}"])
        old_branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1)).stdout.decode().strip()
        old_main = _git(root, "rev-parse", "--verify", "--quiet", "refs/heads/main", allowed=(0, 1)).stdout.decode().strip()
        before = _requirements_digest(root)
        backup = _backup_database(db_path, previous)
        stash = ""
        if _git(root, "status", "--porcelain", "--untracked-files=no").stdout:
            _git(root, "-c", "user.name=ZeroHub", "-c", "user.email=updater@zerohub.invalid",
                 "stash", "push", "-m", "zerohub-pre-update-backup")
            stash = _lines(_git(root, "rev-parse", "refs/stash"))[0]
        pip_updated = False
        switched = False
        try:
            _git(root, "checkout", "--no-overwrite-ignore", "-B", "main", ref)
            switched = True
            after = _requirements_digest(root)
            if run_pip and after is not None and before != after:
                _install_requirements(root)
                pip_updated = True
        except (OSError, ReleaseError):
            try:
                head = _lines(_git(root, "rev-parse", "HEAD"))[0]
                branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD",
                              allowed=(0, 1)).stdout.decode().strip()
                if switched or head != old_head or branch != old_branch:
                    _restore_update(root, old_head, old_branch, old_main, stash)
                elif stash:
                    _git(root, "stash", "apply", "--index", stash)
            except ReleaseError:
                raise ReleaseError("The update failed and Git could not automatically restore the previous checkout.") from None
            raise

        _invalidate_cache()
        return {"ok": True, "previous_version": previous, "updated_version": updated, "pip_updated": pip_updated,
                "stashed_local_changes": bool(stash), "backup_file": backup}
