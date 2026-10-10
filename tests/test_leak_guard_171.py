"""1.7.1: the gaps the first seven adoptions of the 1.7.0 leak guard found.

Each test is red against 1.7.0 and green after, unless its docstring says it is a control or says
the defect did not reproduce:

  * the reusable workflow's job had no `timeout-minutes` (a caller cannot set one);
  * a revision range an OPERATOR typed was echoed with a shape intact when the shape abutted `..`,
    `_` or `:`;
  * `--check-hooks` imported the module instead of running the hook's own command line;
  * an empty tag range fell back to the whole history without saying so;
  * `ci.yml` scanned a new branch's tip only while the reusable workflow scanned all of it.

⚠️ EVERY LEAKING LITERAL IS ASSEMBLED AT RUNTIME, and every one is synthetic. This file is scanned
by the guard it tests.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from kw_common import leakguard as guard
from test_workflow_steps import (
    BASH,
    RELEASE,
    REUSABLE,
    _commit,
    _g,
    _run_version_step,
    _scan_step,
    needs_bash,
    step_script,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_ADDR = "10.20." + "30.40"
_V6 = "fd12:3456:" + "789a::1"
_HOST = "box-a." + "lan"

_PINNED = ("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
           "-c", "tag.gpgsign=false", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *_PINNED, *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, check=check)


def _repo(tmp_path: Path, name: str = "work") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_bytes(b"clean\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _guard(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "kw_common.leakguard", "--repo", str(repo),
                           *args], capture_output=True, text=True, timeout=300)


# ------------------------------------------------------------ 1. the job bounds itself


def _job_timeout(workflow: Path) -> int | None:
    m = re.search(r"^    timeout-minutes: (\d+)\s*$", workflow.read_text("utf-8"), re.MULTILINE)
    return int(m.group(1)) if m else None


def test_the_reusable_workflow_job_carries_its_own_timeout() -> None:
    """A caller cannot set `timeout-minutes` on a reusable-workflow call, so the job in
    `leak-guard.yml` must: unset, it runs to GitHub's six-hour default."""
    minutes = _job_timeout(REUSABLE)
    assert minutes is not None, "leak-guard.yml's job has no timeout-minutes"
    assert 5 <= minutes <= 60, f"{minutes} minutes is not a sensible bound for an install + scan"


# ------------------------------------------- 2. operator-typed text is shape-only, abutting or not

_SEPARATORS = [".", "..", "...", "_", "-", ":", "/", "@", "~", "^", "=", ",", "%09", "0", "a", " ",
               "\t"]


@pytest.mark.parametrize("literal", [_ADDR, _V6, _HOST])
@pytest.mark.parametrize("sep", _SEPARATORS)
def test_operator_text_never_echoes_a_shape_however_it_abuts_other_characters(
    literal: str, sep: str,
) -> None:
    """The patterns' boundaries refuse `main..<addr>` (a `.` before a digit run is a version
    number), `<addr>_x` and `ref:<ipv6>`; for printing OPERATOR text that boundary is a hole."""
    for text in (f"main{sep}{literal}", f"{literal}{sep}main", f"a{sep}{literal}{sep}b"):
        shown = guard._ascii(text, strict=True)
        assert literal not in shown, (text.replace(literal, "<L>"), shown.replace(literal, "<L>"))


def test_a_long_operator_string_is_withheld_not_echoed() -> None:
    shown = guard._ascii("x" * 500 + _ADDR, strict=True)
    assert _ADDR not in shown and "withheld" in shown


def test_ordinary_text_is_not_over_redacted() -> None:
    """Control: a normal range and the usual messages come through untouched."""
    for text in ("origin/main..HEAD", "v1.7.0^{commit}..refs/tags/v1.7.1",
                 "feature/leak-guard", "0123456789abcdef0123456789abcdef01234567..HEAD"):
        assert guard._ascii(text, strict=True) == text


