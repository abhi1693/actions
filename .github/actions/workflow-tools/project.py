"""Small, fixed CI contracts; caller values are data, never shell programs."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

STAGES = ("generate", "lint", "typecheck", "test", "build")
RESERVED = {
    "PATH",
    "HOME",
    "BASH_ENV",
    "ENV",
    "NODE_OPTIONS",
    "PYTHONPATH",
    "PYTHONHOME",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "SHELLOPTS",
    "BASHOPTS",
}


def environment(public, private):
    result = {}
    for raw in (public, private):
        values = json.loads(raw or "{}")
        if not isinstance(values, dict):
            raise ValueError("Project environment must be a JSON object")
        for key, value in values.items():
            if (
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or key.upper().startswith(("GITHUB_", "RUNNER_", "ACTIONS_", "INPUT_"))
                or key.upper() in RESERVED
            ):
                raise ValueError(f"Reserved or invalid environment name: {key}")
            # Unset GitHub vars/secrets become JSON null through toJSON.
            if value is None:
                value = ""
            if not isinstance(value, (str, int, float, bool)) or "\x00" in str(value):
                raise ValueError(f"Environment value must be a scalar: {key}")
            result[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return result


def prepare_secret_masks():
    """Send normalized mask values through a private, exclusive runner IPC file."""
    public = os.getenv("PROJECT_ENV", "{}")
    private = os.getenv("PROJECT_SECRETS", "{}")
    environment(public, private)
    values = environment("{}", private)
    destination = Path(os.environ["PROJECT_MASK_FILE"])
    root = Path(os.environ["RUNNER_TEMP"]).resolve()
    parent = destination.parent
    if (
        not destination.is_absolute()
        or parent.parent != root
        or not parent.name.startswith("project-masks-")
        or destination.name != "values.json"
    ):
        raise ValueError("Invalid private mask transport directory")
    parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(parent_descriptor)
        if info.st_mode & 0o777 != 0o700 or info.st_uid != os.getuid():
            raise ValueError("Invalid private mask transport permissions")
        data = json.dumps([value for value in values.values() if value])
        if len(data.encode()) > 1024 * 1024:
            raise ValueError("Private mask transport is too large")
        descriptor = os.open(
            destination.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(data)
    finally:
        os.close(parent_descriptor)


def export_environment():
    public, private = os.getenv("PROJECT_ENV", "{}"), os.getenv("PROJECT_SECRETS", "{}")
    values = environment(public, private)
    with open(os.environ["GITHUB_ENV"], "a") as target:
        for key, value in values.items():
            delimiter = uuid.uuid4().hex
            target.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def node_stages(checks, scripts):
    checks, scripts = json.loads(checks), json.loads(scripts)
    if not isinstance(checks, list) or not checks or len(checks) != len(set(checks)):
        raise ValueError("checks must be a nonempty list without duplicates")
    if any(stage not in STAGES for stage in checks):
        raise ValueError(f"Only these stages are supported: {STAGES}")
    if not isinstance(scripts, dict) or any(key not in STAGES for key in scripts):
        raise ValueError("scripts must map standard stage names to npm script names")
    mapping = {stage: scripts.get(stage, f"ci:{stage}") for stage in checks}
    if any(
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9_:.-]+", name)
        or name.startswith("-")
        for name in mapping.values()
    ):
        raise ValueError("Invalid npm script name")
    return [(stage, mapping[stage]) for stage in STAGES if stage in mapping]


def node_build_cache(paths, scope, lock_hash, node, revision, public):
    """Restore compiler work only within the same toolchain and build profile."""
    paths = [path.strip() for path in paths.splitlines() if path.strip()]
    scope = scope.strip()
    if not paths and not scope:
        return None
    if not paths or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", scope):
        raise ValueError(
            "Build cache paths and a safe app/profile scope must be set together"
        )
    if not re.fullmatch(r"[a-f0-9]{64}", lock_hash):
        raise ValueError("Build caching requires a matching cache-dependency-path file")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Build caching requires the source commit SHA")
    compatibility = {
        "paths": paths,
        "scope": scope,
        "lock_hash": lock_hash,
        "node": node,
        "project_env": environment(public, "{}"),
    }
    digest = hashlib.sha256(
        json.dumps(compatibility, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    prefix = f"node-build-v1-{node['platform']}-{node['arch']}-{node['version']}-{scope}-{digest}-"
    return {"key": prefix + revision, "restore-prefix": prefix}


def export_build_cache():
    node = json.loads(
        subprocess.check_output(
            [
                "node",
                "-p",
                "JSON.stringify({version:process.version,platform:process.platform,arch:process.arch})",
            ],
            text=True,
        )
    )
    values = node_build_cache(
        os.environ["BUILD_CACHE_PATHS"],
        os.environ["BUILD_CACHE_SCOPE"],
        os.environ["BUILD_CACHE_LOCK_HASH"],
        node,
        os.environ["GITHUB_SHA"],
        os.getenv("PROJECT_ENV", "{}"),
    )
    if values:
        with open(os.environ["GITHUB_OUTPUT"], "a") as target:
            for key, value in values.items():
                target.write(f"{key}={value}\n")


def check_generated(paths):
    paths = json.loads(paths)
    if not isinstance(paths, list) or not paths:
        raise ValueError("generated-paths must be a nonempty JSON array")
    for path in paths:
        if (
            not isinstance(path, str)
            or not path
            or path.startswith(("/", ":"))
            or ".." in Path(path).parts
        ):
            raise ValueError(
                "Generated paths must be relative paths without Git pathspec magic"
            )
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", *paths], check=True)
    extra = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *paths]
    )
    if extra:
        raise ValueError("Generation produced untracked files")


def run_node_stage(stage):
    mapping = dict(node_stages(os.environ["CHECKS"], os.environ["SCRIPTS"]))
    if stage not in mapping:
        return
    package = json.loads(Path("package.json").read_text())
    if mapping[stage] not in package.get("scripts", {}):
        raise ValueError(f"Missing declared npm script: {mapping[stage]}")
    subprocess.run(["npm", "run", mapping[stage]], check=True)
    if stage == "generate" and os.getenv("GENERATED_PATHS", "[]") != "[]":
        check_generated(os.environ["GENERATED_PATHS"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=["environment", "mask-file", "validate-node", "build-cache", *STAGES],
    )
    args = parser.parse_args()
    if args.operation == "environment":
        export_environment()
    elif args.operation == "mask-file":
        prepare_secret_masks()
    elif args.operation == "build-cache":
        export_build_cache()
    elif args.operation == "validate-node":
        mapping = node_stages(os.environ["CHECKS"], os.environ["SCRIPTS"])
        package = json.loads(Path("package.json").read_text())
        for _, script in mapping:
            if script not in package.get("scripts", {}):
                raise ValueError(f"Missing declared npm script: {script}")
    else:
        run_node_stage(args.operation)
