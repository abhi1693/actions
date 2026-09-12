"""Validate image descriptors, select changes, and promote verified image digests."""

import argparse
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path

ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
IMAGE = re.compile(r"ghcr\.io/[a-z0-9][a-z0-9._/-]+")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?"
)
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
PLATFORMS = {"linux/amd64": "ubuntu-24.04", "linux/arm64": "ubuntu-24.04-arm"}


def read_json(path):
    return json.loads(Path(path).read_text())


def output(name, value):
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write(
            f"{name}={json.dumps(value, separators=(',', ':')) if not isinstance(value, str) else value}\n"
        )


def relative(value, root=None):
    if (
        not isinstance(value, str)
        or not value
        or Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise ValueError("Paths must remain inside the repository")
    if root and not (root / value).resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escapes the repository through a symlink")
    return value


def descriptors(data, repository, root=None):
    if not isinstance(data, dict) or set(data) - {
        "schema-version",
        "images",
        "shared-paths",
    }:
        raise ValueError(
            "Manifest must contain schema-version, images and optional shared-paths"
        )
    if (
        data.get("schema-version") != 1
        or not isinstance(data.get("images"), list)
        or not data["images"]
    ):
        raise ValueError("Expected schema-version: 1 and a nonempty images array")
    shared = data.get(
        "shared-paths",
        [
            ".github/**",
            "**/package-lock.json",
            "package-lock.json",
            "**/uv.lock",
            "uv.lock",
        ],
    )
    if not isinstance(shared, list) or any(
        not isinstance(p, str) or not p for p in shared
    ):
        raise ValueError("shared-paths must be an array of glob strings")
    result = []
    allowed = {
        "id",
        "image",
        "context",
        "file",
        "target",
        "platforms",
        "paths",
        "change-dependencies",
        "build-args",
        "smoke-script",
        "labels",
        "extra-tags",
        "secret-id",
    }
    for raw in data["images"]:
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise ValueError("Unknown image descriptor field")
        item = dict(raw)
        if not ID.fullmatch(item.get("id", "")):
            raise ValueError("Invalid component id")
        item.setdefault(
            "image",
            f"ghcr.io/{repository.lower()}"
            + (f"/{item['id']}" if len(data["images"]) > 1 else ""),
        )
        if not IMAGE.fullmatch(item["image"]):
            raise ValueError("Images must be untagged GHCR names")
        for key, default in [("context", "."), ("file", "Dockerfile")]:
            item[key] = relative(item.get(key, default), root)
        item.setdefault("target", "")
        if not isinstance(item["target"], str) or (
            item["target"] and not re.fullmatch(r"[A-Za-z0-9_.-]+", item["target"])
        ):
            raise ValueError("Invalid Docker target")
        item.setdefault("platforms", ["linux/arm64"])
        if (
            not isinstance(item["platforms"], list)
            or not item["platforms"]
            or any(p not in PLATFORMS for p in item["platforms"])
            or len(set(item["platforms"])) != len(item["platforms"])
        ):
            raise ValueError(
                "platforms must contain unique native linux/amd64 or linux/arm64 entries"
            )
        for key, default in [
            ("paths", ["**"]),
            ("change-dependencies", []),
            ("extra-tags", []),
        ]:
            item.setdefault(key, default)
            if not isinstance(item[key], list) or any(
                not isinstance(v, str) or not v for v in item[key]
            ):
                raise ValueError(f"{key} must be a string array")
        if any(not TAG.fullmatch(t) for t in item["extra-tags"]):
            raise ValueError("Invalid extra image tag")
        item.setdefault("secret-id", "env")
        if not isinstance(item["secret-id"], str) or not ID.fullmatch(
            item["secret-id"]
        ):
            raise ValueError("Invalid BuildKit secret id")
        item["paths"] = list(
            dict.fromkeys(
                [
                    *item["paths"],
                    item["file"],
                    ".dockerignore",
                    str(Path(item["context"]) / ".dockerignore"),
                ]
            )
        )
        for key in ("build-args", "labels"):
            item.setdefault(key, {})
            if not isinstance(item[key], dict) or any(
                not isinstance(k, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", k)
                or not isinstance(v, (str, int, float, bool))
                or "\n" in str(v)
                or "\r" in str(v)
                for k, v in item[key].items()
            ):
                raise ValueError(f"{key} must be a map of single-line scalar values")
        item.setdefault("smoke-script", "")
        if item["smoke-script"]:
            relative(item["smoke-script"], root)
        if root:
            if (
                not (root / item["context"]).is_dir()
                or not (root / item["file"]).is_file()
            ):
                raise ValueError("Build context or Dockerfile is missing")
            if item["smoke-script"] and not (root / item["smoke-script"]).is_file():
                raise ValueError("Smoke script is missing")
        result.append(item)
    ids = [x["id"] for x in result]
    if len(set(ids)) != len(ids) or len({x["image"] for x in result}) != len(result):
        raise ValueError("Component ids and image names must be unique within a call")
    for item in result:
        if any(x not in ids or x == item["id"] for x in item["change-dependencies"]):
            raise ValueError("Unknown or self-referencing change dependency")
    return result, shared


def select(items, shared, changed):
    if changed is None or any(
        fnmatch.fnmatchcase(path, pattern) for path in changed for pattern in shared
    ):
        return items
    selected = {
        i["id"]
        for i in items
        if any(fnmatch.fnmatchcase(p, g) for p in changed for g in i["paths"])
    }
    while True:
        dependants = {
            i["id"] for i in items if selected.intersection(i["change-dependencies"])
        }
        if dependants <= selected:
            break
        selected |= dependants
    return [i for i in items if i["id"] in selected]


def version_policy(
    event, ref_type, ref_name, supplied, publish, trusted_branches, event_name, actor
):
    if publish and (
        event_name not in ("push", "release", "workflow_dispatch")
        or actor == "dependabot[bot]"
    ):
        raise ValueError("Publishing requires a trusted push, release or manual event")
    if publish and ref_type == "branch" and ref_name not in trusted_branches:
        raise ValueError("Publishing from this branch is not permitted")
    release = event.get("release", {})
    version = (
        supplied
        or release.get("tag_name", "")
        or (ref_name if ref_type == "tag" else "")
    )
    if version.startswith("v"):
        version = version[1:]
    if version:
        match = SEMVER.fullmatch(version)
        prerelease = match.group(4).split(".") if match and match.group(4) else []
        if not match or any(
            not part or (part.isdigit() and len(part) > 1 and part.startswith("0"))
            for part in prerelease
        ):
            raise ValueError("Release version must be semantic versioning")
        if ref_type == "tag" and ref_name.removeprefix("v") != version:
            raise ValueError("Tag and requested release version disagree")
        # Docker tags cannot contain SemVer build metadata; refuse lossy conversion.
        if "+" in version:
            raise ValueError("Release image versions cannot contain build metadata (+)")
        stable = "-" not in version and not release.get("prerelease", False)
        return version, [version] + (["latest"] if stable else []), "release"
    branch = re.sub(r"[^A-Za-z0-9_.-]", "-", ref_name).strip(".-")[:100]
    return "0.0.0", [branch] if branch else [], "branch"


def plan():
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    source = relative(os.environ["MANIFEST"], workspace)
    data = read_json(workspace / source)
    items, shared = descriptors(data, os.environ["GITHUB_REPOSITORY"], workspace)
    # Any manifest edit invalidates every component, even a manifest stored outside .github.
    shared = [*shared, source]
    event = read_json(os.environ["GITHUB_EVENT_PATH"])
    publish = os.environ["PUBLISH"] == "true"
    branches = json.loads(os.environ["TRUSTED_BRANCHES"])
    if not isinstance(branches, list) or any(not isinstance(x, str) for x in branches):
        raise ValueError("trusted-branches must be a JSON array")
    supplied = os.environ.get("VERSION", "")
    version_file = os.environ.get("VERSION_FILE", "")
    project_version = ""
    if version_file:
        path = workspace / relative(version_file, workspace)
        if path.suffix == ".toml":
            import tomllib

            project_version = tomllib.loads(path.read_text())["project"]["version"]
        else:
            project_version = read_json(path)["version"]
    version, tags, mode = version_policy(
        event,
        os.environ["GITHUB_REF_TYPE"],
        os.environ["GITHUB_REF_NAME"],
        supplied,
        publish,
        branches,
        os.environ["GITHUB_EVENT_NAME"],
        os.environ["GITHUB_ACTOR"],
    )
    if project_version:
        if not isinstance(project_version, str) or not SEMVER.fullmatch(
            project_version
        ):
            raise ValueError("Invalid project version")
        if mode == "release" and version != project_version:
            raise ValueError("Release version does not match the project version")
        if mode != "release":
            version = project_version
    if os.environ.get("LATEST", "true") != "true":
        tags = [tag for tag in tags if tag != "latest"]
    aliases = json.loads(os.environ.get("RELEASE_ALIASES", "[]"))
    if not isinstance(aliases, list) or any(
        alias not in ("major", "minor") for alias in aliases
    ):
        raise ValueError("release-aliases supports only major and minor")
    if (
        mode == "release"
        and "-" not in version
        and not event.get("release", {}).get("prerelease", False)
    ):
        tags.extend(
            ".".join(version.split(".")[: 1 if alias == "major" else 2])
            for alias in aliases
        )
    changed = None
    if mode != "release" and os.environ["CHANGED_ONLY"] == "true":
        base = event.get("pull_request", {}).get("base", {}).get("sha") or event.get(
            "before", ""
        )
        if re.fullmatch(r"[a-f0-9]{40}", base) and base != "0" * 40:
            diff = subprocess.run(
                ["git", "diff", "--no-renames", "--name-only", "-z", base, "HEAD"],
                capture_output=True,
            )
            if diff.returncode == 0:
                changed = [x for x in diff.stdout.decode().split("\0") if x]
    selected = select(items, shared, changed)
    scope = os.environ["ARTIFACT_SCOPE"]
    if not ID.fullmatch(scope):
        raise ValueError("Invalid artifact scope")
    revision, run_id = os.environ["GITHUB_SHA"], os.environ["GITHUB_RUN_ID"]
    candidate = f"candidate-{revision}-{run_id}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    for item in selected:
        item["matrix"] = json.dumps(
            [
                {
                    "runner": os.environ["AMD64_RUNNER"]
                    if p == "linux/amd64"
                    else os.environ["ARM64_RUNNER"],
                    "platform": p,
                }
                for p in item["platforms"]
            ]
        )
        item["cache-scope"] = f"{scope}-{item['id']}"
        item["artifact"] = f"image-result-{scope}-{run_id}-{item['id']}"
        item["build-args"] = (
            "\n".join(f"{k}={v}" for k, v in item["build-args"].items())
            .replace("{version}", version)
            .replace("{revision}", revision)
        )
        labels = {
            **item["labels"],
            "org.opencontainers.image.source": f"https://github.com/{os.environ['GITHUB_REPOSITORY']}",
            "org.opencontainers.image.version": version,
            "org.opencontainers.image.revision": revision,
        }
        item["labels"] = "\n".join(f"{k}={v}" for k, v in labels.items())
        item["final-tags"] = (
            list(
                dict.fromkeys(
                    [
                        *tags,
                        *item["extra-tags"],
                        *([revision] if mode == "branch" else []),
                    ]
                )
            )
            if publish
            else []
        )
        if not all(TAG.fullmatch(t) for t in item["final-tags"]):
            raise ValueError("Invalid final tag")
    manifest = {
        "schema-version": 1,
        "repository": os.environ["GITHUB_REPOSITORY"],
        "revision": revision,
        "run-id": run_id,
        "scope": scope,
        "version": version,
        "mode": mode,
        "images": selected,
    }
    Path(os.environ["RUNNER_TEMP"], f"image-plan-{scope}.json").write_text(
        json.dumps(manifest, indent=2)
    )
    output("matrix", {"include": selected})
    output("selected", "true" if selected else "false")
    output("candidate", candidate)
    output("version", version)


def normalized_platform(platform):
    return (
        platform.removesuffix("/v8")
        if platform.startswith("linux/arm64/")
        else platform
    )


def verify_index(document, expected):
    if not isinstance(document.get("manifests"), list):
        raise ValueError("Expected an OCI image index")
    actual = []
    for entry in document["manifests"]:
        if (
            entry.get("annotations", {}).get("vnd.docker.reference.type")
            == "attestation-manifest"
        ):
            continue
        p = entry.get("platform", {})
        actual.append(
            normalized_platform(
                "/".join(p[k] for k in ("os", "architecture", "variant") if p.get(k))
            )
        )
    if sorted(actual) != sorted(expected):
        raise ValueError(f"Image platform mismatch: {actual} != {expected}")


def record():
    digest = os.environ["IMAGE_DIGEST"]
    if not DIGEST.fullmatch(digest):
        raise ValueError("Invalid image digest")
    data = {
        "image": os.environ["IMAGE_NAME"],
        "digest": digest,
        "revision": os.environ["GITHUB_SHA"],
        "run-id": os.environ["GITHUB_RUN_ID"],
        "platforms": [x["platform"] for x in json.loads(os.environ["BUILD_MATRIX"])],
    }
    Path(os.environ["RECORD_PATH"]).write_text(json.dumps(data, indent=2))


def records_for(plan, directory):
    files = list(Path(directory).rglob("image-result.json"))
    records = [read_json(path) for path in files]
    by_image = {}
    for record in records:
        image = record.get("image")
        if image in by_image:
            raise ValueError("Duplicate image result")
        if (
            record.get("revision") != plan["revision"]
            or record.get("run-id") != plan["run-id"]
            or not DIGEST.fullmatch(record.get("digest", ""))
        ):
            raise ValueError("Stale or invalid image result")
        by_image[image] = record
    if set(by_image) != {x["image"] for x in plan["images"]}:
        raise ValueError("Image results do not match the selected component set")
    for item in plan["images"]:
        if sorted(by_image[item["image"]].get("platforms", [])) != sorted(
            item["platforms"]
        ):
            raise ValueError("Recorded platforms disagree with plan")
    return by_image


def docker_json(*args):
    return json.loads(
        subprocess.check_output(["docker", "buildx", "imagetools", *args], text=True)
    )


def existing_digest(image, tag):
    result = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            f"{image}:{tag}",
            "--format",
            "{{json .Manifest}}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        digest = json.loads(result.stdout)["digest"]
        if not DIGEST.fullmatch(digest):
            raise ValueError("Registry returned an invalid digest")
        return digest
    # Authentication, transport and registry errors must not be mistaken for absent tags.
    if not re.search(
        r"manifest unknown|MANIFEST_UNKNOWN|" + re.escape(f"{image}:{tag}: not found"),
        result.stderr,
    ):
        raise RuntimeError("Could not establish existing tag state: " + result.stderr)
    return None


def promote(plan, directory, destination):
    records = records_for(plan, directory)
    # Validate every image and every immutable tag before making the first tag write.
    for item in plan["images"]:
        digest = records[item["image"]]["digest"]
        verify_index(
            docker_json("inspect", f"{item['image']}@{digest}", "--raw"),
            item["platforms"],
        )
        if plan["mode"] == "release":
            for tag in item["final-tags"]:
                if not SEMVER.fullmatch(tag.removeprefix("v")):
                    continue
                previous = existing_digest(item["image"], tag)
                if previous and previous != digest:
                    raise ValueError(
                        f"Refusing to overwrite immutable tag {item['image']}:{tag}"
                    )
    published = {}
    for item in plan["images"]:
        digest = records[item["image"]]["digest"]
        args = ["docker", "buildx", "imagetools", "create"]
        for tag in item["final-tags"]:
            args.extend(["--tag", f"{item['image']}:{tag}"])
        subprocess.run([*args, f"{item['image']}@{digest}"], check=True)
        for tag in item["final-tags"]:
            if existing_digest(item["image"], tag) != digest:
                raise ValueError("Promotion changed the verified digest")
        published[item["id"]] = f"{item['image']}@{digest}"
    result = {
        k: plan[k]
        for k in (
            "schema-version",
            "repository",
            "revision",
            "run-id",
            "version",
            "scope",
        )
    }
    result["images"] = published
    Path(destination).write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["plan", "record", "promote"])
    args = parser.parse_args()
    if args.operation == "plan":
        plan()
    elif args.operation == "record":
        record()
    else:
        promote(
            read_json(os.environ["PLAN_PATH"]),
            os.environ["RESULTS_DIR"],
            os.environ["MANIFEST_PATH"],
        )
