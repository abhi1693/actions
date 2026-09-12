# Validation and releases

`validate.yml` runs actionlint, Python contract tests and small reusable-workflow
fixtures for Node, Python, dependency/security checks and a native container.
The container fixture exercises local loading, vulnerability scanning and a real
runtime smoke script. Configured reports must exist even when their job fails.

Local checks:

```sh
uv run --with PyYAML==6.0.3 python -m unittest discover -s tests -v
uvx ruff==0.16.7 check --isolated --select E4,E7,E9,F,I .github/actions/workflow-tools tests
uvx ruff==0.16.7 format --isolated --check .github/actions/workflow-tools tests
python3 tests/run_smoke.py
```

The local smoke script uses npm, uv, Go and Docker. It starts a temporary
loopback-only registry, exercises exact digest promotion and a promotion-only
retry, and removes its containers/images. It does not publish to GitHub or GHCR.

These workflows target GitHub.com and Linux runners. Shared helper actions use
GitHub's [`$/` self-repository reference](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#example-using-an-action-in-the-same-repository-as-the-workflow-at-the-running-commit-recommended),
so helpers come from the same commit as the reusable workflow, including when
called from a different repository. This syntax is not supported on GHES.
Actionlint 1.7.12 predates that syntax: validation ignores only its missing-ref
error for the exact `$/ .github/actions/workflow-tools` path (without the space).
Contract tests separately verify that the helper exists and external actions are
SHA-pinned. Remove this narrow exception when actionlint supports self references.

Before advancing a shared major version, run the central fixtures and representative
consumer PR checks. Review changes to job names and required checks during each
consumer migration. Use one caller revision during a staged rollout; do not retain
both old and replacement jobs after validation.

Once the tested commit is on `master`, manually dispatch **Release shared workflows**
with a stable version such as `1.0.0`. This reruns the central validation workflow,
creates the immutable `v1.0.0` tag/release and advances `v1` using a Git lease.
An existing immutable tag pointing elsewhere, a branch other than master, or a
version below a published release in the same major is rejected. The lease also
rejects a concurrent major-ref change. First-party callers using `@v1` receive
compatible changes after promotion; SHA-pinned callers update explicitly.

No major ref is created merely by adding these files. Merging and publishing a
shared version are separate rollout steps. A rollback of a major ref is a deliberate
operator action to the recorded previously tested SHA, not a normal release.

Container promotion preflights every selected image before writing any final tag.
It preserves immutable full-semver tags and permits moving branch/latest/major/minor
aliases. Registry updates across several images are not atomic: a registry failure
can leave partial tag updates. Retry the promotion job against the same verified
artifacts; do not rebuild immutable release versions to recover a tag-write failure.
All reusable calls within one run must use distinct artifact scopes/names. Platform
and component artifacts may be reused by a partial retry only for the same run,
revision, image and platform set. Artifacts from another run are rejected.
