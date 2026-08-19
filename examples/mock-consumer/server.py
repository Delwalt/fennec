from __future__ import annotations

import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
from threading import Lock, Thread
from time import sleep
from typing import Any


TOKEN = os.environ.get("FENNEC_CONSUMER_TOKEN", "")
if len(TOKEN) < 24:
    raise RuntimeError("FENNEC_CONSUMER_TOKEN must contain at least 24 characters")
try:
    STREAM_DELAY_SECONDS = int(os.environ.get("FENNEC_MOCK_STREAM_DELAY_MS", "120")) / 1_000
except ValueError as error:
    raise RuntimeError("FENNEC_MOCK_STREAM_DELAY_MS must be an integer") from error
if not 0 <= STREAM_DELAY_SECONDS <= 5:
    raise RuntimeError("FENNEC_MOCK_STREAM_DELAY_MS must be between 0 and 5000")

STATS = {"turn_requests": 0, "client_disconnects": 0}
STATS_LOCK = Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FennecMockConsumer/0.1"

    def do_GET(self) -> None:
        if self.path == "/metrics":
            with STATS_LOCK:
                metrics = dict(STATS)
            self._json(HTTPStatus.OK, metrics)
            return
        if self.path != "/health":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._json(HTTPStatus.OK, {"status": "ready"})

    def do_POST(self) -> None:
        if self.path != "/v1/turns":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid_credential"})
            return
        try:
            turn = self._read_turn()
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        with STATS_LOCK:
            STATS["turn_requests"] += 1

        generation_id = turn["generation_id"]
        events = [
            {
                "type": "text.delta",
                "generation_id": generation_id,
                "text": "I heard you. ",
            },
            {
                "type": "text.delta",
                "generation_id": generation_id,
                "text": f"You said: {turn['text']}",
            },
            {"type": "text.done", "generation_id": generation_id},
        ]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for event in events:
                line = (json.dumps(event, separators=(",", ":")) + "\n").encode()
                self.wfile.write(f"{len(line):X}\r\n".encode())
                self.wfile.write(line)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                sleep(STREAM_DELAY_SECONDS)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            with STATS_LOCK:
                STATS["client_disconnects"] += 1

    def log_message(self, format: str, *args: Any) -> None:
        print(f"consumer client={self.client_address[0]} {format % args}", flush=True)

    def _authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        supplied = authorization.removeprefix("Bearer ").strip()
        return bool(supplied) and hmac.compare_digest(supplied, TOKEN)

    def _read_turn(self) -> dict[str, str]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("content_length_required")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ValueError("invalid_content_length") from error
        if not 1 <= length <= 65_536:
            raise ValueError("body_size_invalid")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise ValueError("invalid_json") from error
        required = ("session_id", "turn_id", "generation_id", "text")
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(name), str) or not payload[name].strip()
            for name in required
        ):
            raise ValueError("invalid_turn")
        return {name: payload[name] for name in required}

    def _json(self, status: HTTPStatus, payload: dict[str, str | int]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8090), Handler)

    def stop(_: int, __: Any) -> None:
        Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print("mock consumer listening on 0.0.0.0:8090", flush=True)
    server.serve_forever(poll_interval=0.2)
    server.server_close()


if __name__ == "__main__":
    main()
