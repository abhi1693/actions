"""Tests for trust boundaries, selection, exact digest promotion and CI stage behavior."""

import contextlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / ".github/actions/workflow-tools"


def module(name):
    spec = importlib.util.spec_from_file_location(name, HELPERS / f"{name}.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


images = module("images")
project = module("project")
security = module("security")


@contextlib.contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class ProjectContracts(unittest.TestCase):
    def test_secret_scan_event_range_and_invalid_sha(self):
        before, after = "a" * 40, "b" * 40
        event = {"before": before, "after": after}
        self.assertEqual(
            security.secret_log_options("event", "push", event),
            ["--log-opts", f"{before}..{after}"],
        )
        self.assertEqual(security.secret_log_options("history", "push", event), [])
        self.assertEqual(security.secret_log_options("event", "schedule", {}), [])
        event["before"] = "0" * 40
        self.assertEqual(security.secret_log_options("event", "push", event), [])
        event["after"] = "--all"
        with self.assertRaises(ValueError):
            security.secret_log_options("event", "push", event)

    def test_reserved_environment_rejected(self):
        for name in (
            "GITHUB_TOKEN",
            "RUNNER_TEMP",
            "PATH",
            "NODE_OPTIONS",
            "BASH_ENV",
            "LD_PRELOAD",
            "bad-name",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                project.environment(json.dumps({name: "value"}), "{}")

    def test_environment_secret_override_and_multiline(self):
        result = project.environment(
            '{"APP_MODE":"test","APP_PORT":42}', '{"APP_MODE":"line1\\nline2"}'
        )
        self.assertEqual(result, {"APP_MODE": "line1\nline2", "APP_PORT": "42"})
        with self.assertRaises(ValueError):
            project.environment('{"APP": {}}', "{}")

    def test_unset_github_values_export_as_empty_strings(self):
        self.assertEqual(
            project.environment(
                '{"UNSET_VAR":null,"ENABLED":false}', '{"UNSET_SECRET":null}'
            ),
            {"UNSET_VAR": "", "ENABLED": "false", "UNSET_SECRET": ""},
        )

    def test_fixed_stage_order_and_invalid_contract(self):
        self.assertEqual(
            project.node_stages('["build","lint"]', '{"lint":"lint:all"}'),
            [("lint", "lint:all"), ("build", "ci:build")],
        )
        for checks, scripts in [
            ("[]", "{}"),
            ('["test","test"]', "{}"),
            ('["deploy"]', "{}"),
            ('["test"]', '{"test":"test;echo injected"}'),
        ]:
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                project.node_stages(checks, scripts)

    def test_real_node_stage_and_missing_declared_script(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            Path("package.json").write_text(
                json.dumps({"scripts": {"ci:test": 'node -e "process.exit(7)"'}})
            )
            with patch.dict(os.environ, {"CHECKS": '["test"]', "SCRIPTS": "{}"}):
                with self.assertRaises(subprocess.CalledProcessError):
                    project.run_node_stage("test")
            Path("package.json").write_text('{"scripts":{}}')
            with (
                patch.dict(os.environ, {"CHECKS": '["test"]', "SCRIPTS": "{}"}),
                self.assertRaises(ValueError),
            ):
                project.run_node_stage("test")

    def test_generated_files_detect_tracked_staged_and_untracked_changes(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            subprocess.run(["git", "init", "-q"], check=True)
            Path("generated").mkdir()
            Path("generated/client.txt").write_text("original")
            subprocess.run(["git", "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            project.check_generated('["generated"]')
            Path("generated/new.txt").write_text("new")
            with self.assertRaises(ValueError):
                project.check_generated('["generated"]')
            Path("generated/new.txt").unlink()
            Path("generated/client.txt").write_text("changed")
            subprocess.run(["git", "add", "."], check=True)
            with self.assertRaises(subprocess.CalledProcessError):
                project.check_generated('["generated"]')


class ImagePlanning(unittest.TestCase):
    def data(self):
        return {
            "schema-version": 1,
            "shared-paths": ["lockfile"],
            "images": [
                {"id": "api", "paths": ["api/**"]},
                {"id": "web", "paths": ["web/**"], "change-dependencies": ["api"]},
                {"id": "worker", "paths": ["worker/**"]},
            ],
        }

    def test_changed_components_include_dependants_and_shared_changes(self):
        items, shared = images.descriptors(self.data(), "owner/repo")
        self.assertEqual(
            [i["id"] for i in images.select(items, shared, ["api/main.py"])],
            ["api", "web"],
        )
        self.assertEqual(images.select(items, shared, ["README.md"]), [])
        self.assertEqual(images.select(items, shared, ["lockfile"]), items)
        self.assertEqual(images.select(items, shared, None), items)

    def test_manifest_rejects_unsafe_or_ambiguous_descriptors(self):
        bad = [
            {"file": "../Dockerfile"},
            {"context": "/tmp"},
            {"platforms": ["linux/arm64", "linux/arm64"]},
            {"platforms": ["windows/amd64"]},
            {"image": "ghcr.io/owner/repo:latest"},
            {"build-args": {"ARG": "first\nOTHER=secret"}},
            {"unexpected": "value"},
            {"change-dependencies": ["missing"]},
        ]
        for changes in bad:
            data = {"schema-version": 1, "images": [{"id": "api", **changes}]}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                images.descriptors(data, "owner/repo")
        with self.assertRaises(ValueError):
            images.descriptors(
                {"schema-version": 1, "images": [{"id": "same"}, {"id": "same"}]},
                "owner/repo",
            )

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outside").symlink_to("/tmp", target_is_directory=True)
            with self.assertRaises(ValueError):
                images.relative("outside/Dockerfile", root)

    def test_release_prerelease_and_event_trust(self):
        def policy(**kw):
            args = dict(
                event={},
                ref_type="tag",
                ref_name="v1.2.3",
                supplied="",
                publish=True,
                trusted_branches=["master"],
                event_name="release",
                actor="owner",
            )
            args.update(kw)
            return images.version_policy(**args)

        self.assertEqual(policy(), ("1.2.3", ["1.2.3", "latest"], "release"))
        self.assertEqual(policy(ref_name="v1.2.3-rc.1")[1], ["1.2.3-rc.1"])
        self.assertEqual(policy(event={"release": {"prerelease": True}})[1], ["1.2.3"])
        for kw in (
            {"event_name": "pull_request"},
            {"event_name": "pull_request_target"},
            {"actor": "dependabot[bot]"},
            {"supplied": "2.0.0"},
            {"ref_name": "v01.2.3"},
            {"ref_name": "v1.2.3+build"},
            {"ref_type": "branch", "ref_name": "feature", "event_name": "push"},
        ):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                policy(**kw)

    def test_index_checks_exact_platforms_and_ignores_attestations(self):
        index = {
            "manifests": [
                {"platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
                {
                    "platform": {"os": "unknown", "architecture": "unknown"},
                    "annotations": {
                        "vnd.docker.reference.type": "attestation-manifest"
                    },
                },
            ]
        }
        images.verify_index(index, ["linux/arm64"])
        with self.assertRaises(ValueError):
            images.verify_index(index, ["linux/amd64", "linux/arm64"])
        index["manifests"].append(index["manifests"][0])
        with self.assertRaises(ValueError):
            images.verify_index(index, ["linux/arm64"])


class ImagePromotion(unittest.TestCase):
    def test_docker_metadata_tags_drop_only_full_version_prefixes(self):
        self.assertEqual(
            images.docker_tags(
                "localhost:5000/app:v1.2.3\nlocalhost:5000/app:1.2.3\n"
                "localhost:5000/app:v1.2.3-rc.1\nlocalhost:5000/app:latest\n"
                "localhost:5000/app:vendor-build"
            ),
            [
                "localhost:5000/app:1.2.3",
                "localhost:5000/app:1.2.3-rc.1",
                "localhost:5000/app:latest",
                "localhost:5000/app:vendor-build",
            ],
        )
        for invalid in ("", "ghcr.io/owner/app", "ghcr.io/owner/app:v1.2.3;bad"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                images.docker_tags(invalid)

    def test_release_plan_has_unprefixed_deduplicated_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Dockerfile").write_text("FROM scratch\n")
            (root / "images.json").write_text(
                json.dumps(
                    {
                        "schema-version": 1,
                        "images": [{"id": "app", "extra-tags": ["v1.2.3", "1.2.3"]}],
                    }
                )
            )
            (root / "event.json").write_text('{"release":{"tag_name":"v1.2.3"}}')
            env = {
                "GITHUB_WORKSPACE": tmp,
                "MANIFEST": "images.json",
                "GITHUB_EVENT_PATH": str(root / "event.json"),
                "GITHUB_REPOSITORY": "owner/app",
                "PUBLISH": "true",
                "TRUSTED_BRANCHES": '["master"]',
                "VERSION": "",
                "VERSION_FILE": "",
                "GITHUB_REF_TYPE": "tag",
                "GITHUB_REF_NAME": "v1.2.3",
                "GITHUB_EVENT_NAME": "release",
                "GITHUB_ACTOR": "owner",
                "ARTIFACT_SCOPE": "images",
                "GITHUB_SHA": "a" * 40,
                "GITHUB_RUN_ID": "42",
                "GITHUB_RUN_ATTEMPT": "1",
                "RUNNER_TEMP": tmp,
                "GITHUB_OUTPUT": str(root / "output"),
                "AMD64_RUNNER": "ubuntu-24.04",
                "ARM64_RUNNER": "ubuntu-24.04-arm",
                "LATEST": "true",
                "RELEASE_ALIASES": "[]",
            }
            with patch.dict(os.environ, env, clear=True):
                images.plan()
            plan = json.loads((root / "image-plan-images.json").read_text())
            self.assertEqual(plan["version"], "1.2.3")
            self.assertEqual(plan["images"][0]["final-tags"], ["1.2.3", "latest"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.digest = "sha256:" + "a" * 64
        self.plan = {
            "schema-version": 1,
            "repository": "owner/repo",
            "revision": "a" * 40,
            "run-id": "42",
            "scope": "images",
            "version": "1.0.0",
            "mode": "release",
            "images": [
                {
                    "id": "api",
                    "image": "ghcr.io/owner/api",
                    "platforms": ["linux/arm64"],
                    "final-tags": ["1.0.0", "latest"],
                },
                {
                    "id": "web",
                    "image": "ghcr.io/owner/web",
                    "platforms": ["linux/arm64"],
                    "final-tags": ["1.0.0", "latest"],
                },
            ],
        }
        for item in self.plan["images"]:
            directory = self.root / item["id"]
            directory.mkdir()
            (directory / "image-result.json").write_text(
                json.dumps(
                    {
                        "image": item["image"],
                        "digest": self.digest,
                        "revision": "a" * 40,
                        "run-id": "42",
                        "platforms": ["linux/arm64"],
                    }
                )
            )
        self.index = {
            "manifests": [{"platform": {"os": "linux", "architecture": "arm64"}}]
        }

    def test_complete_results_required_and_stale_runs_rejected(self):
        self.assertEqual(len(images.records_for(self.plan, self.root)), 2)
        file = self.root / "api/image-result.json"
        record = json.loads(file.read_text())
        record["run-id"] = "41"
        file.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            images.records_for(self.plan, self.root)
        file.unlink()
        with self.assertRaises(ValueError):
            images.records_for(self.plan, self.root)

    def test_duplicate_results_rejected(self):
        (self.root / "duplicate").mkdir()
        (self.root / "duplicate/image-result.json").write_text(
            (self.root / "api/image-result.json").read_text()
        )
        with self.assertRaises(ValueError):
            images.records_for(self.plan, self.root)

    def test_all_images_preflight_before_first_tag_write(self):
        for mode in ("release", "branch"):
            for tag in ("1.0.0", "v1.0.0", "1.0.0-rc.1", "v1.0.0-rc.1"):
                with self.subTest(mode=mode, tag=tag):
                    self.plan["mode"] = mode
                    for item in self.plan["images"]:
                        item["final-tags"] = [tag, "latest"]
                    with (
                        patch.object(images, "docker_json", return_value=self.index),
                        patch.object(
                            images,
                            "existing_digest",
                            side_effect=[None, "sha256:" + "b" * 64],
                        ),
                        patch.object(images.subprocess, "run") as run,
                    ):
                        with self.assertRaisesRegex(ValueError, "immutable tag"):
                            images.promote(
                                self.plan, self.root, self.root / "manifest.json"
                            )
                        run.assert_not_called()
                        self.assertFalse((self.root / "manifest.json").exists())

    def test_branch_semver_retry_and_moving_aliases(self):
        self.plan["mode"] = "branch"
        tags = ["v1.0.0", "latest", "master", "1", "1.2"]
        for item in self.plan["images"]:
            item["final-tags"] = tags
        with (
            patch.object(images, "docker_json", return_value=self.index),
            patch.object(
                images, "existing_digest", return_value=self.digest
            ) as inspect,
            patch.object(images.subprocess, "run") as run,
        ):
            images.promote(self.plan, self.root, self.root / "manifest.json")
            self.assertEqual(run.call_count, 2)
            # Only full versions are checked before writes; all tags are verified after.
            expected = [(item["image"], "v1.0.0") for item in self.plan["images"]]
            expected += [
                (item["image"], tag) for item in self.plan["images"] for tag in tags
            ]
            self.assertEqual([call.args for call in inspect.call_args_list], expected)

    def test_promoted_digest_must_equal_verified_digest(self):
        with (
            patch.object(images, "docker_json", return_value=self.index),
            patch.object(
                images,
                "existing_digest",
                side_effect=[None, None, "sha256:" + "b" * 64],
            ),
            patch.object(images.subprocess, "run"),
        ):
            with self.assertRaises(ValueError):
                images.promote(self.plan, self.root, self.root / "manifest.json")
            self.assertFalse((self.root / "manifest.json").exists())

    def test_successful_promotion_and_partial_rerun_keep_same_digest(self):
        with (
            patch.object(images, "docker_json", return_value=self.index),
            patch.object(images, "existing_digest", return_value=self.digest),
            patch.object(images.subprocess, "run") as run,
        ):
            output = images.promote(self.plan, self.root, self.root / "manifest.json")
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assertEqual(call.args[0][-1].split("@")[1], self.digest)
            self.assertEqual(
                output["images"]["api"], f"ghcr.io/owner/api@{self.digest}"
            )

    def test_registry_auth_failure_is_not_tag_absence(self):
        failure = subprocess.CompletedProcess(
            [], 1, "", "denied: authentication required"
        )
        with (
            patch.object(images.subprocess, "run", return_value=failure),
            self.assertRaises(RuntimeError),
        ):
            images.existing_digest("ghcr.io/owner/api", "1.0.0")


class SecurityGates(unittest.TestCase):
    def test_missing_failed_and_nonempty_sarif_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.sarif"
            with self.assertRaises(ValueError):
                security.sarif_gate(tmp)
            for runs in (
                [{"results": [{"ruleId": "bad"}]}],
                [{"invocations": [{"executionSuccessful": False}]}],
            ):
                path.write_text(json.dumps({"version": "2.1.0", "runs": runs}))
                with self.assertRaises(ValueError):
                    security.sarif_gate(tmp)
            path.write_text(json.dumps({"version": "2.1.0", "runs": [{"results": []}]}))
            security.sarif_gate(tmp)

    def test_failed_audit_writes_report_and_does_not_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "npm.json"
            failed = subprocess.CompletedProcess(
                [], 1, '{"vulnerabilities":1}', "registry error"
            )
            with (
                patch.object(security.subprocess, "run", return_value=failed),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                security.audit(["npm", "audit"], path)
            self.assertEqual(json.loads(path.read_text()), {"vulnerabilities": 1})


if __name__ == "__main__":
    unittest.main()
