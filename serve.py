#!/usr/bin/env python3
"""Same-origin helper for the llama-swap rig dashboard.

Serves index.html and transparently proxies API traffic to llama-swap on
127.0.0.1:8080 so the browser can pull everything without CORS trouble.
Streams responses chunk-by-chunk so SSE (/api/events) works unbuffered.

Pure stdlib. No parsing, no state. Run: python3 serve.py [--port 8090]
"""
import argparse
import http.server
import os
import socketserver
import sys
import urllib.request

UPSTREAM = "http://127.0.0.1:8080"
# The loaded model's llama-server. Queried DIRECTLY (not via llama-swap) so that
# reading /slots can never trigger a model load/swap. All models on this rig reuse
# this port since only one runs at a time.
UPSTREAM_LLAMA = "http://127.0.0.1:8081"
# Path prefixes proxied to llama-swap; everything else is served as a local file.
PROXY_PREFIXES = ("/api/", "/v1/", "/logs", "/running", "/health", "/unload", "/upstream", "/metrics")
HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _is_proxied(self):
        return any(self.path == p or self.path.startswith(p) for p in PROXY_PREFIXES)

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/_slots":
            # live token counts, read straight from the loaded llama-server (never swaps a model)
            self._proxy("GET", target=UPSTREAM_LLAMA + "/slots")
        elif self._is_proxied():
            self._proxy("GET")
        elif route == "/" or route == "/index.html":
            self._serve_file("index.html")
        elif route == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_error(404, "not found")

    def do_POST(self):
        if self._is_proxied():
            self._proxy("POST")
        else:
            self.send_error(404, "not found")

    def _serve_file(self, name):
        path = os.path.join(HERE, name)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method, target=None):
        length = int(self.headers.get("Content-Length", 0) or 0)
        payload = self.rfile.read(length) if length else None
        req = urllib.request.Request(target or (UPSTREAM + self.path), data=payload, method=method)
        for h in ("Content-Type", "Authorization", "Accept"):
            if h in self.headers:
                req.add_header(h, self.headers[h])
        try:
            # bounded timeout for direct upstream calls; unbounded for the SSE stream
            resp = urllib.request.urlopen(req, timeout=(5 if target else None))
        except urllib.error.HTTPError as e:
            resp = e  # forward upstream error bodies verbatim
        except Exception as e:
            self.send_error(502, f"upstream unreachable: {e}")
            return

        self.send_response(resp.status)
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        is_stream = "text/event-stream" in ctype
        self.send_header("Content-Type", ctype)
        if is_stream:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self._pump(resp)
        else:
            body = resp.read()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _pump(self, resp):
        """Relay an SSE stream line-by-line until the client goes away."""
        try:
            for line in resp:            # yields on each newline as data arrives
                self.wfile.write(line)
                if line == b"\n":         # blank line = end of an SSE event -> flush
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    srv = Server((args.host, args.port), Handler)
    print(f"dashboard on http://{args.host}:{args.port}  ->  proxying {UPSTREAM}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", file=sys.stderr)


if __name__ == "__main__":
    main()
