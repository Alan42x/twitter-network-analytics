"""Run actual Spark on empty, dirty and precision-sensitive inputs."""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SPARK_LOCAL_IP", "127.0.0.1")
os.environ["PYSPARK_PYTHON"] = sys.executable
os.environ["PYSPARK_SUBMIT_ARGS"] = "--master local[2] --driver-memory 1g --conf spark.ui.enabled=false pyspark-shell"
from analysis.reference import analyze
from analysis.spark_job import compute
from pyspark.sql import SparkSession
from scripts.evidence import source_hashes


def main():
    output = ROOT / "artifacts/evidence/spark-edge-verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"status": "RUNNING"}) + "\n")
    cases = {
        "dedup_direction_and_dirty_rows": (ROOT / "tests/fixtures/edges.txt").read_bytes(),
        "empty": b"",
        "loops_only_and_normalized_ids": b"0001 1\n2 02\n1 1\n",
        "crlf_and_embedded_carriage_return": b"1 2\r\n2\r3\n\r\n3 1",
        "precision_and_id_boundaries": b"99999999999999999999 1\n1 99999999999999999999\n123456789012345678901 1\n0 1\n",
        "unicode_and_invalid_utf8": b"1\t2\n\xff 2\n1\xc2\xa02\n\xc2\xa0\n\t\n1 2 extra\n",
    }
    checks = []
    spark = SparkSession.builder.appName("CS5224-Edge-Regression").getOrCreate()
    spark.sparkContext.setLogLevel("ERROR")
    spark.conf.set("spark.sql.shuffle.partitions", "2")
    try:
        with tempfile.TemporaryDirectory(prefix="spark-edge-") as directory:
            for name, content in cases.items():
                path = Path(directory) / (name + ".txt")
                path.write_bytes(content)
                reference = analyze(path)
                users, summary = compute(spark, str(path))
                actual = {row["user_id"]: row.asDict() for row in users.collect()}
                expected = {row["user_id"]: row for row in reference["users"]}
                if actual != expected:
                    raise RuntimeError(f"Spark user mismatch in {name}")
                metrics = summary.first().asDict()
                if any(value != reference["summary"][key] for key, value in metrics.items()):
                    raise RuntimeError(f"Spark audit mismatch in {name}: {metrics} / {reference['summary']}")
                checks.append({"case": name, "summary": metrics})
                spark.catalog.clearCache()
    finally:
        spark.stop()
    result = {"status": "PASS", "verified_at": datetime.now(timezone.utc).isoformat(), "cases": checks,
              "source_sha256": source_hashes(["analysis/spark_job.py", "analysis/reference.py", "scripts/verify_spark_edges.py"])}
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
