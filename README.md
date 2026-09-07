# Shared GitHub Actions

Shared reusable workflows for `abhi1693` repositories.

## Docker Build Push

Use `.github/workflows/docker-build-push.yml` from a caller workflow job to
build and publish Docker images with the common home-lab setup:

- checkout
- Docker Buildx setup
- GHCR login
- Docker metadata generation
- Docker build/push with registry and GitHub Actions cache

Example:

```yaml
jobs:
  build:
    uses: abhi1693/actions/.github/workflows/docker-build-push.yml@master
    permissions:
      contents: read
      packages: write
    with:
      runs-on: ubuntu-24.04-arm
      image: ghcr.io/${{ github.repository }}/example
      context: .
      file: Dockerfile
      platforms: linux/amd64,linux/arm64
      qemu: true
      cache-scope: example
      tags: |
        type=raw,value=latest
        type=sha,format=short,prefix=example-
      labels: |
        org.opencontainers.image.source=https://github.com/${{ github.repository }}
        org.opencontainers.image.title=example
    secrets:
      github-token: ${{ secrets.GITHUB_TOKEN }}
```

Optional inputs include `source-artifact`, `source-artifact-path`, `qemu`,
`target`, `build-args`, `secret-files`, and `build-secret-artifact`/
`build-secret-file`/`build-secret-id` for Docker BuildKit secret files produced
by a previous job.

## Python UV Tests

Use `.github/workflows/python-uv-tests.yml` to run a Python project that uses
`uv` for dependency management.

Example:

```yaml
jobs:
  tests:
    uses: abhi1693/actions/.github/workflows/python-uv-tests.yml@master
    with:
      python-version: "3.13"
      sync-command: uv sync --extra dev
      lint-command: uv run ruff check .
      test-command: uv run pytest
```

Python callers can set `uv-version` for toolchain parity and
`test-results-path` / `test-results-name` to retain test reports, including on
failure. Matrix callers must use distinct artifact names. Docker callers can
set `flavor: latest=false` to disable automatic moving tags. All new inputs are
optional; existing callers retain their defaults. Action references are pinned
to commit SHAs and maintained by Dependabot.

## Native multi-platform Docker images

`docker-multi-platform.yml` calls `docker-build-push.yml` on native AMD64 and
ARM64 runners, then combines their exact digests into a verified OCI index.
It accepts `image`, `file`, `context`, `labels`, `cache-scope`, and the
`github-token` secret. Outputs are `digest` and `tags` for the resulting index.
Callers must grant `contents: read` and `packages: write` and gate the call on
successful tests/security checks. This workflow always publishes candidates;
callers own subsequent vulnerability scanning and release approval.

Tags are `sha-<full-commit>-<run-id>-<run-attempt>`; native intermediates add
`-amd64` or `-arm64`. Partial reruns compute their identity within the rerun job.
No latest/version aliases are generated. Deploy digests only after verification.
