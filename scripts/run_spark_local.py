"""Portable Spark launcher that also works when the project path contains spaces."""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.evidence import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="twitter_combined.txt")
    parser.add_argument("--output", default="artifacts/spark-output/full")
    args = parser.parse_args()
    env = os.environ.copy()
    env["SPARK_LOCAL_IP"] = "127.0.0.1"
    env["PYSPARK_PYTHON"] = sys.executable
    env["PYSPARK_SUBMIT_ARGS"] = "--master local[2] --driver-memory 1g --conf spark.ui.enabled=false pyspark-shell"
    destination = Path(args.output)
    if not destination.is_absolute():
        destination = ROOT / destination
    if destination.exists():
        parser.error("Output directory already exists; choose a new --output directory")
    input_path = Path(args.input).resolve()
    input_hash = sha256(input_path)
    script_hash = sha256(ROOT / "analysis/spark_job.py")
    started = datetime.now(timezone.utc).isoformat()
    log = destination.with_suffix(destination.suffix + ".log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as stream:
        result = subprocess.run([sys.executable, "-m", "analysis.spark_job", "--input", str(input_path),
                                 "--output", str(destination), "--partitions", "4"],
                                cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    print(log.read_text())
    if result.returncode:
        raise SystemExit(result.returncode)
    if input_hash != sha256(input_path) or script_hash != sha256(ROOT / "analysis/spark_job.py"):
        raise RuntimeError("Input or Spark source changed during execution; results cannot be certified")
    import pyspark
    manifest = {"status": "PASS", "input_sha256": input_hash, "input_bytes": input_path.stat().st_size,
                "script_sha256": script_hash, "master": "local[2]", "spark_version": pyspark.__version__,
                "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat()}
    (destination / "run-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
