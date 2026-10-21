"""Regression checks for failures found in the comprehensive review."""
import copy
import gzip
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from analysis.reference import analyze, save_snapshot
from backend.app import NotReady, Snapshot, dispatch
from scripts.verify_results import load_reference
from scripts import cloud
from scripts.evidence import sources_match

ROOT = Path(__file__).resolve().parents[1]


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.input = ROOT / "tests/fixtures/edges.txt"
        cls.content = analyze(cls.input)
        cls.snapshot = Snapshot(cls.content)

    def test_published_spark_snapshot_cannot_be_an_oracle(self):
        content = copy.deepcopy(self.content)
        content["provenance"]["engine"] = "Apache Spark 3.5.2 (local[2])"
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory) / "current.json.gz"
            save_snapshot(content, cached)
            with self.assertRaisesRegex(RuntimeError, "independent SQLite"):
                load_reference(self.input, cached)

    def test_cached_oracle_is_bound_to_current_input_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory) / "reference.json.gz"
            save_snapshot(self.content, cached)
            changed = Path(directory) / "changed.txt"
            changed.write_bytes(self.input.read_bytes() + b"\n")
            with self.assertRaisesRegex(RuntimeError, "current input"):
                load_reference(changed, cached)

    def test_reference_generation_preserves_published_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            current, reference = (Path(directory) / name for name in ("current.json.gz", "reference.json.gz"))
            save_snapshot(self.content, current)
            before = current.read_bytes()
            empty = Path(directory) / "empty.txt"
            empty.touch()
            save_snapshot(analyze(empty), reference)
            self.assertEqual(current.read_bytes(), before)
            self.assertEqual(json.loads(gzip.decompress(current.read_bytes()))["summary"]["total_users"], 7)
            self.assertEqual(json.loads(gzip.decompress(reference.read_bytes()))["summary"]["total_users"], 0)
            self.assertEqual(json.loads(gzip.decompress(reference.read_bytes()))["users"], [])

    def test_snapshot_rejects_malformed_identity_and_types(self):
        for mutate in (lambda c: c.update(schema_version=True),
                       lambda c: c.update(provenance={}),
                       lambda c: c["summary"].update(input_sha256="old"),
                       lambda c: c["users"][0].update(user_id=1),
                       lambda c: c["users"][0].update(followers=True),
                       lambda c: c["users"].append(c["users"][0])):
            content = copy.deepcopy(self.content)
            mutate(content)
            with self.assertRaises(ValueError):
                Snapshot(content)

    def test_snapshot_rejects_impossible_simple_graph_even_if_sums_match(self):
        content = copy.deepcopy(self.content)
        content["users"] = [{"user_id": "1", "followers": 1, "followees": 1}]
        content["summary"].update(raw_lines=1, valid_rows=1, blank_rows=0, invalid_rows=0,
            self_loop_rows=0, duplicate_edges=0, unique_edges=1, total_users=1, followers_sum=1, followees_sum=1)
        with self.assertRaisesRegex(ValueError, "impossible degree"):
            Snapshot(content)

    def test_changed_source_invalidates_previous_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.py"
            path.write_text("original")
            from scripts.evidence import sha256
            record = {"source_sha256": {"source.py": sha256(path)}}
            with patch("scripts.evidence.ROOT", Path(directory)):
                self.assertTrue(sources_match(record))
                path.write_text("changed")
                self.assertFalse(sources_match(record))

    def test_version_pin_prevents_mixed_result_pages(self):
        result = dispatch("GET", "/api/users", {"snapshot_id": "old"}, self.snapshot)
        self.assertEqual(result["statusCode"], 412)
        result = dispatch("GET", "/api/users", {"snapshot_id": self.snapshot.snapshot_id}, self.snapshot)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(json.loads(result["body"])["snapshot_id"], self.snapshot.snapshot_id)
        same_graph = copy.deepcopy(self.content)
        same_graph["provenance"]["engine"] = "Another verified execution"
        self.assertEqual(Snapshot(same_graph).snapshot_id, self.snapshot.snapshot_id)

    def test_unavailable_results_and_bad_query_types_are_controlled(self):
        with patch("backend.app.load_snapshot", side_effect=NotReady("not ready")):
            self.assertEqual(dispatch("GET", "/api/summary")["statusCode"], 503)
        self.assertEqual(dispatch("GET", "/api/users", {"ids": None}, self.snapshot)["statusCode"], 400)
        for query in ([], "", False, {1: "value"}):
            self.assertEqual(dispatch("GET", "/api/users", query, self.snapshot)["statusCode"], 400)
        self.assertEqual(dispatch("POST", "/api/users")["headers"]["allow"], "GET")

    def test_cloud_controller_rejects_region_change_without_connecting(self):
        with self.assertRaisesRegex(ValueError, "region"):
            cloud.session(SimpleNamespace(region="wrong", profile=None), {"region": "ap-southeast-1"})

    def test_cloud_controller_does_not_submit_duplicate_pending_job(self):
        from unittest.mock import Mock
        sdk = Mock()
        sdk.client.return_value.describe_cluster.return_value = {"Cluster": {"Status": {"State": "RUNNING"}}}
        state = {"outputs": {"EmrSubnetId": "subnet-test"}, "last_run": {"cluster_id": "j-test"}}
        with self.assertRaisesRegex(RuntimeError, "already pending"):
            cloud.run(sdk, state)
        sdk.client.return_value.run_job_flow.assert_not_called()

    def classic_state(self):
        from datetime import datetime, timezone
        state = {"outputs": {"DataBucket": "lab-test", "EmrRelease": "emr-7.5.0",
                 "EmrServiceRoleArn": "arn:aws:iam::123456789012:role/emr-test",
                 "SparkInstanceProfile": "spark-test", "EmrSubnetId": "subnet-test",
                 "EmrPrimaryGroupId": "sg-primary", "EmrCoreGroupId": "sg-core"},
                 "script_key": f"scripts/{cloud.sha256(ROOT / 'analysis/spark_job.py')}/spark_job.py", "input_key": "raw/test/input.txt",
                 "api_source_sha256": cloud.sha256(ROOT / "backend/app.py"),
                 "web_source_sha256": cloud.sha256(ROOT / "backend/web.py"),
                 "input_sha256": "0" * 64, "input_bytes": 10}
        state["last_run"] = {"name": "twitter-test", "run_id": "test", "prefix": "results/runs/test",
                             "submitted_at": datetime.now(timezone.utc).isoformat()}
        state["last_run"]["step_args"] = cloud.spark_arguments(state, "results/runs/test")
        return state

    def test_emr_request_is_valid_and_fixed_to_two_nodes_with_termination(self):
        from botocore.session import get_session
        from botocore.validate import validate_parameters
        state = self.classic_state()
        request = cloud.cluster_request(state, state["last_run"])
        shape = get_session().get_service_model("emr").operation_model("RunJobFlow").input_shape
        validate_parameters(request, shape)
        self.assertEqual(sum(group["InstanceCount"] for group in request["Instances"]["InstanceGroups"]), 2)
        self.assertFalse(request["Instances"]["KeepJobFlowAliveWhenNoSteps"])
        self.assertFalse(request["Instances"]["TerminationProtected"])
        self.assertEqual(request["Steps"][0]["ActionOnFailure"], "TERMINATE_CLUSTER")
        self.assertIn("yarn", request["Steps"][0]["HadoopJarStep"]["Args"])

    def test_uncertain_cluster_submission_is_saved_before_network_call(self):
        from unittest.mock import Mock
        sdk = Mock()
        sdk.client.return_value.run_job_flow.side_effect = TimeoutError("response lost")
        state = self.classic_state()
        state.pop("last_run")
        with tempfile.TemporaryDirectory() as directory, patch("scripts.cloud.STATE", Path(directory) / "state.json"), \
                patch("scripts.cloud.independent_reference", return_value=self.snapshot):
            with self.assertRaises(TimeoutError):
                cloud.run(sdk, state)
            saved = json.loads((Path(directory) / "state.json").read_text())
        self.assertEqual(saved["last_run"]["submission_state"], "SUBMITTING")
        self.assertNotIn("cluster_id", saved["last_run"])
        self.assertEqual(sdk.client.return_value.run_job_flow.call_count, 1)

    def test_publish_recovers_an_uncertain_submission_by_unique_name(self):
        from unittest.mock import Mock
        sdk = Mock()
        sdk.client.return_value.get_paginator.return_value.paginate.return_value = [{"Clusters": [
            {"Name": "other", "Id": "j-other"}, {"Name": "twitter-test", "Id": "j-recovered"}]}]
        state = self.classic_state()
        with tempfile.TemporaryDirectory() as directory, patch("scripts.cloud.STATE", Path(directory) / "state.json"):
            self.assertEqual(cloud.recover_cluster(sdk, state), "j-recovered")
        sdk.client.return_value.run_job_flow.assert_not_called()

    def test_failed_step_does_not_publish_a_snapshot(self):
        from unittest.mock import Mock
        sdk = Mock()
        emr = sdk.client.return_value
        emr.describe_cluster.return_value = {"Cluster": {"Status": {"State": "TERMINATING"}}}
        emr.describe_step.return_value = {"Step": {"Status": {"State": "FAILED"}}}
        state = self.classic_state()
        state["last_run"].update(cluster_id="j-test", step_id="s-test")
        with self.assertRaisesRegex(RuntimeError, "Spark step FAILED"):
            cloud.wait_and_publish(sdk, state)
        self.assertEqual([call.args[0] for call in sdk.client.call_args_list], ["emr"])
        emr.put_object.assert_not_called()

    def test_startup_time_limit_requests_cluster_termination(self):
        from unittest.mock import Mock
        sdk = Mock()
        emr = sdk.client.return_value
        emr.describe_cluster.return_value = {"Cluster": {"Status": {"State": "STARTING"}}}
        emr.describe_step.return_value = {"Step": {"Status": {"State": "PENDING"}}}
        state = self.classic_state()
        state["last_run"].update(cluster_id="j-test", step_id="s-test", submitted_at="2000-01-01T00:00:00+00:00")
        with self.assertRaisesRegex(RuntimeError, "termination requested"):
            cloud.wait_and_publish(sdk, state)
        emr.terminate_job_flows.assert_called_once_with(JobFlowIds=["j-test"])

    def test_completed_step_publishes_real_cluster_and_step_provenance(self):
        from io import BytesIO
        from unittest.mock import Mock
        from scripts.evidence import sha256
        sdk, emr, s3 = Mock(), Mock(), Mock()
        sdk.region_name = "ap-southeast-1"
        sdk.client.side_effect = {"emr": emr, "s3": s3}.__getitem__
        state = self.classic_state()
        state["outputs"]["WebsiteUrl"] = "https://example.invalid"
        run = state["last_run"]
        run.update(cluster_id="j-test", step_id="s-test", release_label="emr-7.5.0",
                   input_sha256=self.content["summary"]["input_sha256"], input_bytes=self.content["summary"]["input_bytes"],
                   input_key=state["input_key"], script_key=f"scripts/{sha256(ROOT / 'analysis/spark_job.py')}/spark_job.py")
        run["step_args"] = cloud.spark_arguments({"outputs": state["outputs"], "input_key": run["input_key"],
                                                 "script_key": run["script_key"]}, run["prefix"])
        emr.describe_cluster.return_value = {"Cluster": {"Id": "j-test", "ReleaseLabel": "emr-7.5.0", "Status": {"State": "TERMINATED"}}}
        completed_at = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
        emr.describe_step.return_value = {"Step": {"Id": "s-test", "Config": {"Args": run["step_args"]},
                                                  "Status": {"State": "COMPLETED", "Timeline": {"EndDateTime": completed_at}}}}
        s3.get_paginator.return_value.paginate.side_effect = lambda **kw: [{"Contents": [{"Key": kw["Prefix"] + "part-000.json"}]}]
        from backend.app import AUDIT_FIELDS
        metrics = {key: self.content["summary"][key] for key in AUDIT_FIELDS}
        wrong_results = False
        def get_part(**kw):
            rows = self.content["users"] if "/users/" in kw["Key"] else [metrics]
            if wrong_results and "/users/" in kw["Key"]:
                rows = [{**row, "followers": row["followees"], "followees": row["followers"]} for row in rows]
            return {"Body": BytesIO("\n".join(json.dumps(row) for row in rows).encode())}
        s3.get_object.side_effect = get_part
        with tempfile.TemporaryDirectory() as directory, patch("scripts.cloud.ROOT", Path(directory)):
            script = Path(directory) / "analysis/spark_job.py"
            script.parent.mkdir()
            script.write_bytes((ROOT / "analysis/spark_job.py").read_bytes())
            backend = Path(directory) / "backend/app.py"
            backend.parent.mkdir()
            backend.write_bytes((ROOT / "backend/app.py").read_bytes())
            backend.with_name("web.py").write_bytes((ROOT / "backend/web.py").read_bytes())
            save_snapshot(self.content, Path(directory) / "artifacts/reference.json.gz")
            cloud.wait_and_publish(sdk, state)
            published_path = Path(directory) / "artifacts/current.json.gz"
            first_publication = published_path.read_bytes()
            published = json.loads(gzip.decompress(first_publication))
            evidence = json.loads((Path(directory) / "artifacts/evidence/cloud-job.json").read_text())
            self.assertEqual(Snapshot(published).snapshot_id, self.snapshot.snapshot_id)
            self.assertEqual(published["provenance"]["cluster_id"], "j-test")
            self.assertEqual(evidence["step"]["Status"]["State"], "COMPLETED")
            self.assertEqual(evidence["cluster"]["Id"], "j-test")
            self.assertEqual(published["provenance"]["generated_at"], completed_at.isoformat())
            cloud.wait_and_publish(sdk, state)
            self.assertEqual(published_path.read_bytes(), first_publication)
            wrong_results = True
            with self.assertRaisesRegex(RuntimeError, "independent SQLite"):
                cloud.wait_and_publish(sdk, state)
            self.assertEqual(published_path.read_bytes(), first_publication)
        self.assertEqual(s3.head_object.call_count, 6)
        self.assertEqual(s3.put_object.call_count, 2)

    def test_manual_screenshots_are_bound_to_reviewed_run_and_file_bytes(self):
        from scripts import build_report
        from scripts.evidence import sha256
        content = copy.deepcopy(self.content)
        content["provenance"].update(environment="cloud", cluster_id="j-test", job_run_id="s-test",
            region="ap-southeast-1", input_uri="s3://test/input", output_uri="s3://test/output")
        snapshot = Snapshot(content)
        with (tempfile.TemporaryDirectory() as directory,
              patch("scripts.build_report.EVIDENCE", Path(directory)),
              patch("scripts.build_report.ASSETS", Path(directory))):
            image = Path(directory) / "aws-emr-success.png"
            image.write_bytes(b"original screenshot bytes")
            (Path(directory) / "browser-verification.json").write_text(json.dumps({"url": "https://example.invalid"}))
            record = {"review_status": "REVIEWED", "snapshot_id": snapshot.snapshot_id,
                      "provenance": snapshot.provenance, "url": "https://example.invalid",
                      "source_sha256": {"analysis/spark_job.py": sha256(ROOT / "analysis/spark_job.py")},
                      "files": {image.name: sha256(image)}}
            path = Path(directory) / "manual-captures.json"
            path.write_text(json.dumps(record))
            self.assertTrue(build_report.manual_capture_matches(image.name, snapshot))
            record["provenance"] = {**snapshot.provenance, "job_run_id": "s-another-run"}
            path.write_text(json.dumps(record))
            self.assertFalse(build_report.manual_capture_matches(image.name, snapshot))
            record["provenance"] = snapshot.provenance
            record["review_status"] = "PENDING"
            path.write_text(json.dumps(record))
            self.assertFalse(build_report.manual_capture_matches(image.name, snapshot))
            record["review_status"] = "REVIEWED"
            path.write_text(json.dumps(record))
            image.write_bytes(b"changed screenshot bytes")
            self.assertFalse(build_report.manual_capture_matches(image.name, snapshot))

    def test_browser_evidence_uses_reviewed_images_without_duplicate_captures(self):
        from scripts import build_report
        from scripts.evidence import sha256
        content = copy.deepcopy(self.content)
        content["provenance"].update(environment="cloud", cluster_id="j-test", job_run_id="s-test",
            region="ap-southeast-1", input_uri="s3://test/input", output_uri="s3://test/output")
        snapshot = Snapshot(content)
        with (tempfile.TemporaryDirectory() as directory,
              patch("scripts.build_report.EVIDENCE", Path(directory)),
              patch("scripts.build_report.ASSETS", Path(directory))):
            files = {}
            for name in ("manual-frontend.png", "manual-api.png"):
                image = Path(directory) / name
                image.write_bytes(name.encode())
                files[name] = sha256(image)
            identity = {"snapshot_id": snapshot.snapshot_id, "provenance": snapshot.provenance,
                        "url": "https://example.invalid",
                        "source_sha256": {"analysis/spark_job.py": sha256(ROOT / "analysis/spark_job.py")}}
            (Path(directory) / "manual-captures.json").write_text(json.dumps(
                {**identity, "review_status": "REVIEWED", "files": files}))
            browser = {**identity, "status": "PASS", "captures": {
                "images": {"frontend": "manual-frontend.png", "api": "manual-api.png"}, "files": files}}
            path = Path(directory) / "browser-verification.json"
            path.write_text(json.dumps(browser))
            self.assertTrue(build_report.captures_match(snapshot))
            browser["captures"]["images"] = {}
            path.write_text(json.dumps(browser))
            self.assertFalse(build_report.captures_match(snapshot))
            browser["captures"]["images"] = {"frontend": "manual-frontend.png", "api": "manual-api.png"}
            path.write_text(json.dumps(browser))
            (Path(directory) / "manual-api.png").write_bytes(b"replaced image")
            self.assertFalse(build_report.captures_match(snapshot))

    def test_cloud_report_rejects_wrong_cluster_and_failed_step(self):
        from scripts import build_report
        content = copy.deepcopy(self.content)
        content["provenance"].update(environment="cloud", cluster_id="j-test", job_run_id="s-test",
            region="ap-southeast-1", input_uri="s3://test/input", output_uri="s3://test/output")
        with (tempfile.TemporaryDirectory() as directory,
              patch("scripts.build_report.EVIDENCE", Path(directory)),
              patch("scripts.build_report.ASSETS", Path(directory))):
            job = {"step": {"Id": "s-test", "Status": {"State": "COMPLETED"}}, "cluster": {"Id": "j-test"}}
            path = Path(directory) / "cloud-job.json"
            path.write_text(json.dumps(job))
            missing = build_report.cloud_status(Snapshot(content))
            self.assertNotIn("Matching COMPLETED step and step ID", missing)
            self.assertNotIn("Matching cluster ID", missing)
            job["step"]["Status"]["State"] = "FAILED"
            job["cluster"]["Id"] = "j-other"
            path.write_text(json.dumps(job))
            missing = build_report.cloud_status(Snapshot(content))
            self.assertIn("Matching COMPLETED step and step ID", missing)
            self.assertIn("Matching cluster ID", missing)

    def test_stack_diagnostics_identify_root_failure_in_latest_operation(self):
        from unittest.mock import Mock
        cfn = Mock()
        cfn.get_paginator.return_value.paginate.return_value = [
            {"StackEvents": [
                {"ResourceType": "AWS::CloudFormation::Stack", "LogicalResourceId": "lab2", "ResourceStatus": "ROLLBACK_IN_PROGRESS"},
                {"ResourceType": "AWS::S3::Bucket", "LogicalResourceId": "Bucket", "ResourceStatus": "CREATE_FAILED", "ResourceStatusReason": "Resource creation cancelled"},
                {"ResourceType": "AWS::EMRServerless::Application", "LogicalResourceId": "Spark", "ResourceStatus": "CREATE_FAILED", "ResourceStatusReason": "Access denied"},
            ]},
            {"StackEvents": [
                {"ResourceType": "AWS::CloudFormation::Stack", "LogicalResourceId": "lab2", "ResourceStatus": "CREATE_IN_PROGRESS"},
                {"ResourceType": "AWS::S3::Bucket", "LogicalResourceId": "OldFailure", "ResourceStatus": "CREATE_FAILED", "ResourceStatusReason": "Historical failure"},
            ]},
        ]
        failures = cloud.stack_failures(cfn, "lab2")
        self.assertEqual([item["logical_id"] for item in failures], ["Spark"])
        self.assertEqual(failures[0]["reason"], "Access denied")

    def test_event_lookup_failure_preserves_original_stack_status(self):
        from unittest.mock import Mock
        cfn = Mock()
        cfn.get_paginator.side_effect = RuntimeError("events unavailable")
        with tempfile.TemporaryDirectory() as directory, patch("scripts.cloud.ROOT", Path(directory)):
            error = cloud.failed_deployment(cfn, "lab2", "ROLLBACK_COMPLETE", "ap-southeast-1")
            record = json.loads((Path(directory) / "artifacts/evidence/cloud-deployment-failure.json").read_text())
        self.assertIn("ROLLBACK_COMPLETE", str(error))
        self.assertIn("events unavailable", str(error))
        self.assertEqual(record["stack_status"], "ROLLBACK_COMPLETE")

    def test_deploy_does_not_update_a_rolled_back_stack(self):
        from unittest.mock import Mock
        sdk, cfn, sts = Mock(), Mock(), Mock()
        sdk.region_name = "ap-southeast-1"
        sdk.client.side_effect = {"cloudformation": cfn, "sts": sts}.__getitem__
        cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "ROLLBACK_COMPLETE"}]}
        cfn.get_paginator.return_value.paginate.return_value = []
        args = SimpleNamespace(stack="lab2")
        with tempfile.TemporaryDirectory() as directory, patch("scripts.cloud.ROOT", Path(directory)), \
                patch("scripts.cloud.session", return_value=sdk), patch("scripts.cloud.template", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "cannot be updated"):
                cloud.deploy(args)
        cfn.create_stack.assert_not_called()
        cfn.update_stack.assert_not_called()

    def test_deployment_waits_for_its_own_operation_instead_of_old_completion(self):
        from unittest.mock import Mock
        sdk, cfn = Mock(), Mock()
        sdk.region_name = "ap-southeast-1"
        sdk.client.return_value = cfn
        cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "UPDATE_COMPLETE", "Outputs": []}]}
        root = {"ResourceType": "AWS::CloudFormation::Stack", "LogicalResourceId": "lab2"}
        cfn.get_paginator.return_value.paginate.side_effect = [
            [{"StackEvents": [{**root, "ClientRequestToken": "old", "ResourceStatus": "UPDATE_COMPLETE"}]}],
            [{"StackEvents": [{**root, "ClientRequestToken": "current", "ResourceStatus": "UPDATE_COMPLETE"}]}]]
        args = SimpleNamespace(stack="lab2", profile=None, input="unused")
        with tempfile.TemporaryDirectory() as directory, patch("scripts.cloud.STATE", Path(directory) / "state.json"), \
                patch("scripts.cloud.session", return_value=sdk), patch("scripts.cloud.template", return_value={}), \
                patch("scripts.cloud.uuid.uuid4", return_value=SimpleNamespace(hex="current")), \
                patch("scripts.cloud.upload") as upload, patch("scripts.cloud.time.sleep") as sleep:
            cloud.deploy(args)
        self.assertEqual(cfn.update_stack.call_args.kwargs["ClientRequestToken"], "current")
        sleep.assert_called_once_with(15)
        upload.assert_called_once()

    def test_emr_http_400_internal_error_keeps_submission_uncertain(self):
        from unittest.mock import Mock
        from botocore.exceptions import ClientError
        sdk = Mock()
        sdk.client.return_value.run_job_flow.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "response uncertain"},
             "ResponseMetadata": {"HTTPStatusCode": 400}}, "RunJobFlow")
        state = self.classic_state()
        state.pop("last_run")
        with tempfile.TemporaryDirectory() as directory, patch("scripts.cloud.STATE", Path(directory) / "state.json"), \
                patch("scripts.cloud.independent_reference", return_value=self.snapshot):
            with self.assertRaises(ClientError):
                cloud.run(sdk, state)
        self.assertEqual(state["last_run"]["submission_state"], "SUBMITTING")

    def test_invalid_cluster_configuration_does_not_record_submission_intent(self):
        from unittest.mock import Mock
        state = self.classic_state()
        state.pop("last_run")
        del state["outputs"]["EmrCoreGroupId"]
        sdk = Mock()
        with patch("scripts.cloud.independent_reference", return_value=self.snapshot), patch("scripts.cloud.save_state") as save:
            with self.assertRaises(KeyError):
                cloud.run(sdk, state)
        self.assertNotIn("last_run", state)
        save.assert_not_called()
        sdk.client.return_value.run_job_flow.assert_not_called()

    def test_old_uploaded_script_is_rejected_before_paid_submission(self):
        from unittest.mock import Mock
        state = self.classic_state()
        state.pop("last_run")
        state["script_key"] = "scripts/old/spark_job.py"
        sdk = Mock()
        with self.assertRaisesRegex(RuntimeError, "changed after upload"):
            cloud.run(sdk, state)
        sdk.client.return_value.run_job_flow.assert_not_called()

    def test_evidence_rejects_malformed_hashes_and_incomplete_source_inventory(self):
        from scripts.evidence import source_hashes
        for record in (None, [], {"source_sha256": []}, {"source_sha256": {"../README.md": "0" * 64}},
                       {"source_sha256": {"README.md": None}}):
            self.assertFalse(sources_match(record))
        record = {"source_sha256": source_hashes(["backend/app.py"])}
        self.assertTrue(sources_match(record, ["backend/app.py"]))
        self.assertFalse(sources_match(record, ["backend/app.py", "analysis/spark_job.py"]))


if __name__ == "__main__":
    unittest.main()
