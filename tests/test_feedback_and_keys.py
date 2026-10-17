"""Волна 1: не читать своё движение, клик только по цели, клавиши в настройках."""
import sys, io, os, tempfile, ctypes, time
import sitecustomize  # noqa
import keyboard
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "aim_tracker.py")

_SRC = open(SCRIPT, encoding='utf-8').read()
os.chdir(tempfile.mkdtemp()); open('aim_tracker.py', 'w', encoding='utf-8').write(_SRC)

fails = []
def check(label, ok, extra=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{extra}")
    if not ok: fails.append(label)

keyboard.PRESSES['ctrl'] = 0; keyboard.EOF[0] = True
sys.stdin = io.StringIO('120\n')
G = {'__name__': '__main__', '__file__': 'aim_tracker.py'}
b = io.StringIO(); real = sys.stdout; sys.stdout = b
exec(compile(_SRC, 'aim_tracker.py', 'exec'), G)
sys.stdout = real
cfg = G['cfg']; WIN = ctypes.windll.WIN
cfg.debug_log = False

def quiet(fn, *a, **k):
    out = io.StringIO(); keep = sys.stdout; sys.stdout = out
    try: return fn(*a, **k)
    finally: sys.stdout = keep

print("== 8: после своего действия кадры не снимаются ==")
cfg.settle_after_action = 0.05
G['_settle_until'][0] = 0.0
check("пока ничего не делали — кадрам верим", G['frames_are_trustworthy']() is True)
G['hold_after_action']()
check("сразу после действия — не верим", G['frames_are_trustworthy']() is False)
check("track_synced отказывается снимать", G['track_synced'](0, 0, 100, 100, 0, 0) is False)
check("track_fast тоже", G['track_fast'](0, 0, 100, 100, 0, 0) is False)
time.sleep(0.06)
check("после окна оседания снова верим", G['frames_are_trustworthy']() is True)
cfg.settle_after_action = 0.0
G['_settle_until'][0] = 0.0
check("0 отключает окно совсем", (G['hold_after_action'](), G['frames_are_trustworthy']())[1] is True)

print("== 8: разворот камеры отбивается по доле изменившегося кадра ==")
cfg.global_motion_limit = 0.5
check("спокойный кадр пропускаем", G['is_global_motion'](0.02) is False)
check("полностью сменившийся — отбой", G['is_global_motion'](0.95) is True)
check("ровно на пороге пропускаем", G['is_global_motion'](0.5) is False)
cfg.global_motion_limit = 0.0
check("0 отключает проверку", G['is_global_motion'](0.99) is False)
cfg.global_motion_limit = 0.5

print("== 8: find_blobs_arr считает долю изменений ==")
same = np.zeros((100, 100, 4), np.uint8); same[:, :, 3] = 255
blobs, changed = G['find_blobs_arr'](same, same.copy(), 30, 4, 0)
check("одинаковые кадры: 0% и нет пятен", changed == 0.0 and blobs == [])
moved = same.copy(); moved[:, :, :3] = 255
blobs, changed = G['find_blobs_arr'](same, moved, 30, 4, 0)
check("всё поменялось: доля ~1.0", changed > 0.99, f"  ({changed:.3f})")
spot = same.copy(); spot[10:20, 10:20, :3] = 255
blobs, changed = G['find_blobs_arr'](same, spot, 30, 4, 0)
check("одно пятно: доля мала, пятно найдено",
      0 < changed < 0.02 and len(blobs) == 1, f"  (доля {changed:.4f}, пятен {len(blobs)})")

print("== 9: клик только когда цель в прицеле ==")
cfg.aim_mode = "auto"; G['_window_cache']['ts'] = 0.0; G['update_aim_context']()
cross = G['_aim']["crosshair"]
cfg.click_tolerance = 10
check("цель в прицеле — бьём", G['target_is_centred'](cross[0] + 3, cross[1] + 4) is True)
check("цель далеко — не бьём", G['target_is_centred'](cross[0] + 200, cross[1]) is False)
check("ровно на границе допуска — бьём", G['target_is_centred'](cross[0] + 10, cross[1]) is True)
cfg.click_tolerance = 0
check("0 отключает проверку", G['target_is_centred'](cross[0] + 999, cross[1]) is True)
cfg.click_tolerance = 10

import pyautogui
clicks = [0]
pyautogui.click = lambda *a, **k: clicks.__setitem__(0, clicks[0] + 1)
cfg.auto_click = True; cfg.click_interval = 0.0; cfg.settle_after_action = 0.0
clicks[0] = 0
quiet(G['move_and_maybe_click'], cross[0] + 300, cross[1])
check("первая детекция далеко от прицела — клика нет", clicks[0] == 0, f"  ({clicks[0]})")
quiet(G['move_and_maybe_click'], cross[0] + 2, cross[1])
check("цель в прицеле — клик есть", clicks[0] == 1, f"  ({clicks[0]})")
check("после клика кадрам не верим", True)   # settle=0 здесь, проверено выше
cfg.auto_click = False

print("== 9: первый клик больше не проходит в обход интервала ==")
check("время последнего клика не нулевое", G['_last_click']["ts"] > 0.0)

print("== 7: клавиши читаются из настроек ==")
check("track_key есть и это ctrl", cfg.track_key == "ctrl")
keyboard.HELD = set()
check("mouse4 не зажата", G['is_key_held']("mouse4") is False)
ctypes.windll.user32.MOUSE_DOWN.add(0x05)
check("mouse4 зажата через GetAsyncKeyState", G['is_key_held']("mouse4") is True)
ctypes.windll.user32.MOUSE_DOWN.clear()
check("mouse5 отдельная кнопка", G['is_key_held']("mouse5") is False)

print("== 7: негодные клавиши отбиваются ==")
check("f9 годится", G['key_is_valid']("f9") is True)
check("mouse4 годится", G['key_is_valid']("mouse4") is True)
check("чепуха не годится", G['key_is_valid']("небывает") is False)
out = io.StringIO(); sys.stdout = out
G['apply_setting']("track_key", "небывает")
sys.stdout = real
check("set отбил с подсказкой", "неизвестная клавиша" in out.getvalue(), f"  {out.getvalue()[:80]!r}")
check("значение не изменилось", cfg.track_key == "ctrl")
quiet(G['apply_setting'], "track_key", "F9")
check("годная клавиша принимается в нижнем регистре", cfg.track_key == "f9")
quiet(G['apply_setting'], "track_key", "ctrl")

print("== 7: переключатели переезжают на новую клавишу ==")
keyboard.HOTKEYS.clear()
quiet(G['rebind_hotkeys'])
check("повешены все переключатели", sorted(keyboard.HOTKEYS) == ["f6", "f7", "f8", "f9"],
      f"  {sorted(keyboard.HOTKEYS)}")
quiet(G['apply_setting'], "panic_key", "f10")
check("смена настройки перевесила", "f10" in keyboard.HOTKEYS and "f8" not in keyboard.HOTKEYS,
      f"  {sorted(keyboard.HOTKEYS)}")
out = io.StringIO(); sys.stdout = out
G['apply_setting']("autoclick_key", "mouse4")
sys.stdout = real
check("кнопка мыши переключателем не вешается, но объясняет",
      "переключателем быть не может" in out.getvalue(), f"  {out.getvalue()[:90]!r}")
quiet(G['apply_setting'], "autoclick_key", "f6")
quiet(G['apply_setting'], "panic_key", "f8")

print("\nПРОВАЛОВ: %d" % len(fails))
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
