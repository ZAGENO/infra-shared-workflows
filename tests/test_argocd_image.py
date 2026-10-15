"""Offline Argo CD action tests using yq and a temporary local Git remote."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


MANIFEST_PATH = "charts/production/service/image.yaml"
PARAMETER_NAME = "service.image.tag"
PARAMETERS_PATH = "/spec/sources/0/helm/parameters"
MANIFEST = [
    {
        "op": "add",
        "path": PARAMETERS_PATH,
        "value": [
            {"name": PARAMETER_NAME, "value": "prod-1", "forceString": True},
            {"name": "service.image.repository", "value": "example/service"},
        ],
    },
    {"op": "add", "path": "/spec/project", "value": "default"},
]


class ArgoCDImageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.remote = self.root / "remote.git"
        self.checkout = self.root / "checkout"
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "MANIFEST_PATH": MANIFEST_PATH,
            "HELM_PARAMETER_NAME": PARAMETER_NAME,
            "PARAMETERS_PATH": PARAMETERS_PATH,
            "IMAGE_TAG": "prod-2",
            "BRANCH": "main",
        }
        self.git("init", "--bare", "--initial-branch=main", str(self.remote), cwd=self.root)
        self.git("clone", str(self.remote), str(self.checkout), cwd=self.root)
        self.git("config", "user.name", "Test Runner")
        self.git("config", "user.email", "test@example.invalid")
        self.manifest = self.checkout / MANIFEST_PATH
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(yaml.safe_dump(MANIFEST, sort_keys=False))
        self.git("add", ".")
        self.git("commit", "-m", "Initial manifest")
        self.git("push", "origin", "main")
        self.initial_head = self.git("rev-parse", "HEAD")
        action_path = Path(__file__).resolve().parents[1] / "update-argocd-image" / "action.yaml"
        self.action = yaml.safe_load(action_path.read_text())
        self.steps = {step["name"]: step for step in self.action["runs"]["steps"]}
        self.script = self.steps["Update and push image tag"]["run"]

    def git(self, *arguments, cwd=None):
        result = subprocess.run(
            ["git", *arguments], cwd=cwd or self.checkout, env=self.env,
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def execute(self, **environment):
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", self.script],
            cwd=self.checkout, env={**self.env, **environment},
            capture_output=True, text=True, check=False,
        )

    def remote_manifest(self):
        return yaml.safe_load(self.git("show", f"main:{MANIFEST_PATH}", cwd=self.remote))

    def test_updates_only_requested_tag_and_pushes(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = self.remote_manifest()
        self.assertEqual(updated[0]["value"][0]["value"], "prod-2")
        updated[0]["value"][0]["value"] = "prod-1"
        self.assertEqual(updated, MANIFEST)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "2")

    def test_unchanged_tag_does_not_commit(self):
        result = self.execute(IMAGE_TAG="prod-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.remote_manifest(), MANIFEST)

    def test_missing_parameter_fails_without_committing(self):
        result = self.execute(HELM_PARAMETER_NAME="missing.image.tag")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("found 0", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_duplicate_parameter_fails_without_changing_manifest(self):
        manifest = yaml.safe_load(self.manifest.read_text())
        manifest[0]["value"].append(dict(manifest[0]["value"][0]))
        self.manifest.write_text(yaml.safe_dump(manifest, sort_keys=False))
        original = self.manifest.read_text()
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("found 2", result.stderr)
        self.assertEqual(self.manifest.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial_head)

    def test_invalid_tags_are_rejected_as_data(self):
        for tag in ("", "prod:2", "prod\n2", "$(touch executed)", "a" * 129):
            with self.subTest(tag=tag):
                result = self.execute(IMAGE_TAG=tag)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid container image tag", result.stderr)
                self.assertFalse((self.checkout / "executed").exists())
                self.assertEqual(self.git("status", "--porcelain"), "")

    def test_numeric_tag_remains_a_string(self):
        result = self.execute(IMAGE_TAG="123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_manifest()[0]["value"][0]["value"], "123")

    def test_rebases_over_an_unrelated_manifest_commit(self):
        other_checkout = self.root / "other"
        self.git("clone", str(self.remote), str(other_checkout), cwd=self.root)
        self.git("config", "user.name", "Other Runner", cwd=other_checkout)
        self.git("config", "user.email", "other@example.invalid", cwd=other_checkout)
        (other_checkout / "other.yaml").write_text("image: prod-3\n")
        self.git("add", ".", cwd=other_checkout)
        self.git("commit", "-m", "Update other service", cwd=other_checkout)
        self.git("push", "origin", "main", cwd=other_checkout)
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_manifest()[0]["value"][0]["value"], "prod-2")
        self.assertEqual(self.git("show", "main:other.yaml", cwd=self.remote), "image: prod-3")

    def test_composite_checkout_configuration(self):
        preflight = self.action["runs"]["steps"][0]
        self.assertEqual(preflight["name"], "Verify manifest repository access")
        self.assertEqual(preflight["env"]["MANIFEST_TOKEN"], "${{ inputs.token }}")
        self.assertEqual(preflight["env"]["MANIFEST_REPOSITORY"], "${{ inputs.repository }}")
        self.assertEqual(preflight["env"]["MANIFEST_BRANCH"], "${{ inputs.branch }}")
        checkout = self.steps["Check out manifest repository"]
        self.assertEqual(checkout["uses"], "actions/checkout@v4")
        self.assertEqual(checkout["with"]["repository"], "${{ inputs.repository }}")
        self.assertEqual(checkout["with"]["ref"], "${{ inputs.branch }}")
        self.assertEqual(checkout["with"]["token"], "${{ inputs.token }}")
        self.assertTrue(self.action["inputs"]["token"]["required"])
        self.assertEqual(self.action["inputs"]["branch"]["default"], "main")
        installer = self.steps["Install yq"]
        self.assertEqual(installer["uses"], "dcarbone/install-yq-action@v1.1.1")
        self.assertEqual(installer["with"]["version"], "v4.52.2")
        self.assertEqual(installer["with"]["force"], "true")

    def test_repository_access_preflight(self):
        script = self.steps["Verify manifest repository access"]["run"]
        mock_curl = 'curl() { printf "%s" "$MOCK_HTTP_CODE"; return "$MOCK_CURL_EXIT"; }\n'
        cases = (
            ("fake-token", "200", "0", True, "Token can read"),
            ("", "200", "0", False, "token is empty"),
            ("fake-token", "401", "0", False, "GitHub rejected the token"),
            ("fake-token", "403", "0", False, "organization PAT approval"),
            ("fake-token", "404", "0", False, "Contents permission"),
            ("fake-token", "500", "0", False, "returned HTTP 500"),
            ("fake-token", "000", "6", False, "runner DNS, proxy, TLS"),
        )
        for token, http_code, curl_exit, success, message in cases:
            with self.subTest(http_code=http_code, curl_exit=curl_exit, empty_token=not token):
                result = subprocess.run(
                    ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", mock_curl + script],
                    cwd=self.checkout,
                    env={
                        **self.env, "MANIFEST_TOKEN": token,
                        "MANIFEST_REPOSITORY": "example/manifests", "MANIFEST_BRANCH": "main",
                        "MOCK_HTTP_CODE": http_code, "MOCK_CURL_EXIT": curl_exit,
                    },
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode == 0, success, result.stderr)
                self.assertIn(message, result.stdout + result.stderr)
                self.assertNotIn("fake-token", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()