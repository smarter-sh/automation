#!/usr/bin/env python3
"""The ``packages`` scan: find the npm, PyPI and Go packages the repositories publish, and check that each installs.

Part of :mod:`health.organization_health`, which runs it; see that module for the scans and how
they are run. It uses only Python's standard library and ``gh``.
"""

import json
import re
import subprocess  # nosec B404: runs fixed gh and package-manager commands
import sys
import tempfile
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from github_cli import api_object, api_pages, http_status
from health.common import (
    add_finding,
    file_text,
    is_organization_repository,
    list_repositories,
    new_results,
    record_error,
    workflow_paths,
)

REGISTRIES = {
    "npm": "https://registry.npmjs.org",
    "pypi": "https://pypi.org/pypi",
    "go": "https://proxy.golang.org",
}


MANIFEST_NAMES = {"package.json", "pyproject.toml", "go.mod"}


PYPI_PUBLISH_PATTERN = (
    r"pypa/gh-action-pypi-publish|twine\s+upload|poetry\s+publish|hatch\s+publish|"
    r"publish(?:ing)?\s+to\s+(?:test)?pypi"
)


NPM_PUBLISH_PATTERN = r"\bnpm\s+publish\b|@semantic-release/npm|publish(?:ing)?\s+to\s+npm"


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


def latest_stable_version(versions) -> str | None:
    """Pick the highest stable version.

    :param versions: An iterable of version strings, in any order.
    :returns: The highest stable three-part version, or ``None`` if there is none.
    """
    stable = [value for value in versions if is_stable_version(value)]
    if not stable:
        return None
    return max(stable, key=version_key)


def npm_version(name: str) -> str | None:
    """Return an npm package's ``latest`` tag, or its highest stable version.

    :param name: The npm package name.
    :returns: The version, or ``None`` if the registry lists no stable version.
    :raises HTTPError: If the registry returns an unsuccessful status code.
    :raises URLError: If the registry cannot be reached.
    :raises ValueError: If the response is not a JSON object.
    """
    encoded_name = quote(name, safe="@/")
    metadata = registry_json(f"{REGISTRIES['npm']}/{encoded_name}")
    dist_tags = metadata.get("dist-tags") or {}
    latest = dist_tags.get("latest")
    if isinstance(latest, str) and re.fullmatch(r"\d+\.\d+\.\d+", latest):
        return latest
    versions = metadata.get("versions", {})
    return latest_stable_version(versions)


def pypi_version(name: str) -> str | None:
    """Return a PyPI package's highest stable version that has files.

    :param name: The PyPI project name.
    :returns: The version, or ``None`` if the registry lists no stable version.
    :raises HTTPError: If the registry returns an unsuccessful status code.
    :raises URLError: If the registry cannot be reached.
    :raises ValueError: If the response is not a JSON object.
    """
    encoded_name = quote(name, safe="")
    metadata = registry_json(f"{REGISTRIES['pypi']}/{encoded_name}/json")
    releases = metadata.get("releases", {})
    versions_with_files = [value for value in releases if releases[value]]
    return latest_stable_version(versions_with_files)


def go_escaped_path(name: str) -> str:
    """Escape a Go module path for the module proxy: each capital letter becomes ``!`` and its lowercase.

    :param name: The Go module path.
    :returns: The escaped module path.
    """
    characters = []
    for character in name:
        if character.isupper():
            characters.append(f"!{character.lower()}")
        else:
            characters.append(character)
    return "".join(characters)


def go_version(name: str) -> str | None:
    """Return a Go module's highest stable version from the module proxy.

    :param name: The Go module path.
    :returns: The version, or ``None`` if the proxy lists no stable version.
    :raises HTTPError: If the proxy returns an unsuccessful status code.
    :raises URLError: If the proxy cannot be reached.
    """
    escaped_name = go_escaped_path(name)
    encoded_name = quote(escaped_name, safe="/!")
    url = f"{REGISTRIES['go']}/{encoded_name}/@v/list"
    request = Request(url, headers={"User-Agent": "smarter-sh-automation"})
    with urlopen(request, timeout=30) as response:  # nosec B310: fixed Go module proxy
        body = response.read()
    text = body.decode("utf-8")
    versions = text.splitlines()
    return latest_stable_version(versions)


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
        version = npm_version(name)
    elif ecosystem == "pypi":
        version = pypi_version(name)
    else:
        version = go_version(name)
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
    version = value.removeprefix("v")
    parts = version.split(".")
    if len(parts) != 3:
        return False
    return all(part.isdecimal() for part in parts)


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
    return file_text(f"repos/{repository}/contents/{path}")


