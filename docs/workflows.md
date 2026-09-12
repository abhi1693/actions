# Workflow contracts

Call reusable workflows at job level. Callers own events, `needs`, permissions and
repository-specific scripts. Workflow-level caller `env` values are not inherited;
pass `project-env` explicitly. A `secrets.project-env` JSON object overrides public
application values and is masked before export. Reserved runner/shell variables
cannot be overridden. PostgreSQL uses a health-checked service with a dynamic host
port and exports `CI_DATABASE_URL`; project scripts map that URL to their own setting.

Node stages always run in generate/lint/typecheck/test/build order. `checks` selects
which run; `scripts` maps stages to npm script names, defaulting to `ci:<stage>`.
A missing declared script fails. Optional generated paths are checked for both tracked
and untracked changes. Installation can happen at a monorepo root while scripts run
in a project directory. Test/browser artifact paths are relative to the repository
root, not `working-directory`. Cypress requires a Linux runner with browser system
dependencies; the workflow installs/verifies the project-pinned Cypress binary.
Playwright prepares Chromium and its system dependencies from the project version.

Security audits operate on lockfiles, without running npm lifecycle scripts. Select
thresholds and development dependency coverage explicitly when replacing old checks.
Python audit uses locked uv export plus a pinned pip-audit. CodeQL is opt-in, requires
repository eligibility and `security-events: write`, and fails on reported findings.
Use `none` or `autobuild` build modes; projects needing a custom manual CodeQL build
should retain their own job. Exceptions belong in repository scanner configuration.

Release image tags always omit the leading `v`: GitHub release `v1.2.3` produces
image tag `1.2.3`. This also applies to raw/manual Docker metadata tags and manifest
`extra-tags`. Prereleases retain their suffix (for example, `1.2.3-rc.1`); moving
aliases such as `latest` remain available. Git release tag names are unchanged.

Container builds enable SBOM generation, maximum-detail BuildKit provenance, and
HIGH/CRITICAL vulnerability plus secret scanning by default. Vulnerabilities without
fixes still count. The container suite also enables GitHub build attestations.
Every image uses the same native runner matrix, including single-platform images.
Final tags follow platform checks and verification of the assembled candidate index.

