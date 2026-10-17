import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { nextBuildCachePlan } from "../next-build-cache/key.mjs";

const helper = fileURLToPath(
  new URL("../next-build-cache/key.mjs", import.meta.url),
);
const source = "a".repeat(40);
const environment = { platform: "linux", arch: "x64", node: "24.21.0" };

async function put(root, filename, content) {
  const destination = join(root, filename);
  await mkdir(dirname(destination), { recursive: true });
  await writeFile(destination, content);
}

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), "shared-next-cache-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await put(root, "package.json", '{"name":"reader-workspace"}\n');
  await put(
    root,
    "package-lock.json",
    '{"packages":{"next":{"version":"16.4.1"}}}\n',
  );
  for (const app of ["web", "admin", "partner"]) {
    await put(root, `apps/${app}/package.json`, `{"name":"@devfeed/${app}"}\n`);
    await put(
      root,
      `apps/${app}/tsconfig.json`,
      '{"compilerOptions":{"strict":true}}\n',
    );
    await put(
      root,
      `apps/${app}/next.config.ts`,
      'export default { output: "standalone" };\n',
    );
  }
  return {
    root,
    plan: (overrides = {}) =>
      nextBuildCachePlan({
        root,
        appDirectory: "apps/web",
        profile: "production",
        source,
        environment,
        ...overrides,
      }),
  };
}

test("source revisions can reuse compatible compiler work without sharing an immutable entry", async (t) => {
  const { root, plan } = await fixture(t);
  const before = await plan();
  await put(
    root,
    "apps/web/src/app/page.tsx",
    "export default function Page() { return 'Changed'; }",
  );
  const after = await plan({ source: "b".repeat(40) });
  assert.notEqual(before.key, after.key);
  assert.equal(before.restorePrefix, after.restorePrefix);
  assert.ok(before.key.startsWith(before.restorePrefix));
  assert.ok(after.key.startsWith(after.restorePrefix));
  assert.ok(after.key.length <= 512);
});

test("application, build profile, actual Node, OS and architecture never share a fallback", async (t) => {
  const { plan } = await fixture(t);
  const baseline = await plan();
  for (const overrides of [
    { appDirectory: "apps/admin" },
    { appDirectory: "apps/partner" },
    { profile: "dast" },
    { environment: { ...environment, node: "26.0.0" } },
    { environment: { ...environment, arch: "arm64" } },
  ]) {
    const changed = await plan(overrides);
    assert.notEqual(changed.restorePrefix, baseline.restorePrefix);
    assert.equal(changed.key.startsWith(baseline.restorePrefix), false);
  }
});

for (const [name, filename, content] of [
  ["Next dependency upgrade", "package-lock.json", '{"next":"16.4.2"}\n'],
  [
    "workspace build settings",
    "package.json",
    '{"name":"reader-workspace","version":"2"}\n',
  ],
  [
    "root TypeScript configuration",
    "tsconfig.base.json",
    '{"compilerOptions":{"strict":true}}',
  ],
  [
    "application version",
    "apps/web/package.json",
    '{"name":"@devfeed/web","version":"2"}\n',
  ],
  [
    "Next build configuration",
    "apps/web/next.config.ts",
    "export default { reactCompiler: true };",
  ],
  [
    "TypeScript configuration",
    "apps/web/tsconfig.json",
    '{"compilerOptions":{"strict":false}}',
  ],
  [
    "new CSS build configuration",
    "apps/web/postcss.config.mjs",
    "export default { plugins: {} };",
  ],
]) {
  test(`${name} cannot restore the previous compatibility cache`, async (t) => {
    const { root, plan } = await fixture(t);
    const before = await plan();
    await put(root, filename, content);
    const after = await plan();
    assert.notEqual(after.restorePrefix, before.restorePrefix);
    assert.equal(after.key.startsWith(before.restorePrefix), false);
  });
}

test("the cache selection includes compiler files and excludes runtime data and production output", async (t) => {
  const { root, plan } = await fixture(t);
  const compiled = [
    "apps/web/.next/cache/turbopack/cache.bin",
    "apps/web/.next/cache/webpack/client/0.pack",
    "apps/web/.next/cache/swc/plugin.bin",
    "apps/web/.next/cache/.tsbuildinfo",
  ];
  const excluded = [
    "apps/web/.next/cache/fetch-cache/private-response",
    "apps/web/.next/cache/images/avatar",
    "apps/web/.next/server/app/page.js",
    "apps/web/.next/standalone/server.js",
    "apps/web/.next/static/chunk.js",
    "apps/web/.next/faro-sourcemaps/chunk.js.map",
    "apps/web/.next/BUILD_ID",
    "reports/browser/profile",
    ".env",
  ];
  for (const filename of [...compiled, ...excluded])
    await put(root, filename, "Preserve content");
  const selection = await plan();
  const selected = [];
  for (const filename of selection.paths) {
    if (filename.endsWith(".tsbuildinfo")) selected.push(filename);
    else {
      for (const child of await readdir(join(root, filename), {
        recursive: true,
        withFileTypes: true,
      })) {
        if (child.isFile())
          selected.push(relative(root, join(child.parentPath, child.name)));
      }
    }
  }
  assert.deepEqual(selected.sort(), compiled.sort());
  for (const filename of excluded)
    assert.equal(
      await readFile(join(root, filename), "utf8"),
      "Preserve content",
    );
});