@pytest.mark.timeout(300)
def test_a_range_naming_a_ref_with_a_shape_in_it_is_echoed_shape_only(tmp_path: Path) -> None:
    """End to end through the CLI: refs can legally carry a shape, and the verdict line prints the
    range the operator typed, on the success path and on the could-not-resolve path."""
    repo = _repo(tmp_path)
    branch = f"rel_{_ADDR}"
    _git(repo, "branch", branch)
    ok = _guard(repo, "--range", f"main..{branch}")
    assert ok.returncode == 0 and _ADDR not in ok.stdout + ok.stderr, ok.stdout
    bad = _guard(repo, "--range", f"main..{_ADDR}_gone")
    assert bad.returncode == 1 and "could not scan" in bad.stdout, bad.stdout
    assert _ADDR not in bad.stdout + bad.stderr, bad.stdout
    usage = _guard(repo, "--range", f"--x_{_ADDR}")
    assert usage.returncode == 2 and _ADDR not in usage.stdout + usage.stderr


@pytest.mark.timeout(300)
def test_a_pushed_ref_whose_name_carries_a_shape_is_not_echoed(tmp_path: Path) -> None:
    """pre-push prints the REF NAME when it refuses a tag on a blob; a ref name is operator text."""
    repo = _repo(tmp_path)
    blob = _git(repo, "rev-parse", "HEAD:README.md").stdout.strip()
    name = f"refs/tags/v_{_ADDR}"
    _git(repo, "tag", "-a", f"v_{_ADDR}", "-m", "x", blob)
    line = f"{name} {_git(repo, 'rev-parse', name).stdout.strip()} {name} {'0' * 40}\n"
    res = subprocess.run([sys.executable, "-m", "kw_common.leakguard", "--repo", str(repo),
                          "--pre-push", ""], input=line, capture_output=True, text=True,
                         timeout=300)
    assert res.returncode == 1 and "REFUSED" in res.stdout, res.stdout
    assert _ADDR not in res.stdout + res.stderr, res.stdout


# ------------------------------------------- 3. --check-hooks runs what git will run


def _venv_with_stub_engine(tmp_path: Path, name: str, leakguard_source: str) -> str:
    """A real venv whose `kw_common.leakguard` is a stub: it IMPORTS, but is not this engine."""
    root = tmp_path / name
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(root)
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    stub = tmp_path / f"{name}-stub"
    (stub / "kw_common").mkdir(parents=True)
    (stub / "kw_common" / "__init__.py").write_text('__version__ = "0.0"\n', encoding="utf-8")
    (stub / "kw_common" / "leakguard.py").write_text(leakguard_source, encoding="utf-8")
    purelib = subprocess.run([str(python), "-c", "import sysconfig;print(sysconfig.get_path("
                              "'purelib'))"], capture_output=True, text=True, check=True,
                             timeout=120).stdout.strip()
    (Path(purelib) / "stub.pth").write_text(f"{stub}\n", encoding="utf-8")
    return python.absolute().as_posix()


# An engine that predates the hook flags: importable, and it refuses every one it does not know.
_OLD_ENGINE = (
    "import sys\n"
    "def main(argv):\n"
    "    return 2 if any(a.startswith('--p') for a in argv) else 0\n"
    "if __name__ == '__main__':\n"
    "    sys.exit(main(sys.argv[1:]))\n"
)


def _install_with_python(repo: Path, python: str) -> None:
    assert _guard(repo, "--install-hooks").returncode == 0
    hooks = Path(_git(repo, "rev-parse", "--git-path", "hooks").stdout.strip())
    hooks = hooks if hooks.is_absolute() else repo / hooks
    for name in ("pre-commit", "pre-push"):
        (hooks / name).write_bytes(guard._hook_text(name, python).encode("utf-8"))


