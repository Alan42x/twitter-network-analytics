# Architecture and design decisions

## Batch analysis

`analysis/spark_job.py` is the shared entry point for local Spark and EMR on EC2. It validates two positive decimal IDs per record, normalises leading zeroes, audits malformed and blank rows, retains all valid endpoints, removes self-follows and deduplicates directed edges.

Followers are counted by target; followees are counted by source. Left joins against the endpoint inventory retain zero-degree users. Spark writes partitioned user JSON, one audit summary and, in the cloud, worker-execution evidence. Counts, joins and deduplication run through the distributed DataFrame engine. Only small audit records are collected on the driver.

`analysis/reference.py` independently parses the input into SQLite tables with unique edge and user keys. It computes the same inventory and degrees without reusing Spark's transformation logic. Verification compares every user and audit counter, checks the input identity and confirms edge/count conservation.

## Publication

The AWS controller in `scripts/cloud.py` uses content-addressed input and script keys and a new output directory for each run. Before replacing the publication, it checks:

1. The recorded Spark step completed with the expected input, script and output arguments.
2. User, summary and execution directories have Spark success markers.
3. The monitoring record contains completed tasks on at least two distinct worker hosts.
4. The result is structurally valid and matches the independent reference.

Failures preserve the previous publication. Submission intent is recorded before the EMR request; interrupted controllers can recover the recorded cluster and resume publication. Clusters terminate after their step; the running controller also enforces execution and startup time limits.

## Interactive queries

API Gateway routes `/api/*` to one Python Lambda function and website requests to a separate asset Lambda. Both read private S3 storage. The API caches the published snapshot and its indexes for 30 seconds. A content-derived snapshot ID binds summary and paginated requests to one result. The frontend cancels superseded requests, clears stale rows on errors and offers retry.

The same handlers run behind the standard-library local server. The included result snapshot supports local preview without AWS credentials. It retains the provenance of the recorded cloud computation.

## Capacity and tradeoffs

The recorded cluster used EMR 7.5.0 / Spark 3.5.2, one on-demand primary, one on-demand core worker and one Spot task worker, all `m5.xlarge`. Two 8 GiB executors reserve enough memory that both cannot fit on one 16 GiB worker. Publication additionally verifies completed work on two distinct hosts. A Spot interruption can fail the batch; the previous result remains published.

The API's full in-memory index is suitable for this 81,306-user dataset. Larger graphs need a partitioned query store. The local server is a development adapter, and the cloud app is a read-only public analytics demo; authentication and writes are outside its scope. No throughput or large-scale performance benchmark is claimed.
