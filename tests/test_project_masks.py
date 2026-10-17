"""The runner mask bridge preserves scalar strings without logging private data."""

import contextlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "mask_project", ROOT / ".github/actions/workflow-tools/project.py"
)
project = importlib.util.module_from_spec(spec)
spec.loader.exec_module(project)


class PrivateMaskTransport(unittest.TestCase):
    def prepare(self, root, parent, private, *, public="{}", name="values.json"):
        output = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "RUNNER_TEMP": str(root),
                    "PROJECT_MASK_FILE": str(parent / name),
                    "PROJECT_ENV": public,
                    "PROJECT_SECRETS": private,
                },
            ),
            contextlib.redirect_stdout(output),
        ):
            project.prepare_secret_masks()
        self.assertEqual(output.getvalue(), "")

    def test_lossless_scalar_normalization_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "project-masks-fixture"
            parent.mkdir(mode=0o700)
            self.prepare(
                root,
                parent,
                '{"FLOAT":1.0,"BIG":9007199254740993,"FLAG":true,"EMPTY":"","NULL":null}',
                public='{"PUBLIC":"not-a-mask"}',
            )
            target = parent / "values.json"
            self.assertEqual(
                json.loads(target.read_text()), ["1.0", "9007199254740993", "true"]
            )
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_existing_file_and_symlink_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "project-masks-fixture"
            parent.mkdir(mode=0o700)
            target = parent / "values.json"
            target.write_text("existing")
            with self.assertRaises(FileExistsError):
                self.prepare(root, parent, '{"TOKEN":"fixture"}')
            self.assertEqual(target.read_text(), "existing")
            target.unlink()
            target.symlink_to(root / "outside")
            with self.assertRaises(FileExistsError):
                self.prepare(root, parent, '{"TOKEN":"fixture"}')
            self.assertFalse((root / "outside").exists())

    def test_directory_namespace_permissions_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "project-masks-fixture"
            parent.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                self.prepare(root, parent, '{"TOKEN":"fixture"}')
            self.assertFalse((parent / "values.json").exists())
            parent.chmod(0o700)
            with self.assertRaises(ValueError):
                self.prepare(root, parent, "{}", name="other.json")
            link = root / "project-masks-link"
            link.symlink_to(parent, target_is_directory=True)
            with self.assertRaises(OSError):
                self.prepare(root, link, '{"TOKEN":"fixture"}')
            with self.assertRaises(ValueError):
                self.prepare(root, root, '{"TOKEN":"fixture"}')

    def test_invalid_or_oversized_input_never_creates_a_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "project-masks-fixture"
            parent.mkdir(mode=0o700)
            for raw in (
                '{"PATH":"fixture"}',
                '{"TOKEN":[]}',
                "not-json",
                json.dumps({"TOKEN": "x" * (1024 * 1024)}),
            ):
                with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                    self.prepare(root, parent, raw)
                self.assertEqual(list(parent.iterdir()), [])


class MaskWorkflowOrdering(unittest.TestCase):
    def test_every_environment_export_registers_sdk_masks_first(self):
        callers = []
        for source in (ROOT / ".github/workflows").glob("*.yml"):
            workflow = yaml.safe_load(source.read_text())
            for job in workflow["jobs"].values():
                steps = job.get("steps", [])
                for index, step in enumerate(steps):
                    if step.get("run") != 'python3 "$TOOLS/project.py" environment':
                        continue
                    callers.append(source.name)
                    mask = steps[index - 1]
                    self.assertEqual(mask["name"], "Register project secret masks")
                    self.assertEqual(
                        mask["uses"],
                        "actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3",
                    )
                    self.assertNotIn("if", mask)
                    self.assertNotIn("continue-on-error", mask)
                    self.assertEqual(mask["env"], step["env"])
                    self.assertEqual(
                        mask["with"]["script"],
                        "require(`${process.env.TOOLS}/project-masks.cjs`).registerProjectMasks(core);",
                    )
        self.assertCountEqual(callers, ["node-ci.yml", "python-ci.yml"])

    def test_sdk_contract_suite_is_mandatory(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text())
        steps = workflow["jobs"]["contracts"]["steps"]
        matches = [
            step for step in steps if step.get("name") == "Test project secret masking"
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["run"], "node --test tests/project-masks.test.cjs")
        self.assertNotIn("if", matches[0])
        self.assertNotIn("continue-on-error", matches[0])

    def test_both_hosted_fixtures_exercise_actual_runner_masking(self):
        jobs = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text())[
            "jobs"
        ]
        for runtime in ("node", "python"):
            self.assertEqual(
                json.loads(jobs[runtime]["secrets"]["project-env"]),
                {"CI_MASK_FIXTURE": f"public-{runtime}-mask-fixture"},
            )
