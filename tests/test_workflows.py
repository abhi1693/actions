import json
import os
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


class WorkflowInterfaces(unittest.TestCase):
    def test_builds_and_indexes_consume_normalized_image_tags(self):
        for job in workflow("docker-build-push.yml")["jobs"].values():
            self.assertEqual(
                job["outputs"]["tags"], "${{ steps.normalized-tags.outputs.tags }}"
            )
            steps = job["steps"]
            normalized = next(s for s in steps if s.get("id") == "normalized-tags")
            self.assertEqual(
                normalized["env"]["IMAGE_TAGS"], "${{ steps.meta.outputs.tags }}"
            )
            for step in steps:
                if step.get("id") == "build":
                    self.assertEqual(
                        step["with"]["tags"],
                        "${{ steps.normalized-tags.outputs.tags }}",
                    )
                if step.get("id") == "runtime":
                    self.assertEqual(
                        step["env"]["TAGS"], "${{ steps.normalized-tags.outputs.tags }}"
                    )
        self.assertNotIn(
            "release-tag-prefix",
            workflow("container-images.yml")[True]["workflow_call"]["inputs"],
        )

    def test_container_security_defaults_and_final_tag_gate(self):
        docker = workflow("docker-build-push.yml")
        inputs = docker[True]["workflow_call"]["inputs"]
        for name in ("scan", "sbom"):
            self.assertIs(inputs[name]["default"], True)
        self.assertEqual(inputs["provenance"]["default"], "mode=max")
        self.assertIs(inputs["scan-ignore-unfixed"]["default"], False)
        suite = workflow("container-images.yml")
        self.assertIs(suite[True]["workflow_call"]["inputs"]["attest"]["default"], True)
        for name in ("sbom", "provenance"):
            self.assertNotIn(name, suite["jobs"]["build"]["with"])
        steps = docker["jobs"]["build"]["steps"]
        names = [s.get("name") for s in steps]
        index = docker["jobs"]["index"]
        self.assertEqual(index["needs"], "build")
        self.assertEqual(index["if"], "inputs.push")
        index_names = [s.get("name") for s in index["steps"]]
        self.assertLess(
            index_names.index("Assemble and verify OCI index"),
            index_names.index("Publish verified image tags"),
        )
        for gate in (
            "Scan runtime image",
            "Smoke-test runtime image",
            "Upload runtime reports",
        ):
            self.assertLess(
                names.index(gate), names.index("Record platform image digest")
            )
        sbom = next(s for s in steps if s.get("name") == "Generate runtime SBOM")
        self.assertEqual(sbom["if"], "inputs.sbom")
        reports = next(s for s in steps if s.get("name") == "Upload runtime reports")
        self.assertEqual(reports["with"]["if-no-files-found"], "error")
        self.assertIn("inputs.sbom", reports["if"])

    def test_one_supported_native_build_contract(self):
        docker = workflow("docker-build-push.yml")
        inputs = docker[True]["workflow_call"]["inputs"]
        self.assertIs(inputs["matrix"]["required"], True)
        self.assertIs(inputs["push"]["default"], False)
        for name in ("runs-on", "platforms", "qemu"):
            self.assertNotIn(name, inputs)
        self.assertEqual(
            docker["jobs"]["build"]["strategy"]["matrix"]["include"],
            "${{ fromJSON(inputs.matrix) }}",
        )
        python = workflow("python-ci.yml")[True]["workflow_call"]["inputs"]
        self.assertEqual(python["sync-command"]["default"], "uv sync --locked")
        self.assertEqual(python["timeout-minutes"]["default"], 20)
        self.assertFalse((ROOT / ".github/workflows/python-uv-tests.yml").exists())

    def test_native_matrix_validation_rejects_invalid_builds(self):
        step = next(
            s
            for s in workflow("docker-build-push.yml")["jobs"]["build"]["steps"]
            if s.get("id") == "platform"
        )
        native = "linux/arm64" if platform.machine() == "aarch64" else "linux/amd64"
        other = "linux/amd64" if native == "linux/arm64" else "linux/arm64"
        valid = [{"runner": "native", "platform": native}]
        cases = [
            (valid, native, True),
            (valid, other, False),
            (valid * 2, native, False),
            (
                [{"runner": "native", "platform": "linux/amd64,linux/arm64"}],
                native,
                False,
            ),
            ([], native, False),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for matrix, selected, success in cases:
                with self.subTest(matrix=matrix, selected=selected):
                    result = subprocess.run(
                        ["bash", "-eo", "pipefail", "-c", step["run"]],
                        env=dict(
                            os.environ,
                            BUILD_MATRIX=json.dumps(matrix),
                            PLATFORM=selected,
                            GITHUB_OUTPUT=str(Path(tmp) / "output"),
                        ),
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode == 0, success, result.stderr)

    def test_internal_references_exist_and_external_actions_are_pinned(self):
        for path in (ROOT / ".github").rglob("*.yml"):
            data = yaml.safe_load(path.read_text())

            def visit(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key == "uses":
                            if item.startswith("$/"):
                                self.assertTrue(
                                    (ROOT / item[2:] / "action.yml").is_file(), item
                                )
                            elif item.startswith("./"):
                                self.assertTrue((ROOT / item[2:]).is_file(), item)
                            else:
                                self.assertRegex(item, r"@[a-f0-9]{40}$")
                        visit(item)
                elif isinstance(value, list):
                    for item in value:
                        visit(item)

            visit(data)

    def test_local_reusable_call_contracts(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            for job in yaml.safe_load(path.read_text())["jobs"].values():
                if not job.get("uses", "").startswith("./"):
                    continue
                called = yaml.safe_load((ROOT / job["uses"][2:]).read_text())[True][
                    "workflow_call"
                ]
                for key in job.get("with", {}):
                    self.assertIn(key, called.get("inputs", {}), (path, key))
                for key, value in called.get("inputs", {}).items():
                    if value.get("required"):
                        self.assertIn(key, job.get("with", {}), (path, key))
                for key in job.get("secrets", {}):
                    self.assertIn(key, called.get("secrets", {}), (path, key))

    def test_image_promotion_depends_on_successful_verification(self):
        pipeline = workflow("container-images.yml")["jobs"]
        self.assertEqual(
            pipeline["build"]["uses"], "./.github/workflows/docker-build-push.yml"
        )
        self.assertIn("needs.build.result == 'success'", pipeline["promote"]["if"])
        self.assertIn("needs.attest.result == 'success'", pipeline["promote"]["if"])
        steps = workflow("docker-build-push.yml")["jobs"]["build"]["steps"]
        names = [s.get("name") for s in steps]
        self.assertLess(
            names.index("Scan runtime image"),
            names.index("Record platform image digest"),
        )
        self.assertLess(
            names.index("Smoke-test runtime image"),
            names.index("Record platform image digest"),
        )
        for step in steps:
            if step.get("name") in ("Scan runtime image", "Smoke-test runtime image"):
                self.assertNotIn("continue-on-error", step)
        self.assertEqual(pipeline["required"]["if"], "${{ always() }}")

    def test_configured_reports_fail_when_missing(self):
        for name in ("node-ci.yml", "python-ci.yml"):
            for job in workflow(name)["jobs"].values():
                reports = [
                    s
                    for s in job["steps"]
                    if s.get("name") in ("Upload test reports", "Upload test results")
                ]
                self.assertEqual(len(reports), 1)
                self.assertIn("always()", reports[0]["if"])
                self.assertEqual(reports[0]["with"]["if-no-files-found"], "error")


if __name__ == "__main__":
    unittest.main()
