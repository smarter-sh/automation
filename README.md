# automation

[![Tests](https://github.com/smarter-sh/automation/actions/workflows/tests.yml/badge.svg)](https://github.com/smarter-sh/automation/actions/workflows/tests.yml)

The scheduled jobs of [The Smarter Project](https://github.com/smarter-sh): GitHub Actions cron
workflows that check and maintain the other `smarter-sh` repositories. This repository has no
application code: each workflow checks out the repository it works on, and acts on it.

## The jobs

| Workflow                                                     | When                  | What it does                                                                                                                                                                                                      |
| ------------------------------------------------------------ | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [API Contract Sync](.github/workflows/apiContractSync.yml)   | Mondays, 13:17 UTC    | Checks that the SDKs, the cli and the VS Code extension carry the latest [smarter](https://github.com/smarter-sh/smarter) release's api contract, and opens an issue and a pull request in each one that doesn't. |
| [Embed Version Sync](.github/workflows/embedVersionSync.yml) | Tuesdays, 13:37 UTC   | Checks that the latest release of the WordPress, Drupal, Google Tag Manager, Shopify and docs site integrations loads the latest `@smarter.sh/ui-chat`, and opens a pull request in each one that doesn't.        |
| [Dependabot Merge](.github/workflows/dependabotMerge.yml)    | Wednesdays, 13:47 UTC | Merges every repository's open Dependabot pull requests whose checks pass, closes the ones with merge conflicts, and posts the list to Slack.                                                                     |
| [Repository Tests](.github/workflows/repositoryTests.yml)    | Daily, 08:13 UTC      | Runs every repository's test workflow, overnight in US Central time, and posts the results to Slack.                                                                                                              |
| [Did You Know?](.github/workflows/didYouKnow.yml)            | Daily, 14:07 UTC      | Posts the web console's fact of the day to the project's Slack.                                                                                                                                                   |
| [Pod Log Review](.github/workflows/podLogReview.yml)         | Daily, 09:23 UTC      | Has Claude Code review the deployed platform's pod logs, and keep smarter's issues current with what it finds.                                                                                                    |

Each one also runs by hand from the Actions tab, with a `dry-run` input that checks and reports
without writing anything. Its header comment says what it does, and which secrets it needs.
[tests.yml](.github/workflows/tests.yml) runs the checks and the scripts' tests on every pull request.

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
