"""Exercise the real process/pipe boundary without a desktop or WinAPI."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aim_panel import PanelBridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def bridge(self, source):
        path = Path(self.directory.name, "fake_panel.py")
        path.write_text(source, encoding="utf-8")
        bridge = PanelBridge(path).start()
        self.addCleanup(bridge.close)
        return bridge

    def wait(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.01)
        self.fail("Timed out waiting for panel process")

    def test_state_and_command_unicode_roundtrip(self):
        bridge = self.bridge("""import json,sys
for line in sys.stdin:
    state=json.loads(line)
    print(json.dumps({'command':'set','name':'profile','value':state['profile']}),flush=True)
""")
        self.assertTrue(bridge.alive)
        self.assertTrue(bridge.publish({"profile": "Сцена с водой"}))
        commands = self.wait(bridge.drain)
        self.assertEqual(commands, [{"command": "set", "name": "profile",
                                     "value": "Сцена с водой"}])

    def test_malformed_commands_rejected_without_hiding_valid_command(self):
        bridge = self.bridge("""print('not json',flush=True)
print('[]',flush=True)
print('{"command": "stop"}',flush=True)
""")
        commands = self.wait(bridge.drain)
        self.assertEqual(commands, [{"command": "stop"}])
        self.assertIsNotNone(bridge.error)
        self.wait(lambda: bridge.eof)
        self.assertFalse(bridge.alive)
        self.assertFalse(bridge.publish({}))

    def test_command_queue_bounded_and_drained_in_batches(self):
        bridge = self.bridge("""import json
for n in range(300):
    print(json.dumps({'command':'set','value':n}),flush=True)
""")
        self.wait(lambda: bridge.eof)
        commands = bridge.drain(10000)
        self.assertEqual(len(commands), bridge.COMMAND_CAPACITY)
        self.assertEqual(commands[-1]['value'], 299)
        self.assertEqual(bridge.drain(), [])

    def test_nonreading_child_does_not_block_capture_or_shutdown(self):
        bridge = self.bridge("import time\ntime.sleep(30)\n")
        state = {"preview": "x" * 600000}
        started = time.monotonic()
        for _ in range(15):
            self.assertTrue(bridge.publish(state))
        self.assertLess(time.monotonic() - started, 3)
        started = time.monotonic()
        bridge.close()
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(bridge.alive)
        self.assertFalse(bridge._reader.is_alive())
        self.assertFalse(bridge._writer.is_alive())
        bridge.close()  # idempotent

    def test_bad_states_and_oversize_command_report_errors(self):
        bridge = self.bridge("import sys,time\nsys.stdin.readline()\nprint('x' * 1100000,flush=True)\ntime.sleep(30)\n")
        self.assertFalse(bridge.publish({"bad": float("nan")}))
        self.assertFalse(bridge.publish([1]))
        self.assertFalse(bridge.publish({"preview": "x" * 1100000}))
        self.assertTrue(bridge.publish({"status": "ready"}))
        self.wait(lambda: bridge.eof)
        self.assertEqual(bridge.drain(), [])
        self.assertIsNotNone(bridge.error)

    def test_launch_failure_is_reported(self):
        bridge = PanelBridge("ignored", python_executable="/missing/python").start()
        self.addCleanup(bridge.close)
        self.assertFalse(bridge.alive)
        self.assertIsNotNone(bridge.error)


if __name__ == "__main__":
    unittest.main()
