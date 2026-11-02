---
name: automation-workflows
description: Use when adding, changing or moving a scheduled GitHub Actions workflow in smarter-sh/automation (a cron job that runs another repository's tests, checks published packages, merges Dependabot pull requests, syncs contracts, posts to Slack, ...), or a script in scripts/ that one runs. Covers the workflow skeleton (header comment, schedule and workflow_dispatch, dry-run, concurrency, permissions, timeouts), checking out and writing to other repositories with the PAT, untrusted input, scripts and their tests, the run summary and Slack, and moving a workflow over from smarter-sh/smarter.
---

# Writing a Scheduled Workflow

## Before writing

1. Read the newest two or three workflows here (`git log --format='%ad %s' -- .github/workflows`), and, for a job
   like one that still lives in smarter, `../smarter/.github/workflows/` (`apiContractSync.yml` is the most
   complete example: release, a matrix over repositories, a report).
2. Read the target repository's own CI (`../<repo>/.github/workflows/`, its `Makefile`). A job that runs a
   repository's tests should run them **exactly** as its CI does: same toolchain version, same install, same
   command. Copy, don't reinvent.

## The skeleton

File name: camelCase, after what it does, e.g. `.github/workflows/packageInstallCheck.yml`.

```yaml
---
#------------------------------------------------------------------------------
# <Title>.
#
# <What it checks or does, to which repositories, and how often. What it writes
# (issues, pull requests, merges, Slack) and when it writes nothing.>
#
# The logic is in scripts/<name>.py, whose tests are in scripts/tests.
#
# Secrets:
#   PAT               <what it does with it>. Without it, the run only checks and reports.
#   SLACK_BOT_TOKEN   optional: <channel>. Without it, nothing is posted.
#------------------------------------------------------------------------------
name: <Title>

on:
  schedule:
    - cron: "17 6 * * 1" # weekly, Monday 06:17 UTC
  workflow_dispatch:
    inputs:
      dry-run:
        description: "Check and report only: write nothing to GitHub, and post nothing to Slack."
        required: false
        type: boolean
        default: false

concurrency:
  group: <kebab-name>
  cancel-in-progress: false

permissions:
  contents: read

env:
  DRY_RUN: ${{ inputs.dry-run && 'true' || 'false' }}
  RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}

jobs:
  <job>:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps: ...
```

Why each part is there:

- **Header comment**: the run's page is all most people see. The comment is what a maintainer reads when a job
  misbehaves, and the only list of the secrets it needs.
- **`workflow_dispatch` with `dry-run`**: the only way to try a job without its side effects, and to try it from a
  branch (schedules run only on the default branch). Every step that writes is `if: env.DRY_RUN != 'true'`.
- **Defaults for every input**: a scheduled run has no `inputs`, so read them as `${{ inputs.x || 'default' }}`.
- **`concurrency` without cancelling**: a manual run and a scheduled one must not push the same branch at once, and
  a half-finished merge or push is worse than waiting.
- **`permissions: contents: read`**: the `GITHUB_TOKEN` needs nothing more; writes to other repositories use the
  PAT. Add a permission only for something this repository itself needs (`issues: write` to file an issue here).
- **`timeout-minutes` on every job**: the default is six hours of billed, stuck runner.
- **A minute that isn't 0**, and a time that doesn't clash with the other jobs (`grep -h 'cron:' .github/workflows/*`).
- **Matrix jobs over repositories** with `fail-fast: false`, so one repository's failure doesn't hide the others.

## Working on other repositories

- Check out with `repository: smarter-sh/<repo>`, a `ref`, a `path`, and `token: ${{ secrets.PAT }}` only if the job
  pushes. Set up the toolchain from the target's own pin where it has one (`go-version-file`, `.nvmrc`,
  `python-version` as its CI uses).
- Use `gh ... --repo smarter-sh/<repo>` with `GH_TOKEN: ${{ secrets.PAT }}` in that step's env only.
- **Make each run idempotent.** A job runs again tomorrow: find and update its own branch, issue or pull request
  (by a fixed branch name or label) instead of opening another, and close them once the problem is gone.
