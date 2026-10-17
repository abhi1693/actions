# Persist Next.js compiler mounts between image jobs

Docker layer caches do not persist BuildKit cache-mount contents across fresh
builders. Opt in per image to restore a bounded compiler payload, inject it into
the job's explicit builder, run the normal image build and security checks, then
extract and save the resulting compiler data. Existing GHA/registry layer caches,
native-platform selection, scans, smoke tests and publication gates remain intact.

```json
{
  "id": "web",
  "file": "apps/web/Dockerfile",
  "compiler-cache-target": "/app/apps/web/.next/cache",
  "compiler-cache-key-files": [
    "package-lock.json",
    "package.json",
    "apps/web/package.json",
    "apps/web/next.config.ts",
    "apps/web/tsconfig.json"
  ]
}
```

Mount that exact target on the real Next.js build instruction:

```dockerfile
RUN --mount=type=cache,target=/app/apps/web/.next/cache,sharing=locked \
    npm run web:build
```

Use the implicit cache ID shown here. Custom IDs/namespaces are not supported by
this interface. The two optional manifest fields default to disabled. Direct
`docker-build-push.yml` callers use the same input names, with the key-file list
encoded as a JSON string. Old callers need no changes.

List every dependency and public configuration file that can affect compilation,
including root/app manifests, the lockfile, shared workspace manifests and relevant
Next.js, TypeScript or CSS configuration. Files must be exact, regular repository
paths (1–32 entries); symlink ancestors, symlink files, FIFOs and traversal are
rejected. The Dockerfile and root/context/Dockerfile-specific ignore files are
fingerprinted automatically. Compatibility also includes the image, scope, native
platform, build context/stage, all build arguments, mount target, helper image and
filter version. Source revision is the primary-key suffix; fallback removes only
that suffix. Host Node caches and native Alpine image caches use separate namespaces.
Do not put secrets in build arguments or compatibility files.

Only `turbopack`, `webpack`, `swc` directories and the regular `.tsbuildinfo` file
are restored/exported. Mounting their common parent preserves `.tsbuildinfo` as a
file. Other entries, including `fetch-cache` and `images`, never enter the persisted
payload. Symlinks/special files inside allowed entries fail closed. Payload copying
is bounded to 100,000 entries and 2 GiB. Missing unused compiler formats are normal.

The shared Python helper uses a digest-pinned Alpine image and the same recorded
Buildx builder for injection, the main build and extraction. Transfer RUN steps
disable networking and consume a unique nonce to avoid stale transfer layers.
They deliberately do **not** use `--no-cache`: BuildKit can clear matching cache
mounts under that option ([upstream explanation](https://github.com/moby/buildkit/issues/5305)).
No third-party cache-dance action or floating utility image is used.

The GitHub cache is saved explicitly after successful extraction and preceding
image checks. A main-build layer hit can legitimately leave a fresh mount empty;
empty payloads are reported and not saved. File/byte counts describe actual data,
not proof that compilation reused it. Caches are disposable and can be evicted;
a cold miss never skips the build or weakens a gate.

Run `uv run --with PyYAML==6.0.3 python -m unittest discover -s tests -v` for the
required contracts. They cover namespace/key boundaries, unsafe inputs, whitelist
filtering, nonblocking file checks, fresh non-pruning transfers and strict workflow
ordering. The container fixture also opts in so hosted validation executes the
complete optional path. To prove real Next.js reuse, export a cold build's compiler
files, create a second fresh builder, restore/inject them, change source, and verify
the actual Next RUN and changed runtime output. A Docker layer hit or mocked transfer
alone is insufficient; report local/native architecture and hosted evidence separately.