test("the external helper reads the caller workspace using the executing Node without creating a fake cache", async (t) => {
  const { root } = await fixture(t);
  const output = join(root, "outputs");
  const result = spawnSync(process.execPath, [helper], {
    encoding: "utf8",
    cwd: tmpdir(),
    env: {
      ...process.env,
      GITHUB_WORKSPACE: root,
      GITHUB_OUTPUT: output,
      GITHUB_SHA: source,
      NEXT_CACHE_APP_DIRECTORY: "apps/web",
      NEXT_CACHE_PROFILE: "production",
    },
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  const outputs = await readFile(output, "utf8");
  assert.ok(outputs.includes(`-node${process.versions.node}-production-`));
  assert.ok(
    outputs.includes(
      "paths<<NEXT_COMPILER_CACHE_PATHS\napps/web/.next/cache/turbopack\n",
    ),
  );
  await assert.rejects(readdir(join(root, "apps/web/.next")), {
    code: "ENOENT",
  });
});

test("invalid application/profile/source inputs fail before producing cache paths", async (t) => {
  const { plan } = await fixture(t);
  for (const overrides of [
    { appDirectory: "../web" },
    { appDirectory: "/apps/web" },
    { appDirectory: "apps/web/../admin" },
    { appDirectory: "apps//web" },
    { appDirectory: "apps/web\nextra=output" },
    { appDirectory: "apps/*" },
    { dependencyPath: "../package-lock.json" },
    { dependencyPath: "/tmp/lock" },
    { configurationPaths: "../secret" },
    { configurationPaths: "next.config.ts\n\npackage.json" },
    { configurationPaths: "next.config.ts " },
    { configurationPaths: Array(34).fill("package.json").join("\n") },
    { configurationPaths: "x".repeat(8193) },
    { environment: { ...environment, platform: "darwin" } },
    { profile: "" },
    { profile: "production\npath=.." },
    { profile: "private/profile" },
    { source: "master" },
    { source: `${source}\nextra=output` },
    { environment: { ...environment, arch: "x64\npath=.." } },
  ])
    await assert.rejects(plan(overrides));
});

test("missing or symlinked compatibility inputs fail instead of selecting a broad cache", async (t) => {
  const { root, plan } = await fixture(t);
  await rm(join(root, "package-lock.json"));
  await assert.rejects(plan(), { code: "ENOENT" });
  await put(root, "outside-lock", "Do not use a different file");
  await symlink(join(root, "outside-lock"), join(root, "package-lock.json"));
  await assert.rejects(plan(), /regular file/);
});

test("a non-regular compatibility input is rejected without blocking on a FIFO", async (t) => {
  const { root } = await fixture(t);
  const lock = join(root, "package-lock.json");
  await rm(lock);
  const fifo = spawnSync("mkfifo", [lock], { encoding: "utf8" });
  assert.equal(fifo.status, 0, fifo.stderr);
  const result = spawnSync(process.execPath, [helper], {
    encoding: "utf8",
    timeout: 5000,
    env: {
      ...process.env,
      GITHUB_WORKSPACE: root,
      GITHUB_OUTPUT: join(root, "outputs"),
      GITHUB_SHA: source,
      NEXT_CACHE_APP_DIRECTORY: "apps/web",
      NEXT_CACHE_PROFILE: "production",
    },
  });
  assert.equal(result.error, undefined, result.error?.message);
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.match(result.stderr, /regular file/);
});

test("the composite consumes only the computed paths and pins its cache action", async () => {
  const action = await readFile(
    new URL("../next-build-cache/action.yml", import.meta.url),
    "utf8",
  );
  assert.match(action, /uses: actions\/cache@[a-f0-9]{40} /);
  assert.ok(action.includes("path: ${{ steps.plan.outputs.paths }}"));
  assert.ok(action.includes("key: ${{ steps.plan.outputs.key }}"));
  assert.ok(
    action.includes("restore-keys: ${{ steps.plan.outputs.restore-prefix }}"),
  );
  assert.doesNotMatch(
    action,
    /continue-on-error|skip-build|mkdir|node_modules|\.next\/standalone/,
  );
  assert.ok(
    action.includes("NEXT_CACHE_ACTION_PATH: ${{ github.action_path }}"),
  );
  assert.ok(action.includes('node "$NEXT_CACHE_ACTION_PATH/key.mjs"'));
});

test("a root application with default Next configuration and a pnpm lockfile is supported", async (t) => {
  const { root, plan } = await fixture(t);
  await put(root, "pnpm-lock.yaml", "lockfileVersion: 9\n");
  const selection = await plan({
    appDirectory: ".",
    dependencyPath: "pnpm-lock.yaml",
  });
  assert.deepEqual(selection.paths, [
    ".next/cache/turbopack",
    ".next/cache/webpack",
    ".next/cache/swc",
    ".next/cache/.tsbuildinfo",
  ]);
  const before = selection.restorePrefix;
  await put(
    root,
    "next.config.mjs",
    "export default { reactCompiler: true };\n",
  );
  assert.notEqual(
    (await plan({ appDirectory: ".", dependencyPath: "pnpm-lock.yaml" }))
      .restorePrefix,
    before,
  );
});

test("arbitrary nested JavaScript app layouts use their own required package and optional configs", async (t) => {
  const { root, plan } = await fixture(t);
  await put(root, "sites/docs/package.json", '{"name":"docs"}\n');
  await put(root, "sites/docs/yarn.lock", "__metadata:\n  version: 8\n");
  const overrides = {
    appDirectory: "sites/docs",
    dependencyPath: "sites/docs/yarn.lock",
  };
  const initial = await plan(overrides);
  assert.equal(initial.paths[0], "sites/docs/.next/cache/turbopack");
  await put(
    root,
    "sites/docs/jsconfig.json",
    '{"compilerOptions":{"checkJs":true}}\n',
  );
  assert.notEqual((await plan(overrides)).restorePrefix, initial.restorePrefix);
  await rm(join(root, "sites/docs/package.json"));
  await assert.rejects(plan(overrides), { code: "ENOENT" });
});

test("lockfile location, explicit configs and app paths separate compatibility even with identical bytes", async (t) => {
  const { root, plan } = await fixture(t);
  const baseline = await plan();
  await put(root, "bun.lock", await readFile(join(root, "package-lock.json")));
  assert.notEqual(
    (await plan({ dependencyPath: "bun.lock" })).restorePrefix,
    baseline.restorePrefix,
  );
  for (const path of ["build/next.shared.mjs", "build/compiler.json"])
    await put(root, path, "first");
  const extras = "build/next.shared.mjs\nbuild/compiler.json\n";
  const extra = await plan({ configurationPaths: extras });
  assert.notEqual(extra.restorePrefix, baseline.restorePrefix);
  assert.equal(
    (
      await plan({
        configurationPaths: "build/compiler.json\nbuild/next.shared.mjs",
      })
    ).key,
    extra.key,
  );
  await put(root, "build/compiler.json", "second");
  assert.notEqual(
    (await plan({ configurationPaths: extras })).restorePrefix,
    extra.restorePrefix,
  );
  await assert.rejects(plan({ configurationPaths: "build/missing.json" }), {
    code: "ENOENT",
  });
});

test("both root and application package manifests are required", async (t) => {
  const { root, plan } = await fixture(t);
  await rm(join(root, "package.json"));
  await assert.rejects(plan(), { code: "ENOENT" });
});

test("lockfile names and bytes have an unambiguous compatibility boundary", async (t) => {
  const { root, plan } = await fixture(t);
  await put(root, "lock", "bc");
  await put(root, "lockb", "c");
  assert.notEqual(
    (await plan({ dependencyPath: "lock" })).restorePrefix,
    (await plan({ dependencyPath: "lockb" })).restorePrefix,
  );
});

test("symlinked parent directories are rejected even when their target is inside the workspace", async (t) => {
  const { root, plan } = await fixture(t);
  await symlink(join(root, "apps/web"), join(root, "linked-app"));
  await assert.rejects(
    plan({ appDirectory: "linked-app" }),
    /regular files and directories/,
  );
  await mkdir(join(root, "build"));
  await symlink(join(root, "build"), join(root, "linked-build"));
  await assert.rejects(
    plan({ configurationPaths: "linked-build/missing.json" }),
    /regular files and directories/,
  );
});

test("non-regular optional config inputs fail closed and all descriptors close after failures", async (t) => {
  const { root, plan } = await fixture(t);
  const before = (await readdir("/proc/self/fd")).length;
  await mkdir(join(root, "apps/web/postcss.config.mjs"));
  for (let attempt = 0; attempt < 20; attempt++)
    await assert.rejects(plan(), /regular file/);
  assert.equal((await readdir("/proc/self/fd")).length, before);
});

test("CLI validates all compatibility inputs before writing outputs", async (t) => {
  const { root } = await fixture(t);
  const output = join(root, "outputs");
  const result = spawnSync(process.execPath, [helper], {
    encoding: "utf8",
    env: {
      ...process.env,
      GITHUB_WORKSPACE: root,
      GITHUB_OUTPUT: output,
      GITHUB_SHA: source,
      NEXT_CACHE_APP_DIRECTORY: "apps/web",
      NEXT_CACHE_PROFILE: "production",
      NEXT_CACHE_CONFIGURATION_PATHS: "missing-required-config.json",
    },
  });
  assert.equal(result.status, 1, result.stderr);
  await assert.rejects(readFile(output), { code: "ENOENT" });
  const missing = spawnSync(process.execPath, [helper], {
    encoding: "utf8",
    env: { ...process.env, GITHUB_WORKSPACE: "", GITHUB_OUTPUT: "" },
  });
  assert.equal(missing.status, 1, missing.stderr);
  assert.match(missing.stderr, /requires GITHUB_OUTPUT and GITHUB_WORKSPACE/);
});
