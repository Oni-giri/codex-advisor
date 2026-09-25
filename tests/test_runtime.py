import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/codex-advisor"))
from advisor.context import transcript_excerpt
from advisor.reviewer import Cancelled, ReviewError, validate_result
from advisor.runtime import handle
from advisor.storage import Store

ROOT = Path(__file__).resolve().parents[1] / "plugins/codex-advisor"
BLOCKER = {"severity": "blocker", "note": "The handler drops pending writes.", "evidence": "save() returns before await write()."}
CONCERN = dict(BLOCKER, severity="concern")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.store.configure({"interval": 0})
        self.event = dict(session_id="test-session", turn_id="turn-1", cwd=self.temp.name,
                          model="test-model", permission_mode="default")
        self.send("UserPromptSubmit", prompt="Fix the save operation")

    def send(self, name, reviewer=None, **values):
        event = dict(self.event, hook_event_name=name, **values)
        reviewer = reviewer or (lambda *args: ([], {}))
        with patch("advisor.runtime.build_context", return_value=({}, "test snapshot")):
            return handle(event, self.store, reviewer)

    def test_background_advice_is_context_not_continuation(self):
        output = self.send("PostToolUse", lambda *args: ([BLOCKER], {}), tool_use_id="1")
        self.assertNotIn("decision", output)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertEqual(self.store.get("test-session")["findings"][0]["delivery"], "returned to Codex")

    def test_final_blocker_gets_exactly_one_correction(self):
        output = self.send("Stop", lambda *args: ([BLOCKER], {}))
        self.assertEqual(output["decision"], "block")
        self.send("UserPromptSubmit", prompt=output["reason"])
        self.assertEqual(self.send("Stop", lambda *args: ([BLOCKER], {})), {})
        self.assertEqual(self.store.get("test-session")["corrections"], 1)

    def test_host_stop_hook_active_prevents_loop_even_without_state(self):
        output = self.send("Stop", lambda *args: self.fail("Must not call reviewer"), stop_hook_active=True)
        self.assertEqual(output, {})

    def test_genuine_user_prompt_resets_correction_budget(self):
        self.send("Stop", lambda *args: ([BLOCKER], {}))
        self.send("UserPromptSubmit", prompt="Also check retries", turn_id="turn-2")
        self.assertEqual(self.store.get("test-session")["corrections"], 0)

    def test_plan_mode_never_requests_correction(self):
        output = self.send("Stop", lambda *args: ([BLOCKER], {}), permission_mode="plan")
        self.assertNotIn("decision", output)
        self.assertEqual(self.store.get("test-session")["findings"][0]["delivery"], "shown in CLI")

    def test_non_blocker_at_stop_is_visible_but_does_not_continue(self):
        output = self.send("Stop", lambda *args: ([CONCERN], {}))
        self.assertIn("systemMessage", output)
        self.assertNotIn("decision", output)

    def test_duplicate_notes_do_not_repeat_background_feedback(self):
        self.send("PostToolUse", lambda *args: ([CONCERN], {}), tool_use_id="1")
        output = self.send("PostToolUse", lambda *args: ([CONCERN], {}), tool_use_id="2")
        self.assertEqual(output, {})
        self.assertEqual(len(self.store.get("test-session")["findings"]), 1)

    def test_final_review_can_confirm_previously_returned_blocker(self):
        self.send("PostToolUse", lambda *args: ([BLOCKER], {}), tool_use_id="1")
        output = self.send("Stop", lambda *args: ([BLOCKER], {}))
        self.assertEqual(output["decision"], "block")

    def test_severity_escalation_is_not_suppressed(self):
        self.send("PostToolUse", lambda *args: ([CONCERN], {}), tool_use_id="1")
        output = self.send("PostToolUse", lambda *args: ([BLOCKER], {}), tool_use_id="2")
        self.assertIn("BLOCKER", output["hookSpecificOutput"]["additionalContext"])

    def test_interrupt_cancels_and_never_restarts(self):
        def reviewer(context, model, timeout, cancelled):
            self.send("Interrupt")
            self.assertTrue(cancelled())
            return [BLOCKER], {}
        self.assertEqual(self.send("PostToolUse", reviewer), {})
        self.assertEqual(self.send("Stop", lambda *args: self.fail("Interrupted")), {})
        self.assertEqual(self.store.get("test-session")["status"], "interrupted")

    def test_compaction_and_end_invalidate_inflight_output(self):
        for event in ("PreCompact", "SessionEnd"):
            self.send("UserPromptSubmit", prompt="Continue")
            def reviewer(*args):
                self.send(event)
                return [BLOCKER], {}
            self.assertEqual(self.send("PostToolUse", reviewer), {})

    def test_new_user_turn_discards_old_results(self):
        def reviewer(*args):
            self.send("UserPromptSubmit", prompt="New task", turn_id="turn-2")
            return [BLOCKER], {}
        self.assertEqual(self.send("PostToolUse", reviewer), {})
        self.assertEqual(self.send("PostToolUse", lambda *args: self.fail("Late old event")), {})

    def test_late_tool_hook_after_stop_does_not_start_review(self):
        self.send("Stop")
        self.assertEqual(self.send("PostToolUse", lambda *args: self.fail("Already completed")), {})

    def test_overlapping_hooks_coalesce_into_one_followup_review(self):
        started, release = threading.Event(), threading.Event()
        outputs = []
        def reviewer(*args):
            started.set()
            self.assertTrue(release.wait(3))
            return [BLOCKER], {"input_tokens": 10}
        worker = threading.Thread(target=lambda: outputs.append(self.send("PostToolUse", reviewer, tool_use_id="1")))
        worker.start()
        self.assertTrue(started.wait(3))
        self.assertEqual(self.send("PostToolUse", lambda *args: self.fail("Only one reviewer"), tool_use_id="2"), {})
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIn("hookSpecificOutput", outputs[0])
        state = self.store.get("test-session")
        self.assertEqual(len(state["findings"]), 1)
        self.assertEqual(state["input_tokens"], 20)
        self.assertEqual(state["reviews"], 2)

    def test_continuously_changing_activity_is_bounded_and_stale_output_dropped(self):
        count = [0]
        def reviewer(*args):
            count[0] += 1
            self.send("PostToolUse", lambda *args: self.fail("Worker already active"), tool_use_id=str(count[0]))
            return [BLOCKER], {}
        self.assertEqual(self.send("PostToolUse", reviewer, tool_use_id="initial"), {})
        self.assertEqual(count[0], 2)
        self.assertEqual(self.store.get("test-session")["findings"], [])

    def test_stop_supersedes_background_review(self):
        def reviewer(*args):
            self.send("Stop", lambda *args: ([], {}))
            return [BLOCKER], {}
        self.assertEqual(self.send("PostToolUse", reviewer), {})
        self.assertEqual(self.store.get("test-session")["status"], "clear")

    def test_pause_invalidates_and_resume_waits_for_future_hook(self):
        def reviewer(*args):
            self.store.pause("test-session", True)
            return [BLOCKER], {}
        self.assertEqual(self.send("PostToolUse", reviewer), {})
        self.assertEqual(self.store.get("test-session")["status"], "paused")
        self.store.pause("test-session", False)
        self.assertEqual(self.store.get("test-session")["status"], "watching")

    def test_reviewer_error_fails_open_and_visible(self):
        def broken(*args):
            raise ReviewError("Review timed out")
        output = self.send("Stop", broken)
        self.assertIn("timed out", output["systemMessage"])
        self.assertNotIn("decision", output)
        self.assertEqual(self.store.get("test-session")["status"], "error")

    def test_subagent_does_not_modify_parent(self):
        before = self.store.get("test-session")
        self.send("PostToolUse", lambda *args: self.fail("Subagent"), agent_id="child")
        self.assertEqual(self.store.get("test-session"), before)

    def test_child_process_recursion_guard(self):
        with patch.dict(os.environ, {"CODEX_ADVISOR_CHILD": "1"}):
            self.assertEqual(self.send("Stop", lambda *args: self.fail("Recursive")), {})

    def test_hook_entrypoint_stdout_is_json_and_invalid_input_fails_open(self):
        for payload in ("{bad", json.dumps(dict(self.event, hook_event_name="SessionStart"))):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/advisor.py"), "--data-dir", self.temp.name, "hook"],
                                    input=payload, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0)
            self.assertIsInstance(json.loads(result.stdout), dict)
            self.assertEqual(result.stderr, "")

    def test_schema_rejects_malformed_findings(self):
        for result in ({}, {"findings": "ok"}, {"findings": [dict(BLOCKER, severity="urgent")]},
                       {"findings": [dict(BLOCKER, evidence="")]}, {"findings": [BLOCKER]*4}):
            with self.assertRaises(ReviewError):
                validate_result(result)

    def test_transcript_parser_is_bounded_and_skips_reasoning(self):
        path = Path(self.temp.name) / "transcript.jsonl"
        path.write_text('\n'.join(json.dumps(row) for row in [
            {"type":"response_item", "payload":{"type":"reasoning", "text":"SECRET REASONING"}},
            {"type":"response_item", "payload":{"type":"message", "role":"developer", "content":"PRIVATE"}},
            {"type":"response_item", "payload":{"type":"message", "role":"assistant", "content":"Done"}}])+'\n{partial')
        text, coverage = transcript_excerpt(path)
        self.assertIn("Done", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("unavailable", coverage)


if __name__ == "__main__":
    unittest.main()