@pytest.mark.timeout(300)
def test_check_FAILS_for_an_interpreter_that_imports_the_guard_but_cannot_run_the_hook(
    tmp_path: Path,
) -> None:
    """An engine too old to know `--pre-commit` imports cleanly, so an import check called the
    hooks active; git then runs `-I -m kw_common.leakguard --pre-commit` and refuses every
    commit."""
    repo = _repo(tmp_path)
    python = _venv_with_stub_engine(tmp_path, "oldvenv", _OLD_ENGINE)
    _install_with_python(repo, python)
    premise = subprocess.run([python, "-I", "-c", "import kw_common.leakguard"],
                             capture_output=True, timeout=120)
    assert premise.returncode == 0, "premise: the stub engine imports"
    res = _guard(repo, "--check-hooks")
    assert res.returncode == 1 and "HOOKS NOT ACTIVE" in res.stdout, res.stdout
    assert "cannot run" in res.stdout, res.stdout
    (repo / "n.md").write_bytes(b"clean\n")
    _git(repo, "add", "n.md")
    assert _git(repo, "commit", "-qm", "x", check=False).returncode != 0, "git refuses too"


@pytest.mark.timeout(300)
def test_the_check_runs_the_hook_isolated_so_a_repository_package_cannot_answer_for_it(
    tmp_path: Path,
) -> None:
    """The probe must carry the hook's `-I`: without it `-m` puts the working directory first and a
    `kw_common/` committed to the repository answers `hook probe ok` for an interpreter whose own
    engine cannot run the hook."""
    repo = _repo(tmp_path)
    (repo / "kw_common").mkdir()
    (repo / "kw_common" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "kw_common" / "leakguard.py").write_text(
        "print('hook probe ok: impostor')\n", encoding="utf-8")
    python = _venv_with_stub_engine(tmp_path, "oldvenv2", _OLD_ENGINE)
    _install_with_python(repo, python)
    res = _guard(repo, "--check-hooks")
    assert res.returncode == 1 and "impostor" not in res.stdout, res.stdout


@pytest.mark.timeout(300)
def test_check_passes_and_names_the_version_for_a_working_install(tmp_path: Path) -> None:
    """Control: the real engine answers its probe; the check stays green."""
    repo = _repo(tmp_path)
    assert _guard(repo, "--install-hooks").returncode == 0
    res = _guard(repo, "--check-hooks")
    assert res.returncode == 0 and "hooks active" in res.stdout, res.stdout


def test_the_probe_answers_without_a_repository_and_is_only_a_hook_modifier(
    tmp_path: Path,
) -> None:
    for argv in (["--pre-commit", "--probe"], ["--pre-push", "", "--probe"]):
        res = subprocess.run([sys.executable, "-I", "-m", "kw_common.leakguard", *argv],
                             cwd=tmp_path, capture_output=True, text=True, timeout=120)
        assert res.returncode == 0 and res.stdout.startswith("hook probe ok: kw-common "), (
            res.stdout, res.stderr)
    for argv in (["--probe"], ["--staged", "--probe"], ["--range", "a..b", "--probe"]):
        with pytest.raises(guard.UsageError):
            guard.parse_args(argv)


# ------------------------------------------- 4. the tag edge cases


@needs_bash
@pytest.mark.parametrize("tag,package", [
    ("v1.7.1rc0", "1.7.1"),
    ("v1.7.1rc00", "1.7.1"),
    ("v1.7.1RC1", "1.7.1"),
    ("v1.7.1rc", "1.7.1"),
    ("v1.7.1rc-1", "1.7.1"),
    ("v1.7.1rc1 ", "1.7.1"),
])
def test_a_rc0_tag_is_refused_outright_not_published_as_a_full_release(
    tmp_path: Path, tag: str, package: str,
) -> None:
    """This did NOT reproduce as a defect: 1.7.0's `release.yml` already refuses `rcN` without a
    leading non-zero digit (the tag matches neither the package version nor a candidate of it), so
    nothing is published at all - not as a pre-release and not as a full release. Pinned here so a
    loosened regex cannot make `rc0` a release."""
    code, out, outputs = _run_version_step(tmp_path, tag, package)
    assert code != 0, f"{tag!r} was accepted against {package}: {out}"
    assert outputs == {}, outputs


