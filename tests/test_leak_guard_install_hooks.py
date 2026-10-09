"""#17: hooks a checkout cannot remove, installed by the package, driven through REAL git.

`--install-hooks` writes two shims into the repository's git directory; `--pre-commit` and
`--pre-push` are what they call. Every behavioural test here runs an actual `git commit` or
`git push`, because the property under test is that GIT runs the hook — a test that called the
Python entry points directly would pass on a hook git never invokes.

⚠️ Every leaking literal is assembled at runtime; this file is scanned by the guard it tests.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from kw_common import leakguard as guard

_ADDR = "192.168." + "77.77"
_HOST = "host-a." + "lan"

_PINNED = ("-c", "user.name=guard test", "-c", "user.email=t@example.com",
           "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
           "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *_PINNED, *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, check=check)


def _guard(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "kw_common.leakguard", "--repo", str(repo),
                           *args], capture_output=True, text=True, timeout=300)


def _repo(tmp_path: Path, name: str = "work") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_bytes(b"clean\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _hooks(repo: Path) -> Path:
    out = _git(repo, "rev-parse", "--git-path", "hooks").stdout.strip()
    return (repo / out).resolve()


# ------------------------------------------------------------------------------- installing


@pytest.mark.timeout(300)
def test_install_writes_both_hooks_into_the_git_directory_and_check_says_active(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    res = _guard(repo, "--install-hooks")
    assert res.returncode == 0, res.stdout + res.stderr
    for name in ("pre-commit", "pre-push"):
        text = (_hooks(repo) / name).read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh\n") and guard._HOOK_MARK in text, text
        assert Path(sys.executable).absolute().as_posix() in text, text
        assert "\r" not in text
    assert _guard(repo, "--check-hooks").returncode == 0
    # Idempotent: installing over its own hooks is an update, not a refusal.
    assert _guard(repo, "--install-hooks").returncode == 0


@pytest.mark.timeout(300)
def test_a_COMMIT_carrying_a_leak_is_refused_by_git_and_survives_an_OLD_checkout(
    tmp_path: Path,
) -> None:
    """⭐ #17 itself. The hooks live in the git directory, so checking out a commit from before
    they existed (here: the root commit, and an orphan branch) leaves them in force. With the old
    `core.hooksPath .githooks` install the same checkout left no hook at all."""
    repo = _repo(tmp_path)
    assert _guard(repo, "--install-hooks").returncode == 0
    root = _git(repo, "rev-parse", "HEAD").stdout.strip()

    for checkout in (("checkout", "-q", root), ("checkout", "-q", "--orphan", "fresh")):
        _git(repo, *checkout)
        (repo / "notes.md").write_bytes(f"AGENT={_ADDR}\n".encode())
        _git(repo, "add", "notes.md")
        before = _git(repo, "rev-parse", "-q", "--verify", "HEAD", check=False).stdout.strip()
        res = _git(repo, "commit", "-qm", "leak", check=False)
        assert res.returncode != 0, f"git committed a leak after {checkout}: {res.stdout}"
        assert "notes.md:1: private IPv4 (RFC1918)" in res.stdout + res.stderr
        assert _git(repo, "rev-parse", "-q", "--verify", "HEAD",
                    check=False).stdout.strip() == before
        _git(repo, "rm", "-q", "--cached", "notes.md")
        (repo / "notes.md").unlink()

    (repo / "notes.md").write_bytes(b"clean\n")
    _git(repo, "add", "notes.md")
    assert _git(repo, "commit", "-qm", "clean", check=False).returncode == 0


@pytest.mark.timeout(300)
def test_the_pre_commit_hook_scans_the_INDEX_not_only_the_worktree(tmp_path: Path) -> None:
    """A leak staged and then tidied in the worktree without re-staging: the tree scan is clean,
    the commit would record the leak. The hook runs `--staged` too, so git refuses it."""
    repo = _repo(tmp_path)
    assert _guard(repo, "--install-hooks").returncode == 0
    (repo / "cfg.txt").write_bytes(f"AGENT={_ADDR}\n".encode())
    _git(repo, "add", "cfg.txt")
    (repo / "cfg.txt").write_bytes(b"clean\n")
    res = _git(repo, "commit", "-qm", "staged leak", check=False)
    assert res.returncode != 0, res.stdout + res.stderr
    assert "STAGED FOR COMMIT" in res.stdout + res.stderr


@pytest.mark.timeout(300)
def test_a_linked_worktree_runs_the_same_hooks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _guard(repo, "--install-hooks").returncode == 0
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(linked), "-b", "side")
    assert _hooks(linked) == _hooks(repo)
    (linked / "notes.md").write_bytes(f"host {_HOST}\n".encode())
    _git(linked, "add", "notes.md")
    assert _git(linked, "commit", "-qm", "leak", check=False).returncode != 0


@pytest.mark.timeout(300)
def test_install_REFUSES_while_core_hooksPath_is_set_and_check_says_inactive(
    tmp_path: Path,
) -> None:
    """git ignores the git directory's hooks while `core.hooksPath` is set, so installing there
    would be the silent absence #17 is about, reached a different way."""
    repo = _repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".githooks")
    res = _guard(repo, "--install-hooks")
    assert res.returncode == 1 and "core.hooksPath is set" in res.stdout, res.stdout
    assert not (_hooks(repo) / "pre-commit").exists() or guard._HOOK_MARK not in (
        _hooks(repo) / "pre-commit").read_text(encoding="utf-8", errors="replace")
    assert _guard(repo, "--check-hooks").returncode == 1


