# Cloud execution evidence

The published report includes original frontend, API and AWS console captures from its recorded execution. For a new run, use the identities in your own `cloud-state.json` and generated evidence; screenshots from an earlier run cannot certify a new execution.

| Report asset | Required visible details |
|---|---|
| `manual-frontend.png` | Deployed HTTPS address, cloud provenance, dataset metrics and both required user cards |
| `manual-api.png` | HTTPS API address, both user records and the snapshot ID |
| `aws-emr-cluster.png` | Cluster ID, EMR/Spark versions, primary/core/task topology and cluster status |
| `aws-emr-success.png` | Matching run and Step ID with Completed status |
| `aws-s3-output.png` | Matching run directory, `_SUCCESS` and partitioned JSON output |

Runtime images are supplied and reviewed separately from automated browser checks. Save original captures under `report/assets/` without reconstructing console pages or changing result values. The local `artifacts/evidence/manual-captures.json` records reviewed image hashes and matching execution provenance. Replacing an image or publishing a different run requires a new review.

The Spark job also writes executor IDs, worker hosts and completed-task counters to the execution output directory. The controller requires completed tasks on two distinct worker hosts before publication. The report includes this verified activity table; a Spark Executors screenshot is optional additional evidence.

After the matching records and reviewed images are present:

```bash
.venv/bin/python scripts/check_project.py
.venv/bin/python scripts/build_report.py
cat artifacts/report-status.json
```

A complete report has status `CLOUD_EVIDENCE_INCLUDED` and no `missing_evidence`. See [reproducibility](reproducibility.md) for the distinction between previewing included results and rerunning the analysis.
