"""Lossless diagnostic recordings and input-free offline replay."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aim_recording import SessionRecorder, load_session, replay_session


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prev = np.zeros((40, 60, 4), np.uint8)
        self.current = self.prev.copy()
        self.current[10:20, 20:30, :3] = 255
        self.mask = np.zeros((40, 60), np.uint8)
        self.mask[10:20, 20:30] = 1
        self.region = (-100, 30, 60, 40)
        self.metadata = {"prev_ts": 1.0, "current_ts": 1.02, "reason": "confirmed",
                         "options": {"threshold": 30, "min_area": 4, "denoise_kernel": 3}}

    def remember(self, recorder, **changes):
        values = dict(prev=self.prev, current=self.current, region=self.region,
                      metadata=self.metadata, mask=self.mask)
        values.update(changes)
        recorder.remember(**values)

    def test_lossless_snapshot_including_metadata_and_timestamps(self):
        recorder = SessionRecorder()
        self.remember(recorder)
        original = self.current.copy()
        self.current[:] = 7
        self.metadata["reason"] = "mutated"
        self.mask[:] = 5
        folder = recorder.dump(self.root)
        frames = list(load_session(folder))
        self.assertEqual(len(frames), 1)
        prev, current, region, metadata, mask = frames[0]
        self.assertTrue(np.array_equal(prev, self.prev))
        self.assertTrue(np.array_equal(current, original))
        self.assertTrue(np.all(mask[10:20, 20:30] == 1))
        self.assertEqual(region, self.region)
        self.assertEqual(metadata["reason"], "confirmed")
        self.assertEqual((metadata["prev_ts"], metadata["current_ts"]), (1., 1.02))

    def test_ring_keeps_recent_pairs_and_resize_trims(self):
        recorder = SessionRecorder(max_frames=2)
        for number in range(4):
            self.remember(recorder, metadata={"sequence": number}, mask=None)
        self.assertEqual(len(recorder), 2)
        frames = list(load_session(recorder.dump(self.root)))
        self.assertEqual([frame[3]["sequence"] for frame in frames], [2, 3])
        self.assertIsNone(frames[0][4])
        recorder.max_frames = 1
        self.assertEqual(len(recorder), 1)

    def test_repeated_dumps_never_overwrite_and_clear_is_memory_only(self):
        recorder = SessionRecorder()
        self.remember(recorder)
        first = recorder.dump(self.root)
        original = (first / "0000.npz").read_bytes()
        second = recorder.dump(self.root)
        self.assertNotEqual(first, second)
        self.assertEqual((first / "0000.npz").read_bytes(), original)
        recorder.clear()
        self.assertEqual(len(recorder), 0)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        with self.assertRaises(ValueError):
            recorder.dump(self.root)

    def test_byte_budget_also_evicts_older_frames(self):
        recorder = SessionRecorder(max_frames=20)
        pair_size = self.prev.nbytes + self.current.nbytes
        with patch("aim_recording.MAX_SESSION_BYTES", pair_size + 10):
            self.remember(recorder, metadata={"sequence": 1}, mask=None)
            self.remember(recorder, metadata={"sequence": 2}, mask=None)
            self.assertEqual(len(recorder), 1)
        frames = list(load_session(recorder.dump(self.root)))
        self.assertEqual(frames[0][3]["sequence"], 2)

    def test_finalize_decision_does_not_change_frames_or_older_dump(self):
        recorder = SessionRecorder()
        token = recorder.remember(self.prev, self.current, self.region,
                                  {"decision_pending": True, "action_allowed": True})
        before = recorder.dump(self.root)
        self.assertTrue(recorder.update_last_metadata(
            {"decision_pending": False, "action_allowed": False,
             "skip_reason": "frame_stale"}, token=token))
        after = recorder.dump(self.root)
        earlier = next(load_session(before))
        final = next(load_session(after))
        self.assertTrue(earlier[3]["decision_pending"])
        self.assertFalse(final[3]["decision_pending"])
        self.assertFalse(final[3]["action_allowed"])
        self.assertEqual(final[3]["skip_reason"], "frame_stale")
        self.assertTrue(np.array_equal(earlier[1], final[1]))
        self.remember(recorder)
        self.assertFalse(recorder.update_last_metadata({"stale_update": True}, token=token))
        self.assertNotIn("stale_update", list(load_session(recorder.dump(self.root)))[-1][3])

    def test_rejects_invalid_frames_regions_and_metadata(self):
        recorder = SessionRecorder()
        for changes in ({"current": self.current.astype(float)},
                        {"current": self.current[:-1]},
                        {"region": [0, 0, 40, 60]},
                        {"mask": self.mask.astype(bool)},
                        {"metadata": {"latency": float("nan")}},
                        {"metadata": {"frame": self.current}}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ValueError):
                    self.remember(recorder, **changes)
        self.assertEqual(len(recorder), 0)

    def test_rejects_manifest_path_traversal_and_invalid_archive(self):
        recorder = SessionRecorder()
        self.remember(recorder)
        folder = recorder.dump(self.root)
        path = folder / "session.json"
        manifest = json.loads(path.read_text())
        manifest["frames"][0]["file"] = "../outside.npz"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "path"):
            list(load_session(folder))
        manifest["frames"][0]["file"] = "0000.npz"
        path.write_text(json.dumps(manifest))
        np.savez(folder / "0000.npz", prev=np.array([object()], dtype=object), current=self.current)
        with self.assertRaises(ValueError):
            list(load_session(folder))

    def test_rejects_symlinked_recording(self):
        recorder = SessionRecorder()
        self.remember(recorder)
        folder = recorder.dump(self.root)
        archive = folder / "0000.npz"
        outside = self.root / "outside.npz"
        archive.rename(outside)
        try:
            archive.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks are not available on this host")
        with self.assertRaisesRegex(ValueError, "escapes"):
            list(load_session(folder))

    def test_replay_and_cli_run_without_application_or_input_modules(self):
        existing_modules = set(sys.modules)
        recorder = SessionRecorder()
        self.remember(recorder)
        folder = recorder.dump(self.root)
        result = replay_session(folder)
        self.assertEqual(result["totals"]["pairs"], 1)
        self.assertEqual(result["totals"]["candidates"], 1)
        self.assertEqual(replay_session(folder, threshold=255)["totals"]["candidates"], 0)
        process = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] /
                                    "aim_recording.py"), str(folder)],
                                 text=True, capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        lines = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(lines[0]["prev_ts"], 1.)
        self.assertEqual(lines[-1]["totals"]["pairs"], 1)
        imported = set(sys.modules) - existing_modules
        self.assertNotIn("aim_tracker", imported)
        self.assertNotIn("pyautogui", imported)


if __name__ == "__main__":
    unittest.main()
