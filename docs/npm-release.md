# npm package releases

`npm-release.yml` prepares packages; `npm-publish.yml` publishes prepared packages. Together they own the build/archive checks, checksums, GitHub provenance,
draft assets, exact tag/source validation, npm publish and registry byte verification.
Consumers keep their CI, event caller, package metadata, notes and optional product smoke check.
Pin both callers to a reviewed actions commit; derive the preparation revision from the immutable release tag for provenance verification. No npm token is accepted or inherited.

Call after your CI job with `operation: prepare`, `package-name` and `version`,
granting contents, id-token and attestations write. It creates a draft only.
Use `package-check-preset: medusa-access-client` for a Medusa access client package:
the shared consumer check verifies both ESM and CommonJS imports, factory behavior,
and encoded actor paths against the extracted archive. No check script is copied
into the consumer. With no preset, generic package/export/README checks still run.
`build-command` defaults to `npm run build`.
`working-directory` defaults to `.`, `notes-directory` to `docs/releases`, and
`default-branch` to `master`. Stable versions must already match both manifests.

From a `release: types: [published]` caller, call `npm-publish.yml` with `package-name`
and `tag: ${{ github.event.release.tag_name || inputs.tag }}`. Grant contents and
attestations read plus id-token write. A recovery workflow_dispatch runs from the reviewed default branch,
checks out the immutable release tag, and requires that tag to match the published
release target and belong to the default branch. The publisher automatically reads the pinned builder revision from the tagged
preparation caller, then requires the signed provenance to match that revision
and the tagged source. There is no per-release SHA input. For a differently named
preparation caller, set the stable `preparation-workflow` filename once. Never move a release tag. Keep the caller filename (for example publish.yml) registered
as the npm trusted publisher in the consumer repository. npm validates the caller
identity for reusable workflows: https://docs.npmjs.com/trusted-publishers/ .
Use GitHub-hosted runners, Node 24/npm >=11.5.1 and no token fallback.

`operation: check` runs installation, build and real archive verification without
creating releases or publishing. This is CI evidence, not proof of npm authentication.
The workflow validates provenance against the shared signer workflow and exact
shared commit, while separately checking the consumer source commit. It refuses
foreign-tag recovery, unprepared assets, altered bytes and backwards latest moves.
An existing immutable version succeeds only when registry integrity and downloaded
bytes match. Publication is attempted once; bounded registry polling never republishes.

## RBAC rollout gate

RBAC currently publishes 0.0.1. Land the reviewed actions and RBAC caller changes,
choose and approve the next version (normally 0.0.2), update package.json and both
lockfile root versions, and add docs/releases/VERSION.md. Run Prepare release on
that exact merged commit, inspect the draft assets and validation, then approve
publishing the GitHub release. Only a successful npm publish with matching archive
bytes/provenance proves the rollout. Migrate no other plugins until this succeeds.
Do not retry 0.0.1 through the new signer: it was prepared by the old workflow.

## Package-specific file contracts

Declare any additional distributable files in package.json under
`config.release.requiredFiles`, an array of archive-relative paths. The shared
packager checks these in addition to its standard manifest, README, license,
export, image and private-file checks. This is useful for generated migration
snapshots and discovered API routes that are not explicit package exports.
The same configuration applies to release preparation and local/CI packaging
checks; consumers need no copy of the shared validation implementation.
