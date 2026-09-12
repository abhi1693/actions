"""Exercise real local npm/uv and Docker operations using disposable resources.

No GitHub or production registry writes. A loopback-only registry is removed on exit.
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".github/actions/workflow-tools"
spec = importlib.util.spec_from_file_location("images", TOOLS / "images.py")
images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images)


def run(*command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def capture(*command):
    return subprocess.check_output(command, text=True).strip()


def main():
    node = ROOT / "tests/fixtures/node"
    run("npm", "ci", "--ignore-scripts", cwd=node)
    env = dict(
        os.environ,
        CI_FIXTURE="node",
        CHECKS='["lint","typecheck","test","build"]',
        SCRIPTS="{}",
    )
    for stage in ("validate-node", "lint", "typecheck", "test", "build"):
        run("python3", str(TOOLS / "project.py"), stage, cwd=node, env=env)
    run("uv", "sync", "--locked", "--extra", "dev", cwd=ROOT / "tests/fixtures/python")
    run(
        "uv",
        "run",
        "python",
        "check.py",
        cwd=ROOT / "tests/fixtures/python",
        env=dict(os.environ, CI_FIXTURE="python"),
    )
    suffix = uuid.uuid4().hex[:12]
    registry = f"actions-smoke-registry-{suffix}"
    local = f"actions-smoke:{suffix}"
    fixture = ROOT / "tests/fixtures/container"
    pushed = []
    try:
        run(
            "go",
            "build",
            "-trimpath",
            "-o",
            str(fixture / "fixture"),
            str(fixture / "main.go"),
            env=dict(os.environ, CGO_ENABLED="0", GOOS="linux", GOARCH="amd64"),
        )
        run(
            "docker",
            "buildx",
            "build",
            "--load",
            "--provenance=false",
            "-t",
            local,
            str(fixture),
        )
        run("bash", str(fixture / "smoke.sh"), local, "linux/amd64", "1.0.0")
        run(
            "docker",
            "run",
            "--detach",
            "--name",
            registry,
            "--publish",
            "127.0.0.1::5000",
            "registry:3",
        )
        port = capture(
            "docker",
            "inspect",
            registry,
            "--format",
            '{{(index (index .NetworkSettings.Ports "5000/tcp") 0).HostPort}}',
        )
        import urllib.request

        for attempt in range(30):
            try:
                urllib.request.urlopen(
                    f"http://localhost:{port}/v2/", timeout=2
                ).close()
                break
            except OSError:
                if attempt == 29:
                    raise
                time.sleep(0.2)
        image = f"localhost:{port}/fixture"
        pushed.extend([image + ":platform", image + ":candidate"])
        run("docker", "tag", local, image + ":platform")
        run("docker", "push", image + ":platform")
        run(
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--tag",
            image + ":candidate",
            image + ":platform",
        )
        digest = json.loads(
            capture(
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                image + ":candidate",
                "--format",
                "{{json .Manifest}}",
            )
        )["digest"]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "result").mkdir()
            # Execute the actual index assembly script, including its platform gate.
            workflow = yaml.safe_load(
                (ROOT / ".github/workflows/docker-build-push.yml").read_text()
            )
            index_step = next(
                step
                for step in workflow["jobs"]["index"]["steps"]
                if step.get("id") == "index"
            )
            platform_digest = images.existing_digest(image, "platform")
            (directory / "linux-amd64.digest").write_text(platform_digest)
            output = directory / "outputs"
            env = dict(
                os.environ,
                IMAGE=image,
                CANDIDATE=image + ":verified-index",
                DIGEST_DIR=str(directory),
                BUILD_MATRIX='[{"platform":"linux/amd64"}]',
                GITHUB_OUTPUT=str(output),
            )
            run("bash", "-eo", "pipefail", "-c", index_step["run"], env=env)
            digest = output.read_text().strip().removeprefix("digest=")
            assert images.existing_digest(image, "1.0.0") is None
            assert images.existing_digest(image, "latest") is None
            plan = {
                "schema-version": 1,
                "repository": "fixture/local",
                "revision": "a" * 40,
                "run-id": "1",
                "scope": "fixture",
                "version": "1.0.0",
                "mode": "release",
                "images": [
                    {
                        "id": "fixture",
                        "image": image,
                        "platforms": ["linux/amd64"],
                        "final-tags": ["1.0.0", "latest"],
                    }
                ],
            }
            record = {
                "image": image,
                "digest": digest,
                "revision": "a" * 40,
                "run-id": "1",
                "platforms": ["linux/amd64"],
            }
            (directory / "result/image-result.json").write_text(json.dumps(record))
            result = images.promote(
                plan, directory / "result", directory / "manifest.json"
            )
            assert result["images"]["fixture"] == f"{image}@{digest}"
            # A promotion-only retry must be idempotent against real registry manifests.
            images.promote(plan, directory / "result", directory / "manifest.json")
            # The shared index publisher must preserve the verified digest too.
            with patch.dict(
                os.environ,
                {
                    "IMAGE_NAME": image,
                    "IMAGE_DIGEST": digest,
                    "IMAGE_TAGS": f"{image}:v1.0.0\n{image}:latest",
                },
            ):
                images.promote_image()
            assert images.existing_digest(image, "1.0.0") == digest
            assert images.existing_digest(image, "v1.0.0") is None
        print(
            "PASS: real Node/Python fixtures, loaded-image smoke, exact digest promotion and retry"
        )
    finally:
        subprocess.run(["docker", "rm", "--force", registry], capture_output=True)
        subprocess.run(
            ["docker", "image", "rm", "--force", local, *pushed], capture_output=True
        )
        (fixture / "fixture").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
