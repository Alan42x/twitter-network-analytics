# Reproducibility

The repository supports three different workflows. The saved results and exact original input are both included. Previewing results needs only Python; recomputing the graph additionally needs the analysis environment. Recreating the AWS deployment also needs your own AWS access.

## 1. Preview the recorded results

From a fresh checkout:

```bash
python3 -m backend.local_server
```

Open **http://127.0.0.1:8000**. The local preview uses only Python's standard library; Python 3.10+ is recommended. It does not require pip packages, Node.js, Spark, Java or AWS credentials.

`artifacts/current.json.gz` is deliberately tracked. It contains the complete, validated per-user results from the recorded EMR run, including the two required profiles. The local server runs the actual Python API and frontend against that saved publication. It does not rerun Spark or provision cloud resources. The displayed cloud provenance describes where the data was computed, rather than where this preview server runs.

## 2. Recompute the same results

The exact modified [`twitter_combined.txt`](../twitter_combined.txt) supplied for the lab is included in the repository root. Use this file to reproduce the reported counts. The public SNAP page describes its source collection; this repository includes the specific modified input used in the report.

| Input identity | Expected value |
|---|---|
| Size | 44,647,787 bytes |
| Rows | 2,426,143 |
| SHA-256 | `0246d0cae6b4b3c874c968e5caed5e08753a98ce36be2ddb861fce982afdf92c` |

Git preserves the dataset's exact bytes across platforms. You can verify its identity before running the analysis:

```bash
python3 - <<'PY'
import hashlib
from pathlib import Path
path = Path("twitter_combined.txt")
assert path.stat().st_size == 44647787
assert hashlib.sha256(path.read_bytes()).hexdigest() == "0246d0cae6b4b3c874c968e5caed5e08753a98ce36be2ddb861fce982afdf92c"
print("Dataset verified")
PY
```

The recorded local environment used Python 3.13, Java 8 and PySpark 3.5.2. Set `JAVA_HOME` when Java is not discoverable.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt

ANALYTICS_OUTPUT="artifacts/spark-output/$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python scripts/run_spark_local.py --output "$ANALYTICS_OUTPUT"
.venv/bin/python scripts/verify_results.py \
  --spark-output "$ANALYTICS_OUTPUT" \
  --publish-local
```

Use a new output directory for each run. Verification independently recomputes the SQLite result, compares every user and audit counter, verifies input and Spark source identities, and checks output success markers. `--publish-local` replaces the preview snapshot with those validated local results.

For another compatible two-column graph, pass the same `--input` to both commands. Its statistics will differ, and the current frontend expects the two lab-specific example profiles to be present.

## 3. Recreate the AWS pipeline

Complete the independent verification above first, then follow [AWS deployment](deployment.md). Deployment requires AWS CLI/SDK authentication, permissions for the documented services and sufficient EC2/Spot quota. Provision resources in your own account; `cloud-state.json` is local recovery state and is not part of the repository.

The deployed Spark job uses EMR 7.5.0 / Spark 3.5.2 and two compute workers. Publication checks the completed step, three success markers, completed work on distinct worker hosts and agreement with the reference. AWS resources incur usage charges.

## Verification

```bash
.venv/bin/python scripts/check_project.py
.venv/bin/python scripts/verify_spark_edges.py
```

The first command runs Python regressions, syntax checks and cfn-lint without provisioning AWS resources. The second requires Java/Spark and exercises seven small boundary cases. Neither needs the full raw dataset.

For browser checks, keep the preview server running and use another terminal:

```bash
npm ci
npm run verify:browser
```

This optional workflow needs Node.js 20+ and an installed Chrome or Chromium. Set `CHROME_PATH` outside the default macOS installation. `APP_URL` defaults to `http://127.0.0.1:8000`; it can point to your own deployment. Generated verification output remains under the ignored `artifacts/` directory.

## Published reports and regeneration

The [PDF](../report/A0329678H.pdf), [Markdown report](../report/A0329678H.md), source text and referenced screenshots are included. They document the recorded cloud execution and can be read directly after cloning.

Regenerating a fully certified cloud report additionally needs the matching execution records, source checks, screenshot-review manifest and browser verification. Those local records are excluded from Git. Running `scripts/build_report.py` from a checkout without them intentionally creates a draft; it does not certify the old screenshots as evidence of a new run.

To document a new deployment, complete analysis, publication, verification and [screenshot review](cloud-evidence.md), then regenerate the report. Source changes require rerunning the affected checks.
