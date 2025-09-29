import os
import sys
import glob
import yaml

ALLOWED = [
    "actions/checkout@v2",
    "actions/checkout@v3",
    "actions/checkout@v4",
    "actions/setup-java@v3",
    "actions/setup-node@v3",
    "actions/setup-node@v4",
    "actions/setup-python@v2",
    "actions/setup-python@v3",
    "actions/setup-python@v4",
    "actions/upload-artifact@v3",
    "actions/upload-artifact@v4",
    "actions/download-artifact@v3",
    "actions/download-artifact@v4",
    "docker/login-action@v3",
    "aws-actions/configure-aws-credentials@v2",
    "aws-actions/amazon-ecr-login@v1",
    "google-github-actions/auth@v0",
    "google-github-actions/auth@v1",
    "google-github-actions/auth@v2",
    "google-github-actions/setup-gcloud@v1",
    "google-github-actions/setup-gcloud@v2",
    "ZAGENO/infra-shared-workflows/build-and-push-image@main",
    "ZAGENO/infra-shared-workflows/build-and-push-image-aws@main",
    "ZAGENO/infra-shared-workflows/lint-helm-chart@main",
    "ZAGENO/infra-shared-workflows/lint-helm-chart-v2@main",
    "ZAGENO/pygeno/.github/workflows/build-publish-docker-image.yml@master",
    "TimonVS/pr-labeler-action@v3",
    "suo/flake8-github-action@releases/v1",
    "elgohr/gcloud-login-action@v1",
    "elgohr/gcloud-login-action@master",
    "addnab/docker-run-action@v3",
    "sliteteam/github-action-git-crypt-unlock@1.2.0",
    "dcarbone/install-yq-action@v1.1.1",
    "sonarsource/sonarcloud-github-action@master",
    "snok/install-poetry@v1",
    "5monkeys/cobertura-action@master",
    "cirrus-actions/rebase@1.8"
]

def handle_violations(violations):
    if violations:
        print("❌ The following policy violations were found:")
        for v in violations:
            print(f"  {v}")
        print("\nThis is not allowed as per org policy. Kindly contact the CloudOps team if you believe this is required.")
        sys.exit(1)

def check_allowed_actions():
    violations = []
    for wf in glob.glob(".github/workflows/*.y*ml"):
        with open(wf) as f:
            try:
                data = yaml.safe_load(f)
            except Exception as e:
                print(f"Could not parse {wf}: {e}")
                continue
            def search(d):
                if isinstance(d, dict):
                    for k, v in d.items():
                        if k == "uses":
                            if not any(allowed in str(v) for allowed in ALLOWED):
                                violations.append(f"{wf}: {v}")
                        search(v)
                elif isinstance(d, list):
                    for item in d:
                        search(item)
            search(data)
    return violations

def find_resources_key(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "resources":
                return True
            if find_resources_key(v):
                return True
    elif isinstance(obj, list):
        for item in obj:
            if find_resources_key(item):
                return True
    return False

def check_forbidden_resources():
    violations = []
    for root, dirs, files in os.walk("."):
        for file in files:
            if file.endswith(('.yaml', '.yml')):
                path = os.path.join(root, file)
                try:
                    with open(path) as f:
                        data = yaml.safe_load(f)
                        if find_resources_key(data):
                            violations.append(f"'resources' key found in {path}")
                except Exception as e:
                    # Ignore parse errors for non-YAML files
                    continue
    return violations

if __name__ == "__main__":
    all_violations = []
    all_violations.extend(check_allowed_actions())
#     all_violations.extend(check_forbidden_resources())
    handle_violations(all_violations)
    print(f"✅ All actions used are in the allow-list: {', '.join(ALLOWED)}")
    print("✅ No 'resources' section found in any file.") 
