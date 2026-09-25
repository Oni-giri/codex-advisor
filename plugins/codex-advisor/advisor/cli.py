import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

from . import __version__
from .context import scrub
from .reviewer import executable
from .runtime import handle
from .storage import Store


def doctor():
    binary = executable()
    checks = []
    checks.append((bool(binary), "Codex CLI", binary or "Not found; set CODEX_ADVISOR_CODEX"))
    if binary:
        try:
            help_result = subprocess.run([binary, "exec", "--help"], capture_output=True, text=True, timeout=10)
            supported = all(flag in help_result.stdout for flag in ("--ignore-user-config", "--ephemeral", "--output-schema"))
            checks.append((supported, "Review isolation", "Supported" if supported else "Update Codex: required exec flags are missing"))
            features = subprocess.run([binary, "features", "list"], capture_output=True, text=True, timeout=10)
            checks.append((any(line.split()[0] == "hooks" for line in features.stdout.splitlines() if line.split()),
                           "Hook runtime", "Check /hooks in Codex to verify trust and enabled handlers"))
            auth = subprocess.run([binary, "login", "status"], capture_output=True, text=True, timeout=10)
            checks.append((auth.returncode == 0, "Codex login", "Available" if auth.returncode == 0 else "Run codex login"))
        except (OSError, subprocess.TimeoutExpired):
            checks.append((False, "CLI check", "Codex could not complete its local diagnostics"))
    for passed, name, detail in checks:
        print("%s  %-18s %s" % ("OK  " if passed else "FAIL", name, detail))
    print("\nHook trust and async support must also be verified in the host running your task.")
    return 0 if all(c[0] for c in checks) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Codex Advisor · independent review, visible state")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data-dir", help="State directory (also CODEX_ADVISOR_DATA)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="Codex hook protocol: JSON stdin / JSON stdout")
    status = sub.add_parser("status", help="Show recent sessions and review delivery")
    status.add_argument("--json", action="store_true")
    status.add_argument("--session")
    for name in ("pause", "resume"):
        control = sub.add_parser(name, help=name.capitalize() + " a session or all advisor reviews")
        control.add_argument("session", help="Session id or 'all'")
    config = sub.add_parser("configure", help="Choose reviewer model and minimum review interval")
    config.add_argument("--model", help="Model id; 'inherit' uses the primary model")
    config.add_argument("--interval", type=int, help="Minimum seconds between background reviews (0–600)")
    sub.add_parser("doctor", help="Check Codex executable and login without inference")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return doctor()
    if args.command == "hook" and os.environ.get("CODEX_ADVISOR_CHILD") == "1":
        print("{}")
        return 0
    try:
        store = Store(args.data_dir)
        if args.command == "hook":
            def stop_worker(signum, frame):
                raise SystemExit(0)  # Unwind reviewer finally blocks and reap its process group.
            signal.signal(signal.SIGTERM, stop_worker)
            payload = sys.stdin.read(1_000_001)
            if len(payload) > 1_000_000:
                raise ValueError("Hook input exceeded 1 MB")
            print(json.dumps(handle(json.loads(payload), store)))
        elif args.command == "status":
            data = {"sessions": store.all(), "settings": store.settings()}
            for session in data["sessions"]:
                for private in ("prompt", "events", "continuation"):
                    session.pop(private, None)
                if session["job"] and session["job"]["expires"] < time.time():
                    session.update(status="stalled", error="Review worker did not finish; a future eligible hook can retry.")
            if args.session:
                data["sessions"] = [s for s in data["sessions"] if s["id"] == args.session]
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                print("CODEX ADVISOR  ·  " + ("Paused globally" if data["settings"].get("paused") else "Enabled"))
                if not data["sessions"]:
                    print("No sessions yet. Install and trust the hooks, then start a Codex task.")
                for state in data["sessions"][:10]:
                    print("\n%s  %s\n  %s" % (state["status"].upper(), scrub(state["id"]), scrub(state["cwd"])))
                    print("  %s reviews · %s correction passes" % (state["reviews"], state["corrections"]))
                    print("  Model: %s · Reported tokens: %s" % (scrub(data["settings"].get("model") or state["model"] or "inherit"),
                          state["input_tokens"] + state["output_tokens"]))
                    if state.get("coverage"):
                        print("  Coverage: " + scrub(state["coverage"]))
                    if state["error"]:
                        print("  " + scrub(state["error"]))
                    for finding in state["findings"][-3:]:
                        print("  %s · %s\n    %s\n    Evidence: %s" % (finding["severity"].upper(), finding["delivery"], scrub(finding["note"]), scrub(finding["evidence"])))
                print("\nReturned feedback is not a receipt that Codex acted on it.")
        elif args.command in ("pause", "resume"):
            paused = args.command == "pause"
            if args.session == "all":
                store.configure({"paused": paused})
                # Fence workers before re-enabling; never let pre-pause output leak through.
                for state in store.all():
                    store.pause(state["id"], paused)
            else:
                store.pause(args.session, paused)
            print("%s: %s" % ("Paused" if paused else "Resumed", args.session))
        elif args.command == "configure":
            values = {}
            if args.model is not None:
                if not args.model.strip() or len(args.model) > 200:
                    raise ValueError("Provide a model id or 'inherit'")
                values["model"] = "" if args.model == "inherit" else args.model
            if args.interval is not None:
                if not 0 <= args.interval <= 600:
                    raise ValueError("Interval must be between 0 and 600 seconds")
                values["interval"] = args.interval
            print(json.dumps(store.configure(values), indent=2))
        return 0
    except Exception as exc:
        if args.command == "hook":
            # Fail open and visibly. Never print logs to the hook protocol stream.
            print(json.dumps({"systemMessage": "Codex Advisor unavailable (%s); no review verdict. Codex can continue." % type(exc).__name__}))
            return 0
        print("Advisor: " + scrub(str(exc)), file=sys.stderr)
        return 1
