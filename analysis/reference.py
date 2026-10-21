"""Independent SQLite oracle; does not substitute for the required cloud run."""
import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROW = re.compile(r"\s*([0-9]{1,20})\s+([0-9]{1,20})\s*", re.ASCII)


def analyze(path):
    metrics = dict(raw_lines=0, valid_rows=0, blank_rows=0, invalid_rows=0,
                   self_loop_rows=0)
    digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix="twitter-reference-") as scratch:
        db = sqlite3.connect(str(Path(scratch) / "edges.sqlite"))
        db.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE edges (source TEXT, target TEXT, PRIMARY KEY(source,target)) WITHOUT ROWID;
            CREATE TABLE users (user_id TEXT PRIMARY KEY) WITHOUT ROWID;
        """)
        edge_batch, user_batch = [], []

        def flush():
            db.executemany("INSERT OR IGNORE INTO users VALUES (?)", user_batch)
            db.executemany("INSERT OR IGNORE INTO edges VALUES (?,?)", edge_batch)
            edge_batch.clear()
            user_batch.clear()

        with open(path, "rb") as source:
            for raw in source:
                digest.update(raw)
                metrics["raw_lines"] += 1
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    metrics["invalid_rows"] += 1
                    continue
                if not line.strip(" \t\n\r\f\v"):
                    metrics["blank_rows"] += 1
                    continue
                match = ROW.fullmatch(line)
                if not match:
                    metrics["invalid_rows"] += 1
                    continue
                a, b = (value.lstrip("0") for value in match.groups())
                if not a or not b:
                    metrics["invalid_rows"] += 1
                    continue
                metrics["valid_rows"] += 1
                user_batch.extend(((a,), (b,)))
                if a == b:
                    metrics["self_loop_rows"] += 1
                else:
                    edge_batch.append((a, b))
                if len(user_batch) >= 10000:
                    flush()
        flush()
        db.commit()
        unique_edges = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        db.executescript("""
            CREATE TABLE incoming AS SELECT target AS user_id, COUNT(*) AS followers FROM edges GROUP BY target;
            CREATE UNIQUE INDEX incoming_id ON incoming(user_id);
            CREATE TABLE outgoing AS SELECT source AS user_id, COUNT(*) AS followees FROM edges GROUP BY source;
            CREATE UNIQUE INDEX outgoing_id ON outgoing(user_id);
        """)
        rows = db.execute("""
            SELECT u.user_id, COALESCE(i.followers,0), COALESCE(o.followees,0)
            FROM users u LEFT JOIN incoming i USING(user_id) LEFT JOIN outgoing o USING(user_id)
            ORDER BY LENGTH(u.user_id), u.user_id
        """)
        users = [dict(user_id=u, followers=f, followees=e) for u, f, e in rows]
        db.close()
    metrics.update(unique_edges=unique_edges, total_users=len(users),
                   duplicate_edges=metrics["valid_rows"] - metrics["self_loop_rows"] - unique_edges,
                   followers_sum=sum(row["followers"] for row in users),
                   followees_sum=sum(row["followees"] for row in users),
                   input_sha256=digest.hexdigest(), input_bytes=Path(path).stat().st_size)
    assert metrics["followers_sum"] == metrics["followees_sum"] == unique_edges
    return {"schema_version": 1, "summary": metrics, "users": users,
            "provenance": {"environment": "local", "engine": "SQLite independent reference",
                           "generated_at": datetime.now(timezone.utc).isoformat()}}


def save_snapshot(snapshot, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(gzip.compress(json.dumps(snapshot, separators=(",", ":")).encode(), mtime=0))
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="twitter_combined.txt")
    parser.add_argument("--output", default="artifacts/reference.json.gz")
    args = parser.parse_args()
    snapshot = analyze(args.input)
    save_snapshot(snapshot, args.output)
    print(json.dumps(snapshot["summary"], indent=2))


if __name__ == "__main__":
    main()
