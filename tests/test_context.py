import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"plugins/codex-advisor"))
from advisor.context import bounded, scrub, transcript_excerpt


class ContextTests(unittest.TestCase):
    def test_truncation_preserves_head_and_tail(self):
        text=bounded("START"+"x"*30000+"END",1000)
        self.assertTrue(text.startswith("START"))
        self.assertTrue(text.endswith("END"))
        self.assertLessEqual(len(text),1000)

    def test_terminal_escape_and_common_tokens_are_removed(self):
        text=scrub("\x1b[31mred\x1b[0m sk-abcdefghijklmnopqrstuv Bearer abcdefghijklmnopqrstuv")
        self.assertNotIn("\x1b",text)
        self.assertNotIn("abcdefghijkl",text)
        self.assertIn("[REDACTED]",text)

    def test_missing_and_unrecognized_transcripts_are_explicitly_partial(self):
        for path in (None,"/does-not-exist"):
            text,coverage=transcript_excerpt(path)
            self.assertEqual(text,"")
            self.assertIn("captured hook activity",coverage)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"unknown.jsonl"
            path.write_text(json.dumps({"new-format":"v99"}))
            text,coverage=transcript_excerpt(path)
            self.assertEqual(text,"")
            self.assertIn("No supported transcript records",coverage)


if __name__ == "__main__":
    unittest.main()
