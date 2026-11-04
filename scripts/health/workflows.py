#!/usr/bin/env python3
"""The ``workflow`` scan: full-SHA action pins, explicit token permissions, and job timeouts in workflow files.

Part of :mod:`health.organization_health`, which runs it; see that module for the scans and how
they are run. It uses only Python's standard library and ``gh``.
"""

import re
import subprocess  # nosec B404: only for CalledProcessError
from urllib.parse import quote

from health.common import (
    add_finding,
    file_text,
    is_branch_name,
    is_organization_repository,
    list_repositories,
    new_results,
    record_error,
    workflow_paths,
)


def is_pinned_to_sha(reference: str) -> bool:
    """Check that an action reference ends in a full 40-character commit SHA.

    :param reference: The value of a ``uses:`` key, such as ``actions/checkout@<sha>``.
    :returns: Whether the reference is pinned to a full commit SHA.
    """
    parts = reference.rsplit("@", 1)
    if len(parts) < 2:
        return False
    version = parts[-1]
    return re.fullmatch(r"[0-9a-fA-F]{40}", version) is not None


def unpinned_action_findings(source: str) -> list[tuple[str, str]]:
    """Find actions that are not pinned to a full commit SHA.

    :param source: The contents of one GitHub Actions workflow file.
    :returns: Category and description pairs, one per unpinned action.
    """
    findings = []
    uses_pattern = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)
    for reference in uses_pattern.findall(source):
        is_local_or_docker = reference.startswith("./") or reference.startswith("docker://")
        if is_local_or_docker:
            continue
        if not is_pinned_to_sha(reference):
            findings.append(("Unpinned action", f"`{reference}` is not pinned to a full commit SHA."))
    return findings


def job_blocks(source: str) -> list[tuple[str, str]]:
    """Split a workflow's ``jobs:`` section into its jobs.

    :param source: The contents of one GitHub Actions workflow file.
    :returns: Each job's name and the text of its body, in file order.
    """
    jobs = re.search(r"(?m)^jobs:\s*$", source)
    if not jobs:
        return []
    tail = source[jobs.end() :]
    job_starts = list(re.finditer(r"(?m)^ {2}[A-Za-z0-9_-]+:\s*$", tail))
    blocks = []
    for index, match in enumerate(job_starts):
        start = match.end()
        if index + 1 < len(job_starts):
            end = job_starts[index + 1].start()
        else:
            end = len(tail)
        heading = match.group().strip()
        job_name = heading.removesuffix(":")
        blocks.append((job_name, tail[start:end]))
    return blocks


def permission_findings(source: str, blocks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Find missing token permissions and write permissions.

    :param source: The contents of one GitHub Actions workflow file.
    :param blocks: The workflow's jobs, from :func:`job_blocks`.
    :returns: Category and description pairs for each permissions finding.
    """
    findings = []
    has_workflow_permissions = re.search(r"(?m)^permissions\s*:", source) is not None
    job_permissions = [re.search(r"(?m)^ {4}permissions\s*:", block) is not None for _, block in blocks]
    every_job_has_permissions = bool(job_permissions) and all(job_permissions)
    if not has_workflow_permissions and not every_job_has_permissions:
        findings.append(("Token permissions", "The workflow does not declare explicit token permissions."))
    if re.search(r"(?m)^permissions\s*:\s*write-all\s*$", source):
        findings.append(("Token permissions", "The workflow grants write-all permissions."))
    elif re.search(r"(?m)^ {2,}[A-Za-z_-]+\s*:\s*write\s*(?:#.*)?$", source):
        findings.append(("Token permissions", "The workflow grants one or more write permissions."))
    return findings


def workflow_findings(source: str) -> list[tuple[str, str]]:
    """Find unpinned actions, write permissions, and missing job timeouts.

    :param source: The contents of one GitHub Actions workflow file.
    :returns: Category and description pairs for each workflow finding.
    """
    blocks = job_blocks(source)
    findings = unpinned_action_findings(source)
    findings.extend(permission_findings(source, blocks))
    for job_name, block in blocks:
        if not re.search(r"(?m)^ {4}timeout-minutes\s*:", block):
            findings.append(("Missing timeout", f"Job `{job_name}` has no `timeout-minutes`."))
    return findings


def scan_workflows() -> dict:
    """Review workflow files on every repository's default branch.

    :returns: The scan results with findings and counts of incomplete API requests.
    """
    results = new_results("workflow")
    for item in list_repositories(results):
        repository = item.get("full_name")
        branch = item.get("default_branch")
        if not is_organization_repository(repository):
            record_error(results, "Repository inventory")
            continue
        if not is_branch_name(branch):
            record_error(results, "Default branch")
            continue
        scan_repository_workflows(results, repository, branch)
    return results


def scan_repository_workflows(results: dict, repository: str, branch: str) -> None:
    """Review the workflow files on one repository's default branch.

    :param results: The mutable scan results.
    :param repository: The repository's ``owner/name`` identifier.
    :param branch: The repository's default branch name.
    """
    branch_ref = quote(branch, safe="")
    try:
        paths = workflow_paths(repository, branch_ref)
    except (subprocess.CalledProcessError, ValueError) as error:
        record_error(results, "Workflow inventory", error)
        return
    for path in paths:
        encoded_path = quote(path, safe="/")
        try:
            source = file_text(f"repos/{repository}/contents/{encoded_path}?ref={branch_ref}")
        except (subprocess.CalledProcessError, ValueError, UnicodeDecodeError) as error:
            record_error(results, "Workflow file", error)
            continue
        web_url = f"https://github.com/{repository}/blob/{branch}/{path}"
        for category, detail in workflow_findings(source):
            add_finding(results, repository, category, detail, web_url, path)
