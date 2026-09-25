import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/codex-advisor"))
from advisor.reviewer import Cancelled, ReviewError, review


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.binary = Path(self.temp.name) / "fake-codex"

    def install(self, body):
        self.binary.write_text("#!" + sys.executable + "\n" + body)
        self.binary.chmod(0o755)
        env = patch.dict(os.environ, {"CODEX_ADVISOR_CODEX":str(self.binary)})
        env.start()
        self.addCleanup(env.stop)

    def test_real_subprocess_contract_is_isolated_and_extracts_usage(self):
        self.install('''import json, os, pathlib, sys
args=sys.argv
assert '--ignore-user-config' in args and '--ephemeral' in args
assert args[args.index('--sandbox')+1]=='read-only'
assert args[args.index('--disable')+1]=='hooks'
assert os.environ['CODEX_ADVISOR_CHILD']=='1'
assert 'user_request' in sys.stdin.read()
pathlib.Path(args[args.index('--output-last-message')+1]).write_text('{"findings": []}')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':50,'output_tokens':7}}))
''')
        findings, usage = review({"user_request":"Test"}, "test-model", 3, lambda:False)
        self.assertEqual(findings, [])
        self.assertEqual(usage["input_tokens"], 50)

    def test_timeout_terminates_child(self):
        pidfile = Path(self.temp.name) / "pid"
        self.install("import os,time,pathlib\npathlib.Path(%r).write_text(str(os.getpid()))\ntime.sleep(60)\n" % str(pidfile))
        with self.assertRaisesRegex(ReviewError, "timed out"):
            review({}, "", 0.3, lambda:False)
        pid = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_cancellation_does_not_wait_for_model_timeout(self):
        self.install("import time\ntime.sleep(60)\n")
        start = time.monotonic()
        with self.assertRaises(Cancelled):
            review({}, "", 50, lambda:True)
        self.assertLess(time.monotonic() - start, 3)

    def test_provider_error_is_not_exposed_verbatim(self):
        self.install("import sys\nprint('SECRET-CREDENTIAL',file=sys.stderr)\nsys.exit(9)\n")
        with self.assertRaises(ReviewError) as error:
            review({}, "", 3, lambda:False)
        self.assertIn("code 9", str(error.exception))
        self.assertNotIn("SECRET", str(error.exception))

    def test_invalid_model_output_is_never_a_clean_verdict(self):
        self.install("import pathlib,sys\npathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('not json')\n")
        with self.assertRaisesRegex(ReviewError, "valid review JSON"):
            review({}, "", 3, lambda:False)


if __name__ == "__main__":
    unittest.main()
