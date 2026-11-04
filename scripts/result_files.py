#!/usr/bin/env python3
"""Save the results of a workflow's step as JSON, and read them back in a later step or job.

A job's ``run`` step saves its results, and its ``report`` step, or a ``report`` job that downloads
every job's artifact, reads them back. The results are dataclasses, e.g. one ``Result`` per
repository.

When a file is read, the keys that the dataclass doesn't have are ignored, so that a file written
by an older or newer version of a script can still be read.

It uses only Python's standard library, because the workflows' runners have no virtual environment.
"""

import json
from dataclasses import asdict, fields
from pathlib import Path


def save_result(result, path: str | Path) -> None:
    """
    Write one result as indented JSON, replacing the file if it exists.

    :param result: A dataclass instance.
    :param path: The file to write.
    """
    data = asdict(result)
    text = json.dumps(data, indent=2)
    Path(path).write_text(text + "\n", encoding="utf-8")


def save_results(results: list, path: str | Path) -> None:
    """
    Write a list of results as indented JSON, replacing the file if it exists.

    :param results: Dataclass instances, all of one class.
    :param path: The file to write.
    """
    items = [asdict(result) for result in results]
    text = json.dumps(items, indent=2)
    Path(path).write_text(text + "\n", encoding="utf-8")


def from_dict(result_class, data: dict):
    """
    Build a result from a dict, leaving out the keys its class doesn't have.

    :param result_class: The dataclass, e.g. ``Result``.
    :param data: The values, as :func:`save_result` wrote them.
    :returns: The result.
    :raises TypeError: When ``data`` lacks a field that has no default, e.g. ``repository``.
    """
    known_names = {known.name for known in fields(result_class)}
    values = {}
    for key, value in data.items():
        if key in known_names:
            values[key] = value
    return result_class(**values)


def load_result(result_class, path: str | Path):
    """
    Read one result that :func:`save_result` wrote.

    :param result_class: The result's dataclass.
    :param path: The file.
    :returns: The result.
    :raises OSError: When the file can't be read.
    :raises ValueError: When it isn't JSON.
    :raises TypeError: When it isn't an object, or lacks a required field.
    """
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise TypeError(f"{path} doesn't hold one result")
    return from_dict(result_class, data)


def load_results(result_class, path: str | Path) -> list | None:
    """
    Read a list of results that :func:`save_results` wrote.

    :param result_class: The results' dataclass.
    :param path: The file.
    :returns: The results, or None when the file is missing or isn't JSON.
    :raises TypeError: When an item lacks a required field.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
        items = json.loads(text)
    except (OSError, ValueError):
        return None
    results = []
    for item in items:
        results.append(from_dict(result_class, item))
    return results


def load_result_folder(result_class, directory: str | Path) -> list:
    """
    Read every result that the jobs of a matrix uploaded, and a ``report`` job downloaded.

    A file that isn't a result, e.g. one cut short by a cancelled job, is skipped: the report
    covers the results it has.

    :param result_class: The results' dataclass. It must have a ``repository`` field.
    :param directory: The folder, searched recursively for ``*.json`` files.
    :returns: The results, sorted by repository.
    """
    paths = sorted(Path(directory).rglob("*.json"))
    results = []
    for path in paths:
        try:
            result = load_result(result_class, path)
        except (OSError, ValueError, TypeError):
            continue
        results.append(result)
    return sorted(results, key=lambda result: result.repository)
