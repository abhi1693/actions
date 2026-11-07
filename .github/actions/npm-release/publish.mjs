import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { createHash } from "node:crypto"
import { readFile } from "node:fs/promises"
import path from "node:path"
import { setTimeout as delay } from "node:timers/promises"
import { pathToFileURL } from "node:url"

export const REGISTRY = "https://registry.npmjs.org/"
const stable = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/
const verificationAttempts = 31
const verificationDelay = 60_000
const digest = (bytes, algorithm, encoding) =>
  createHash(algorithm).update(bytes).digest(encoding)

export function validateRelease({
  tag,
  source,
  packed,
  basename,
  bytes,
  checksums,
  expectedName = source.name,
}) {
  assert.equal(typeof tag, "string", "RELEASE_TAG is required")
  assert.ok(
    tag.startsWith("v") && stable.test(tag.slice(1)),
    "Use a stable vX.Y.Z release tag"
  )
  const version = tag.slice(1)
  for (const manifest of [source, packed]) {
    assert.equal(manifest.name, expectedName, "Unexpected package name")
    assert.equal(
      manifest.version,
      version,
      "Tag, source and archive versions must match"
    )
    assert.ok(!manifest.private, "Cannot publish a private package")
  }
  for (const field of ["repository", "exports", "peerDependencies", "engines"])
    assert.deepEqual(
      packed[field],
      source[field],
      `Archive ${field} differs from the checked-out source`
    )
  assert.equal(
    basename,
    `${expectedName.replace(/^@/, "").replace("/", "-")}-${version}.tgz`,
    "Unexpected release archive filename"
  )
  const sha256 = digest(bytes, "sha256", "hex")
  assert.equal(
    checksums,
    `${sha256}  ${basename}\n`,
    "SHA256SUMS must contain only the canonical archive checksum"
  )
  for (const [key, value] of Object.entries(packed.publishConfig || {})) {
    if (key === "registry" || key.endsWith(":registry"))
      assert.equal(
        new URL(value).href,
        REGISTRY,
        "Archive must target the public npm registry"
      )
    if (key === "access")
      assert.equal(value, "public", "Archive must have public access")
    if (key === "provenance")
      assert.equal(value, true, "Archive must enable provenance")
    if (key === "tag")
      assert.equal(value, "latest", "Archive must use the latest dist-tag")
  }
  if (packed.tag !== undefined)
    assert.equal(packed.tag, "latest", "Unexpected archive dist-tag")
  return {
    name: expectedName,
    version,
    sha256,
    integrity: `sha512-${digest(bytes, "sha512", "base64")}`,
  }
}

function registryUrl(value) {
  const url = new URL(value)
  assert.equal(
    url.origin,
    REGISTRY.slice(0, -1),
    "Expected a public npm registry URL"
  )
  assert.ok(!url.username && !url.password && !url.hash, "Invalid registry URL")
  return url.href
}

async function request(url, fetchImpl) {
  const response = await fetchImpl(registryUrl(url), {
    headers: { accept: "application/json", "cache-control": "no-cache" },
    redirect: "error",
    signal: AbortSignal.timeout(10_000),
  })
  if (response.status === 404) return null
  assert.ok(response.ok, `Public npm registry returned HTTP ${response.status}`)
  return response
}

export async function readRegistryJson(url, fetchImpl = fetch) {
  const response = await request(url, fetchImpl)
  if (!response) return null
  const result = await response.json()
  assert.ok(
    result && typeof result === "object" && !Array.isArray(result),
    "Invalid registry metadata"
  )
  return result
}

function compareVersions(left, right) {
  assert.match(left, stable, "Registry latest must be a stable version")
  for (const [index, part] of left.split(".").entries()) {
    const difference = BigInt(part) - BigInt(right.split(".")[index])
    if (difference) return difference > 0n ? 1 : -1
  }
  return 0
}

export async function verifyRegistryContent(
  metadata,
  release,
  bytes,
  fetchImpl = fetch
) {
  assert.equal(metadata.name, release.name, "Registry package name differs")
  assert.equal(
    metadata.version,
    release.version,
    "Registry package version differs"
  )
  assert.equal(
    metadata.dist?.integrity,
    release.integrity,
    "Published SHA512 integrity differs from the release archive"
  )
  const response = await request(metadata.dist.tarball, fetchImpl)
  if (!response) return null
  assert.ok(
    Buffer.from(await response.arrayBuffer()).equals(bytes),
    "Published tarball bytes differ from the release archive"
  )
  const attestations = metadata.dist.attestations
  if (attestations === undefined) return { provenance: null }
  const predicateType = attestations?.provenance?.predicateType
  assert.equal(typeof predicateType, "string", "Invalid provenance metadata")
  assert.ok(
    predicateType.startsWith("https://slsa.dev/provenance/"),
    "Unexpected provenance predicate type"
  )
  return { provenance: { url: registryUrl(attestations.url), predicateType } }
}

