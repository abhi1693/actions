# Shared GitHub Actions

Standard CI, security and container workflows for `abhi1693` repositories.
Application repositories keep their triggers and project configuration; tool
versions, execution stages and release mechanics are maintained here.

| Workflow | Purpose |
| --- | --- |
| [Node CI](.github/workflows/node-ci.yml) | Locked npm installs, named validation stages, generated-file checks, optional PostgreSQL/Python/browser tooling and reports. |
| [Python UV Tests](.github/workflows/python-uv-tests.yml) | uv installation, lint/tests, project validation, optional PostgreSQL and reports. Existing inputs retain their defaults. |
| [Security](.github/workflows/security.yml) | Selectable secret scans, dependency audits, workflow lint and CodeQL findings gates. |
| [Container Images](.github/workflows/container-images.yml) | Select affected components, build native images, verify them, attest and promote exact digests. |

The existing [Docker Build Push](.github/workflows/docker-build-push.yml) remains
the common build implementation and supports existing callers, including custom
metadata, source artifacts and build secrets.

See [workflow contracts and usage](docs/workflows.md), [caller examples](docs/examples),
and [validation and releases](docs/maintenance.md).

Shared workflow versions use Git refs rather than separate versioned files.
Adopt a tested major ref such as `@v1` after its first release; immutable commit
SHAs are available for staged migrations and reproducible consumers.
