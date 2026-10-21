"""Deploy, run and publish the actual AWS pipeline. Credentials stay in the AWS SDK."""
import argparse
import gzip
import json
import mimetypes
import re
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STATE = ROOT / "cloud-state.json"
from scripts.evidence import sha256, source_hashes


def template():
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    def intrinsic(loader, tag, node):
        name = tag if tag == "Ref" else "Fn::" + tag
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
            if tag == "GetAtt":
                value = value.split(".", 1)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        else:
            value = loader.construct_mapping(node)
        return {name: value}

    Loader.add_multi_constructor("!", intrinsic)
    result = yaml.load((ROOT / "infra/cloudformation.yaml").read_text(), Loader=Loader)
    for function, source in (("ApiFunction", "backend/app.py"), ("WebFunction", "backend/web.py")):
        result["Resources"][function]["Properties"]["Code"]["ZipFile"] = (ROOT / source).read_text()
    return result


def save_state(state):
    with tempfile.NamedTemporaryFile(mode="w", dir=STATE.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(state, indent=2) + "\n")
    try:
        temporary.replace(STATE)
    finally:
        temporary.unlink(missing_ok=True)


def read_state():
    if not STATE.exists():
        raise RuntimeError("Run deploy first; cloud-state.json does not exist.")
    return json.loads(STATE.read_text())


def session(args, state=None):
    if state and args.region and args.region != state["region"]:
        raise ValueError("Requested region differs from the deployed stack's recorded region")
    import boto3
    return boto3.Session(profile_name=args.profile or (state or {}).get("profile"),
                         region_name=args.region or (state or {}).get("region") or "ap-southeast-1")


def stack_failures(cfn, stack_name):
    """Read true resource failures from the latest create/update operation."""
    failures = []
    for page in cfn.get_paginator("describe_stack_events").paginate(StackName=stack_name):
        for event in page["StackEvents"]:
            status = event.get("ResourceStatus", "")
            if event.get("ResourceType") == "AWS::CloudFormation::Stack" and event.get("LogicalResourceId") == stack_name:
                if status in ("CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS"):
                    return list(reversed(failures))
                continue
            reason = event.get("ResourceStatusReason", "")
            if status.endswith("FAILED") and reason.casefold() not in (
                    "resource creation cancelled", "resource update cancelled"):
                failures.append({"logical_id": event["LogicalResourceId"], "type": event["ResourceType"],
                                 "status": status, "reason": reason})
    return list(reversed(failures))


def failed_deployment(cfn, stack_name, status, region):
    record = {"status": "FAILED", "stack": stack_name, "stack_status": status,
              "region": region, "observed_at": datetime.now(timezone.utc).isoformat()}
    try:
        failures = stack_failures(cfn, stack_name)
        record["resource_failures"] = failures
        detail = "\n".join(f"{item['logical_id']} ({item['type']}): {item['reason']}" for item in failures)
    except Exception as error:
        record["event_lookup_error"] = str(error)
        detail = f"Unable to read stack events: {error}"
    destination = ROOT / "artifacts/evidence/cloud-deployment-failure.json"
    try:
        record["retained_resources"] = [
            {"logical_id": item["LogicalResourceId"], "type": item["ResourceType"],
             "physical_id": item["PhysicalResourceId"], "status": item["ResourceStatus"]}
            for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack_name)
            for item in page.get("StackResourceSummaries", [])
            if item["ResourceType"] == "AWS::S3::Bucket" and item.get("PhysicalResourceId")]
    except Exception as error:
        record["resource_lookup_error"] = str(error)
    try:
        previous = json.loads(destination.read_text())
    except (OSError, ValueError):
        previous = {}
    if isinstance(previous, dict) and previous:
        history = previous.pop("previous_failures", [])
        record["previous_failures"] = (history if isinstance(history, list) else []) + [previous]
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(record, indent=2) + "\n")
    except OSError:
        pass  # Reporting failures must not hide the AWS failure itself.
    return RuntimeError(f"Stack failed: {status}. No Spark job was submitted.\n"
                        f"{detail or 'Inspect CloudFormation Events for the resource failure.'}\n"
                        "Resolve the failure before retrying. A ROLLBACK_COMPLETE stack cannot be updated.")


