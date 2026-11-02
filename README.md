# automation

[![Tests](https://github.com/smarter-sh/automation/actions/workflows/test.yml/badge.svg)](https://github.com/smarter-sh/automation/actions/workflows/test.yml)

The scheduled jobs of [The Smarter Project](https://github.com/smarter-sh): GitHub Actions cron
workflows that check and maintain the other `smarter-sh` repositories. This repository has no
application code: each workflow checks out the repository it works on, and acts on it.

## The jobs

| Workflow                                                   | When               | What it does                                                                                                                                                                                                      |
| ---------------------------------------------------------- | ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [API Contract Sync](.github/workflows/apiContractSync.yml) | Mondays, 13:17 UTC | Checks that the SDKs, the cli and the VS Code extension carry the latest [smarter](https://github.com/smarter-sh/smarter) release's api contract, and opens an issue and a pull request in each one that doesn't. |
| [Did You Know?](.github/workflows/didYouKnow.yml)          | Daily, 14:07 UTC   | Posts the web console's fact of the day to the project's Slack.                                                                                                                                                   |
| [Pod Log Review](.github/workflows/podLogReview.yml)       | Daily, 09:23 UTC   | Has Claude Code review the deployed platform's pod logs, and keep smarter's issues current with what it finds.                                                                                                    |

Each one also runs by hand from the Actions tab, with a `dry-run` input that checks and reports
without writing anything. Its header comment says what it does, and which secrets it needs.
[test.yml](.github/workflows/test.yml) runs the checks and the scripts' tests on every pull request.

## Working on it

```bash
make init      # a venv/ with pre-commit, the git hooks, and a .env from .env.example
make lint      # the pre-commit hooks on every file
make test      # the scripts' tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md). AI coding agents start at [CLAUDE.md](CLAUDE.md) and
[skills/](skills/).

## License

[GNU Affero General Public License v3.0](LICENSE)
