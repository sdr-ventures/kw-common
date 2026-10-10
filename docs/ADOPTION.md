# Adopting the leak guard

A repository adopts the guard by **pinning a kw-common version**. Nothing is copied: the engine,
its hooks and its CI job all come from the pin, and the repository's own content is reduced to
configuration — one `.leakguard.json`, one workflow job, one dependency line.

Every step below uses `vX.Y.Z` for the pin. Use the same tag everywhere it appears; a consumer
bump is one PR that changes all of them.

## What every repository ends up with

| Piece | Where | Contents |
|---|---|---|
| The pin | the dependency file the repository already uses (or a dev-only one) | `kw-common @ git+https://github.com/sdr-ventures/kw-common@vX.Y.Z` |
| CI | one job in an existing workflow | the reusable `leak-guard.yml` (below) |
| Hooks | the git directory, written by `kw-leak-guard --install-hooks` | a pre-commit and a pre-push shim — nothing tracked |
| Allowances | `.leakguard.json` at the root, tracked | **only if** a legitimate value trips a shape |

There is **no** tracked `.githooks/` guard hook, **no** `core.hooksPath`, and **no** vendored
`check_no_internal_info.py`.

### The CI job (all kinds)

```yaml
jobs:
  leak-guard:
    uses: sdr-ventures/kw-common/.github/workflows/leak-guard.yml@vX.Y.Z
    with:
      version: vX.Y.Z
```

Put it in a workflow that runs on `pull_request` and `push`, and on the release-tag push if the
repository has one. It needs no secrets and only `contents: read`. The job id must be
`leak-guard`, so the required status check is the same in every repository:

```
leak-guard / No internal info (leak guard)
```

It runs the self-test, the tree scan, and a range scan of what the event publishes: a pull
request's commits, a push's new commits, or — for a tag — the commits since the previous
`v[0-9]*` tag **plus the tag object** (its annotation is published on the release page). The job
is bounded at 30 minutes inside the workflow: a caller cannot set `timeout-minutes` on a job that
calls a reusable workflow, so the bound lives there.

⚠️ **Some events scan the whole history, not just what is new.** A range is never empty-and-clean:
when there is nothing to start from, everything reachable is scanned, and the log says so
(`::notice::… scanning ALL history`). That happens for the **first `v*` tag** a repository ever
pushes (there is no earlier `v[0-9]*` tag), for the **default branch's first push**, for a new
branch when the default branch is not available to compare against, a push whose previous tip no
longer exists (a force-push or history rewrite), and a manual or scheduled run. The log says so
for each except the rewrite, which prints a `WIDENING` notice from the guard itself. A history older than the guard can hold a value the guard now rejects; that run
goes red once. Make the first tag after adopting from a repository that already has one, or review
the findings (they name the shape, file and line — never the value) and rewrite or allow them.

### The hooks (all kinds)

From an environment that has the pin installed:

```
kw-leak-guard --install-hooks
kw-leak-guard --check-hooks
```

⛔ **That environment must live outside every worktree of the repository and outside any
session's scratch directory** (a per-user virtual environment such as `~/.venvs/kw-leak-guard`).
The hooks name the interpreter that installed them, and the git directory is shared by every
linked worktree. A hook bound to a deleted interpreter fails closed: git refuses **every** commit
and push in **every** worktree until `--install-hooks` is re-run from a live environment. A
virtual environment inside a worktree, or in a directory a tool cleans up when its session ends,
is exactly that deleted interpreter waiting to happen. `--check-hooks` runs each hook's own
command line (`-I -m kw_common.leakguard …`), so it reports an interpreter that cannot run the hook
— missing, an engine too old to know the hook's flags, or not importable in isolated mode — before git does.

`pre-commit` runs the tree scan and the `--staged` scan; `pre-push` range-scans every ref the push
publishes. Both live in the git directory, so no checkout removes them and every linked worktree
shares them. Each calls the interpreter that installed it: delete that environment and the hooks
**fail** (git refuses the commit or push) rather than silently skipping — re-run
`--install-hooks` from the new environment. The command refuses while `core.hooksPath` is set (an
empty value too: it turns every hook off) and never overwrites a hook it did not write.

The hooks run the interpreter in isolated mode (`-I`), so a `kw_common/` directory in the
repository can never stand in for the guard. Install the pin into a virtual environment, not with
`pip install --user` — isolated mode does not read the user site.

A push of a tag that points at a blob or a tree is refused: no scan reads that content.

### The project-side check is a separate layer

The pin's hooks and CI job know **shapes** (private address ranges, `.lan`, pool paths…). They
cannot know a literal only the owner knows: a particular hostname, container name or account.
Where a project keeps such a check of its own (outside this repository, since the list is itself
private), it is not run by the pin's hooks or by the reusable workflow. Run it yourself before every
push, in addition to the pin; passing one says nothing about the other.

### Allowances

Create `.leakguard.json` **only when** a legitimate value actually trips a shape (a functional
URL, a fixture UUID) — never speculatively, and never as an empty or placeholder file. With none,
nothing is excused, which is the intended state. One exact literal per entry, each with a `why`:

