"""Tests for env's lookups and for storage's use of env/util (stdlib unittest)."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline_sdk import env, log_mask, storage, util  # noqa: E402

NODE_CONTEXT = json.dumps({
    "node": {"name": "n", "slug": "s"},
    "inputs": [{"output": {"path": "s3://no-code-upload/user-from-input/file.csv"}}],
    "output": {"files": [{"name": "r", "path": "s3://no-code-artifact/user-from-output/p/e/n/r.json"}]},
    "config": {},
})


def clean_env(**extra: str):
    """A patch that removes every variable these tests care about, then sets `extra`."""
    keys = ("BACKEND_URL", "API_KEY", "NODE_CONTEXT", "PIPELINE_USER_ID", "ARTIFACT_S3_BUCKET", "INPUT_S3_BUCKET")
    base = {k: v for k, v in os.environ.items() if k not in keys}
    base.update(extra)
    return mock.patch.dict(os.environ, base, clear=True)


class UserIdTests(unittest.TestCase):
    def test_explicit_variable_wins(self) -> None:
        with clean_env(PIPELINE_USER_ID="explicit", NODE_CONTEXT=NODE_CONTEXT):
            self.assertEqual(env.user_id(), "explicit")

    def test_recovered_from_a_declared_output_path(self) -> None:
        with clean_env(NODE_CONTEXT=NODE_CONTEXT):
            self.assertEqual(env.user_id(), "user-from-output")

    def test_recovered_from_an_input_path_when_there_is_no_output(self) -> None:
        ctx = json.dumps({"inputs": [{"output": {"path": "s3://b/user-from-input/f"}}], "output": {"files": []}})
        with clean_env(NODE_CONTEXT=ctx):
            self.assertEqual(env.user_id(), "user-from-input")

    def test_unknown_user_raises_env_error(self) -> None:
        with clean_env():
            with self.assertRaises(env.EnvError):
                env.user_id()
        with clean_env(NODE_CONTEXT="not json"):
            with self.assertRaises(env.EnvError):
                env.user_id()


class BucketTests(unittest.TestCase):
    def test_bucket_accessors(self) -> None:
        with clean_env(ARTIFACT_S3_BUCKET="art", INPUT_S3_BUCKET="inp"):
            self.assertEqual(env.artifact_bucket(), "art")
            self.assertEqual(env.input_bucket(), "inp")
        with clean_env():
            self.assertIsNone(env.artifact_bucket())
            self.assertIsNone(env.input_bucket())

    def test_require_artifact_bucket_names_the_missing_variable(self) -> None:
        with clean_env(ARTIFACT_S3_BUCKET="art"):
            self.assertEqual(env.require_artifact_bucket(), "art")
        with clean_env():
            with self.assertRaisesRegex(env.EnvError, "ARTIFACT_S3_BUCKET"):
                env.require_artifact_bucket()

    def test_s3_for_path_picks_the_credential_set_by_bucket(self) -> None:
        env_vars = {}
        for prefix, bucket in (("ARTIFACT", "art"), ("INPUT", "inp")):
            env_vars.update({
                f"{prefix}_S3_BUCKET": bucket,
                f"{prefix}_S3_ENDPOINT": "host:9000",
                f"{prefix}_S3_ACCESS_KEY": "ak",
                f"{prefix}_S3_SECRET_KEY": "sk",
            })
        with clean_env(**env_vars):
            self.assertEqual(env.s3_for_path("s3://art/u/x").prefix, "ARTIFACT")
            self.assertEqual(env.s3_for_path("s3://inp/u/x").prefix, "INPUT")


class GetSecretTests(unittest.TestCase):
    def setUp(self) -> None:
        log_mask._reset()
        self.addCleanup(log_mask._reset)

    def run_with(self, side_effect=None, return_value=None):
        patch = mock.patch.object(env.util, "post_json", side_effect=side_effect, return_value=return_value)
        return patch

    def test_calls_the_secret_endpoint_with_the_key(self) -> None:
        with clean_env(BACKEND_URL="http://backend/", API_KEY="the-key"):
            with self.run_with(return_value={"value": "s3cret-value"}) as post:
                self.assertEqual(env.get_secret("NAME"), "s3cret-value")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://backend/api/node/secret/get")
        self.assertEqual(args[1], {"name": "NAME"})
        self.assertEqual(kwargs["token"], "the-key")

    def test_missing_secret_is_a_clear_env_error(self) -> None:
        with clean_env(BACKEND_URL="http://b", API_KEY="k"):
            with self.run_with(side_effect=util.ApiError("x", status=404)):
                with self.assertRaisesRegex(env.EnvError, "No secret named 'NAME'"):
                    env.get_secret("NAME")

    def test_server_error_names_the_secret(self) -> None:
        with clean_env(BACKEND_URL="http://b", API_KEY="k"):
            with self.run_with(side_effect=util.ApiError("/api/node/secret/get failed (500): Internal error", status=500)):
                with self.assertRaisesRegex(env.EnvError, "Could not fetch secret 'NAME'.*500"):
                    env.get_secret("NAME")

    def test_missing_configuration_is_an_env_error_not_a_network_call(self) -> None:
        with clean_env(API_KEY="k"):
            with self.run_with() as post:
                with self.assertRaises(env.EnvError):
                    env.get_secret("NAME")
        post.assert_not_called()
        with clean_env(BACKEND_URL="http://b"):
            with self.run_with() as post:
                with self.assertRaises(env.EnvError):
                    env.get_secret("NAME")
        post.assert_not_called()


class StorageUsesEnvTests(unittest.TestCase):
    def test_post_without_configuration_raises_storage_error(self) -> None:
        with clean_env():
            with self.assertRaises(storage.StorageError):
                storage._post("/api/node/storage/x", {})

    def test_post_sends_the_key_from_env_to_the_backend_url_from_env(self) -> None:
        with clean_env(BACKEND_URL="http://backend/", API_KEY="the-key"):
            with mock.patch.object(storage.util, "post_json", return_value={"ok": 1}) as post:
                self.assertEqual(storage._post("/api/node/storage/x", {"a": 1}), {"ok": 1})
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://backend/api/node/storage/x")
        self.assertEqual(kwargs["token"], "the-key")

    def test_quota_error_is_mapped(self) -> None:
        err = util.ApiError("x", status=403, detail="storage limit reached")
        with clean_env(BACKEND_URL="http://b", API_KEY="k"):
            with mock.patch.object(storage.util, "post_json", side_effect=err):
                with self.assertRaises(storage.StorageQuotaExceeded) as ctx:
                    storage._post("/api/node/storage/x", {})
        self.assertEqual(ctx.exception.status, 403)

    def test_gone_error_is_mapped(self) -> None:
        err = util.ApiError("x", status=410, detail="moved")
        with clean_env(BACKEND_URL="http://b", API_KEY="k"):
            with mock.patch.object(storage.util, "post_json", side_effect=err):
                with self.assertRaisesRegex(storage.StorageError, "mismatched"):
                    storage._post("/api/node/storage/x", {})

    def test_other_errors_keep_their_status(self) -> None:
        err = util.ApiError("/e failed (404): nope", status=404, detail="nope")
        with clean_env(BACKEND_URL="http://b", API_KEY="k"):
            with mock.patch.object(storage.util, "post_json", side_effect=err):
                with self.assertRaises(storage.StorageError) as ctx:
                    storage._post("/e", {})
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(str(ctx.exception), "/e failed (404): nope")

    def test_storage_errors_are_api_errors(self) -> None:
        self.assertTrue(issubclass(storage.StorageError, util.ApiError))
        self.assertTrue(issubclass(storage.StorageQuotaExceeded, storage.StorageError))

    def test_read_paths_use_the_bucket_names_from_env(self) -> None:
        with clean_env(ARTIFACT_S3_BUCKET="art", INPUT_S3_BUCKET="inp", PIPELINE_USER_ID="u1"):
            self.assertEqual(storage._normalize_read_path("s3://art/u1/x"), "s3://art/u1/x")
            self.assertEqual(storage._normalize_read_path("art/u1/x"), "art/u1/x")
            self.assertEqual(storage._normalize_read_path("flow/exec/file"), "u1/flow/exec/file")


if __name__ == "__main__":
    unittest.main()
