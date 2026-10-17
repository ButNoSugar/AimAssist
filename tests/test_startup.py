"""Регрессия на то, что перестановка импортов и бутстрап ничего не сломали."""

import sys, io, os, tempfile, json, ctypes, threading
import sitecustomize  # noqa
import keyboard

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "aim_tracker.py")
STUBS = os.path.join(HERE, "stubs")
_SRC = open(SCRIPT, encoding='utf-8').read()

fails = []
def check(label, ok, extra=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{extra}")
    if not ok: fails.append(label)

def run(stdin_text="140\n", presses=0):
    os.chdir(tempfile.mkdtemp())
    open('aim_tracker.py', 'w', encoding='utf-8').write(_SRC)
    keyboard.LIMIT[0] = 0; keyboard.EOF[0] = True; keyboard.PRESSES['ctrl'] = presses
    sys.stdin = io.StringIO(stdin_text)
    G = {'__name__': '__main__', '__file__': 'aim_tracker.py'}
    buf = io.StringIO(); real = sys.stdout; sys.stdout = buf
    exc = None
    try:
        exec(compile(_SRC, 'aim_tracker.py', 'exec'), G)
    except BaseException as e:
        exc = e
    finally:
        sys.stdout = real
    return buf.getvalue(), G, exc

print("== обычный старт цел: конфиг, профиль, бенчмарк, оверлей ==")
out, G, exc = run()
check("без исключений", exc is None, f"  ({type(exc).__name__ if exc else '-'})")
check("бутстрап промолчал (модули на месте)", "перезапускаюсь через" not in out)
check("спросил margin и создал конфиг", "Введите отступ" in out and os.path.exists("aim_config.json"))
check("прогнал бенчмарк", "Замер производительности" in out)
check("оверлей поднялся", G['overlay'].error is None)
check("margin из ввода", G['cfg'].margin == 140, f"  ({G['cfg'].margin})")
saved = json.load(open("aim_config.json", encoding="utf-8"))["profiles"]["default"]
check("все настройки Config в профиле", len(saved) == len(vars(G['Config']())),
      f"  ({len(saved)} из {len(vars(G['Config']()))})")
check("pause_on_start есть и выключен", saved["pause_on_start"] is False)

print("== наведение в игре на месте ==")
WIN = ctypes.windll.WIN
WIN["client"] = (100, 50, 800, 600); WIN["sent"].clear()
G['_window_cache']['ts'] = 0.0
view = G['update_aim_context']()
check("окно найдено, прицел = центр окна", view == (100, 50, 800, 600)
      and G['_aim']["crosshair"] == (500, 350), f"  {G['_aim']['crosshair']}")
G['cfg'].camera_gain = 1.0; G['cfg'].dead_zone = 0
G['move_relative_to_target'](600, 300)
check("SendInput получил смещение от прицела", WIN["sent"][0][:2] == (100, -50), f"  {WIN['sent'][:1]}")

print("== клик-троттлинг на месте ==")
check("click_interval в настройках", hasattr(G['cfg'], 'click_interval'))
check("click_is_due пускает при 0", G['cfg'].click_interval == 0.0 and G['click_is_due']() is True)

print("== hold_console не вешает процесс без tty ==")
done = threading.Event()
def worker():
    sys.stdin = io.StringIO("")          # StringIO.isatty() -> False
    G['_console_held'][0] = False
    G['hold_console']()
    done.set()
threading.Thread(target=worker, daemon=True).start()
check("вернулся сразу, не ждёт Enter", done.wait(5))

print("== из run.bat скрипт не спрашивает Enter второй раз ==")
G['_console_held'][0] = False
os.environ['AIMTRACKER_LAUNCHER'] = '1'
done2 = threading.Event()
def worker2():
    G['hold_console'](); done2.set()
threading.Thread(target=worker2, daemon=True).start()
check("сразу вернулся при LAUNCHER=1", done2.wait(5))
check("флаг удержания не взведён", G['_console_held'][0] is False)
del os.environ['AIMTRACKER_LAUNCHER']

print("== проверка платформы и деградация оверлея целы ==")
sys.platform = "linux"
out2, G2, exc2 = run()
check("на чужой ОС SystemExit(1)", isinstance(exc2, SystemExit) and exc2.code == 1)
check("объяснил причину", "рассчитан на Windows" in out2)
sys.platform = "win32"

import tkinter as tk
real_tk = tk.Tk
class BrokenTk:
    def __init__(self): raise RuntimeError("нет дисплея")
tk.Tk = BrokenTk
out3, G3, exc3 = run()
tk.Tk = real_tk
check("сломанный оверлей не валит скрипт", exc3 is None, f"  ({type(exc3).__name__ if exc3 else '-'})")
check("ушёл в быстрый режим", G3['cfg'].overlay_enabled is False)

print("\nПРОВАЛОВ: %d" % len(fails))
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
