"""Dependency audits and a fail-closed SARIF findings gate."""

import json
import os
import subprocess
import sys
from pathlib import Path


def audit(command, destination):
    result = subprocess.run(command, capture_output=True, text=True)
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_text(result.stdout)
    # Do not turn registry/network/tool errors into clean audits.
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    result.check_returncode()


def sarif_gate(directory):
    reports = list(Path(directory).rglob("*.sarif"))
    if not reports:
        raise ValueError("CodeQL did not produce SARIF reports")
    findings = 0
    for report in reports:
        data = json.loads(report.read_text())
        runs = data.get("runs")
        if data.get("version") != "2.1.0" or not isinstance(runs, list) or not runs:
            raise ValueError("Invalid SARIF report")
        for run in runs:
            for invocation in run.get("invocations", []):
                if invocation.get("executionSuccessful") is False:
                    raise ValueError("SARIF records a failed analysis")
            results = run["results"]
            if not isinstance(results, list):
                raise ValueError("Invalid SARIF results")
            # Existing/baselined findings still count. Exceptions belong in scanner config.
            findings += len(results)
    if findings:
        raise ValueError(f"CodeQL reported {findings} finding(s)")


def npm_audit():
    severity = os.environ["AUDIT_LEVEL"]
    if severity not in ("low", "moderate", "high", "critical"):
        raise ValueError("Invalid npm audit threshold")
    command = ["npm", "audit", "--json", f"--audit-level={severity}"]
    if os.environ["INCLUDE_DEV"] != "true":
        command.append("--omit=dev")
    audit(command, os.environ["AUDIT_REPORT"])


def python_audit():
    requirements = str(
        Path(os.environ["RUNNER_TEMP"]) / "shared-audit-requirements.txt"
    )
    command = ["uv", "export", "--locked", "--no-hashes", "--output-file", requirements]
    if os.environ["ALL_PACKAGES"] == "true":
        command.extend(["--all-packages", "--no-emit-workspace"])
    else:
        command.append("--no-emit-project")
    if os.environ["INCLUDE_DEV"] != "true":
        command.append("--no-dev")
    extras = json.loads(os.environ["EXTRAS"])
    if not isinstance(extras, list) or any(
        not isinstance(x, str) or not x or x.startswith("-") for x in extras
    ):
        raise ValueError("Python extras must be a JSON array of extra names")
    for extra in extras:
        command.extend(["--extra", extra])
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    audit(
        [
            "uvx",
            "pip-audit==2.10.1",
            "-r",
            requirements,
            "--disable-pip",
            "--no-deps",
            "--format",
            "json",
            "--progress-spinner",
            "off",
        ],
        os.environ["AUDIT_REPORT"],
    )


if __name__ == "__main__":
    {
        "npm": npm_audit,
        "python": python_audit,
        "sarif": lambda: sarif_gate(os.environ["SARIF_DIRECTORY"]),
    }[sys.argv[1]]()
