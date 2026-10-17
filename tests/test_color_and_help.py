"""status_hz, цвет и подробные объяснения."""

import sys, io, os, re, tempfile, ctypes
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

os.environ["COLUMNS"] = "78"; os.environ.pop("NO_COLOR", None)
keyboard.PRESSES['ctrl'] = 0; keyboard.EOF[0] = True
sys.stdin = io.StringIO('120\n')
G = {'__name__': '__main__', '__file__': 'aim_tracker.py'}
b = io.StringIO(); real = sys.stdout; sys.stdout = b
exec(compile(_SRC, 'aim_tracker.py', 'exec'), G)
sys.stdout = real
cfg = G['cfg']; ESC = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

def grab(fn, *a):
    out = io.StringIO(); keep = sys.stdout; sys.stdout = out
    try: fn(*a)
    finally: sys.stdout = keep
    return out.getvalue()

print("== VT включается, настройка color подчиняется ==")
check("VT разрешён", G['_vt']["ok"] is True)
cfg.color = "auto"
check("auto: не красит, когда вывод не в терминал", G['color_enabled']() is False)
cfg.color = "on"
check("on: красит через силу", G['color_enabled']() is True)
cfg.color = "off"
check("off: не красит никогда", G['color_enabled']() is False)
cfg.color = "on"
os.environ["NO_COLOR"] = "1"
check("NO_COLOR не отменяет явное 'on'", G['color_enabled']() is True)
cfg.color = "auto"
check("NO_COLOR выключает 'auto'", G['color_enabled']() is False)
del os.environ["NO_COLOR"]
ctypes.windll.kernel32.VT_OK[0] = False
check("консоль без ANSI -> enable_vt() False", G['enable_vt']() is False)
ctypes.windll.kernel32.VT_OK[0] = True

print("== цвет задержки привязан к бюджету кадра 16.6 мс ==")
budget = G['FRAME_BUDGET_MS']
check("бюджет = 60 Гц", abs(budget - 1000/60) < 0.01, f"  ({budget:.1f} мс)")
check("3 мс зелёный", G['latency_style'](3.0) == "green")
check("10 мс жёлтый", G['latency_style'](10.0) == "yellow")
check("17 мс красный", G['latency_style'](17.0) == "red")
check("ровно на границе бюджета — красный", G['latency_style'](budget) == "red")

print("== живая строка с цветом: обрезка по видимой ширине, коды не рвутся ==")
cfg.color = "on"
G['_stat'].update(target=(781, 469), blobs=2, locked=True, area=24, ms=4.1)
G['update_aim_context']()
os.environ["COLUMNS"] = "40"
text = grab(G['show_status'], G['render_status']("Ctrl"))
visible = ESC.sub("", text).replace("\r", "")
check("видимая часть не шире окна", len(visible) <= 40, f"  ({len(visible)})")
check("нет обрубленных escape", "\x1b[" not in ESC.sub("", text))
opens = len(re.findall(r"\x1b\[(?:36|1|94|32|33|31|90)m", text))
closes = text.count("\x1b[0m")
check("каждый открытый стиль закрыт", opens == closes, f"  (открыто {opens}, закрыто {closes})")
os.environ["COLUMNS"] = "78"
text = grab(G['show_status'], G['render_status']("Ctrl"))
check("стирание через ESC[K, а не пробелами", text.rstrip().endswith("\x1b[K"))

print("== status_hz управляет перерисовкой ==")
check("по умолчанию 12", cfg.status_hz == 12)
check("0 допустим", G['bounds_error']("status_hz", 0) is None)
check("отрицательное отбито", G['bounds_error']("status_hz", -1) is not None)
check("выше 60 отбито", G['bounds_error']("status_hz", 120) is not None)
G['apply_setting']("status_hz", "4")
check("set status_hz 4 применился", cfg.status_hz == 4)
check("целое, не float", type(cfg.status_hz) is int)
cfg.status_hz = 12

