"""Synthetic regression cases for the platform-independent detection pipeline.

Run directly with ``python tests/test_detection.py``. These cases exercise the
real NumPy/OpenCV implementation and never inject mouse or keyboard events.
"""
from pathlib import Path
import sys
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aim_detection import DetectionOptions, TargetTracker, detect_changes


def frame(height=100, width=100):
    result = np.zeros((height, width, 4), dtype=np.uint8)
    result[:, :, 3] = 255
    return result


def blob(x, y, area=36, side=6):
    half = side / 2
    return {"center": (x, y), "area": area,
            "bbox": (x - half, y - half, x + half, y + half)}


class DetectionTests(unittest.TestCase):
    def test_identical_frames_and_alpha_changes_do_not_create_targets(self):
        before = frame()
        after = before.copy()
        after[:, :, 3] = 0
        result = detect_changes(before, after, DetectionOptions())
        self.assertEqual(result.blobs, [])
        self.assertEqual(result.changed_fraction, 0)
        self.assertFalse(np.any(result.mask))

    def test_small_noise_is_rejected_but_a_real_patch_survives(self):
        before = frame()
        after = before.copy()
        after[5, 5, :3] = 255
        after[40:46, 40:46, :3] = 255
        result = detect_changes(before, after,
                                DetectionOptions(denoise_kernel=0, min_area=4))
        self.assertEqual(len(result.blobs), 1)
        self.assertEqual(result.blobs[0]["area"], 36)
        self.assertEqual(len(result.rejected), 1)
        self.assertTrue(result.rejected[0]["reason"])
        self.assertGreater(result.changed_fraction, 0)
        self.assertLess(result.changed_fraction, 0.01)

    def test_default_morphology_removes_salt_noise(self):
        before = frame()
        after = before.copy()
        after[::8, ::8, :3] = 255
        result = detect_changes(before, after, DetectionOptions())
        self.assertEqual(result.blobs, [])

    def test_hud_rectangle_and_bottom_strip_are_excluded(self):
        before = frame()
        after = before.copy()
        after[20:26, 70:76, :3] = 255
        after[85:91, 10:16, :3] = 255
        after[40:46, 40:46, :3] = 255
        result = detect_changes(
            before, after,
            DetectionOptions(ignore_bottom_fraction=0.2,
                             ignore_rects=[(0.6, 0.1, 0.9, 0.4)]))
        self.assertEqual(len(result.blobs), 1)
        self.assertTrue(np.allclose(result.blobs[0]["center"], (42.5, 42.5), atol=0.51))
        self.assertFalse(np.any(result.mask[80:]))
        self.assertFalse(np.any(result.mask[10:40, 60:90]))

    def test_large_scene_change_is_not_a_target(self):
        before = frame()
        after = before.copy()
        after[10:80, 10:80, :3] = 255
        result = detect_changes(before, after, DetectionOptions())
        self.assertEqual(result.blobs, [])
        self.assertTrue(result.rejected)
        self.assertTrue(result.rejected[0]["reason"])

    def test_thin_edge_is_rejected_as_an_implausible_target(self):
        before = frame()
        after = before.copy()
        after[20:22, 20:60, :3] = 255
        result = detect_changes(before, after,
                                DetectionOptions(denoise_kernel=0))
        self.assertEqual(result.blobs, [])
        self.assertTrue(result.rejected)
        self.assertTrue(result.rejected[0]["reason"])

    def test_center_uses_changed_pixels_instead_of_empty_bbox_middle(self):
        before = frame()
        after = before.copy()
        after[10, 10:20, :3] = 255
        after[10:20, 10, :3] = 255
        result = detect_changes(before, after,
                                DetectionOptions(denoise_kernel=0))
        self.assertEqual(len(result.blobs), 1)
        ys, xs = np.nonzero(after[:, :, 0])
        self.assertTrue(np.allclose(result.blobs[0]["center"], (xs.mean(), ys.mean()),
                                    atol=0.51))

    def test_mismatched_frames_are_rejected_explicitly(self):
        with self.assertRaises(ValueError):
            detect_changes(frame(80, 100), frame(100, 100), DetectionOptions())


class CameraCompensationTests(unittest.TestCase):
    @staticmethod
    def translated_pair():
        rng = np.random.default_rng(2026)
        before = rng.integers(0, 256, (128, 160, 3), dtype=np.uint8)
        before = cv2.GaussianBlur(before, (5, 5), 0)
        matrix = np.float32([[1, 0, 4], [0, 1, -3]])
        after = cv2.warpAffine(before, matrix, (160, 128),
                               flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT)
        return before, after

    def test_camera_translation_does_not_become_a_target(self):
        before, after = self.translated_pair()
        result = detect_changes(before, after,
                                DetectionOptions(compensate_camera=True))
        self.assertTrue(result.camera_reliable)
        self.assertTrue(np.allclose(result.camera_shift, (4, -3), atol=0.5))
        self.assertEqual(result.blobs, [])

    def test_local_change_survives_camera_compensation(self):
        before, after = self.translated_pair()
        after[55:65, 65:75] = 255
        result = detect_changes(before, after,
                                DetectionOptions(compensate_camera=True))
        self.assertTrue(result.camera_reliable)
        self.assertTrue(any(abs(b["center"][0] - 69.5) < 4 and
                            abs(b["center"][1] - 59.5) < 4
                            for b in result.blobs))

    def test_unreliable_flat_scene_does_not_produce_an_actionable_target(self):
        before = frame()
        after = before.copy()
        after[40:50, 40:50, :3] = 255
        result = detect_changes(before, after,
                                DetectionOptions(compensate_camera=True))
        self.assertFalse(result.camera_reliable)
        self.assertEqual(result.reason, "camera_unreliable")
        self.assertEqual(result.blobs, [])


