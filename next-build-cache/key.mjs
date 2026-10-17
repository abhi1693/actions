import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { appendFile, open } from "node:fs/promises";
import { posix, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const runtime = {
  platform: process.platform,
  arch: process.arch,
  node: process.versions.node,
};

function relativePath(value, label, directory = false) {
  if (directory && value === ".") return value;
  if (
    typeof value !== "string" ||
    value.length > 240 ||
    !value
      .split("/")
      .every(
        (part) => /^[a-zA-Z0-9_.-]+$/.test(part) && ![".", ".."].includes(part),
      )
  ) {
    throw new Error(`${label} must be a normalized repository-relative path`);
  }
  return value;
}

// Pin every parent directory while reading through its descriptor. O_NOFOLLOW
// protects the final input too; O_NONBLOCK lets fstat reject FIFOs without waiting.
// /proc/self/fd and O_DIRECTORY make this action explicitly Linux-only.
async function configuration(root, filename, required = false) {
  const directories = [];
  let file;
  try {
    let directory = await open(
      root,
      constants.O_RDONLY | constants.O_DIRECTORY,
    );
    directories.push(directory);
    const parts = filename.split("/");
    for (const part of parts.slice(0, -1)) {
      directory = await open(
        `/proc/self/fd/${directory.fd}/${part}`,
        constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW,
      );
      directories.push(directory);
    }
    file = await open(
      `/proc/self/fd/${directory.fd}/${parts.at(-1)}`,
      constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
    );
    if (!(await file.stat()).isFile()) {
      throw new Error(`Build cache input must be a regular file: ${filename}`);
    }
    return await file.readFile();
  } catch (error) {
    if (!required && error.code === "ENOENT") return null;
    if (["ELOOP", "ENOTDIR"].includes(error.code)) {
      throw new Error(
        `Build cache input must use regular files and directories: ${filename}`,
      );
    }
    throw error;
  } finally {
    if (file) await file.close();
    for (const directory of directories.reverse()) await directory.close();
  }
}

export async function nextBuildCachePlan({
  root,
  appDirectory = ".",
  profile,
  dependencyPath = "package-lock.json",
  configurationPaths = "",
  source,
  environment = runtime,
}) {
  relativePath(appDirectory, "Next cache app-directory", true);
  relativePath(dependencyPath, "Next cache dependency-path");
  if (typeof profile !== "string" || !/^[a-z][a-z0-9-]{0,47}$/.test(profile)) {
    throw new Error(
      "Next cache profile must be a lowercase name of at most 48 characters",
    );
  }
  if (typeof source !== "string" || !/^[a-f0-9]{40}$/.test(source)) {
    throw new Error("Next cache source must be the full GitHub commit SHA");
  }
  for (const field of ["platform", "arch", "node"]) {
    if (!/^[a-zA-Z0-9.-]+$/.test(environment[field] ?? "")) {
      throw new Error(`Invalid Next cache runtime ${field}`);
    }
  }
  if (environment.platform !== "linux")
    throw new Error("Next cache requires a Linux runner");
  if (
    typeof configurationPaths !== "string" ||
    configurationPaths.length > 8192
  ) {
    throw new Error(
      "Next cache configuration-paths must be a bounded newline-separated list",
    );
  }
  const extra =
    configurationPaths === ""
      ? []
      : configurationPaths.replace(/\n$/, "").split("\n");
  if (extra.length > 32)
    throw new Error("Next cache supports at most 32 extra configuration paths");
  for (const filename of extra)
    relativePath(filename, "Next cache configuration-paths entry");

  const lock = await configuration(root, dependencyPath, true);
  const dependencies = createHash("sha256")
    .update(JSON.stringify(dependencyPath))
    .update(lock)
    .digest("hex");
  const config = createHash("sha256");
  config.update(JSON.stringify([appDirectory, [...new Set(extra)].sort()]));
  const files = new Map([
    ["package.json", true],
    ["tsconfig.json", false],
    ["tsconfig.base.json", false],
  ]);
  for (const filename of [
    "package.json",
    "tsconfig.json",
    "jsconfig.json",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "postcss.config.js",
    "postcss.config.cjs",
    "postcss.config.mjs",
    "tailwind.config.js",
    "tailwind.config.cjs",
    "tailwind.config.mjs",
    "tailwind.config.ts",
  ]) {
    const path = posix.join(appDirectory, filename);
    files.set(path, files.get(path) === true || filename === "package.json");
  }
  for (const path of extra) files.set(path, true);
  for (const [filename, required] of [...files].sort(([a], [b]) =>
    a.localeCompare(b, "en"),
  )) {
    const content = await configuration(root, filename, required);
    config.update(
      JSON.stringify([filename, content?.toString("base64") ?? null]),
    );
  }
  const restorePrefix = [
    "shared-next-v1",
    environment.platform,
    environment.arch,
    `node${environment.node}`,
    profile,
    dependencies,
    config.digest("hex"),
    "",
  ].join("-");
  const cache = posix.join(appDirectory, ".next/cache");
  return {
    key: `${restorePrefix}${source}`,
    restorePrefix,
    paths: [
      `${cache}/turbopack`,
      `${cache}/webpack`,
      `${cache}/swc`,
      `${cache}/.tsbuildinfo`,
    ],
  };
}

async function main() {
  const output = process.env.GITHUB_OUTPUT;
  const root = process.env.GITHUB_WORKSPACE;
  if (!output || !root)
    throw new Error("Next cache requires GITHUB_OUTPUT and GITHUB_WORKSPACE");
  const plan = await nextBuildCachePlan({
    root,
    appDirectory: process.env.NEXT_CACHE_APP_DIRECTORY,
    profile: process.env.NEXT_CACHE_PROFILE,
    dependencyPath: process.env.NEXT_CACHE_DEPENDENCY_PATH,
    configurationPaths: process.env.NEXT_CACHE_CONFIGURATION_PATHS,
    source: process.env.GITHUB_SHA,
  });
  await appendFile(
    output,
    `key=${plan.key}\nrestore-prefix=${plan.restorePrefix}\npaths<<NEXT_COMPILER_CACHE_PATHS\n${plan.paths.join("\n")}\nNEXT_COMPILER_CACHE_PATHS\n`,
  );
}

if (
  process.argv[1] &&
  pathToFileURL(resolve(process.argv[1])).href === import.meta.url
) {
  await main();
}
