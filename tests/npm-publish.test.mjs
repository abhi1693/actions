import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { createHash } from "node:crypto"
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import test from "node:test"
import {
  REGISTRY,
  publishArchive,
  readRegistryJson,
  validateRelease,
} from "../.github/actions/npm-release/publish.mjs"

const PACKAGE = "@fixture/example"
const packageUrl = REGISTRY + encodeURIComponent(PACKAGE)
const manifest = {
  name: PACKAGE,
  version: "0.0.1",
  publishConfig: { access: "public" },
}
const hash = (bytes, algorithm, encoding) =>
  createHash(algorithm).update(bytes).digest(encoding)

async function fixture(t, packed = manifest) {
  const cwd = await mkdtemp(path.join(tmpdir(), "rbac-publish-test-"))
  t.after(() => rm(cwd, { recursive: true, force: true }))
  await mkdir(path.join(cwd, "input/package"), { recursive: true })
  await writeFile(path.join(cwd, "package.json"), JSON.stringify(manifest))
  await writeFile(
    path.join(cwd, "input/package/package.json"),
    JSON.stringify(packed)
  )
  const basename = "fixture-example-0.0.1.tgz"
  const archive = path.join(cwd, basename)
  execFileSync("tar", [
    "-czf",
    archive,
    "-C",
    path.join(cwd, "input"),
    "package",
  ])
  const bytes = await readFile(archive)
  const checksums = `${hash(bytes, "sha256", "hex")}  ${basename}\n`
  await writeFile(path.join(cwd, "SHA256SUMS"), checksums)
  return {
    cwd,
    archive,
    bytes,
    checksums,
    basename,
    source: manifest,
    packed,
    tag: "v0.0.1",
  }
}

function registry(
  f,
  {
    exists = false,
    latest = exists ? "0.0.1" : null,
    pending = 0,
    metadata = {},
    tarball = f.bytes,
  } = {}
) {
  const calls = [],
    uploads = [],
    waits = [],
    events = []
  let published = exists
  let reads = 0
  const fetchImpl = async (url) => {
    calls.push(url)
    if (url === `${packageUrl}/0.0.1`) {
      if (!published || reads++ < pending)
        return new Response("not found", { status: 404 })
      return Response.json({
        name: PACKAGE,
        version: "0.0.1",
        dist: {
          integrity: `sha512-${hash(f.bytes, "sha512", "base64")}`,
          tarball: `${REGISTRY}artifact.tgz`,
        },
        ...metadata,
      })
    }
    if (url === packageUrl)
      return latest
        ? Response.json({ name: PACKAGE, "dist-tags": { latest } })
        : new Response("not found", { status: 404 })
    if (url === `${REGISTRY}artifact.tgz`) return new Response(tarball)
    throw new Error(`Unexpected registry request: ${url}`)
  }
  return {
    calls,
    uploads,
    waits,
    events,
    fetchImpl,
    runNpm: async (args) => {
      uploads.push(args)
      published = true
      latest = "0.0.1"
    },
    sleep: async (milliseconds) => waits.push(milliseconds),
    report: (event) => events.push(event),
  }
}

test("release identity, bytes and canonical checksum are checked before publication", async (t) => {
  const f = await fixture(t)
  const valid = validateRelease(f)
  assert.equal(valid.version, "0.0.1")
  for (const changed of [
    { tag: undefined },
    { tag: "v0.0.1-beta.1" },
    { tag: "0.0.1" },
    { tag: "v0.0.2" },
    { source: { ...manifest, version: "0.0.2" } },
    { packed: { ...manifest, version: "0.0.2" } },
    { packed: { ...manifest, name: "@other/package" } },
    {
      packed: {
        ...manifest,
        repository: { url: "https://example.com/repo.git" },
      },
    },
    { packed: { ...manifest, exports: { ".": "./other.js" } } },
    {
      packed: {
        ...manifest,
        peerDependencies: { "@medusajs/medusa": "^2.0.0" },
      },
    },
    { packed: { ...manifest, engines: { node: ">=18" } } },
    { basename: "other.tgz" },
    { bytes: Buffer.from("changed archive") },
    { checksums: f.checksums.replace("  ", " ") },
    { checksums: f.checksums + f.checksums },
    {
      packed: {
        ...manifest,
        publishConfig: { registry: "https://example.com" },
      },
    },
    { packed: { ...manifest, publishConfig: { access: "restricted" } } },
    { packed: { ...manifest, publishConfig: { provenance: false } } },
  ])
    assert.throws(() => validateRelease({ ...f, ...changed }))
  await writeFile(path.join(f.cwd, "SHA256SUMS"), "wrong checksum\n")
  await assert.rejects(
    publishArchive({
      ...f,
      runNpm: () => assert.fail("must not publish"),
      fetchImpl: () => assert.fail("must not query registry"),
    }),
    /SHA256SUMS/
  )
})

