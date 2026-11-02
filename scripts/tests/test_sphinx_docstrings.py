"""Check that the scripts' public modules and functions use Sphinx field-list docstrings."""

import ast
import re
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]


class TestSphinxDocstrings(unittest.TestCase):
    """Every production script has module documentation and documented function fields."""

    def test_module_docstrings(self):
        for path in sorted(SCRIPTS.glob("*.py")):
            with self.subTest(module=path.name):
                module = ast.parse(path.read_text(encoding="utf-8"))
                self.assertTrue(ast.get_docstring(module), f"{path.name} has no module docstring")

    def test_function_parameters_and_returns(self):
        for path in sorted(SCRIPTS.glob("*.py")):
            module = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(module):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                with self.subTest(module=path.name, function=node.name):
                    docstring = ast.get_docstring(node)
                    self.assertTrue(docstring, f"{node.name} has no function docstring")
                    parameters = [
                        argument.arg
                        for argument in [
                            *node.args.posonlyargs,
                            *node.args.args,
                            *node.args.kwonlyargs,
                        ]
                        if argument.arg not in ("self", "cls")
                    ]
                    for parameter in parameters:
                        self.assertRegex(docstring, rf":param\s+{re.escape(parameter)}\s*:")
                    returns_value = node.returns is not None and not (
                        isinstance(node.returns, ast.Constant) and node.returns.value is None
                    )
                    if returns_value:
                        self.assertIn(":returns:", docstring)


if __name__ == "__main__":
    unittest.main()
