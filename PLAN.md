# Codex Advisor implementation plan

Build a local Codex plugin that reviews activity independently, explains its state,
and returns actionable advice without taking over the primary agent.

1. **Verify the host contract.** Audit released hook documentation and Rust handlers;
   distinguish tool boundaries from user turns, background feedback from continuation,
   and explicit interruption from normal completion. Record the supported baseline.
2. **Implement the runtime.** Use Python's standard library and SQLite transactions
   for per-session state, cancellation generations, coalesced activity, bounded review
   time, finding deduplication, and at most one correction per user request. Start with
   ephemeral Codex CLI reviews using the user's existing login, with hooks disabled.
3. **Make state understandable in the Codex CLI.** Return review notes through native
   hook messages, with severity and evidence. Provide terminal status, doctor,
   pause/resume, and explicit delivery labels. Never equate a timeout with a clean review
   or claim that returning feedback proves the main model acted on it.
4. **Verify behavior.** Exercise hook entrypoints in subprocesses, overlapping reviews,
   cancellation, stale completions, compaction, repeated Stop, missing transcripts,
   invalid reviewer output, and terminal output. Perform a real Codex
   smoke test when the environment permits it.
5. **Ship.** Document installation, trust, model selection, costs and limitations;
   validate packaging, inspect the diff, commit and push to the requested repository.

Scope: macOS/Linux, Python 3.9+, recent Codex with async command hooks. The first
release reviews a bounded snapshot rather than granting the advisor workspace tools.
No native Codex sidebar extension, undocumented transcript writes, or idle-task wakeups.

See [hook contract](docs/hook-contract.md) for the lifecycle decisions and evidence.

## Verification completed

- 35 deterministic regression tests pass.
- Plugin and skill validators pass.
- Real Codex reviewer returned structured output using the existing login.
- Real Codex host dispatched SessionStart, UserPromptSubmit, async PostToolUse,
  initial Stop, exactly one guarded continuation Stop, and SessionEnd.
- All review presentation is in the CLI through native hook notices.
