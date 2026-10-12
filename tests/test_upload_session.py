"""Uploads and the boto3 adapter against a fake storage backend on a real socket
(stdlib unittest; run from packages/pipeline-sdk-py).

The fake backend implements the multipart-only upload-session API: create /
create-session / part-urls / complete / abort / download-url, plus the part PUT
and the file GET. Parts are 8 bytes so small payloads still span several parts.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline_sdk import boto3_compat, env, storage, util  # noqa: E402

CHUNK = 8
USER = "user-1"


class Backend:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.next_id = 1
        self.sessions: dict[int, dict] = {}
        self.files: dict[str, bytes] = {}
        self.created_paths: list[str] = []
        self.calls: list[str] = []
        self.fail_once: str | None = None  # endpoint that answers 404 once, dropping the session


BACKEND = Backend()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self) -> None:
        _, _, session, part = self.path.split("/")
        data = self.rfile.read(int(self.headers["Content-Length"]))
        BACKEND.sessions[int(session)]["parts"][int(part)] = data
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        key = self.path.removeprefix("/get/")
        data = BACKEND.files.get(key) or BACKEND.files.get(key.split("/", 1)[1])  # "{bucket}/{key}"
        if data is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        endpoint = self.path.removeprefix("/api/node/storage/")
        BACKEND.calls.append(endpoint)
        base = f"http://127.0.0.1:{self.server.server_port}"

        if endpoint in ("create", "create-session"):
            session = BACKEND.next_id
            BACKEND.next_id += 1
            BACKEND.sessions[session] = {"path": body["path"], "parts": {}}
            BACKEND.created_paths.append(body["path"])
            return self.reply(
                200, {"sessionId": session, "declaredChunkBytes": CHUNK, "checksumsEnabled": False, "partUrls": []}
            )

        if endpoint == "download-url":
            return self.reply(200, {"url": f"{base}/get/{body['path'].removeprefix('s3://')}"})

        session = body.get("sessionId")
        if BACKEND.fail_once == endpoint:
            BACKEND.fail_once = None
            BACKEND.sessions.pop(session, None)
        if session not in BACKEND.sessions:
            return self.reply(404, {"error": "session not found"})

        if endpoint == "part-urls":
            return self.reply(
                200,
                {"partUrls": [{"partNumber": p["partNumber"], "url": f"{base}/put/{session}/{p['partNumber']}"} for p in body["parts"]]},
            )
        if endpoint == "complete":
            state = BACKEND.sessions.pop(session)
            data = b"".join(state["parts"][n] for n in sorted(state["parts"]))
            BACKEND.files[state["path"]] = data
            return self.reply(200, {"path": state["path"], "sizeBytes": len(data)})
        if endpoint == "abort":
            BACKEND.sessions.pop(session, None)
            return self.reply(200, {})
        self.reply(404, {"error": "unknown endpoint"})


class UploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        BACKEND.reset()
        patch = mock.patch.dict(
            os.environ,
            {"BACKEND_URL": self.url, "API_KEY": "k", "PIPELINE_USER_ID": USER, "ARTIFACT_S3_BUCKET": "artifacts"},
        )
        patch.start()
        self.addCleanup(patch.stop)
        delay = mock.patch.object(util, "RETRY_DELAY_S", 0)
        delay.start()
        self.addCleanup(delay.stop)

    # -- paths ---------------------------------------------------------------
    def test_write_paths_are_normalised_to_the_users_prefix(self) -> None:
        for given in (
            "results/out.bin",
            "/results/out.bin",
            f"{USER}/results/out.bin",
            f"s3://artifacts/{USER}/results/out.bin",
        ):
            with self.subTest(path=given):
                BACKEND.reset()
                storage.write_bytes(given, b"x")
                self.assertEqual(BACKEND.created_paths, [f"{USER}/results/out.bin"])

    # -- known-size and streaming writes ---------------------------------------
    def test_write_bytes_spans_several_parts_and_round_trips(self) -> None:
        data = bytes(range(20))
        storage.write_bytes("a/b.bin", data)
        self.assertEqual(BACKEND.files[f"{USER}/a/b.bin"], data)
        self.assertEqual(storage.read(f"s3://artifacts/{USER}/a/b.bin"), data)

    def test_empty_file_is_a_single_empty_part(self) -> None:
        storage.write_bytes("empty.bin", b"")
        self.assertEqual(BACKEND.files[f"{USER}/empty.bin"], b"")

    def test_streaming_write_commits_on_clean_close(self) -> None:
        with storage.open_write("s/out.bin") as out:
            for piece in (b"abc", b"defghijk", b"lmnopqrstuv"):
                out.write(piece)
        self.assertEqual(BACKEND.files[f"{USER}/s/out.bin"], b"abcdefghijklmnopqrstuv")

    def test_an_exception_inside_the_writer_aborts_instead_of_committing(self) -> None:
        with self.assertRaises(RuntimeError):
            with storage.open_write("s/never.bin") as out:
                out.write(b"partial data")
                raise RuntimeError("node crashed")
        self.assertNotIn(f"{USER}/s/never.bin", BACKEND.files)
        self.assertIn("abort", BACKEND.calls)

    def test_upload_file_reads_a_local_file(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"0123456789abcdefXYZ")
        self.addCleanup(os.unlink, handle.name)
        storage.upload_file("up/file.bin", handle.name)
        self.assertEqual(BACKEND.files[f"{USER}/up/file.bin"], b"0123456789abcdefXYZ")

    # -- session vanishes mid-upload ----------------------------------------
    def test_a_session_lost_before_a_part_url_is_replayed_into_a_fresh_one(self) -> None:
        BACKEND.fail_once = "part-urls"
        data = bytes(range(30))
        with storage.open_write("rot/out.bin") as out:
            out.write(data)
        self.assertEqual(BACKEND.files[f"{USER}/rot/out.bin"], data)
        self.assertEqual(len(BACKEND.created_paths), 2)  # the original and the replacement session

    def test_a_session_lost_at_commit_is_replayed_into_a_fresh_one(self) -> None:
        BACKEND.fail_once = "complete"
        data = bytes(range(21))
        with storage.open_write("rot/commit.bin") as out:
            out.write(data)
        self.assertEqual(BACKEND.files[f"{USER}/rot/commit.bin"], data)
        self.assertEqual(len(BACKEND.created_paths), 2)

    def test_known_size_upload_survives_a_lost_session_too(self) -> None:
        BACKEND.fail_once = "complete"
        data = bytes(range(25))
        storage.write_bytes("rot/known.bin", data)
        self.assertEqual(BACKEND.files[f"{USER}/rot/known.bin"], data)

    def test_a_second_lost_session_is_not_retried_forever(self) -> None:
        BACKEND.fail_once = "complete"
        original = Handler.do_POST

        def always_drop_complete(handler: Handler) -> None:
            BACKEND.fail_once = "complete" if BACKEND.fail_once is None else BACKEND.fail_once
            original(handler)

        with mock.patch.object(Handler, "do_POST", always_drop_complete):
            with self.assertRaises(storage.StorageError):
                storage.write_bytes("rot/twice.bin", b"0123456789")

    # -- boto3 adapter -------------------------------------------------------
    def test_boto3_adapter_writes_reads_and_copies_through_storage(self) -> None:
        client = boto3_compat.client("s3", endpoint_url="ignored")
        client.put_object(Bucket="artifacts", Key=f"{USER}/p/one.bin", Body=b"hello world, adapter")
        self.assertEqual(BACKEND.files[f"{USER}/p/one.bin"], b"hello world, adapter")

        body = client.get_object(Bucket="artifacts", Key=f"{USER}/p/one.bin")["Body"].read()
        self.assertEqual(body, b"hello world, adapter")

        client.copy_object(Bucket="artifacts", Key=f"{USER}/p/two.bin", CopySource={"Bucket": "artifacts", "Key": f"{USER}/p/one.bin"})
        self.assertEqual(BACKEND.files[f"{USER}/p/two.bin"], b"hello world, adapter")

        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "down.bin")
            client.download_file("artifacts", f"{USER}/p/two.bin", target)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"hello world, adapter")

    def test_boto3_adapter_sets_inert_s3_placeholders_only_where_unset(self) -> None:
        with mock.patch.dict(os.environ, {"INPUT_S3_ENDPOINT": "real:9000"}, clear=False):
            for name in ("ARTIFACT_S3_ENDPOINT", "INPUT_S3_ACCESS_KEY"):
                os.environ.pop(name, None)
            env.use_placeholder_s3_credentials()
            self.assertEqual(os.environ["INPUT_S3_ENDPOINT"], "real:9000")
            self.assertEqual(os.environ["ARTIFACT_S3_ENDPOINT"], "storage-v2")
            self.assertEqual(os.environ["INPUT_S3_ACCESS_KEY"], "backend-mediated")


if __name__ == "__main__":
    unittest.main()
