"""The engine-correctness fixes that make kw-common the one leak guard the fleet runs.

Each test here is red against the engine before these fixes and green after, unless its
docstring says it is the control half:

  * consumer#245 — IPv6 (unique-local, link-local) and RFC 8375 `.home.arpa` were not shapes at all;
  * a finding printed the MATCHED LITERAL, so every CI log republished the value it reported;
  * #31 — a `working-tree-encoding` checkout false-red a clean tree;
  * consumer#239 — one `git cat-file` subprocess per staged-but-absent file;
  * unraid-templates#37 — a C-quoted `diff --git` header made an ordinary file unscannable
    (decoding unit tests here; the real-git range test is in `test_leak_guard_range.py`);
  * a git that could not answer `--is-shallow-repository` was read as a complete clone.

⚠️ EVERY LEAKING LITERAL IS ASSEMBLED AT RUNTIME, and every one is synthetic. This file is scanned
by the guard it tests, and the guard skips only its own source.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from kw_common import leakguard as guard

_SCRIPT = Path(guard.__file__).resolve()
COMPILED = guard.compile_patterns()

# Synthetic, fragmented: neither half matches alone.
_ULA = "fd12:3456:" + "789a:1::1"
_LINK_LOCAL = "fe80::1ff:" + "fe23:4567:890a"
_HOME = "printer." + "home.arpa"
_ADDR = "192.168." + "77.77"
_HOST = "host-a." + "lan"

_PINNED = ("-c", "user.name=guard test", "-c", "user.email=t@example.com",
           "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
           "-c", "core.autocrlf=false")


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *_PINNED, *args], cwd=repo, capture_output=True, check=True,
                         timeout=300)
    return out.stdout.decode("utf-8", errors="replace")


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")


def _cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(_SCRIPT), "--repo", str(repo), *args],
                          capture_output=True, text=True, timeout=300)


# ------------------------------------------------------------- consumer#245: the two new shapes


@pytest.mark.parametrize("line", [
    f"peer {_ULA} on the tunnel",
    f"AGENT_URL=http://[{_ULA}]:9999/mcp",
    f"gateway {_LINK_LOCAL}%eth0",
    "tailnet v6 fd7a:115c:" + "a1e0::1",
    "FD12:3456:789A:0001:" + "0000:0000:0000:0001",
    "seven hextets fd12:1:2:3:" + "4:5:6:: elided",
    f"the subnet is {_ULA.rsplit('::', 1)[0]}::/64",
])
def test_a_private_IPv6_address_is_a_finding(line: str) -> None:
    assert [h[1] for h in guard.scan_text(line, COMPILED)] == ["private IPv6 (ULA / link-local)"]


@pytest.mark.parametrize("line", [
    "started 12:34:56 on the 3rd",
    "hwaddr fd:12:34:56:78:9a",
    "image sha256:fd12ab34cd56ef7890fd12ab34cd56ef7890",
    "fdab: true",
    "std::vector<int> v;",
    "docs use 2001:db8::1",
    "bind [::1]:8080",
    "ULA is fc00::/7, in practice fd00::/8; link-local is fe80::/10",
    "fe80 and fd12 alone are not addresses",
])
def test_hex_and_colon_text_that_is_not_a_private_address_is_not_a_finding(line: str) -> None:
    """The control half: these pass on both engines. They are what keeps the IPv6 pattern
    precise — a timestamp, a MAC, a digest, a YAML key, a scope operator, the RFC 3849
    documentation prefix, loopback, and the range bases written as ranges."""
    assert guard.scan_text(line, COMPILED) == []


def test_the_range_base_allowance_grants_no_amnesty_to_a_real_address() -> None:
    """The allow-span covers `fd00::/8` exactly. A real address beside it, or one that merely
    starts with the base, still fires."""
    for line in (f"fd00::/8 and {_ULA}", "host fd" + "00::1 on the lan", "fe" + "80::1/64"):
        assert "private IPv6 (ULA / link-local)" in {h[1] for h in guard.scan_text(line, COMPILED)}


def test_a_home_arpa_host_is_a_finding_and_the_zone_itself_is_not() -> None:
    assert [h[1] for h in guard.scan_text(f"ping {_HOME}.", COMPILED)] == [
        "home network domain (RFC 8375)"]
    assert guard.scan_text("RFC 8375 reserves home.arpa for this", COMPILED) == []
    assert guard.scan_text("PTR 1.2.0.192.in-addr.arpa", COMPILED) == []


# ---------------------------------- findings name the SHAPE, the file and the line, never the value


_PLANTS = [
    ("private IPv6 (ULA / link-local)", _ULA),
    ("private IPv6 (ULA / link-local)", _LINK_LOCAL),
    ("home network domain (RFC 8375)", _HOME),
]


@pytest.mark.timeout(300)
@pytest.mark.parametrize("label,literal", _PLANTS)
def test_a_planted_new_shape_reds_ALL_THREE_scans_and_no_output_carries_the_literal(
    tmp_path: Path, label: str, literal: str,
) -> None:
    """⭐ The acceptance case for each new shape: the tree scan, the staged scan and the range
    scan all exit 1 and name the shape, the file and the line — and neither stdout nor stderr
    contains the matched literal."""
    repo = tmp_path / "plant"
    _init(repo)
    (repo / "README.md").write_bytes(b"clean\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "notes.md").write_bytes(f"line one\nAGENT={literal}\n".encode())
    _git(repo, "add", "notes.md")

    for name, proc in (("staged", _cli(repo, "--staged")), ("tree", _cli(repo))):
        out = proc.stdout + proc.stderr
        assert proc.returncode == 1, f"{name} scan missed {label}: {out}"
        assert f"notes.md:2: {label}" in out, f"{name} scan did not name file, line, shape: {out}"
        assert literal not in out, f"{name} scan printed the matched literal: {out}"

    _git(repo, "commit", "-qm", "add notes")
    proc = _cli(repo, "--range", f"{base}..HEAD")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, f"range scan missed {label}: {out}"
    assert f"notes.md:2: {label}" in out, out
    assert literal not in out, f"range scan printed the matched literal: {out}"


@pytest.mark.timeout(300)
def test_no_SURFACE_prints_the_literal_not_a_path_a_message_or_an_identity(
    tmp_path: Path,
) -> None:
    """A `<path>` finding IS a path with the value in it, so the finding strings alone cannot
    keep the value out of the log; `_ascii` redacts every printed string. Asserted on all five
    surfaces at once: content, path, commit message, identity and an annotated tag."""
    repo = tmp_path / "surfaces"
    _init(repo)
    (repo / "README.md").write_bytes(b"clean\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "docs").mkdir()
    (repo / "docs" / f"{_ADDR}.md").write_bytes(f"host {_HOST}\n".encode())
    _git(repo, "add", "-A")
    _git(repo, "-c", f"user.name=deploy {_HOME}", "commit", "-qm", f"deployed from {_ULA}")
    _git(repo, "tag", "-a", "v9", "-m", f"cut on {_HOST}")

    for args in ((), ("--staged",), ("--range", f"{base}..refs/tags/v9")):
        proc = _cli(repo, *args)
        out = proc.stdout + proc.stderr
        for literal in (_ADDR, _HOST, _HOME, _ULA):
            assert literal not in out, f"{args or 'tree'} printed a matched literal: {out}"
    proc = _cli(repo, "--range", f"{base}..refs/tags/v9")
    out = proc.stdout
    assert proc.returncode == 1, out
    for surface in ("<path>", "<commit message>", "<author name>", "<tag object>",
                    ":1: private lan domain"):
        assert surface in out, f"the {surface} finding is missing: {out}"


def test_the_finding_STRINGS_carry_no_literal_before_any_redaction() -> None:
    """`scan_range` is public and returns these strings, so a caller that prints them without
    `_ascii` must not get the value either. (A `<path>` finding is a path by nature; it is the
    one surface that relies on `_ascii`'s redaction, which the test above covers.)"""
    added = guard.ParsedDiff([("notes.md", 2, f"AGENT={_ULA}")], [], [])
    lines = (guard.scan_added("a" * 40, added, COMPILED)[0]
             + guard.scan_identity("a" * 40, [("author name", f"ops {_HOME}")], COMPILED)
             + guard.scan_message("a" * 40, f"deployed from {_HOST}")
             + guard.scan_tags([("tag object", "b" * 40, f"object x\n\ncut on {_ADDR}")]))
    assert len(lines) == 4, lines
    for line in lines:
        for literal in (_ULA, _HOME, _HOST, _ADDR):
            assert literal not in line, line


def test_ascii_redacts_every_shape_and_leaves_ordinary_text_alone() -> None:
    line = f"docs/{_ADDR}/x.md: <path>: private IPv4 (RFC1918) at {_ULA}"
    shown = guard._ascii(line)
    assert _ADDR not in shown and _ULA not in shown
    assert "<private IPv4 (RFC1918)>" in shown and "<private IPv6 (ULA / link-local)>" in shown
    assert guard._ascii("README.md:3: tailnet name") == "README.md:3: tailnet name"


def test_ascii_escapes_control_bytes_a_decoded_path_can_now_carry() -> None:
    """A C-quoted path is decoded now, so ESC or CR in a filename would reach the terminal raw and
    could rewrite the finding line. Every control byte but the newline is escaped."""
    assert guard._ascii("a\x1b[2Kb\rc\x01d\ne") == "a\\x1b[2Kb\\x0dc\\x01d\ne"


# --------------------------------------------------------------- #31: working-tree-encoding


@pytest.mark.timeout(300)
def test_a_working_tree_encoding_checkout_does_not_false_red_a_CLEAN_tree(tmp_path: Path) -> None:
    """#31. The blob is clean UTF-8; the checkout is UTF-16LE, so the worktree bytes carry a NUL
    after every character and used to be refused as "not UTF-8 text"."""
    repo = tmp_path / "wte"
    _init(repo)
    (repo / ".gitattributes").write_bytes(b"*.txt text working-tree-encoding=UTF-16LE\n")
    (repo / "notes.txt").write_bytes("clean text\n".encode("utf-16-le"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "utf-16 checkout")
    assert b"\x00" in (repo / "notes.txt").read_bytes(), "premise: the checkout is UTF-16LE"
    assert b"\x00" not in subprocess.run(["git", "cat-file", "blob", "HEAD:notes.txt"], cwd=repo,
                                         capture_output=True, check=True).stdout

    proc = _cli(repo)
    assert proc.returncode == 0, f"a clean working-tree-encoding file was refused: {proc.stdout}"


@pytest.mark.timeout(300)
def test_a_LEAK_in_a_working_tree_encoding_file_is_still_caught(tmp_path: Path) -> None:
    repo = tmp_path / "wteleak"
    _init(repo)
    (repo / ".gitattributes").write_bytes(b"*.txt text working-tree-encoding=UTF-16LE\n")
    (repo / "notes.txt").write_bytes(f"ok\nAGENT={_ADDR}\n".encode("utf-16-le"))
    _git(repo, "add", "-A")
    proc = _cli(repo)
    assert proc.returncode == 1, proc.stdout
    assert "notes.txt:2: private IPv4 (RFC1918)" in proc.stdout, proc.stdout


@pytest.mark.timeout(300)
def test_a_NUL_bearing_file_WITHOUT_the_attribute_is_still_refused(tmp_path: Path) -> None:
    """The control half: the fallback is keyed on the attribute, so BOM-less UTF-16 with no
    `working-tree-encoding` is still the #242 refusal."""
    repo = tmp_path / "noattr"
    _init(repo)
    (repo / "notes.txt").write_bytes("clean text\n".encode("utf-16-le"))
    _git(repo, "add", "-A")
    proc = _cli(repo)
    assert proc.returncode == 1 and "contains a NUL byte" in proc.stdout, proc.stdout


@pytest.mark.timeout(300)
def test_an_UNSTAGED_utf16_overwrite_of_a_clean_file_is_not_vouched_for_by_its_blob(
    tmp_path: Path,
) -> None:
    """Why the fallback is keyed on the ATTRIBUTE and not merely on "the blob is clean": without
    the attribute, a NUL-bearing worktree file that differs from a clean blob is content this
    scan cannot read, and reading the blob instead would clear a worktree it never looked at."""
    repo = tmp_path / "overwrite"
    _init(repo)
    (repo / "notes.txt").write_bytes(b"clean text\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "clean")
    (repo / "notes.txt").write_bytes(f"AGENT={_ADDR}\n".encode("utf-16-le"))
    proc = _cli(repo)
    assert proc.returncode == 1 and "contains a NUL byte" in proc.stdout, proc.stdout


# -------------------------------------------------- consumer#239: one batch for staged-but-absent


@pytest.mark.timeout(300)
def test_absent_files_are_read_in_ONE_subprocess_not_one_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """consumer#239. Forty tracked files deleted from the worktree without staging: the tree scan
    must ask git for their staged bytes once, not forty times — and still report the one leak
    among them at its line."""
    repo = tmp_path / "absent"
    _init(repo)
    for i in range(40):
        body = f"x\nAGENT={_ADDR}\n" if i == 17 else "clean\n"
        (repo / f"f{i:02}.txt").write_bytes(body.encode())
    _git(repo, "add", "-A")
    for i in range(40):
        (repo / f"f{i:02}.txt").unlink()

    calls: list[list[str]] = []
    real_run = subprocess.run

    def counting(cmd: list[str], *a: object, **k: object) -> object:
        calls.append(list(cmd))
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(guard.subprocess, "run", counting)
    assert guard.main(["--repo", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "f17.txt:2: private IPv4 (RFC1918)" in out, out
    cat_files = [c for c in calls if "cat-file" in c]
    assert len(cat_files) <= 2, (
        f"{len(cat_files)} cat-file subprocesses for 40 absent files (one for the config lookup "
        f"is expected, one batch for the files): {cat_files[:3]}")


@pytest.mark.timeout(300)
@pytest.mark.parametrize("leak_byte,clean_byte", [
    (b"\xfe", b"\xff"), (b"\xff", b"\xfe"),
    # An invalid byte that sorts BEFORE a valid path holding a literal U+FFFD: the valid entry
    # comes second and would win the shared key without the collision check.
    (b"\x80", "�".encode()),
])
def test_two_NON_UTF8_paths_that_decode_alike_are_never_vouched_for_by_the_wrong_blob(
    tmp_path: Path, leak_byte: bytes, clean_byte: bytes,
) -> None:
    """Two index paths that differ only in an invalid byte both decode to `a\\ufffd.txt`, a name
    no file has, so they reach the absent-file branch. The batched read keyed them by that one
    decoded name and scanned whichever blob came last — in one index order a committed leak
    exited 0. Neither order may pass now: the path is reported unreadable, as it was before."""
    repo = tmp_path / "collide"
    _init(repo)

    def blob(data: bytes) -> str:
        return subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=data,
                              capture_output=True, check=True).stdout.decode().strip()

    leak, clean = blob(f"AGENT={_ADDR}\n".encode()), blob(b"clean\n")
    info = (b"100644 " + leak.encode() + b"\ta" + leak_byte + b".txt\0"
            + b"100644 " + clean.encode() + b"\ta" + clean_byte + b".txt\0")
    subprocess.run(["git", "update-index", "-z", "--add", "--index-info"], cwd=repo, input=info,
                   capture_output=True, check=True)
    proc = _cli(repo)
    assert proc.returncode == 1, f"a leak behind a colliding path scanned clean: {proc.stdout}"


def _index_only(repo: Path, entries: list[tuple[bytes, bytes, str]]) -> None:
    """Write (mode, raw path, content-or-sha) entries straight into the index — names Windows
    cannot create, and modes no checkout produces."""
    info = b""
    for mode, path, content in entries:
        sha = content if mode == b"160000" else subprocess.run(
            ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=content.encode(),
            capture_output=True, check=True).stdout.decode().strip()
        info += mode + b" " + sha.encode() + b"\t" + path + b"\0"
    subprocess.run(["git", "update-index", "-z", "--add", "--index-info"], cwd=repo, input=info,
                   capture_output=True, check=True)


@pytest.mark.timeout(300)
def test_a_GITLINK_whose_name_collides_cannot_make_a_leaking_file_look_like_a_submodule(
    tmp_path: Path,
) -> None:
    """The mode is taken from the LAST entry under a decoded key, so a gitlink listed after a
    leaking file (`\\xff` sorts after `\\xfe`) used to skip that file as a submodule, exit 0."""
    repo = tmp_path / "gitlink"
    _init(repo)
    _index_only(repo, [(b"100644", b"x\xfe.txt", f"AGENT={_ADDR}\n"),
                       (b"160000", b"x\xff.txt", "e" * 40)])
    proc = _cli(repo)
    assert proc.returncode == 1, f"a leak hidden behind a colliding gitlink: {proc.stdout}"


@pytest.mark.timeout(300)
def test_a_LONE_non_utf8_path_is_still_read_and_scanned(tmp_path: Path) -> None:
    """Only a SHARED key loses its SHA. A single non-UTF-8 name (ordinary on Linux) is read from
    the index by its own SHA: clean passes, a leak is found at its line."""
    repo = tmp_path / "lone"
    _init(repo)
    _index_only(repo, [(b"100644", b"caf\xe9.txt", "clean\n")])
    assert _cli(repo).returncode == 0, _cli(repo).stdout
    _index_only(repo, [(b"100644", b"caf\xe9.txt", f"ok\nAGENT={_ADDR}\n")])
    proc = _cli(repo)
    assert proc.returncode == 1 and ":2: private IPv4 (RFC1918)" in proc.stdout, proc.stdout


def test_the_batch_reader_cannot_be_shifted_by_a_missing_or_non_blob_answer(
    tmp_path: Path,
) -> None:
    """The desync the removed batch reader had: a response it did not fully consume shifted
    every later answer onto the wrong file. A tree object (a body this does not want) and a
    missing object sit in the MIDDLE here, and the answers after them must still be right."""
    repo = tmp_path / "batch"
    _init(repo)

    def blob(data: bytes) -> str:
        return subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=data,
                              capture_output=True, check=True).stdout.decode().strip()

    a, b = blob(b"first\n"), blob(b"second\nwith two lines\n")
    tree = subprocess.run(["git", "mktree"], cwd=repo, input=f"100644 blob {a}\tx\n".encode(),
                          capture_output=True, check=True).stdout.decode().strip()
    missing = "0123456789abcdef0123456789abcdef01234567"
    got = guard._staged_blobs(repo, [a, tree, "", missing, b])
    assert got == [b"first\n", None, None, None, b"second\nwith two lines\n"]


# ------------------------------------------------------- unraid-templates#37: C-unquoting


@pytest.mark.parametrize("tail,path", [
    ('"a/quo\\"te.txt" "b/quo\\"te.txt"', 'quo"te.txt'),
    ('"a/back\\\\slash.txt" "b/back\\\\slash.txt"', "back\\slash.txt"),
    ('"a/ctrl\\001char.txt" "b/ctrl\\001char.txt"', "ctrl\x01char.txt"),
    ('"a/tab\\there.md" "b/tab\\there.md"', "tab\there.md"),
    ('"a/caf\\303\\251.bin" "b/caf\\303\\251.bin"', "café.bin"),
])
def test_a_C_quoted_header_is_decoded_to_the_real_path(tail: str, path: str) -> None:
    assert guard._header_path(tail) == path


@pytest.mark.parametrize("tail", [
    '"a/x" "b/y"',                       # the two sides disagree
    '"a/bad\\q" "b/bad\\q"',             # an escape git never emits
    '"a/dangling\\" "b/dangling\\"',     # a backslash with nothing after it
    '"a/x"y" "b/x"y"',                   # an unescaped quote inside
])
def test_a_malformed_C_quoted_header_still_fails_CLOSED(tail: str) -> None:
    assert guard._is_marker(guard._header_path(tail))


# ----------------------------------------------------------------- shallow: fail closed


def test_a_git_that_cannot_answer_is_shallow_is_treated_as_SHALLOW(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    def refuse(root: Path, *args: str) -> str:
        raise subprocess.CalledProcessError(129, ["git", *args])

    monkeypatch.setattr(guard, "_git", refuse)
    assert guard.is_shallow(tmp_path) is True


def test_only_a_plain_false_is_a_complete_clone(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    for answer, shallow in (("false\n", False), ("true\n", True), ("\n", True)):
        monkeypatch.setattr(guard, "_git", lambda root, *a, _ans=answer: _ans)
        assert guard.is_shallow(tmp_path) is shallow, answer
