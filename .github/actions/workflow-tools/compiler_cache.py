"""Persist only Next.js compiler entries from an explicit BuildKit cache mount."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from images import compiler_cache_settings, relative

HELPER_IMAGE = "alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b"
DIRECTORIES = ("turbopack", "webpack", "swc")
ENTRIES = (*DIRECTORIES, ".tsbuildinfo")
MAX_FILES = 100_000
MAX_BYTES = 2 * 1024**3
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def read_input(root, name):
    """Read regular compatibility files through pinned directory descriptors."""
    descriptors = [os.open(root, READ_FLAGS | os.O_DIRECTORY)]
    try:
        for part in Path(name).parts[:-1]:
            descriptors.append(
                os.open(part, READ_FLAGS | os.O_DIRECTORY, dir_fd=descriptors[-1])
            )
        descriptor = os.open(Path(name).name, READ_FLAGS, dir_fd=descriptors[-1])
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024**2:
            raise ValueError("Compatibility input must be a bounded regular file")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            value = stream.read(32 * 1024**2 + 1)
        if len(value) > 32 * 1024**2:
            raise ValueError("Compatibility input is too large")
        return value
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def settings(environment):
    target = environment.get("COMPILER_CACHE_TARGET", "")
    files = json.loads(environment.get("COMPILER_CACHE_KEY_FILES", "[]"))
    compiler_cache_settings(target, files)
    if not target:
        raise ValueError("Compiler caching was not enabled")
    platform = environment["PLATFORM"]
    revision = environment["GITHUB_SHA"]
    if platform not in ("linux/amd64", "linux/arm64") or not re.fullmatch(
        r"[a-f0-9]{40}", revision
    ):
        raise ValueError("Expected a native platform and exact source revision")
    root = Path(environment["GITHUB_WORKSPACE"])
    dockerfile = environment["DOCKERFILE"]
    compiler_cache_settings(target, [dockerfile])
    inputs = {}
    for name in sorted(set([dockerfile, *files])):
        inputs[name] = hashlib.sha256(read_input(root, name)).hexdigest()
    context = environment.get("BUILD_CONTEXT", ".")
    relative(context, root)
    if str(Path(context)) != context or not (root / context).is_dir():
        raise ValueError("Compiler caching requires a normalized local build context")
    # Docker ignore rules can change the effective compiler inputs.
    for name in dict.fromkeys(
        [".dockerignore", f"{context}/.dockerignore", f"{dockerfile}.dockerignore"]
    ):
        name = str(Path(name))
        try:
            inputs[name] = hashlib.sha256(read_input(root, name)).hexdigest()
        except FileNotFoundError:
            inputs[name] = None
    compatibility = {
        "version": 1,
        "helper": HELPER_IMAGE,
        "entries": ENTRIES,
        "image": environment["IMAGE"],
        "scope": environment["CACHE_SCOPE"],
        "platform": platform,
        "mount": target,
        "context": context,
        "build_target": environment.get("BUILD_TARGET", ""),
        "build_args": environment.get("BUILD_ARGS", ""),
        "inputs": inputs,
    }
    digest = hashlib.sha256(
        json.dumps(compatibility, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    prefix = f"docker-next-v1-{platform.replace('/', '-')}-{digest}-"
    return {
        "target": target,
        "platform": platform,
        "key": prefix + revision,
        "restore-prefix": prefix,
    }


def outputs(values):
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        for name, value in values.items():
            if "\n" in str(value):
                stream.write(
                    f"{name}<<COMPILER_CACHE_OUTPUT\n{value}\nCOMPILER_CACHE_OUTPUT\n"
                )
            else:
                stream.write(f"{name}={value}\n")


def plan():
    value = settings(os.environ)
    directory = Path(os.environ["RUNNER_TEMP"]) / "docker-compiler-cache"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Cache staging directory cannot be a symlink")
    payload = directory / "payload"
    value["payload"] = str(payload)
    destination = directory / "plan.json"
    destination.write_text(json.dumps(value))
    outputs(
        {
            **{k: value[k] for k in ("key", "restore-prefix")},
            "plan": str(destination),
            "paths": "\n".join(str(payload / e) for e in ENTRIES),
        }
    )


def filtered_copy(source, destination):
    """Copy only compiler entries, rejecting symlinks/special files and bounding size."""
    destination.mkdir(parents=True, exist_ok=True)
    if not source.exists() and not source.is_symlink():
        return {"files": 0, "bytes": 0}
    counts = {"files": 0, "bytes": 0}
    entries_seen = 0

    def copy_entry(parent, name, output, directory):
        nonlocal entries_seen
        entries_seen += 1
        if entries_seen > MAX_FILES:
            raise ValueError("Compiler payload exceeds its entry bound")
        descriptor = os.open(
            name, READ_FLAGS | (os.O_DIRECTORY if directory else 0), dir_fd=parent
        )
        try:
            info = os.fstat(descriptor)
            if directory:
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("Expected compiler directory")
                output.mkdir(exist_ok=True)
                for child in sorted(os.listdir(descriptor)):
                    child_info = os.stat(
                        child, dir_fd=descriptor, follow_symlinks=False
                    )
                    if not (
                        stat.S_ISDIR(child_info.st_mode)
                        or stat.S_ISREG(child_info.st_mode)
                    ):
                        raise ValueError(
                            "Compiler payload contains a symlink or special file"
                        )
                    copy_entry(
                        descriptor,
                        child,
                        output / child,
                        stat.S_ISDIR(child_info.st_mode),
                    )
            else:
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Expected regular compiler file")
                counts["files"] += 1
                if (
                    counts["files"] > MAX_FILES
                    or counts["bytes"] + info.st_size > MAX_BYTES
                ):
                    raise ValueError("Compiler payload exceeds its file/size bounds")
                with (
                    os.fdopen(os.dup(descriptor), "rb") as stream,
                    output.open("wb") as target,
                ):
                    while chunk := stream.read(1024 * 1024):
                        counts["bytes"] += len(chunk)
                        if counts["bytes"] > MAX_BYTES:
                            raise ValueError("Compiler payload exceeds its size bound")
                        target.write(chunk)
                os.utime(output, ns=(info.st_atime_ns, info.st_mtime_ns))
        finally:
            os.close(descriptor)

    descriptor = os.open(source, READ_FLAGS | os.O_DIRECTORY)
    try:
        for entry in ENTRIES:
            try:
                os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            copy_entry(descriptor, entry, destination / entry, entry in DIRECTORIES)
    finally:
        os.close(descriptor)
    return counts


def transfer(value, builder, extract):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", builder):
        raise ValueError("Expected an explicit Buildx builder name")
    compiler_cache_settings(value["target"], ["compatibility-file"])
    if value["platform"] not in ("linux/amd64", "linux/arm64"):
        raise ValueError("Unsupported native platform")
    payload = Path(value["payload"])
    with tempfile.TemporaryDirectory(
        prefix="compiler-transfer-", dir=payload.parent
    ) as temporary:
        context = Path(temporary)
        seed = context / "payload"
        counts = (
            filtered_copy(payload, seed) if not extract else {"files": 0, "bytes": 0}
        )
        if extract:
            script = "set -eu; mkdir -p /out; "
            for entry in ENTRIES:
                expected = "d" if entry in DIRECTORIES else "f"
                script += (
                    f"if [ -e /cache/{entry} ] || [ -L /cache/{entry} ]; then "
                    f"test -{expected} /cache/{entry}; test ! -L /cache/{entry}; "
                    f'test -z "$(find /cache/{entry} ! -type d ! -type f)"; '
                    f"cp -a /cache/{entry} /out/; fi; "
                )
            body = ""
        else:
            body = "COPY payload/ /seed/\n"
            script = "set -eu; find /cache -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; cp -a /seed/. /cache/"
        dockerfile = context / "Dockerfile"
        dockerfile.write_text(
            f"FROM {HELPER_IMAGE} AS transfer\n{body}"
            "ARG COMPILER_TRANSFER_NONCE\n"
            f"RUN --network=none --mount=type=cache,id={value['target']},target=/cache,sharing=locked "
            f'set -eu; test -n "$COMPILER_TRANSFER_NONCE"; {script}\n'
            + ("FROM scratch\nCOPY --from=transfer /out/ /\n" if extract else "")
        )
        output = context / "export"
        subprocess.run(
            [
                "docker",
                "buildx",
                "build",
                "--builder",
                builder,
                "--platform",
                value["platform"],
                "--file",
                str(dockerfile),
                "--build-arg",
                f"COMPILER_TRANSFER_NONCE={uuid.uuid4().hex}",
                "--network=none",
                "--provenance=false",
                "--output",
                f"type=local,dest={output}" if extract else "type=cacheonly",
                str(context),
            ],
            check=True,
        )
        if extract:
            checked = context / "checked"
            counts = filtered_copy(output, checked)
            if payload.is_symlink():
                raise ValueError("Cache payload directory cannot be a symlink")
            if payload.exists():
                shutil.rmtree(payload)
            checked.rename(payload)
        print(
            f"Compiler cache {'extracted' if extract else 'injected'}: {counts['files']} files, {counts['bytes']} bytes"
        )
        return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "inject", "extract"))
    command = parser.parse_args().command
    if command == "plan":
        plan()
    else:
        value = json.loads(Path(os.environ["COMPILER_CACHE_PLAN"]).read_text())
        counts = transfer(value, os.environ["BUILDER"], command == "extract")
        if command == "extract":
            outputs(
                {
                    "files": str(counts["files"]),
                    "bytes": str(counts["bytes"]),
                    "nonempty": "true" if counts["files"] else "false",
                }
            )


if __name__ == "__main__":
    main()