def python_project_name(pyproject_text: str) -> str | None:
    """Return the project name from a ``pyproject.toml``.

    :param pyproject_text: The contents of the repository's ``pyproject.toml``.
    :returns: The ``[project]`` name, or ``None`` if there is none.
    :raises tomllib.TOMLDecodeError: If the manifest is invalid TOML.
    """
    pyproject = tomllib.loads(pyproject_text)
    project = pyproject.get("project", {})
    if not isinstance(project, dict):
        return None
    name = project.get("name")
    if not isinstance(name, str):
        return None
    return name


def go_module_name(go_mod_text: str) -> str | None:
    """Return the module path from a ``go.mod``.

    :param go_mod_text: The contents of the repository's ``go.mod``.
    :returns: The module path, or ``None`` if there is no ``module`` line.
    """
    match = re.search(r"(?m)^module\s+(\S+)", go_mod_text)
    if not match:
        return None
    return match.group(1)


def package_candidates(repository: str, root_items: list[dict], release_tag: str, workflow_text: str) -> list[dict]:
    """Discover published npm, PyPI, and Go module packages from manifests and release workflows.

    A package in an npm workspace (e.g. smarter-console's ``packages/*``) is found too. Its
    ``expected_version`` is ``None``: a monorepo publishes each package only when it changed, so
    its npm version doesn't follow the repository's release tag.

    :param repository: The package repository's ``owner/name`` identifier.
    :param root_items: Entries returned by the repository's root contents endpoint.
    :param release_tag: Its latest stable GitHub release tag.
    :param workflow_text: The combined release workflow source text.
    :returns: Package metadata mappings for the supported registries.
    :raises subprocess.CalledProcessError: If GitHub cannot return a manifest.
    :raises json.JSONDecodeError: If a package manifest is invalid JSON.
    :raises tomllib.TOMLDecodeError: If a Python package manifest is invalid TOML.
    """
    file_names = {item.get("name") for item in root_items}
    candidates = []
    workspace_candidates = []

    if "package.json" in file_names:
        package_text = manifest_value(repository, "package.json")
        package_name = published_npm_package_name(package_text, workflow_text)
        if package_name:
            candidates.append({"ecosystem": "npm", "name": package_name})
        workspace_candidates = workspace_package_candidates(repository, package_text, workflow_text)

    publishes_to_pypi = re.search(PYPI_PUBLISH_PATTERN, workflow_text, re.I) is not None
    if "pyproject.toml" in file_names and publishes_to_pypi:
        pyproject_text = manifest_value(repository, "pyproject.toml")
        project_name = python_project_name(pyproject_text)
        if project_name:
            candidates.append({"ecosystem": "pypi", "name": project_name})

    if "go.mod" in file_names:
        go_mod_text = manifest_value(repository, "go.mod")
        module_name = go_module_name(go_mod_text)
        if module_name:
            candidates.append({"ecosystem": "go", "name": module_name})

    expected_version = release_tag.lstrip("v")
    for candidate in candidates:
        candidate["repository"] = repository
        candidate["expected_version"] = expected_version
    for candidate in workspace_candidates:
        candidate["repository"] = repository
        candidate["expected_version"] = None
        candidates.append(candidate)
    return candidates


def workspace_folders(package_text: str) -> list[str]:
    """Return the folders that a root ``package.json``'s ``workspaces`` names, as globs or paths.

    :param package_text: The contents of the repository's root ``package.json``.
    :returns: The patterns, e.g. ``["packages/*"]``, or an empty list if it has no workspaces.
    :raises json.JSONDecodeError: If the package manifest is invalid JSON.
    """
    package = json.loads(package_text)
    if not isinstance(package, dict):
        return []
    workspaces = package.get("workspaces")
    # Yarn also allows {"packages": [...]}.
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    if not isinstance(workspaces, list):
        return []
    return [pattern for pattern in workspaces if isinstance(pattern, str)]


def workspace_package_paths(repository: str, pattern: str) -> list[str]:
    """List the folders that one workspace pattern matches.

    Only a plain folder (``tools/cli``) or a folder's direct subfolders (``packages/*``) are
    understood; any other glob is skipped.

    :param repository: The repository's ``owner/name`` identifier.
    :param pattern: One ``workspaces`` entry.
    :returns: The repository-relative folders.
    :raises subprocess.CalledProcessError: If GitHub cannot list the folder.
    """
    if not pattern.endswith("/*"):
        if "*" in pattern:
            return []
        return [pattern.strip("/")]
    parent = pattern.removesuffix("/*")
    if "*" in parent:
        return []
    encoded_parent = quote(parent, safe="/")
    entries = api_pages(f"repos/{repository}/contents/{encoded_parent}")
    return [entry["path"] for entry in entries if entry.get("type") == "dir"]


