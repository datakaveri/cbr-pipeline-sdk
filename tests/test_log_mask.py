"""Tests for pipeline_sdk.log_mask (stdlib unittest; run from packages/pipeline-sdk-py).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import textwrap
import unittest
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline_sdk import env, log_mask  # noqa: E402
from pipeline_sdk.log_mask import REDACTED, MaskingStream  # noqa: E402

SECRET = "sk-live-Abc123-xyz"


def stream_pair() -> tuple[MaskingStream, io.StringIO]:
    sink = io.StringIO()
    return MaskingStream(sink), sink


class MaskTests(unittest.TestCase):
    def setUp(self) -> None:
        log_mask._reset()

    def tearDown(self) -> None:
        log_mask._reset()

    def test_plain_value_is_masked(self) -> None:
        log_mask.register(SECRET)
        self.assertEqual(log_mask.mask(f"key={SECRET}!"), f"key={REDACTED}!")

    def test_passes_through_untouched_when_nothing_registered(self) -> None:
        stream, sink = stream_pair()
        stream.write("hello world\n")
        self.assertEqual(sink.getvalue(), "hello world\n")

    def test_value_in_a_print_is_masked(self) -> None:
        log_mask.register(SECRET)
        stream, sink = stream_pair()
        print(f"the value is {SECRET}", file=stream, flush=True)
        self.assertEqual(sink.getvalue(), f"the value is {REDACTED}\n")
        self.assertNotIn(SECRET, sink.getvalue())

    def test_value_split_across_two_writes_is_masked(self) -> None:
        log_mask.register(SECRET)
        stream, sink = stream_pair()
        stream.write("token: " + SECRET[:7])
        stream.write(SECRET[7:] + " done\n")
        self.assertEqual(sink.getvalue(), f"token: {REDACTED} done\n")

    def test_value_split_across_many_one_char_writes(self) -> None:
        log_mask.register(SECRET)
        stream, sink = stream_pair()
        for ch in f"x {SECRET} y\n":
            stream.write(ch)
        self.assertEqual(sink.getvalue(), f"x {REDACTED} y\n")

    def test_multiline_secret_is_masked_across_writes(self) -> None:
        pem = "-----BEGIN KEY-----\nabcdef123456\n-----END KEY-----"
        log_mask.register(pem)
        stream, sink = stream_pair()
        stream.write(pem.split("\n")[0] + "\n")
        stream.write(pem.split("\n", 1)[1] + "\n")
        self.assertEqual(sink.getvalue(), f"{REDACTED}\n")

    def test_flush_does_not_leak_a_partial_value(self) -> None:
        log_mask.register(SECRET)
        stream, sink = stream_pair()
        stream.write("a " + SECRET[:6])
        stream.flush()
        self.assertNotIn(SECRET[:6], sink.getvalue())
        stream.write(SECRET[6:] + "\n")
        self.assertEqual(sink.getvalue(), f"a {REDACTED}\n")

    def test_held_back_text_that_never_completes_is_released_at_exit(self) -> None:
        log_mask.register(SECRET)
        stream, sink = stream_pair()
        stream.write("progress sk-live")
        self.assertEqual(sink.getvalue(), "progress ")
        stream.flush_all()
        self.assertEqual(sink.getvalue(), "progress sk-live")

    def test_common_encodings_are_masked(self) -> None:
        log_mask.register(SECRET)
        raw = SECRET.encode()
        forms = [
            base64.b64encode(raw).decode(),
            base64.urlsafe_b64encode(raw).decode(),
            base64.b64encode(raw).decode().rstrip("="),
            quote(SECRET, safe=""),
            json.dumps(SECRET)[1:-1],
        ]
        for form in forms:
            with self.subTest(form=form):
                self.assertNotIn(form, log_mask.mask(f"x {form} y"))

    def test_escaped_form_of_a_value_with_quotes_is_masked(self) -> None:
        tricky = 'pa"ss\\word-123'
        log_mask.register(tricky)
        masked = log_mask.mask(json.dumps({"p": tricky}))
        self.assertNotIn(json.dumps(tricky)[1:-1], masked)
        self.assertIn(REDACTED, masked)

    def test_values_shorter_than_the_minimum_are_ignored(self) -> None:
        log_mask.register("abc")
        self.assertEqual(log_mask.mask("abc abc"), "abc abc")

    def test_longer_value_wins_over_a_shorter_one_it_contains(self) -> None:
        log_mask.register("secret")
        log_mask.register("secret-with-suffix")
        self.assertEqual(log_mask.mask("a secret-with-suffix b"), f"a {REDACTED} b")

    def test_non_string_is_ignored(self) -> None:
        log_mask.register(None)
        log_mask.register(12345678)
        self.assertEqual(log_mask.mask("12345678"), "12345678")

    def test_wrapped_stream_attributes_still_work(self) -> None:
        stream, sink = stream_pair()
        self.assertTrue(stream.writable())
        stream.writelines(["a\n", "b\n"])
        self.assertEqual(sink.getvalue(), "a\nb\n")

    def test_bytes_writes_are_passed_through(self) -> None:
        raw = io.BytesIO()
        stream = MaskingStream(raw)
        log_mask.register(SECRET)
        stream.write(b"bytes")
        self.assertEqual(raw.getvalue(), b"bytes")


class GetSecretTests(unittest.TestCase):
    ENV = {"BACKEND_URL": "http://backend", "API_KEY": "k"}

    def setUp(self) -> None:
        log_mask._reset()

    def tearDown(self) -> None:
        log_mask._reset()

    def test_get_secret_registers_the_value_by_default(self) -> None:
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(
            env.util, "post_json", return_value={"value": SECRET}
        ):
            self.assertEqual(env.get_secret("API_KEY"), SECRET)
        self.assertEqual(log_mask.mask(SECRET), REDACTED)

    def test_get_secret_has_no_opt_out(self) -> None:
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(
            env.util, "post_json", return_value={"value": SECRET}
        ):
            with self.assertRaises(TypeError):
                env.get_secret("API_KEY", mask=False)  # type: ignore[call-arg]


class InstallTests(unittest.TestCase):
    def test_install_is_idempotent_and_wraps_both_streams(self) -> None:
        log_mask.install()
        first_out, first_err = sys.stdout, sys.stderr
        log_mask.install()
        self.assertIs(sys.stdout, first_out)
        self.assertIs(sys.stderr, first_err)
        self.assertIsInstance(sys.stdout, MaskingStream)
        self.assertIsInstance(sys.stderr, MaskingStream)


class RealProcessTests(unittest.TestCase):
    """Run an actual interpreter, so stdout/stderr, tracebacks and exit are real."""

    SCRIPT = textwrap.dedent(
        """
        import os, sys
        from unittest import mock
        from pipeline_sdk import env
        os.environ["BACKEND_URL"] = "http://backend"
        os.environ["API_KEY"] = "k"
        with mock.patch.object(env.util, "post_json", return_value={"value": "%(secret)s"}):
            value = env.get_secret("API_KEY")
        print("plain: " + value, flush=True)
        print("to stderr: " + value, file=sys.stderr, flush=True)
        sys.stdout.write("partial " + value[:5])
        sys.stdout.write(value[5:] + "\\n")
        raise RuntimeError("boom " + value)
        """
    ) % {"secret": SECRET}

    def test_nothing_reaches_the_process_output_unmasked(self) -> None:
        root = os.path.join(os.path.dirname(__file__), "..")
        proc = subprocess.run(
            [sys.executable, "-c", self.SCRIPT],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        combined = proc.stdout + proc.stderr
        self.assertNotIn(SECRET, combined)
        self.assertIn(f"plain: {REDACTED}", proc.stdout)
        self.assertIn(f"to stderr: {REDACTED}", proc.stderr)
        self.assertIn(f"partial {REDACTED}", proc.stdout)
        self.assertIn(f"RuntimeError: boom {REDACTED}", proc.stderr)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
