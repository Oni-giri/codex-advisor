# Hook contract and design decisions

Audited against the released [Codex hook documentation](https://learn.chatgpt.com/docs/hooks)
and Codex CLI 0.153.4 on 2026-09-24. The public `main` branch can be ahead of a
released binary; use the release documentation and local feature checks together.

| Event | Host behavior | Advisor behavior |
|---|---|---|
| SessionStart | Session startup, resume, clear or compact | Invalidate old work and register the session |
| UserPromptSubmit | User input enters a turn | Store the objective; reset the budget only for a genuinely new request |
| PostToolUse | After a supported tool completes | Async bounded review; emit visible notice and model context |
| Stop | Agent attempts to finish | Synchronous final review; optionally request one continuation |
| Interrupt | User interrupts an active main turn | Invalidate pending work; never resume |
| PreCompact | Before context compaction | Drop stale snapshots and cancel in-flight review |
| SessionEnd | Session is actually closed | Cancel pending work; no review or continuation |

`Stop` is not every model step. Async output waits for a safe boundary and does not
start an idle turn. `systemMessage` is user-visible; `additionalContext` is
model-visible. `Stop` uses `decision: "block"` plus a reason to request continuation.
`stop_hook_active` identifies an already-continued turn. Tool hooks are not an
exhaustive execution-policy boundary. Transcript format is explicitly unstable.

## Source audit

- [`hooks/src/lib.rs`](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/lib.rs)
  lists events, including Stop, Interrupt and compaction.
- [`events/post_tool_use.rs`](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/events/post_tool_use.rs)
  constructs input with tool arguments/results, extracts additional context, and
  distinguishes informational output from control effects.
- [`events/stop.rs`](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/events/stop.rs)
  includes the continuation flag and aggregates continuation decisions separately
  from stopping. A separate hook can take precedence; Advisor cannot promise that
  its requested continuation actually executes.
- [`events/user_prompt_submit.rs`](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/events/user_prompt_submit.rs)
  includes turn and subagent metadata. Advisor ignores subagent events to avoid
  combining them with parent state.
- [`engine/dispatcher.rs`](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/engine/dispatcher.rs)
  dispatches configured handlers. Advisor's JSON uses the event-specific envelope.

## Our invariants

1. **No recursive reviews.** Child Codex runs disable hooks, plugins and other
   optional capabilities. A process environment marker is an additional guard.
   The child uses a temporary working directory, read-only sandbox, no approvals,
   no project instructions, and a structured response schema. Credentials remain
   managed by Codex; this plugin neither reads nor copies them.
2. **No database lock during model work.** Short SQLite transactions reserve a
   session job. Completion checks the job identity and generation again. Pause,
   interruption, a new request, compaction and termination invalidate ownership.
3. **No unlimited catch-up.** A review can re-snapshot once if tools advanced.
   If that snapshot becomes obsolete too, discard its notes and record the skip.
4. **No unbounded continuation.** Both `stop_hook_active` and persisted request-level
   correction count gate Stop. Exact echo of the generated continuation reason
   does not reset the budget. A new user-authored request does.
5. **No review after completion.** Late tool hooks for a completed turn are ignored.
   The final check supersedes background work. A reused turn's continuation is
   guarded even if no new UserPromptSubmit hook arrives.
6. **No false success.** Failed, cancelled or malformed reviews are never clean
   verdicts. Status shows coverage and returned notes; it does not claim delivery
   acknowledgement or verified correction. A crashed worker's lease eventually
   expires so the next eligible event can recover.
7. **No terminal escapes from model output.** Notes are sanitized and emitted as
   JSON for the host to render. The plugin does not write progress logs into the
   hook protocol stream or manipulate the live TUI directly.

## Deliberate limits

The first version is a snapshot reviewer, not a persistent second model transcript.
It does not see hidden reasoning, approve actions or investigate with tools. It
does not register custom native panels or slash commands. CLI status is local
inspection; lifecycle feedback is emitted through native hook notices.

There is a small unavoidable boundary after a hook returns its output: Codex owns
delivery from that point, so plugin cancellation cannot retract already-returned
context. Hooks queued before compaction may start afterward; the generation fence
protects workers already running, while newly started workers use fresh bounded
context. No undocumented transcript write or idle wakeup is used.

Tests cover state transitions, overlapping hooks, stale results, errors, transcript
fallbacks, severity escalation, terminal visibility, and actual subprocess timeout
and cancellation. `tests/live_smoke.py` additionally tests dispatch in the installed
Codex host rather than assuming that direct calls to the hook entrypoint are enough.
