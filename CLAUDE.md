# CLAUDE.md

Guidance for Claude Code (and other coding agents) working in this repository.

This repository holds the scheduled GitHub Actions workflows of [The Smarter Project](https://github.com/smarter-sh):
cron jobs that test, check and maintain the other `smarter-sh` repositories (run test suites, check that published
packages still install, merge Dependabot pull requests, sync contracts, post to Slack, ...). It has no application
code. A workflow here checks out the repository it works on, and acts on it with the `PAT` secret, because the
default `GITHUB_TOKEN` only reaches this repository. Local checkouts of the other repositories usually sit next to
this one (`../smarter`, `../smarter-cli`, `../smarter-python`, ...).

Detailed know-how lives in the Agent Skills under `skills/` (symlinked to `.claude/skills`). **Load
`automation-development` first for any change.** Its table says which narrower skill covers each kind of work. This
file holds only what applies to nearly every task.

## Hard rules

1. **Never run git write commands.** "Write a git commit" means print the command as text. Never run `git commit`,
   `git add`, `git reset`, `git push` or anything else that changes the index or history. Format and authorship are
   in `automation-git-commits`.
2. **Never start, re-run or cancel a workflow, and never act on another repository by hand.** No `gh workflow run`,
   `gh run rerun`, `gh pr merge`, `gh pr close`, `gh issue close`, `gh secret set`, and no `gh api` call that
   writes. These workflows push branches, merge pull requests and post to Slack with real credentials. Hand the
   user the command instead. Read-only `gh` calls (`gh run list`, `gh run view`, `gh pr list`, `gh api` GETs) are
   fine.
3. **Every workflow can run as a dry run.** A `workflow_dispatch` input `dry-run` makes it check and report only:
   no push, no merge, no issue, no Slack post. Tell the user to try a new or changed workflow with it first.
4. **This repository is public, and so are its workflow logs.** No tokens, account IDs, private hostnames or customer
   data in a workflow, script, skill or log line. Secrets are referenced as `${{ secrets.NAME }}`, never echoed.
5. **The pre-commit hooks are the standard** (actionlint, check-yaml, codespell, detect-private-key, file hygiene).
   Run them yourself on the changed files before you finish.
6. **Logic goes in `scripts/`, with tests.** A workflow step that is more than a few lines of shell calls a Python
   script in `scripts/` (standard library only, run with `python3 -I`), tested in `scripts/tests/`. Run the tests
   yourself.

## Repository map

| Path                     | Contents                                                                                                                                                                                                |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.github/workflows/`     | One workflow per job, named in camelCase (`apiContractSync.yml`), each with a header comment. `tests.yml` is the exception: no schedule, it runs the hooks and the scripts' tests on every pull request |
| `scripts/`               | Python (standard library only) and shell that the workflows run                                                                                                                                         |
| `scripts/tests/`         | unittest tests of `scripts/`. `-t scripts`: the scripts import each other by name                                                                                                                       |
| `.env.example`           | Local copies of the secrets (GH_TOKEN, Slack, Claude, AWS). Never read or print `.env`                                                                                                                  |
| `.github/prompts/`       | Instructions and permission rules for the jobs that run Claude Code                                                                                                                                     |
| `.github/dependabot.yml` | Keeps the actions the workflows use, and the pre-commit hooks, current                                                                                                                                  |
| `skills/`                | Agent Skills for working on this repository                                                                                                                                                             |

## Common commands

```bash
# once: a venv with pre-commit, and the git hook
python3 -m venv venv && venv/bin/pip install pre-commit && venv/bin/pre-commit install

# the changed files only (zsh doesn't word-split $FILES, so use xargs)
git diff --name-only | xargs venv/bin/pre-commit run --files
venv/bin/pre-commit run --all-files

# the scripts' tests
python3 -m unittest discover -s scripts/tests -t scripts

# what ran, read-only
gh run list --limit 20
gh run list --workflow <file>.yml --limit 5
```

## Conventions

- **Pattern first.** Read the newest two or three workflows here (and, while jobs are still moving over, the
  scheduled workflows in `../smarter/.github/workflows/`) and copy their shape: the header comment, the
  `schedule` + `workflow_dispatch` triggers, `dry-run`, `concurrency`, `permissions: contents: read`,
  `timeout-minutes`, the summary on the run's page, the Slack post. See `automation-workflows`.
- **Cron times are UTC**, run only from the default branch, and may start late. Spread jobs over the day rather than
  stacking them on the hour.
- **Nothing a workflow writes asks GitHub to skip CI.** Never put a CI skip instruction in a commit message or pull
  request a workflow makes: the target repository's required checks would then never start.

## Definition of done

- The workflow follows an existing pattern and has a header comment that lists its secrets.
- The pre-commit hooks pass on the changed files, and the scripts' tests pass.
- Your summary says what changed, what you verified, what you didn't (a workflow can only really be tried on
  GitHub), and the exact `gh workflow run ... -f dry-run=true` command for the user to try it.