def workspace_package_candidates(repository: str, package_text: str, workflow_text: str) -> list[dict]:
    """Find the public npm packages that a repository's npm workspaces publish.

    :param repository: The repository's ``owner/name`` identifier.
    :param package_text: The contents of the repository's root ``package.json``.
    :param workflow_text: The combined source of its GitHub Actions workflows.
    :returns: An ``npm`` candidate for each published workspace package, without versions.
    :raises subprocess.CalledProcessError: If GitHub cannot list a folder or return a manifest.
    :raises json.JSONDecodeError: If a package manifest is invalid JSON.
    """
    candidates = []
    for pattern in workspace_folders(package_text):
        for folder in workspace_package_paths(repository, pattern):
            manifest_path = quote(f"{folder}/package.json", safe="/")
            try:
                workspace_text = manifest_value(repository, manifest_path)
            except subprocess.CalledProcessError as error:
                # A folder without a package.json isn't a package.
                if http_status(error) == "404":
                    continue
                raise
            package_name = published_npm_package_name(workspace_text, workflow_text)
            if package_name:
                candidates.append({"ecosystem": "npm", "name": package_name})
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

    has_publish_workflow = re.search(NPM_PUBLISH_PATTERN, workflow_text, re.I) is not None
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
        if not is_organization_repository(repository):
            record_error(results, "Repository inventory")
            continue
        if item.get("archived"):
            continue
        candidates = discover_packages(results, repository, item)
        for candidate in candidates:
            check_package(results, repository, candidate)
    return results


def discover_packages(results: dict, repository: str, item: dict) -> list[dict]:
    """Find the packages one repository publishes, recording any API errors.

    :param results: The mutable scan results.
    :param repository: The repository's ``owner/name`` identifier.
    :param item: The repository's metadata from the organization inventory.
    :returns: The package candidates, or an empty list if there are none or they can't be read.
    """
    try:
        root = api_pages(f"repos/{repository}/contents")
    except (subprocess.CalledProcessError, ValueError) as error:
        record_error(results, "Package manifest inventory", error)
        return []
    root_names = {entry.get("name") for entry in root}
    if not root_names.intersection(MANIFEST_NAMES):
        return []
    try:
        return released_package_candidates(repository, item, root)
    except subprocess.CalledProcessError as error:
        if http_status(error) != "404":
            record_error(results, "Package release metadata", error)
        return []
    except (ValueError, tomllib.TOMLDecodeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        record_error(results, "Package metadata", error)
        return []


def released_package_candidates(repository: str, item: dict, root: list[dict]) -> list[dict]:
    """Find the packages a repository with a GitHub release publishes.

    :param repository: The repository's ``owner/name`` identifier.
    :param item: The repository's metadata from the organization inventory.
    :param root: Entries returned by the repository's root contents endpoint.
    :returns: The package candidates, or an empty list if the repository has no release.
    :raises subprocess.CalledProcessError: If a GitHub API request fails.
    :raises ValueError: If GitHub returns an unexpected response shape.
    :raises UnicodeDecodeError: If a workflow or manifest is not UTF-8.
    """
    release = api_object(f"repos/{repository}/releases/latest")
    if not isinstance(release, dict):
        return []
    release_tag = release.get("tag_name")
    if not isinstance(release_tag, str):
        return []
    default_branch = item.get("default_branch") or "main"
    branch_ref = quote(default_branch, safe="")
    workflow_text = ""
    for path in workflow_paths(repository, branch_ref):
        encoded_path = quote(path, safe="/")
        source = file_text(f"repos/{repository}/contents/{encoded_path}")
        workflow_text += "\n" + source
    return package_candidates(repository, root, release_tag, workflow_text)


def check_package(results: dict, repository: str, candidate: dict) -> None:
    """Compare one package's registry version with its release, if it has one, and try to install it.

    :param results: The mutable scan results.
    :param repository: The repository's ``owner/name`` identifier.
    :param candidate: The package, from :func:`package_candidates`.
    """
    ecosystem = candidate["ecosystem"]
    name = candidate["name"]
    expected = candidate["expected_version"]
    releases_url = f"https://github.com/{repository}/releases"
    try:
        available = registry_version(ecosystem, name)
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, HTTPError):
            status = str(error.code)
        else:
            status = "unavailable"
        detail = f"Could not read `{name}` registry metadata ({status})."
        add_finding(results, repository, f"{ecosystem} registry", detail, releases_url)
        return
    # A workspace package has no expected version: see package_candidates.
    if expected is not None:
        available_version = normalized_version(available)
        expected_version = normalized_version(expected)
        if available_version != expected_version:
            detail = f"`{name}` latest registry version is `{available}`; latest stable GitHub release is `{expected}`."
            add_finding(results, repository, f"{ecosystem} version mismatch", detail, releases_url)
    try:
        install_package(ecosystem, name, available)
    except (OSError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            status = f"exit {error.returncode}"
        else:
            status = "tool unavailable"
        detail = f"Installing `{name}@{available}` failed ({status})."
        add_finding(results, repository, f"{ecosystem} install", detail, releases_url)