class TargetTrackerTests(unittest.TestCase):
    def test_near_crosshair_beats_large_distant_distraction(self):
        tracker = TargetTracker(confirm_frames=1)
        near = blob(108, 102, 36)
        far = blob(250, 180, 1600, 40)
        primary = tracker.choose([far, near], (0, 0), (100, 100), now=1.0)
        self.assertEqual(primary["center"], near["center"])

    def test_single_observation_cannot_trigger_and_second_confirms(self):
        tracker = TargetTracker(confirm_frames=2)
        candidate = blob(50, 50)
        self.assertIsNone(tracker.choose([candidate], (0, 0), (50, 50), now=1.0))
        self.assertFalse(tracker.locked)
        primary = tracker.choose([candidate], (0, 0), (50, 50), now=1.02)
        self.assertEqual(primary["center"], candidate["center"])
        self.assertTrue(tracker.locked)

    def test_missing_observation_breaks_confirmation(self):
        tracker = TargetTracker(confirm_frames=2)
        candidate = blob(50, 50)
        tracker.choose([candidate], (0, 0), (50, 50), now=1.0)
        self.assertIsNone(tracker.choose([], (0, 0), (50, 50), now=1.02))
        self.assertIsNone(tracker.choose([candidate], (0, 0), (50, 50), now=1.04))
        self.assertIsNotNone(tracker.choose([candidate], (0, 0), (50, 50), now=1.06))

    def test_retained_first_candidate_is_still_reported_locked(self):
        tracker = TargetTracker(confirm_frames=1)
        candidate = blob(50, 50)
        tracker.choose([candidate], (0, 0), (50, 50), now=1.0)
        primary = tracker.choose([candidate], (0, 0), (50, 50), now=1.02)
        self.assertIsNotNone(primary)
        self.assertTrue(tracker.locked)

    def test_stale_observation_and_reset_require_confirmation_again(self):
        tracker = TargetTracker(confirm_frames=2, timeout=0.1)
        candidate = blob(50, 50)
        tracker.choose([candidate], (0, 0), (50, 50), now=1.0)
        self.assertIsNone(tracker.choose([candidate], (0, 0), (50, 50), now=1.2))
        self.assertIsNotNone(tracker.choose([candidate], (0, 0), (50, 50), now=1.22))
        tracker.reset()
        self.assertFalse(tracker.locked)
        self.assertIsNone(tracker.choose([candidate], (0, 0), (50, 50), now=1.24))

    def test_absolute_origin_is_used_for_crosshair_preference(self):
        tracker = TargetTracker(confirm_frames=1)
        near = blob(12, 12)
        far = blob(70, 70, 400, 20)
        primary = tracker.choose([far, near], (500, 200), (512, 212), now=1.0)
        self.assertEqual(primary["center"], near["center"])

    def test_lock_does_not_switch_for_small_score_fluctuations(self):
        tracker = TargetTracker(confirm_frames=1, switch_margin=0.2)
        first = blob(40, 50, 36)
        second = blob(60, 50, 36)
        tracker.choose([first, second], (0, 0), (49, 50), now=1.0)
        primary = tracker.choose([first, second], (0, 0), (51, 50), now=1.02)
        self.assertEqual(primary["center"], first["center"])

    def test_better_challenger_must_persist_before_switching(self):
        tracker = TargetTracker(confirm_frames=2, switch_margin=0.2)
        first = blob(30, 50)
        challenger = blob(100, 50)
        tracker.choose([first], (0, 0), (30, 50), now=1.0)
        tracker.choose([first], (0, 0), (30, 50), now=1.02)
        primary = tracker.choose([first, challenger], (0, 0), (100, 50), now=1.04)
        self.assertEqual(primary["center"], first["center"])
        primary = tracker.choose([first, challenger], (0, 0), (100, 50), now=1.06)
        self.assertEqual(primary["center"], challenger["center"])

    def test_lost_confirmed_target_never_returns_stale_coordinates(self):
        tracker = TargetTracker(confirm_frames=2)
        candidate = blob(50, 50)
        tracker.choose([candidate], (0, 0), (50, 50), now=1.0)
        self.assertIsNotNone(tracker.choose([candidate], (0, 0), (50, 50), now=1.02))
        self.assertIsNone(tracker.choose([], (0, 0), (50, 50), now=1.04))
        self.assertFalse(tracker.locked)
        self.assertIsNone(tracker.choose([candidate], (0, 0), (50, 50), now=1.06))

    def test_selection_does_not_mutate_callers_detection_list(self):
        tracker = TargetTracker(confirm_frames=1)
        far, near = blob(90, 90), blob(50, 50)
        candidates = [far.copy(), near.copy()]
        tracker.choose(candidates, (0, 0), (50, 50), now=1.0)
        self.assertEqual(candidates, [far, near])


if __name__ == "__main__":
    unittest.main()
