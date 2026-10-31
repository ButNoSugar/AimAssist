"""Integration regressions for capture/input/config without starting the desktop.

The functions are compiled unchanged from the application's AST. Only the OS
adapters and monotonic clock are replaced, so these tests exercise the actual
integration rather than another implementation of the pipeline.
"""
import ast
import ctypes
from functools import wraps
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from contextlib import contextmanager

import sitecustomize  # supplied by tests/run.py: only desktop/input adapters
import keyboard
import numpy as np
import pyautogui

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aim_detection import DetectionOptions, TargetTracker, detect_changes
from aim_recording import SessionRecorder, load_session


class Grabber:
    def __init__(self):
        self.frame = None
        self.ts = 0.0
        self.regions = []

    def set_region(self, region):
        self.regions.append(region)

    def get_latest(self, region):
        return self.frame, self.ts


NAMES = {
    'Config', 'state_locked', 'reset_lock', 'reset_frame_history', 'reset_tracking',
    'hold_after_action', 'frames_are_trustworthy', 'track_fast',
    'move_and_maybe_click', 'input_allowed', 'panic_stop', 'detection_options',
    'coerce_setting', 'bounds_error', 'parse_bool', 'key_is_valid',
    'load_config_file', 'save_config_file', 'current_values', 'builtin_defaults',
    'empty_config', 'apply_values', 'assign_setting', 'profile_load',
    'profile_delete', 'profile_save', 'profile_names', 'intersect_region',
    'process_pair', 'select_target', 'is_global_motion',
    'window_text', 'window_matches', 'process_name',
    'apply_settings_batch', 'handle_engine_command',
    'update_aim_context', 'client_rect_on_screen', 'list_windows',
    'refresh_panel_windows', 'configure_window_api', 'RECT', 'POINT',
}
CONSTANTS = {'BOOL_TRUE', 'BOOL_FALSE', 'MIN_VALUES', 'MAX_VALUES',
             'ALLOWED_VALUES', 'MOUSE_KEYS', 'KEY_SETTINGS', 'DEFAULT_PROFILE_NAME',
             'PROCESS_QUERY_LIMITED_INFORMATION', 'WNDENUMPROC'}
TREE = ast.parse((ROOT / 'aim_tracker.py').read_text(encoding='utf-8'))


@contextmanager
def replaced_attribute(obj, name, value):
    previous = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, previous)


class IntegrationRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = [10.0]
        self.g = {
            'math': math, 'json': json, 'os': os, 'shutil': shutil,
            'tempfile': tempfile, 'ctypes': ctypes, 'keyboard': keyboard,
            'wraps': wraps, '_state_lock': threading.RLock(),
            'pyautogui': pyautogui, 'np': np,
            'DetectionOptions': DetectionOptions, 'detect_changes': detect_changes,
            'time': SimpleNamespace(perf_counter=lambda: self.clock[0]),
            'center_x': 840, 'center_y': 525, 'MAX_BLOBS_SHOWN': 24,
            'print': lambda *args, **kwargs: None,
            'paint': lambda value, *args: value,
        }
        for node in TREE.body:
            selected = (isinstance(node, (ast.FunctionDef, ast.ClassDef))
                        and node.name in NAMES)
            if isinstance(node, ast.Assign):
                selected = any(isinstance(target, ast.Name)
                               and target.id in CONSTANTS for target in node.targets)
            if selected:
                exec(compile(ast.Module(body=[node], type_ignores=[]),
                             'aim_tracker.py', 'exec'), self.g)
        g = self.g
        g['cfg'] = g['Config']()
        g['_target_tracker'] = TargetTracker()
        g['_prev_holder'] = {'frame': None}
        g['_last_used_ts'] = 0.0
        g['_previous_ts'] = [0.0]
        g['_baseline_region'] = [None]
        g['_baseline_after'] = [0.0]
        g['_settle_until'] = [0.0]
        g['_move_remainder'] = {'x': 0.0, 'y': 0.0}
        g['_stop'] = threading.Event()
        g['_runtime'] = {'paused': False, 'dirty': False, 'reason': '', 'error': ''}
        g['_tune'] = {'paused': False, 'frames': []}
        g['_lock'] = {'pos': None, 'ts': 0., 'held': False}
        g['_stat'] = {'motion': 0.0}
        g['_detect'] = {'rejected': [], 'mode': 'Margin'}
        g['_aim'] = {'hwnd': 4242, 'viewport': (100, 50, 800, 600),
                     'crosshair': (500, 350), 'relative': True}
        g['_selected_hwnd'] = [None]
        g['_panel'] = None
        g['foreground_window_title'] = lambda: (ctypes.windll.WIN['hwnd'], 'Minecraft')
        g['_config_lock'] = threading.RLock()
        g['_config'] = {'data': g['empty_config'](), 'active': 'default',
                        'autosave_ok': True}
        g['CONFIG_PATH'] = str(Path(self.directory.name, 'aim_config.json'))
        g['grabber'] = Grabber()
        g['format_value'] = str
        g['set_overlay_enabled'] = lambda value: setattr(g['cfg'], 'overlay_enabled', value)
        g['rebind_hotkeys'] = lambda: None
        g['preview_region'] = lambda: None
        g['confirm'] = lambda message: True
        self.focus = ctypes.windll.WIN['hwnd']
        ctypes.windll.WIN['hwnd'] = 4242
        self.addCleanup(lambda: ctypes.windll.WIN.__setitem__('hwnd', self.focus))

    def call(self, name, *args):
        return self.g[name](*args)

    def test_desktop_mode_ignores_game_and_forces_absolute_cursor(self):
        self.g['cfg'].require_game_window = False
        self.g['cfg'].aim_mode = 'relative'
        self.assertIsNone(self.call('update_aim_context'))
        self.assertFalse(self.g['_aim']['relative'])
        self.assertEqual(self.g['_aim']['crosshair'], (840, 525))
        self.assertEqual(self.g['_aim']['hwnd'], 0)
        ctypes.windll.WIN['hwnd'] = 7
        self.assertTrue(self.call('input_allowed'))

    def test_mode_command_resets_history_and_returns_to_observation(self):
        self.g['cfg'].observe_only = False
        self.g['_prev_holder']['frame'] = np.zeros((10, 10, 4), np.uint8)
        self.call('handle_engine_command', {'command': 'set_mode', 'mode': 'desktop'})
        self.assertFalse(self.g['cfg'].require_game_window)
        self.assertTrue(self.g['cfg'].observe_only)
        self.assertIsNone(self.g['_prev_holder']['frame'])
        self.assertFalse(self.g['_aim']['relative'])
        self.call('handle_engine_command', {'command': 'set_mode', 'mode': 'window'})
        self.assertTrue(self.g['cfg'].require_game_window)
        self.assertEqual(self.g['_aim']['viewport'], (100, 50, 800, 600))
        self.assertTrue(self.g['_aim']['relative'])

    def test_desktop_click_is_checked_after_cursor_arrives(self):
        self.input_spies()
        self.g['cfg'].require_game_window = False
        self.g['cfg'].observe_only = False
        self.g['_aim']['relative'] = False
        cursor = [0, 0]
        def move(x, y):
            self.actions.append(('absolute', (x, y)))
            cursor[:] = [x, y]
        self.g['move_absolute_to_target'] = move
        self.g['target_is_centred'] = lambda x, y: tuple(cursor) == (x, y)
        self.assertTrue(self.call('move_and_maybe_click', 150, 200))
        self.assertEqual(self.actions, [('absolute', (150, 200)), ('click',)])

    def test_window_catalog_includes_small_and_pointer_sized_handles(self):
        large = (1 << 40) + 42
        additions = {large: ('Large HWND', 'app.exe', 0, 0, 500, 400),
                     27: ('Small tool', 'tool.exe', 0, 0, 50, 40)}
        windows = ctypes.windll.WIN['windows']
        original = dict(windows)
        windows.update(additions)
        try:
            result = self.call('list_windows')
        finally:
            windows.clear()
            windows.update(original)
        ids = {item[0] for item in result}
        self.assertIn(large, ids)
        self.assertIn(27, ids)

    def test_process_metadata_failure_does_not_hide_window(self):
        self.g['process_name'] = lambda hwnd: (_ for _ in ()).throw(OSError('denied'))
        result = self.call('list_windows')
        self.assertTrue(result)
        self.assertTrue(all(item[2] == '' for item in result))

    def test_failed_windows_enumeration_is_not_reported_as_empty_success(self):
        with replaced_attribute(ctypes.windll.user32, 'EnumWindows', lambda *args: 0):
            with self.assertRaisesRegex(OSError, 'перечислить'):
                self.call('list_windows')

    def test_partial_desktop_movement_does_not_click_before_arrival(self):
        self.input_spies()
        self.g['cfg'].require_game_window = False
        self.g['cfg'].observe_only = False
        self.g['_aim']['relative'] = False
        self.g['move_absolute_to_target'] = lambda x, y: self.actions.append(('partial', (x, y)))
        self.g['target_is_centred'] = lambda x, y: False
        self.call('move_and_maybe_click', 150, 200)
        self.assertEqual(self.actions, [('partial', (150, 200))])

    def test_window_refresh_keeps_last_catalog_and_reports_failure(self):
        previous = [{'id': 7, 'title': 'Notepad', 'exe': 'notepad.exe'}]
        self.g['_runtime']['windows'] = previous
        self.g['_runtime']['error'] = 'invalid setting'
        self.g['list_windows'] = lambda: (_ for _ in ()).throw(OSError('enumeration failed'))
        self.call('handle_engine_command', {'command': 'refresh'})
        self.assertEqual(self.g['_runtime']['windows'], previous)
        self.assertIn('enumeration failed', self.g['_runtime']['windows_error'])
        self.assertEqual(self.g['_runtime']['error'], 'invalid setting')

    def test_window_api_uses_pointer_sized_hwnd_and_process_handles(self):
        class Function:
            pass
        user32 = SimpleNamespace(**{name: Function() for name in (
            'GetForegroundWindow', 'GetWindowTextW', 'EnumWindows')})
        kernel32 = SimpleNamespace(**{name: Function() for name in ('OpenProcess', 'CloseHandle')})
        with replaced_attribute(ctypes, 'windll', SimpleNamespace(user32=user32, kernel32=kernel32)):
            self.call('configure_window_api')
        self.assertIs(user32.GetForegroundWindow.restype, ctypes.c_void_p)
        self.assertIs(kernel32.OpenProcess.restype, ctypes.c_void_p)
        self.assertIs(user32.GetWindowTextW.argtypes[0], ctypes.c_void_p)
        self.assertIs(kernel32.CloseHandle.argtypes[0], ctypes.c_void_p)
        self.assertIs(self.g['WNDENUMPROC']._restype_, ctypes.c_int)

    def prepare_frames(self, region=(0, 0, 30, 30)):
        g = self.g
        g['_baseline_region'][0] = region
        g['cfg'].overlay_enabled = False
        a = np.zeros((region[3], region[2], 4), np.uint8)
        b = a.copy(); b[5:10, 5:10, :3] = 255
        c = b.copy(); c[15:20, 15:20, :3] = 255
        self.pairs = []
        g['process_pair'] = lambda *args: self.pairs.append(args) or True
        return a, b, c

    def frame(self, value, timestamp, now, region=(0, 0, 30, 30)):
        self.clock[0] = now
        self.g['grabber'].frame, self.g['grabber'].ts = value, timestamp
        return self.call('track_fast', *region, region[0], region[1])

    def test_action_cooldown_requires_two_post_action_frames(self):
        a, b, c = self.prepare_frames()
        self.assertFalse(self.frame(a, 9.99, 10.0))
        self.call('hold_after_action')
        self.assertIsNone(self.g['_prev_holder']['frame'])
        self.assertFalse(self.frame(b, 10.02, 10.03))  # during cooldown
        self.assertFalse(self.frame(b, 10.02, 10.06))  # captured during cooldown
        self.assertFalse(self.frame(b, 10.051, 10.06))  # first trustworthy frame
        self.assertTrue(self.frame(c, 10.07, 10.08))
        self.assertEqual(len(self.pairs), 1)
        self.assertIs(self.pairs[0][0], b)
        self.assertIs(self.pairs[0][1], c)
        self.assertGreaterEqual(self.pairs[0][3], 10.05)

    def test_same_size_region_shift_discards_previous_region(self):
        a, b, c = self.prepare_frames()
        self.assertFalse(self.frame(a, 9.99, 10.))
        moved = (50, 50, 30, 30)
        self.assertFalse(self.frame(b, 10.01, 10.01, moved))
        self.assertTrue(self.frame(c, 10.02, 10.03, moved))
        self.assertEqual(len(self.pairs), 1)
        self.assertIs(self.pairs[0][0], b)
        self.assertEqual(self.pairs[0][2], moved)

    def test_stale_frame_flushes_history_and_cannot_generate_action(self):
        a, b, c = self.prepare_frames()
        self.g['_prev_holder']['frame'] = a
        self.g['_previous_ts'][0] = 8.
        self.assertFalse(self.frame(b, 8.02, 10.))
        self.assertIsNone(self.g['_prev_holder']['frame'])
        self.assertEqual(self.pairs, [])
        self.assertFalse(self.frame(b, 10.01, 10.02))
        self.assertTrue(self.frame(c, 10.03, 10.04))
        self.assertIs(self.pairs[0][0], b)

    def test_duplicate_frame_timestamp_is_not_confirmation(self):
        a, b, _ = self.prepare_frames()
        self.assertFalse(self.frame(a, 9.99, 10.))
        self.assertFalse(self.frame(b, 9.99, 10.01))
        self.assertEqual(self.pairs, [])

    def test_pair_that_ages_during_processing_cannot_generate_input(self):
        g = self.g
        g['cfg'].overlay_enabled = False
        g['cfg'].observe_only = False
        g['cfg'].confirm_frames = 1
        g['capture_tune_frame'] = lambda *args, **kwargs: None
        g['maybe_save_last_frame'] = lambda *args: None
        actions = []
        g['move_and_maybe_click'] = lambda *args: actions.append(args)
        a = np.zeros((30, 30, 4), np.uint8)
        b = a.copy(); b[5:10, 5:10, :3] = 255
        self.assertTrue(self.call('process_pair', a, b, (0, 0, 30, 30), 9.7, 9.8))
        self.assertEqual(actions, [])
        self.assertIn('устарел', g['_runtime']['reason'])

    def test_recording_copies_exact_input_pair_before_action(self):
        g = self.g
        g['cfg'].overlay_enabled = False
        g['cfg'].observe_only = False
        g['cfg'].confirm_frames = 1
        g['cfg'].tune_mode = True
        g['_recorder'] = SessionRecorder()
        g['capture_tune_frame'] = lambda *args, **kwargs: None
        g['maybe_save_last_frame'] = lambda *args: None
        a = np.zeros((30, 30, 4), np.uint8)
        b = a.copy(); b[5:10, 5:10, :3] = 255
        original = b.copy()
        # A deliberate mutation makes recording-after-action observably wrong.
        def move(*args):
            b.fill(0)
            return True
        g['move_and_maybe_click'] = move
        self.assertTrue(self.call('process_pair', a, b, (0, 0, 30, 30), 9.98, 9.99))
        self.assertEqual(int(b.sum()), 0)
        folder = g['_recorder'].dump(self.directory.name)
        records = list(load_session(folder))
        self.assertEqual(len(records), 1)
        before, after, region, metadata, mask = records[0]
        self.assertTrue(np.array_equal(before, a))
        self.assertTrue(np.array_equal(after, original))
        self.assertEqual(region, (0, 0, 30, 30))
        self.assertEqual(metadata['prev_ts'], 9.98)
        self.assertEqual(metadata['current_ts'], 9.99)
        self.assertFalse(metadata['observe_only'])
        self.assertTrue(metadata['action_allowed'])
        self.assertTrue(metadata['input_requested'])
        self.assertFalse(metadata['decision_pending'])
        self.assertEqual(metadata['skip_reason'], '')
        self.assertIsNotNone(mask)

    def test_negative_decisions_are_recorded_for_replay(self):
        g = self.g
        g['cfg'].overlay_enabled = False
        g['cfg'].tune_mode = True
        g['_recorder'] = SessionRecorder()
        g['capture_tune_frame'] = lambda *args, **kwargs: None
        actions = []
        g['move_and_maybe_click'] = lambda *args: actions.append(args)
        a = np.zeros((30, 30, 4), np.uint8)
        self.assertFalse(self.call('process_pair', a, a.copy(), (0, 0, 30, 30), 9.98, 9.99))
        self.assertEqual(actions, [])
        records = list(load_session(g['_recorder'].dump(self.directory.name)))
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0][3]['target'])
        self.assertFalse(records[0][3]['action_allowed'])
        self.assertFalse(records[0][3]['input_requested'])
        self.assertFalse(records[0][3]['decision_pending'])
        self.assertEqual(records[0][3]['skip_reason'], 'no_target')
        self.assertEqual(int(records[0][4].sum()), 0)

    def test_recording_latency_rechecks_age_and_finalizes_skip_reason(self):
        g, clock = self.g, self.clock
        class SlowRecorder(SessionRecorder):
            def remember(self, *args, **kwargs):
                token = super().remember(*args, **kwargs)
                clock[0] += .2
                return token
        g['cfg'].overlay_enabled = False
        g['cfg'].observe_only = False
        g['cfg'].confirm_frames = 1
        g['cfg'].tune_mode = True
        g['_recorder'] = SlowRecorder()
        g['capture_tune_frame'] = lambda *args, **kwargs: None
        g['maybe_save_last_frame'] = lambda *args: None
        actions = []
        g['move_and_maybe_click'] = lambda *args: actions.append(args) or True
        a = np.zeros((30, 30, 4), np.uint8)
        b = a.copy(); b[5:10, 5:10, :3] = 255
        self.assertTrue(self.call('process_pair', a, b, (0, 0, 30, 30), 9.98, 9.99))
        self.assertEqual(actions, [])
        record = list(load_session(g['_recorder'].dump(self.directory.name)))[0]
        metadata = record[3]
        self.assertFalse(metadata['action_allowed'])
        self.assertFalse(metadata['input_requested'])
        self.assertFalse(metadata['decision_pending'])
        self.assertEqual(metadata['skip_reason'], 'frame_stale')
        self.assertGreater(metadata['frame_age_ms'], 200)

    def input_spies(self):
        self.actions = []
        g = self.g
        g['move_relative_to_target'] = lambda *args: self.actions.append(('move', args))
        g['move_absolute_to_target'] = lambda *args: self.actions.append(('absolute', args))
        g['target_is_centred'] = lambda *args: True
        g['click_is_due'] = lambda: True
        # Replace the adapter in this namespace; don't mutate the shared stub.
        g['pyautogui'] = SimpleNamespace(click=lambda: self.actions.append(('click',)))
        g['cfg'].auto_click = True

    def test_observe_only_prevents_both_movement_and_clicks(self):
        self.input_spies()
        self.g['cfg'].observe_only = True
        self.assertFalse(self.call('move_and_maybe_click', 500, 350))
        self.assertEqual(self.actions, [])
        self.assertEqual(self.g['_settle_until'][0], 0)

    def test_inactive_game_prevents_input(self):
        self.input_spies()
        self.g['cfg'].observe_only = False
        ctypes.windll.WIN['hwnd'] = 7
        self.assertFalse(self.call('move_and_maybe_click', 500, 350))
        self.assertEqual(self.actions, [])

    def test_releasing_tracking_key_prevents_input_from_pending_pair(self):
        self.input_spies()
        self.g['cfg'].observe_only = False
        self.g['_runtime']['held_key'] = 'ctrl'
        self.g['is_key_held'] = lambda key: False
        self.assertFalse(self.call('input_allowed'))
        self.assertFalse(self.call('move_and_maybe_click', 500, 350))
        self.assertEqual(self.actions, [])

    def test_focus_change_after_movement_prevents_click(self):
        self.input_spies()
        self.g['cfg'].observe_only = False
        def move(*args):
            self.actions.append(('move', args))
            ctypes.windll.WIN['hwnd'] = 7
        self.g['move_relative_to_target'] = move
        self.assertTrue(self.call('move_and_maybe_click', 500, 350))
        self.assertEqual(self.actions, [('move', (500, 350))])

    def test_pause_and_emergency_stop_prevent_input(self):
        self.input_spies()
        self.g['cfg'].observe_only = False
        for cause in ('runtime', 'tune', 'stop'):
            with self.subTest(cause=cause):
                self.g['_runtime']['paused'] = cause == 'runtime'
                self.g['_tune']['paused'] = cause == 'tune'
                if cause == 'stop':
                    self.g['_stop'].set()
                self.assertFalse(self.call('move_and_maybe_click', 500, 350))
        self.assertEqual(self.actions, [])

    def test_f8_in_tune_mode_stops_without_deleting_recordings(self):
        sentinel = Path(self.directory.name, 'saved-session.npz')
        sentinel.write_bytes(b'valuable recorded scene')
        self.g['cfg'].tune_mode = True
        self.g['_tune']['frames'] = [b'frame']
        self.call('panic_stop')
        self.assertTrue(self.g['_stop'].is_set())
        self.assertEqual(self.g['_tune']['frames'], [b'frame'])
        self.assertEqual(sentinel.read_bytes(), b'valuable recorded scene')
        self.assertFalse(self.g['_tune']['paused'])

    def test_viewport_masks_are_mapped_to_crop_coordinates(self):
        self.g['cfg'].exclusion_rects = '[[0.25,0.25,0.5,0.5]]'
        options = self.call('detection_options', (300, 200, 200, 200))
        self.assertEqual(options.ignore_rects, [(0, 0, 1, .75)])
        a = np.zeros((200, 200, 4), np.uint8)
        b = a.copy(); b[20:30, 20:30, :3] = 255; b[170:180, 20:30, :3] = 255
        result = detect_changes(a, b, options)
        self.assertEqual(len(result.blobs), 1)
        self.assertGreater(result.blobs[0]['center'][1], 150)
        self.assertEqual(int(result.mask[:150].sum()), 0)

    def test_bottom_mask_uses_game_bottom_not_crop_bottom(self):
        self.g['cfg'].ignore_bottom_fraction = .2
        options = self.call('detection_options', (300, 450, 200, 200))
        self.assertEqual(options.ignore_bottom_fraction, 0)
        self.assertEqual(options.ignore_rects, [(0, .4, 1, 1)])
        upper_crop = self.call('detection_options', (300, 200, 200, 200))
        self.assertEqual(upper_crop.ignore_rects, [])
        self.assertEqual(upper_crop.ignore_bottom_fraction, 0)

    def test_crop_intersection_stays_inside_game_client(self):
        self.assertEqual(self.call('intersect_region', (50, 0, 300, 300), (100, 50, 800, 600)),
                         (100, 50, 250, 250))

    def test_nonfinite_values_fractional_integers_and_bad_booleans_rejected(self):
        for name in ('scan_interval', 'camera_gain', 'max_frame_age', 'min_blob_area'):
            for value in ('nan', 'inf', '-inf', float('nan')):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.call('coerce_setting', name, value)
        for value in (1, [], {}, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.call('coerce_setting', 'auto_click', value)
        with self.assertRaises(ValueError):
            self.call('coerce_setting', 'min_blob_area', '1.5')
        self.assertIsNotNone(self.call('bounds_error', 'diff_threshold', 256))

    def test_invalid_exclusion_rects_and_conflicting_hotkeys_rejected(self):
        for value in ('null', '[[0,0,2,1]]', '[[0,0,NaN,1]]', '[[0,1,1,0]]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.call('coerce_setting', 'exclusion_rects', value)
        with self.assertRaises(ValueError):
            self.call('coerce_setting', 'panic_key', 'mouse4')
        with self.assertRaises(ValueError):
            self.call('coerce_setting', 'panic_key', self.g['cfg'].track_key)

    def test_malformed_profile_values_are_skipped(self):
        data = {'version': 1, 'default_profile': [], 'bench': [],
                'profiles': {'valid': {'margin': 200}, 'null': None, 'array': [], '': {}}}
        Path(self.g['CONFIG_PATH']).write_text(json.dumps(data), encoding='utf-8')
        loaded = self.call('load_config_file')
        self.assertEqual(loaded['profiles'], {'valid': {'margin': 200}})
        self.assertEqual(loaded['default_profile'], 'default')
        self.assertEqual(loaded['bench'], {})
        old_margin = self.g['cfg'].margin
        self.call('apply_values', None)
        self.assertEqual(self.g['cfg'].margin, old_margin)

    def test_invalid_profile_setting_does_not_poison_current_config(self):
        self.call('apply_values', {'scan_interval': float('nan'), 'auto_click': [1],
                                   'margin': 200})
        self.assertTrue(math.isfinite(self.g['cfg'].scan_interval))
        self.assertFalse(self.g['cfg'].auto_click)
        self.assertEqual(self.g['cfg'].margin, 200)

    def test_profile_can_swap_hotkeys_without_transient_conflict(self):
        self.call('apply_values', {'track_key': 'alt', 'fullscreen_key': 'ctrl'})
        self.assertEqual(self.g['cfg'].track_key, 'alt')
        self.assertEqual(self.g['cfg'].fullscreen_key, 'ctrl')
        self.call('apply_values', self.call('builtin_defaults'))
        self.assertEqual(self.g['cfg'].track_key, 'ctrl')
        self.assertEqual(self.g['cfg'].fullscreen_key, 'alt')

    def test_invalid_hotkey_fallback_cannot_create_duplicate_binding(self):
        before = {name: getattr(self.g['cfg'], name) for name in self.g['KEY_SETTINGS']}
        self.call('apply_values', {'track_key': 'alt', 'fullscreen_key': 'not_a_key',
                                   'margin': 200})
        after = {name: getattr(self.g['cfg'], name) for name in self.g['KEY_SETTINGS']}
        self.assertEqual(after, before)
        self.assertEqual(len(set(after.values())), len(after))
        self.assertEqual(self.g['cfg'].margin, 200)

    def test_gui_batch_can_swap_hotkeys_and_rebinds_final_mapping(self):
        bindings = []
        self.g['rebind_hotkeys'] = lambda: bindings.append(
            {name: getattr(self.g['cfg'], name) for name in self.g['KEY_SETTINGS']})
        self.call('apply_settings_batch', {'track_key': 'alt', 'fullscreen_key': 'ctrl',
                                          'diff_threshold': 42})
        self.assertEqual(self.g['cfg'].track_key, 'alt')
        self.assertEqual(self.g['cfg'].fullscreen_key, 'ctrl')
        self.assertEqual(self.g['cfg'].diff_threshold, 42)
        self.assertEqual(len(bindings), 1)
        self.assertEqual(len(set(bindings[0].values())), len(bindings[0]))

    def test_invalid_gui_batch_cannot_enable_input_or_partially_apply(self):
        original = self.call('current_values')
        self.call('handle_engine_command', {
            'command': 'set_many',
            'settings': {'observe_only': False, 'camera_gain': .8, 'diff_threshold': 999},
        })
        self.assertEqual(self.call('current_values'), original)
        self.assertTrue(self.g['cfg'].observe_only)
        self.assertTrue(self.g['_runtime']['error'])

    def test_gui_batch_enables_input_after_other_fields_apply(self):
        applied = []
        assign = self.g['assign_setting']
        def observe_order(name, value):
            applied.append((name, self.g['cfg'].observe_only))
            assign(name, value)
        self.g['assign_setting'] = observe_order
        self.call('apply_settings_batch', {'observe_only': False, 'camera_gain': .8,
                                          'diff_threshold': 42})
        self.assertEqual([name for name, _ in applied],
                         ['camera_gain', 'diff_threshold', 'observe_only'])
        self.assertTrue(all(observing for _, observing in applied))
        self.assertFalse(self.g['cfg'].observe_only)

    def test_invalid_gui_batch_rolls_back_supplied_key_changes(self):
        original = self.call('current_values')
        with self.assertRaises(ValueError):
            self.call('apply_settings_batch', {'track_key': 'alt', 'fullscreen_key': 'ctrl',
                                              'min_blob_area': -1})
        self.assertEqual(self.call('current_values'), original)

    def test_deleting_active_profile_loads_replacement_before_autosave(self):
        cfg = self.g['cfg']
        self.g['_config']['data']['profiles'] = {'keep': {'margin': 222}, 'delete': {'margin': 333}}
        self.g['_config']['data']['default_profile'] = 'keep'
        self.g['_config']['active'] = 'delete'
        cfg.margin = 333
        self.call('profile_delete', 'delete')
        self.assertEqual(self.g['_config']['active'], 'keep')
        self.assertEqual(cfg.margin, 222)
        self.call('profile_save')  # simulate shutdown autosave
        saved = json.loads(Path(self.g['CONFIG_PATH']).read_text(encoding='utf-8'))
        self.assertNotIn('delete', saved['profiles'])
        self.assertEqual(saved['profiles']['keep']['margin'], 222)

    def test_atomic_save_preserves_previous_file_when_replace_fails(self):
        path = Path(self.g['CONFIG_PATH'])
        original = json.dumps({'version': 1, 'profiles': {'old': {'margin': 150}}})
        path.write_text(original, encoding='utf-8')
        def failed_replace(*args):
            raise OSError('simulated interrupted replacement')
        self.g['os'] = SimpleNamespace(path=os.path, fsync=os.fsync,
                                        replace=failed_replace, unlink=os.unlink)
        self.assertFalse(self.call('save_config_file'))
        self.assertEqual(path.read_text(encoding='utf-8'), original)
        self.assertEqual(Path(str(path) + '.bak').read_text(encoding='utf-8'), original)
        self.assertEqual(list(path.parent.glob('.aim_config-*.tmp')), [])

    def test_atomic_save_rejects_nonfinite_json_without_truncating_file(self):
        path = Path(self.g['CONFIG_PATH'])
        path.write_text('{"profiles": {}}', encoding='utf-8')
        self.g['_config']['data']['bad'] = float('nan')
        self.assertFalse(self.call('save_config_file'))
        self.assertEqual(path.read_text(encoding='utf-8'), '{"profiles": {}}')
        self.assertEqual(list(path.parent.glob('.aim_config-*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
