"""Tests of scripts/result_files.py, which saves the workflows' results as JSON and reads them back."""

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

import result_files


@dataclass
class Result:
    """A result, as the scripts define theirs."""

    repository: str
    status: str = "unknown"


class TestOneResult(unittest.TestCase):
    """Save_result() and load_result() round-trip one result, and ignore unknown keys."""

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            result_files.save_result(Result("smarter-sh/a", "current"), path)
            loaded = result_files.load_result(Result, path)
        self.assertEqual(loaded, Result("smarter-sh/a", "current"))

    def test_unknown_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text('{"repository": "smarter-sh/a", "newer_field": 1}', encoding="utf-8")
            loaded = result_files.load_result(Result, path)
        self.assertEqual(loaded, Result("smarter-sh/a"))

    def test_not_an_object(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(TypeError):
                result_files.load_result(Result, path)


class TestResultList(unittest.TestCase):
    """Save_results() and load_results() round-trip a list; a missing or broken file reads as None."""

    def test_round_trip(self):
        results = [Result("smarter-sh/a"), Result("smarter-sh/b", "failure")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            result_files.save_results(results, path)
            loaded = result_files.load_results(Result, path)
        self.assertEqual(loaded, results)

    def test_missing_or_broken(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            broken = Path(directory) / "broken.json"
            broken.write_text("{", encoding="utf-8")
            self.assertIsNone(result_files.load_results(Result, missing))
            self.assertIsNone(result_files.load_results(Result, broken))


class TestResultFolder(unittest.TestCase):
    """Load_result_folder() reads every result under a folder, sorted, and skips the rest."""

    def test_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "b").mkdir()
            result_files.save_result(Result("smarter-sh/b"), folder / "b" / "result.json")
            result_files.save_result(Result("smarter-sh/a"), folder / "a.json")
            (folder / "cut-short.json").write_text("{", encoding="utf-8")
            (folder / "no-repository.json").write_text("{}", encoding="utf-8")
            loaded = result_files.load_result_folder(Result, folder)
        repositories = [result.repository for result in loaded]
        self.assertEqual(repositories, ["smarter-sh/a", "smarter-sh/b"])


if __name__ == "__main__":
    unittest.main()
