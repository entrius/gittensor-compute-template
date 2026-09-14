# The MIT License (MIT)
# Copyright © 2025 Entrius

"""PLACEHOLDER. Replace with your inference server.

A stdlib HTTP server that answers ``GET /v1/models`` (so the health probe and ``scripts/validate --build`` pass
without a GPU) and returns 501 for everything else. It exists so the template image builds and starts as-is; it
serves no model and must not ship in a blessed image.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('PORT', '8080'))
MODEL_ID = os.environ.get('PLACEHOLDER_MODEL_ID', 'placeholder-replace-me')


class Handler(BaseHTTPRequestHandler):
    server_version = 'gittensor-placeholder'
    sys_version = ''

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split('?', 1)[0] == '/v1/models':
            return self._send(200, {'object': 'list', 'data': [{'id': MODEL_ID, 'object': 'model'}]})
        self._send(404, {'error': 'not found'})

    def do_POST(self) -> None:
        length = int(self.headers.get('Content-Length') or 0)
        self.rfile.read(length)
        self._send(501, {'error': 'placeholder server: replace placeholder_server.py with your runtime'})

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        sys.stderr.write('placeholder: ' + fmt % args + '\n')


def main() -> None:
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    server.daemon_threads = True

    def stop(signum, frame) -> None:  # the drain contract: SIGTERM -> finish -> exit
        server.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    sys.stderr.write(f'placeholder server on :{PORT} (REPLACE ME)\n')
    server.serve_forever()


if __name__ == '__main__':
    main()
