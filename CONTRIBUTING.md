# Contributing

Thank you for helping with The Smarter Project's scheduled jobs. This repository holds only GitHub
Actions workflows (`.github/workflows/`) and the scripts they run (`scripts/`), so a contribution is
usually a new job, a change to what a job checks or writes, or a fix to a script.

## Set up

```bash
git clone https://github.com/smarter-sh/automation.git
cd automation
make init      # a venv/ with pre-commit, the git hooks, and a .env from .env.example
```

`.env` holds your local copies of the repository's secrets, for the few tasks that need one (reading
runs with `gh`, trying a Slack post). [.env.example](.env.example) says what each is for. Never commit it.

The local checkouts of the repositories the jobs work on (`smarter`, `smarter-cli`, the SDKs, ...)
usually sit next to this one, so you can read how each builds and tests.

## Make a change

- Follow an existing workflow: its header comment (what it does, the secrets it uses), the
  `schedule` and `workflow_dispatch` triggers, a `dry-run` input, `concurrency`,
  `permissions: contents: read`, `timeout-minutes`, a summary on the run's page, and one Slack post.
  [CLAUDE.md](CLAUDE.md) and the skills in [skills/](skills/) explain each part.
- Logic goes in a Python script in `scripts/`, standard library only, with tests in `scripts/tests/`
  that never reach GitHub, Slack, a registry or a cloud.
- This repository and its workflow logs are public: never put a token, account id or private hostname
  in a workflow, a script or a log line.

## Check it

```bash
make lint      # the pre-commit hooks, actionlint among them, on every file
make test      # the scripts' tests
```

A workflow can only really be tried on GitHub. Push your branch and start it as a dry run, which
writes nothing:

```bash
gh workflow run <file>.yml --ref <branch> -f dry-run=true
gh run list --workflow <file>.yml --limit 1
```

Put the dry run's url in your pull request. Schedules only run from `main`, so a changed cron takes
effect when it is merged.

## Commit messages

Conventional commits, e.g. `ci(pod-log-review): read the logs of the last 25 hours`. The commit-msg
hook checks them. The types and scopes are in the `automation-git-commits` skill.

## Code of conduct

Please read our [Code of Conduct](CODE_OF_CONDUCT.md). Security problems go through
[SECURITY.md](SECURITY.md), never a public issue.
