"""Tests for pipeline_sdk.util (stdlib unittest; run from packages/pipeline-sdk-py)."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline_sdk import util  # noqa: E402


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int, body: dict | str) -> urllib.error.HTTPError:
    raw = body if isinstance(body, str) else json.dumps(body)
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(raw.encode()))  # type: ignore[arg-type]


class PostJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(util.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_returns_decoded_json_and_sends_bearer_token(self) -> None:
        seen = {}

        def fake_urlopen(request, timeout):
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data)
            return FakeResponse(b'{"ok": true}')

        with mock.patch.object(util.urllib.request, "urlopen", fake_urlopen):
            result = util.post_json("http://b/api", {"a": 1}, token="tok", label="/api")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertEqual(seen["body"], {"a": 1})

    def test_client_error_fails_immediately_without_retry(self) -> None:
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            raise http_error(404, {"error": "Not found"})

        with mock.patch.object(util.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(util.ApiError) as ctx:
                util.post_json("http://b/api", {}, token="t", label="/api/x")
        self.assertEqual(len(calls), 1)
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.detail, "Not found")
        self.assertIn("/api/x failed (404): Not found", str(ctx.exception))

    def test_server_error_is_retried_then_raised(self) -> None:
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            raise http_error(500, {"error": "Internal error"})

        with mock.patch.object(util.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(util.ApiError) as ctx:
                util.post_json("http://b/api", {}, token="t", label="/api/x", retries=3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(self.sleep.call_count, 3)

    def test_server_error_then_success_recovers(self) -> None:
        outcomes = [http_error(503, "busy"), FakeResponse(b'{"v": 1}')]

        def fake_urlopen(request, timeout):
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(util.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(util.post_json("http://b", {}, token="t"), {"v": 1})

    def test_unreachable_backend_raises_after_retries(self) -> None:
        def fake_urlopen(request, timeout):
            raise urllib.error.URLError("connection refused")

        with mock.patch.object(util.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(util.ApiError) as ctx:
                util.post_json("http://b", {}, token="t", label="/api/x", retries=2)
        self.assertIn("unreachable", str(ctx.exception))
        self.assertIsNone(ctx.exception.status)

    def test_non_json_error_body_is_used_as_the_detail(self) -> None:
        def fake_urlopen(request, timeout):
            raise http_error(400, "plain text problem")

        with mock.patch.object(util.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(util.ApiError) as ctx:
                util.post_json("http://b", {}, token="t")
        self.assertEqual(ctx.exception.detail, "plain text problem")


if __name__ == "__main__":
    unittest.main()
