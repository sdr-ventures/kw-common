"""Workflow steps, EXECUTED rather than grepped.

* #37: the release workflow publishes a `vX.Y.ZrcN` tag as a PRE-RELEASE of `X.Y.Z`.
* the reusable `leak-guard.yml`: what range each triggering event scans — above all the TAG
  path, whose base is the previous `v[0-9]*` tag and whose range names the tag object.

Each step's `run:` block is lifted out of its workflow file and run under bash with the variables
GitHub sets. A grep would pass on a script that never ran.

⚠️ Every leaking literal is assembled at runtime; this file is scanned by the guard.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _bash() -> str | None:
    """git's own bash on Windows (PATH order can put WSL's `bash.exe` first), PATH elsewhere."""
    if os.name == "nt":
        git = shutil.which("git")
        for base in ([Path(git).resolve().parents[1], Path(git).resolve().parents[2]]
                     if git else []):
            candidate = base / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
        return None
    return shutil.which("bash")


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no bash to run the workflow step with")


def step_script(workflow: Path, name: str) -> str:
    """The dedented `run: |` body of the step called `name`."""
    lines = workflow.read_text(encoding="utf-8").split("\n")
    i = next(n for n, line in enumerate(lines) if line.strip() == f"- name: {name}")
    indent = len(lines[i]) - len(lines[i].lstrip())
    j = next(n for n in range(i + 1, len(lines)) if lines[n].strip() == "run: |")
    body: list[str] = []
    for line in lines[j + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


def _run_version_step(tmp_path: Path, tag: str, package: str) -> tuple[int, str, dict[str, str]]:
    init = tmp_path / "src" / "kw_common" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text(f'__version__ = "{package}"\n', encoding="utf-8")
    out_file = tmp_path / "github_output"
    out_file.write_text("", encoding="utf-8")
    env = {**os.environ, "GITHUB_REF_NAME": tag, "GITHUB_OUTPUT": str(out_file),
           # `python` in the step must be THIS interpreter, never a Store stub.
           "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")}
    script = step_script(RELEASE, "The tag must match the package version")
    assert BASH is not None
    proc = subprocess.run([BASH, "-c", script], cwd=tmp_path, env=env, capture_output=True,
                          text=True, timeout=120)
    outputs = dict(line.split("=", 1) for line in
                   out_file.read_text(encoding="utf-8").splitlines() if "=" in line)
    return proc.returncode, proc.stdout + proc.stderr, outputs


@needs_bash
@pytest.mark.parametrize("tag,package,prerelease", [
    ("v1.7.0", "1.7.0", "false"),
    ("v1.7.0rc1", "1.7.0", "true"),
    ("v1.7.0rc12", "1.7.0", "true"),
    ("v1.7.0rc2", "1.7.0rc2", "true"),
])
def test_a_release_or_a_candidate_for_it_is_accepted(tmp_path: Path, tag: str, package: str,
                                                      prerelease: str) -> None:
    code, out, outputs = _run_version_step(tmp_path, tag, package)
    assert code == 0, out
    assert outputs == {"version": package, "prerelease": prerelease}, (outputs, out)


@needs_bash
@pytest.mark.parametrize("tag,package", [
    ("v1.7.1", "1.7.0"),
    ("v1.7.1rc1", "1.7.0"),     # a candidate for a DIFFERENT version
    ("v1.7.0rc", "1.7.0"),      # no candidate number
    ("v1.7.0-rc1", "1.7.0"),    # not the PEP 440 spelling the fleet pins
    ("v1.7.0rc1x", "1.7.0"),
])
def test_any_other_tag_is_refused_and_publishes_nothing(tmp_path: Path, tag: str,
                                                        package: str) -> None:
    code, out, outputs = _run_version_step(tmp_path, tag, package)
    assert code != 0, f"{tag} was accepted against {package}: {out}"
    assert outputs == {}, f"a refused tag still wrote outputs: {outputs}"


def test_publish_reads_the_prerelease_flag_and_the_package_version_from_build() -> None:
    """The flag is only worth computing if `publish` uses it — this is what was hardcoded."""
    text = RELEASE.read_text(encoding="utf-8")
    assert "prerelease: ${{ needs.build.outputs.prerelease == 'true' }}" in text
    assert "prerelease: false" not in text
    assert "VERSION: ${{ needs.build.outputs.version }}" in text
    assert "PKG_VERSION: ${{ steps.version.outputs.version }}" in text


# ------------------------------------------------- the reusable leak-guard workflow

REUSABLE = REPO_ROOT / ".github" / "workflows" / "leak-guard.yml"
_ADDR = "192.168." + "77.77"
_PINNED = ("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
           "-c", "tag.gpgsign=false", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main")


def _g(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *_PINNED, *args], cwd=repo, capture_output=True, text=True,
                          check=True, timeout=120).stdout.strip()


def _commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_bytes(body.encode())
    _g(repo, "add", name)
    _g(repo, "commit", "-qm", f"add {name}")
    return _g(repo, "rev-parse", "HEAD")


def _scan_step(repo: Path, **env: str) -> tuple[int, str]:
    """Run 'Scan what this event publishes' in `repo`, with `kw-leak-guard` on PATH."""
    script = step_script(REUSABLE, "Scan what this event publishes")
    full = {**os.environ, "EVENT": "", "REF": "", "PR_BASE": "", "PR_HEAD": "",
            "PUSH_BEFORE": "", "HEAD_SHA": "", **env,
            "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")}
    assert BASH is not None
    proc = subprocess.run([BASH, "-c", script], cwd=repo, env=full, capture_output=True,
                          text=True, timeout=300)
    return proc.returncode, proc.stdout + proc.stderr


@needs_bash
@pytest.mark.timeout(300)
def test_a_TAG_scans_from_the_previous_release_tag_and_reads_the_tag_object(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "tags"
    repo.mkdir()
    _g(repo, "init", "-q")
    first = _commit(repo, "a.md", "clean\n")
    _g(repo, "tag", "-a", "v1.0.0", "-m", "first release")
    second = _commit(repo, "b.md", "clean\n")
    _g(repo, "tag", "-a", "v1.1.0", "-m", f"cut on {_ADDR}")

    code, out = _scan_step(repo, EVENT="push", REF="refs/tags/v1.1.0", HEAD_SHA=second)
    assert "range: v1.0.0^{commit}..refs/tags/v1.1.0" in out, out
    assert code == 1 and "<tag object>" in out and _ADDR not in out, out

    third = _commit(repo, "c.md", "clean\n")
    _g(repo, "tag", "-a", "v1.2.0", "-m", "clean release")
    code, out = _scan_step(repo, EVENT="push", REF="refs/tags/v1.2.0", HEAD_SHA=third)
    assert code == 0 and "range: v1.1.0^{commit}..refs/tags/v1.2.0" in out, out

    # A twin tag at the same commit must not become the base and empty the range.
    _g(repo, "tag", "-a", "v1.2.1", "-m", "twin")
    code, out = _scan_step(repo, EVENT="push", REF="refs/tags/v1.2.1", HEAD_SHA=third)
    assert code == 0 and "range: v1.1.0^{commit}..refs/tags/v1.2.1" in out, out
    assert first  # the base commit exists; the first tag is what the first case scanned from


@needs_bash
@pytest.mark.timeout(300)
def test_the_FIRST_tag_scans_all_history_rather_than_an_empty_range(tmp_path: Path) -> None:
    repo = tmp_path / "first"
    repo.mkdir()
    _g(repo, "init", "-q")
    _commit(repo, "a.md", f"AGENT={_ADDR}\n")
    head = _commit(repo, "a.md", "clean\n")
    _g(repo, "tag", "-a", "v0.1.0", "-m", "first")
    code, out = _scan_step(repo, EVENT="push", REF="refs/tags/v0.1.0", HEAD_SHA=head)
    assert "range: refs/tags/v0.1.0" in out, out
    assert code == 1 and "a.md:1: private IPv4 (RFC1918)" in out, out


@needs_bash
@pytest.mark.timeout(300)
def test_a_PULL_REQUEST_and_a_PUSH_scan_exactly_their_new_commits(tmp_path: Path) -> None:
    repo = tmp_path / "events"
    repo.mkdir()
    _g(repo, "init", "-q")
    base = _commit(repo, "a.md", "clean\n")
    leaky = _commit(repo, "b.md", f"AGENT={_ADDR}\n")
    clean = _commit(repo, "b.md", "clean\n")

    code, out = _scan_step(repo, EVENT="pull_request", PR_BASE=base, PR_HEAD=clean)
    assert code == 1 and f"range: {base}..{clean}" in out, out
    code, out = _scan_step(repo, EVENT="push", REF="refs/heads/main", PUSH_BEFORE=leaky,
                           HEAD_SHA=clean)
    assert code == 0 and f"range: {leaky}..{clean}" in out, out
    code, out = _scan_step(repo, EVENT="push", REF="refs/heads/new", PUSH_BEFORE="0" * 40,
                           HEAD_SHA=leaky)
    assert code == 1 and f"range: {leaky}^..{leaky}" in out, out
    code, out = _scan_step(repo, EVENT="workflow_dispatch", REF="refs/heads/main",
                           HEAD_SHA=clean)
    assert code == 1 and f"range: {clean}\n" in out, out


@needs_bash
@pytest.mark.parametrize("version", ["main", "v1.7", "1.7.0", "abc1234", "v1.7.0-rc1",
                                     "v1.7.0; echo pwned"])
def test_the_install_step_refuses_a_pin_that_can_move_or_is_malformed(tmp_path: Path,
                                                                      version: str) -> None:
    script = step_script(REUSABLE, "Install the pinned guard")
    assert BASH is not None
    proc = subprocess.run([BASH, "-c", script], cwd=tmp_path, capture_output=True, text=True,
                          env={**os.environ, "VERSION": version,
                               "RUNNER_TEMP": str(tmp_path), "GITHUB_PATH": str(tmp_path / "p")},
                          timeout=120)
    assert proc.returncode == 1 and "is not a release tag" in proc.stdout, proc.stdout
    assert not (tmp_path / "kw-leak-guard").exists(), "it installed before validating"


def test_the_reusable_workflow_is_callable_and_names_its_check_like_ci() -> None:
    text = REUSABLE.read_text(encoding="utf-8")
    assert "workflow_call:" in text and "version:" in text and "required: true" in text
    assert "name: No internal info (leak guard)" in text
    assert "fetch-depth: 0" in text and "persist-credentials: false" in text
    assert "kw-leak-guard --selftest" in text and "kw-leak-guard --range" in text


@needs_bash
@pytest.mark.timeout(300)
def test_the_RELEASE_scan_does_not_re_read_the_PREVIOUS_tags_annotation(tmp_path: Path) -> None:
    """A tag cut after a leaky earlier tag must be judged on what IT publishes. With the base
    named as a tag, the guard read the base's tag object too and reddened every later release."""
    repo = tmp_path / "rel"
    repo.mkdir()
    _g(repo, "init", "-q")
    _commit(repo, "a.md", "clean\n")
    _g(repo, "tag", "-a", "v1.0.0", "-m", f"cut on {_ADDR}")
    _commit(repo, "b.md", "clean\n")
    _g(repo, "tag", "-a", "v1.1.0", "-m", "clean")
    script = step_script(RELEASE, "Scan the commits AND THE TAG OBJECT this tag would publish")
    assert BASH is not None
    proc = subprocess.run([BASH, "-c", script], cwd=repo, capture_output=True, text=True,
                          timeout=300, env={**os.environ, "GITHUB_REF": "refs/tags/v1.1.0",
                                            "PATH": os.path.dirname(sys.executable) + os.pathsep
                                            + os.environ.get("PATH", "")})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0 and "v1.0.0^{commit}..refs/tags/v1.1.0" in out, out


def test_ADOPTION_names_the_workflow_and_check_that_actually_exist() -> None:
    """The adoption doc is what an operator copies; its job id, workflow path and check name must
    be the real ones, or every adopting repository protects a check that never reports."""
    doc = (REPO_ROOT / "docs" / "ADOPTION.md").read_text(encoding="utf-8")
    assert "uses: sdr-ventures/kw-common/.github/workflows/leak-guard.yml@vX.Y.Z" in doc
    assert "  leak-guard:\n    uses:" in doc
    check = "No internal info (leak guard)"
    assert f"name: {check}" in REUSABLE.read_text(encoding="utf-8")
    assert f"leak-guard / {check}" in doc
    for flag in ("--install-hooks", "--check-hooks", "--pre-commit", "--pre-push"):
        assert flag in doc
