---
name: advisor
description: Inspect Codex Advisor status, configure its reviewer model, or pause and resume its reviews when the user asks about this plugin.
---

# Codex Advisor

Run the bundled CLI at `../../scripts/advisor.py` relative to this skill directory.
Use `python3 <absolute-plugin-path>/scripts/advisor.py status` to inspect sessions;
`status --json` gives structured data. State defaults to `~/.local/share/codex-advisor`;
honor `CODEX_ADVISOR_DATA` when set. Never guess that an empty default data directory
proves the plugin is inactive in a different host or configured data directory.

- `doctor` checks Codex CLI and authentication without running a model.
- `pause <session-id>` / `resume <session-id>` controls one session; `all` controls all.
- `configure --model <model-id>` selects a reviewer; `--model inherit` restores the
  primary model. `--interval <seconds>` adjusts background review frequency.

Report failures and partial coverage honestly. “Returned to Codex” only means the
hook emitted feedback, not that Codex consumed or accepted it. Final non-blocker
findings are shown in the CLI without requesting continuation. Historical findings are not proof that an issue is
still unresolved. Reviews use additional Codex model usage.

Do not invoke a new model review just to show status, and do not install, trust, or
enable hooks unless the user's request calls for that change.
