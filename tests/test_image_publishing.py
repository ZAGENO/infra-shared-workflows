"""Offline composite-action tests. Run with python -m unittest discover -s tests -v.

Requires PyYAML. External actions and Docker context creation are recorded, not run;
the actual validation/tag-generation Bash steps are executed.
"""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ("build-and-push-image-aws", "build-and-push-image")
DEV_ONLY = "${{ inputs.environment == 'dev' }}"
ECR = "ecr.example.invalid/team/service"
GAR = "gar.example.invalid/example-project/example-repository/team/service"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
SECONDARY = {
    ACTIONS[0]: (
        "gar_host", "gar_project", "gar_repository",
        "gcp_workload_identity_provider", "gcp_service_account", "image_name",
    ),
    ACTIONS[1]: (
        "aws_region", "aws_access_key_id", "aws_secret_access_key", "image_name",
    ),
}


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate YAML mapping keys")
        return super().construct_mapping(node, deep=deep)


def load_action(name):
    return yaml.load((ROOT / name / "action.yaml").read_text(), Loader=UniqueKeyLoader)


def inputs_for(action, environment, image_tag=""):
    inputs = {
        name: str(spec.get("default", "")) for name, spec in action["inputs"].items()
    }
    inputs.update(
        environment=environment, image_tag=image_tag, image_name="team/service",
        ecr_repository="unused-legacy-repository", tags="unused-legacy-tags",
        registry_url="unused-legacy-url", gar_host="gar.example.invalid",
        gar_project="example-project", gar_repository="example-repository",
        gcp_workload_identity_provider="example-provider",
        gcp_service_account="example-account", aws_access_key_id="dummy-key",
        aws_secret_access_key="dummy-secret",
    )
    return inputs


def execute(action, inputs, head_ref="", ref="refs/heads/main"):
    """Minimal runner for this composite's supported expressions and shell steps."""
    context = {f"inputs.{key}": value for key, value in inputs.items()}
    context.update({"github.sha": "abcdef0123456789", "github.repository": "example/service"})
    calls, outputs = [], {}

    def resolve(value):
        if not isinstance(value, str):
            return value
        # Skipped step outputs evaluate to an empty string in GitHub Actions.
        return EXPRESSION.sub(lambda match: context.get(match[1], ""), value)

    with tempfile.TemporaryDirectory() as directory:
        output_file = Path(directory) / "output"
        env = {
            "PATH": os.environ["PATH"], "GITHUB_SHA": context["github.sha"],
            "GITHUB_RUN_NUMBER": "42", "GITHUB_HEAD_REF": head_ref, "GITHUB_REF": ref,
            "GITHUB_OUTPUT": str(output_file),
        }
        for step in action["runs"]["steps"]:
            if "if" in step:
                if step["if"] != DEV_ONLY:
                    raise AssertionError(f"Unsupported step condition: {step['if']}")
                if inputs["environment"] != "dev":
                    continue
            if "uses" in step:
                calls.append((step["uses"], {
                    key: resolve(value) for key, value in step.get("with", {}).items()
                }))
                if step["uses"] == "aws-actions/amazon-ecr-login@v2":
                    context[f"steps.{step['id']}.outputs.registry"] = "ecr.example.invalid"
                if step["uses"] == "google-github-actions/auth@v2":
                    context[f"steps.{step['id']}.outputs.access_token"] = "dummy-token"
                continue
            if step["run"].strip() == "docker context create builders":
                calls.append(("docker-context", {}))
                continue
            output_file.write_text("")
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                env={**env, **{key: resolve(value) for key, value in step.get("env", {}).items()}},
                capture_output=True, text=True, check=False,
            )
            if result.returncode:
                return calls, outputs, result.stderr
            content = output_file.read_text()
            if content:
                lines = content.splitlines()
                if lines[0] != "tags<<IMAGE_TAGS" or lines[-1] != "IMAGE_TAGS":
                    raise AssertionError(f"Unexpected output format: {content}")
                outputs["tags"] = lines[1:-1]
                context[f"steps.{step['id']}.outputs.tags"] = "\n".join(outputs["tags"])
    return calls, outputs, ""


