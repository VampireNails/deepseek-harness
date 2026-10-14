"""Real Linux process and descriptor fences; these tests never stop a deployed service."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))
from worker import freeze_writers, stop_writers, verify_quiescent


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / '.dsh'
        self.home.mkdir()
        self.deployment = self.root / 'deployment'
        self.deployment.mkdir()
        self.config = {'home': str(self.home), 'deploymentRoot': str(self.deployment), 'runnerEntrypoints': []}
        self.children = []
    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            if child.stdout:
                child.stdout.close()
        self.temp.cleanup()
    def test_detached_tagged_writer_is_frozen_and_terminated(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True,
                                 env={**os.environ, 'DSH_HOME': str(self.home), 'INTEL_TASK_ID': 'fixture', 'INTEL_RUN_ID': 'fixture'})
        self.children.append(child)
        owned = freeze_writers(self.config)
        self.assertIn(child.pid, owned)
        stop_writers(owned)
        self.assertIsNotNone(child.wait(timeout=5))
    def test_unowned_writable_descriptor_refuses_erasure(self):
        script = 'import sys,time;f=open(sys.argv[1],"w");print("ready",flush=True);time.sleep(60)'
        child = subprocess.Popen([sys.executable, '-c', script, str(self.home / 'open')], stdout=subprocess.PIPE, text=True)
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        with self.assertRaisesRegex(ValueError, 'HOST_RESET_UNCONFIRMED_WRITER'):
            verify_quiescent(self.config)
        self.assertTrue((self.home / 'open').exists())


if __name__ == '__main__':
    unittest.main()
