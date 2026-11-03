#!/usr/bin/env python3
"""Report repository security, workflow, package, and stale-item health.

The four scans read metadata from every repository in the ``smarter-sh`` organization:

``security``
    Check Actions token defaults, branch protection, secret scanning, and Dependabot settings.
``workflow``
    Check workflow files for full-SHA action pins, explicit token permissions, and job timeouts.
``packages``
    Discover npm, PyPI, and Go modules from package manifests and release workflows; compare
    stable versions and attempt a package install or module download.
``stale``
    Report open pull requests and issues older than a calendar-month threshold.

The scans are read-only apart from installing packages into temporary runner directories.
Findings are saved as JSON for the workflow summary and Slack report. The public Actions summary
contains counts only; detailed repository and item information is written only to Slack.

Run a scan from the repository root::

    python3 -E -s scripts/organization_health.py run security --results results.json
    python3 -E -s scripts/organization_health.py run stale --months 1 --results results.json
    python3 -E -s scripts/organization_health.py report --results results.json \\
        --run-url URL --summary summary.md --slack-output slack.txt

The script uses the Python standard library and ``gh`` (authenticated through ``GH_TOKEN``).
Tests replace all GitHub, registry, and package-manager calls.
"""

import argparse
import base64
import calendar
import json
import os
import re
import subprocess  # nosec B404: runs fixed gh and package-manager commands
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from github_cli import ORGANIZATION, error_line, run_json

PAGE_SIZE = 100
REGISTRIES = {
    "npm": "https://registry.npmjs.org",
    "pypi": "https://pypi.org/pypi",
    "go": "https://proxy.golang.org",
}
DISPLAY_NAMES = {
    "security": "Repository security settings",
    "workflow": "Workflow security hygiene",
    "packages": "Published package health",
    "stale": "Stale pull requests and issues",
}