SBOM generation is independent of vulnerability scanning. No-push checks retain
CycloneDX/scan reports as artifacts; published images also carry BuildKit SBOM and
provenance attestations. Docker's classic local image store cannot retain those
registry attestations. See [Docker's attestation guidance](https://docs.docker.com/build/ci/github-actions/attestations/).
Maximum-detail provenance includes build arguments: pass credentials using BuildKit
secret inputs (`build-env` or `secret-files`). Explicit opt-outs remain available
for callers that require them; security checks are never silently downgraded.

Repository-specific npm resolution settings belong in the project `.npmrc`, as
recommended by [npm ci](https://docs.npmjs.com/cli/v11/commands/npm-ci/). The shared
workflow runs locked installs without separate dependency-compatibility modes.

## Image manifests

`container-images.yml` reads a JSON file with `schema-version: 1`, `images`, and optional
`shared-paths`. See [the image example](examples/images.json). Each image has a unique
`id`; optional fields are `image`, `context`, `file`, `target`, `platforms`, `paths`,
`change-dependencies`, `build-args`, `labels`, `smoke-script`, `extra-tags`, `secret-id`.
Unknown fields fail validation. `image` defaults to `ghcr.io/<repository>` for one
image or `ghcr.io/<repository>/<id>` for several. Platforms are native Linux AMD64
and/or ARM64; default ARM64. Specialized metadata policies use the same native
builder directly, with an explicit runner/platform matrix.

Paths use Python fnmatch globs (`*` matches across slashes), conservatively selecting
components. Dockerfile and Docker ignore files always invalidate the relevant image;
manifest changes invalidate all. Unavailable Git history selects all.
`change-dependencies` propagates change selection, not build ordering. For base/derived
image dependencies, use two calls with distinct artifact scopes and `needs`.
Release/tag and explicit-version runs always select the full image set.

Build argument values support `{version}` and `{revision}` substitutions. Labels
for source/revision/version are maintained centrally. A smoke script receives
`IMAGE_REFERENCE PLATFORM VERSION` and must return nonzero on failure. The shared
builder loads no-push images into its own Docker daemon, so the script runs against
the actual built image. Scans, runtime checks and SBOM reports precede digest handoff.

Publishing is opt-in and rejected for PR events, Dependabot, and non-trusted branches.
Stable releases get full semver and optional latest/major/minor aliases; prereleases
never advance those automatic aliases. Branch runs get the branch name and full SHA.
`extra-tags` adds explicit literal tags. Version files are checked against release
versions. Published candidates are verified by digest before attestation/promotion;
GitHub environments can protect the build and its credentials. `secrets.build-env`
is a JSON map from component id to secret file content, passed only to that component
under its `secret-id` (default `env`); secret files are removed after the build.

Image plans and final manifests are artifacts. The final `image-manifest-<scope>-
<run-id>-<attempt>` contains the exact promoted image references for downstream use.
Matrix outputs are not used as a substitute for the complete component result set.
A no-change selection succeeds through `Images required` without building anything.

The input tables below describe the implementation. Examples use `@v1`; before its
first release, use the concrete reviewed shared-workflow SHA for staged migration.

## `node-ci.yml`

| Input | Default | Meaning |
| --- | --- | --- |
| `runs-on` | `ubuntu-24.04` | Linux runner label. |
| `node-version` | `24` | Node version; ignored when node-version-file is provided. |
| `node-version-file` | `` | Node version file relative to repository root. |
| `working-directory` | `.` | Directory containing package.json and the declared scripts. |
| `install-directory` | `` | Lockfile installation directory; defaults to working-directory. |
| `cache-dependency-path` | `package-lock.json` | Lockfile path relative to repository root. |
| `checks` | `["lint","test","build"]` | JSON array of generate, lint, typecheck, test, build. |
| `scripts` | `{}` | JSON map of standard stages to npm scripts; default ci:<stage>. |
| `generated-paths` | `[]` | JSON paths checked for tracked and untracked changes after generation. |
| `ignore-scripts` | `false` | Disable npm install lifecycle scripts. |
| `enable-cache` | `true` | Enable npm download caching. |
| `project-env` | `{}` | JSON object of non-secret application environment variables. |
| `postgres-image` | `` | Optional PostgreSQL service image. Exports CI_DATABASE_URL. |
| `python-version` | `` | Optional Python setup for API generation; empty skips Python/uv. |
| `uv-version` | `` | Optional uv version when Python tooling is enabled. |
| `uv-directory` | `.` | Directory to install Python generation dependencies with uv sync --locked. |
| `uv-sync-args` | `[]` | JSON additional uv sync arguments. |
| `browser` | `none` | Optional browser tooling: none, cypress or playwright. |
| `browser-results-path` | `` | Optional screenshots/videos/traces to retain on failure. |
| `timeout-minutes` | `20` | Maximum duration. |
| `test-results-path` | `` | Test report path relative to the repository root. |
| `test-results-name` | `node-test-results` | Unique report artifact name per caller/matrix job. |
| `database-env-name` | `CI_DATABASE_URL` | Application variable to receive the disposable PostgreSQL URL. |
| `database-url-scheme` | `postgresql` | Database URL scheme, such as postgresql+asyncpg. |
| `prepare-script` | `` | Optional repository Bash script run before dependency installation, e.g. test environment generation. |
| `postgres-database` | `ci` | Disposable PostgreSQL database name, including an application-required test suffix. |


## `python-ci.yml`

| Input | Default | Meaning |
| --- | --- | --- |
| `runs-on` | `ubuntu-24.04` | Runner label for the test job. |
| `python-version` | `3.13` | Python version passed to actions/setup-python. |
| `uv-version` | `` | Optional uv version; an empty value uses setup-uv's default. |
| `test-results-path` | `` | Optional path to test reports to upload, relative to the workspace. |
| `test-results-name` | `python-test-results` | Unique artifact name when test-results-path is set. |
| `working-directory` | `.` | Directory containing the Python project. |
| `sync-command` | `uv sync --locked` | Locked dependency sync; select project extras explicitly. |
| `lint-command` | `` | Optional lint command. Leave empty to skip. |
| `test-command` | `uv run pytest` | Test command. |
| `timeout-minutes` | `20` | Maximum job duration. |
| `enable-cache` | `true` | Enable uv caching. |
| `project-env` | `{}` | JSON non-secret application environment. |
| `postgres-image` | `` | Optional PostgreSQL service image; exports CI_DATABASE_URL. |
| `validation-command` | `` | Optional project validation after tests, such as generated OpenAPI checks. |
| `node-version` | `` | Optional Node setup for projects whose Python checks use npm scripts. |
| `database-env-name` | `CI_DATABASE_URL` | Application variable to receive the disposable PostgreSQL URL. |
| `database-url-scheme` | `postgresql` | Database URL scheme, such as postgresql+asyncpg. |
| `postgres-database` | `ci` | Disposable PostgreSQL database name, including an application-required test suffix. |


## `security.yml`

| Input | Default | Meaning |
| --- | --- | --- |
| `runs-on` | `ubuntu-24.04` | Linux runner label. |
| `secret-scan` | `true` | Scan complete Git history for secrets. |
| `gitleaks-config` | `` | Optional repository-owned gitleaks configuration path. |
| `workflow-lint` | `true` | Run actionlint. |
| `npm-audit` | `false` | Audit npm lockfile. |
| `npm-directory` | `.` | Directory containing the npm lockfile. |
| `node-version` | `24` | Node version for npm auditing. |
| `npm-severity` | `high` | Minimum npm severity: low, moderate, high, critical. |
| `npm-include-dev` | `true` | Include development dependencies. |
| `python-audit` | `false` | Audit locked uv dependencies. |
| `python-directory` | `.` | Directory containing the uv project. |
| `python-version` | `3.13` | Python version for dependency export. |
| `uv-version` | `` | Optional uv version. |
| `python-include-dev` | `true` | Include Python development dependencies. |
| `python-all-packages` | `false` | Audit every package in the uv workspace. |
| `python-extras` | `[]` | JSON list of Python extras to include. |
| `codeql-languages` | `[]` | JSON language list; empty disables CodeQL. |
| `codeql-config` | `` | Optional CodeQL configuration path. |
| `codeql-build-mode` | `none` | CodeQL build mode for supported languages. |
| `artifact-prefix` | `security` | Unique artifact prefix per call. |
| `timeout-minutes` | `30` | Maximum duration per security job. |
| `secret-scan-scope` | `history` | history scans all commits; event scans PR/push commits and all history for other events. |


## `container-images.yml`

| Input | Default | Meaning |
| --- | --- | --- |
| `manifest` | `.github/images.json` | Repository-owned JSON image descriptors. |
| `publish` | `false` | Publish only on trusted branches or release events; PRs must leave false. |
| `version` | `` | Explicit semver for manual releases; otherwise derived from release/tag. |
| `version-file` | `` | Optional package.json or pyproject.toml to check release version parity. |
| `changed-only` | `true` | Select affected images on pushes and PRs. Releases always select all. |
| `artifact-scope` | `images` | Unique scope for concurrent image workflow calls. |
| `trusted-branches` | `["master","main"]` | JSON branch names allowed to publish. |
| `amd64-runner` | `ubuntu-24.04` | Native AMD64 Linux runner. |
| `arm64-runner` | `ubuntu-24.04-arm` | Native ARM64 Linux runner. |
| `scan` | `true` | Generate SBOM and require HIGH/CRITICAL runtime scan to pass. |
| `scan-ignore-unfixed` | `false` | Exclude vulnerabilities without fixes. |
| `attest` | `true` | Attest verified published digests before final promotion. |
| `environment` | `` | Optional GitHub environment for build credentials/protection. |
| `source-artifact` | `` | Optional same-run artifact containing generated build files. |
| `source-artifact-path` | `.` | Download source artifact here relative to workspace. |
| `latest` | `true` | Advance latest for stable releases. |
| `release-aliases` | `[]` | JSON optional moving major/minor release aliases. |


## `docker-build-push.yml`

Use the same native matrix for every build, for example
`matrix: '[{"runner":"ubuntu-24.04-arm","platform":"linux/arm64"}]'`.
Add an AMD64 entry with `ubuntu-24.04` for a multi-platform image. The index job
assembles checked platform digests, verifies the candidate index, and then promotes
its exact digest to the requested tags. Outputs describe that published index;
no-push checks produce reports and leave publication outputs empty.

| Input | Default | Meaning |
| --- | --- | --- |
| `image-title` | `` | Human-readable service name in the Actions job graph. |
| `matrix` | `required` | JSON array of native runner/platform pairs, one platform per job. |
| `image` | `required` | Fully qualified image name passed to docker/metadata-action. |
| `context` | `.` | Docker build context. |
| `source-artifact` | `` | Artifact to download into the checked-out workspace before building. |
| `source-artifact-path` | `.` | Path where the source artifact should be downloaded. |
| `file` | `required` | Dockerfile path. |
| `build-args` | `` | Multiline Docker build args. |
| `target` | `` | Dockerfile target stage. |
| `secret-files` | `` | Multiline Docker secret file mappings. |
| `build-secret-artifact` | `` | Artifact containing a Docker build secret file. |
| `build-secret-file` | `` | File name inside the build secret artifact. |
| `build-secret-id` | `` | Docker BuildKit secret id for the artifact file. |
| `tags` | `required` | Multiline docker/metadata-action tag rules. |
| `flavor` | `` | Optional docker/metadata-action flavor configuration. |
| `labels` | `` | Multiline docker/metadata-action labels. |
| `cache-scope` | `required` | Shared cache scope suffix. |
| `short-sha-length` | `7` | DOCKER_METADATA_SHORT_SHA_LENGTH value. |
| `push` | `false` | Opt in to publishing verified images. |
| `provenance` | `mode=max` | BuildKit provenance mode for published images. |
| `sbom` | `true` | Generate BuildKit SBOM attestations and a runtime CycloneDX report. |
| `scan` | `true` | Block HIGH/CRITICAL vulnerabilities and detected secrets before final tags. |
| `scan-ignore-unfixed` | `false` | Ignore vulnerabilities without fixes. |
| `smoke-script` | `` | Repository Bash script receiving image reference, platform and app version. |
| `app-version` | `` | Version passed to runtime smoke checks. |
| `digest-artifact-name` | `` | Optional unique component result artifact name for downstream promotion. |
| `environment` | `` | Optional GitHub environment for build secrets/protections. |
| `build-env-secret-id` | `env` | BuildKit secret id for the build-env secret. |
