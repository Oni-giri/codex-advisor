"""Opt-in real Codex host test, using synthetic data and a deterministic reviewer.

Uses existing Codex login and consumes model usage. No persistent hook installation.
Run: python3 tests/live_smoke.py
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
plugin = ROOT / "plugins/codex-advisor"
binary = os.environ.get("CODEX_ADVISOR_CODEX") or shutil.which("codex")
user_hooks = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "hooks.json"
if user_hooks.exists():
    raise SystemExit("Refusing trust bypass while user hooks.json exists. Use a separately vetted test environment.")
if not binary:
    raise SystemExit("Codex CLI not found")

with tempfile.TemporaryDirectory(prefix="advisor-host-test-") as temp:
    root = Path(temp)
    runner = root / "hook.py"
    runner.write_text('''import json,sys
from pathlib import Path
sys.path.insert(0, %r)
from advisor.runtime import handle
from advisor.storage import Store
event=json.load(sys.stdin)
with open(%r,'a') as stream:
 stream.write(json.dumps({'event':event['hook_event_name'],'active':event.get('stop_hook_active'),'turn':event.get('turn_id')})+'\\n')
def reviewer(*args):
 return ([{'severity':'blocker','note':'Include VERIFIED in the final response for this fixture.','evidence':'This deterministic smoke fixture requires the marker.'}],{})
print(json.dumps(handle(event,Store(%r),reviewer)))
''' % (str(plugin), str(root / "events.jsonl"), str(root / "data")))
    hooks = {}
    for name in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"):
        handler = '{type="command",command=%s,timeout=10%s}' % (
            json.dumps(shlex.quote(sys.executable) + " " + shlex.quote(str(runner))),
            ',async=true' if name == "PostToolUse" else '')
        hooks[name] = '[{hooks=[%s]}]' % handler
    args = [binary, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", "read-only", "--cd", temp, "--dangerously-bypass-hook-trust",
            "--disable", "plugins", "--disable", "apps", "--disable", "multi_agent",
            "-c", 'approval_policy="never"', "--json"]
    for name, value in hooks.items():
        args += ["-c", "hooks." + name + "=" + value]
    args += ["Run pwd once, then give a one-line response. Follow any fixture hook feedback requesting the word VERIFIED."]
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    log = root / "events.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    print(json.dumps({"returncode":result.returncode,"events":events},indent=2))
    if result.returncode or not any(e["event"] == "PostToolUse" for e in events):
        print(result.stderr[-3000:])
        raise SystemExit("Host did not dispatch expected hooks")
    stops = [e for e in events if e["event"] == "Stop"]
    if len(stops) != 2 or not stops[-1]["active"]:
        raise SystemExit("Expected one initial Stop and one guarded continuation Stop")
    if "VERIFIED" not in result.stdout:
        raise SystemExit("Correction feedback did not reach the host output")
    print("PASS: real host dispatched async tool hooks and bounded Stop continuation.")