```json
{
  "allow_literals": [
    {"literal": "github.com/<owner>", "why": "functional: this repository's clone URL"}
  ]
}
```

`git add` it — the guard reads it from the index. The README's leak-guard section states exactly
what an allowance can and cannot do.

## By kind of repository

### Python service

1. Add the pin to the dependency file. If the service already depends on kw-common for alerting,
   the guard comes with that same pin — bump it rather than adding a second line.
2. Add the CI job.
3. Install the hooks from the development environment: `kw-leak-guard --install-hooks`.

### Container (a service that builds an image)

The guard runs in CI and in the hooks, not in the image.

1. If the image's Python already installs kw-common, nothing changes there. Otherwise put the pin
   in a **development** dependency file only, so the image does not carry the guard.
2. Add the CI job. Make the image-build job `needs: leak-guard`, so an image is never built from a
   tree the guard refused.
3. Install the hooks from the development environment.

### Template or configuration repository (no Python of its own)

1. Create a development environment just for the guard, **outside every worktree** and outside
   any session scratch directory (see the warning under "The hooks"):

   ```
   python -m venv ~/.venvs/kw-leak-guard
   ~/.venvs/kw-leak-guard/bin/python -m pip install "kw-common @ git+https://github.com/sdr-ventures/kw-common@vX.Y.Z"
   ~/.venvs/kw-leak-guard/bin/kw-leak-guard --install-hooks
   ```

   (`Scripts\` instead of `bin/` on Windows.)
2. Add the CI job. The reusable workflow installs its own copy, so CI needs nothing else.

### Migrating from a vendored copy of the guard

Everything above, plus removing the fork. Do it in one PR, so there is never a commit with two
guards or none.

1. Move the fork's allowances into `.leakguard.json`. Each literal in the fork's hand-edited
   `ALLOW_LITERALS` tail becomes one entry with a `why`; a literal no shape matches any more can be
   dropped. Nothing else in the fork was configuration.
2. Delete the vendored guard and the test suite copied with it
   (`scripts/check_no_internal_info.py`, its tests).
3. Delete the tracked hooks that called it and unset the relative hooks path:

   ```
   git rm .githooks/pre-commit .githooks/pre-push      # the guard hooks only
   git config --unset core.hooksPath
   kw-leak-guard --install-hooks
   ```

   ⚠️ **`core.hooksPath` is one repository-wide setting.** It lives in the shared `.git/config`, so
   unsetting it changes the shared checkout **and every linked worktree at once**, whoever is
   working in them; from that moment git runs hooks from the git directory instead, so install the
   new hooks in the same step. (A clone has its own `.git/config`: repeat this in each clone. With `extensions.worktreeConfig`
   enabled, a worktree can carry its own `core.hooksPath` that the shared unset does not touch:
   run `git config --worktree --get core.hooksPath` in each.)

   If `.githooks/` also holds hooks unrelated to the guard, keep them and call
   `python -I -m kw_common.leakguard --pre-commit` / `--pre-push "$1"` from them instead; git runs
   only one hooks directory.
4. Replace the CI steps that ran the fork with the CI job.
5. History still contains the fork's synthetic deny corpus. Pull requests, pushes to an existing
   branch, new branches compared with the default branch, and tags with a previous `v[0-9]*` tag
   scan only new commits and never see it. A whole-history scan reports it: a repository's
   first-ever `v*` tag, a manual or scheduled run, the default branch's own first push, and a new
   branch when the default branch is not available to compare against. Run the first release
   after the migration from a repository that already has a `v*` tag, or expect such a run to
   need review.

## Verifying an adoption

Run from the repository root, in the environment that has the pin. Every line must hold.
`<base>` is the branch the adoption PR targets — the default branch for a single-branch
repository, the **integration branch** (`dev`, say) for a two-environment one that merges there
first. Compare with what the PR will publish, not with a branch it does not target.

```
python -c "import kw_common; print(kw_common.__version__)"   # X.Y.Z, the pin
kw-leak-guard --selftest                                     # selftest ok
kw-leak-guard                                                # no internal info found
kw-leak-guard --range origin/<base>..HEAD                    # no internal info added
kw-leak-guard --check-hooks                                  # hooks active
git config --get core.hooksPath; echo $?                     # exit 1 (empty counts as set)
git ls-files | grep -ci check_no_internal_info               # 0
```

Then prove the hook bites — a synthetic RFC 1918 address, on a scratch branch:

```
git switch -c leak-guard-check
echo "AGENT=192.168.$((76+1)).$((76+1))" > leak-guard-check.txt   # assembled: not a literal here
git add leak-guard-check.txt
git commit -m check            # REFUSED: leak-guard-check.txt:1: private IPv4 (RFC1918)
git rm -q --cached leak-guard-check.txt && rm leak-guard-check.txt
git switch - && git branch -D leak-guard-check
```

And in CI: the adoption PR shows `leak-guard / No internal info (leak guard)` green. Branch
protection on the default branch should require exactly that check.
