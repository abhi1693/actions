import { preparationReference } from "./provenance.mjs"
import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import { execFileSync } from "node:child_process"

const run = (command, args) => execFileSync(command, args, { encoding: "utf8" }).trim()
const gh = args => run("gh", args)
const git = args => run("git", args)
const json = args => JSON.parse(gh(args))
const sha = process.env.RELEASE_SOURCE_SHA || process.env.GITHUB_SHA
const tag = process.env.RELEASE_TAG || `v${process.env.RELEASE_VERSION}`
assert.match(tag, /^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/)
assert.equal(git(["rev-parse", "HEAD"]), sha, "Checkout differs from workflow source")
const pkg = JSON.parse(fs.readFileSync("package.json", "utf8"))
assert.equal(pkg.name, process.env.PACKAGE_NAME, "Unexpected package name")
assert.equal(tag, `v${pkg.version}`, "Tag differs from package version")
const archive = `${pkg.name.replace(/^@/, "").replace("/", "-")}-${pkg.version}.tgz`
const output = (key, value) => fs.appendFileSync(process.env.GITHUB_OUTPUT, `${key}=${value}\n`)

if (process.argv[2] === "source") {
  assert.equal(git(["rev-parse", `${tag}^{commit}`]), sha, "Tag differs from workflow source")
  const branch = process.env.DEFAULT_BRANCH
  assert.match(branch, /^[A-Za-z0-9][A-Za-z0-9._/-]*$/)
  if (process.env.GITHUB_EVENT_NAME === "workflow_dispatch") {
    assert.equal(process.env.GITHUB_REF, `refs/heads/${branch}`, "Run recovery from the reviewed default-branch workflow")
  } else {
    assert.equal(process.env.GITHUB_EVENT_NAME, "release", "Unsupported publication event")
    assert.equal(process.env.GITHUB_REF, `refs/tags/${tag}`, "Release event must match the selected tag")
    assert.equal(process.env.GITHUB_SHA, sha, "Release event differs from tagged source")
  }
  git(["merge-base", "--is-ancestor", sha, `origin/${branch}`])
  const release = json(["release", "view", tag, "--json", "isDraft,isPrerelease,tagName,targetCommitish,assets"])
  assert.equal(release.isDraft, false, "Only published releases can reach npm")
  assert.equal(release.isPrerelease, false, "Only stable releases can reach npm")
  assert.equal(release.tagName, tag)
  assert.equal(release.targetCommitish, sha, "Release target differs from validated source")
  for (const name of [archive, "SHA256SUMS", "provenance.json"])
    assert.equal(release.assets.filter(asset => asset.name === name && asset.state === "uploaded").length, 1, `Missing prepared asset: ${name}`)
  const preparationWorkflow = process.env.PREPARATION_WORKFLOW || "release.yml"
  assert.match(preparationWorkflow, /^[A-Za-z0-9_-]+\.ya?ml$/, "Use a preparation workflow filename")
  const workflow = git(["show", `${tag}:.github/workflows/${preparationWorkflow}`])
  output("preparation-ref", preparationReference(workflow))
  output("archive", archive)
} else if (process.argv[2] === "draft") {
  const notes = path.resolve(process.env.NOTES_DIRECTORY, `${pkg.version}.md`)
  const releaseNotes = fs.existsSync(notes) ? ["--notes-file", notes] : ["--generate-notes"]
  if (fs.existsSync(notes)) assert.ok(fs.statSync(notes).isFile(), "Release notes must be a file")
  const refs = new Map(git(["ls-remote", "--tags", "origin", `refs/tags/${tag}`, `refs/tags/${tag}^{}`]).split("\n").filter(Boolean).map(line => line.split(/\s+/).reverse()))
  const target = refs.get(`refs/tags/${tag}^{}`) || refs.get(`refs/tags/${tag}`)
  assert.ok(!target || target === sha, "Existing release tag targets another commit")
  const existing = json(["release", "list", "--limit", "1000", "--json", "tagName"]).some(release => release.tagName === tag)
  const directory = process.env.RELEASE_OUTPUT_DIR
  fs.copyFileSync(process.env.PROVENANCE_PATH, path.join(directory, "provenance.json"))
  const assets = [archive, "SHA256SUMS", "provenance.json"].map(name => path.join(directory, name))
  if (existing) {
    const release = json(["release", "view", tag, "--json", "isDraft,targetCommitish"])
    assert.equal(release.isDraft, true, "Cannot replace a published release")
    assert.equal(release.targetCommitish, sha, "Cannot replace another commit's draft")
    gh(["release", "upload", tag, ...assets, "--clobber"])
  } else {
    gh(["release", "create", tag, ...assets, "--draft", "--target", sha, "--title", tag, ...releaseNotes])
  }
  output("archive", archive)
} else {
  throw new Error("Expected source or draft operation")
}
