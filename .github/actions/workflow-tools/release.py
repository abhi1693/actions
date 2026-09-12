"""Publish a tested shared-workflow version and advance its major ref using a lease."""

import json
import os
import re
import subprocess


def run(*args):
    return subprocess.run(
        args, check=True, text=True, capture_output=True
    ).stdout.strip()


def release_version(value):
    value = value.removeprefix("v")
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise ValueError("Shared workflow releases require a stable semantic version")
    return f"v{value}", "v" + value.split(".")[0]


def publish(version):
    tag, major = release_version(version)
    repository, sha = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
    if os.environ["GITHUB_REF"] != "refs/heads/master":
        raise ValueError("Release shared workflows from master only")
    if run("git", "rev-parse", "HEAD") != sha:
        raise ValueError("Checkout differs from the validated commit")
    previous = run("git", "ls-remote", "--tags", "origin", f"refs/tags/{major}").split()
    lease = previous[0] if previous else ""
    releases = json.loads(
        run(
            "gh",
            "release",
            "list",
            "--repo",
            repository,
            "--limit",
            "100",
            "--json",
            "tagName,isDraft",
        )
    )
    requested = tuple(int(x) for x in tag[1:].split("."))
    for release in releases:
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", release["tagName"])
        if not release["isDraft"] and match:
            published = tuple(int(x) for x in match.groups())
            if published[0] == requested[0] and published > requested:
                raise ValueError("Refusing to move the major ref backwards")
    existing = run(
        "git",
        "ls-remote",
        "--tags",
        "origin",
        f"refs/tags/{tag}",
        f"refs/tags/{tag}^{{}}",
    ).splitlines()
    if existing:
        targets = {line.split()[1]: line.split()[0] for line in existing}
        target = targets.get(f"refs/tags/{tag}^{{}}", targets.get(f"refs/tags/{tag}"))
        if target != sha:
            raise ValueError("The immutable release tag already targets another commit")
    else:
        run("git", "push", "origin", f"{sha}:refs/tags/{tag}")
    if not any(release["tagName"] == tag for release in releases):
        run(
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            repository,
            "--verify-tag",
            "--generate-notes",
            "--title",
            tag,
        )
    # Update only this major tag and only if nobody changed it since the initial read.
    run(
        "git",
        "push",
        f"--force-with-lease=refs/tags/{major}:{lease}",
        "origin",
        f"{sha}:refs/tags/{major}",
    )
    if (
        run("git", "ls-remote", "--tags", "origin", f"refs/tags/{major}").split()[0]
        != sha
    ):
        raise ValueError("Remote major ref did not converge")
    print(f"Published {tag}; {major} now resolves to {sha}")


if __name__ == "__main__":
    publish(os.environ["RELEASE_VERSION"])
