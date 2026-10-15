# This is a PUBLIC REPO. DO NOT add any company proprietary code.

## Container image publishing

These composite actions **build and publish images**; they do not deploy images
to running services:

- `build-and-push-image-aws`: Amazon ECR is the primary destination.
- `build-and-push-image`: Google Artifact Registry (GAR) is the primary destination.

For **exactly `environment: dev`**, either existing action automatically
authenticates to both clouds and builds once with tags for both registries. There
is no opt-in toggle and no second action invocation. An explicit `image_tag`
does not disable dev dual publishing.

Staging, production, an empty environment, and all other values keep publishing
only to the action's primary cloud. Secondary authentication does not run, and
secondary settings/credentials are not required. Branch names and image tags
never select dual publishing, even if they contain `dev`.

### Destinations and tags

Both ECR destinations use `<ECR login registry>/<image_name>`. On the AWS action,
the existing `ecr_repository` input remains accepted but is unused; it does not
change the primary destination. On the GCP action, the secondary ECR repository
is **`image_name`**, including any namespace prefix; no separate ECR repository
input is needed. The ECR login registry comes from the configured AWS account
and region (`aws_region` defaults to `us-east-1`).

Both GAR destinations use
`<gar_host>/<gar_project>/<gar_repository>/<image_name>`. The existing GCP
`registry_url` input and both actions' `tags` inputs remain accepted but unused.
Tag selection remains:

| Inputs                           | Tags on each selected destination                    |
| -------------------------------- | ---------------------------------------------------- |
| Explicit`image_tag`            | That tag only (no automatic`latest`)               |
| Environment set, no explicit tag | `<environment>.<sha8>-<run_number>` and `latest` |
| Neither set                      | `<branch>.<sha8>-<run_number>` only                |

The branch comes from `GITHUB_HEAD_REF` when present, otherwise from
`GITHUB_REF` with `refs/heads/` removed. Existing Docker tag validity restrictions
still apply. Build context, Dockerfile, build arguments, `linux/amd64`,
provenance setting, revision/source labels, and GHA cache behavior are preserved.

Dev validates secondary settings before any authentication, build, or push.
Missing values fail the invocation with input names, never credential values:

- AWS entry point additionally needs `gar_host`, `gar_project`, `gar_repository`,
  `gcp_workload_identity_provider`, and `gcp_service_account`.
- GCP entry point additionally needs `aws_access_key_id`, `aws_secret_access_key`,
  and `aws_region` (its default is sufficient).
- Both need the existing shared `image_name` for the secondary image path.

Authentication or publication failures fail the action. **Cross-registry pushes
are not atomic and do not roll back automatically**: one registry may receive
the image before a failure on the other. Check both destinations before
deploying or retrying.

### Dev caller examples

Use **one** of these steps in a consuming service workflow. The variable/secret
names below are generic placeholders: configure them in the consuming
repository/environment. Replace `<reviewed-commit-sha>` with the reviewed
revision containing this change (or an appropriate updated release ref).
Do not put actual credentials or private infrastructure values in this public
repository.

AWS-primary example:

```yaml
jobs:
  publish-dev:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: ZAGENO/infra-shared-workflows/build-and-push-image-aws@<reviewed-commit-sha>
        with:
          environment: dev
          image_name: ${{ vars.IMAGE_NAME }}
          ecr_repository: ${{ vars.IMAGE_NAME }} # Legacy input; destination uses image_name.
          tags: dev # Legacy input; generated tags follow the table above.
          aws_region: ${{ vars.AWS_REGION }}
          aws_access_key_id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws_secret_access_key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          gar_host: ${{ vars.GAR_HOST }}
          gar_project: ${{ vars.GAR_PROJECT }}
          gar_repository: ${{ vars.GAR_REPOSITORY }}
          gcp_workload_identity_provider: ${{ vars.GCP_WORKLOAD_IDENTITY_PROVIDER }}
          gcp_service_account: ${{ vars.GCP_SERVICE_ACCOUNT }}
```

GCP-primary example:

```yaml
jobs:
  publish-dev:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: ZAGENO/infra-shared-workflows/build-and-push-image@<reviewed-commit-sha>
        with:
          environment: dev
          image_name: ${{ vars.IMAGE_NAME }}
          tags: dev
          gar_host: ${{ vars.GAR_HOST }}
          gar_project: ${{ vars.GAR_PROJECT }}
          gar_repository: ${{ vars.GAR_REPOSITORY }}
          registry_url: ${{ vars.GAR_HOST }} # Legacy input; path uses the GAR inputs above.
          gcp_workload_identity_provider: ${{ vars.GCP_WORKLOAD_IDENTITY_PROVIDER }}
          gcp_service_account: ${{ vars.GCP_SERVICE_ACCOUNT }}
          aws_region: ${{ vars.AWS_REGION }}
          aws_access_key_id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws_secret_access_key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
```

Omit `aws_region` to use `us-east-1`; if explicitly supplied, it must be nonempty
for dev. Optionally add `image_tag` for a single explicit tag in both registries.
The existing `build_context`, `dockerfile_path`, and `build_args` inputs can be
set as before.

GCP OIDC authentication requires `id-token: write` on the calling job and a
workload identity provider that trusts the caller, with permission to
impersonate the supplied service account. `contents: read` permits checkout.
The service account must have Artifact Registry write access. AWS credentials
must permit ECR authentication and image publishing in the selected account and
region. Provision the ECR `image_name` repository and the GAR repository first.
These actions do not provision registries or deploy workloads.

### Adoption and migration

**Existing dev callers without secondary settings must be updated** before
adopting this revision; otherwise they fail early rather than silently publishing
to only one cloud. Update the service workflow's variables/secrets, GCP OIDC
permissions/trust, and cloud repository permissions together with the action ref.
Services pinned to a commit or release do not receive this change until their
`uses:` ref is updated. Services tracking a moving ref need their dev settings
ready before that ref includes this change.

For staging/production, keep the current action and primary inputs and set
`environment: staging` or `environment: production`. No secondary inputs or
additional cloud permissions are needed; GCP-primary callers still need their
existing GCP OIDC permissions. Empty/other environments also remain single-cloud.
Do not invoke both actions just to enable dev dual publishing.
Consuming repositories and actual service deployment workflows are outside this
change's scope.

### Offline validation

With Python 3 and PyYAML available, run from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
```

The tests parse both composite action YAML files, check their structure, execute
their actual Bash validation/tag-generation steps, and mock cloud actions and
Docker setup. They cover dev/generated/explicit tags, non-dev isolation, missing
configuration, preserved paths/build settings, and safe shell/output handling.
They do not authenticate to clouds, build containers, or push images.
