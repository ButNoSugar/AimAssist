"""UI state and validation tests; no display or Windows stubs required."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from aim_ui import SettingsModel, parse_value


class SettingsModelTests(unittest.TestCase):
    def test_default_is_observation(self):
        self.assertIs(SettingsModel().settings["observe_only"], True)

    def test_snapshot_does_not_overwrite_unfinished_edit(self):
        model = SettingsModel()
        model.receive({"settings": {"diff_threshold": 30}})
        model.edit("diff_threshold", "45")
        model.receive({"settings": {"diff_threshold": 31}})
        self.assertEqual(model.displayed("diff_threshold"), "45")
        self.assertEqual(model.settings["diff_threshold"], 31)

    def test_pending_edit_waits_for_authoritative_acknowledgement(self):
        model = SettingsModel()
        model.receive({"settings": {"confirm_frames": 2}})
        model.edit("confirm_frames", "3")
        self.assertEqual(model.commands(), [{"command": "set_many", "settings": {"confirm_frames": 3}}])
        model.receive({"settings": {"confirm_frames": 2}})
        self.assertEqual(model.displayed("confirm_frames"), 3)
        model.receive({"settings": {"confirm_frames": 3}})
        self.assertEqual(model.pending, {})

    def test_invalid_batch_keeps_all_edits_and_sends_nothing(self):
        model = SettingsModel()
        model.receive({"settings": {"diff_threshold": 30, "margin": 100}})
        model.edit("diff_threshold", "50")
        model.edit("margin", "0")
        with self.assertRaises(ValueError):
            model.commands()
        self.assertEqual(len(model.edits), 2)
        self.assertEqual(model.pending, {})

    def test_rejection_returns_to_parent_value(self):
        model = SettingsModel()
        model.edit("observe_only", False)
        model.commands()
        model.receive({"error": "Окно не выбрано", "settings": {"observe_only": True}})
        self.assertIs(model.displayed("observe_only"), True)
        self.assertFalse(model.pending)

    def test_discard_preserves_latest_parent_values(self):
        model = SettingsModel()
        model.edit("observe_only", False)
        model.discard()
        self.assertIs(model.displayed("observe_only"), True)

    def test_numeric_validation(self):
        for invalid in ("nan", "inf", "-inf", "1.5", "256", "-1"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                parse_value("diff_threshold", invalid, 30)
        self.assertEqual(parse_value("diff_threshold", "45", 30), 45)
        self.assertEqual(parse_value("camera_gain", "0.3", 0.5), 0.3)

    def test_boolean_validation(self):
        self.assertIs(parse_value("auto_click", "off", False), False)
        self.assertIs(parse_value("auto_click", True, False), True)
        with self.assertRaises(ValueError):
            parse_value("auto_click", "maybe", False)

    def test_exclusion_rectangles(self):
        expected = [[0.0, 0.8, 1, 1]]
        self.assertEqual(parse_value("exclusion_rects", "[[0, 0.8, 1, 1]]", []), expected)
        self.assertEqual(parse_value("exclusion_rects", "[[0, 0.8, 1, 1]]", "[]"), "[[0, 0.8, 1, 1]]")
        for invalid in ("{}", "[[0,0,0,1]]", "[[0,0,2,1]]", "[[0,0,1]]", "[[false,0,1,1]]"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                parse_value("exclusion_rects", invalid, [])

    def test_choice_validation(self):
        self.assertEqual(parse_value("aim_mode", "AUTO", "relative"), "auto")
        with self.assertRaises(ValueError):
            parse_value("aim_mode", "other", "auto")


if __name__ == "__main__":
    unittest.main()
