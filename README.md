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
