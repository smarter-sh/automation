# automation

[![Tests](https://github.com/smarter-sh/automation/actions/workflows/tests.yml/badge.svg)](https://github.com/smarter-sh/automation/actions/workflows/tests.yml)

The scheduled jobs of [The Smarter Project](https://github.com/smarter-sh): GitHub Actions cron workflows that test,
check and maintain the other `smarter-sh` repositories, and report to the project's Slack. This repository has no
application code. Each workflow checks out the repository it works on, acts on it, and posts one message per run.

## The jobs

| Workflow                                                                     | When                              | What it does                                                                                                                                                                                                                                                        | Slack      |
| ---------------------------------------------------------------------------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| [Repository Tests](.github/workflows/repositoryTests.yml)                    | Daily, 08:13 UTC                  | Starts the test workflow (`tests.yml`, or `test.yml`) of every repository on its default branch, waits for the runs, and reports which passed and which failed. 08:13 UTC is 2:13 a.m. Central (3:13 in summer).                                                    | `#general` |
| [Pod Log Review](.github/workflows/podLogReview.yml)                         | Daily, 09:23 UTC                  | Has Claude Code review the deployed platform's pod logs of the last day, read from its Loki, and keep smarter's issues current with what it finds.                                                                                                                  | `#smarter` |
| [Did You Know?](.github/workflows/didYouKnow.yml)                            | Daily, 14:07 UTC                  | Posts the web console's fact of the day.                                                                                                                                                                                                                            | `#general` |
| [API Contract Sync](.github/workflows/apiContractSync.yml)                   | Mondays, 13:17 UTC                | Checks that the SDKs, the cli and the VS Code extension carry the latest [smarter](https://github.com/smarter-sh/smarter) release's api contract, and opens an issue and a pull request in each one that doesn't.                                                   | `#general` |
| [Embed Version Sync](.github/workflows/embedVersionSync.yml)                 | Tuesdays, 13:37 UTC               | Checks that the latest release of the WordPress, Drupal, Google Tag Manager, Shopify and docs site integrations loads the latest `@smarter.sh/ui-chat` from npm, and opens a pull request into `alpha` in each one that doesn't.                                    | `#general` |
| [Dependabot Merge](.github/workflows/dependabotMerge.yml)                    | Wednesdays, 13:47 UTC             | Goes through every repository's open Dependabot pull requests: merges the ones whose checks pass, sets the ones still running to auto-merge, closes the ones with merge conflicts, and lists the closed ones and the ones that need a person (e.g. failing checks). | `#general` |
| [Secrets Age Review](.github/workflows/secretsAgeReview.yml)                 | Fridays, 13:57 UTC                | Checks organization, repository and environment Actions secrets across every repository, and posts names and last-updated dates for secrets older than the configured month threshold (default: 6).                                                                 | `#general` |
| [Repository Security Review](.github/workflows/repositorySecurityReview.yml) | Mondays, 19:17 CST / 20:17 CDT    | Reports drift in Actions token defaults, default-branch protection, secret scanning, and Dependabot security settings across every repository.                                                                                                                      | `#general` |
| [Workflow Security Review](.github/workflows/workflowSecurityReview.yml)     | Tuesdays, 19:37 CST / 20:37 CDT   | Checks all Actions workflows for unpinned actions, explicit token permissions, write scopes, and missing job timeouts.                                                                                                                                              | `#general` |
| [Published Package Health Check](.github/workflows/packageHealthCheck.yml)   | Wednesdays, 19:57 CST / 20:57 CDT | Discovers npm, PyPI, and Go releases, compares registry versions with GitHub releases, and verifies package installs/module downloads.                                                                                                                              | `#general` |
| [Stale Items Report](.github/workflows/staleItemsReport.yml)                 | Thursdays, 20:17 CST / 21:17 CDT  | Reports open pull requests and issues older than one calendar month across active repositories; it does not close or comment.                                                                                                                                       | `#general` |

The four new weekly jobs run on Monday–Thursday evenings Central time, spread out across the nights. Their UTC cron
hours shift by one hour between Central Standard Time and Central Daylight Time.

Each workflow's header comment says exactly what it does, to which repositories, what it writes, and which secrets it
needs. [tests.yml](.github/workflows/tests.yml) has no schedule: it runs the checks and the scripts' tests on every
pull request and every push to `main`.

### How they reach the other repositories

The `GITHUB_TOKEN` that GitHub gives a run reaches only this repository, so everything a job does elsewhere (push a
branch, open or merge a pull request, start a workflow) goes through the `PAT` secret, and shows its owner's name.
A job never pushes to `main`, never force-pushes a branch it doesn't own, and never merges with `--admin`: changes go
through pull requests into each repository's `alpha`, and run that repository's own tests.

The logic is in [scripts/](scripts/): Python, standard library only, with its tests in
[scripts/tests/](scripts/tests/), and no test ever reaches GitHub, Slack, npm or AWS.

## Running a job by hand

Every job runs from the Actions tab, or with `gh`, and has a `dry-run` input: the run checks and reports on its page,
but writes nothing to GitHub and posts nothing to Slack. Try a new or changed job that way first.

```bash
gh workflow run embedVersionSync.yml --repo smarter-sh/automation -f dry-run=true
gh run list --repo smarter-sh/automation --workflow embedVersionSync.yml --limit 1
gh run watch <run id> --repo smarter-sh/automation
```

`-f name=value` sets one of the workflow's inputs; some jobs have more, e.g. `-f base=alpha` or
`-f version=0.39.0` for Embed Version Sync, and `-f ref=alpha` for Repository Tests.

## Secrets

Set in the repository's Settings > Secrets and variables > Actions.

| Secret                                                                         | Used by                                                                                    | For                                                                                                                                                                                                                                                                                                                                                                           |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PAT`                                                                          | every job that acts on another repository, Secrets Age Review and all organization reports | A classic personal access token. `public_repo` to push branches and open pull requests and issues; `workflow` to merge Dependabot's updates of `.github/workflows/`; GitHub's docs ask for `repo` to start another repository's workflows. The organization reports also need `admin:org`, and its owner must be an organization owner with admin access to every repository. |
| `SLACK_BOT_TOKEN`                                                              | every job that posts                                                                       | The Smarter Slack app's bot token, with `chat:write.public` (or the bot in the channel). Optional: without it, nothing is posted.                                                                                                                                                                                                                                             |
| `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`                               | Pod Log Review                                                                             | Claude Code.                                                                                                                                                                                                                                                                                                                                                                  |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `EKS_CLUSTER_NAME` | Pod Log Review                                                                             | Reading the pods' inventory and logs, with kubectl and a port-forward to the environment's Loki. Nothing in the cluster is changed.                                                                                                                                                                                                                                           |

## Schedules

- **After adding a workflow, or changing a `cron:` line, re-enable it as a maintainer.** In this organization, new
  schedules haven't run until a maintainer disabled and re-enabled the workflow, which makes that person the
  schedule's owner. Once the change is on `main`:

  ```bash
  gh workflow disable repositoryTests.yml --repo smarter-sh/automation
  gh workflow enable repositoryTests.yml --repo smarter-sh/automation
  ```

- **Schedules stop after 60 days without a commit.** GitHub turns off a public repository's schedules when nothing
  was committed for 60 days, and only emails a notice. Merging Dependabot's monthly updates (which Dependabot Merge
  does) keeps this repository active. If every job stops at once, check
  `gh api repos/smarter-sh/automation/actions/workflows --jq '.workflows[] | "\(.state)\t\(.path)"'` for
  `disabled_inactivity`.
- **Schedules run only from `main`**, in UTC, and often start late, by an hour or more at busy times. Use
  `workflow_dispatch` to try a branch.

## Working on it

```bash
make init      # a venv/ with pre-commit and ShellCheck, the git hooks, and a .env from .env.example
make lint      # the pre-commit hooks on every file (actionlint runs ShellCheck on the workflows' run: blocks)
make test      # the scripts' tests
```

Activate the venv (`source venv/bin/activate`) before `make lint` or a commit, so that actionlint finds ShellCheck;
without it, actionlint skips those checks and only CI reports them.

See [CONTRIBUTING.md](CONTRIBUTING.md). AI coding agents start at [CLAUDE.md](CLAUDE.md) and [skills/](skills/).

## License

[GNU Affero General Public License v3.0](LICENSE)
