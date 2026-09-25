# Codex Advisor

A second perspective inside the **Codex CLI**. The advisor reviews recent work,
shows concrete findings in the conversation, and can request one focused correction
before Codex finishes. No web UI, server, or separate account connection.

```text
Advisor | Independent review
CONCERN: The retry path can charge the customer twice.
  Evidence: The second request does not reuse the original idempotency key.

Advisor | One correction pass requested
BLOCKER: The handler returns before the write completes.
  Evidence: save() calls write() without awaiting its promise.
```

Messages use Codex's native hook notices; their surrounding formatting depends on
your Codex version. Background findings also enter the model's context. The advisor
does not approve actions, edit your files, or override your instructions.

## Install

Requires macOS or Linux, Python **3.9+**, and a logged-in Codex CLI with async command
hooks, plugin hooks, `--ignore-user-config`, and `--output-schema`. Developed and
smoke-tested against **codex-cli 0.153.4**. No Python packages are needed at runtime.

```sh
git clone https://github.com/Oni-giri/codex-advisor.git
cd codex-advisor
python3 advisor.py doctor
codex plugin marketplace add .
codex plugin add codex-advisor@codex-advisor
```

Start a new Codex session, inspect its `/hooks` view, and review/trust the plugin's
hook definitions. Installation does not automatically trust executable hooks.
Keep `python3` and `codex` on the host's PATH. If needed, set
`CODEX_ADVISOR_CODEX` to the absolute Codex executable path **before starting Codex**.

The repository is a small plugin marketplace. The complete plugin is in
[`plugins/codex-advisor`](plugins/codex-advisor); installation copies that directory.
No global config or hook trust is changed by running `doctor` or the tests.

## What happens while you work

- **After tools:** a background review examines a bounded snapshot. Tool activity
  arriving during that review is coalesced into at most one follow-up review.
  Findings from a snapshot that is still out of date are discarded.
- **At completion:** a synchronous review checks the final response. A fresh,
  evidence-backed blocker can request **one** correction pass per user request.
  Suggestions and concerns are shown directly without restarting the agent.
- **On interruption:** pending review results are invalidated. The advisor never
  restarts a task you stopped. New user input enables reviews again.
- **On failure:** Codex continues and a notice explicitly says no verdict was
  produced. A timeout is never reported as a clean review.

The final check reports when it finds no actionable issue. Background clean reviews
stay quiet. Plan mode never triggers automatic correction. After the correction
budget is used, the final response is released without another model review; the
advisor does not claim to have verified the fix.

## Terminal controls

From this checkout:

```sh
python3 advisor.py status
python3 advisor.py status --session SESSION_ID --json
python3 advisor.py pause SESSION_ID
python3 advisor.py resume SESSION_ID
python3 advisor.py pause all
python3 advisor.py resume all
python3 advisor.py configure --model YOUR_MODEL_ID
python3 advisor.py configure --model inherit
python3 advisor.py configure --interval 30
```

The installed plugin also provides the `$advisor` skill for these controls inside
Codex. There is no custom `/advisor` slash command. `status` includes findings and
their delivery labels; “returned to Codex” is not confirmation that the agent acted
on the advice. Findings are historical, not a live list of unresolved bugs.

By default the reviewer inherits the primary model id, uses your existing Codex
login, and starts with isolated CLI settings. Custom provider/profile configuration
is **not inherited**. Configure an accessible Codex model if your primary model
comes from a custom provider. Reviews consume additional model usage. Reported
token totals cover successful reviewer responses, not aborted or failed requests.

## Context and local data

The reviewer receives the latest user request, recent tool events, a supported
excerpt of the transcript when available, a bounded `git diff HEAD` for tracked
changes, and optional `WATCHDOG.md` from the session working directory. It gets no
workspace tools. Untracked files, omitted transcript formats, and truncated context
are outside its coverage. Secret redaction is best-effort; review snapshots can
contain code and tool output and are sent through your Codex model connection.

State lives in `~/.local/share/codex-advisor/advisor.sqlite3`, independent of plugin
cache versions. Override it with `CODEX_ADVISOR_DATA` in the host environment, or
`--data-dir PATH` for manual CLI commands. Use the **same directory** for both.
The database stores bounded request/tool excerpts, findings, and recent activity;
it persists until you remove it while Codex is stopped. Temporary reviewer files
are removed on normal completion/cancellation. A machine crash may leave temporary
files behind. No browser or HTTP endpoint exposes this state.

## Timing and limitations

Background reviews have a 55-second model-work budget and a default 20-second
minimum interval after the previous review. The final model check has 25 seconds.
The host hook timeouts are 90 and 35 seconds respectively, leaving cleanup headroom.
New activity or completion can cancel earlier work. A fast-moving agent may outrun
background review; the final check remains independent.

Async feedback is delivered by Codex at its next safe boundary. It cannot interrupt
an in-flight model request, and it cannot wake an idle task. Main-session hooks are
supported; subagent events are deliberately excluded because their session IDs can
refer to the parent. See the [hook contract](docs/hook-contract.md) for details.

## Development and verification

```sh
python3 -m unittest discover -s tests -v
python3 advisor.py doctor
# Optional: consumes Codex usage; synthetic host-dispatch fixture, no installation.
python3 tests/live_smoke.py
```

The test suite uses deterministic reviewers to exercise cancellation, concurrency,
deduplication, process cleanup, CLI output and correction limits. CI runs on macOS
and Linux with Python 3.9 and 3.12. The opt-in host test refuses to bypass hook trust
if a user `hooks.json` exists; its temporary hooks are authored in the test itself.

Inspired by [oh-my-pi's advisor](https://github.com/can1357/oh-my-pi/blob/main/docs/advisor-watchdog.md).
This implementation is independent and uses Codex's supported hook interface.
