"""Тир A: живая строка, приглашение, шапка, edit по ширине окна."""

import sys, io, os, tempfile, threading
import sitecustomize  # noqa
import keyboard

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "aim_tracker.py")
STUBS = os.path.join(HERE, "stubs")
_SRC = open(SCRIPT, encoding='utf-8').read()
os.chdir(tempfile.mkdtemp()); open('aim_tracker.py','w',encoding='utf-8').write(_SRC)

fails = []
def check(label, ok, extra=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{extra}")
    if not ok: fails.append(label)

os.environ["COLUMNS"] = "78"
keyboard.PRESSES['ctrl'] = 0; keyboard.EOF[0] = True
sys.stdin = io.StringIO('120\n')
G = {'__name__': '__main__', '__file__': 'aim_tracker.py'}
buf = io.StringIO(); real = sys.stdout; sys.stdout = buf
exec(compile(_SRC, 'aim_tracker.py', 'exec'), G)
sys.stdout = real
startup = buf.getvalue()

print("== стартовый вывод компактный, со шапкой ==")
check("шапка есть", "AimTracker" in startup and "═" in startup)
check("шапка назвала окно игры", "Minecraft" in startup and "800x600" in startup)
check("шапка перед замерами", startup.index("AimTracker") < startup.index("Замер производительности"))
check("справка НЕ вываливается при старте", "profile save|load|delete" not in startup)
# мерим от шапки: выше идёт приглашение ask_margin, слипшееся с сообщением о
# конфиге, потому что у StringIO нет эха Enter — в терминале такого не бывает
block = startup[startup.index("AimTracker"):].splitlines()
longest = max((len(l), l) for l in block)
check("нет строк шире 80 колонок", longest[0] <= 80, f"  (самая длинная {longest[0]}: {longest[1][:50]!r})")

print("== подробный лог по умолчанию выключен ==")
check("debug_log off", G['cfg'].debug_log is False)

print("== живая строка: пишется поверх себя, не копится ==")
out = io.StringIO(); sys.stdout = out
plain = lambda t: [(t, None)]     # show_status теперь принимает куски (текст, стиль)
G['show_status'](plain("первая строка состояния"))
G['show_status'](plain("вторая, короче"))
G['show_status'](plain("третья"))
sys.stdout = real
text = out.getvalue()
check("каждое обновление начинается с \\r", text.count("\r") == 3, f"  ({text.count(chr(13))})")
check("нет переводов строк", "\n" not in text)
visible = text.split("\r")[-1]
check("последняя версия видна целиком", visible.startswith("третья"), f"  {visible[:20]!r}")
check("хвост прошлой строки стирается через ESC[K", visible.endswith("\x1b[K"),
      f"  {visible[-6:]!r}")

# то же самое на консоли без VT: там стирать нечем, кроме пробелов
G["_vt"]["ok"] = False
out = io.StringIO(); sys.stdout = out
G["show_status"](plain("длинная строка состояния"))
G["show_status"](plain("короткая"))
sys.stdout = real
tail2 = out.getvalue().split("\r")[-1]
check("без VT хвост затирается пробелами",
      tail2.startswith("короткая") and len(tail2) >= len("длинная строка состояния"),
      f"  ({len(tail2)})")
G["_vt"]["ok"] = True

print("== print убирает живую строку, а не разрезает её ==")
out = io.StringIO(); sys.stdout = out
G['show_status']([("состояние на экране", None)])
G['print']("[Конфиг] сообщение из другого потока")
sys.stdout = real
text = out.getvalue()
head, _, tail = text.partition("[Конфиг]")
check("перед сообщением есть стирание", "\r\x1b[K" in head, f"  {head[-8:]!r}")
check("сообщение на своей строке", tail.endswith("\n"))
check("флаг строки снят", G['_status']["shown"] is False)

print("== строка состояния обрезается по ширине окна ==")
os.environ["COLUMNS"] = "40"
out = io.StringIO(); sys.stdout = out
G['show_status']([("x" * 200, None)])
sys.stdout = real
import re as _re
shown = _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out.getvalue().split("\r")[-1])
check("видимая часть не длиннее окна", len(shown) <= 40, f"  ({len(shown)} при окне 40)")
os.environ["COLUMNS"] = "78"

print("== render_status собирает осмысленную строку ==")
G['_stat'].update(target=(781, 469), blobs=2, locked=True, area=24, ms=4.1)
G['update_aim_context']()
line = "".join(t for t, _ in G['render_status']("Ctrl"))
check("есть режим, цель, пятна, залипание, задержка",
      all(s in line for s in ("[Ctrl]", "(781,469)", "24px", "2", "lock", "4.1")), f"\n      {line}")
check("назван способ движения", "SendInput" in line or "курсор" in line)
G['_stat']["target"] = None
check("без цели — 'ищу'", "ищу" in "".join(t for t, _ in G['render_status']("Alt")))

print("== приглашение ввода видно ==")
out = io.StringIO(); sys.stdout = out
sys.stdin = keyboard.EofFlagStdin("help\n")
G['console_loop']()
sys.stdout = real
check("console_loop печатает '> '", "> " in out.getvalue())

print("== edit укладывается в окно ==")
out = io.StringIO(); sys.stdout = out
G['print_settings_menu']()
sys.stdout = real
menu = out.getvalue()
longest = max(len(l) for l in menu.splitlines())
check("нет строк шире 80", longest <= 80, f"  (самая длинная {longest})")
check("описание под именем, а не в строку", "\n        отступ от центра" in menu)
check("группы заглавными", "ДЕТЕКЦИЯ" in menu and "ПРИЦЕЛ В ИГРЕ" in menu)
check("значения и границы на месте", "0.01..5.0" in menu and "auto|absolute|relative" in menu)

print("\nПРОВАЛОВ: %d" % len(fails))
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
