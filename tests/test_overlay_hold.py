"""Волна 2: удержание рамок, квадрат области, команда window."""
import sys, io, os, tempfile, time, ctypes
import sitecustomize  # noqa
import keyboard

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
cfg = G['cfg']; overlay = G['overlay']
cfg.debug_log = False

def grab(fn, *a, **k):
    out = io.StringIO(); keep = sys.stdout; sys.stdout = out
    try: fn(*a, **k)
    finally: sys.stdout = keep
    return out.getvalue()

print("== 6: рамки держатся на экране и на это время съёмка стоит ==")
G['_overlay_hold'][0] = 0.0
check("по умолчанию удержания нет", G['overlay_is_held']() is False)
check("box_hold по умолчанию 0 — поведение прежнее", cfg.box_hold == 0.0)
G['hold_overlay'](0.0)
check("0 не включает удержание", G['overlay_is_held']() is False)
G['hold_overlay'](0.3)
check("после hold_overlay рамки держатся", G['overlay_is_held']() is True)
check("пока держатся, track_synced не снимает",
      G['track_synced'](0, 0, 100, 100, 0, 0) is False)
G['_overlay_hold'][0] = 0.0
check("по истечении удержание снято", G['overlay_is_held']() is False)

print("== 6: удержание берётся из настройки после показа рамок ==")
cfg.box_hold = 0.25
cfg.settle_after_action = 0.0
G['_overlay_hold'][0] = 0.0
grab(G['track_synced'], 0, 0, 200, 200, 0, 0)
check("после детекции рамки удерживаются", G['overlay_is_held']() is True)
G['_overlay_hold'][0] = 0.0
cfg.box_hold = 0.0
grab(G['track_synced'], 0, 0, 200, 200, 0, 0)
check("при box_hold=0 удержания нет", G['overlay_is_held']() is False)

print("== 11: смена margin показывает квадрат области ==")
drawn = []
overlay.show_region_and_wait = lambda box: drawn.append(box)
cfg.margin_preview = 1.5
cfg.overlay_enabled = True
G['_overlay_hold'][0] = 0.0
G['_window_cache']['ts'] = 0.0; G['update_aim_context']()
grab(G['apply_setting'], "margin", "160")
check("квадрат нарисован", len(drawn) == 1, f"  {drawn}")
if drawn:
    left, top, right, bottom = drawn[0]
    cross = G['_aim']["crosshair"]
    check("квадрат построен вокруг прицела",
          abs((left + right) // 2 - cross[0]) <= 1 and abs((top + bottom) // 2 - cross[1]) <= 1,
          f"  центр квадрата ({(left+right)//2},{(top+bottom)//2}) против прицела {cross}")
    check("сторона равна 2*margin", (right - left) == 320, f"  ({right - left})")
check("на время показа детекция приостановлена", G['overlay_is_held']() is True)
G['_overlay_hold'][0] = 0.0
cfg.margin_preview = 0.0
drawn.clear()
grab(G['apply_setting'], "margin", "120")
check("margin_preview=0 ничего не рисует", drawn == [])
cfg.margin_preview = 2.0

print("== 3: window больше не показывает бесполезную строку ==")
ctypes.windll.WIN["client"] = (100, 50, 800, 600)
G['_window_cache']['ts'] = 0.0; G['update_aim_context']()
out = grab(G['print_window_info'])
check("строки 'Активно сейчас' нет", "Активно сейчас" not in out, f"\n      {out[:200]}")
check("показано, к чему привязано", "Привязано к:" in out and "Minecraft" in out)
check("показано, когда видели", "Видели:" in out)
check("свежую привязку называет 'прямо сейчас'", "прямо сейчас" in out)
check("показана область и прицел", "800x600" in out and "Прицел:" in out)
check("показан способ движения", "SendInput" in out or "SetCursorPos" in out)

G['_aim']["last_seen"] = time.perf_counter() - 42
out = grab(G['print_window_info'])
check("несвежую привязку называет возрастом", "42 с назад" in out,
      f"  {[l for l in out.splitlines() if 'Видели' in l]}")

G['_aim']["last_match"] = None
out = grab(G['print_window_info'])
check("без привязки честно говорит об этом", "ещё ни разу не попадалось" in out)

print("\nПРОВАЛОВ: %d" % len(fails))
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
