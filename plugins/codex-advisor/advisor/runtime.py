"""Hook state machine. All model work is outside transactions and generation-fenced."""
import hashlib
import json
import os
import time
import unicodedata
import uuid

from .context import bounded, build_context
from .reviewer import Cancelled, ReviewError, review
from .storage import Store, record

EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "Interrupt", "PreCompact", "SessionEnd"}
RANK = {"nit": 0, "concern": 1, "blocker": 2}


def invalidate(state, status):
    state.update(generation=state["generation"] + 1, job=None, status=status)


def fingerprint(note):
    normalized = "".join(c if c.isalnum() else " " for c in unicodedata.normalize("NFKC", note).lower())
    return hashlib.sha256(" ".join(normalized.split()).encode()).hexdigest()


def advice(findings):
    return "[Codex Advisor — independent review; weigh this advice against the user's request]\n" + "\n".join(
        "%s: %s\nEvidence: %s" % (f["severity"].upper(), f["note"], f["evidence"]) for f in findings)


def context_output(event_name, findings):
    if not findings:
        return {}
    return {"systemMessage": display_review(findings),
            "hookSpecificOutput": {"hookEventName": event_name, "additionalContext": advice(findings)}}


def display_review(findings, outcome=""):
    lines = ["Advisor | " + (outcome or "Independent review")]
    for finding in sorted(findings, key=lambda f: RANK[f["severity"]], reverse=True):
        lines.extend(["%s: %s" % (finding["severity"].upper(), finding["note"]),
                      "  Evidence: " + finding["evidence"]])
    return "\n".join(lines)


