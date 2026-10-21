"""Serve the exact cloud API and frontend locally, without runtime dependencies."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from backend.app import dispatch
from backend.web import dispatch as dispatch_web

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


class Handler(BaseHTTPRequestHandler):
    def send_result(self, method):
        parsed = urlsplit(self.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
        if parsed.path.startswith("/api/"):
            result = dispatch(method, parsed.path, query)
        else:
            result = dispatch_web(method, parsed.path, reader=lambda key: (FRONTEND / key).read_bytes())
        content = result["body"].encode()
        self.send_response(result["statusCode"])
        for key, value in result["headers"].items():
            self.send_header(key, value)
        if "content-length" not in result["headers"]:
            self.send_header("content-length", str(len(content)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(content)

    def do_GET(self):
        self.send_result("GET")

    def do_POST(self):
        self.send_result("POST")

    def do_HEAD(self):
        self.send_result("HEAD")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Twitter analytics: http://{args.host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