@pytest.mark.timeout(300)
def test_install_never_overwrites_a_hook_it_did_not_write(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    foreign = _hooks(repo) / "pre-push"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_bytes(b"#!/bin/sh\necho mine\n")
    res = _guard(repo, "--install-hooks")
    assert res.returncode == 1 and "was not written by this command" in res.stdout, res.stdout
    assert foreign.read_bytes() == b"#!/bin/sh\necho mine\n"
    assert not (_hooks(repo) / "pre-commit").exists(), "a partial install was left behind"


@pytest.mark.timeout(300)
def test_check_FAILS_when_the_interpreter_a_hook_names_is_gone(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _guard(repo, "--install-hooks").returncode == 0
    hook = _hooks(repo) / "pre-commit"
    hook.write_bytes(guard._hook_text("pre-commit", (tmp_path / "no" / "python").as_posix())
                     .encode())
    res = _guard(repo, "--check-hooks")
    assert res.returncode == 1 and "cannot import kw_common.leakguard" in res.stdout, res.stdout
    # ...and git, running that hook, REFUSES rather than passing.
    (repo / "notes.md").write_bytes(b"clean\n")
    _git(repo, "add", "notes.md")
    assert _git(repo, "commit", "-qm", "x", check=False).returncode != 0


# ---------------------------------------------------------------------------------- pushing


def _with_remote(tmp_path: Path) -> tuple[Path, Path]:
    repo = _repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, timeout=120)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    assert _guard(repo, "--install-hooks").returncode == 0
    return repo, remote


def _remote_sha(remote: Path, ref: str) -> str:
    return subprocess.run(["git", "rev-parse", "-q", "--verify", ref], cwd=remote,
                          capture_output=True, text=True, timeout=120).stdout.strip()


@pytest.mark.timeout(300)
def test_a_PUSH_of_a_commit_carrying_a_leak_is_refused_and_the_remote_does_not_move(
    tmp_path: Path,
) -> None:
    repo, remote = _with_remote(tmp_path)
    before = _remote_sha(remote, "refs/heads/main")
    (repo / "notes.md").write_bytes(f"AGENT={_ADDR}\n".encode())
    _git(repo, "add", "notes.md")
    _git(repo, "-c", "core.hooksPath=/nonexistent", "commit", "-qm", "leak")  # past pre-commit
    (repo / "notes.md").write_bytes(b"clean\n")
    _git(repo, "-c", "core.hooksPath=/nonexistent", "commit", "-qam", "removes it")
    res = _git(repo, "push", "origin", "main", check=False)
    assert res.returncode != 0, res.stdout + res.stderr
    assert "pushing publishes HISTORY" in res.stdout + res.stderr
    assert _remote_sha(remote, "refs/heads/main") == before


@pytest.mark.timeout(300)
def test_a_clean_push_a_new_branch_and_a_deletion_go_through(tmp_path: Path) -> None:
    repo, remote = _with_remote(tmp_path)
    (repo / "more.md").write_bytes(b"clean\n")
    _git(repo, "add", "more.md")
    _git(repo, "commit", "-qm", "clean")
    assert _git(repo, "push", "-q", "origin", "main", check=False).returncode == 0
    _git(repo, "switch", "-q", "-c", "side")
    assert _git(repo, "push", "-q", "origin", "side", check=False).returncode == 0
    assert _git(repo, "push", "-q", "origin", "--delete", "side", check=False).returncode == 0
    assert _remote_sha(remote, "refs/heads/side") == ""


@pytest.mark.timeout(300)
def test_a_TAG_push_whose_annotation_leaks_is_refused(tmp_path: Path) -> None:
    """A tag cut at an already-pushed commit adds no commits; the tag OBJECT is what publishes."""
    repo, remote = _with_remote(tmp_path)
    _git(repo, "tag", "-a", "v1.0.0", "-m", f"cut on {_HOST}")
    res = _git(repo, "push", "origin", "v1.0.0", check=False)
    assert res.returncode != 0 and "<tag object>" in res.stdout + res.stderr, res.stderr
    assert _remote_sha(remote, "refs/tags/v1.0.0") == ""


def test_an_unparseable_ref_line_is_a_refusal(tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    assert guard._pre_push(tmp_path, "origin", "only three fields\n", []) == 1
    assert "cannot parse" in capsys.readouterr().out


@pytest.mark.parametrize("argv,says", [
    (["--pre-commit", "--staged"], "do different things"),
    (["--install-hooks", "--check-hooks"], "do different things"),
    (["--pre-push", "origin", "--range", "A..B"], "do different things"),
    (["--install-hooks", "--config", "x.json"], "--config would be ignored"),
    (["--check-hooks", "--help"], "--help does not combine"),
    (["--pre-push"], "--pre-push needs a value"),
])
def test_the_new_modes_are_exclusive_and_strict(argv: list[str], says: str) -> None:
    """Each refusal for its OWN reason — an unknown flag would also raise, which proves nothing."""
    with pytest.raises(guard.UsageError, match=re.escape(says)):
        guard.parse_args(argv)
    assert guard.parse_args(argv[:1] + (["origin"] if argv[0] == "--pre-push" else []))


@pytest.mark.timeout(300)
def test_an_EMPTY_core_hooksPath_turns_hooks_off_and_is_refused(tmp_path: Path) -> None:
    """`core.hooksPath=` (empty) at any scope runs NO hooks, and `--git-path hooks` then answers
    the worktree root. It is a setting like any other, not "unset"."""
    repo = _repo(tmp_path)
    _git(repo, "config", "core.hooksPath", "")
    res = _guard(repo, "--install-hooks")
    assert res.returncode == 1 and "core.hooksPath is set" in res.stdout, res.stdout
    assert not (repo / "pre-commit").exists(), "the shims were written into the worktree"
    assert _guard(repo, "--check-hooks").returncode == 1


@pytest.mark.timeout(300)
def test_a_kw_common_package_in_the_REPOSITORY_cannot_replace_the_guard(tmp_path: Path) -> None:
    """Hooks run from the worktree root, and `python -m` puts the current directory first on
    `sys.path`. The shim runs isolated (`-I`), so a committed `kw_common/` is never imported."""
    repo = _repo(tmp_path)
    assert _guard(repo, "--install-hooks").returncode == 0
    (repo / "kw_common").mkdir()
    (repo / "kw_common" / "__init__.py").write_bytes(b"")
    (repo / "kw_common" / "leakguard.py").write_bytes(b"import sys\nprint('shadow')\nsys.exit(0)\n")
    _git(repo, "add", "kw_common")
    _git(repo, "-c", "core.hooksPath=/nonexistent", "commit", "-qm", "shadow package")
    (repo / "l.txt").write_bytes(f"AGENT={_ADDR}\n".encode())
    _git(repo, "add", "l.txt")
    res = _git(repo, "commit", "-m", "leak", check=False)
    assert res.returncode != 0 and "shadow" not in res.stdout + res.stderr, res.stdout
    assert _guard(repo, "--check-hooks").returncode == 0


@pytest.mark.timeout(300)
def test_a_TAG_on_a_BLOB_or_TREE_is_refused_on_push(tmp_path: Path) -> None:
    """A range scan walks commits, so content a tag publishes directly is read by nothing."""
    repo, remote = _with_remote(tmp_path)
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo,
                          input=f"AGENT={_ADDR}\n".encode(), capture_output=True,
                          check=True).stdout.decode().strip()
    tree = subprocess.run(["git", "mktree"], cwd=repo, input=f"100644 blob {blob}\tf\n".encode(),
                          capture_output=True, check=True).stdout.decode().strip()
    for name, target in (("blobtag", blob), ("treetag", tree)):
        _git(repo, "tag", name, target)
        res = _git(repo, "push", "origin", name, check=False)
        assert res.returncode != 0 and "not a commit" in res.stdout + res.stderr, res.stderr
        assert _remote_sha(remote, f"refs/tags/{name}") == ""


@pytest.mark.timeout(300)
def test_a_push_to_a_PATH_with_whitespace_still_scans_the_new_commits(tmp_path: Path) -> None:
    """git hands the hook the URL when there is no remote name; split on whitespace it became
    extra `--not <word>` revisions and excluded the leak."""
    repo, _ = _with_remote(tmp_path)
    spaced = tmp_path / "x feat"
    subprocess.run(["git", "init", "-q", "--bare", str(spaced)], check=True, timeout=120)
    _git(repo, "switch", "-q", "-c", "feat")
    (repo / "l.txt").write_bytes(f"AGENT={_ADDR}\n".encode())
    _git(repo, "add", "l.txt")
    _git(repo, "-c", "core.hooksPath=/nonexistent", "commit", "-qm", "leak")
    res = _git(repo, "push", str(spaced), "feat", check=False)
    assert res.returncode != 0, res.stdout + res.stderr


def test_the_shim_quotes_any_interpreter_path_and_check_reads_it_back(tmp_path: Path) -> None:
    """`$`, a backtick and a quote in the interpreter path are literal to the hook's shell, and
    `--check-hooks` recovers exactly that path from the shim."""
    odd = "/opt/py$HOME/`id`/it's/python"
    text = guard._hook_text("pre-commit", odd)
    assert "exec '/opt/py$HOME/`id`/it'\"'\"'s/python' -I -m kw_common.leakguard" in text
    found = guard._SHIM_EXEC.search(text)
    assert found and found.group(1).replace("'\"'\"'", "'") == odd


@pytest.mark.timeout(300)
def test_check_FAILS_on_a_shim_EDITED_after_install(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _guard(repo, "--install-hooks").returncode == 0
    hook = _hooks(repo) / "pre-commit"
    text = hook.read_text(encoding="utf-8")
    for edited in (text.replace("#!/bin/sh\n", "#!/bin/sh\nexit 0\n"),
                   text.replace("--pre-commit", "--selftest")):
        hook.write_bytes(edited.encode())
        res = _guard(repo, "--check-hooks")
        assert res.returncode == 1 and "not exactly as --install-hooks" in res.stdout, res.stdout
