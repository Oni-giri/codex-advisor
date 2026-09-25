import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"plugins/codex-advisor"))
from advisor.runtime import display_review, handle
from advisor.storage import Store


class CliTests(unittest.TestCase):
    def test_notes_are_visible_in_native_hook_notice_and_model_context(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(temp)
            event=dict(hook_event_name="PostToolUse",session_id="cli",cwd=temp,tool_name="Bash")
            finding=dict(severity="concern",note="Handle the timeout.",evidence="The retry branch has no catch.")
            output=handle(event,store,lambda *args:([finding],{}))
            self.assertIn("CONCERN: Handle the timeout.",output["systemMessage"])
            self.assertIn("Evidence: The retry branch has no catch.",output["systemMessage"])
            self.assertIn("Handle the timeout",output["hookSpecificOutput"]["additionalContext"])

    def test_status_never_prints_raw_prompts_or_tool_results(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(temp)
            with store.transaction("cli") as state:
                state["prompt"]="PRIVATE_PROMPT"
                state["events"]=[{"result":"PRIVATE_TOOL_RESULT"}]
            result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/"advisor.py"),
                                   "--data-dir",temp,"status","--json"],capture_output=True,text=True)
            self.assertEqual(result.returncode,0)
            self.assertNotIn("PRIVATE",result.stdout)
            self.assertEqual(json.loads(result.stdout)["sessions"][0]["id"],"cli")

    def test_high_severity_notes_appear_first(self):
        notice=display_review([dict(severity="nit",note="MINOR",evidence="a"),
                               dict(severity="blocker",note="MAJOR",evidence="b")])
        self.assertLess(notice.index("MAJOR"),notice.index("MINOR"))

    def test_web_interface_is_not_exposed(self):
        result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/"advisor.py"),"--help"],capture_output=True,text=True)
        self.assertNotIn("dashboard",result.stdout)


if __name__ == "__main__":
    unittest.main()
