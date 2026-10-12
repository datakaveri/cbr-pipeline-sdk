"""The SDK's layering rules, checked on the source (stdlib unittest).

    util  ->  env  ->  storage  ->  context  /  boto3_compat

Each module may import only the modules to its left, and `env` is the only
module that reads the environment.
"""

from __future__ import annotations

import ast
import os
import unittest

PKG = os.path.join(os.path.dirname(__file__), "..", "pipeline_sdk")

ALLOWED_SDK_IMPORTS = {
    "util": set(),
    "log_mask": set(),
    "env": {"util", "log_mask"},
    "storage": {"util", "env"},
    "context": {"util", "env", "storage"},
    "boto3_compat": {"env", "storage"},
}


def parse(module: str) -> ast.AST:
    with open(os.path.join(PKG, f"{module}.py"), encoding="utf-8") as f:
        return ast.parse(f.read())


def sdk_imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                found.add(node.module.split(".")[0])
            else:
                found.update(alias.name for alias in node.names)
    return found


def reads_environment(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
            if node.attr in ("environ", "getenv", "environb"):
                return True
    return False


class LayeringTests(unittest.TestCase):
    def test_modules_only_import_the_layers_below_them(self) -> None:
        for module, allowed in ALLOWED_SDK_IMPORTS.items():
            with self.subTest(module=module):
                extra = sdk_imports(parse(module)) - allowed
                self.assertFalse(extra, f"{module}.py must not import {sorted(extra)}")

    def test_only_env_reads_the_environment(self) -> None:
        for module in ALLOWED_SDK_IMPORTS:
            with self.subTest(module=module):
                expected = module == "env"
                self.assertEqual(reads_environment(parse(module)), expected,
                                 f"{module}.py {'must' if expected else 'must not'} read os.environ")

    def test_env_does_not_depend_on_storage(self) -> None:
        self.assertNotIn("storage", sdk_imports(parse("env")))


if __name__ == "__main__":
    unittest.main()
