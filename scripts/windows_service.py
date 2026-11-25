"""Background Windows servers with file-based, graceful shutdown."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import selectors
import socket
import socketserver
import threading
import time


ROOT = Path(__file__).resolve().parents[1]


def watch_stop(stop_file, shutdown):
    while not stop_file.exists():
        time.sleep(0.25)
    shutdown()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("backend", "frontend", "graph-bridge"))
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--stop-file", required=True, type=Path)
    parser.add_argument("--instance", required=True)
    args = parser.parse_args()
    if not args.stop_file.resolve().is_relative_to((ROOT / ".runtime").resolve()):
        parser.error("Stop file must be inside the project runtime directory")
    if args.role == "graph-bridge":
        class BridgeHandler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    with socket.create_connection(("::1", args.port), timeout=5) as upstream, selectors.DefaultSelector() as selector:
                        self.request.settimeout(10)
                        upstream.settimeout(10)
                        selector.register(self.request, selectors.EVENT_READ, upstream)
                        selector.register(upstream, selectors.EVENT_READ, self.request)
                        while True:
                            for key, _ in selector.select(timeout=1):
                                data = key.fileobj.recv(65536)
                                if not data:
                                    return
                                key.data.sendall(data)
                except OSError:
                    return

        class Bridge(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Bridge(("127.0.0.1", args.port), BridgeHandler)
        threading.Thread(target=watch_stop, args=(args.stop_file, server.shutdown), daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            server.server_close()
    elif args.role == "backend":
        import uvicorn
        server = uvicorn.Server(uvicorn.Config(
            "app.main:app", host="127.0.0.1", port=args.port, log_level="info",
            timeout_graceful_shutdown=30,
        ))
        def shutdown():
            server.should_exit = True
        threading.Thread(target=watch_stop, args=(args.stop_file, shutdown), daemon=True).start()
        server.run()
    else:
        class Handler(SimpleHTTPRequestHandler):
            def list_directory(self, path):
                self.send_error(404)
                return None

            def do_GET(self):
                if self.path == "/_runtime/health":
                    body = json.dumps({"status": "ok", "instance": args.instance}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                super().do_GET()

            def end_headers(self):
                self.send_header("Cache-Control", "no-cache")
                super().end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, directory=str(ROOT / "frontend")))
        threading.Thread(target=watch_stop, args=(args.stop_file, server.shutdown), daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
