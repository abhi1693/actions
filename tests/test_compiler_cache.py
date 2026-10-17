"""Compiler-only payload, compatibility and BuildKit lifecycle regressions."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / ".github/actions/workflow-tools"
with patch.object(sys, "path", [str(HELPERS), *sys.path]):
    spec = importlib.util.spec_from_file_location(
        "compiler_cache", HELPERS / "compiler_cache.py"
    )
    cache = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cache)


class CompilerCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "Dockerfile").write_text("FROM scratch\n")
        (self.root / "package-lock.json").write_text('{"lockfileVersion":3}')
        self.environment = {
            "COMPILER_CACHE_TARGET": "/app/apps/web/.next/cache",
            "COMPILER_CACHE_KEY_FILES": '["package-lock.json"]',
            "PLATFORM": "linux/amd64",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_WORKSPACE": str(self.root),
            "DOCKERFILE": "Dockerfile",
            "IMAGE": "ghcr.io/example/web",
            "CACHE_SCOPE": "web",
        }

    def test_opt_in_validation_and_legacy_descriptor_defaults(self):
        cache.compiler_cache_settings("", [])
        cache.compiler_cache_settings("/app/.next/cache", ["package-lock.json"])
        for target, files in [
            (None, []),
            (False, []),
            ("", ["lock"]),
            ("/app/.next/cache", []),
            ("relative/.next/cache", ["lock"]),
            ("/app/../.next/cache", ["lock"]),
            ("//app/.next/cache", ["lock"]),
            ("/app/.next/cache/", ["lock"]),
            ("/app/.next/cache,id=other", ["lock"]),
            ("/etc", ["lock"]),
            ("/app/.next/cache", ["../lock"]),
            ("/app/.next/cache", ["*.json"]),
            ("/app/.next/cache", ["lock\ninjected"]),
            ("/app/.next/cache", ["lock", "lock"]),
        ]:
            with (
                self.subTest(target=target, files=files),
                self.assertRaises(ValueError),
            ):
                cache.compiler_cache_settings(target, files)

    def test_source_changes_keep_compatibility_but_not_primary_key(self):
        first = cache.settings(self.environment)
        second = cache.settings({**self.environment, "GITHUB_SHA": "b" * 40})
        self.assertEqual(first["restore-prefix"], second["restore-prefix"])
        self.assertNotEqual(first["key"], second["key"])
        self.assertTrue(first["key"].startswith(first["restore-prefix"]))
        self.assertTrue(first["key"].startswith("docker-next-v1-linux-amd64-"))

    def test_every_declared_compatibility_boundary_changes_the_prefix(self):
        first = cache.settings(self.environment)["restore-prefix"]
        for key, value in {
            "PLATFORM": "linux/arm64",
            "IMAGE": "ghcr.io/example/admin",
            "CACHE_SCOPE": "other",
            "COMPILER_CACHE_TARGET": "/app/other/.next/cache",
            "BUILD_TARGET": "production",
            "BUILD_ARGS": "PUBLIC_API=https://api.example",
        }.items():
            with self.subTest(key=key):
                self.assertNotEqual(
                    cache.settings({**self.environment, key: value})["restore-prefix"],
                    first,
                )
        for name in (
            "package-lock.json",
            "Dockerfile",
            ".dockerignore",
            "Dockerfile.dockerignore",
        ):
            with self.subTest(name=name):
                file = self.root / name
                old = file.read_bytes() if file.exists() else None
                file.write_text("changed")
                self.assertNotEqual(
                    cache.settings(self.environment)["restore-prefix"], first
                )
                if old is None:
                    file.unlink()
                else:
                    file.write_bytes(old)
        # SHA-valued compiler configuration remains significant; no guessed metadata exception.
        a = cache.settings(
            {**self.environment, "BUILD_ARGS": "PUBLIC_COMMIT=" + "a" * 40}
        )
        b = cache.settings(
            {
                **self.environment,
                "BUILD_ARGS": "PUBLIC_COMMIT=" + "b" * 40,
                "GITHUB_SHA": "b" * 40,
            }
        )
        self.assertNotEqual(a["restore-prefix"], b["restore-prefix"])

    def test_compatibility_files_reject_symlinks_ancestors_fifos_and_large_files(self):
        outside = self.root / "outside"
        outside.write_text("private")
        (self.root / "link").symlink_to(outside)
        (self.root / "directory").mkdir()
        (self.root / "alias").symlink_to(
            self.root / "directory", target_is_directory=True
        )
        (self.root / "directory/input").write_text("regular")
        os.mkfifo(self.root / "fifo")
        with (self.root / "large").open("wb") as stream:
            stream.truncate(33 * 1024**2)
        for name in ("link", "alias/input", "fifo", "large"):
            with self.subTest(name=name), self.assertRaises((OSError, ValueError)):
                cache.settings(
                    {**self.environment, "COMPILER_CACHE_KEY_FILES": json.dumps([name])}
                )

    def test_payload_copies_only_actual_compiler_entries_and_preserves_ts_file(self):
        source = self.root / "payload"
        (source / "turbopack/nested").mkdir(parents=True)
        (source / "turbopack/nested/compiler").write_bytes(b"cache")
        (source / ".tsbuildinfo").write_bytes(b"typescript")
        for excluded in ("fetch-cache", "images", "reports"):
            (source / excluded).mkdir()
            (source / excluded / "private").write_text("not compiler data")
        destination = self.root / "filtered"
        counts = cache.filtered_copy(source, destination)
        self.assertEqual(counts, {"files": 2, "bytes": 15})
        self.assertEqual(
            {p.name for p in destination.iterdir()}, {"turbopack", ".tsbuildinfo"}
        )
        self.assertTrue((destination / ".tsbuildinfo").is_file())
        self.assertEqual(
            (destination / "turbopack/nested/compiler").read_bytes(), b"cache"
        )

    def test_missing_cache_and_unused_compiler_formats_are_allowed(self):
        self.assertEqual(
            cache.filtered_copy(self.root / "missing", self.root / "out"),
            {"files": 0, "bytes": 0},
        )

    def test_payload_rejects_allowed_symlinks_and_special_files_without_blocking(self):
        for kind in ("root-link", "entry-link", "nested-link", "fifo", "ts-directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "input"
                source.mkdir()
                if kind == "root-link":
                    source.rmdir()
                    source.symlink_to(self.root, target_is_directory=True)
                elif kind == "entry-link":
                    (source / "turbopack").symlink_to(
                        self.root, target_is_directory=True
                    )
                elif kind == "ts-directory":
                    (source / ".tsbuildinfo").mkdir()
                else:
                    (source / "turbopack").mkdir()
                    if kind == "fifo":
                        os.mkfifo(source / "turbopack/special")
                    else:
                        (source / "turbopack/link").symlink_to(
                            self.root / "package-lock.json"
                        )
                with self.assertRaises((OSError, ValueError)):
                    cache.filtered_copy(source, root / "out")

    def test_payload_bounds_include_empty_directories_and_actual_bytes(self):
        source = self.root / "payload"
        (source / "turbopack/empty/nested").mkdir(parents=True)
        with patch.object(cache, "MAX_FILES", 2), self.assertRaises(ValueError):
            cache.filtered_copy(source, self.root / "too-many")
        (source / "turbopack/data").write_bytes(b"12345")
        with patch.object(cache, "MAX_BYTES", 4), self.assertRaises(ValueError):
            cache.filtered_copy(source, self.root / "too-large")

    def test_transfer_uses_same_builder_nonce_and_never_prunes_mounts(self):
        payload = self.root / "payload"
        (payload / "turbopack").mkdir(parents=True)
        (payload / "turbopack/compiler").write_text("warm")
        (payload / "images").mkdir()
        (payload / "images/forbidden").write_text("private")
        value = {**cache.settings(self.environment), "payload": str(payload)}
        calls = []

        def build(command, **kwargs):
            self.assertTrue(kwargs["check"])
            self.assertNotIn("shell", kwargs)
            self.assertEqual(command[command.index("--builder") + 1], "owned-builder")
            self.assertNotIn("--no-cache", command)
            self.assertIn("--network=none", command)
            dockerfile = Path(command[command.index("--file") + 1]).read_text()
            self.assertIn(f"FROM {cache.HELPER_IMAGE}", dockerfile)
            self.assertIn(
                "id=/app/apps/web/.next/cache,target=/cache,sharing=locked", dockerfile
            )
            self.assertIn('test -n "$COMPILER_TRANSFER_NONCE"', dockerfile)
            nonce = command[command.index("--build-arg") + 1]
            calls.append(nonce)
            output = command[command.index("--output") + 1]
            if output == "type=cacheonly":
                seed = Path(command[-1]) / "payload"
                self.assertTrue((seed / "turbopack/compiler").is_file())
                self.assertFalse((seed / "images").exists())
            else:
                exported = Path(output.removeprefix("type=local,dest="))
                (exported / "turbopack").mkdir(parents=True)
                (exported / "turbopack/compiler").write_text(f"updated-{len(calls)}")
                (exported / "fetch-cache").mkdir()
                (exported / "fetch-cache/forbidden").write_text("never persist")

        with patch.object(cache.subprocess, "run", side_effect=build):
            cache.transfer(value, "owned-builder", False)
            cache.transfer(value, "owned-builder", True)
            self.assertEqual((payload / "turbopack/compiler").read_text(), "updated-2")
            cache.transfer(value, "owned-builder", True)
            self.assertEqual((payload / "turbopack/compiler").read_text(), "updated-3")
        self.assertEqual(len(set(calls)), 3)
        self.assertFalse((payload / "fetch-cache").exists())

    def test_failed_export_preserves_previous_payload(self):
        payload = self.root / "payload"
        payload.mkdir()
        (payload / ".tsbuildinfo").write_text("previous")
        value = {**cache.settings(self.environment), "payload": str(payload)}
        with patch.object(
            cache.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, "docker"),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                cache.transfer(value, "owned-builder", True)
        self.assertEqual((payload / ".tsbuildinfo").read_text(), "previous")


class CompilerWorkflowTests(unittest.TestCase):
    def test_python_sast_explicitly_includes_hidden_shared_helpers_without_exclusions(
        self,
    ):
        workflow = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text())
        security = workflow["jobs"]["security"]
        self.assertIn("python", json.loads(security["with"]["codeql-languages"]))
        config = yaml.safe_load((ROOT / security["with"]["codeql-config"]).read_text())
        self.assertEqual(config["paths"], [".", ".github/actions/workflow-tools"])
        self.assertNotIn("paths-ignore", config)
        self.assertIn("security", workflow["jobs"]["required"]["needs"])

    def test_image_descriptor_opt_in_is_validated_and_fixture_executes_it(self):
        with patch.object(sys, "path", [str(HELPERS), *sys.path]):
            import images

        manifest = json.loads(
            (ROOT / "tests/fixtures/container/images.json").read_text()
        )
        selected, _ = images.descriptors(manifest, "example/repo", ROOT)
        self.assertEqual(selected[0]["compiler-cache-target"], "/fixture/.next/cache")
        old = json.loads(json.dumps(manifest))
        del old["images"][0]["compiler-cache-target"]
        del old["images"][0]["compiler-cache-key-files"]
        legacy, _ = images.descriptors(old, "example/repo", ROOT)
        self.assertNotIn("compiler-cache-target", legacy[0])
        malformed = json.loads(json.dumps(manifest))
        malformed["images"][0]["compiler-cache-key-files"] = []
        with self.assertRaises(ValueError):
            images.descriptors(malformed, "example/repo", ROOT)

    def test_plan_cli_outputs_only_four_nested_payload_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            (root / "lock").write_text("compatibility")
            output = root / "output"
            subprocess.run(
                [sys.executable, str(HELPERS / "compiler_cache.py"), "plan"],
                check=True,
                env=dict(
                    os.environ,
                    COMPILER_CACHE_TARGET="/app/.next/cache",
                    COMPILER_CACHE_KEY_FILES='["lock"]',
                    IMAGE="fixture",
                    CACHE_SCOPE="fixture",
                    PLATFORM="linux/amd64",
                    DOCKERFILE="Dockerfile",
                    GITHUB_SHA="a" * 40,
                    GITHUB_WORKSPACE=str(root),
                    RUNNER_TEMP=str(root),
                    GITHUB_OUTPUT=str(output),
                ),
            )
            raw = output.read_text()
            for entry in cache.ENTRIES:
                self.assertIn(str(root / "docker-compiler-cache/payload" / entry), raw)
            self.assertNotIn("fetch-cache", raw)
            self.assertNotIn("images", raw)

    def test_optional_payload_lifecycle_preserves_main_build_and_security_gates(self):
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/docker-build-push.yml").read_text()
        )
        inputs = workflow[True]["workflow_call"]["inputs"]
        self.assertEqual(inputs["compiler-cache-target"]["default"], "")
        self.assertEqual(inputs["compiler-cache-key-files"]["default"], "[]")
        steps = workflow["jobs"]["build"]["steps"]
        by_name = {s.get("name"): s for s in steps}
        order = [
            "Restore compiler cache payload",
            "Inject compiler cache mount",
            "Build and push",
            "Scan runtime image",
            "Smoke-test runtime image",
            "Extract compiler cache payload",
            "Save compiler cache payload",
        ]
        indices = [steps.index(by_name[name]) for name in order]
        self.assertEqual(indices, sorted(indices))
        builder = "${{ steps.buildx.outputs.name }}"
        self.assertEqual(by_name["Set up Docker Buildx"]["id"], "buildx")
        self.assertEqual(by_name["Build and push"]["with"]["builder"], builder)
        for name in ("Inject compiler cache mount", "Extract compiler cache payload"):
            self.assertEqual(by_name[name]["env"]["BUILDER"], builder)
            self.assertEqual(by_name[name]["if"], "inputs.compiler-cache-target != ''")
        self.assertIn(
            "steps.compiler-extract.outputs.nonempty == 'true'",
            by_name["Save compiler cache payload"]["if"],
        )
        self.assertNotIn("always()", by_name["Save compiler cache payload"]["if"])
        restore = by_name["Restore compiler cache payload"]["with"]
        self.assertEqual(
            restore["restore-keys"],
            "${{ steps.compiler-cache.outputs.restore-prefix }}",
        )
        self.assertEqual(restore["path"], "${{ steps.compiler-cache.outputs.paths }}")
        self.assertEqual(
            by_name["Build and push"]["with"]["cache-from"],
            "${{ steps.cache.outputs.from }}",
        )
        self.assertEqual(
            by_name["Build and push"]["with"]["cache-to"],
            "${{ steps.cache.outputs.to }}",
        )
        self.assertEqual(by_name["Scan runtime image"]["with"]["exit-code"], "1")


if __name__ == "__main__":
    unittest.main()
