# AWS deployment

The template provisions private S3 storage, a dedicated EMR network, IAM roles, two Lambda functions and an API Gateway HTTP API. It launches compute only when a job is submitted.

## Prerequisites and authentication

Install AWS CLI v2, prepare the [Python environment and independent verification](reproducibility.md), and configure an authenticated AWS SDK profile. The examples use `analytics`; substitute your own configured profile.

For an IAM Identity Center account, use the portal and account assigned by your organisation:

```bash
aws configure sso --profile analytics
aws sso login --profile analytics
aws sts get-caller-identity --profile analytics --region ap-southeast-1
```

The deployment identity needs access to CloudFormation, IAM/PassRole, S3, EMR, EC2/VPC, Lambda, API Gateway and logging. The Spot task worker also requires the AWS-managed `AWSServiceRoleForEC2Spot` service-linked role. Create it once if the account has not used Spot: `aws iam create-service-linked-role --aws-service-name spot.amazonaws.com --profile analytics`. AWS may create it automatically when the launching identity has permission. Credentials are resolved by the AWS SDK; no application `.env` file is required.

## Plan, deploy and run

Complete the local verification first so that `artifacts/reference.json.gz` matches the input. Then:

```bash
.venv/bin/python scripts/cloud.py plan
.venv/bin/cfn-lint artifacts/cloudformation.generated.json
.venv/bin/python scripts/cloud.py deploy \
  --profile analytics --region ap-southeast-1 --execute
.venv/bin/python scripts/cloud.py run --profile analytics --execute
```

`plan` and lint run locally. Deployment uses the generated JSON template, which embeds the current Lambda handlers; the source YAML contains code placeholders. Successful deployment records resource identifiers and the HTTPS `WebsiteUrl` in `cloud-state.json`.

The Spark job uses **EMR 7.5.0 / Spark 3.5.2**, an on-demand primary, an on-demand core worker and a Spot task worker, all **`m5.xlarge`**, and two YARN executors. The two on-demand nodes use 8 vCPUs; the extra worker uses Spot capacity to respect a small on-demand quota. A Spot interruption fails the job and preserves the previous publication. Each executor reserves 8 GiB plus overhead, so both cannot fit on one 16 GiB worker. Input splits are bounded at 8 MiB, and Spark waits for both executors before scheduling. After analysis, the job records live executor hosts and completed-task counters; publication requires completed tasks on at least two distinct worker hosts. The cluster terminates after its step completes or fails. While the controller remains running, it also enforces a 15-minute step limit and a 30-minute overall startup/execution limit.

AWS services are metered. Compute, storage and request charges depend on actual usage; credit coverage depends on the account. See [Amazon EMR pricing](https://aws.amazon.com/emr/pricing/).

Publication requires a matching `COMPLETED` step, success markers for users, summary and execution evidence, consistent audit totals and a full comparison against the independent reference. The controller preserves the previous publication if validation fails.

## Resume or update a deployment

Resume an interrupted submission, wait or publication using the recorded job:

```bash
.venv/bin/python scripts/cloud.py publish --profile analytics --execute
```

Keep `cloud-state.json` for recovery. When submission outcome is uncertain, use `publish` to find the existing cluster before starting another run.

Upload changes to frontend files, input or the Spark script with:

```bash
.venv/bin/python scripts/cloud.py upload --profile analytics --execute
```

Changes to Lambda handlers or infrastructure require `deploy`. Changing the input also requires a new matching independent reference. CloudFormation failure details are recorded in `artifacts/evidence/cloud-deployment-failure.json`; resolve the reported resource failure before retrying a failed deployment.

## Resource cleanup

After retaining the required results and execution evidence:

```bash
.venv/bin/python scripts/cloud.py cleanup --profile analytics --execute
```

Cleanup terminates a recorded active cluster, waits for termination and requests deletion of the CloudFormation stack. Both S3 buckets have a `Retain` policy: they remain after stack deletion and must be emptied and deleted separately when no longer needed. Confirm stack deletion and use the resource names in `cloud-state.json` to identify retained storage. Also inspect storage retained by any earlier failed stacks.
