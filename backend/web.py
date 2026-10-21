"""Serve the lab's three frontend assets from private S3 through API Gateway."""
import logging
import os

LOGGER = logging.getLogger(__name__)
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
_s3 = None


def read_asset(key):
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3")
    response = _s3.get_object(Bucket=os.environ["WEB_BUCKET"], Key=key)
    with response["Body"] as stream:
        return stream.read()


def dispatch(method, path, reader=None):
    headers = {"content-type": "text/plain; charset=utf-8", "cache-control": "no-store",
               "x-content-type-options": "nosniff", "x-frame-options": "DENY",
               "referrer-policy": "no-referrer"}
    if path not in ASSETS:
        status, body = 404, "Page not found."
    elif method not in ("GET", "HEAD"):
        status, body = 405, "Only GET and HEAD are supported."
        headers["allow"] = "GET, HEAD"
    else:
        key, content_type = ASSETS[path]
        try:
            body = (reader or read_asset)(key).decode("utf-8")
        except Exception:
            LOGGER.exception("Unable to load frontend asset %s", key)
            status, body = 503, "Website files are not ready; upload the frontend first."
        else:
            status = 200
            headers["content-type"] = content_type
    if method == "HEAD":
        headers["content-length"] = str(len(body.encode("utf-8")))
    return {"statusCode": status, "headers": headers, "isBase64Encoded": False,
            "body": "" if method == "HEAD" else body}


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    return dispatch(method, event.get("rawPath", "/"))