- **Never force-push, and never push to `main`.** Changes go to a branch of the job's own, and into the target's
  `alpha` (or its default) branch through a pull request.
- **Commits a job makes** use a fixed bot identity (`git config user.name/user.email`), a conventional-commit
  message, and never a CI skip instruction: the target's required checks would never start.

### Merging pull requests (e.g. Dependabot)

- Merge only when GitHub says so: `gh pr view <n> --repo ... --json mergeable,mergeStateStatus,statusCheckRollup`,
  and only `mergeStateStatus == "CLEAN"`. Never `--admin`, which bypasses branch protection.
- Restrict by author (`app/dependabot`) and by what changed: e.g. patch and minor updates only, or only the
  ecosystems the job is for. A major version, or a pull request that touches anything else, gets a comment or a
  report line, not a merge.
- Prefer `gh pr merge --auto --squash` where the repository allows auto-merge: GitHub then merges only after the
  required checks pass, even if they finish after the run.

## Untrusted input

Pull request titles, branch names, commit messages, issue text, package metadata and logs are written by others.

- Never put them in `${{ }}` inside a `run:` block, where they become shell code. Pass them through `env:` and use
  `"$VAR"`. actionlint flags many of these.
- Don't echo them into `$GITHUB_OUTPUT` or `$GITHUB_ENV` without stripping newlines.
- A job that hands untrusted text to Claude Code follows `../smarter/.github/workflows/podLogReview.yml`: Harden-Runner
  with an egress allow list, credentials removed before Claude starts, read-only tools, writes only through a
  server that checks them.

## Scripts and tests

- More than a few lines of shell, or any parsing or decision, belongs in `scripts/<name>.py`: standard library only
  (the runner has no virtual environment), run with `python3 -I scripts/<name>.py`, with subcommands for each step
  (e.g. `status`, `publish`, `report`), as `../smarter/scripts/api_contract_sync.py` does.
- Test it in `scripts/tests/test_<name>.py` with `unittest`, faking every network call (`gh`, `urllib`, Slack).
  No test may reach GitHub, Slack, a package registry or AWS.
- Shell scripts start with `set -euo pipefail` and pass ShellCheck (actionlint runs it on `run:` blocks too).

## Reporting

- Write a summary to `$GITHUB_STEP_SUMMARY`: what was checked, what was found, what was written, with links.
- Slack: one message per run, posted by `scripts/slack_post.py`, in a step with `if: always()` and
  `continue-on-error: true`, so a Slack outage never fails the job. Post a short clean bill of health when all is
  well, so silence means something is wrong.
- Fail the run when something needs a person (a repository's tests failed, a publish failed). A green run must mean
  "nothing to do".
- Nothing secret, nothing from a private log, and no account id in the summary, the job log or an artifact: this
  repository's logs are public.

## Moving a workflow from smarter-sh/smarter

1. Copy the workflow, and every `scripts/` file it runs (with its tests, and `slack_post.py` if it posts), and any
   `.github/prompts/` file it reads.
2. Its steps assumed the smarter checkout at the workspace root. Add a checkout of `smarter-sh/smarter` with a
   `path: smarter` wherever it still needs that repository's files (its `skills/*/scripts`, `.github/prompts`,
   `smarter/react`, `requirements/`), and fix those paths.
   A job that runs Claude Code in the smarter checkout (`podLogReview.yml`) checks out both repositories side by
   side (`path: automation` and `path: smarter`) instead: Claude Code reads every `CLAUDE.md` above its working
   directory, so smarter nested inside this checkout would also get this repository's instructions.
3. `git push ... ${{ github.repository }}` and `github.repository` in links now mean smarter-sh/automation: change
   them to the repository the job works on.
4. Steps that used the `GITHUB_TOKEN` to write to smarter now need the PAT.
5. Check that its secrets exist here (`gh secret list --repo smarter-sh/automation`, which lists names only), and
   list any that are missing for the user.
6. Tell the user to delete the old workflow from smarter once the new one has had a good run, so the job doesn't
   run twice. Don't delete it yourself as part of this repository's change.
7. Run the hooks and the tests, and give the user the dry-run command.