@needs_bash
@pytest.mark.timeout(300)
def test_the_reusable_workflow_SAYS_so_when_a_tag_scans_all_history(tmp_path: Path) -> None:
    """A tag with no earlier `v[0-9]*` tag publishes every commit reachable from it, so that is the
    range - never an empty one read as clean - and the log now carries a notice saying why."""
    repo = tmp_path / "first"
    repo.mkdir()
    _g(repo, "init", "-q")
    _commit(repo, "a.md", "clean\n")
    head = _commit(repo, "b.md", "clean\n")
    _g(repo, "tag", "-a", "v0.1.0", "-m", "first")
    code, out = _scan_step(repo, EVENT="push", REF="refs/tags/v0.1.0", HEAD_SHA=head)
    assert code == 0 and "range: refs/tags/v0.1.0" in out, out
    assert "::notice::" in out and "scanning ALL history" in out, out

    # Control: with an earlier tag the range is narrow and the notice is absent.
    nxt = _commit(repo, "c.md", "clean\n")
    _g(repo, "tag", "-a", "v0.2.0", "-m", "second")
    code, out = _scan_step(repo, EVENT="push", REF="refs/tags/v0.2.0", HEAD_SHA=nxt)
    assert code == 0 and "range: v0.1.0^{commit}..refs/tags/v0.2.0" in out, out
    assert "scanning ALL history" not in out, out


@needs_bash
@pytest.mark.timeout(300)
def test_a_branch_with_nothing_to_compare_against_says_it_scans_all_history(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "nobase"
    repo.mkdir()
    _g(repo, "init", "-q")
    tip = _commit(repo, "a.md", "clean\n")
    code, out = _scan_step(repo, EVENT="push", REF="refs/heads/feature", PUSH_BEFORE="0" * 40,
                           HEAD_SHA=tip)
    assert code == 0 and f"range: {tip}\n" in out, out
    assert "::notice::" in out and "scanning ALL history" in out, out


def test_the_release_workflow_also_says_so_when_it_widens_a_tag_range() -> None:
    """`release.yml` already announced the widening; pinned so the two workflows stay aligned."""
    script = step_script(RELEASE, "Scan the commits AND THE TAG OBJECT this tag would publish")
    assert "scanning ALL" in script and "-eq 0" in script


# ------------------------------------------- 5. ci.yml scans what the reusable workflow scans


def test_ci_scans_exactly_what_the_reusable_workflow_scans() -> None:
    """One behaviour, not two: ci.yml's range step is the reusable workflow's, byte for byte, with
    the same environment. It used to scan a new branch's tip only."""
    assert step_script(CI, "Scan what this event publishes") == step_script(
        REUSABLE, "Scan what this event publishes")

    def env_keys(workflow: Path) -> set[str]:
        text = workflow.read_text("utf-8")
        i = text.index("- name: Scan what this event publishes")
        block = text[i:].split("run: |")[0]
        return set(re.findall(r"^\s+([A-Z_]+): \$\{\{", block, re.MULTILINE))

    assert env_keys(CI) == env_keys(REUSABLE) and len(env_keys(REUSABLE)) == 7


@needs_bash
@pytest.mark.timeout(300)
def test_the_ci_step_scans_a_new_branch_from_the_default_branch(tmp_path: Path) -> None:
    """Run ci.yml's OWN step (not the reusable one) on a branch whose first commit leaks and whose
    second removes it: the tip is clean, the push publishes both."""
    repo = tmp_path / "newbranch"
    repo.mkdir()
    _g(repo, "init", "-q")
    base = _commit(repo, "a.md", "clean\n")
    _g(repo, "update-ref", "refs/remotes/origin/main", base)
    _commit(repo, "b.md", "AGENT=192.168." + "77.77\n")
    tip = _commit(repo, "b.md", "clean\n")
    script = step_script(CI, "Scan what this event publishes")
    env = {**os.environ, "EVENT": "push", "REF": "refs/heads/feature", "PR_BASE": "",
           "PR_HEAD": "", "PUSH_BEFORE": "0" * 40, "HEAD_SHA": tip, "DEFAULT_BRANCH": "main",
           "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")}
    assert BASH is not None
    proc = subprocess.run([BASH, "-c", script], cwd=repo, env=env, capture_output=True,
                          text=True, timeout=300)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1 and f"range: refs/remotes/origin/main..{tip}" in out, out


