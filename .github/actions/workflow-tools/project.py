"""Small, fixed CI contracts; caller values are data, never shell programs."""

import argparse
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


def export_environment():
    public, private = os.getenv("PROJECT_ENV", "{}"), os.getenv("PROJECT_SECRETS", "{}")
    values = environment(public, private)
    secrets = json.loads(private or "{}")
    for value in secrets.values():
        if value is None or value == "":
            continue
        # GitHub command escaping, including multiline values.
        masked = (
            str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        )
        print(f"::add-mask::{masked}")
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
    parser.add_argument("operation", choices=["environment", "validate-node", *STAGES])
    args = parser.parse_args()
    if args.operation == "environment":
        export_environment()
    elif args.operation == "validate-node":
        mapping = node_stages(os.environ["CHECKS"], os.environ["SCRIPTS"])
        package = json.loads(Path("package.json").read_text())
        for _, script in mapping:
            if script not in package.get("scripts", {}):
                raise ValueError(f"Missing declared npm script: {script}")
    else:
        run_node_stage(args.operation)
