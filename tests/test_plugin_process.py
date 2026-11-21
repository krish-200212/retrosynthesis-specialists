import subprocess
import sys
import time
import unittest
from retro_specialists.plugin_process import bounded_run, WorkerLimitError

class ProcessTests(unittest.TestCase):
    def test_normal_input_output(self):
        result = bounded_run([sys.executable, '-c', 'import sys;print(sys.stdin.read());print("diagnostic",file=sys.stderr)'], b'input', 5, 1000)
        self.assertEqual(result[0], 0)
        self.assertIn(b'input', result[1])
        self.assertIn(b'diagnostic', result[2])
    def test_stdout_flood_stopped_while_process_running(self):
        started = time.monotonic()
        with self.assertRaisesRegex(WorkerLimitError, 'output_limit'):
            bounded_run([sys.executable, '-c', 'import sys,time;sys.stdout.write("x"*20000);sys.stdout.flush();time.sleep(60)'], b'', 10, 1000)
        self.assertLess(time.monotonic()-started, 5)
    def test_stderr_flood_stopped(self):
        with self.assertRaisesRegex(WorkerLimitError, 'output_limit'):
            bounded_run([sys.executable, '-c', 'import sys,time;sys.stderr.write("x"*20000);sys.stderr.flush();time.sleep(60)'], b'', 10, 1000)
    def test_timeout_is_bounded(self):
        started = time.monotonic()
        with self.assertRaisesRegex(WorkerLimitError, 'timeout'):
            bounded_run([sys.executable, '-c', 'import time;time.sleep(60)'], b'', .1, 1000)
        self.assertLess(time.monotonic()-started, 5)

if __name__ == '__main__':
    unittest.main()