# ------------------------------------------- 6. ADOPTION.md says what the seven adoptions found

ADOPTION = REPO_ROOT / "docs" / "ADOPTION.md"


def test_adoption_carries_the_corrections_the_seven_adoptions_found() -> None:
    text = " ".join(ADOPTION.read_text("utf-8").replace("**", "").split())
    assert "origin/main" not in text and "--range origin/<base>..HEAD" in text
    assert "integration branch" in text
    assert "must live outside every worktree" in text and "session's scratch directory" in text
    assert "fails closed" in text and "every commit and push in every worktree" in text
    assert "`core.hooksPath` is one repository-wide setting" in text
    assert "shared checkout" in text and "every linked worktree" in text
    assert "first `v*` tag" in text and "default branch's first push" in text
    assert "separate layer" in text and "not run by the pin's hooks" in text
    assert "only when" in text and "never speculatively" in text
    assert "timeout" in text
    # No venv is placed inside the repository any more.
    assert ".venv-guard" not in text


@pytest.mark.timeout(300)
def test_the_found_and_widening_messages_echo_the_range_shape_only(tmp_path: Path) -> None:
    """The two remaining sites that print the operator's range: the INTERNAL INFO FOUND headline
    and the unreachable-base widening notice."""
    repo = _repo(tmp_path)
    branch = f"rel_{_ADDR}"
    _git(repo, "switch", "-q", "-c", branch)
    (repo / "leak.txt").write_bytes(("AGENT=192.168." + "77.77\n").encode())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "leak")
    found = _guard(repo, "--range", f"main..{branch}")
    assert found.returncode == 1 and "INTERNAL INFO FOUND" in found.stdout, found.stdout
    assert _ADDR not in found.stdout and "192.168." + "77.77" not in found.stdout, found.stdout
    gone = f"{'0' * 39}1_{_ADDR}"
    widened = _guard(repo, "--range", f"{gone}..{branch}")
    assert "WIDENING" in widened.stdout and _ADDR not in widened.stdout, widened.stdout


# ------------------------------------------- findings of the independent verification


@pytest.mark.timeout(300)
def test_install_and_check_do_not_echo_a_shape_in_the_repository_path(tmp_path: Path) -> None:
    """The hook paths and the repository root are operator-controlled text too."""
    repo = _repo(tmp_path, f"r_{_ADDR}")
    for argv in (["--install-hooks"], ["--check-hooks"]):
        res = _guard(repo, *argv)
        assert res.returncode == 0 and _ADDR not in res.stdout + res.stderr, res.stdout
    _git(repo, "config", "core.hooksPath", f"//{_ADDR}_share/hooks")
    res = _guard(repo, "--check-hooks")
    assert res.returncode == 1 and _ADDR not in res.stdout + res.stderr, res.stdout


@pytest.mark.timeout(300)
def test_a_hung_hook_interpreter_is_an_inactive_hook_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    assert _guard(repo, "--install-hooks").returncode == 0
    real = subprocess.run

    def hang(cmd: list[str], *a: object, **k: object) -> subprocess.CompletedProcess[str]:
        if "--probe" in cmd:
            raise subprocess.TimeoutExpired(cmd, 1)
        return real(cmd, *a, **k)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(guard.subprocess, "run", hang)
    assert guard.check_hooks(repo) == 1
    assert "HOOKS NOT ACTIVE" in capsys.readouterr().out


def test_a_very_long_operator_string_is_withheld_whole() -> None:
    shown = guard._ascii(" ".join(["x" * 150] * 40), strict=True)
    assert "withheld" in shown and len(shown) < 100


@needs_bash
@pytest.mark.timeout(300)
def test_a_manual_run_says_it_scans_all_history(tmp_path: Path) -> None:
    repo = tmp_path / "manual"
    repo.mkdir()
    _g(repo, "init", "-q")
    head = _commit(repo, "a.md", "clean\n")
    code, out = _scan_step(repo, EVENT="workflow_dispatch", REF="refs/heads/main", HEAD_SHA=head)
    assert code == 0 and "::notice::" in out and "scanning ALL history" in out, out
