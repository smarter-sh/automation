#!/usr/bin/env python3
"""The ``security`` scan: token defaults, branch protection, secret scanning and Dependabot settings.

Part of :mod:`organization_health`, which runs it; see that module for the scans and how
they are run. It uses only Python's standard library and ``gh``.
"""

import subprocess  # nosec B404: only for CalledProcessError
from urllib.parse import quote

from github_cli import api_object, http_status
from health_common import (
    add_finding,
    is_branch_name,
    is_organization_repository,
    list_repositories,
    new_results,
    record_error,
)


def scan_security() -> dict:
    """Compare repository Actions, branch, secret scanning, and Dependabot settings.

    :returns: The scan results with findings and counts of incomplete API requests.
    """
    results = new_results("security")
    # Private repositories are skipped: on GitHub's Free plan they have no branch protection or
    # secret scanning, and their settings endpoints answer with errors (403s, sometimes 503s).
    all_repositories = list_repositories(results)
    repositories = [item for item in all_repositories if item.get("private") is False]
    results["repositories_scanned"] = len(repositories)
    for item in repositories:
        scan_repository_security(results, item)
    return results


def scan_repository_security(results: dict, item: dict) -> None:
    """Check one repository's security settings.

    :param results: The mutable scan results.
    :param item: The repository's metadata from the organization inventory.
    """
    repository = item.get("full_name")
    default_branch = item.get("default_branch")
    if not is_organization_repository(repository):
        record_error(results, "Repository inventory")
        return
    check_secret_scanning(results, repository)
    check_actions_token_permissions(results, repository)
    check_dependabot(results, repository)
    if not is_branch_name(default_branch):
        record_error(results, "Default branch")
        return
    check_branch_protection(results, repository, default_branch)


def check_secret_scanning(results: dict, repository: str) -> None:
    """Report a repository whose secret scanning is not enabled.

    :param results: The mutable scan results.
    :param repository: The repository's ``owner/name`` identifier.
    """
    try:
        details = api_object(f"repos/{repository}")
        security = details.get("security_and_analysis")
        if not isinstance(security, dict):
            raise ValueError("security settings are unavailable")
        secret_scanning = security.get("secret_scanning") or {}
        scanning_status = secret_scanning.get("status")
    except (subprocess.CalledProcessError, ValueError) as error:
        record_error(results, "Repository security metadata", error)
        return
    if scanning_status != "enabled":
        web_url = f"https://github.com/{repository}"
        add_finding(results, repository, "Secret scanning", "Secret scanning is not enabled.", web_url)


def check_actions_token_permissions(results: dict, repository: str) -> None:
    """Report a repository whose new workflows don't default to a read-only token.

    :param results: The mutable scan results.
    :param repository: The repository's ``owner/name`` identifier.
    """
    try:
        permissions = api_object(f"repos/{repository}/actions/permissions/workflow")
    except (subprocess.CalledProcessError, ValueError) as error:
        record_error(results, "Actions permissions", error)
        return
    if permissions.get("default_workflow_permissions") != "read":
        add_finding(
            results,
            repository,
            "Actions token permissions",
            "New workflows do not default to read-only GITHUB_TOKEN permissions.",
            f"https://github.com/{repository}/settings/actions",
        )


def check_dependabot(results: dict, repository: str) -> None:
    """Report a repository whose Dependabot alerts or security updates are off.

    :param results: The mutable scan results.
    :param repository: The repository's ``owner/name`` identifier.
    """
    settings_url = f"https://github.com/{repository}/settings/security_analysis"
    checks = (
        (f"repos/{repository}/vulnerability-alerts", "Dependabot alerts"),
        (f"repos/{repository}/automated-security-fixes", "Dependabot security updates"),
    )
    for endpoint, label in checks:
        try:
            api_object(endpoint)
        except subprocess.CalledProcessError as error:
            if http_status(error) == "404":
                add_finding(results, repository, label, f"{label} are not enabled.", settings_url)
            else:
                record_error(results, label, error)
        except ValueError as error:
            record_error(results, label, error)


def check_branch_protection(results: dict, repository: str, default_branch: str) -> None:
    """Report a default branch that has no protection, or allows force pushes or deletion.

    :param results: The mutable scan results.
    :param repository: The repository's ``owner/name`` identifier.
    :param default_branch: The repository's default branch name.
    """
    branch = quote(default_branch, safe="")
    branches_url = f"https://github.com/{repository}/settings/branches"
    try:
        protection = api_object(f"repos/{repository}/branches/{branch}/protection")
    except subprocess.CalledProcessError as error:
        if http_status(error) == "404":
            detail = f"The {default_branch} branch has no protection rules."
            add_finding(results, repository, "Default branch protection", detail, branches_url)
        else:
            record_error(results, "Branch protection", error)
        return
    except ValueError as error:
        record_error(results, "Branch protection", error)
        return
    if not protection:
        web_url = f"https://github.com/{repository}"
        add_finding(results, repository, "Default branch protection", "Protection rules are missing.", web_url)
        return
    # Required pull requests and rules that bind administrators are not checked on purpose:
    # semantic-release commits each release straight to the default branch with the PAT, as an
    # administrator, and a single maintainer can't approve their own pull requests.
    force_pushes = protection.get("allow_force_pushes") or {}
    if force_pushes.get("enabled", False):
        detail = "Force pushes are allowed on the default branch."
        add_finding(results, repository, "Default branch protection", detail, branches_url)
    deletions = protection.get("allow_deletions") or {}
    if deletions.get("enabled", False):
        detail = "The default branch can be deleted."
        add_finding(results, repository, "Default branch protection", detail, branches_url)
