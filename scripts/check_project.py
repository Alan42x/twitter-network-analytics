"""Record actual unit/static checks with the versions of the reviewed files."""
import compileall
import io
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.cloud import template
from scripts.evidence import project_sources, source_hashes


def main():
    destination = ROOT / "artifacts/evidence/project-checks.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"status": "RUNNING"}) + "\n")
    generated = ROOT / "artifacts/cloudformation.generated.json"
    generated.write_text(json.dumps(template(), indent=2) + "\n")
    initial_sources = source_hashes(project_sources())
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    print(stream.getvalue())
    if not result.wasSuccessful():
        raise RuntimeError("Python tests failed")
    lint = Path(sys.executable).with_name("cfn-lint")
    subprocess.run([str(lint), str(ROOT / "infra/cloudformation.yaml"), str(generated)], check=True)
    for folder in ("analysis", "backend", "scripts", "tests"):
        if not compileall.compile_dir(str(ROOT / folder), quiet=1):
            raise RuntimeError(f"Python syntax failure in {folder}")
    if source_hashes(project_sources()) != initial_sources:
        raise RuntimeError("Source files changed during project verification; rerun the checks")
    record = {"status": "PASS", "python_tests": result.testsRun,
              "checks": ["Python tests", "Python syntax", "Source and generated CloudFormation cfn-lint"],
              "verified_at": datetime.now(timezone.utc).isoformat(), "source_sha256": initial_sources}
    destination.write_text(json.dumps(record, indent=2) + "\n")
    print(f"PASS: {result.testsRun} Python tests; both templates valid")


if __name__ == "__main__":
    main()
