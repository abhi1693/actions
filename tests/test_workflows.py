import json
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
                        "${{ steps.staged-tags.outputs.tags }}",
                    )
                if step.get("id") in ("runtime", "index"):
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
        publish = next(
            s for s in steps if s.get("name") == "Publish verified image tags"
        )
        self.assertEqual(publish["if"], "inputs.push && inputs.matrix == ''")
        for gate in (
            "Scan runtime image",
            "Smoke-test runtime image",
            "Upload runtime reports",
        ):
            self.assertLess(names.index(gate), names.index(publish["name"]))
        sbom = next(s for s in steps if s.get("name") == "Generate runtime SBOM")
        self.assertEqual(sbom["if"], "inputs.sbom")
        reports = next(s for s in steps if s.get("name") == "Upload runtime reports")
        self.assertEqual(reports["with"]["if-no-files-found"], "error")
        self.assertIn("inputs.sbom", reports["if"])

    def test_existing_inputs_defaults_outputs_and_secrets_remain_compatible(self):
        legacy = json.loads(
            (ROOT / "tests/contracts/legacy-workflows.json").read_text()
        )
        for name, previous in legacy.items():
            current = workflow(name)[True]["workflow_call"]
            for key, old in previous["inputs"].items():
                for field in ("type", "default", "required"):
                    self.assertEqual(
                        current["inputs"][key].get(field),
                        old.get(field),
                        (name, key, field),
                    )
            for key, old in previous.get("secrets", {}).items():
                self.assertEqual(current["secrets"][key]["required"], old["required"])
            self.assertEqual(current.get("outputs", {}), previous.get("outputs", {}))

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
        for name in ("node-ci.yml", "python-uv-tests.yml"):
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