def deploy(args):
    from botocore.exceptions import ClientError
    sdk = session(args)
    cfn = sdk.client("cloudformation")
    # Resolve identity before provisioning any resource.
    sdk.client("sts").get_caller_identity()
    document = template()
    cfn.validate_template(TemplateBody=json.dumps(document))
    token = uuid.uuid4().hex
    options = dict(StackName=args.stack, TemplateBody=json.dumps(document),
                   Capabilities=["CAPABILITY_IAM"], ClientRequestToken=token)
    try:
        existing = cfn.describe_stacks(StackName=args.stack)["Stacks"][0]
    except ClientError as error:
        if "does not exist" not in str(error):
            raise
        cfn.create_stack(**options)
        expected = "CREATE_COMPLETE"
    else:
        status = existing["StackStatus"]
        if status.startswith("ROLLBACK"):
            raise failed_deployment(cfn, args.stack, status, sdk.region_name)
        if status.endswith("_IN_PROGRESS"):
            raise RuntimeError(f"Stack is still {status}. Wait for the current operation to finish before deploying again.")
        try:
            cfn.update_stack(**options)
            expected = "UPDATE_COMPLETE"
        except ClientError as error:
            if "No updates are to be performed" not in str(error):
                raise
            expected = None
    deadline = time.monotonic() + 3600
    while expected:
        # DescribeStacks may briefly return the previous operation's terminal state.
        status = stack_operation_status(cfn, args.stack, token)
        print(f"Stack {args.stack}: {status or 'waiting for operation events'}", flush=True)
        if status == expected:
            break
        if status and ("FAILED" in status or "ROLLBACK" in status):
            raise failed_deployment(cfn, args.stack, status, sdk.region_name)
        if time.monotonic() > deadline:
            raise RuntimeError("Stack wait timed out. Check AWS before retrying; deployment may still be running.")
        time.sleep(15)
    stack = cfn.describe_stacks(StackName=args.stack)["Stacks"][0]
    previous = read_state() if STATE.exists() else {}
    state = {"stack": args.stack, "region": sdk.region_name, "profile": args.profile or previous.get("profile"),
             "compute": "emr-ec2",
             "api_source_sha256": sha256(ROOT / "backend/app.py"),
             "web_source_sha256": sha256(ROOT / "backend/web.py"),
             "outputs": {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}}
    if previous.get("stack") == state["stack"] and previous.get("region") == state["region"] and previous.get("outputs") == state["outputs"]:
        state = {**previous, **state}
    save_state(state)
    upload(sdk, state, args.input)
    print(json.dumps(state["outputs"], indent=2))


def stack_operation_status(cfn, stack_name, token):
    for page in cfn.get_paginator("describe_stack_events").paginate(StackName=stack_name):
        for event in page["StackEvents"]:
            if (event.get("ClientRequestToken") == token
                    and event.get("ResourceType") == "AWS::CloudFormation::Stack"
                    and event.get("LogicalResourceId") == stack_name):
                return event["ResourceStatus"]
    return None


