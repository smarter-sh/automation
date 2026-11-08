---
name: automation-development
description: Start here for any change to the smarter-sh/automation repository (the scheduled GitHub Actions workflows that test, check and maintain the other smarter-sh repositories). Gives the rules, how a workflow here reaches another repository, the secrets, GitHub's scheduling quirks, the checks, and an index of the narrower automation-* skills.
---

# Automation Development

This repository is a set of cron jobs: `.github/workflows/*.yml`, plus the scripts in `scripts/` that they run.
Each job works on _other_ `smarter-sh` repositories. `CLAUDE.md` has the hard rules; this skill explains them.

## Code style

Write Python for people who are learning how to program in Python. Prefer plain, familiar syntax, descriptive names,
and explicit steps over clever or compressed code. Readability beats brevity.

**One operation per line.** Each line does one thing: one call, one transformation, one test, one assignment. When a
line calls a function on the result of another call, filters and maps and joins in one go, or puts a condition
inside an f-string, name each intermediate value on its own line. A good name explains the step better than a
comment would. This is the rule, not a guideline: break it only for an expression any Python beginner reads at a
glance (`len(items) - 1`, `", ".join(names)`, `name.strip()`).

```python
# no: four operations, and the reader has to unpack them from the inside out
lines.append(f"| {result.repository} | {result.workflow.rsplit('/', 1)[-1]} | {emoji} {text}{f' ({result.reason})' if result.reason else ''} |")

# yes
workflow_file = result.workflow.rsplit("/", 1)[-1]
reason = f" ({result.reason})" if result.reason else ""
lines.append(f"| {result.repository} | {workflow_file} | {emoji} {text}{reason} |")
```

The same applies to:

- **Comprehensions:** one `for` and at most one simple `if`. Nested comprehensions, a comprehension that builds a
  dict of comprehensions, or one whose expression is itself a chain of calls, become a plain `for` loop.
- **Conditions:** at most two checks joined by `and`/`or`. More, or any check that needs reading twice, gets a
  named boolean (`is_dependabot = ...`) or a small function with a docstring.
- **Chained calls:** `(run_json(...) or {}).get("workflows", [])` is three steps; write three lines.
- **Packed assignments:** `a, b, c = x, y, z` only for values that belong together (a pair a function returns);
  never to set unrelated variables in one line.
- **Ternaries:** fine for choosing between two plain values; never nested, never wrapped around a call chain.
- **Functions:** short and doing one thing. flake8 fails at complexity 10; split well before that.

Use comments to explain why, not what, and avoid repeating the code in the comment.

## Which skill to load

| Task                                                          | Skill                    |
| ------------------------------------------------------------- | ------------------------ |
| Add, change or move a scheduled workflow, or a script it runs | `automation-workflows`   |
| A run failed, or the user gives you an Actions url            | `automation-ci-failures` |
| Write a commit message                                        | `automation-git-commits` |

## How a workflow reaches another repository

The `GITHUB_TOKEN` that GitHub gives a run can read and write only this repository. Anything else needs the `PAT`
secret: a classic personal access token with the `public_repo` scope, the same one the smarter repository's
workflows use.

- **Checkout:** `actions/checkout` with `repository: smarter-sh/<repo>`, `ref: <branch>`, `path: <folder>` and
  `token: ${{ secrets.PAT }}` (only when the job will push; otherwise leave the token out, since public repositories
  need none to read).
- **gh:** `GH_TOKEN: ${{ secrets.PAT }}` in the step's `env`, and `--repo smarter-sh/<repo>` on every command.
- **Pushes and pull requests made with the PAT start the target's workflows.** That is what we want: a branch a job
  pushes gets the target's Tests like any other. (Events made with `GITHUB_TOKEN` start no workflows, which is
  another reason not to use it for this.)
- **Whatever a job does, it does as the PAT's owner.** Commits, comments and merges show that user's name. Keep each
  job's writes small and explained, and link back to the run (`RUN_URL`) in anything it writes.

## GitHub's scheduling quirks

These bite cron-only repositories in particular:

- **Schedules are disabled after 60 days without activity.** In a public repository, GitHub turns off every
  `schedule` trigger when nothing was committed to it for 60 days, and only emails a notice. A repository that
  holds only cron jobs can go quiet that long. Dependabot's monthly pull requests for the actions keep it active
  only when someone merges them. If every job stops at once, check this first:
  `gh api repos/smarter-sh/automation/actions/workflows --jq '.workflows[] | "\(.state)\t\(.path)"'`
  (`disabled_inactivity` is the tell; the user re-enables it in the Actions tab).
- **Schedules run only from the default branch.** A changed cron on a feature branch does nothing until it is
  merged. Use `workflow_dispatch` to try a branch.
- **Times are UTC, and runs start late**, by an hour or more at busy times, and on the hour most of all. Avoid
  `0 * * * *`-style minutes, and don't let one job depend on another having finished at a fixed time.
- **A scheduled run has no `inputs`.** Every input needs a default that applies to schedules too, e.g.
  `BASE: ${{ inputs.base || 'alpha' }}` in the workflow's `env`.

## Secrets

Set by the user in the repository's settings. Never ask for their values, print them, or write them to a file that
is uploaded. Each workflow's header comment lists the ones it uses, and what it does without each optional one.

| Secret                                         | Used for                                                                                                                                                                                                                                                      |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PAT`                                          | Pushing, pull requests, issues and merges in other repositories, and starting their workflows. Merging a pull request that changes `.github/workflows/` (Dependabot's github-actions updates) needs its `workflow` scope; starting a workflow may need `repo` |
| `SLACK_BOT_TOKEN`                              | Posting a run's report to Slack (optional: no token, no post)                                                                                                                                                                                                 |
| `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` | Jobs that run Claude Code                                                                                                                                                                                                                                     |
| `AWS_*`, `EKS_CLUSTER_NAME`                    | Jobs that read a deployed environment (kubectl, read-only)                                                                                                                                                                                                    |

Add a row when a new workflow needs a new secret, and tell the user to create it.

## The checks

`.pre-commit-config.yaml` is the standard: file hygiene, `check-yaml`, `detect-private-key`, codespell, and
**actionlint**, which type-checks workflows (expressions, `needs`, inputs, matrix keys) and runs ShellCheck on every
`run:` block. Most mistakes in a workflow are caught by it, and the rest only on GitHub.

```bash
git diff --name-only | xargs venv/bin/pre-commit run --files   # changed files (add new ones by hand)
venv/bin/pre-commit run --all-files
python3 -m unittest discover -s scripts/tests -t scripts
```

You can't run a workflow locally. Say so in your summary, and give the user the dry-run command:
`gh workflow run <file>.yml --ref <branch> -f dry-run=true`, then `gh run watch` or `gh run list --workflow <file>.yml`.

## Related repositories

Local checkouts sit next to this one: `../smarter` (the platform; its `scripts/` and `skills/` hold code several
jobs used before they moved here), `../smarter-cli`, `../smarter-python`, `../smarter-nodejs`, `../smarter-golang`,
`../smarter-vscode-yaml`, `../smarter-console`, `../example-manifests`, `../stackademy`, `../extensions-registry`. Read them to learn how a
repository builds and tests, so a job does exactly what that repository's own CI does. Don't change them as part of
a task here unless the user asks.
