"""#37: the release workflow publishes a `vX.Y.ZrcN` tag as a PRE-RELEASE of `X.Y.Z`.

The step under test is EXECUTED, not grepped: its `run:` block is lifted out of `release.yml` and
run under bash against a scratch `__init__.py`, with the variables GitHub sets. A grep would pass
on a script that never ran.
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
