"""D104: `--pre-push` refuses a push whose published ref NAME matches a content rule.

Driven through the command the installed hook runs, with git's ref list on stdin. The remote's
name is the one checked: a refspec can rename, so a clean local name can publish a leaking one.

⚠️ Every literal a rule would match is assembled from fragments, because this file is scanned by
the guard it tests.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HOST = "not-a-real-host" + ".lan"                          # matches `private lan domain`
UUID = "3f2a9c1e-" + "7b4d-4e8a-9c2f-1a2b3c4d5e6f"         # matches the tenant-id rule
ZERO = "0" * 40
_PINNED = ("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
           "-c", "init.defaultBranch=main")


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "work"
    repo.mkdir()
    subprocess.run(["git", *_PINNED, "init", "-q"], cwd=repo, check=True, timeout=120)
    (repo / "a.md").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", *_PINNED, "add", "a.md"], cwd=repo, check=True, timeout=120)
    subprocess.run(["git", *_PINNED, "commit", "-qm", "clean"], cwd=repo, check=True, timeout=120)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                         check=True, timeout=120).stdout.strip()
    return repo, sha


def _pre_push(repo: Path, refs: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "kw_common.leakguard", "--pre-push", "origin"],
                          cwd=repo, input=refs, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)


@pytest.mark.timeout(300)
def test_a_CLEAN_ref_name_is_NOT_refused(tmp_path: Path) -> None:
    """The false-red direction first: a correct name must pass."""
    repo, sha = _repo(tmp_path)
    res = _pre_push(repo, f"refs/heads/main {sha} refs/heads/main {ZERO}\n")
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.timeout(300)
def test_a_REMOTE_name_that_matches_is_REFUSED_and_the_ref_is_named(tmp_path: Path) -> None:
    repo, sha = _repo(tmp_path)
    res = _pre_push(repo, f"refs/heads/main {sha} refs/tags/{HOST} {ZERO}\n")
    assert res.returncode == 1, res.stdout + res.stderr
    # The ref is named with the matched part replaced by its rule's label, never the text itself.
    assert "pre-push REFUSED: refs/tags/<private lan domain>" in res.stdout, res.stdout
    assert HOST not in res.stdout, res.stdout


@pytest.mark.timeout(300)
def test_a_CLEAN_local_name_publishing_a_MATCHING_remote_name_is_REFUSED(tmp_path: Path) -> None:
    """The rename case. Checking only the local name would pass this push."""
    repo, sha = _repo(tmp_path)
    res = _pre_push(repo, f"refs/heads/main {sha} refs/heads/{HOST} {ZERO}\n")
    assert res.returncode == 1, res.stdout + res.stderr


@pytest.mark.timeout(300)
def test_a_MATCHING_local_name_that_publishes_a_CLEAN_remote_name_is_NOT_refused(
    tmp_path: Path,
) -> None:
    """Only the published name counts. The local name is not sent to the remote."""
    repo, sha = _repo(tmp_path)
    res = _pre_push(repo, f"refs/tags/{HOST} {sha} refs/tags/v1.0.0 {ZERO}\n")
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.timeout(300)
def test_a_UUID_shaped_branch_name_is_REFUSED(tmp_path: Path) -> None:
    repo, sha = _repo(tmp_path)
    res = _pre_push(repo, f"refs/heads/release/{UUID} {sha} refs/heads/release/{UUID} {ZERO}\n")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "uuid" in res.stdout, res.stdout


@pytest.mark.timeout(300)
def test_DELETING_a_matching_name_publishes_nothing_and_is_NOT_refused(tmp_path: Path) -> None:
    """A deletion sends no name. Refusing it would block the cleanup of the very leak it names."""
    repo, sha = _repo(tmp_path)
    res = _pre_push(repo, f"(delete) {ZERO} refs/tags/{HOST} {sha}\n")
    assert res.returncode == 0, res.stdout + res.stderr
