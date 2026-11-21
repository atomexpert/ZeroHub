"""Isolated coverage of checkpoints with ignored build files and nested repositories."""
import pytest

from app import git_service as service


def git(root, *args):
    rc, result, error = service._exec_git_raw(root, list(args))
    assert rc == 0, error
    return result.strip()


def write(root, name, content):
    file = root / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Offline fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    write(tmp_path, "src/main.js", "const initial = true;\n")
    write(tmp_path, ".gitignore", "# fixture\n")
    git(tmp_path, "add", "--", ".gitignore", "src/main.js")
    git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def seed_nested(repo):
    nested = repo / "Backup"
    nested.mkdir(exist_ok=True)
    git(nested, "init", "-q")
    write(repo, "Backup/private.txt", "nested work\n")
    return nested


def test_tracked_build_files_can_be_updated_and_removed_after_ignore(repo):
    android = "src-tauri/gen/android/app"
    changed = f"{android}/build/tracked.txt"
    removed = f"{android}/build/removed.txt"
    write(repo, changed, "old build\n")
    write(repo, removed, "obsolete build\n")
    git(repo, "add", "--", changed, removed)
    git(repo, "commit", "-qm", "tracked build artifacts")
    ignore = f"{android}/.gitignore"
    write(repo, ignore, "/build\n")
    write(repo, changed, "new build\n")
    (repo / removed).unlink()
    write(repo, "src/main.js", "const updated = true;\n")
    write(repo, f"{android}/build/ignored.txt", "local build\n")

    result = service.create_project_checkpoint(repo)
    assert result["created"]
    assert set(result["files"]) == {changed, removed, ignore, "src/main.js"}
    assert git(repo, "show", "HEAD:" + changed) == "new build"
    assert removed not in git(repo, "ls-tree", "-r", "--name-only", "HEAD")
    assert (repo / android / "build/ignored.txt").is_file()


def test_nested_git_is_hidden_excluded_and_left_on_disk(repo):
    nested = seed_nested(repo)
    write(repo, ".gitignore", "Backup/\n")
    write(repo, "src/main.js", "const next = true;\n")
    status = service.get_project_git_status(repo)
    assert {item["path"] for item in status["changed_files"]} == {"src/main.js", ".gitignore"}
    assert status["ignored_or_blocked_count"] >= 1
    assert service.get_project_git_diff(repo, path="Backup/private.txt")["blocked"]
    assert service.create_project_checkpoint(repo)["created"]
    assert "Backup" not in git(repo, "ls-tree", "HEAD")
    write(repo, "src/main.js", "const dirty = true;\n")
    assert service.rollback_project_to_checkpoint(repo, "discard_uncommitted")["backup_stash_created"]
    assert (nested / ".git").is_dir()
    assert (nested / "private.txt").read_text() == "nested work\n"


def test_new_ignore_and_source_commit_without_untracked_dist(repo):
    write(repo, ".gitignore", "dist/\n")
    write(repo, "dist/asset.js", "ignored\n")
    write(repo, "app.py", "example = True\n")
    assert service._filter_out_git_ignored_paths(repo, ["dist/asset.js", "app.py"]) == ["app.py"]
    assert set(service.create_project_checkpoint(repo)["files"]) == {".gitignore", "app.py"}
    assert "dist" not in git(repo, "ls-tree", "HEAD")


@pytest.mark.parametrize("mode", ["discard_uncommitted", "restore_checkpoint"])
def test_rollback_preserves_nested_git_and_ignored_builds(repo, mode):
    write(repo, "Backup/tracked.txt", "historical content\n")
    git(repo, "add", "--", "Backup/tracked.txt")
    git(repo, "commit", "-qm", "before nesting")
    previous = git(repo, "rev-parse", "HEAD")
    write(repo, "Backup/tracked.txt", "parent new content\n")
    write(repo, "src/main.js", "const checkpoint = true;\n")
    git(repo, "add", "--", "Backup/tracked.txt", "src/main.js")
    git(repo, "commit", "-qm", "newer checkpoint")
    latest = git(repo, "rev-parse", "HEAD")
    nested = seed_nested(repo)
    write(repo, "Backup/tracked.txt", "nested worktree edit\n")
    write(repo, ".gitignore", "Backup/\ndist/\n")
    write(repo, "dist/local.txt", "compiler output\n")
    write(repo, "src/main.js", "const dirty = true;\n")
    write(repo, "new.txt", "temporary edit\n")

    assert "diff --git a/Backup/" not in service.get_project_git_diff(repo)["diff"]
    assert "Backup/" not in service.get_project_git_diff(repo, commit=latest)["diff"]
    result = service.rollback_project_to_checkpoint(repo, mode, previous if mode == "restore_checkpoint" else "")
    assert result["ok"] and result["backup_stash_created"]
    expected = "const initial = true;\n" if mode == "restore_checkpoint" else "const checkpoint = true;\n"
    assert (repo / "src/main.js").read_text() == expected
    assert (nested / ".git").is_dir()
    assert (nested / "tracked.txt").read_text() == "nested worktree edit\n"
    assert git(repo, "show", "HEAD:Backup/tracked.txt") == "parent new content"
    assert (repo / "dist/local.txt").read_text() == "compiler output\n"


def test_unignored_nested_git_file_is_not_added_as_gitlink(repo):
    nested = seed_nested(repo)
    git_dir = nested / ".git"
    external = repo / ".git" / "nested-git-dir"
    git_dir.rename(external)
    git_dir.write_text(f"gitdir: {external.as_posix()}\n", encoding="utf-8")
    assert service._is_nested_git_repo_path(repo, "Backup/private.txt")
    assert service.get_project_git_status(repo)["ignored_or_blocked_count"] >= 1
    service.create_project_checkpoint(repo)
    assert "Backup" not in git(repo, "ls-tree", "HEAD")


def test_ignore_change_between_status_and_stage_keeps_checkpoint(repo, monkeypatch):
    write(repo, "keep.txt", "save this\n")
    write(repo, "race.txt", "ignore this\n")
    real_exec = service._exec_git_raw

    def exec_git(root, args, **kwargs):
        if args[:3] == ["--literal-pathspecs", "add", "--"] and "race.txt" in args:
            write(repo, ".gitignore", "race.txt\n")
        return real_exec(root, args, **kwargs)

    monkeypatch.setattr(service, "_exec_git_raw", exec_git)
    result = service.create_project_checkpoint(repo)
    assert result["files"] == ["keep.txt"]
    assert (repo / "race.txt").is_file()



def test_discard_staged_addition_missing_on_disk(repo):
    write(repo, "staged.txt", "not yet committed\n")
    git(repo, "add", "--", "staged.txt")
    (repo / "staged.txt").unlink()
    write(repo, "src/main.js", "const saved = true;\n")
    result = service.create_project_checkpoint(repo)
    assert result["files"] == ["src/main.js"]


def test_restore_preserves_ignored_collision_after_stashing_ignore_rule(repo):
    write(repo, "dist/out.txt", "old tracked result\n")
    git(repo, "add", "--", "dist/out.txt")
    git(repo, "commit", "-qm", "old build result")
    target = git(repo, "rev-parse", "HEAD")
    git(repo, "rm", "--", "dist/out.txt")
    git(repo, "commit", "-qm", "remove build result")
    write(repo, ".gitignore", "dist/\n")
    write(repo, "dist/out.txt", "keep local compiler output\n")

    assert service.rollback_project_to_checkpoint(repo, "restore_checkpoint", target)["ok"]
    assert (repo / "dist/out.txt").read_text() == "keep local compiler output\n"
    assert not git(repo, "ls-files", "dist/out.txt")