test("the actual tar manifest cannot substitute another version", async (t) => {
  const f = await fixture(t, { ...manifest, version: "0.0.2" })
  await assert.rejects(
    publishArchive({ ...f, runNpm: () => assert.fail("must not publish") }),
    /versions must match/
  )
})

test("only HTTP 404 means a registry version is absent", async () => {
  assert.equal(
    await readRegistryJson(
      packageUrl,
      async () => new Response("not JSON", { status: 404 })
    ),
    null
  )
  for (const status of [401, 403, 429, 500, 503])
    await assert.rejects(
      readRegistryJson(
        packageUrl,
        async () => new Response("unavailable", { status })
      ),
      new RegExp(`HTTP ${status}`)
    )
  await assert.rejects(
    readRegistryJson(packageUrl, async () => {
      throw new Error("offline")
    }),
    /offline/
  )
  await assert.rejects(
    readRegistryJson(packageUrl, async () => new Response("invalid JSON")),
    SyntaxError
  )
})

test("new publication uses the exact archive and verifies registry bytes and latest", async (t) => {
  const f = await fixture(t),
    r = registry(f)
  const result = await publishArchive({ ...f, ...r })
  assert.equal(result.status, "published")
  assert.equal(result.latest, "0.0.1")
  assert.equal(result.provenance, null)
  assert.equal(r.uploads.length, 1)
  const [args] = r.uploads
  assert.equal(args[0], "publish")
  assert.equal(args[1], f.archive)
  for (const flag of [
    `--registry=${REGISTRY}`,
    "--access=public",
    "--provenance=true",
    "--ignore-scripts",
    "--tag=latest",
  ])
    assert.ok(
      args.includes(flag),
      `Missing required publication policy ${flag}`
    )
  assert.ok(!args.includes("--dry-run"))
})

test("retry accepts identical immutable bytes and preserves a newer latest version", async (t) => {
  const f = await fixture(t),
    r = registry(f, { exists: true, latest: "0.0.10" })
  const result = await publishArchive({ ...f, ...r })
  assert.equal(result.status, "already-published")
  assert.equal(result.latest, "0.0.10")
  assert.deepEqual(r.uploads, [])
})

test("immutable retries reject mismatched integrity and downloaded bytes", async (t) => {
  const f = await fixture(t)
  for (const options of [
    {
      metadata: {
        dist: { integrity: "sha512-wrong", tarball: `${REGISTRY}artifact.tgz` },
      },
    },
    { tarball: Buffer.from("different registry archive") },
    { metadata: { name: "@other/package" } },
    { metadata: { version: "0.0.2" } },
    {
      metadata: {
        dist: {
          integrity: `sha512-${hash(f.bytes, "sha512", "base64")}`,
          tarball: "https://example.com/archive.tgz",
        },
      },
    },
  ]) {
    const r = registry(f, { exists: true, ...options })
    await assert.rejects(
      publishArchive({ ...f, ...r }),
      /differ|Expected a public npm registry URL/
    )
    assert.deepEqual(r.uploads, [])
  }
})

test("absent older releases cannot move latest backwards", async (t) => {
  const f = await fixture(t),
    r = registry(f, { latest: "0.0.10" })
  await assert.rejects(publishArchive({ ...f, ...r }), /move latest backwards/)
  assert.deepEqual(r.uploads, [])
})

