---
name: automation-git-commits
description: Use whenever the user asks you to "write a git commit" (or a commit message, or the git add) in the smarter-sh/automation repository. Covers the never-execute rule, authorship, the multiple -m command format, the conventional-commit types and scopes used for workflows, and how thorough the body must be.
---

# Automation Git Commits

Same rules as smarter-sh/smarter's `smarter-git-commits` skill (`../smarter/skills/smarter-git-commits/`), with the
types and scopes of this repository.

## The rule: write, never execute

"Write a git commit", in any wording or tone, means **output the command as text**. Never run `git commit`,
`git add`, `git reset`, `git restore --staged`, `git push`, `git tag`, `git stash`, or any other git command that
writes. Read-only commands (`git status`, `git diff`, `git log`) are fine. Don't volunteer commit messages.

## Authorship

You are the author; Lawrence McDaniel is the co-author.

```console
git commit --author="Claude <noreply@anthropic.com>" \
  -m "<type>(<scope>): <subject>" \
  -m "<body paragraph>" \
  -m "<body paragraph or bullet list>" \
  -m "Co-authored-by: Lawrence McDaniel <lpm0073@gmail.com>"
```

- One `-m` per paragraph. No heredoc, no `-F`, no `git add` in the same block.
- No backticks, `$` or `!` inside the double quotes: the user's shell is zsh, where `!` is history expansion and
  aborts the command, and backticks and `$` run or expand. Reword instead (a workflow expression can be described
  as "the inputs.base expression").
- Never write a CI skip instruction (`[skip ci]` and its variants) anywhere in the message, even to describe one.
- When asked for the `git add` too, write it as a separate code block, listing the paths.

## Subject line

`<type>(<scope>): <subject>`, imperative, lower case, no trailing period, **72 characters or fewer** in total.
Measure it, don't count by eye: `printf %s "<header>" | wc -c`.

- **type:** `ci` for a workflow (new or changed), `fix` for a script or workflow that was broken, `test` for
  `scripts/tests` only, `docs` for the README, `CLAUDE.md` or `skills/`, `chore` for housekeeping and Dependabot
  configuration. `feat` only for a whole new job, and sparingly.
- **scope:** the workflow's short name in kebab-case (`api-contract-sync`, `dependabot-merge`, `did-you-know`), or
  `scripts`, `skills`. Omit it for changes across the repository.

## Body

Written for a reviewer with no context, six months from now:

- Start with the motivation: what the job is for, or what went wrong and how it showed up (link the failed run).
- Then what the job does, step by step, and what it writes where: which repositories, branches, pull requests,
  merges, Slack channel. A reviewer must be able to tell its reach from the message alone.
- Name the secrets it needs, and what it does without the optional ones.
- When a job moved from another repository, say from where, and that the old copy still has to be deleted there.
- End with the status: hooks and tests passed, whether it ran on GitHub (dry run or real), and follow-ups.

## Before writing

1. `git status` and `git diff --stat`, so the message describes the working tree, not your memory of it.
2. Measure the header with `wc -c`.
3. Start your reply with a one-line `Written for:` note naming the audience.
