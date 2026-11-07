import { checkPackage } from "./check-package.mjs"
import assert from "node:assert/strict"
import { createHash } from "node:crypto"
import { execFileSync } from "node:child_process"
import {
  access,
  appendFile,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"

const root = process.cwd()
const args = process.argv.slice(2)
assert.ok(
  args.every((arg) => arg === "--check-only"),
  "Unknown packaging argument"
)
const checkOnly = args.includes("--check-only")
const pkg = JSON.parse(await readFile(path.join(root, "package.json"), "utf8"))
const lock = JSON.parse(
  await readFile(path.join(root, "package-lock.json"), "utf8")
)
assert.equal(
  lock.version,
  pkg.version,
  "Lockfile version must match package.json"
)
assert.equal(
  lock.packages[""].version,
  pkg.version,
  "Lockfile root version must match package.json"
)
assert.equal(
  lock.packages[""].name,
  pkg.name,
  "Lockfile root name must match package.json"
)
assert.equal(pkg.name, process.env.PACKAGE_NAME, "Unexpected source package name")
assert.ok(!pkg.private, "Cannot release a private package")
const version = process.env.RELEASE_VERSION || pkg.version
assert.match(version, /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/)
assert.equal(pkg.version, version, "Release version must match package.json")
let revision = null
try {
  revision = execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim()
} catch (error) {
  if (!checkOnly) throw error
}
if (process.env.GITHUB_SHA)
  assert.equal(
    revision,
    process.env.GITHUB_SHA,
    "Checkout differs from validated commit"
  )
if (!checkOnly) {
  const dirty = execFileSync("git", ["status", "--porcelain"], {
    cwd: root,
    encoding: "utf8",
  })
  assert.equal(
    dirty,
    "",
    "Release packaging requires a clean committed checkout"
  )
}

const temporary = await mkdtemp(path.join(tmpdir(), "npm-release-package-"))
const output = checkOnly
  ? temporary
  : path.resolve(process.env.RELEASE_OUTPUT_DIR || path.join(root, ".release"))
assert.ok(!/[\r\n]/.test(output), "Release output path must be a single line")
await mkdir(output, { recursive: true })

try {
  const [packed] = JSON.parse(
    execFileSync(
      "npm",
      ["pack", "--ignore-scripts", "--json", "--pack-destination", output],
      { cwd: root, encoding: "utf8" }
    )
  )
  assert.equal(packed.name, pkg.name)
  assert.equal(packed.version, version)
  assert.match(packed.filename, /^[A-Za-z0-9._-]+\.tgz$/)
  const archive = path.join(output, packed.filename)
  const files = new Set(packed.files.map((file) => file.path))
  for (const name of files) {
    assert.ok(
      !path.isAbsolute(name) && !name.split("/").includes(".."),
      "Unsafe package path"
    )
    assert.ok(
      !/(^|\/)(?:node_modules|tests|\.git)(\/|$)|(^|\/)\.env(?:\.|$)/.test(
        name
      ),
      "Private or test files must not ship"
    )
  }
  for (const required of ["package.json", "README.md", "LICENSE"])
    assert.ok(files.has(required), `Missing package file: ${required}`)
  const readme = await readFile(path.join(root, "README.md"), "utf8")
  const images = new Set(
    [
      ...readme.matchAll(/!\[[^\]]*\]\(<?(docs\/assets\/[^)\s>]+)>?\)/g),
      ...readme.matchAll(/<img\b[^>]*\bsrc=["'](docs\/assets\/[^"']+)["']/gi),
    ].map((match) => decodeURIComponent(match[1].split(/[?#]/)[0]))
  )
  for (const image of images)
    assert.ok(files.has(image), `Missing packed README image: ${image}`)
  const targets = (value) =>
    typeof value === "string" ? [value] : value ? Object.values(value).flatMap(targets) : []
  const exports = targets(pkg.exports).filter((target) => !target.includes("*"))
  for (const target of exports) {
    assert.ok(
      target.startsWith("./") && !target.split("/").includes(".."),
      "Unsafe package export"
    )
    assert.ok(files.has(target.slice(2)), `Missing packed export: ${target}`)
  }

  const extracted = path.join(temporary, "extracted")
  await mkdir(extracted)
  execFileSync("tar", ["-xzf", archive, "-C", extracted])
  const extractedPackage = path.join(extracted, "package")
  for (const target of exports)
    await access(path.join(extractedPackage, target))
  await checkPackage(extractedPackage, pkg, process.env.PACKAGE_CHECK_PRESET)
  const digest = createHash("sha256")
    .update(await readFile(archive))
    .digest("hex")
  const checksums = path.join(output, "SHA256SUMS")
  await writeFile(checksums, `${digest}  ${packed.filename}\n`)
  if (process.env.GITHUB_OUTPUT && !checkOnly)
    await appendFile(
      process.env.GITHUB_OUTPUT,
      `tag=v${version}\npackage-path=${archive}\nchecksums-path=${checksums}\n`
    )
  console.info(
    JSON.stringify({
      name: pkg.name,
      version,
      revision,
      exports: exports.length,
      readme_images: images.size,
      package: checkOnly ? "verified temporary archive" : archive,
      sha256: digest,
      consumer_check: process.env.PACKAGE_CHECK_PRESET || "exports-only",
    })
  )
} finally {
  await rm(temporary, { recursive: true, force: true })
}
