"""The always-on Slack listeners must never leave a gap (no network; reads the workflow files).

    python3 -m unittest discover -s tests -v
"""
import re
import unittest
from pathlib import Path

WF = Path(__file__).resolve().parent.parent / '.github' / 'workflows'
LISTENERS = ['website-requests.yml', 'request-router.yml']


class ListenerWorkflowTests(unittest.TestCase):
    def check(self, name):
        text = (WF / name).read_text()
        with self.subTest(name=name):
            # started by cron (backup) and by the previous listener (hand-off)
            self.assertIn('cron: "*/15 * * * *"', text)
            self.assertRegex(text, r'workflow_dispatch:\n    inputs:\n      handoff:')
            # only one at a time; a hand-off run waits for its predecessor instead of exiting
            self.assertIn('select(.id < ${{ github.run_id }})', text)
            self.assertIn('[ "${{ inputs.handoff }}" = "true" ] || break', text)
            # the last step hands off, but not after a manual cancel or a crash (< 5 min)
            last = text.rsplit('\n      - name: ', 1)[1]
            self.assertTrue(last.startswith('Hand off to the next'), last[:40])
            self.assertIn("!cancelled() && steps.gate.outputs.go == 'true'", last)
            self.assertIn('-lt 300', last)
            self.assertIn(f'gh workflow run {name} ', last)
            self.assertRegex(text, r'permissions:\n(?:  .*\n)*  actions: write')
            # listening window stays inside the job timeout
            minutes = int(re.search(r'LISTEN_MINUTES: "(\d+)"', text).group(1))
            timeout = int(re.search(r'timeout-minutes: (\d+)', text).group(1))
            self.assertLess(minutes + 10, timeout)

    def test_listeners(self):
        for name in LISTENERS:
            self.check(name)


if __name__ == '__main__':
    unittest.main()
