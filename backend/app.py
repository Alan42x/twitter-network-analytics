import bisect
import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)
USER_ID = re.compile(r"[0-9]{1,20}", re.ASCII)
AUDIT_FIELDS = ("raw_lines", "valid_rows", "blank_rows", "invalid_rows", "self_loop_rows",
                "duplicate_edges", "unique_edges", "total_users", "followers_sum", "followees_sum")
ROOT = Path(__file__).resolve().parents[1]
_cache = None
_checked_at = 0
_lock = threading.Lock()
_s3 = None


class NotReady(Exception):
    pass


def numeric_key(value):
    return len(value), value


class Snapshot:
    def __init__(self, content):
        if not isinstance(content, dict) or type(content.get("schema_version")) is not int or content["schema_version"] != 1:
            raise ValueError("Unsupported snapshot schema")
        self.summary, self.provenance = content.get("summary"), content.get("provenance")
        if not isinstance(self.summary, dict) or not isinstance(self.provenance, dict):
            raise ValueError("Missing summary or provenance")
        if self.provenance.get("environment") not in ("local", "cloud") or any(
                not isinstance(self.provenance.get(key), str) or not self.provenance[key]
                for key in ("engine", "generated_at")):
            raise ValueError("Invalid provenance")
        if self.provenance["environment"] == "cloud" and any(
                not isinstance(self.provenance.get(key), str) or not self.provenance[key]
                for key in ("job_run_id", "cluster_id", "region", "input_uri", "output_uri")):
            raise ValueError("Incomplete cloud provenance")
        if not isinstance(self.summary.get("input_sha256"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", self.summary["input_sha256"]) or type(self.summary.get("input_bytes")) is not int or self.summary["input_bytes"] < 0:
            raise ValueError("Invalid input identity")
        if any(type(self.summary.get(key)) is not int or self.summary[key] < 0 for key in AUDIT_FIELDS):
            raise ValueError("Invalid audit counters")
        if self.summary["raw_lines"] != sum(self.summary[k] for k in ("valid_rows", "blank_rows", "invalid_rows")):
            raise ValueError("Input row accounting mismatch")
        if self.summary["valid_rows"] != sum(self.summary[k] for k in ("self_loop_rows", "duplicate_edges", "unique_edges")):
            raise ValueError("Valid edge accounting mismatch")
        if any(self.summary[k] != self.summary["unique_edges"] for k in ("followers_sum", "followees_sum")):
            raise ValueError("Degree audit mismatch")
        users = content.get("users")
        if not isinstance(users, list) or any(not isinstance(row, dict) or
                not isinstance(row.get("user_id"), str) or not USER_ID.fullmatch(row["user_id"]) or
                row["user_id"].startswith("0") or any(type(row.get(k)) is not int or
                not 0 <= row[k] < max(1, len(users)) for k in ("followers", "followees")) for row in users):
            raise ValueError("Invalid user statistics or impossible degree")
        self.users = sorted(users, key=lambda row: numeric_key(row["user_id"]))
        self.by_id = {row["user_id"]: row for row in self.users}
        if len(self.users) != self.summary["total_users"] or len(self.by_id) != len(self.users):
            raise ValueError("User inventory mismatch")
        for field in ("followers", "followees"):
            if sum(row[field] for row in self.users) != self.summary["unique_edges"]:
                raise ValueError("Edge/count conservation failed")
        self.orders = {"id": self.users}
        for field in ("followers", "followees"):
            self.orders[field] = sorted(self.users, key=lambda row: (-row[field], numeric_key(row["user_id"])))
        self.lexical = sorted(self.by_id)
        identity = {"summary": {key: self.summary[key] for key in
                    (*AUDIT_FIELDS, "input_sha256", "input_bytes")}, "users": self.users}
        self.snapshot_id = hashlib.sha256(json.dumps(identity, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()


def load_snapshot():
    global _cache, _checked_at, _s3
    now = time.monotonic()
    if _cache is not None and now - _checked_at < 30:
        return _cache
    with _lock:
        if _cache is not None and time.monotonic() - _checked_at < 30:
            return _cache
        try:
            if os.getenv("RESULTS_BUCKET"):
                import boto3
                if _s3 is None:
                    _s3 = boto3.client("s3")
                response = _s3.get_object(Bucket=os.environ["RESULTS_BUCKET"],
                                          Key=os.getenv("RESULTS_KEY", "results/current.json.gz"))
                compressed = response["Body"].read()
            else:
                compressed = Path(os.getenv("LOCAL_SNAPSHOT", ROOT / "artifacts/current.json.gz")).read_bytes()
            fresh = Snapshot(json.loads(gzip.decompress(compressed)))
        except Exception as error:
            LOGGER.exception("Unable to load the result snapshot")
            # Do not claim a fresh result after an inaccessible/corrupt publication.
            raise NotReady("Statistics are not ready; run and publish the analysis first.") from error
        _cache, _checked_at = fresh, time.monotonic()
        return _cache


def response(status, payload):
    return {"statusCode": status,
            "headers": {"content-type": "application/json; charset=utf-8",
                        "cache-control": "no-store", "x-content-type-options": "nosniff"},
            "body": json.dumps(payload, separators=(",", ":"))}


def normalize_id(value):
    if not isinstance(value, str) or not USER_ID.fullmatch(value) or not value.lstrip("0"):
        raise ValueError("User ID must contain 1 to 20 decimal digits and be positive.")
    return value.lstrip("0")


def dispatch(method, path, query=None, snapshot=None):
    if query is None:
        query = {}
    if method != "GET":
        result = response(405, {"error": "Only GET is supported."})
        result["headers"]["allow"] = "GET"
        return result
    if path not in ("/api/health", "/api/summary", "/api/users") and not path.startswith("/api/users/"):
        return response(404, {"error": "API route not found."})
    try:
        if not isinstance(query, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                               for key, value in query.items()):
            raise ValueError("Query parameters must be strings.")
        store = snapshot or load_snapshot()
        if query.get("snapshot_id") and query["snapshot_id"] != store.snapshot_id:
            return response(412, {"error": "Published statistics changed. Reload the analysis to use the current result."})
        if path == "/api/health":
            return response(200, {"status": "ok", "environment": store.provenance["environment"]})
        if path == "/api/summary":
            return response(200, {"summary": store.summary, "provenance": store.provenance,
                                  "snapshot_id": store.snapshot_id})
        if path.startswith("/api/users/"):
            user_id = normalize_id(path.removeprefix("/api/users/"))
            return (response(200, store.by_id[user_id]) if user_id in store.by_id else
                    response(404, {"error": "User not present in this dataset."}))
        if "ids" in query:
            values = query["ids"].split(",")
            if not 1 <= len(values) <= 20:
                raise ValueError("Request between 1 and 20 user IDs.")
            ids = list(dict.fromkeys(normalize_id(value) for value in values))
            return response(200, {"users": [store.by_id[value] for value in ids if value in store.by_id],
                                  "missing_ids": [value for value in ids if value not in store.by_id],
                                  "snapshot_id": store.snapshot_id})
        limit, offset = int(query.get("limit", "50")), int(query.get("offset", "0"))
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("limit must be 1..200 and offset must be nonnegative.")
        order = query.get("sort", "id")
        if order not in store.orders:
            raise ValueError("sort must be id, followers, or followees.")
        prefix = query.get("search", "").strip()
        if prefix:
            if not USER_ID.fullmatch(prefix):
                raise ValueError("Search must be a decimal user ID prefix.")
            start = bisect.bisect_left(store.lexical, prefix)
            end = bisect.bisect_right(store.lexical, prefix + "\uffff")
            rows = [store.by_id[value] for value in store.lexical[start:end]]
            if order == "id":
                rows.sort(key=lambda row: numeric_key(row["user_id"]))
            else:
                rows.sort(key=lambda row: (-row[order], numeric_key(row["user_id"])))
        else:
            rows = store.orders[order]
        next_offset = offset + limit if offset + limit < len(rows) else None
        return response(200, {"users": rows[offset:offset + limit], "total": len(rows),
                              "offset": offset, "limit": limit, "next_offset": next_offset,
                              "snapshot_id": store.snapshot_id})
    except (ValueError, TypeError) as error:
        return response(400, {"error": str(error)})
    except NotReady as error:
        return response(503, {"error": str(error)})


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    return dispatch(method, event.get("rawPath", "/"), event.get("queryStringParameters"))
