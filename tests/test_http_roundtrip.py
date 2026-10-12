"""Real-socket check: the SDK against a tiny local HTTP server (stdlib unittest)."""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline_sdk import env, log_mask, storage, util  # noqa: E402

SEEN: list[dict] = []
FLAKY = {"remaining": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if FLAKY["remaining"] > 0:
            FLAKY["remaining"] -= 1
            return self.reply(500, {"error": "Internal error"})
        if self.path == "/api/node/secret/get":
            if body.get("name") == "MISSING":
                return self.reply(404, {"error": "Not found"})
            return self.reply(200, {"value": "real-secret-value"})
        if self.path == "/api/node/storage/download-url":
            return self.reply(200, {"url": "http://example/file"})
        self.reply(403, {"error": "storage limit reached"})

    def reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class RoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        SEEN.clear()
        FLAKY["remaining"] = 0
        log_mask._reset()
        self.addCleanup(log_mask._reset)
        patch = mock.patch.dict(os.environ, {"BACKEND_URL": self.url, "API_KEY": "the-key"})
        patch.start()
        self.addCleanup(patch.stop)
        delay = mock.patch.object(util, "RETRY_DELAY_S", 0)
        delay.start()
        self.addCleanup(delay.stop)

    def test_get_secret_over_a_real_socket(self) -> None:
        self.assertEqual(env.get_secret("API_KEY_NAME"), "real-secret-value")
        self.assertEqual(SEEN[-1]["path"], "/api/node/secret/get")
        self.assertEqual(SEEN[-1]["auth"], "Bearer the-key")
        self.assertEqual(SEEN[-1]["body"], {"name": "API_KEY_NAME"})
        self.assertEqual(log_mask.mask("x real-secret-value y"), "x [REDACTED] y")

    def test_missing_secret_is_404_with_one_attempt(self) -> None:
        with self.assertRaisesRegex(env.EnvError, "No secret named 'MISSING'"):
            env.get_secret("MISSING")
        self.assertEqual(len(SEEN), 1)

    def test_a_server_error_is_retried_and_then_succeeds(self) -> None:
        FLAKY["remaining"] = 2
        self.assertEqual(env.get_secret("NAME"), "real-secret-value")
        self.assertEqual(len(SEEN), 3)

    def test_storage_post_uses_the_same_path_and_maps_quota(self) -> None:
        self.assertEqual(storage._post("/api/node/storage/download-url", {"path": "p"}), {"url": "http://example/file"})
        with self.assertRaises(storage.StorageQuotaExceeded):
            storage._post("/api/node/storage/create-session", {})


if __name__ == "__main__":
    unittest.main()
