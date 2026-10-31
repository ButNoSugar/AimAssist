"""UI state and validation tests; no display or Windows stubs required."""
import pathlib
import ast
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from aim_ui import ControlPanel, SettingsModel, parse_value, setting_matches


class Variable:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def trace_add(self, *args):
        pass


class Widget:
    def __init__(self, parent=None, **options):
        self.options, self.children = options, []
        self.index, self.packed = -1, False
        if parent is not None:
            parent.children.append(self)

    def pack(self, **options):
        self.packed = True

    def pack_forget(self):
        self.packed = False

    def configure(self, **options):
        self.options.update(options)

    def current(self, index=None):
        if index is not None:
            self.index = index
        return self.index

    def set(self, value):
        self.index = -1


def panel_without_display():
    panel = object.__new__(ControlPanel)
    panel.model = SettingsModel()
    panel.tk = SimpleNamespace(BooleanVar=Variable, StringVar=Variable)
    panel.ttk = SimpleNamespace(**{name: Widget for name in (
        'Frame', 'Checkbutton', 'Label', 'Entry', 'Combobox')})
    panel.variables, panel.all_rows, panel.window_ids = {}, {}, []
    panel.main_fields, panel.all_fields = Widget(), Widget()
    panel.search, panel.dirty = Variable(''), Variable('')
    for name in ('status', 'profile', 'mode_hint', 'reason', 'target',
                 'message', 'work_mode', 'window_hint'):
        setattr(panel, name, Variable(''))
    panel.windows, panel.profiles, panel.preview = Widget(), Widget(), Widget()
    panel.last_preview, panel.preview_image = None, None
    panel.profile_editing = False
    panel.sent, panel.scheduled = [], []
    panel.send = lambda command, **values: panel.sent.append({'command': command, **values})
    panel.root = SimpleNamespace(after=lambda delay, callback: panel.scheduled.append((delay, callback)))
    return panel


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

    def test_all_settings_page_contains_every_application_setting(self):
        source = pathlib.Path(__file__).resolve().parents[1] / 'aim_tracker.py'
        config = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                      if isinstance(node, ast.ClassDef) and node.name == 'Config')
        namespace = {}
        exec(compile(ast.Module(body=[config], type_ignores=[]), str(source), 'exec'), namespace)
        settings = vars(namespace['Config']())
        panel = panel_without_display()
        panel.model.receive({'settings': settings})
        panel.sync_fields()
        self.assertEqual(set(panel.all_rows), set(settings))
        full_checkbox = panel.all_rows['observe_only'].children[0]
        main_checkbox = panel.main_fields.children[0].children[0]
        self.assertIs(full_checkbox.options['variable'], main_checkbox.options['variable'])
        panel.model.edit('diff_threshold', '45')
        panel.search.set('MAX_FRAME_AGE')
        panel.filter_settings()
        self.assertEqual([name for name, row in panel.all_rows.items() if row.packed], ['max_frame_age'])
        panel.search.set('')
        panel.filter_settings()
        self.assertTrue(all(row.packed for row in panel.all_rows.values()))
        self.assertEqual(panel.model.displayed('diff_threshold'), '45')

    def test_search_supports_russian_labels_and_internal_names(self):
        self.assertTrue(setting_matches('camera_gain', 'доводки'))
        self.assertTrue(setting_matches('max_frame_age', 'MAX_FRAME'))
        self.assertFalse(setting_matches('camera_gain', 'несуществующий'))

    def test_window_snapshot_populates_selector_and_preserves_handle(self):
        panel = panel_without_display()
        large = (1 << 40) + 42
        state = {'windows': [{'id': large, 'title': 'Программа', 'exe': 'app.exe'}],
                 'window_id': large, 'settings': {'require_game_window': True}, 'preview': None}
        panel.model.receive(state)
        panel.update_state(state)
        self.assertEqual(panel.windows.options['values'], ['Программа  ·  app.exe'])
        self.assertEqual(panel.window_ids[panel.windows.current()], large)
        self.assertIn('1', panel.window_hint.get())
        state['windows'] = [{'id': 7, 'title': 'Другое окно'}, *state['windows']]
        panel.model.receive(state)
        panel.update_state(state)
        self.assertEqual(panel.windows.current(), 1)

    def test_empty_window_list_and_refresh_error_are_visible(self):
        panel = panel_without_display()
        panel.update_state({'windows': []})
        self.assertIn('не найдены', panel.window_hint.get())
        panel.update_state({'windows_error': 'Ошибка Windows'})
        self.assertEqual(panel.window_hint.get(), 'Ошибка Windows')

    def test_desktop_selection_preserves_other_unapplied_edits(self):
        panel = panel_without_display()
        panel.model.receive({'settings': {'diff_threshold': 30}})
        panel.model.edit('diff_threshold', '45')
        panel.model.edit('observe_only', False)
        panel.work_mode.set('Рабочий стол (основной экран)')
        panel.select_mode()
        self.assertEqual(panel.sent, [{'command': 'set_mode', 'mode': 'desktop'}])
        self.assertEqual(panel.model.edits, {'diff_threshold': '45'})

    def test_auto_refresh_requests_new_windows_periodically(self):
        panel = panel_without_display()
        panel.refresh_windows()
        self.assertEqual(panel.sent, [{'command': 'refresh'}])
        self.assertEqual(panel.scheduled[0][0], 3000)

    def test_profile_load_keeps_requested_name(self):
        panel = panel_without_display()
        panel.profile.set('Рабочий стол')
        panel.profile_command('profile_load')
        self.assertEqual(panel.sent, [{'command': 'profile_load', 'name': 'Рабочий стол'}])


if __name__ == "__main__":
    unittest.main()