class ImagePublishingTests(unittest.TestCase):
    def test_environment_and_tag_matrix(self):
        for name in ACTIONS:
            action = load_action(name)
            for environment in ("dev", "staging", "production", "", "preview", "Dev", " dev"):
                for explicit in ("", "release-123"):
                    with self.subTest(action=name, environment=environment, tag=explicit):
                        calls, outputs, error = execute(
                            action, inputs_for(action, environment, explicit),
                        )
                        self.assertEqual(error, "")
                        primary = ECR if name == ACTIONS[0] else GAR
                        secondary = GAR if name == ACTIONS[0] else ECR
                        destinations = [primary, secondary] if environment == "dev" else [primary]
                        suffixes = [explicit] if explicit else (
                            [f"{environment}.abcdef01-42", "latest"] if environment
                            else ["main.abcdef01-42"]
                        )
                        expected = [f"{image}:{tag}" for image in destinations for tag in suffixes]
                        self.assertEqual(outputs["tags"], expected)
                        builds = [options for uses, options in calls
                                  if uses == "docker/build-push-action@v5"]
                        self.assertEqual(len(builds), 1)
                        self.assertEqual(builds[0]["tags"].splitlines(), expected)
                        self.assertEqual(sum(uses == "docker-context" for uses, _ in calls), 1)
                        uses = [uses for uses, _ in calls]
                        self.assertEqual(
                            "google-github-actions/auth@v2" in uses,
                            name == ACTIONS[1] or environment == "dev",
                        )
                        self.assertEqual(
                            "docker/login-action@v3" in uses,
                            name == ACTIONS[1] or environment == "dev",
                        )
                        for aws_action in ("aws-actions/configure-aws-credentials@v3",
                                           "aws-actions/amazon-ecr-login@v2"):
                            self.assertEqual(
                                aws_action in uses, name == ACTIONS[0] or environment == "dev",
                            )

    def test_non_dev_needs_no_secondary_settings(self):
        for name in ACTIONS:
            action = load_action(name)
            for environment in ("staging", "production", "", "preview"):
                with self.subTest(action=name, environment=environment):
                    inputs = inputs_for(action, environment)
                    for key in SECONDARY[name]:
                        if key != "image_name":
                            inputs[key] = ""
                    _, outputs, error = execute(action, inputs, head_ref="dev")
                    self.assertEqual(error, "")
                    self.assertTrue(outputs["tags"])
                    self.assertTrue(all(tag.startswith(ECR if name == ACTIONS[0] else GAR)
                                        for tag in outputs["tags"]))
                    if not environment:
                        self.assertEqual(outputs["tags"][0].split(":")[-1], "dev.abcdef01-42")

    def test_missing_secondary_inputs_fail_before_any_external_step(self):
        for name in ACTIONS:
            action = load_action(name)
            for key in SECONDARY[name]:
                for missing in ("", " \t\n"):
                    for explicit in ("", "release-123"):
                        with self.subTest(action=name, input=key, missing=missing, tag=explicit):
                            inputs = inputs_for(action, "dev", explicit)
                            inputs[key] = missing
                            calls, outputs, error = execute(action, inputs)
                            self.assertEqual(calls, [])
                            self.assertEqual(outputs, {})
                            self.assertEqual(error, f"Missing required dev input: {key}\n")
                            self.assertNotIn("dummy-secret", error)
                            self.assertNotIn("dummy-key", error)
            inputs = inputs_for(action, "dev")
            for key in SECONDARY[name]:
                inputs[key] = ""
            calls, _, error = execute(action, inputs)
            self.assertEqual(calls, [])
            self.assertEqual(error.splitlines(),
                             [f"Missing required dev input: {key}" for key in SECONDARY[name]])

    def test_branch_compatibility(self):
        for name in ACTIONS:
            for head_ref, ref, branch in (
                ("feature", "refs/pull/1/merge", "feature"),
                ("", "refs/heads/feature", "feature"),
                ("", "refs/tags/v1", "refs/tags/v1"),
            ):
                with self.subTest(action=name, branch=branch):
                    _, outputs, error = execute(
                        load_action(name), inputs_for(load_action(name), ""),
                        head_ref=head_ref, ref=ref,
                    )
                    self.assertEqual(error, "")
                    self.assertEqual(outputs["tags"][0].split(":")[-1],
                                     f"{branch}.abcdef01-42")

    def test_untrusted_inputs_are_data_and_outputs_reject_line_breaks(self):
        for name in ACTIONS:
            action = load_action(name)
            with tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / "executed"
                literal = f'$(touch "{marker}")'
                _, outputs, error = execute(action, inputs_for(action, "dev", literal))
                self.assertEqual(error, "")
                self.assertFalse(marker.exists())
                self.assertTrue(all(tag.endswith(":" + literal) for tag in outputs["tags"]))
            for key in ("image_tag", "image_name", "gar_host", "gar_project", "gar_repository"):
                for newline in ("\n", "\r"):
                    with self.subTest(action=name, input=key, newline=newline):
                        inputs = inputs_for(action, "dev")
                        inputs[key] = f"value{newline}IMAGE_TAGS{newline}injected=value"
                        calls, outputs, error = execute(action, inputs)
                        self.assertEqual(error, "Image paths and tags must not contain line breaks\n")
                        self.assertEqual(outputs, {})
                        self.assertNotIn("docker/build-push-action@v5", [use for use, _ in calls])

    def test_composite_structure_and_build_compatibility(self):
        for name in ACTIONS:
            with self.subTest(action=name):
                action = load_action(name)
                self.assertIsInstance(action["name"], str)
                self.assertIsInstance(action["description"], str)
                for spec in action["inputs"].values():
                    self.assertIsInstance(spec["description"], str)
                    self.assertIsInstance(spec["required"], bool)
                self.assertEqual(action["runs"]["using"], "composite")
                steps = action["runs"]["steps"]
                ids = [step["id"] for step in steps if "id" in step]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(steps[0]["name"], "Validate dev secondary inputs")
                self.assertEqual(steps[0]["if"], DEV_ONLY)
                self.assertEqual(action["inputs"]["aws_region"]["default"], "us-east-1")
                secondary_actions = (
                    ("google-github-actions/auth@v2", "docker/login-action@v3")
                    if name == ACTIONS[0] else
                    ("aws-actions/configure-aws-credentials@v3", "aws-actions/amazon-ecr-login@v2")
                )
                for key in SECONDARY[name]:
                    if key != "image_name":
                        self.assertFalse(action["inputs"][key]["required"])
                seen_ids = set()
                for step in steps:
                    self.assertNotIn("continue-on-error", step)
                    self.assertNotIn("${{", step.get("run", ""))
                    if "run" in step:
                        self.assertEqual(step["shell"], "bash")
                    if step.get("uses") in secondary_actions:
                        self.assertEqual(step["if"], DEV_ONLY)
                        self.assertIn("id", step)
                    if step.get("uses") == "google-github-actions/auth@v2":
                        self.assertEqual(step["with"], {
                            "token_format": "access_token", "create_credentials_file": False,
                            "workload_identity_provider": "${{ inputs.gcp_workload_identity_provider }}",
                            "service_account": "${{ inputs.gcp_service_account }}",
                        })
                    if step.get("uses") == "aws-actions/configure-aws-credentials@v3":
                        self.assertEqual(step["with"], {
                            "aws-access-key-id": "${{ inputs.aws_access_key_id }}",
                            "aws-secret-access-key": "${{ inputs.aws_secret_access_key }}",
                            "aws-region": "${{ inputs.aws_region }}",
                        })
                    if step.get("uses") == "docker/login-action@v3":
                        auth_id = "gcp-auth" if name == ACTIONS[0] else "auth"
                        self.assertEqual(step["with"], {
                            "registry": "${{ inputs.gar_host }}", "username": "oauth2accesstoken",
                            "password": "${{ steps." + auth_id + ".outputs.access_token }}",
                        })
                    for expression in EXPRESSION.findall(str(step)):
                        for input_name in re.findall(r"inputs\.([a-z_]+)", expression):
                            self.assertIn(input_name, action["inputs"])
                        for step_id in re.findall(r"steps\.([\w-]+)\.outputs", expression):
                            self.assertIn(step_id, seen_ids)
                    if "id" in step:
                        seen_ids.add(step["id"])
                builds = [step for step in steps if step.get("uses") == "docker/build-push-action@v5"]
                self.assertEqual(len(builds), 1)
                self.assertNotIn("if", builds[0])
                self.assertEqual(builds[0]["with"], {
                    "context": "${{ inputs.build_context }}", "file": "${{ inputs.dockerfile_path }}",
                    "platforms": "linux/amd64", "provenance": False, "push": True,
                    "build-args": "${{ inputs.build_args }}\n",
                    "tags": "${{ steps.image-tags.outputs.tags }}",
                    "labels": "org.opencontainers.image.revision=${{ github.sha }}\n"
                              "org.opencontainers.image.source=github.com/${{ github.repository }}\n",
                    "cache-from": "type=gha", "cache-to": "type=gha,mode=max",
                })


if __name__ == "__main__":
    unittest.main()