export function publishArgs(archive, dryRun) {
  return [
    "publish",
    archive,
    `--registry=${REGISTRY}`,
    "--access=public",
    "--provenance=true",
    "--ignore-scripts",
    "--tag=latest",
    ...(dryRun ? ["--dry-run"] : []),
  ]
}

export async function publishArchive({
  archive,
  tag,
  cwd = process.cwd(),
  dryRun = false,
  expectedName,
  fetchImpl = fetch,
  runNpm = (args) =>
    execFileSync("npm", args, {
      cwd,
      stdio: "inherit",
      env: { ...process.env, npm_config_registry: REGISTRY },
    }),
  sleep = delay,
  report = (event) => console.info(JSON.stringify(event)),
}) {
  archive = path.resolve(cwd, archive)
  const bytes = await readFile(archive)
  const source = JSON.parse(
    await readFile(path.join(cwd, "package.json"), "utf8")
  )
  const packed = JSON.parse(
    execFileSync("tar", ["-xOzf", archive, "package/package.json"], {
      encoding: "utf8",
      maxBuffer: 1024 * 1024,
    })
  )
  const checksums = await readFile(
    path.join(path.dirname(archive), "SHA256SUMS"),
    "utf8"
  )
  const release = validateRelease({
    tag,
    source,
    packed,
    basename: path.basename(archive),
    bytes,
    checksums,
    expectedName,
  })
  const packageUrl = REGISTRY + encodeURIComponent(release.name)
  if (dryRun) {
    await runNpm(publishArgs(archive, true))
    return { ...release, status: "dry-run", latest: null, provenance: null }
  }
  const versionUrl = `${packageUrl}/${release.version}`
  const existing = await readRegistryJson(versionUrl, fetchImpl)
  let publishError
  if (!existing) {
    const metadata = await readRegistryJson(packageUrl, fetchImpl)
    if (metadata) {
      assert.equal(metadata.name, release.name, "Registry package name differs")
      assert.ok(
        typeof metadata["dist-tags"]?.latest === "string",
        "Registry package has no latest dist-tag"
      )
      assert.ok(
        compareVersions(metadata["dist-tags"].latest, release.version) < 0,
        "Refusing to move latest backwards or replace an existing version"
      )
    }
    try {
      await runNpm(publishArgs(archive, false))
    } catch (error) {
      publishError = error
    }
  }
  for (let attempt = 0; attempt < verificationAttempts; attempt++) {
    try {
      const metadata = await readRegistryJson(versionUrl, fetchImpl)
      if (metadata) {
        const content = await verifyRegistryContent(
          metadata,
          release,
          bytes,
          fetchImpl
        )
        if (content) {
          const packument = await readRegistryJson(packageUrl, fetchImpl)
          if (packument) {
            assert.equal(
              packument.name,
              release.name,
              "Registry package name differs"
            )
            const latest = packument["dist-tags"]?.latest
            if (
              typeof latest === "string" &&
              compareVersions(latest, release.version) >= 0
            )
              return {
                ...release,
                status: existing ? "already-published" : "published",
                latest,
                ...content,
              }
          }
        }
      }
    } catch (error) {
      if (!publishError) throw error
    }
    if (attempt < verificationAttempts - 1) {
      report({
        status: "waiting-for-registry",
        stage: existing
          ? "verify-existing-release"
          : publishError
            ? "verify-publish-outcome"
            : "npm-processing",
        name: release.name,
        version: release.version,
        attempt: attempt + 1,
        maximum_attempts: verificationAttempts,
        retry_in_seconds: verificationDelay / 1000,
        message:
          "Waiting for npm registry metadata and archive visibility; no additional publish will be attempted.",
      })
      await sleep(verificationDelay)
    }
  }
  throw (
    publishError ||
    new Error(
      `Published archive/latest verification did not converge after ${verificationAttempts} attempts`
    )
  )
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href
) {
  const [archive, ...flags] = process.argv.slice(2)
  try {
    assert.ok(
      archive &&
        flags.length <= 1 &&
        flags.every((flag) => flag === "--dry-run"),
      "Usage: RELEASE_TAG=vX.Y.Z node scripts/publish-npm.mjs <archive.tgz> [--dry-run]"
    )
    console.info(
      JSON.stringify(
        await publishArchive({
          archive,
          tag: process.env.RELEASE_TAG,
          expectedName: process.env.PACKAGE_NAME,
          dryRun: flags.includes("--dry-run"),
        })
      )
    )
  } catch (error) {
    console.error(error.message)
    process.exitCode = 1
  }
}
