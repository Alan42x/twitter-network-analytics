"""Exercise private-S3 website delivery and its generated deployment wiring."""
import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from backend import web
from scripts import cloud
from scripts.evidence import sha256

ROOT = Path(__file__).resolve().parents[1]


class WebTests(unittest.TestCase):
    def test_s3_assets_use_exact_keys_utf8_and_browser_content_types(self):
        s3 = Mock()
        payload = "Example → 用户".encode("utf-8")
        s3.get_object.side_effect = lambda **kwargs: {"Body": BytesIO(payload)}
        with patch.object(web, "_s3", s3), patch.dict(os.environ, {"WEB_BUCKET": "private-web"}):
            for path, (key, content_type) in web.ASSETS.items():
                result = web.handler({"rawPath": path, "requestContext": {"http": {"method": "GET"}}}, None)
                self.assertEqual(result["statusCode"], 200)
                self.assertEqual(result["body"], payload.decode("utf-8"))
                self.assertEqual(result["headers"]["content-type"], content_type)
                self.assertEqual(result["headers"]["cache-control"], "no-store")
                self.assertFalse(result["isBase64Encoded"])
                s3.get_object.assert_called_with(Bucket="private-web", Key=key)

    def test_unknown_paths_never_read_s3(self):
        reader = Mock()
        for path in ("/../twitter_combined.txt", "/raw/input.txt", "/api/users", "/app.js/other"):
            self.assertEqual(web.dispatch("GET", path, reader)["statusCode"], 404)
        reader.assert_not_called()

    def test_head_reports_utf8_length_without_sending_a_body(self):
        payload = "Hello →".encode("utf-8")
        result = web.dispatch("HEAD", "/", reader=lambda key: payload)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "")
        self.assertEqual(int(result["headers"]["content-length"]), len(payload))

    def test_invalid_methods_and_storage_failures_are_controlled(self):
        reader = Mock(side_effect=PermissionError("private-bucket-detail"))
        rejected = web.dispatch("POST", "/", reader)
        self.assertEqual(rejected["statusCode"], 405)
        self.assertEqual(rejected["headers"]["allow"], "GET, HEAD")
        reader.assert_not_called()
        with self.assertLogs("backend.web", level="ERROR"):
            unavailable = web.dispatch("GET", "/", reader)
        self.assertEqual(unavailable["statusCode"], 503)
        self.assertNotIn("private-bucket-detail", unavailable["body"])

    def test_deployment_routes_private_website_and_api_without_cloudfront(self):
        document = cloud.template()
        resources = document["Resources"]
        self.assertFalse(any(item["Type"].startswith("AWS::CloudFront::") for item in resources.values()))
        self.assertEqual(document["Outputs"]["WebsiteUrl"], document["Outputs"]["ApiUrl"])
        self.assertNotIn("DistributionId", document["Outputs"])
        for function, source in (("ApiFunction", "backend/app.py"), ("WebFunction", "backend/web.py")):
            code = resources[function]["Properties"]["Code"]["ZipFile"]
            self.assertEqual(code, (ROOT / source).read_text())
            compile(code, source, "exec")
        routes = {item["Properties"]["RouteKey"] for item in resources.values()
                  if item["Type"] == "AWS::ApiGatewayV2::Route"}
        self.assertEqual(routes, {"GET /api/{proxy+}", "GET /", "GET /{proxy+}", "HEAD /", "HEAD /{proxy+}"})
        self.assertTrue(all(resources["WebBucket"]["Properties"]["PublicAccessBlockConfiguration"].values()))
        policy = resources["WebRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"][0]
        self.assertEqual(policy["Action"], "s3:GetObject")
        self.assertEqual(len(policy["Resource"]), 3)
        self.assertLessEqual(len(json.dumps(document).encode()), 51200)

    def test_upload_works_without_a_distribution_or_invalidation(self):
        sdk = Mock()
        state = {"outputs": {"DataBucket": "private-data", "WebBucket": "private-web"}}
        with tempfile.TemporaryDirectory() as directory, patch.object(cloud, "STATE", Path(directory) / "state.json"):
            cloud.upload(sdk, state, ROOT / "tests/fixtures/edges.txt")
        self.assertEqual([call.args[0] for call in sdk.client.call_args_list], ["s3"])
        self.assertEqual(sdk.client.return_value.upload_file.call_count, 5)
        self.assertEqual(state["input_sha256"], sha256(ROOT / "tests/fixtures/edges.txt"))

    def test_stale_website_handler_is_rejected_before_cloud_execution(self):
        state = {"api_source_sha256": sha256(ROOT / "backend/app.py"), "web_source_sha256": "old"}
        script_key = f"scripts/{sha256(ROOT / 'analysis/spark_job.py')}/spark_job.py"
        with self.assertRaisesRegex(RuntimeError, "Website handler deployment"):
            cloud.verify_deployed_sources(state, script_key)

    def test_new_deployment_failure_preserves_prior_bucket_inventory(self):
        cfn = Mock()
        cfn.get_paginator.return_value.paginate.return_value = []
        previous = {"status": "FAILED", "stack": "lab2", "retained_resources": [{"physical_id": "old-web"}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(cloud, "ROOT", Path(directory)):
            path = Path(directory) / "artifacts/evidence/cloud-deployment-failure.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(previous))
            cloud.failed_deployment(cfn, "lab2", "ROLLBACK_COMPLETE", "ap-southeast-1")
            current = json.loads(path.read_text())
        self.assertEqual(current["previous_failures"], [previous])
        self.assertEqual(current["stack_status"], "ROLLBACK_COMPLETE")


if __name__ == "__main__":
    unittest.main()