def cutoff_months(now: datetime, months: int) -> datetime:
    """Return a calendar-month cutoff for an aware datetime.

    :param now: The cutoff reference time, with a timezone.
    :param months: A positive whole number of calendar months.
    :returns: The clamped calendar date and time ``months`` before ``now``.
    :raises ValueError: If ``months`` is not positive or the cutoff predates year 1.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if months < 1:
        raise ValueError("months must be a positive integer")
    current_month_index = now.year * 12 + now.month - 1
    target_month_index = current_month_index - months
    year, month_offset = divmod(target_month_index, 12)
    if year < 1:
        raise ValueError("months is too large")
    month = month_offset + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(now.day, last_day)
    return now.replace(year=year, month=month, day=day)


def api_pages(endpoint: str, key: str | None = None) -> list[dict]:
    """Read every page of a GitHub list endpoint.

    :param endpoint: The GitHub REST endpoint, optionally with query parameters.
    :param key: The object key containing the list, or ``None`` for a top-level list.
    :returns: The concatenated response objects.
    :raises subprocess.CalledProcessError: If a GitHub API request fails.
    :raises ValueError: If GitHub returns an unexpected response shape.
    """
    found = []
    page = 1
    while True:
        separator = "&" if "?" in endpoint else "?"
        response = run_json("gh", "api", f"{endpoint}{separator}per_page={PAGE_SIZE}&page={page}")
        if key is None:
            values = response
        elif isinstance(response, dict):
            values = response.get(key)
        else:
            values = None
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise ValueError("GitHub returned an unexpected list response")
        found.extend(values)
        if len(values) < PAGE_SIZE:
            return found
        page += 1


def github_json(endpoint: str) -> dict | None:
    """Read a GitHub API resource and require a JSON object.

    :param endpoint: The GitHub REST API endpoint.
    :returns: The decoded object, or ``None`` for an empty response.
    :raises subprocess.CalledProcessError: If the API request fails.
    :raises ValueError: If GitHub returns a non-object response.
    """
    result = run_json("gh", "api", endpoint)
    if result is not None and not isinstance(result, dict):
        raise ValueError("GitHub returned an unexpected object response")
    return result


def status_code(error: subprocess.CalledProcessError) -> str:
    """Extract only the HTTP status from ``gh`` error output.

    :param error: The failed ``gh`` command.
    :returns: The HTTP status code, or ``"unknown"`` if it is absent.
    """
    match = re.search(r"\bHTTP (\d{3})\b", error_line(error))
    return match.group(1) if match else "unknown"


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
    status = status_code(error) if isinstance(error, subprocess.CalledProcessError) else "unavailable"
    key = f"{category} ({status})"
    results["errors"][key] = results["errors"].get(key, 0) + 1


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


def scan_security() -> dict:
    """Compare repository Actions, branch, secret scanning, and Dependabot settings.

    :returns: The scan results with findings and counts of incomplete API requests.
    """
    results = new_results("security")
    for item in list_repositories(results):
        repository = item.get("full_name")
        default_branch = item.get("default_branch")
        if not isinstance(repository, str) or not repository.startswith(f"{ORGANIZATION}/"):
            record_error(results, "Repository inventory")
            continue
        base = f"repos/{repository}"
        web_url = f"https://github.com/{repository}"
        try:
            details = github_json(base) or {}
            security = details.get("security_and_analysis")
            if not isinstance(security, dict):
                raise ValueError("security settings are unavailable")
            secret_scanning = security.get("secret_scanning") or {}
            if secret_scanning.get("status") != "enabled":
                add_finding(results, repository, "Secret scanning", "Secret scanning is not enabled.", web_url)
        except (subprocess.CalledProcessError, ValueError) as error:
            record_error(results, "Repository security metadata", error)

        try:
            permissions = github_json(f"{base}/actions/permissions/workflow") or {}
            if permissions.get("default_workflow_permissions") != "read":
                add_finding(
                    results,
                    repository,
                    "Actions token permissions",
                    "New workflows do not default to read-only GITHUB_TOKEN permissions.",
                    f"{web_url}/settings/actions",
                )
        except (subprocess.CalledProcessError, ValueError) as error:
            record_error(results, "Actions permissions", error)

        for endpoint, label in (
            (f"{base}/vulnerability-alerts", "Dependabot alerts"),
            (f"{base}/automated-security-fixes", "Dependabot security updates"),
        ):
            try:
                github_json(endpoint)
            except subprocess.CalledProcessError as error:
                if status_code(error) == "404":
                    add_finding(
                        results,
                        repository,
                        label,
                        f"{label} are not enabled.",
                        f"{web_url}/settings/security_analysis",
                    )
                else:
                    record_error(results, label, error)
            except ValueError as error:
                record_error(results, label, error)

        if not isinstance(default_branch, str) or not default_branch:
            record_error(results, "Default branch")
            continue
        branch = quote(default_branch, safe="")
        try:
            protection = github_json(f"{base}/branches/{branch}/protection")
        except subprocess.CalledProcessError as error:
            if status_code(error) == "404":
                add_finding(
                    results,
                    repository,
                    "Default branch protection",
                    f"The {default_branch} branch has no protection rules.",
                    f"{web_url}/settings/branches",
                )
            else:
                record_error(results, "Branch protection", error)
            continue
        except ValueError as error:
            record_error(results, "Branch protection", error)
            continue
        if not protection:
            add_finding(results, repository, "Default branch protection", "Protection rules are missing.", web_url)
            continue
        reviews = protection.get("required_pull_request_reviews")
        if not reviews:
            add_finding(
                results,
                repository,
                "Default branch protection",
                "Pull requests are not required for the default branch.",
                f"{web_url}/settings/branches",
            )
        enforce_admins = protection.get("enforce_admins") or {}
        if not enforce_admins.get("enabled", False):
            add_finding(
                results,
                repository,
                "Default branch protection",
                "Branch protection rules do not apply to administrators.",
                f"{web_url}/settings/branches",
            )
        force_pushes = protection.get("allow_force_pushes") or {}
        if force_pushes.get("enabled", False):
            add_finding(
                results,
                repository,
                "Default branch protection",
                "Force pushes are allowed on the default branch.",
                f"{web_url}/settings/branches",
            )
    return results


def workflow_findings(source: str) -> list[tuple[str, str]]:
    """Find unpinned actions, write permissions, and missing job timeouts.

    :param source: The contents of one GitHub Actions workflow file.
    :returns: Category and description pairs for each workflow finding.
    """
    findings = []
    uses_pattern = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)
    for reference in uses_pattern.findall(source):
        if reference.startswith("./") or reference.startswith("docker://"):
            continue
        ref = reference.rsplit("@", 1)
        if len(ref) < 2 or not re.fullmatch(r"[0-9a-fA-F]{40}", ref[-1]):
            findings.append(("Unpinned action", f"`{reference}` is not pinned to a full commit SHA."))

    jobs = re.search(r"(?m)^jobs:\s*$", source)
    if jobs:
        tail = source[jobs.end() :]
    else:
        tail = ""
    job_starts = list(re.finditer(r"(?m)^ {2}[A-Za-z0-9_-]+:\s*$", tail))
    job_blocks = []
    for index, match in enumerate(job_starts):
        start = match.end()
        if index + 1 < len(job_starts):
            end = job_starts[index + 1].start()
        else:
            end = len(tail)
        job_blocks.append(tail[start:end])
    workflow_permissions = re.search(r"(?m)^permissions\s*:", source) is not None
    job_permissions = [re.search(r"(?m)^ {4}permissions\s*:", block) is not None for block in job_blocks]
    if not workflow_permissions and (not job_permissions or not all(job_permissions)):
        findings.append(("Token permissions", "The workflow does not declare explicit token permissions."))
    if re.search(r"(?m)^permissions\s*:\s*write-all\s*$", source):
        findings.append(("Token permissions", "The workflow grants write-all permissions."))
    elif re.search(r"(?m)^ {2,}[A-Za-z_-]+\s*:\s*write\s*(?:#.*)?$", source):
        findings.append(("Token permissions", "The workflow grants one or more write permissions."))

    if jobs:
        for index, match in enumerate(job_starts):
            block = job_blocks[index]
            if not re.search(r"(?m)^ {4}timeout-minutes\s*:", block):
                job_name = match.group().strip().removesuffix(":")
                findings.append(("Missing timeout", f"Job `{job_name}` has no `timeout-minutes`."))
    return findings


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


def scan_workflows() -> dict:
    """Review workflow files on every repository's default branch.

    :returns: The scan results with findings and counts of incomplete API requests.
    """
    results = new_results("workflow")
    for item in list_repositories(results):
        repository, branch = item.get("full_name"), item.get("default_branch")
        if not isinstance(repository, str) or not repository.startswith(f"{ORGANIZATION}/"):
            record_error(results, "Repository inventory")
            continue
        if not isinstance(branch, str) or not branch:
            record_error(results, "Default branch")
            continue
        branch_ref = quote(branch, safe="")
        try:
            tree = github_json(f"repos/{repository}/git/trees/{branch_ref}?recursive=1") or {}
            paths = [
                node["path"]
                for node in tree.get("tree", [])
                if isinstance(node, dict)
                and isinstance(node.get("path"), str)
                and re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", node["path"])
            ]
        except (subprocess.CalledProcessError, ValueError) as error:
            record_error(results, "Workflow inventory", error)
            continue
        for path in paths:
            try:
                response = github_json(f"repos/{repository}/contents/{quote(path, safe='/')}?ref={branch_ref}")
                source = decode_content(response or {})
            except (subprocess.CalledProcessError, ValueError, UnicodeDecodeError) as error:
                record_error(results, "Workflow file", error)
                continue
            web_url = f"https://github.com/{repository}/blob/{branch}/{path}"
            for category, detail in workflow_findings(source):
                add_finding(results, repository, category, detail, web_url, path)
    return results


def registry_json(url: str) -> dict:
    """Read a package registry's JSON metadata from a fixed public endpoint.

    :param url: The constructed URL for a supported public package registry.
    :returns: The decoded registry metadata object.
    :raises HTTPError: If the registry returns an unsuccessful status code.
    :raises URLError: If the registry cannot be reached.
    :raises ValueError: If the response is not a JSON object.
    """
    headers = {"Accept": "application/json", "User-Agent": "smarter-sh-automation"}
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:  # nosec B310: registry URLs are fixed by ecosystem
        body = response.read()
    text = body.decode("utf-8")
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("registry returned an unexpected JSON response")
    return result


def registry_version(ecosystem: str, name: str) -> str:
    """Return the registry's latest stable version for a discovered package.

    :param ecosystem: One of ``npm``, ``pypi``, or ``go``.
    :param name: The package or module name from its source manifest.
    :returns: The latest stable version listed by the registry.
    :raises HTTPError: If the registry returns an unsuccessful status code.
    :raises URLError: If the registry cannot be reached.
    :raises ValueError: If the registry has no stable package version.
    """
    if ecosystem == "npm":
        metadata = registry_json(f"{REGISTRIES['npm']}/{quote(name, safe='@/')}")
        version = (metadata.get("dist-tags") or {}).get("latest")
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            stable = [value for value in metadata.get("versions", {}) if is_stable_version(value)]
            version = max(stable, key=version_key) if stable else None
    elif ecosystem == "pypi":
        metadata = registry_json(f"{REGISTRIES['pypi']}/{quote(name, safe='')}/json")
        versions = [
            value for value in metadata.get("releases", {}) if is_stable_version(value) and metadata["releases"][value]
        ]
        version = max(versions, key=version_key) if versions else None
    else:
        escaped_name = "".join(f"!{character.lower()}" if character.isupper() else character for character in name)
        url = f"{REGISTRIES['go']}/{quote(escaped_name, safe='/!')}/@v/list"
        request = Request(url, headers={"User-Agent": "smarter-sh-automation"})
        with urlopen(request, timeout=30) as response:  # nosec B310: fixed Go module proxy
            versions = response.read().decode("utf-8").splitlines()
        stable = [value for value in versions if is_stable_version(value)]
        version = max(stable, key=version_key) if stable else None
    if not isinstance(version, str) or not version:
        raise ValueError("registry has no stable package version")
    return version


def version_key(value: str) -> tuple[int, ...]:
    """Convert a stable semantic version into an ordering key.

    :param value: A version with three numeric components and an optional ``v`` prefix.
    :returns: The major, minor, and patch components as integers.
    """
    version = value.removeprefix("v")
    parts = version.split(".")
    major, minor, patch = parts
    major_number = int(major)
    minor_number = int(minor)
    patch_number = int(patch)
    return major_number, minor_number, patch_number


def is_stable_version(value: str) -> bool:
    """Check that a version contains only three numeric components.

    :param value: The registry version to check.
    :returns: Whether the version is a stable three-part version.
    """
    parts = value.removeprefix("v").split(".")
    return len(parts) == 3 and all(part.isdecimal() for part in parts)


def normalized_version(value: str) -> str:
    """Remove a conventional leading ``v`` from a release or registry version.

    :param value: The release tag or registry version.
    :returns: The version without its optional leading ``v``.
    """
    return value[1:] if value.startswith("v") else value


def manifest_value(repository: str, path: str) -> str:
    """Read and decode one package manifest from the repository.

    :param repository: The package repository's ``owner/name`` identifier.
    :param path: The repository-relative manifest path.
    :returns: The decoded UTF-8 manifest text.
    :raises subprocess.CalledProcessError: If GitHub cannot return the file.
    :raises ValueError: If the contents response has an unexpected shape.
    :raises UnicodeDecodeError: If the file content is not UTF-8.
    """
    response = github_json(f"repos/{repository}/contents/{path}")
    return decode_content(response or {})


def package_candidates(repository: str, root_items: list[dict], release_tag: str, workflow_text: str) -> list[dict]:
    """Discover published npm, PyPI, and Go module packages from manifests and release workflows.

    :param repository: The package repository's ``owner/name`` identifier.
    :param root_items: Entries returned by the repository's root contents endpoint.
    :param release_tag: Its latest stable GitHub release tag.
    :param workflow_text: The combined release workflow source text.
    :returns: Package metadata mappings for the supported registries.
    :raises subprocess.CalledProcessError: If GitHub cannot return a manifest.
    :raises json.JSONDecodeError: If a package manifest is invalid JSON.
    :raises tomllib.TOMLDecodeError: If a Python package manifest is invalid TOML.
    """
    by_name = {item.get("name"): item for item in root_items if isinstance(item.get("name"), str)}
    candidates = []

    if "package.json" in by_name:
        package_text = manifest_value(repository, "package.json")
        package_name = published_npm_package_name(package_text, workflow_text)
        if package_name:
            candidate = {"ecosystem": "npm", "name": package_name}
            candidates.append(candidate)

    if "pyproject.toml" in by_name and re.search(
        r"pypa/gh-action-pypi-publish|twine\s+upload|poetry\s+publish|hatch\s+publish|"
        r"publish(?:ing)?\s+to\s+(?:test)?pypi",
        workflow_text,
        re.I,
    ):
        project = tomllib.loads(manifest_value(repository, "pyproject.toml")).get("project", {})
        if isinstance(project, dict) and isinstance(project.get("name"), str):
            candidates.append({"ecosystem": "pypi", "name": project["name"]})

    if "go.mod" in by_name:
        match = re.search(r"(?m)^module\s+(\S+)", manifest_value(repository, "go.mod"))
        if match:
            candidates.append({"ecosystem": "go", "name": match.group(1)})

    for candidate in candidates:
        candidate["repository"] = repository
        candidate["expected_version"] = release_tag.lstrip("v")
    return candidates


def published_npm_package_name(package_text: str, workflow_text: str) -> str | None:
    """Return a public npm package's name when the repository publishes it.

    :param package_text: The contents of the repository's ``package.json``.
    :param workflow_text: The combined source of its GitHub Actions workflows.
    :returns: The package name, or ``None`` if it is not a published public package.
    :raises json.JSONDecodeError: If the package manifest is invalid JSON.
    """
    package = json.loads(package_text)
    if not isinstance(package, dict):
        return None

    package_name = package.get("name")
    if not isinstance(package_name, str):
        return None

    is_private = package.get("private")
    if is_private:
        return None

    publish_config = package.get("publishConfig")
    if publish_config:
        return package_name

    publish_pattern = r"\bnpm\s+publish\b|@semantic-release/npm|publish(?:ing)?\s+to\s+npm"
    has_publish_workflow = re.search(publish_pattern, workflow_text, re.I) is not None
    if has_publish_workflow:
        return package_name

    return None


def install_package(ecosystem: str, name: str, version: str) -> None:
    """Install or resolve a package in a temporary directory without running package scripts.

    :param ecosystem: One of ``npm``, ``pypi``, or ``go``.
    :param name: The package or module name.
    :param version: The exact version to install or download.
    :raises OSError: If the package manager executable is unavailable.
    :raises subprocess.CalledProcessError: If package installation or module download fails.
    """
    with tempfile.TemporaryDirectory(prefix="org-package-check-") as directory:
        command: tuple[str, ...]
        if ecosystem == "npm":
            command = ("npm", "install", "--ignore-scripts", "--no-save", "--package-lock=false", f"{name}@{version}")
        elif ecosystem == "pypi":
            command = (
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--disable-pip-version-check",
                f"{name}=={version}",
            )
        else:
            subprocess.run(
                ("go", "mod", "init", "check.invalid/package-health"),
                cwd=directory,
                check=True,
                capture_output=True,
                text=True,
            )  # nosec B603: fixed command in a fresh temporary directory
            command = ("go", "mod", "download", f"{name}@{version}")
        subprocess.run(command, cwd=directory, check=True, capture_output=True, text=True)  # nosec B603


def scan_packages() -> dict:
    """Find published packages and check their registry versions and installs.

    The supported registries are npm, PyPI, and the Go module proxy. Package candidates come
    from manifests and their release workflows; archived repositories are skipped.

    :returns: The scan results with version/install findings and API error counts.
    """
    results = new_results("packages")
    for item in list_repositories(results):
        repository = item.get("full_name")
        if not isinstance(repository, str) or not repository.startswith(f"{ORGANIZATION}/"):
            record_error(results, "Repository inventory")
            continue
        if item.get("archived"):
            continue
        try:
            root = api_pages(f"repos/{repository}/contents")
        except (subprocess.CalledProcessError, ValueError) as error:
            record_error(results, "Package manifest inventory", error)
            continue
        paths = {entry.get("name") for entry in root}
        manifest_paths = paths.intersection({"package.json", "pyproject.toml", "go.mod"})
        if not manifest_paths:
            continue
        try:
            releases = github_json(f"repos/{repository}/releases/latest")
            if not isinstance(releases, dict) or not isinstance(releases.get("tag_name"), str):
                continue
            default_branch = item.get("default_branch") or "main"
            branch_ref = quote(default_branch, safe="")
            tree_endpoint = f"repos/{repository}/git/trees/{branch_ref}?recursive=1"
            tree = github_json(tree_endpoint) or {}
            workflow_paths = [
                node["path"]
                for node in tree.get("tree", [])
                if isinstance(node, dict)
                and isinstance(node.get("path"), str)
                and re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", node["path"])
            ]
            workflow_text = ""
            for path in workflow_paths:
                encoded_path = quote(path, safe="/")
                content_endpoint = f"repos/{repository}/contents/{encoded_path}"
                response = github_json(content_endpoint)
                source = decode_content(response or {})
                workflow_text += "\n" + source
            candidates = package_candidates(repository, root, releases["tag_name"], workflow_text)
        except subprocess.CalledProcessError as error:
            if status_code(error) != "404":
                record_error(results, "Package release metadata", error)
            continue
        except (ValueError, tomllib.TOMLDecodeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            record_error(results, "Package metadata", error)
            continue

        for candidate in candidates:
            ecosystem, name = candidate["ecosystem"], candidate["name"]
            expected = candidate["expected_version"]
            try:
                available = registry_version(ecosystem, name)
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
                if isinstance(error, HTTPError):
                    status = str(error.code)
                else:
                    status = "unavailable"
                add_finding(
                    results,
                    repository,
                    f"{ecosystem} registry",
                    f"Could not read `{name}` registry metadata ({status}).",
                    f"https://github.com/{repository}/releases",
                )
                continue
            available_version = normalized_version(available)
            expected_version = normalized_version(expected)
            if available_version != expected_version:
                add_finding(
                    results,
                    repository,
                    f"{ecosystem} version mismatch",
                    f"`{name}` latest registry version is `{available}`; latest stable GitHub release is `{expected}`.",
                    f"https://github.com/{repository}/releases",
                )
            try:
                install_package(ecosystem, name, available)
            except (OSError, subprocess.CalledProcessError) as error:
                status = (
                    f"exit {error.returncode}"
                    if isinstance(error, subprocess.CalledProcessError)
                    else "tool unavailable"
                )
                add_finding(
                    results,
                    repository,
                    f"{ecosystem} install",
                    f"Installing `{name}@{available}` failed ({status}).",
                    f"https://github.com/{repository}/releases",
                )
    return results


def scan_stale(months: int = 1, now: datetime | None = None) -> dict:
    """List open issues and pull requests created before the calendar-month cutoff.

    :param months: The positive calendar-month age threshold.
    :param now: Optional timezone-aware reference time, used by tests.
    :returns: The scan results with stale items, cutoff, and API error counts.
    :raises ValueError: If the threshold is invalid or ``now`` is naive.
    """
    results = new_results("stale")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cutoff = cutoff_months(now.astimezone(timezone.utc), months)
    results["cutoff"] = cutoff.isoformat()
    results["months"] = months
    for item in list_repositories(results):
        repository = item.get("full_name")
        if not isinstance(repository, str) or not repository.startswith(f"{ORGANIZATION}/") or item.get("archived"):
            continue
        try:
            issues = api_pages(f"repos/{repository}/issues?state=open", None)
        except (subprocess.CalledProcessError, ValueError) as error:
            record_error(results, "Open issues", error)
            continue
        for issue in issues:
            created = issue.get("created_at")
            url = issue.get("html_url")
            title = issue.get("title")
            number = issue.get("number")
            if not isinstance(created, str):
                record_error(results, "Issue metadata")
                continue
            if not isinstance(url, str):
                record_error(results, "Issue metadata")
                continue
            if not isinstance(title, str):
                record_error(results, "Issue metadata")
                continue
            if not isinstance(number, int):
                record_error(results, "Issue metadata")
                continue
            if "pull_request" in issue:
                category = "Pull request"
            else:
                category = "Issue"
            try:
                normalized_created = created.replace("Z", "+00:00")
                created_at = datetime.fromisoformat(normalized_created)
                if created_at.tzinfo is None:
                    raise ValueError("timestamp has no timezone")
            except ValueError:
                record_error(results, "Issue timestamp")
                continue
            if created_at.astimezone(timezone.utc) < cutoff:
                add_finding(
                    results,
                    repository,
                    category,
                    f"#{number}: {title} (opened {created_at.date().isoformat()})",
                    url,
                )
    results["cutoff"] = cutoff.isoformat()
    results["findings"].sort(key=lambda finding: (finding["repository"], finding["category"], finding["detail"]))
    return results


def slack_escape(value: str) -> str:
    """Escape API-provided text so it cannot create Slack links or mentions.

    :param value: An API-provided repository, path, or finding description.
    :returns: The value with HTML-sensitive characters escaped.
    """
    return escape(value, quote=False)


def slack_message(results: dict, run_url: str) -> str:
    """Format detailed findings for Slack.

    :param results: Results returned by one of the scan functions.
    :param run_url: The workflow run's URL.
    :returns: A Slack mrkdwn report with findings and any scan errors.
    :raises KeyError: If ``results`` contains an unsupported scan kind.
    """
    title = DISPLAY_NAMES[results["kind"]]
    lines = [f"*{title}*"]
    if results["kind"] == "stale":
        lines.append(
            f"Open pull requests and issues older than {results['months']} month(s) (before {results['cutoff']})."
        )
    if not results["findings"] and not results["errors"]:
        lines.append("No findings.")
    else:
        for finding in results["findings"][:100]:
            repository = slack_escape(finding["repository"])
            category = slack_escape(finding["category"])
            detail = slack_escape(finding["detail"])
            path = f" (`{slack_escape(finding['path'])}`)" if finding.get("path") else ""
            lines.append(f"• <{finding['url']}|{repository}> · {category}{path}: {detail}")
        if len(results["findings"]) > 100:
            lines.append(f"• {len(results['findings']) - 100} more finding(s) omitted; see the run summary counts.")
        if results["errors"]:
            lines.append(f"⚠️ Scan incomplete: {sum(results['errors'].values())} GitHub API request(s) failed.")
            for error, count in sorted(results["errors"].items()):
                lines.append(f"• {slack_escape(error)}: {count}")
    lines.append(f"<{run_url}|View workflow run>")
    return "\n".join(lines)


def write_report(results: dict, run_url: str, summary_path: str, slack_path: str) -> None:
    """Write a names-free Actions summary and a detailed Slack report.

    :param results: Results returned by one of the scan functions.
    :param run_url: The workflow run's URL.
    :param summary_path: The path to the GitHub Actions summary file.
    :param slack_path: The path to the temporary Slack message file.
    :raises OSError: If either report file cannot be written.
    """
    summary = [
        f"## {DISPLAY_NAMES[results['kind']]}",
        "",
        f"- Repositories checked: {results['repositories_scanned']}.",
        f"- Findings: {len(results['findings'])}.",
        f"- API/data errors: {sum(results['errors'].values())}.",
    ]
    if results["kind"] == "stale":
        summary.insert(
            2, f"- Age threshold: older than {results['months']} calendar month(s), before {results['cutoff']}."
        )
    summary.extend(
        [
            "- Detailed findings are prepared for #general; repository and issue details are omitted from this public summary.",
            f"- Run: {run_url}",
        ]
    )
    Path(summary_path).write_text("\n".join(summary) + "\n", encoding="utf-8")
    Path(slack_path).write_text(slack_message(results, run_url) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Run one report, writing its data to a temporary file.

    :param argv: Optional command-line arguments, excluding the program name.
    :returns: 0 on a successful report; 1 if the scan found findings or errors.
    :raises OSError: If the results or report files cannot be read or written.
    :raises ValueError: If the requested scan parameters or saved JSON are invalid.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("run", help="scan the organization")
    scan_parser.add_argument("kind", choices=("security", "workflow", "packages", "stale"))
    scan_parser.add_argument("--results", required=True)
    scan_parser.add_argument("--months", type=int, default=1)
    report_parser = commands.add_parser("report", help="write the Actions summary and Slack message")
    report_parser.add_argument("--results", required=True)
    report_parser.add_argument("--run-url", required=True)
    report_parser.add_argument("--summary", required=True)
    report_parser.add_argument("--slack-output", required=True)
    args = parser.parse_args(argv)

    if args.command == "report":
        results = json.loads(Path(args.results).read_text(encoding="utf-8"))
        write_report(results, args.run_url, args.summary, args.slack_output)
        return 0

    if args.kind == "stale" and args.months < 1:
        parser.error("--months must be a positive integer")
    if not os.environ.get("GH_TOKEN"):
        # Without a token every API call fails; say so once instead of counting failures.
        results = new_results(args.kind)
        results["errors"]["GH_TOKEN is empty: is the GH_TOKEN organization secret available to this repository?"] = 1
        if args.kind == "stale":
            results.update(months=args.months, cutoff="n/a")
    elif args.kind == "stale":
        results = scan_stale(args.months)
    else:
        results = {
            "security": scan_security,
            "workflow": scan_workflows,
            "packages": scan_packages,
        }[args.kind]()
    Path(args.results).write_text(json.dumps(results), encoding="utf-8")
    # Counts and error categories only: the log is public.
    print(
        f"{results['repositories_scanned']} repositories checked, {len(results['findings'])} finding(s), "
        f"{sum(results['errors'].values())} API error(s).",
        file=sys.stderr,
    )
    for error, count in sorted(results["errors"].items()):
        print(f"error: {error}: {count}", file=sys.stderr)
    return 1 if results["findings"] or results["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
