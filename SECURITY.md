# Security Policy

This repository holds The Smarter Project's scheduled GitHub Actions workflows. They run with
credentials that reach other `smarter-sh` repositories (a GitHub token that pushes branches, opens
issues and pull requests, and merges), a read-only view of the deployed platform's Kubernetes
cluster, and the Slack and Anthropic tokens. A flaw here can therefore reach every repository of the
project, so we treat security reports as our highest priority. Thank you for helping.

## Reporting a Vulnerability

**Please do not report security vulnerabilities in public GitHub issues,
discussions or pull requests.**

Report them privately, in either of these ways:

1. **GitHub private vulnerability reporting (preferred).** Open the
   repository's [Security tab](https://github.com/smarter-sh/automation/security)
   and choose **Report a vulnerability**. The report, and our discussion of it,
   stay private until an advisory is published.
2. **Email** [lpm0073@gmail.com](mailto:lpm0073@gmail.com), with
   `[smarter security]` in the subject.

Please include the workflow or script, what an attacker can do and with what access, and the steps
to reproduce.

A vulnerability of the Smarter platform itself belongs in
[smarter-sh/smarter's policy](https://github.com/smarter-sh/smarter/security/policy).

## What to Expect

| Stage                                       | Target                 |
| ------------------------------------------- | ---------------------- |
| Acknowledgement of your report              | 3 business days        |
| Initial assessment and severity (CVSS v3.1) | 7 days                 |
| Fix, critical or high severity              | 30 days                |
| Fix, medium or low severity                 | 90 days                |
| Public disclosure                           | when the fix is merged |

## Scope

In scope:

- a workflow or script here that can be made to leak a secret, write to a repository it shouldn't,
  merge something it shouldn't, or run code an outsider controls, e.g. through a pull request title,
  a branch name, an issue, a package's metadata, or a pod log that a job reads;
- prompt injection of the jobs that run Claude Code that gets past their limits: a write that the
  job's prompt does not allow, a credential that reaches Claude or GitHub, or network access beyond
  the job's allow list;
- anything this repository's public workflow logs reveal that they shouldn't.

Out of scope: vulnerabilities of GitHub Actions or third-party actions themselves (report them
upstream, and tell us if a job here is affected), and findings that need a maintainer's account.

## Safe Harbor

We will not take legal action against, or ask law enforcement to investigate, anyone who researches
and reports a vulnerability in good faith under this policy: don't access or change other people's
data, don't degrade the project's services, and give us a reasonable time to fix the issue before
you disclose it.
