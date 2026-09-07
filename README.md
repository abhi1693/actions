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

Use the existing `docker-build-push.yml` with `native-multi-platform: true` and
`push: true` to build on native AMD64 and ARM64 runners and assemble one verified
OCI index. The default is `false`, preserving existing single-runner/QEMU callers.
All existing build, metadata, secret and cache inputs remain supported.

Set `image-title` (for example `Admin API`) for distinct service/platform job
names. Use a unique `cache-scope` for each concurrent image; native mode adds
architecture suffixes to its registry/GHA caches and transient digest artifacts.
The intermediate platform tags are unique candidates. The final index uses the
caller's existing `tags`/`flavor` rules and returns `digest`, `tags` and `labels`.
Callers control mutable branch aliases and immutable release-tag promotion after
scanning; they must grant `contents: read` and `packages: write`.