def upload(sdk, state, input_path):
    s3 = sdk.client("s3")
    outputs = state["outputs"]
    path = Path(input_path)
    checksum = sha256(path)
    input_key = f"raw/{checksum}/twitter_combined.txt"
    script_path = ROOT / "analysis/spark_job.py"
    script_hash = sha256(script_path)
    script_key = f"scripts/{script_hash}/spark_job.py"
    s3.upload_file(str(path), outputs["DataBucket"], input_key,
                   ExtraArgs={"Metadata": {"sha256": checksum}})
    s3.upload_file(str(script_path), outputs["DataBucket"], script_key)
    for path in sorted((ROOT / "frontend").iterdir()):
        if path.is_file():
            s3.upload_file(str(path), outputs["WebBucket"], path.name,
                           ExtraArgs={"ContentType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                                      "CacheControl": "no-store"})
    state["input_sha256"] = checksum
    state["input_key"] = input_key
    state["script_key"] = script_key
    state["input_bytes"] = Path(input_path).stat().st_size
    save_state(state)


def spark_arguments(state, prefix):
    bucket = state["outputs"]["DataBucket"]
    return ["spark-submit", "--master", "yarn", "--deploy-mode", "cluster",
            "--driver-memory", "1g", "--executor-memory", "2g", "--executor-cores", "1",
            "--num-executors", "2", "--conf", "spark.dynamicAllocation.enabled=false",
            "--conf", "spark.yarn.maxAppAttempts=1",
            f"s3://{bucket}/{state['script_key']}", "--input", f"s3://{bucket}/{state['input_key']}",
            "--output", f"s3://{bucket}/{prefix}", "--partitions", "4"]


def cluster_request(state, run):
    outputs = state["outputs"]
    return {
        "Name": run["name"], "ReleaseLabel": outputs["EmrRelease"],
        "Applications": [{"Name": "Spark"}],
        "ServiceRole": outputs["EmrServiceRoleArn"], "JobFlowRole": outputs["SparkInstanceProfile"],
        "LogUri": f"s3://{outputs['DataBucket']}/logs/", "VisibleToAllUsers": True,
        "EbsRootVolumeSize": 15, "StepConcurrencyLevel": 1,
        "Tags": [{"Key": "for-use-with-amazon-emr-managed-policies", "Value": "true"},
                 {"Key": "Lab2RunId", "Value": run["run_id"]}],
        "Instances": {
            "Ec2SubnetId": outputs["EmrSubnetId"],
            "EmrManagedMasterSecurityGroup": outputs["EmrPrimaryGroupId"],
            "EmrManagedSlaveSecurityGroup": outputs["EmrCoreGroupId"],
            "KeepJobFlowAliveWhenNoSteps": False, "TerminationProtected": False,
            "InstanceGroups": [{"Name": name, "InstanceRole": role, "Market": "ON_DEMAND",
                                "InstanceType": "m5.xlarge", "InstanceCount": 1,
                                "EbsConfiguration": {"EbsBlockDeviceConfigs": [{
                                    "VolumeSpecification": {"VolumeType": "gp3", "SizeInGB": 32},
                                    "VolumesPerInstance": 1}]}}
                               for name, role in (("Primary", "MASTER"), ("Core", "CORE"))]},
        "Steps": [{"Name": run["name"], "ActionOnFailure": "TERMINATE_CLUSTER",
                   "HadoopJarStep": {"Jar": "command-runner.jar", "Args": run["step_args"]}}],
    }


def recover_cluster(sdk, state):
    run = state["last_run"]
    if run.get("cluster_id"):
        return run["cluster_id"]
    emr = sdk.client("emr")
    found = []
    for page in emr.get_paginator("list_clusters").paginate(
            CreatedAfter=datetime.fromisoformat(run["submitted_at"]) - timedelta(minutes=5)):
        found.extend(cluster["Id"] for cluster in page["Clusters"] if cluster["Name"] == run["name"])
    if len(found) != 1:
        raise RuntimeError("Previous submission outcome is unknown. Check EMR Clusters before resubmitting; "
                           "publish can recover a cluster by its recorded unique name.")
    run.update(cluster_id=found[0], submission_state="SUBMITTED")
    save_state(state)
    return found[0]


def independent_reference(record):
    from backend.app import Snapshot
    path = ROOT / "artifacts/reference.json.gz"
    if not path.is_file():
        raise RuntimeError("Independent reference missing; run scripts/verify_results.py before cloud execution")
    reference = Snapshot(json.loads(gzip.decompress(path.read_bytes())))
    if (reference.provenance.get("engine") != "SQLite independent reference"
            or reference.summary["input_sha256"] != record["input_sha256"]
            or reference.summary["input_bytes"] != record["input_bytes"]):
        raise RuntimeError("Independent reference does not match the uploaded input; verify results again")
    return reference


def verify_deployed_sources(state, script_key):
    if script_key != f"scripts/{sha256(ROOT / 'analysis/spark_job.py')}/spark_job.py":
        raise RuntimeError("Spark source changed after upload/submission; upload and run the current version")
    if state.get("api_source_sha256") != sha256(ROOT / "backend/app.py"):
        raise RuntimeError("Backend deployment is missing or stale; deploy the current version before continuing")
    if state.get("web_source_sha256") != sha256(ROOT / "backend/web.py"):
        raise RuntimeError("Website handler deployment is missing or stale; deploy the current version before continuing")


def run(sdk, state):
    from botocore.config import Config
    from botocore.exceptions import ClientError
    from botocore.session import get_session
    from botocore.validate import validate_parameters
    if "EmrSubnetId" not in state["outputs"]:
        raise RuntimeError("Redeploy the EMR on EC2 template before submitting a cluster")
    # RunJobFlow has no idempotency token. Persist intent and disable automatic mutation retries.
    emr = sdk.client("emr", config=Config(retries={"total_max_attempts": 1}))
    previous = state.get("last_run")
    if previous and previous.get("submission_state") != "REJECTED":
        cluster_id = recover_cluster(sdk, state)
        status = emr.describe_cluster(ClusterId=cluster_id)["Cluster"]["Status"]["State"]
        if status not in ("TERMINATED", "TERMINATED_WITH_ERRORS"):
            raise RuntimeError("A Spark cluster is already pending or running; use publish to resume it")
    verify_deployed_sources(state, state["script_key"])
    independent_reference(state)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    prefix = f"results/runs/{run_id}"
    pending = {"run_id": run_id, "name": f"twitter-{run_id}", "prefix": prefix,
                         "submitted_at": datetime.now(timezone.utc).isoformat(), "submission_state": "SUBMITTING",
                         "input_key": state["input_key"], "input_sha256": state["input_sha256"],
                         "input_bytes": state["input_bytes"], "script_key": state["script_key"],
                         "release_label": state["outputs"]["EmrRelease"], "step_args": spark_arguments(state, prefix)}
    request = cluster_request(state, pending)
    shape = get_session().get_service_model("emr").operation_model("RunJobFlow").input_shape
    validate_parameters(request, shape)
    state["last_run"] = pending
    save_state(state)
    try:
        submitted = emr.run_job_flow(**request)
    except ClientError as error:
        # EMR InternalServerError can use HTTP 400; only definite rejections permit retry.
        if error.response.get("Error", {}).get("Code") in (
                "AccessDenied", "AccessDeniedException", "ValidationException",
                "InvalidRequestException", "UnrecognizedClientException", "ExpiredTokenException"):
            state["last_run"].update(submission_state="REJECTED", submission_error=str(error))
            save_state(state)
        raise
    state["last_run"].update(cluster_id=submitted["JobFlowId"], submission_state="SUBMITTED")
    save_state(state)
    wait_and_publish(sdk, state)


def wait_and_publish(sdk, state):
    emr = sdk.client("emr")
    run = state["last_run"]
    cluster_id = recover_cluster(sdk, state)
    while True:
        cluster = emr.describe_cluster(ClusterId=cluster_id)["Cluster"]
        cluster_status = cluster["Status"]["State"]
        if not run.get("step_id"):
            steps = emr.list_steps(ClusterId=cluster_id)["Steps"]
            matches = [step for step in steps if step["Name"] == run["name"]]
            if len(matches) > 1:
                raise RuntimeError("Multiple steps match this run; inspect EMR before publishing")
            if matches:
                run["step_id"] = matches[0]["Id"]
                save_state(state)
        step = emr.describe_step(ClusterId=cluster_id, StepId=run["step_id"])["Step"] if run.get("step_id") else None
        status = step["Status"]["State"] if step else "PENDING"
        print(f"EMR cluster {cluster_id}: {cluster_status}; Spark step: {status}", flush=True)
        if status == "COMPLETED":
            break
        if status in ("FAILED", "CANCELLED", "INTERRUPTED") or cluster_status in ("TERMINATED", "TERMINATED_WITH_ERRORS"):
            detail = step["Status"] if step else cluster["Status"]
            raise RuntimeError(f"Spark step {status}; cluster {cluster_status}: {detail}. Previous published result is preserved.")
        now = datetime.now(timezone.utc)
        started = step["Status"].get("Timeline", {}).get("StartDateTime") if step else None
        if (now - datetime.fromisoformat(run["submitted_at"])).total_seconds() > 1800 or (
                started and (now - started).total_seconds() > 900):
            emr.terminate_job_flows(JobFlowIds=[cluster_id])
            raise RuntimeError("Execution/startup time limit reached; cluster termination requested. Inspect EMR before retrying.")
        time.sleep(15)
    s3 = sdk.client("s3")
    bucket, prefix = state["outputs"]["DataBucket"], run["prefix"]
    expected_arguments = spark_arguments({"outputs": state["outputs"], "script_key": run["script_key"],
                                         "input_key": run["input_key"]}, prefix)
    if run["step_args"] != expected_arguments or step.get("Config", {}).get("Args") != expected_arguments or cluster.get("ReleaseLabel") != run["release_label"]:
        raise RuntimeError("Successful job does not match the recorded input/script/output configuration")
    verify_deployed_sources(state, run["script_key"])
    completed_at = step["Status"].get("Timeline", {}).get("EndDateTime")
    if not isinstance(completed_at, datetime) or completed_at.tzinfo is None:
        raise RuntimeError("Completed step has no AWS completion timestamp yet; retry publish")
    users = read_parts(s3, bucket, prefix + "/users")
    summaries = read_parts(s3, bucket, prefix + "/summary")
    if len(summaries) != 1:
        raise RuntimeError("Expected exactly one summary record")
    summary = summaries[0]
    summary.update(input_sha256=run["input_sha256"], input_bytes=run["input_bytes"])
    snapshot = {"schema_version": 1, "summary": summary, "users": users, "provenance": {
        "environment": "cloud", "engine": "Apache Spark on Amazon EMR (EC2 / YARN)",
        "generated_at": completed_at.astimezone(timezone.utc).isoformat(), "job_run_id": run["step_id"],
        "cluster_id": cluster_id, "input_uri": f"s3://{bucket}/{run['input_key']}",
        "output_uri": f"s3://{bucket}/{prefix}", "region": sdk.region_name}}
    from backend.app import Snapshot
    from analysis.reference import save_snapshot
    validated = Snapshot(snapshot)  # Validate inventory, degrees, and edge conservation before publishing.
    if validated.snapshot_id != independent_reference(run).snapshot_id:
        raise RuntimeError("Cloud results differ from the independent SQLite reference; previous result preserved")
    body = gzip.compress(json.dumps(snapshot, separators=(",", ":")).encode(), mtime=0)
    s3.put_object(Bucket=bucket, Key="results/current.json.gz", Body=body,
                  ContentType="application/gzip", CacheControl="no-store")
    save_snapshot(snapshot, ROOT / "artifacts/current.json.gz")
    evidence = ROOT / "artifacts/evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "cloud-job.json").write_text(json.dumps({"provider": "emr-ec2", "cluster": cluster, "step": step}, indent=2, default=str) + "\n")
    (evidence / "cloud-summary.json").write_text(json.dumps(
        {"summary": summary, "provenance": snapshot["provenance"], "snapshot_id": validated.snapshot_id,
         "source_sha256": source_hashes(["analysis/spark_job.py", "backend/app.py", "backend/web.py", "scripts/cloud.py"])}, indent=2) + "\n")
    print("Published a validated cloud result. Website:", state["outputs"]["WebsiteUrl"])


def read_parts(s3, bucket, prefix):
    s3.head_object(Bucket=bucket, Key=prefix + "/_SUCCESS")
    records = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "/"):
        for item in page.get("Contents", []):
            if Path(item["Key"]).name.startswith("part-") and item["Key"].endswith(".json"):
                content = s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read().decode()
                records.extend(json.loads(line) for line in content.splitlines() if line.strip())
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "deploy", "run", "publish", "upload", "cleanup"])
    parser.add_argument("--region", help="Default ap-southeast-1; resumed commands use recorded region")
    parser.add_argument("--profile", help="An AWS SDK profile; never put access keys on the command line")
    parser.add_argument("--stack", default="cs5224-lab2")
    parser.add_argument("--input", default=str(ROOT / "twitter_combined.txt"))
    parser.add_argument("--execute", action="store_true", help="Provision/run/update billable AWS resources")
    args = parser.parse_args()
    if args.action == "plan":
        path = ROOT / "artifacts/cloudformation.generated.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(template(), indent=2) + "\n")
        print("Generated deployable template:", path)
        print("Region:", args.region or "ap-southeast-1")
        print("No AWS resources were created. Compute uses EMR on EC2; clusters start only with run --execute.")
        print("HTTPS website and API: API Gateway + separate Lambda handlers; frontend stays in private S3.")
        print("Cluster: 1 primary + 1 core, both m5.xlarge on demand; terminate after steps finish.")
        print("While monitored: 15-minute step limit; 30-minute total startup/execution limit.")
        return
    if not args.execute:
        parser.error("AWS changes require --execute. Review README.md before provisioning billable resources.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,39}", args.stack):
        parser.error("Use a stack name of 1 to 40 letters, digits or hyphens, starting with a letter.")
    if args.action == "deploy":
        deploy(args)
    else:
        state = read_state()
        sdk = session(args, state)
        if args.action == "run":
            run(sdk, state)
        elif args.action == "publish":
            if "last_run" not in state:
                raise RuntimeError("No job has been submitted")
            wait_and_publish(sdk, state)
        elif args.action == "upload":
            upload(sdk, state, args.input)
        elif args.action == "cleanup":
            emr = sdk.client("emr")
            if state.get("last_run") and state["last_run"].get("submission_state") != "REJECTED":
                cluster_id = recover_cluster(sdk, state)
                status = emr.describe_cluster(ClusterId=cluster_id)["Cluster"]["Status"]["State"]
            else:
                status = "TERMINATED"
            if status not in ("TERMINATED", "TERMINATED_WITH_ERRORS"):
                emr.terminate_job_flows(JobFlowIds=[cluster_id])
                deadline = time.monotonic() + 600
                while emr.describe_cluster(ClusterId=cluster_id)["Cluster"]["Status"]["State"] not in ("TERMINATED", "TERMINATED_WITH_ERRORS"):
                    if time.monotonic() > deadline:
                        raise RuntimeError("Termination timed out; check EMR before retrying cleanup.")
                    time.sleep(10)
            sdk.client("cloudformation").delete_stack(StackName=state["stack"])
            print("Stack deletion requested. The two S3 buckets are retained; remove them manually after saving evidence.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
