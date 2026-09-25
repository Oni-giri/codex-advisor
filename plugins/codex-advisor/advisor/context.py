"""Bounded, best-effort context. Unknown transcript records are never instructions."""
import json
from pathlib import Path
import re
import subprocess
import tempfile


def scrub(text):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(text))
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{12,}", r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})", "[REDACTED]", text)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def bounded(value, limit):
    text = scrub(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    if len(text) <= limit:
        return text
    half = (limit - 40) // 2
    return text[:half] + "\n[... bounded excerpt ...]\n" + text[-half:]


def transcript_excerpt(path, limit=18000):
    if not path:
        return "", "No transcript supplied; reviewing captured hook activity."
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 160000))
            if size > 160000:
                stream.readline()
            raw = stream.read(160000).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return "", "Transcript unavailable; reviewing captured hook activity."
    messages = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
            payload = item.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if item.get("type") == "response_item" and payload.get("type") in (
                    "message", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"):
                # Exclude system/developer context, reasoning and our own feedback.
                if payload.get("role") in ("system", "developer"):
                    continue
                text = bounded(payload, 4500)
                if "[Codex Advisor" not in text:
                    messages.append(text)
        except (ValueError, AttributeError):
            continue
    if not messages:
        return "", "No supported transcript records; reviewing captured hook activity."
    return scrub("\n".join(messages)[-limit:]), "Bounded transcript and hook activity"


def git_snapshot(cwd):
    try:
        # Disable external diff drivers and pagers. Tracked changes only; no untracked file reads.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(["git", "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
                                    cwd=cwd, stdout=output, stderr=subprocess.DEVNULL, timeout=3)
            if result.returncode:
                return "Tracked diff unavailable."
            size = output.tell()
            output.seek(0)
            if size <= 14000:
                raw = output.read(14000)
            else:
                raw = output.read(6800) + b"\n[... bounded diff excerpt ...]\n"
                output.seek(-6800, 2)
                raw += output.read(6800)
            return scrub(raw.decode("utf-8", errors="replace"))
    except (OSError, subprocess.TimeoutExpired):
        return "Tracked diff unavailable."


def build_context(state, event):
    transcript, coverage = transcript_excerpt(event.get("transcript_path"))
    watchdog = ""
    try:
        with (Path(state["cwd"]) / "WATCHDOG.md").open() as stream:
            watchdog = bounded(stream.read(6000), 6000)
    except OSError:
        pass
    context = dict(user_request=state["prompt"], recent_activity=state["events"][-12:],
                   transcript=transcript, tracked_diff=git_snapshot(state["cwd"]),
                   last_assistant_message=bounded(event.get("last_assistant_message") or "", 6000),
                   review_priorities=watchdog, coverage=coverage,
                   previous_findings=[f["note"] for f in state["findings"][-12:]])
    return context, coverage