print("== выравнивание в edit не ломается цветом ==")
for mode in ("off", "on"):
    cfg.color = mode
    menu = ESC.sub("", grab(G['print_settings_menu']))
    lines = menu.splitlines()
    widest = max(len(l) for l in lines)
    # колонка значений должна быть одна и та же у всех строк, где бы она ни была
    wrong, value_columns = [], set()
    for name in vars(cfg):
        row = next((l for l in lines if l.startswith(f"    {name} ")
                    or l.rstrip() == f"    {name}"), None)
        if row is None:
            wrong.append((name, "строки нет")); continue
        want = G['format_value'](getattr(cfg, name))
        at = row.find(want, 4 + len(name))
        if at < 0:
            wrong.append((name, f"значения {want!r} нет в строке"))
        else:
            value_columns.add(at)
    check(f"color={mode}: у каждой настройки есть строка", not wrong, f"  {wrong[:3]}")
    check(f"color={mode}: колонка значений одна для всех",
          len(value_columns) == 1, f"  колонки: {sorted(value_columns)}")
    check(f"color={mode}: нет строк шире 80", widest <= 80, f"  ({widest})")
cfg.color = "off"

print("== подробные объяснения ==")
need = ["overlay_enabled", "aim_mode", "camera_gain", "lock_radius",
        "diff_threshold", "denoise_kernel", "status_hz", "color", "click_interval", "margin"]
missing = [n for n in need if n not in G['SETTINGS_HELP']]
check("разобраны настройки, которые сложно понять впервые", not missing, f"  {missing}")
check("все имена существуют в Config",
      all(hasattr(cfg, n) for n in G['SETTINGS_HELP']),
      f"  {[n for n in G['SETTINGS_HELP'] if not hasattr(cfg, n)]}")
out = grab(G['describe_setting'], "overlay_enabled")
check("'set <имя>' печатает длинный текст", "ВКЛ (рамка)" in out and "ВЫКЛ (быстрый)" in out)
check("и короткое описание тоже", "рамка оверлея" in out)
check("и подсказку, как менять", "изменить: set overlay_enabled" in out)

print("== описания есть у ВСЕХ настроек ==")
uncovered = [n for n in vars(cfg) if n not in G['SETTINGS_HELP']]
check("ни одна настройка не осталась без разбора", not uncovered, f"  {uncovered}")
no_short = [n for n in vars(cfg) if n not in G['SETTINGS_INFO']]
check("и без короткого описания тоже", not no_short, f"  {no_short}")
out = grab(G['describe_setting'], "нетакой")
check("неизвестное имя не падает", "Нет такой настройки" in out)

print("== все тексты укладываются в 80 колонок ==")
# мерим напечатанное, а не хранимое: перенос делается на выводе
def widest_of(fn, *a):
    lines = ESC.sub("", grab(fn, *a)).splitlines()
    return max((len(l), l) for l in lines) if lines else (0, "")

for cols in ("78", "60", "100"):
    os.environ["COLUMNS"] = cols
    worst = (0, "", "")
    for name in G['SETTINGS_HELP']:
        n, line = widest_of(G['describe_setting'], name)
        if n > worst[0]: worst = (n, line, f"set {name}")
    # 'профили' содержит выровненную по колонкам таблицу команд: переложить её
    # под узкое окно нельзя, не потеряв выравнивание — для неё предел 80
    limit = min(int(cols), 80) if int(cols) >= 78 else 80
    check(f"при окне {cols}: вывод укладывается в {limit}",
          worst[0] <= limit, f"  ({worst[0]} в '{worst[2]}': {worst[1][:40]!r})")

    prose = (0, "", "")
    for name in G['SETTINGS_HELP']:
        n, line = widest_of(G['describe_setting'], name)
        if n > prose[0]: prose = (n, line, f"set {name}")
    check(f"при окне {cols}: проза переложена под окно",
          prose[0] <= min(int(cols), 80), f"  ({prose[0]} в '{prose[2]}')")
os.environ["COLUMNS"] = "78"
flat = [l for t in G['SETTINGS_HELP'].values() for l in t.splitlines()]
check("нет хвостовых пробелов в текстах", not [l for l in flat if l != l.rstrip()])
out = grab(G['describe_setting'], "overlay_enabled")
check("длинный разбор переложен под окно",
      max(len(l) for l in ESC.sub("", out).splitlines()) <= 78)

print("\nПРОВАЛОВ: %d" % len(fails))
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