def handle(event, store=None, reviewer=review):
    if os.environ.get("CODEX_ADVISOR_CHILD") == "1":
        return {}
    if not isinstance(event, dict) or event.get("hook_event_name") not in EVENTS:
        return {}
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 200:
        return {}
    # Subagent hooks share their parent's session_id. Never mix their state with it.
    if event.get("agent_id") or event.get("agent_type"):
        return {}
    store = store or Store()
    config = store.settings()
    name = event["hook_event_name"]
    turn = event.get("turn_id", "")
    now = time.time()
    with store.transaction(session_id, event.get("cwd", "")) as state:
        if event.get("cwd"):
            state["cwd"] = event["cwd"]
        if event.get("model"):
            state["model"] = event["model"]
        if name == "SessionStart":
            invalidate(state, "paused" if state["paused"] else "watching")
            state.update(interrupted=False, ended=False, events=[], turn_id="", error=None, last_stop_turn="")
            record(state, "session", "Watching session (%s)" % event.get("source", "startup"))
            return {}
        if name == "UserPromptSubmit":
            prompt = event.get("prompt", "")
            continuation = bool(state["continuation"] and prompt == state["continuation"])
            invalidate(state, "paused" if state["paused"] else "watching")
            state.update(interrupted=False, ended=False, turn_id=turn, error=None, last_stop_turn="")
            if not continuation:
                state.update(prompt=bounded(prompt, 8000), corrections=0, continuation="", events=[], findings=[])
            record(state, "request", "Correction pass started" if continuation else "New request; review context reset")
            return {}
        if name in ("Interrupt", "SessionEnd", "PreCompact"):
            if name == "Interrupt":
                state["interrupted"] = True
            if name == "SessionEnd":
                state["ended"] = True
            if name == "PreCompact":
                state["events"] = []
            status = {"Interrupt": "interrupted", "SessionEnd": "ended", "PreCompact": "watching"}[name]
            invalidate(state, status)
            record(state, "session", {"Interrupt": "You interrupted Codex; advisor will not restart it",
                   "SessionEnd": "Session ended; pending review cancelled",
                   "PreCompact": "Context compacting; pending review invalidated"}[name])
            return {}
        if config.get("paused") or state["paused"] or state["interrupted"] or state["ended"]:
            return {}
        if turn and state["turn_id"] and turn != state["turn_id"]:
            # Late async invocation from an earlier user turn.
            return {}
        if turn:
            state["turn_id"] = turn
        if name == "PostToolUse":
            if turn and state.get("last_stop_turn") == turn:
                return {}
            call_id = event.get("tool_use_id")
            if call_id and any(e.get("id") == call_id for e in state["events"]):
                return {}
            state["revision"] += 1
            state["events"] = (state["events"] + [{
                "id": call_id, "tool": event.get("tool_name", "unknown"),
                "input": bounded(event.get("tool_input", {}), 1800),
                "result": bounded(event.get("tool_response", {}), 3500)}])[-20:]
            if state["job"] and state["job"]["expires"] > now:
                return {}
            if now - state["last_review"] < config.get("interval", 20):
                state["status"] = "watching"
                return {}
        else:  # Stop is a completion check, not a per-model-step event.
            if event.get("stop_hook_active") or state["corrections"] >= 1:
                invalidate(state, "limited")
                record(state, "limit", "Correction limit reached; final response is released")
                return {}
            if state.get("last_stop_turn") == turn and turn:
                return {}
            # Supersede background work, including late-arriving async tool hooks.
            invalidate(state, "reviewing")
            state["last_stop_turn"] = turn
        state["status"] = "reviewing"
        state["error"] = None
        job = dict(id=uuid.uuid4().hex, generation=state["generation"], revision=state["revision"],
                   expires=now + (32 if name == "Stop" else 70))
        state["job"] = job
        record(state, "review", "Checking final response" if name == "Stop" else "Reviewing recent activity")
        snapshot = json.loads(json.dumps(state))

    def cancelled():
        current = store.get(session_id)
        return (not current or not current["job"] or current["job"]["id"] != job["id"]
                or current["generation"] != job["generation"] or bool(store.settings().get("paused")))

    try:
        started = time.monotonic()
        context, coverage = build_context(snapshot, event)
        findings, usage = reviewer(context, config.get("model") or snapshot["model"],
                                   25 if name == "Stop" else 55, cancelled)
        attempts = 1
        # Coalesce intervening tool events into one bounded follow-up snapshot. Never
        # keep a worker alive indefinitely trying to catch a fast primary agent.
        current = store.get(session_id)
        remaining = 55 - (time.monotonic() - started)
        if (name == "PostToolUse" and not cancelled() and current["revision"] != job["revision"]
                and remaining >= 8):
            job["revision"] = current["revision"]
            context, coverage = build_context(current, event)
            findings, extra_usage = reviewer(context, config.get("model") or current["model"],
                                            max(1, 55 - (time.monotonic() - started)), cancelled)
            usage = {key: usage.get(key, 0) + extra_usage.get(key, 0)
                     for key in ("input_tokens", "output_tokens")}
            attempts += 1
    except Cancelled:
        return {}
    except Exception as exc:
        error = str(exc) if isinstance(exc, ReviewError) else "Review unavailable (%s). Codex can continue." % type(exc).__name__
        with store.transaction(session_id) as state:
            if state["job"] and state["job"]["id"] == job["id"]:
                state.update(job=None, status="error", error=error, last_review=time.time())
                record(state, "error", error)
                return {"systemMessage": "Codex Advisor: " + error}
        return {}

    with store.transaction(session_id) as state:
        if (not state["job"] or state["job"]["id"] != job["id"]
                or state["generation"] != job["generation"] or store.settings().get("paused")):
            return {}
        state.update(job=None, last_review=time.time(), reviews=state["reviews"] + attempts,
                     coverage=coverage, error=None)
        for key in ("input_tokens", "output_tokens"):
            count = usage.get(key, 0)
            if isinstance(count, int) and count >= 0:
                state[key] += count
        # Background findings reference an older snapshot if tool activity progressed.
        if name == "PostToolUse" and state["revision"] != job["revision"]:
            state["status"] = "watching"
            record(state, "skipped", "Activity changed during review; stale findings discarded")
            return {}
        new = []
        for finding in findings:
            key = fingerprint(finding["note"])
            prior = [f for f in state["findings"] + new if f["fingerprint"] == key]
            if prior and max(RANK[f["severity"]] for f in prior) >= RANK[finding["severity"]]:
                continue
            finding = dict(finding, fingerprint=key, at=time.time(), delivery="recorded")
            new.append(finding)
        state["findings"] = (state["findings"] + new)[-60:]
        state["status"] = "findings" if findings else "clear"
        record(state, "result", "%s finding(s); %s new" % (len(findings), len(new)) if findings else "No actionable findings in reviewed snapshot")
        if name == "Stop":
            # A fresh final review may confirm an already-delivered blocker is unresolved.
            blocker_keys = {fingerprint(f["note"]) for f in findings if f["severity"] == "blocker"}
            blockers = [f for f in state["findings"] if f["severity"] == "blocker" and f["fingerprint"] in blocker_keys]
            if blockers and event.get("permission_mode") != "plan":
                reason = advice(blockers) + "\nMake one focused correction pass. If the evidence is wrong, explain why."
                state.update(corrections=1, continuation=reason, status="correcting")
                for finding in blockers:
                    finding["delivery"] = "correction requested"
                record(state, "delivery", "One correction pass requested through Stop")
                return {"decision": "block", "reason": reason,
                        "systemMessage": display_review(new or blockers, "One correction pass requested")}
            for finding in new:
                finding["delivery"] = "shown in CLI"
            if new:
                return {"systemMessage": display_review(new,
                        "Plan mode; no automatic correction" if event.get("permission_mode") == "plan"
                        else "Review complete; no correction requested")}
            return {"systemMessage": "Advisor | Final review complete. " +
                    ("No new findings." if findings else "No actionable findings in the reviewed snapshot.")}
        for finding in new:
            finding["delivery"] = "returned to Codex"
        return context_output(name, new)