test("post-publication convergence is bounded to 31 attempts and thirty minutes of delays", async (t) => {
  const f = await fixture(t),
    r = registry(f, { pending: 30 })
  assert.equal((await publishArchive({ ...f, ...r })).status, "published")
  assert.equal(r.waits.length, 30)
  assert.equal(
    r.waits.reduce((total, value) => total + value, 0),
    1_800_000
  )
  const never = registry(f, { pending: 31 })
  await assert.rejects(publishArchive({ ...f, ...never }), /31 attempts/)
  assert.equal(never.calls.filter((url) => url.endsWith("/0.0.1")).length, 32)
  assert.equal(never.uploads.length, 1)
  assert.deepEqual(never.waits, Array(30).fill(60_000))
})

test("npm processing can delay registry visibility for twelve minutes without another publish or false completion", async (t) => {
  const f = await fixture(t),
    r = registry(f, { pending: 12 })
  const result = await publishArchive({ ...f, ...r })
  assert.equal(result.status, "published")
  assert.equal(r.uploads.length, 1)
  assert.deepEqual(r.waits, Array(12).fill(60_000))
  assert.equal(r.events.length, 12)
  assert.ok(
    r.events.every(
      (event) =>
        event.status === "waiting-for-registry" &&
        event.stage === "npm-processing"
    )
  )
  assert.equal(r.events.at(-1).attempt, 12)
  assert.ok(
    r.events.every(
      (event) =>
        event.retry_in_seconds === 60 &&
        /no additional publish/.test(event.message)
    )
  )
})

test("registry provenance metadata is reported when available and malformed metadata fails", async (t) => {
  const f = await fixture(t)
  const attestations = {
    url: `${REGISTRY}-/npm/v1/attestations/package@0.0.1`,
    provenance: { predicateType: "https://slsa.dev/provenance/v1" },
  }
  const dist = {
    integrity: `sha512-${hash(f.bytes, "sha512", "base64")}`,
    tarball: `${REGISTRY}artifact.tgz`,
    attestations,
  }
  const r = registry(f, { exists: true, metadata: { dist } })
  assert.deepEqual((await publishArchive({ ...f, ...r })).provenance, {
    url: attestations.url,
    predicateType: attestations.provenance.predicateType,
  })
  const invalid = registry(f, {
    exists: true,
    metadata: { dist: { ...dist, attestations: {} } },
  })
  await assert.rejects(
    publishArchive({ ...f, ...invalid }),
    /Invalid provenance metadata/
  )
})

test("dry run invokes npm dry-run and never reports a registry publication", async (t) => {
  const f = await fixture(t),
    calls = []
  const result = await publishArchive({
    ...f,
    dryRun: true,
    fetchImpl: () => assert.fail("dry run does not verify a publication"),
    runNpm: async (args) => calls.push(args),
  })
  assert.equal(result.status, "dry-run")
  assert.equal(result.latest, null)
  assert.equal(calls.length, 1)
  assert.equal(calls[0][1], f.archive)
  assert.ok(calls[0].includes("--dry-run"))
  assert.ok(calls[0].includes("--ignore-scripts"))
})

test("a lost npm response succeeds only after the uploaded archive is verified", async (t) => {
  const f = await fixture(t),
    r = registry(f)
  const result = await publishArchive({
    ...f,
    ...r,
    runNpm: async (args) => {
      await r.runNpm(args)
      throw new Error("Upload response lost")
    },
  })
  assert.equal(result.status, "published")
  assert.equal(result.latest, "0.0.1")
  assert.equal(r.uploads.length, 1)
  const changed = registry(f, {
    tarball: Buffer.from("unrelated uploaded bytes"),
  })
  const lost = new Error("Upload response lost")
  await assert.rejects(
    publishArchive({
      ...f,
      ...changed,
      runNpm: async (args) => {
        await changed.runNpm(args)
        throw lost
      },
    }),
    (error) => error === lost
  )
  assert.deepEqual(changed.waits, Array(30).fill(60_000))
  assert.ok(
    changed.events.every((event) => event.stage === "verify-publish-outcome")
  )
})

test("a denied publication without registry bytes preserves the original npm error", async (t) => {
  const f = await fixture(t),
    r = registry(f)
  const denied = new Error("npm publish was denied")
  await assert.rejects(
    publishArchive({
      ...f,
      ...r,
      runNpm: async () => {
        throw denied
      },
    }),
    (error) => error === denied
  )
  assert.deepEqual(r.waits, Array(30).fill(60_000))
  assert.ok(r.events.every((event) => event.stage === "verify-publish-outcome"))
})
