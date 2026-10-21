"""Compare every Spark result against the independent SQLite oracle."""
import argparse
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.reference import analyze, save_snapshot
from backend.app import AUDIT_FIELDS, Snapshot
from scripts.evidence import sha256, source_hashes


def load_reference(input_path, cached=None):
    if cached is None:
        return analyze(input_path)
    reference = json.loads(gzip.decompress(Path(cached).read_bytes()))
    Snapshot(reference)
    if reference["provenance"].get("engine") != "SQLite independent reference":
        raise RuntimeError("Cached reference must come from the independent SQLite oracle, not published Spark results")
    if reference["summary"]["input_sha256"] != sha256(input_path) or reference["summary"]["input_bytes"] != Path(input_path).stat().st_size:
        raise RuntimeError("Cached reference does not match the current input")
    return reference


def read_parts(path):
    path = Path(path)
    if not (path / "_SUCCESS").exists():
        raise RuntimeError(f"Missing Spark success marker: {path}")
    return [json.loads(line) for part in sorted(path.glob("part-*.json"))
            for line in part.read_text().splitlines() if line]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spark-output", default="artifacts/spark-output/full")
    parser.add_argument("--input", default="twitter_combined.txt")
    parser.add_argument("--reference", help="Optional SQLite-only cache; default recomputes an independent oracle")
    parser.add_argument("--publish-local", action="store_true")
    args = parser.parse_args()
    output = ROOT / "artifacts/evidence/local-verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"status": "RUNNING"}) + "\n")
    reference = load_reference(args.input, args.reference)
    manifest_path = Path(args.spark_output) / "run-manifest.json"
    if not manifest_path.exists():
        raise RuntimeError("Missing local Spark run manifest; rerun scripts/run_spark_local.py in a new output directory")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("status") != "PASS" or manifest.get("input_sha256") != reference["summary"]["input_sha256"] or manifest.get("input_bytes") != reference["summary"]["input_bytes"]:
        raise RuntimeError("Spark run manifest does not match the current input")
    if manifest.get("script_sha256") != sha256(ROOT / "analysis/spark_job.py"):
        raise RuntimeError("Spark output was produced by a different source version; rerun the current code")
    users = read_parts(Path(args.spark_output) / "users")
    summary_rows = read_parts(Path(args.spark_output) / "summary")
    if len(summary_rows) != 1:
        raise RuntimeError("Spark should produce one summary record")
    summary = summary_rows[0]
    if set(summary) != set(AUDIT_FIELDS):
        raise RuntimeError("Spark audit schema mismatch")
    for key, value in summary.items():
        if reference["summary"].get(key) != value:
            raise RuntimeError(f"Summary mismatch at {key}: Spark={value}; reference={reference['summary'].get(key)}")
    actual = {row["user_id"]: row for row in users}
    expected = {row["user_id"]: row for row in reference["users"]}
    if actual != expected or len(actual) != len(users):
        raise RuntimeError("User-by-user comparison failed")
    required = [actual[user] for user in ("214328887", "107830991") if user in actual]
    summary.update({key: reference["summary"][key] for key in ("input_sha256", "input_bytes")})
    snapshot = {"schema_version": 1, "summary": summary, "users": users,
                "provenance": {"environment": "local", "engine": f"Apache Spark {manifest['spark_version']} ({manifest['master']})",
                               "generated_at": manifest["finished_at"]}}
    validated = Snapshot(snapshot)
    result = {"status": "PASS", "compared_users": len(actual), "summary": summary,
              "snapshot_id": validated.snapshot_id, "run_manifest": manifest,
              "verified_at": datetime.now(timezone.utc).isoformat(),
              "source_sha256": source_hashes(["analysis/spark_job.py", "analysis/reference.py", "scripts/verify_results.py"]),
              "required_users": required, "environment": "local",
              "checks": ["Every user matches independent SQLite aggregation", "No duplicate output user IDs",
                         "All audit counters match", "Both degree sums equal the number of unique edges"]}
    print(json.dumps(result, indent=2))
    output.write_text(json.dumps(result, indent=2) + "\n")
    save_snapshot(reference, ROOT / "artifacts/reference.json.gz")
    if args.publish_local:
        save_snapshot(snapshot, ROOT / "artifacts/current.json.gz")


if __name__ == "__main__":
    main()
