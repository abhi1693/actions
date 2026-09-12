"""Dependency audits and a fail-closed SARIF findings gate."""

import json
import os
import re
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


def trusted_workflow_finding(result, trusted, workspace):
    """Allow only the declared major refs at the exact reported source location."""
    if result.get("ruleId") != "actions/unpinned-tag" or not trusted:
        return False
    locations = result.get("locations", [])
    if len(locations) != 1:
        return False
    location = locations[0].get("physicalLocation", {})
    uri = location.get("artifactLocation", {}).get("uri", "")
    region = location.get("region", {})
    line = region.get("startLine")
    if not isinstance(line, int) or line < 1 or region.get("endLine", line) != line:
        return False
    directory = (workspace / ".github/workflows").resolve()
    source = (workspace / uri).resolve()
    if not source.is_relative_to(directory) or source.suffix not in (".yml", ".yaml"):
        return False
    try:
        text = source.read_text().splitlines()[line - 1]
    except (OSError, IndexError):
        return False
    match = re.fullmatch(r"\s*uses:\s*([^\s#]+)\s*(?:#.*)?", text)
    return bool(match and match[1].strip("\"'") in trusted)


def sarif_gate(directory, trusted_workflows="[]", workspace=None):
    trusted = json.loads(trusted_workflows)
    if not isinstance(trusted, list) or any(
        not isinstance(ref, str)
        or not re.fullmatch(
            r"[\w.-]+/[\w.-]+/\.github/workflows/[\w.-]+\.ya?ml@v[1-9][0-9]*", ref
        )
        for ref in trusted
    ):
        raise ValueError("Trusted workflows must be exact reusable workflow major refs")
    workspace = Path(workspace or Path.cwd())
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
            # Keep accepted warnings in SARIF; the exception affects only this gate.
            for result in results:
                if trusted_workflow_finding(result, trusted, workspace):
                    print(
                        "Accepted unpinned-tag warning for an explicitly trusted workflow"
                    )
                else:
                    findings += 1
    if findings:
        raise ValueError(f"CodeQL reported {findings} finding(s)")


def secret_log_options(scope, event_name, event):
    if scope not in ("history", "event"):
        raise ValueError("Secret scan scope must be history or event")
    if scope == "history" or event_name not in ("push", "pull_request"):
        return []
    if event_name == "pull_request":
        before, after = (
            event["pull_request"]["base"]["sha"],
            event["pull_request"]["head"]["sha"],
        )
    else:
        before, after = event["before"], event["after"]
    for sha in (before, after):
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Invalid event commit SHA")
    if before == "0" * 40:
        return []
    return ["--log-opts", f"{before}..{after}"]


def secret_scan():
    report = Path(os.environ["REPORTS"]) / "gitleaks.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "go",
        "run",
        "github.com/zricethezav/gitleaks/v8@v8.30.1",
        "git",
        "--redact",
        "--no-banner",
        "--report-format",
        "json",
        "--report-path",
        str(report),
    ]
    if os.environ.get("CONFIG"):
        command.extend(["--config", os.environ["CONFIG"]])
    command.extend(
        secret_log_options(
            os.environ["SCAN_SCOPE"],
            os.environ["GITHUB_EVENT_NAME"],
            json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()),
        )
    )
    subprocess.run([*command, "."], check=True)


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
        "secrets": secret_scan,
        "npm": npm_audit,
        "python": python_audit,
        "sarif": lambda: sarif_gate(
            os.environ["SARIF_DIRECTORY"], os.environ.get("TRUSTED_WORKFLOWS", "[]")
        ),
    }[sys.argv[1]]()
