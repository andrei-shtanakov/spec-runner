# Release runbook

Four surfaces, in this order. Each has been missed at least once, which is why
each is written down rather than remembered.

```
tag → publish.yml → PyPI → GitHub Release → the red tag-guard run
```

## 1. Cut the release

On a branch, never on `master` (direct pushes are refused by a branch rule):

1. Bump `version` in `pyproject.toml`.
2. Move `[Unreleased]` into `## [X.Y.Z] - YYYY-MM-DD`, and state in the section
   itself why the bump is major/minor/patch — the reader of the CHANGELOG is
   the one who needs it, not the reader of the PR.
3. Repoint the compare links at the **bottom** of the file: `[Unreleased]` →
   `vX.Y.Z...HEAD`, plus a new `[X.Y.Z]` → `vPREV...vX.Y.Z`.
4. `python3 scripts/check_changelog_links.py --tag vX.Y.Z` — the same check
   `publish.yml` runs, so a failure here is a failure there.

Semver, as practised here: a change to `--json-result` or the state-DB **format**
is major. Additive tables, additive JSON keys and changed exit codes are minor
(precedents: 2.16.0, 2.21.0, 2.23.0). A defect fix that moves no public surface
is patch (2.27.1) — check that claim with a diff of `schemas/`,
`docs/state-schema.md` and the `add_argument` lines in `cli.py`, rather than
asserting it.

## 2. Tag — the step that gets forgotten

After the release PR is merged:

```bash
git switch master && git pull --ff-only
git tag -a vX.Y.Z <merge-commit> -m "vX.Y.Z — <one line>"
git push origin vX.Y.Z
```

Tag the **release commit**, not the tip, when later work has already landed.

Forgetting this is the recurring failure: v2.4.0, v2.10.0 and v2.22.0 all merged
with a bumped `pyproject` and no tag, so `publish.yml` never ran and PyPI lagged
for weeks. `release-tag-guard.yml` now goes red on `master` for exactly this.

## 3. Watch publish.yml

Pushing the tag triggers it: release-hygiene check → build → PyPI via Trusted
Publisher. There is no manual `twine` step any more.

```bash
gh run list --workflow=publish.yml --limit 1
```

## 4. Verify PyPI — both checks

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://pypi.org/pypi/spec-runner/X.Y.Z/json
curl -s https://pypi.org/pypi/spec-runner/json | grep -o '"version":"[^"]*"' | head -1
```

The per-version endpoint is the reliable one; the project endpoint caches and
may report the previous version for a minute after a successful publish.

## 5. Create the GitHub Release by hand

`publish.yml` does **not** create it. From the CHANGELOG section:

```bash
awk '/^## \[X.Y.Z\]/{f=1} /^## \[PREV\]/{f=0} f' CHANGELOG.md | tail -n +2 > /tmp/notes.md
gh release create vX.Y.Z --title "vX.Y.Z" --notes-file /tmp/notes.md
```

## 6. Re-run the red tag-guard job

`release-tag-guard` fails on `master` between the release merge and the tag.
Pushing a tag does not re-run a branch-triggered workflow on that commit, so
find the failed run and re-run it:

```bash
gh run list --workflow=release-tag-guard.yml --limit 5
gh run rerun <id>
```

## 7. Verify the published artifact, not the checkout

The last step, and the one that has repeatedly earned its place: install what a
user would install and exercise the thing the release claims to fix.

```bash
uv tool install --refresh --reinstall --no-cache spec-runner==X.Y.Z
spec-runner --version        # MUST print X.Y.Z before anything else is believed
```

**All three flags and the version assertion are load-bearing.** Measured during
the 2.27.1 release: `uv tool install spec-runner==2.27.1` reported
`unsatisfiable` from a stale index and left the *previous* binary in place. The
scenario then ran green — against the old build. The run looked like a
verification and was not one.

`--no-cache` joined the line after 2.30.0, where `--refresh --reinstall` alone
hit the same stale index — the **fourth** occurrence (2.27.1, 2.28.1, 2.28.3,
2.30.0). It is not a fallback to reach for when the install misbehaves: by the
time you notice, the rehearsal has already run against the wrong binary. The
cost is one slower install; the cost of the alternative is a verification that
proves nothing, convincingly.

That is a defect in how a test stand is attributed, not in spec-runner, and it
is fixed by discipline rather than by a feature: **a run counts only after the
version printed matches the version expected.**

Then run something real. Unit tests are green in the checkout by construction;
what the artifact test is for is ordering, exit codes, and the interaction
between a stop and the next run — the class that
`docs/superpowers/specs/2026-08-11-tdd-battle-report.md` documents finding three
times.
