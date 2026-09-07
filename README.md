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

## Configurable platform matrix

The existing `docker-build-push.yml` accepts `matrix`, a JSON array of runner and
platform pairs. Without it, the existing `runs-on`/`platforms` inputs are used;
the default remains `ubuntu-24.04-arm` / `linux/arm64`.

```yaml
with:
  image-title: Admin API
  image: ghcr.io/${{ github.repository }}/admin-api
  file: apps/admin-api/Dockerfile
  cache-scope: admin-api
  matrix: |
    [
      {"runner": "ubuntu-24.04", "platform": "linux/amd64"},
      {"runner": "ubuntu-24.04-arm", "platform": "linux/arm64"}
    ]
  tags: type=ref,event=branch
```

Each matrix entry builds independently. With `push: true`, their exact digests
are assembled into one index, checked against the requested platform set, and
returned as `digest`, `tags` and `labels`. With `push: false`, every build is
checked without publishing or assembling an index; matrix outputs are empty.
Use matching native runners to avoid emulation, or opt into `qemu` when needed.

`image-title` provides distinct service/platform job names. Use a unique
`cache-scope` per concurrent image; registry/GHA caches and transient digest
artifacts are separated by platform. Intermediate platform tags are temporary
candidates, while the final index follows the caller's `tags`/`flavor` rules.
Callers own post-build scanning, moving branch aliases, and immutable release-tag
promotion. All prior build, metadata, secret and cache inputs remain supported.
