# Next.js compiler cache

Restore Next.js compiler work before a fresh build. This composite action works with
root applications and monorepos on Linux runners with Node.js 22.13 or newer. Set up
Node and check out the caller repository first:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
- uses: actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0
  with:
    node-version: "24"
- uses: abhi1693/actions/next-build-cache@<reviewed-commit-sha>
  with:
    app-directory: apps/web
    profile: production
- run: npm ci
- run: npm run build --workspace apps/web
```

For a root application, omit `app-directory` and run the repository's normal build.
Pin the action to a reviewed immutable commit; this new action is not available in
older shared-workflow releases. A new shared namespace intentionally starts with a
cold cache when migrating from the original DevFeed-local action.

## Inputs and outputs

| Input                 | Default             | Behavior                                                                                                                                                               |
| :-------------------- | :------------------ | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app-directory`       | `.`                 | Next.js application directory, relative to the caller repository root.                                                                                                 |
| `profile`             | Required            | Stable lowercase build environment name, such as `production`, `dast`, or `browser-budgets`. Up to 48 characters; letters, digits and hyphens, starting with a letter. |
| `dependency-path`     | `package-lock.json` | Required lockfile relative to the caller repository root. Use an explicit path for pnpm, Yarn, Bun, or a nested lockfile.                                              |
| `configuration-paths` | Empty               | Extra build configuration files, one repository-relative path per line. Every listed file must exist; order and duplicate entries do not change the key.               |

Paths use `/` separators and letters, digits, dots, underscores and hyphens, up to
240 characters. Absolute paths, traversal, globs, whitespace, empty path segments
and symlinks are rejected. `configuration-paths` accepts at most 32 entries and
8192 characters. Missing lockfiles or package manifests fail before restoring a
cache. The helper pins directory and file descriptors while reading compatibility
inputs, rejects non-regular files without blocking on FIFOs, and does not log file
contents. Its directory-descriptor handling uses Linux `/proc/self/fd`.

The `cache-hit` output has the pinned `actions/cache` meaning: `true` for an exact
key match, `false` for a compatible fallback, and empty when no cache was restored.
The `key` output contains the computed immutable cache key. Neither output should
be used to skip builds or tests.

For example, a nested pnpm application can include an imported root build config:

```yaml
- uses: abhi1693/actions/next-build-cache@<reviewed-commit-sha>
  with:
    app-directory: sites/docs
    profile: production
    dependency-path: pnpm-lock.yaml
    configuration-paths: |
      pnpm-workspace.yaml
      build/next.shared.mjs
```

## Compatibility and cached paths

The key separates the actual executing Node version, platform, architecture,
application path, profile, lockfile path and bytes, and build configuration. Root
and application `package.json` files are required. Root TypeScript configs and
application TypeScript, JavaScript, Next, PostCSS and Tailwind config variants
are hashed when present, including their absence. A Next config file is optional:
the framework's defaults remain a valid build. Add imported configs, workspace
configuration and other build inputs through `configuration-paths`.

Use different profiles for different public build environment settings, and bump
the profile when those settings change. Environment values and `.env` files are
not read automatically or exported. Next.js still validates its own compiler cache; the action
does not replace compiler invalidation or discover every transitive build input.

The full source commit SHA makes each revision a new immutable entry. A
source-only change can restore an earlier entry with the same complete
compatibility prefix. There is no broader runtime, dependency or profile fallback.
The helper runs from `github.action_path`; all inputs and cache paths belong to
`GITHUB_WORKSPACE`, regardless of the shell's current directory.

Only these paths under the chosen application's default `.next/cache` are cached:

- `turbopack`
- `webpack`
- `swc`
- `.tsbuildinfo`

Custom Next.js `distDir` values are not supported. Runtime fetch/image caches,
complete build output, standalone servers, source maps, reports and browser
profiles are outside the cache selection. Keep secrets out of compiler output;
GitHub caches are not a secret store. Builds inside test commands need the action
before that command too. The action creates no empty cache directories, skips no
build steps, and a first cold build can still warn that no build cache was found.

The pinned cache action saves compiler data only after a successful job. Normal
GitHub branch and pull-request cache restrictions apply. Docker BuildKit caches
remain separate; this action does not populate Docker cache mounts.

## Validation

Run `node --test --experimental-test-coverage --test-coverage-include=next-build-cache/key.mjs tests/next-build-cache.test.mjs`.
Required shared-workflow validation runs these tests, uploads native LCOV coverage,
and includes JavaScript CodeQL analysis without a new npm dependency.

For a real build proof, build cold, preserve only the listed compiler paths,
remove the remaining production output, restore those compiler paths, and build
again. Check both builds and runtime behavior. A warm compiler cache must not
substitute for a fresh production build. Compare build durations separately from
browser performance measurements.

See [Next.js CI caching](https://nextjs.org/docs/app/guides/ci-build-caching),
[GitHub action and workspace contexts](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#github-context),
and [GitHub cache restrictions](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache).
