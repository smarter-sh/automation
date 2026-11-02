# Pull Request Template

## Type of Change

<!-- What type of change does your code introduce? -->

- [ ] New scheduled job
- [ ] Change to a job
- [ ] Bug fix
- [ ] Documentation
- [ ] Chore

## Resolves

- Fixes #[Add issue number here.]

## Changes

<!-- Describe your changes in detail: which workflows and scripts, and what each now does. -->

_Describe what this Pull Request does_

## What the job writes

<!-- Issues, pull requests, merges, Slack posts, and in which repositories. "Nothing new" is a fine answer. -->

_Describe what the changed jobs write, and where_

## Secrets

<!-- New or changed secrets, by name only. Never paste a value. -->

_List secrets, or "none"_

## Testing

<!-- A workflow can only really be tried on GitHub. Give the dry run you ran, and its run url. -->

- [ ] `make lint` and `make test` pass
- [ ] Dry run: `gh workflow run <file>.yml --ref <branch> -f dry-run=true`, run: _url_

## Breaking Changes

<!-- e.g. a schedule moved, a secret renamed, a job that now writes somewhere new -->

_Describe any breaking changes_
