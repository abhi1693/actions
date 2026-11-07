import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import fs from "node:fs"
import path from "node:path"
import { pathToFileURL } from "node:url"

const builder = "https://github.com/abhi1693/actions/.github/workflows/npm-release.yml"
const commit = /^[0-9a-f]{40}$/
const version = /^v(0|[1-9]\d*)(?:\.(0|[1-9]\d*)){0,2}$/

export function preparationReference(workflow) {
  const uses = [...workflow.matchAll(/^[ \t]+uses:[ \t]+["']?abhi1693\/actions\/\.github\/workflows\/npm-release\.yml@([^\s"'#]+)["']?(?:[ \t]+#.*)?[ \t]*$/gm)]
  assert.equal(uses.length, 1, "Tagged preparation workflow must reference exactly one shared builder")
  const reference = uses[0][1]
  assert.ok(commit.test(reference) || version.test(reference), "Use a shared builder commit or released version tag")
  return reference
}

export function verifyProvenance({ archive, bundle, repository, source, reference }) {
  assert.match(repository, /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/)
  assert.match(source, commit, "Expected the exact tagged source commit")
  assert.ok(commit.test(reference) || version.test(reference), "Use a shared builder commit or released version tag")
  const identity = `${builder}@${commit.test(reference) ? reference : `refs/tags/${reference}`}`
  const args = [
    "attestation", "verify", path.resolve(archive),
    "--repo", repository,
    "--bundle", path.resolve(bundle),
    "--cert-identity", identity,
    "--cert-oidc-issuer", "https://token.actions.githubusercontent.com",
    "--source-digest", source,
    "--deny-self-hosted-runners",
    "--format", "json",
    ...(commit.test(reference) ? ["--signer-digest", reference] : []),
  ]
  // Only use the result returned after gh verifies the signature, subject digest,
  // issuer, exact signer identity and source. Predicate fields are caller-authored.
  const verified = JSON.parse(execFileSync("gh", args, {
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  }))
  assert.ok(Array.isArray(verified) && verified.length, "No verified provenance returned")
  const signers = new Set()
  for (const result of verified) {
    const certificate = result.verificationResult?.signature?.certificate
    assert.ok(certificate, "Verified signing certificate is required")
    assert.equal(certificate.subjectAlternativeName, identity)
    assert.equal(certificate.buildSignerURI, identity)
    assert.equal(certificate.issuer, "https://token.actions.githubusercontent.com")
    assert.equal(certificate.sourceRepositoryURI, `https://github.com/${repository}`)
    assert.equal(certificate.sourceRepositoryDigest, source)
    assert.equal(certificate.runnerEnvironment, "github-hosted")
    assert.match(certificate.buildSignerDigest, commit, "Verified builder digest is required")
    if (commit.test(reference)) assert.equal(certificate.buildSignerDigest, reference)
    signers.add(certificate.buildSignerDigest)
  }
  assert.equal(signers.size, 1, "Prepared bundle must identify one immutable builder revision")
  if (!commit.test(reference)) {
    // The first verification authenticated this digest in the certificate. Bind
    // the second verification to that original commit, never today's tag target.
    execFileSync("gh", [...args, "--signer-digest", [...signers][0]], {
      encoding: "utf8", maxBuffer: 16 * 1024 * 1024,
    })
  }
  return { status: "verified", reference, source, signers: [...signers].sort() }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    const [archive, bundle, ...extra] = process.argv.slice(2)
    assert.ok(archive && bundle && !extra.length, "Usage: provenance.mjs <archive> <bundle>")
    const result = verifyProvenance({
      archive, bundle,
      repository: process.env.GH_REPO,
      source: process.env.RELEASE_SOURCE_SHA,
      reference: process.env.PREPARATION_REF,
    })
    console.info(JSON.stringify(result))
    if (process.env.GITHUB_OUTPUT)
      fs.appendFileSync(process.env.GITHUB_OUTPUT, `verified-signers=${JSON.stringify(result.signers)}\n`)
  } catch (error) {
    console.error(error.message)
    process.exitCode = 1
  }
}

