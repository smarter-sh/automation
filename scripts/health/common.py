#!/usr/bin/env python3
"""What every organization health scan shares: its results, the repository list, and reading files.

Part of :mod:`health.organization_health`, which runs it; see that module for the scans and how
they are run. It uses only Python's standard library and ``gh``.
"""

import base64
import re
import subprocess  # nosec B404: only for CalledProcessError
from typing import TypeGuard

from github_cli import ORGANIZATION, api_object, api_pages, http_status

DISPLAY_NAMES = {
    "security": "Repository security settings",
    "workflow": "Workflow security hygiene",
    "packages": "Published package health",
    "stale": "Stale pull requests and issues",
}


WORKFLOW_PATH_PATTERN = r"\.github/workflows/[^/]+\.ya?ml"


def new_results(kind: str) -> dict:
    """Create the common results document.

    :param kind: The scan name, as a key in :data:`DISPLAY_NAMES`.
    :returns: An empty results mapping for that scan.
    """
    return {"kind": kind, "repositories_scanned": 0, "findings": [], "errors": {}}


def record_error(results: dict, category: str, error: Exception | None = None) -> None:
    """Count API errors without exposing raw output in the report.

    :param results: The mutable scan results.
    :param category: A safe category describing the failed request.
    :param error: The optional API or response error.
    """
    if isinstance(error, subprocess.CalledProcessError):
        status = http_status(error)
    else:
        status = "unavailable"
    key = f"{category} ({status})"
    errors = results["errors"]
    errors[key] = errors.get(key, 0) + 1


def add_finding(results: dict, repository: str, category: str, detail: str, url: str, path: str = "") -> None:
    """Record a finding for the detailed Slack report.

    :param results: The mutable scan results.
    :param repository: The repository's ``owner/name`` identifier.
    :param category: The kind of finding.
    :param detail: A short description of the issue.
    :param url: The related GitHub URL.
    :param path: The optional repository-relative file path.
    """
    results["findings"].append(
        {"repository": repository, "category": category, "detail": detail, "url": url, "path": path}
    )


def list_repositories(results: dict) -> list[dict]:
    """List every organization repository, including archived repositories.

    :param results: The mutable scan results, whose repository count is updated.
    :returns: Repository metadata, or an empty list after a recorded API failure.
    """
    try:
        repositories = api_pages(f"orgs/{ORGANIZATION}/repos?type=all")
    except (subprocess.CalledProcessError, ValueError) as error:
        record_error(results, "Repository inventory", error)
        return []
    results["repositories_scanned"] = len(repositories)
    return repositories


def is_organization_repository(repository: object) -> TypeGuard[str]:
    """Check that an API-provided repository name belongs to the organization.

    :param repository: The ``full_name`` field of a repository.
    :returns: Whether it is an ``owner/name`` string in :data:`ORGANIZATION`.
    """
    if not isinstance(repository, str):
        return False
    return repository.startswith(f"{ORGANIZATION}/")


def is_branch_name(branch: object) -> TypeGuard[str]:
    """Check that an API-provided default branch is a non-empty string.

    :param branch: The ``default_branch`` field of a repository.
    :returns: Whether it can be used as a branch name.
    """
    return isinstance(branch, str) and branch != ""


def decode_content(response: dict) -> str:
    """Decode a GitHub contents API response.

    :param response: The JSON object returned by the contents endpoint.
    :returns: The file's decoded UTF-8 text.
    :raises ValueError: If the object has no content field.
    :raises UnicodeDecodeError: If the content is not UTF-8.
    """
    content = response.get("content")
    if not isinstance(content, str):
        raise ValueError("GitHub file response has no content")
    decoded = base64.b64decode(content)
    return decoded.decode("utf-8")


def file_text(endpoint: str) -> str:
    """Read one file's text from a GitHub contents endpoint.

    :param endpoint: The contents endpoint, with the path already URL-encoded.
    :returns: The file's decoded UTF-8 text.
    :raises subprocess.CalledProcessError: If GitHub cannot return the file.
    :raises ValueError: If the contents response has an unexpected shape.
    :raises UnicodeDecodeError: If the file content is not UTF-8.
    """
    response = api_object(endpoint)
    return decode_content(response)


def workflow_paths(repository: str, branch_ref: str) -> list[str]:
    """List the workflow files on a branch.

    :param repository: The repository's ``owner/name`` identifier.
    :param branch_ref: The branch name, URL-encoded.
    :returns: The repository-relative paths of ``.github/workflows/*.yml`` and ``*.yaml``.
    :raises subprocess.CalledProcessError: If GitHub cannot return the tree.
    :raises ValueError: If GitHub returns a non-object response.
    """
    tree = api_object(f"repos/{repository}/git/trees/{branch_ref}?recursive=1")
    paths = []
    for node in tree.get("tree", []):
        if not isinstance(node, dict):
            continue
        path = node.get("path")
        if not isinstance(path, str):
            continue
        if re.fullmatch(WORKFLOW_PATH_PATTERN, path):
            paths.append(path)
    return paths


def finding_sort_key(finding: dict) -> tuple[str, str, str]:
    """Order findings by repository, then category, then description.

    :param finding: One finding from :func:`add_finding`.
    :returns: The sort key.
    """
    return finding["repository"], finding["category"], finding["detail"]
