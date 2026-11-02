# Agent Skills for automation

This folder holds the [Agent Skills](https://agentskills.io/specification) for working on this repository with an
AI coding agent such as Claude Code. This README is written for people.

A skill is a folder of written know-how that an agent loads only when a task needs it. Each one contains a
`SKILL.md` file, and sometimes a `scripts/` folder. These skills are developer tooling, like
`.pre-commit-config.yaml`. No workflow loads them.

They follow the skills of [smarter-sh/smarter](https://github.com/smarter-sh/smarter/tree/main/skills), whose
README explains the format, the security considerations and when to add a skill, at more length.

## What's here

Start with `automation-development`. It sets out the rules that apply to every change, and says which narrower
skill to load for each kind of task.

| Skill                    | Use it when                                                                  |
| ------------------------ | ---------------------------------------------------------------------------- |
| `automation-development` | You're making any change. Start here.                                        |
| `automation-workflows`   | You're adding, changing or moving a scheduled workflow, or a script it runs. |
| `automation-ci-failures` | A scheduled run failed, or the user gives you an Actions run url.            |
| `automation-git-commits` | You've been asked to write a commit message.                                 |

## Claude Code

Claude Code looks for a project's skills in `.claude/skills/`, not in a top-level `skills/` folder. This
repository connects the two with a committed symlink, created with:

```bash
# from the repository root
mkdir -p .claude
ln -s ../skills .claude/skills
```

If the symlink breaks, for example on a Windows checkout without symlink support, Claude Code silently stops
seeing the skills. `CLAUDE.md`, at the repository root, is what Claude Code reads at the start of every session.
`.claude/settings.local.json` holds each contributor's own permissions, and git ignores it.

## Adding or changing a skill

- Name the folder `automation-<topic>`, and set the frontmatter `name` to the same value.
- Write the `description` as "Use when ...", naming the situations that should trigger it.
- Keep `SKILL.md` short. Point to real files rather than copying them, because copies go stale.
- Explain _why_ a rule exists, so an agent can handle the cases you didn't list.
- Add it to the table above, and to the index in `automation-development/SKILL.md`.
- No secrets, hostnames or account IDs: this repository is public.

**Review a change to this folder as carefully as a change to a workflow.** An agent follows a skill's instructions,
often without asking, and the workflows here hold tokens that can push to and merge in every `smarter-sh`
repository. A hidden instruction or a new script is a prompt injection risk for the next contributor.
