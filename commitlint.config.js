/*
 * The commit-msg hook (see .pre-commit-config.yaml) checks each commit message against the
 * conventional commit format, as in smarter-sh/smarter. The types and scopes are in the
 * automation-git-commits skill.
 */
const Configuration = {
  extends: ["@commitlint/config-conventional", "@commitlint/config-angular"],
  rules: {
    // One paragraph, or bullet list, per line (one git commit -m per paragraph), so body and
    // footer lines are longer than config-conventional's 100.
    "body-max-line-length": [0, "always"],
    "footer-max-line-length": [0, "always"],
    // config-angular's types, plus chore, for housekeeping.
    "type-enum": [
      2,
      "always",
      ["build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test"],
    ],
  },
  defaultIgnores: true,
};

module.exports = Configuration;
