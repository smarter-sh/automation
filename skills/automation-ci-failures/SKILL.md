---
name: automation-ci-failures
description: Use when the user gives a GitHub Actions run or job url for smarter-sh/automation (github.com/smarter-sh/automation/actions/runs/<id>, optionally /job/<id>), or says a scheduled job failed, didn't run, ran late, merged or opened something it shouldn't have, or stopped posting to Slack. Covers downloading the failed job's log with the bundled script, telling whether the job or the repository it checks is at fault, the usual causes (an expired or under-scoped PAT, schedules disabled after 60 days, a target repository's own failure, transient GitHub or registry errors), reproducing it, fixing it, and reporting.
---

# Investigating a Failed Scheduled Run

A failure here has two possible owners: **this repository's workflow** (a bug, a missing secret, a moved path), or
**the repository it checked** (its tests fail, its package doesn't install). The second is the job doing its work.
Find out which before changing anything.

## 1. Download the failure

```console
skills/automation-ci-failures/scripts/fetch-run-logs.sh <run url | job url | run id> [output dir]
```

Use your scratchpad as the output dir. The script prints the run's workflow, event (`schedule` or
`workflow_dispatch`), commit, every job and its conclusion, then each failed job's errors (Python unittest blocks,
Vitest failures, the `##[error]` lines), and writes the whole log to `<job id>.log`. It retries GitHub's 502s,
strips the color codes that make `gh run view --log-failed` refuse to print, and, without a `gh` login, reads
`GH_TOKEN` from the repository's `.env` without printing it. Never ask the user to `gh auth login`, and never read
or print `.env` yourself.

In a matrix job over repositories, the job's name says which repository failed.

## 2. The usual causes

| Symptom                                                                 | Likely cause                                                                                                                                                                                                  |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No runs at all, for every job, for days                                 | Schedules disabled after 60 days without a commit. `gh api repos/smarter-sh/automation/actions/workflows --jq '.workflows[] \| "\(.state)\t\(.path)"'` shows `disabled_inactivity`. The user re-enables them. |
| A changed schedule never ran                                            | The change isn't on the default branch yet, or the cron is wrong (UTC; check it at crontab.guru).                                                                                                             |
| `HTTP 401`, `Bad credentials`, `could not read Username`                | The `PAT` expired or was revoked. The user replaces the secret.                                                                                                                                               |
| `HTTP 403`, `Resource not accessible`, `refusing to allow ... workflow` | The PAT lacks a scope (pushing a file under `.github/workflows/` needs `workflow`), or a step used `GITHUB_TOKEN` to write to another repository.                                                             |
| `Context access might be invalid`, an empty secret                      | The secret doesn't exist in this repository. `gh secret list --repo smarter-sh/automation` (names only).                                                                                                      |
| A step can't find a file that exists in smarter                         | The workflow moved here and still assumes smarter is checked out at the root. See `automation-workflows`, "Moving a workflow".                                                                                |
| The target repository's tests or install failed                         | The job worked. Reproduce in `../<repo>` at the ref it checked, and report it as that repository's problem; fix it there only if the user asks.                                                               |
| 502/503, `ECONNRESET`, a registry timeout, `rate limit`                 | Transient. Check the job's previous runs (`gh run list --workflow <file>.yml --limit 5`); if they passed, the user re-runs it.                                                                                |
| Slack step failed but the run passed                                    | Expected: Slack steps are `continue-on-error`. A missing post usually means `SLACK_BOT_TOKEN` is missing or the bot isn't in the channel.                                                                     |

## 3. Reproduce and fix

- Workflow syntax and expressions: `venv/bin/pre-commit run actionlint --all-files`.
- A script: run its tests (`python3 -m unittest discover -s scripts/tests -t scripts`), add one that reproduces the
  failure, then fix it.
- A target repository's failure: run its own test command in `../<repo>` at the same ref, read-only (no checkout or
  switch: ask the user, or use `git -C ../<repo> show <ref>:<path>`).

Never re-run, start or cancel a workflow yourself, and never undo what a job did in another repository (close a pull
request, revert a merge). Give the user the commands.

## 4. Report

Lead with one sentence: which job, which repository, and whose fault: the workflow's, a secret's, GitHub's, or the
checked repository's. Then the evidence (the error line), the fix and how you verified it, and the command for the
user: the commit (`automation-git-commits`), then `gh workflow run <file>.yml -f dry-run=true` to confirm.
