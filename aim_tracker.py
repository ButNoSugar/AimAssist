import atexit
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import traceback

# ---------- Запуск без IDE ----------
# PyCharm запускает скрипт интерпретатором из venv, а двойной клик по файлу или
# `python aim_tracker.py` — системным, в котором модулей нет. Отсюда и выходило,
# что без IDE ничего не стартует. Скрипт сам находит venv рядом с собой и, если
# модулей не видно, передаёт себя его интерпретатору. Модули при этом должны
# быть уже установлены — просто находиться они будут сами.

BOOTSTRAP_MARKER = "AIMTRACKER_BOOTSTRAPPED"   # защита от бесконечного перезапуска
LAUNCHER_MARKER = "AIMTRACKER_LAUNCHER"        # выставляет run.bat, у него своя пауза
PYTHON_OVERRIDE = "AIMTRACKER_PYTHON"          # путь к интерпретатору вручную
VENV_DIRS = (".venv", "venv", "env", ".env")

SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)

_console_held = [False]


def hold_console(message="\nНажми Enter, чтобы закрыть окно..."):
    """Не даёт окну консоли закрыться, пока не нажмёшь Enter.

    Без этого двойной клик по файлу с ошибкой на старте выглядел так: окно
    мигнуло и исчезло, прочитать стектрейс невозможно.

    Из run.bat не спрашиваем — там своя pause, иначе Enter пришлось бы жать
    дважды. И не спрашиваем, когда консоли нет (запуск из теста, перенаправленный
    ввод): там ждать нажатия было бы вечным зависанием.
    """
    if _console_held[0] or os.environ.get(LAUNCHER_MARKER):
        return
    _console_held[0] = True
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return
        input(message)
    except (EOFError, KeyboardInterrupt, OSError, ValueError):
        pass


def _report_unhandled(exc_type, exc, tb):
    traceback.print_exception(exc_type, exc, tb)
    print("\n[Ошибка] Скрипт остановился из-за ошибки выше.")   # до настройки цвета
    hold_console()


def dependencies_present():
    try:
        import cv2, keyboard, mss, numpy, pyautogui  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


def find_venv_python():
    """Интерпретатор из venv рядом со скриптом, или заданный переменной среды."""
    override = os.environ.get(PYTHON_OVERRIDE, "").strip().strip('"')
    if override:
        return override if os.path.isfile(override) else None
    if os.name == "nt":
        parts = ("Scripts", "python.exe")       # pythonw.exe не берём: он без консоли
    else:
        parts = ("bin", "python3")
    for folder in VENV_DIRS:
        candidate = os.path.join(SCRIPT_DIR, folder, *parts)
        if os.path.isfile(candidate):
            return candidate
    return None


def explain_missing_dependencies():
    print("[Запуск] Не хватает модулей, и venv рядом со скриптом не найден.")
    print(f"         Скрипт лежит в: {SCRIPT_DIR}")
    print(f"         Текущий питон:  {sys.executable}")
    print("\n  Создать окружение и поставить модули (один раз, в этой папке):")
    if os.name == "nt":
        print("    py -m venv .venv")
        print("    .venv\\Scripts\\python -m pip install -r requirements.txt")
        print("\n  Дальше запускать run.bat — он сам найдёт .venv.")
    else:
        print("    python3 -m venv .venv")
        print("    .venv/bin/python -m pip install -r requirements.txt")
    print("\n  Если venv уже есть в другом месте, укажи путь к его питону:")
    print(f"    set {PYTHON_OVERRIDE}=C:\\путь\\к\\venv\\Scripts\\python.exe")


def bootstrap_dependencies():
    """Перезапускает скрипт питоном из venv, если в текущем модулей нет."""
    if dependencies_present():
        return
    if os.environ.get(BOOTSTRAP_MARKER):
        # уже перезапускались — значит и в venv модулей нет, второй круг не поможет
        print("[Запуск] Даже в venv не хватает модулей.")
        explain_missing_dependencies()
        hold_console()
        raise SystemExit(1)

    interpreter = find_venv_python()
    if interpreter is None:
        explain_missing_dependencies()
        hold_console()
        raise SystemExit(1)

    print(f"[Запуск] В этом питоне модулей нет, перезапускаюсь через:\n         {interpreter}")
    environment = dict(os.environ, **{BOOTSTRAP_MARKER: "1"})
    # subprocess, а не os.execv: на Windows execv отпускает родителя сразу, и
    # консоль (вместе с pause в run.bat) успевала закрыться под живым процессом
    completed = subprocess.run([interpreter, SCRIPT_PATH] + sys.argv[1:], env=environment)
    raise SystemExit(completed.returncode)


sys.excepthook = _report_unhandled
atexit.register(hold_console)
bootstrap_dependencies()

# Импорты модулей — после бутстрапа: до него их может просто не быть видно.
import cv2
import keyboard
import mss
import numpy as np
import pyautogui
import tkinter as tk
from PIL import Image


# ---------- Вывод в консоль ----------
# Живая строка состояния пишется поверх себя через \r, и любой обычный print
# её бы разрезал. Поэтому print в этом модуле намеренно затенён: так все
# существующие вызовы автоматически сначала убирают строку состояния, и не
# пришлось переписывать шесть десятков мест.

# ---------- Цвет ----------
# Консоль Windows по умолчанию не разбирает ANSI-последовательности: их надо
# разрешить вручную через SetConsoleMode. Делается один раз при старте, после
# чего цвет ничего не стоит — живая строка перерисовывается 12 раз в секунду,
# и лишние 40 байт на такой частоте не значат ничего.

STD_OUTPUT_HANDLE = -11
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

SGR = {
    "dim": "90", "bold": "1", "cyan": "36", "green": "32",
    "yellow": "33", "red": "31", "blue": "94",
}

FRAME_BUDGET_MS = 1000.0 / 60.0   # 60 Гц: за этим порогом реакция не успевает к кадру

_vt = {"ok": False}


def enable_vt():
    """Разрешает консоли разбирать ANSI. False — не вышло, цвета не будет."""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        return bool(kernel32.SetConsoleMode(
            handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:         # noqa: BLE001 — на любой осечке просто живём без цвета
        return False


def color_enabled():
    """Красить ли вывод прямо сейчас.

    'auto' отключает цвет там, где он превратился бы в мусор вида ←[32m:
    консоль не умеет ANSI, вывод перенаправлен в файл, или пользователь
    попросил через переменную NO_COLOR (общепринятое соглашение).
    'on' красит через силу — на случай, если терминал ANSI умеет, а наше
    определение этого не увидело.
    """
    if cfg.color == "off":
        return False
    if cfg.color == "on":
        return True
    if os.environ.get("NO_COLOR"):
        return False
    try:
        if not sys.stdout.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    return _vt["ok"]


def paint(text, style=None):
    if style is None or not color_enabled():
        return text
    return f"\x1b[{SGR[style]}m{text}\x1b[0m"


def latency_style(ms):
    """Цвет задержки относительно бюджета кадра: зелёный — с запасом,
    жёлтый — ещё успеваем, красный — в кадр уже не уложились."""
    if ms < FRAME_BUDGET_MS / 3:
        return "green"
    if ms < FRAME_BUDGET_MS:
        return "yellow"
    return "red"


_real_print = print          # до def print ниже это ещё встроенный print
_out_lock = threading.Lock()
_status = {"width": 0, "shown": False}


def console_width():
    return shutil.get_terminal_size((80, 25)).columns


def _clear_status_locked():
    if not _status["shown"]:
        return
    if _vt["ok"]:
        _real_print("\r\x1b[K", end="", flush=True)   # короче, чем простыня пробелов
    else:
        _real_print("\r" + " " * _status["width"] + "\r", end="", flush=True)
    _status["shown"] = False


def print(*args, **kwargs):          # noqa: A001 — затенение осознанное, см. выше
    with _out_lock:
        _clear_status_locked()
        _real_print(*args, **kwargs)


def clear_status():
    with _out_lock:
        _clear_status_locked()


def show_status(segments):
    """Пишет строку состояния поверх предыдущей.

    Принимает не готовую строку, а куски (текст, стиль). Иначе обрезка по
    ширине окна считала бы длину вместе с невидимыми ANSI-кодами и могла
    разрубить последовательность посередине — на экран полез бы мусор.
    А обрезать надо: строка длиннее окна перенеслась бы на вторую, и \r
    возвращал бы курсор уже не туда — вместо живой строки вышла бы лестница.
    """
    limit = max(20, console_width() - 1)
    pieces, visible = [], 0
    for text, style in segments:
        if visible >= limit:
            break
        text = text[:limit - visible]
        visible += len(text)
        pieces.append(paint(text, style))
    body = "".join(pieces)

    with _out_lock:
        if _vt["ok"]:
            _real_print("\r" + body + "\x1b[K", end="", flush=True)
        else:
            _real_print("\r" + body + " " * max(0, _status["width"] - visible),
                        end="", flush=True)
        _status["width"] = visible
        _status["shown"] = True


pyautogui.FAILSAFE = True  # двинь мышь в угол экрана, чтобы аварийно остановить pyautogui
pyautogui.PAUSE = 0  # ВАЖНО: по умолчанию pyautogui сам добавляет 0.1с паузы после КАЖДОГО
                     # вызова (moveTo, click...) — это и было основным источником задержки

TRACK_KEY = "ctrl"
FULLSCREEN_KEY = "alt"
AUTOCLICK_TOGGLE_KEY = "f6"
OVERLAY_TOGGLE_KEY = "f7"
PANIC_KEY = "f8"

PRIMARY_COLOR = "#00FF00"
SECONDARY_COLOR = "#FFA500"
CROP_PADDING = 15
MAX_BLOBS_SHOWN = 24
FAILSAFE_MARGIN = 2  # на столько пикселей держимся подальше от углов экрана (см. clamp_to_safe_area)

IS_WINDOWS = sys.platform == "win32"


def require_windows():
    """Останавливает запуск на чужой ОС с внятным объяснением.

    Раньше на Linux/macOS первым падал tk.Tk() внутри потока оверлея: в
    консоль сыпался стектрейс, поток умирал, а главный поток навсегда вис на
    _ready.wait(). Разобраться, что дело в платформе, по такой картине было
    невозможно.
    """
    if IS_WINDOWS:
        return
    print(f"[Платформа] Скрипт рассчитан на Windows, а запущен на {sys.platform!r}.")
    print("Что именно завязано на Windows:")
    print("  - прозрачный клик-сквозной оверлей: ctypes.windll.user32 и")
    print('    tkinter-атрибут "-transparentcolor" есть только в Windows;')
    print("  - глобальные хоткеи keyboard (Ctrl/Alt/F6/F7): на Linux нужен root,")
    print("    на macOS — разрешение на мониторинг ввода;")
    print("  - определение времени загрузки для бенчмарка (GetTickCount64).")
    print("Захват экрана (mss) и курсор (pyautogui) кроссплатформенны, так что")
    print("портирование возможно, но это отдельная работа, а не пара правок.")
    raise SystemExit(1)


# ---------- Настройки, изменяемые на лету через консоль ----------
class Config:
    def __init__(self):
        self.aim_mode = "auto"          # auto | absolute | relative (см. move_and_maybe_click)
        self.window_title = "Minecraft"  # ключевое слово в заголовке окна игры (пусто = не привязываться)
        self.camera_gain = 0.5          # доля ошибки, проходимая за кадр в относительном режиме
        self.margin = 100               # отступ от центра до края области слежения (режим Ctrl), px
        self.scan_interval = 0.005      # пауза между итерациями главного цикла (сек)
        self.diff_threshold = 30        # порог чувствительности разницы пикселей (0-255)
        self.min_blob_area = 4          # мин. площадь пятна в пикселях (отсекает шум)
        self.move_duration = 0.0        # плавность движения мыши (0 = мгновенно, быстрее всего)
        self.hide_settle_delay = 0.02   # пауза после скрытия рамок (только режим с оверлеем)
        self.denoise_kernel = 3         # размер ядра фильтра шума перед поиском пятен (0 = выкл)
        self.lock_radius = 120          # радиус залипания на цель в пикселях (0 = выкл)
        self.lock_timeout = 0.35        # через столько секунд без детекций цель забывается (сек)
        self.move_smoothing = 1.0       # доля пути до цели за кадр: 1.0 = мгновенно, 0.3 = плавно
        self.dead_zone = 0              # не двигать курсор, если цель ближе стольких пикселей
        self.debug_log = False          # подробный таймлог каждой детекции (основной вид — строка состояния)
        self.save_last_frame = False    # сохранять last.jpg (лишняя запись на диск = задержка)
        self.auto_click = False
        self.click_interval = 0.0       # минимальная пауза между автокликами, сек (0 = каждую детекцию)
        self.overlay_enabled = True     # True = точный, но медленный синхронный режим
                                         # False = быстрый режим с фоновым захватом экрана
        self.autosave = True            # сохранять активный профиль при выходе
        self.bench_on_boot = True       # прогонять бенчмарк один раз за загрузку компьютера
        self.pause_on_start = False      # ждать Enter после вывода настроек, перед слежением
        self.status_hz = 12             # как часто перерисовывать живую строку, раз/с (0 = не рисовать)
        self.color = "auto"             # auto | on | off


cfg = Config()


# ---------- Win32: относительный ввод мыши и привязка к окну игры ----------
# Зачем это нужно: pyautogui двигает курсор через SetCursorPos, а это просто
# запись позиции — она не проходит через очередь ввода, и игра, читающая
# raw input (Minecraft в их числе), её вообще не видит. Клик у pyautogui идёт
# через mouse_event, то есть настоящую инъекцию ввода, и до игры доходит.
# Отсюда и симптом: персонаж бьёт по воздуху, а камера стоит на месте.
# SendInput с MOUSEEVENTF_MOVE без флага ABSOLUTE — тоже инъекция, и приходит
# как относительное смещение, которого игра с захваченным курсором и ждёт.

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTUNION)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_sendinput_ready = [False]


def send_relative_mouse_move(dx, dy):
    """Сдвигает мышь на (dx, dy) отсчётов через SendInput."""
    if dx == 0 and dy == 0:
        return True
    user32 = ctypes.windll.user32
    if not _sendinput_ready[0]:
        # без argtypes на 64-битной Python указатель ужался бы до int
        user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
        user32.SendInput.restype = ctypes.c_uint
        _sendinput_ready[0] = True
    event = INPUT(type=INPUT_MOUSE)
    event.union.mi = MOUSEINPUT(dx=int(dx), dy=int(dy), mouseData=0,
                                dwFlags=MOUSEEVENTF_MOVE, time=0, dwExtraInfo=0)
    return user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) == 1


def window_text(hwnd):
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


TITLE_CACHE_TTL = 0.5
_window_cache = {"hwnd": 0, "title": "", "ts": 0.0}


def foreground_window_title():
    """(hwnd, заголовок) активного окна, с кэшем на заголовок.

    GetWindowTextW шлёт окну WM_GETTEXT, то есть синхронно лезет в чужой
    процесс и ждёт, пока тот разберёт очередь сообщений. В цикле наведения,
    который крутится сотни раз в секунду, занятая игра могла бы задержать
    такой вызов на целый кадр. Сам hwnd берём каждый раз — он дешёвый и
    сразу показывает переключение окна; заголовок перечитываем только когда
    окно сменилось или прошло TITLE_CACHE_TTL.
    """
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    now = time.perf_counter()
    if hwnd != _window_cache["hwnd"] or now - _window_cache["ts"] > TITLE_CACHE_TTL:
        _window_cache["hwnd"] = hwnd
        _window_cache["title"] = window_text(hwnd) if hwnd else ""
        _window_cache["ts"] = now
    return hwnd, _window_cache["title"]


def client_rect_on_screen(hwnd):
    """Клиентская область окна в экранных координатах: (left, top, w, h) или None.

    Именно клиентская, а не рамка окна: в неё не входят заголовок и границы,
    то есть это ровно тот прямоугольник, который игра рисует. Его центр и
    есть прицел — в оконном режиме он не совпадает с центром экрана.
    """
    user32 = ctypes.windll.user32
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return (origin.x, origin.y, width, height)


def clamp_region(left, top, width, height):
    """Прижимает область к экрану: mss не умеет снимать за его границами.

    Нужно с тех пор, как область слежения привязана к окну: окно у края
    экрана легко даёт прицел, вокруг которого квадрат margin вылезает наружу.
    """
    left = max(0, min(int(left), screen_width - 1))
    top = max(0, min(int(top), screen_height - 1))
    width = max(1, min(int(width), screen_width - left))
    height = max(1, min(int(height), screen_height - top))
    return left, top, width, height


# Обновляется раз за итерацию главного цикла, читается наводкой и логами.
_aim = {
    "crosshair": (0, 0),
    "relative": False,
    "last_delta": (0, 0),
    "last_match": None,     # (заголовок, область) последнего найденного окна игры
}


def update_aim_context():
    """Определяет точку прицела и способ движения на эту итерацию.

    Окно ищем только среди активных: целиться в неактивную игру бессмысленно,
    а перебор всех окон давал бы ложные совпадения — браузер со словом
    Minecraft в заголовке, папка с таким же именем.

    Возвращает область игры или None (обычный рабочий стол).
    """
    viewport = None
    needle = cfg.window_title.strip().lower()   # ключевое слово, а не весь заголовок
    if needle:
        hwnd, title = foreground_window_title()
        if hwnd and needle in title.lower():
            viewport = client_rect_on_screen(hwnd)
            if viewport is not None:
                _aim["last_match"] = (title, viewport)

    if viewport is None:
        _aim["crosshair"] = (center_x, center_y)
        _aim["relative"] = cfg.aim_mode == "relative"
    else:
        left, top, width, height = viewport
        _aim["crosshair"] = (left + width // 2, top + height // 2)
        _aim["relative"] = cfg.aim_mode in ("auto", "relative")
    return viewport



# ---------- Оверлей (используется только когда cfg.overlay_enabled=True) ----------
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020


class Overlay:
    """
    Полноэкранное прозрачное окно с рамками вокруг найденных изменений.
    Клики проходят насквозь. Только Windows.

    hide_all_and_wait()/show_boxes_and_wait() — синхронные: ждут, пока
    поток tkinter реально перерисует кадр, прежде чем вернуть управление.
    Это нужно только в режиме с оверлеем, чтобы сама рамка не попадала
    в diff и не начинала расти по кругу.
    """

    def __init__(self, screen_width, screen_height, max_rects=MAX_BLOBS_SHOWN, thickness=2):
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.max_rects = max_rects
        self.thickness = thickness
        self._ready = threading.Event()
        self._shown_lock = threading.Lock()
        self._boxes_shown = False
        self.error = None          # не None -> окно не поднялось, работаем без рамок
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self._ready.wait(timeout=10):
            self.error = RuntimeError("окно оверлея не поднялось за 10 секунд")

    def _run(self):
        try:
            self._build_window()
        except Exception as exc:      # noqa: BLE001 — причина уходит в self.error
            self.error = exc
            self._ready.set()         # иначе главный поток ждал бы вечно
            return
        self._ready.set()
        self.root.mainloop()

    def _build_window(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry(f"{self.screen_width}x{self.screen_height}+0+0")

        transparent_key = "#000001"
        self.root.config(bg=transparent_key)
        self.root.attributes("-transparentcolor", transparent_key)

        self.canvas = tk.Canvas(
            self.root, width=self.screen_width, height=self.screen_height,
            highlightthickness=0, bg=transparent_key,
        )
        self.canvas.pack()

        self.rect_ids = [
            self.canvas.create_rectangle(-10, -10, -10, -10, outline=SECONDARY_COLOR, width=self.thickness)
            for _ in range(self.max_rects)
        ]

        self.root.update_idletasks()
        self._make_click_through()

    def _make_click_through(self):
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        styles = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, styles | WS_EX_LAYERED | WS_EX_TRANSPARENT)

    def _run_synced(self, draw_fn):
        if self.error is not None:
            return
        done = threading.Event()

        def _job():
            draw_fn()
            self.root.update_idletasks()
            self.root.update()
            done.set()

        self.root.after(0, _job)
        done.wait(timeout=0.2)

    def hide_all_and_wait(self, force=False):
        """Прячет все рамки. force=True — безусловно.

        Главный цикл зовёт это каждые scan_interval, пока клавиша слежения не
        нажата, то есть сотни раз в секунду вхолостую: каждый вызов ставил
        задание в поток tkinter и ждал реальной перерисовки. Флаг убирает эту
        работу, когда прятать нечего.

        Но там, где скрытие обязательно — перед парой скриншотов в
        track_synced и при смене режима — зовём с force=True: пропущенное
        скрытие вернуло бы старый баг, когда рамка попадала в собственный
        скриншот и разрасталась по кругу.
        """
        with self._shown_lock:
            if not force and not self._boxes_shown:
                return
            self._boxes_shown = False

        def _hide():
            for rect_id in self.rect_ids:
                self.canvas.coords(rect_id, -10, -10, -10, -10)
        self._run_synced(_hide)

    def show_boxes_and_wait(self, boxes, primary_index=0):
        def _draw():
            for i, rect_id in enumerate(self.rect_ids):
                if i < len(boxes):
                    x1, y1, x2, y2 = boxes[i]
                    color = PRIMARY_COLOR if i == primary_index else SECONDARY_COLOR
                    self.canvas.coords(rect_id, x1, y1, x2, y2)
                    self.canvas.itemconfig(rect_id, outline=color)
                else:
                    self.canvas.coords(rect_id, -10, -10, -10, -10)
        self._run_synced(_draw)
        with self._shown_lock:
            self._boxes_shown = bool(boxes)


# ---------- Фоновый непрерывный захват экрана (используется когда оверлей выключен) ----------
class FrameGrabber:
    """
    Крутится в отдельном потоке и без остановки грабит весь экран через mss,
    складывая последний кадр в self.latest. Главный цикл просто читает уже
    готовые кадры вместо того, чтобы блокирующе ждать новый скриншот —
    это и даёт основной прирост скорости в режиме без оверлея.

    Снимает ровно ту область, которую просит режим слежения: в режиме Ctrl это
    квадрат вокруг центра, а не весь экран. Полный кадр 1680x1050 — это 7 МБ,
    которые копировались на каждом захвате только чтобы вырезать из них
    середину.

    ВАЖНО: занимает одно ядро CPU практически полностью, пока запущен.
    Поэтому стартует только когда оверлей выключен, и останавливается,
    когда его включают обратно.
    """

    def __init__(self, width, height):
        self.region = (0, 0, width, height)
        self.lock = threading.Lock()
        self.latest = None
        self.latest_ts = 0.0
        self.capture_time_ms = 0.0
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def set_region(self, region):
        """Переключает снимаемую область. Кадры от прежней сразу выбрасываются."""
        with self.lock:
            if region == self.region:
                return
            self.region = region
            self.latest = None
            self.latest_ts = 0.0

    def _run(self):
        with mss.MSS() as sct:
            while self._running:
                with self.lock:
                    region = self.region
                left, top, width, height = region
                t0 = time.perf_counter()
                shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
                arr = np.array(shot, dtype=np.uint8)
                t1 = time.perf_counter()
                with self.lock:
                    if self.region != region:
                        continue          # область сменили, пока снимали — кадр уже не нужен
                    self.latest = arr
                    self.latest_ts = t1
                    self.capture_time_ms = (t1 - t0) * 1000

    def get_latest(self, region):
        """Последний кадр, но только если он снят именно с этой области.

        Без проверки кадр от прежней области ушёл бы в детектор вместе с
        новыми смещениями, и курсор поехал бы не туда.
        """
        with self.lock:
            if self.region != region or self.latest is None:
                return None, 0.0
            return self.latest, self.latest_ts


# ---------- Инициализация ----------
require_windows()

screen_width, screen_height = pyautogui.size()
center_x, center_y = screen_width // 2, screen_height // 2
overlay = Overlay(screen_width, screen_height)
if overlay.error is not None:
    # само по себе не смертельно: быстрый режим рамок и не рисует
    print(f"[Оверлей] Окно рамок не поднялось: {overlay.error}")
    print("[Оверлей] Работаю без рамок. Настройка overlay/F7 включить его не сможет.")
    cfg.overlay_enabled = False
grabber = FrameGrabber(screen_width, screen_height)
main_sct = mss.MSS()  # для синхронного режима (используется только из главного потока)

_stop = threading.Event()
_prev_holder = {"frame": None}
_last_used_ts = 0.0

_lock = {"pos": None, "ts": 0.0, "held": False}


def reset_lock():
    _lock["pos"] = None
    _lock["ts"] = 0.0
    _lock["held"] = False


def set_overlay_enabled(enabled):
    global _last_used_ts
    if enabled and overlay.error is not None:
        print(f"[Оверлей] Недоступен ({overlay.error}) — остаюсь в быстром режиме.")
        enabled = False
    cfg.overlay_enabled = enabled
    if enabled:
        grabber.stop()
    else:
        overlay.hide_all_and_wait(force=True)
        _prev_holder["frame"] = None
        _last_used_ts = 0.0
        grabber.start()
    reset_lock()
    print(paint("[Оверлей]", "cyan"),
          "ВКЛ — рамка рисуется, захват синхронный (точнее, медленнее)" if enabled
          else "ВЫКЛ — быстрый режим, кадры берутся из фонового потока")


def toggle_autoclick():
    cfg.auto_click = not cfg.auto_click
    print(paint("[Автоклик]", "cyan"), "включен" if cfg.auto_click else "выключен")


def panic_stop():
    """Аварийная остановка.

    В относительном режиме курсор по экрану не ездит, поэтому угловой failsafe
    у pyautogui там не сработает — эта клавиша его заменяет.
    """
    _stop.set()
    print(paint(f"\n[Стоп] Аварийная остановка по {PANIC_KEY.upper()}.", "red"))


keyboard.add_hotkey(AUTOCLICK_TOGGLE_KEY, toggle_autoclick)
keyboard.add_hotkey(PANIC_KEY, panic_stop)
keyboard.add_hotkey(OVERLAY_TOGGLE_KEY, lambda: set_overlay_enabled(not cfg.overlay_enabled))

def ask_margin():
    """Спрашивает отступ от центра до края области слежения в режиме Ctrl.

    Ограничен половиной меньшей стороны экрана: при большем значении регион
    вылезает за границы экрана, и mss возвращает кадр не того размера, который
    ожидает детектор. Раньше здесь был голый int(input(...)) — пустая строка
    или буква роняли скрипт на старте.
    """
    max_margin = MAX_VALUES["margin"]
    while True:
        try:
            raw = input(f"Введите отступ от центра экрана (для режима Ctrl), 1-{max_margin}: ").strip()
        except EOFError:
            raise SystemExit("Ввод прерван.")
        try:
            value = int(raw)
        except ValueError:
            print("Нужно целое число.")
            continue
        if not 1 <= value <= max_margin:
            print(f"Значение должно быть от 1 до {max_margin}.")
            continue
        return value




# ---------- Детекция ----------
def find_blobs_arr(arr1_bgra, arr2_bgra, threshold, min_area, denoise_kernel=0):
    """Ищет пятна изменений между двумя кадрами.

    Принимает сырые BGRA-кадры от mss как есть, без среза [:, :, :3]:
    трёхканальный срез BGRA-массива не упакован (между пикселями 4 байта при
    трёх каналах), и OpenCV вынужден копировать его целиком. Альфа у mss
    константная, поэтому absdiff по ней всегда 0 и на максимум не влияет.

    Разница считается через absdiff/max/threshold, то есть в uint8 и на SIMD.
    Прежний np.abs(a.astype(int16) - b.astype(int16)).max(axis=2) аллоцировал
    два int16-буфера размером с кадр и был на порядок дороже — на полном
    экране именно он, а не разметка, съедал основную часть времени детекции.
    Маска на выходе побитово та же, что и раньше.

    denoise_kernel > 0 применяет морфологическое "открытие" к маске перед
    разметкой связных областей — убирает единичные шумные пиксели ДО того,
    как cv2 начнёт размечать их по одному. Без этого сцена с постоянным
    мелким шумом (анимация воды, рука с предметом и т.п.) может давать
    сотни-тысячи крошечных пятен, и сама разметка становится дорогой —
    именно это было причиной скачков diff до 75-100мс.
    """
    diff = cv2.absdiff(arr1_bgra, arr2_bgra)
    b, g, r = cv2.split(diff)[:3]
    merged = cv2.max(cv2.max(b, g), r)
    _, mask = cv2.threshold(merged, threshold, 1, cv2.THRESH_BINARY)
    if denoise_kernel > 0:
        kernel = np.ones((denoise_kernel, denoise_kernel), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    blobs = []
    for label_id in range(1, num_labels):
        x, y, w, h, area = stats[label_id]
        if area < min_area:
            continue
        blobs.append({
            "bbox": (int(x), int(y), int(x + w), int(y + h)),
            "area": int(area),
            "center": (int(x + w // 2), int(y + h // 2)),
        })
    return blobs


def clamp_to_safe_area(x, y):
    """Отодвигает точку от углов экрана.

    pyautogui.FAILSAFE срабатывает, когда курсор оказывается ровно в одном из
    четырёх углов, и бросает FailSafeException. В режиме Alt (весь экран) пятно
    у края экрана легко даёт такие координаты — и скрипт падал посреди работы.
    Углы остаются пользователю как аварийный тормоз, сами мы туда не ходим.
    """
    x = min(max(int(x), FAILSAFE_MARGIN), screen_width - 1 - FAILSAFE_MARGIN)
    y = min(max(int(y), FAILSAFE_MARGIN), screen_height - 1 - FAILSAFE_MARGIN)
    return x, y


def select_target(blobs, offset_x, offset_y):
    """Выбирает пятно, за которым следим, и переставляет его в начало списка.

    Без залипания целью каждый кадр становится просто самое крупное пятно, и
    курсор прыгает между разными пятнами, как только их площади меняются
    местами — на записи это выглядит как дёрганье. Если предыдущая цель ещё
    "жива" (с последней детекции прошло меньше lock_timeout) и в радиусе
    lock_radius от неё есть пятно, держимся за ближайшее к ней, а не за
    самое большое.

    Пятно-цель ставится в blobs[0], поэтому вся логика ниже по коду
    (зелёная рамка, срез MAX_BLOBS_SHOWN) продолжает работать как раньше.
    """
    blobs.sort(key=lambda b: b["area"], reverse=True)
    chosen = 0
    prev = _lock["pos"]
    now = time.perf_counter()

    if prev is not None and cfg.lock_radius > 0 and now - _lock["ts"] <= cfg.lock_timeout:
        px, py = prev
        best_d2 = cfg.lock_radius * cfg.lock_radius
        for i, b in enumerate(blobs):
            dx = offset_x + b["center"][0] - px
            dy = offset_y + b["center"][1] - py
            d2 = dx * dx + dy * dy
            if d2 <= best_d2:
                best_d2 = d2
                chosen = i

    if chosen:
        blobs.insert(0, blobs.pop(chosen))

    primary = blobs[0]
    _lock["pos"] = (offset_x + primary["center"][0], offset_y + primary["center"][1])
    _lock["ts"] = now
    _lock["held"] = bool(chosen)
    return primary


# Что показывает живая строка. Заполняется детекцией, читается главным циклом.
_stat = {"target": None, "blobs": 0, "locked": False, "area": 0, "ms": 0.0}

def render_status(key_name):
    """Собирает живую строку как куски (текст, стиль).

    Задержка красится по бюджету кадра, залипание на цель — отдельно: это два
    числа, на которые смотрят в первую очередь, когда подбирают настройки.
    """
    way = "SendInput" if _aim["relative"] else "курсор"
    if _stat["target"] is None:
        return [(f"[{key_name}]", "cyan"), (" ищу цель…   ", None),
                (f"прицел {_aim['crosshair']}   ", "dim"), (way, "dim")]
    x, y = _stat["target"]
    parts = [
        (f"[{key_name}]", "cyan"),
        (f" цель ({x},{y})", "bold"),
        (f"  area {_stat['area']}px  пятен {_stat['blobs']}", None),
    ]
    if _stat["locked"]:
        parts.append((" lock", "blue"))
    parts.append(("  ", None))
    parts.append((f"{_stat['ms']:.1f} мс", latency_style(_stat["ms"])))
    parts.append((f"  {way}", "dim"))
    return parts


_move_remainder = {"x": 0.0, "y": 0.0}


def move_absolute_to_target(x, y):
    """Ставит курсор в точку экрана — прежний способ, для рабочего стола.

    move_smoothing < 1.0 — экспоненциальное сглаживание: за кадр проходим
    только эту долю пути до цели, и курсор подъезжает за несколько кадров
    вместо телепорта. dead_zone гасит микродёрганье на один-два пикселя.

    В игре, захватившей курсор, этот способ не работает: SetCursorPos до неё
    не доходит (см. send_relative_mouse_move).
    """
    if cfg.move_smoothing < 1.0 or cfg.dead_zone > 0:
        cur_x, cur_y = pyautogui.position()
        dx, dy = x - cur_x, y - cur_y
        if dx * dx + dy * dy <= cfg.dead_zone * cfg.dead_zone:
            return
        if cfg.move_smoothing < 1.0:
            x = cur_x + dx * cfg.move_smoothing
            y = cur_y + dy * cfg.move_smoothing

    x, y = clamp_to_safe_area(x, y)
    pyautogui.moveTo(x, y, duration=cfg.move_duration)


def move_relative_to_target(x, y):
    """Доворачивает камеру к цели относительным смещением мыши.

    Игра не сообщает, куда смотрит камера, поэтому это замкнутый контур: за
    кадр сдвигаем мышь на долю ошибки, снимаем экран заново и повторяем, пока
    цель не придёт в прицел. Благодаря этому точная калибровка не нужна —
    достаточно camera_gain меньше единицы, и контур сойдётся сам. Слишком
    большой gain даёт перелёт и раскачку.

    Пиксели ошибки и "отсчёты" мыши — разные величины: коэффициент между ними
    зависит от чувствительности и FOV в игре, и подбирается как раз
    camera_gain.
    """
    cross_x, cross_y = _aim["crosshair"]
    error_x, error_y = x - cross_x, y - cross_y
    if error_x * error_x + error_y * error_y <= cfg.dead_zone * cfg.dead_zone:
        _move_remainder["x"] = _move_remainder["y"] = 0.0
        _aim["last_delta"] = (0, 0)
        return

    # SendInput принимает только целые отсчёты, а дробный остаток копим между
    # кадрами: иначе при небольшой ошибке и gain<1 смещение каждый раз
    # округлялось бы в ноль, и наводка замирала бы, чуть не дойдя до цели.
    step_x = error_x * cfg.camera_gain + _move_remainder["x"]
    step_y = error_y * cfg.camera_gain + _move_remainder["y"]
    dx, dy = int(step_x), int(step_y)
    _move_remainder["x"] = step_x - dx
    _move_remainder["y"] = step_y - dy
    _aim["last_delta"] = (dx, dy)
    send_relative_mouse_move(dx, dy)


_last_click = {"ts": 0.0}


def click_is_due():
    """Пора ли кликать с учётом click_interval.

    Детекция идёт сотнями раз в секунду, и автоклик без паузы столько же раз
    и жал. В игре это почти всегда вхолостую: у атаки своя перезарядка, и
    лишние нажатия ничего не добавляют.
    """
    if cfg.click_interval <= 0:
        return True
    now = time.perf_counter()
    if now - _last_click["ts"] < cfg.click_interval:
        return False
    _last_click["ts"] = now
    return True


def move_and_maybe_click(x, y):
    """Наводит на цель (x, y) в экранных координатах и, если включено, кликает."""
    if _aim["relative"]:
        move_relative_to_target(x, y)
    else:
        move_absolute_to_target(x, y)
    if cfg.auto_click and click_is_due():
        pyautogui.click()


def aim_log_suffix():
    if not _aim["relative"]:
        return ""
    dx, dy = _aim["last_delta"]
    return f" | SendInput dx={dx} dy={dy}"


def maybe_save_last_frame(arr_bgr, bbox, region_w, region_h):
    if not cfg.save_last_frame:
        return
    x1, y1, x2, y2 = bbox
    crop = arr_bgr[
        max(y1 - CROP_PADDING, 0):min(y2 + CROP_PADDING, region_h),
        max(x1 - CROP_PADDING, 0):min(x2 + CROP_PADDING, region_w),
    ]
    Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGRA2RGB)).save("last.jpg")  # BGRA -> RGB для PIL


def track_synced(region_left, region_top, region_w, region_h, offset_x, offset_y):
    """Точный, но медленный режим: рамка синхронно прячется/показывается вокруг захвата."""
    overlay.hide_all_and_wait(force=True)
    time.sleep(cfg.hide_settle_delay)

    region = {"left": region_left, "top": region_top, "width": region_w, "height": region_h}
    t0 = time.perf_counter()
    arr1 = np.array(main_sct.grab(region), dtype=np.uint8)   # BGRA как есть, см. find_blobs_arr
    arr2 = np.array(main_sct.grab(region), dtype=np.uint8)
    t1 = time.perf_counter()

    blobs = find_blobs_arr(arr1, arr2, cfg.diff_threshold, cfg.min_blob_area, cfg.denoise_kernel)
    t2 = time.perf_counter()

    if not blobs:
        return False

    primary = select_target(blobs, offset_x, offset_y)
    screen_boxes = [
        (offset_x + x1, offset_y + y1, offset_x + x2, offset_y + y2)
        for (x1, y1, x2, y2) in (b["bbox"] for b in blobs[:MAX_BLOBS_SHOWN])
    ]
    overlay.show_boxes_and_wait(screen_boxes, primary_index=0)
    t3 = time.perf_counter()

    local_cx, local_cy = primary["center"]
    screen_cx, screen_cy = offset_x + local_cx, offset_y + local_cy
    maybe_save_last_frame(arr2, primary["bbox"], region_w, region_h)
    move_and_maybe_click(screen_cx, screen_cy)
    t4 = time.perf_counter()

    _stat.update(target=(screen_cx, screen_cy), blobs=len(blobs), locked=_lock["held"],
                 area=primary["area"], ms=(t4 - t0) * 1000)

    if cfg.debug_log:
        print(
            f"[sync] пятен={len(blobs)}{' [lock]' if _lock['held'] else ''} "
            f"area={primary['area']}px -> ({screen_cx},{screen_cy}) | "
            f"захват={((t1 - t0) * 1000):.1f}мс diff={((t2 - t1) * 1000):.1f}мс "
            f"оверлей={((t3 - t2) * 1000):.1f}мс move={((t4 - t3) * 1000):.1f}мс "
            f"| итого={((t4 - t0) * 1000):.1f}мс" + aim_log_suffix()
        )
    return True


def track_fast(region_left, region_top, region_w, region_h, offset_x, offset_y):
    """Быстрый режим: без оверлея, кадры уже лежат готовые от фонового FrameGrabber."""
    global _last_used_ts

    region = (region_left, region_top, region_w, region_h)
    grabber.set_region(region)                 # в режиме Ctrl снимаем только квадрат у центра
    current, ts_now = grabber.get_latest(region)
    if current is None or ts_now == _last_used_ts:
        return False  # фоновый поток ещё не успел снять новый кадр этой области

    prev = _prev_holder["frame"]
    _prev_holder["frame"] = current
    _last_used_ts = ts_now
    if prev is None or prev.shape != current.shape:
        return False

    # грабер отдал ровно нужную область — вырезать из кадра больше нечего
    t0 = time.perf_counter()
    blobs = find_blobs_arr(prev, current, cfg.diff_threshold, cfg.min_blob_area, cfg.denoise_kernel)
    t1 = time.perf_counter()

    if not blobs:
        return False

    primary = select_target(blobs, offset_x, offset_y)
    local_cx, local_cy = primary["center"]
    screen_cx, screen_cy = offset_x + local_cx, offset_y + local_cy
    maybe_save_last_frame(current, primary["bbox"], region_w, region_h)
    move_and_maybe_click(screen_cx, screen_cy)
    t2 = time.perf_counter()

    _stat.update(target=(screen_cx, screen_cy), blobs=len(blobs), locked=_lock["held"],
                 area=primary["area"], ms=(t2 - t0) * 1000)

    if cfg.debug_log:
        print(
            f"[fast] пятен={len(blobs)}{' [lock]' if _lock['held'] else ''} "
            f"area={primary['area']}px -> ({screen_cx},{screen_cy}) | "
            f"diff={((t1 - t0) * 1000):.1f}мс move={((t2 - t1) * 1000):.1f}мс "
            f"| захват фоном ~{grabber.capture_time_ms:.1f}мс/кадр" + aim_log_suffix()
        )
    return True


def track(region_left, region_top, region_w, region_h, offset_x, offset_y):
    if cfg.overlay_enabled:
        return track_synced(region_left, region_top, region_w, region_h, offset_x, offset_y)
    return track_fast(region_left, region_top, region_w, region_h, offset_x, offset_y)


# ---------- Бенчмарк ----------
def dummy_frame_pair(h, w):
    """Пара BGRA-кадров, похожая на реальную сцену.

    Прежний бенчмарк брал два полностью случайных кадра: там отличался каждый
    пиксель, маска выходила сплошной, связная область получалась ровно одна, и
    разметка оказывалась подозрительно дешёвой — мерилось не то, что тормозит
    в жизни. Здесь кадры почти одинаковые, с редким точечным шумом (вода, рука
    с предметом) и несколькими настоящими пятнами — как раз тот случай, ради
    которого добавлялся denoise_kernel.
    """
    rng = np.random.default_rng(0)
    first = np.empty((h, w, 4), np.uint8)
    first[:, :, :3] = rng.integers(50, 70, (h, w, 3), dtype=np.uint8)
    first[:, :, 3] = 255
    second = first.copy()
    second[rng.random((h, w)) < 0.002, :3] = 255              # точечный шум
    for _ in range(6):                                        # настоящие пятна
        y = int(rng.integers(0, max(h - 12, 1)))
        x = int(rng.integers(0, max(w - 12, 1)))
        second[y:y + 10, x:x + 10, :3] = 255
    return first, second


def run_benchmark(samples=30):
    """Меряет захват и детекцию. Печатает результат и возвращает его текстом,
    чтобы конфиг мог показать его при следующем запуске, не меряя заново."""
    print("Замер производительности (может занять секунду)...")
    margin = int(cfg.margin)
    with mss.MSS() as sct:
        full_region = {"left": 0, "top": 0, "width": screen_width, "height": screen_height}
        small_region = {"left": center_x - margin, "top": center_y - margin, "width": margin * 2, "height": margin * 2}

        t = []
        for _ in range(samples):
            t0 = time.perf_counter(); sct.grab(full_region); t.append((time.perf_counter() - t0) * 1000)
        full_avg = sum(t) / samples

        t = []
        for _ in range(samples):
            t0 = time.perf_counter(); sct.grab(small_region); t.append((time.perf_counter() - t0) * 1000)
        small_avg = sum(t) / samples

    def time_diff(h, w):
        a, b = dummy_frame_pair(h, w)
        find_blobs_arr(a, b, cfg.diff_threshold, cfg.min_blob_area, cfg.denoise_kernel)  # прогрев
        t0 = time.perf_counter()
        for _ in range(samples):
            find_blobs_arr(a, b, cfg.diff_threshold, cfg.min_blob_area, cfg.denoise_kernel)
        return (time.perf_counter() - t0) / samples * 1000

    diff_small = time_diff(margin * 2, margin * 2)
    diff_full = time_diff(screen_height, screen_width)

    text = "\n".join([
        f"  Захват всего экрана ({screen_width}x{screen_height}): {full_avg:.1f} мс",
        f"  Захват области {margin*2}x{margin*2} (режим Ctrl):    {small_avg:.1f} мс",
        f"  Поиск пятен на области {margin*2}x{margin*2} (режим Ctrl): {diff_small:.1f} мс",
        f"  Поиск пятен на всём экране (режим Alt):        {diff_full:.1f} мс",
        "  Это ориентировочный потолок скорости на твоём железе для этих операций.",
    ])
    print(text)
    return text


# ---------- Консольное управление настройками ----------
def print_help():
    print(
        "\nКоманды консоли:\n"
        "  edit                         — меню всех переменных: значения, границы, описания\n"
        "  show                         — то же самое, но коротким списком\n"
        "  set <имя> <значение>         — изменить переменную\n"
        "  set <имя>                    — показать одну переменную с описанием\n"
        "  log on|off                   — подробный лог каждой детекции (по умолчанию выкл)\n"
        "  overlay on|off               — вкл/выкл рамку (F7 делает то же самое)\n"
        "  click on|off                 — вкл/выкл автоклик (F6 делает то же самое)\n"
        "  save on|off                  — сохранять last.jpg при детекции (тратит время!)\n"
        "  window                       — к какому окну привязан прицел и как двигаем мышь\n"
        "  bench                        — замерить скорость захвата/детекции сейчас\n"
        "  profiles                     — страница профилей: список и команды\n"
        "  profile save|load|delete|default|defaults\n"
        "  reset_config                 — стереть файл конфига (спросит подтверждение)\n"
        "  help                         — эта справка\n"
        "\nПодробные объяснения:\n"
        "  help <тема>                  — " + ", ".join(HELP_TOPICS) + "\n"
        "  set <имя>                    — разбор одной настройки целиком\n"
    )


BOOL_TRUE = {"1", "on", "true", "yes", "да"}
BOOL_FALSE = {"0", "off", "false", "no", "нет"}

# нижние границы для настроек, которые иначе уронят cv2 (отрицательное ядро и т.п.)
MIN_VALUES = {
    "scan_interval": 0.0,
    "diff_threshold": 0,
    "min_blob_area": 1,
    "move_duration": 0.0,
    "hide_settle_delay": 0.0,
    "denoise_kernel": 0,
    "lock_radius": 0,
    "lock_timeout": 0.0,
    "move_smoothing": 0.01,   # 0 полностью заморозило бы курсор
    "dead_zone": 0,
    "margin": 1,
    "camera_gain": 0.01,
    "click_interval": 0.0,
    "status_hz": 0,
}

# значения-строки: что вообще можно вписать
ALLOWED_VALUES = {
    "aim_mode": ("auto", "absolute", "relative"),
    "color": ("auto", "on", "off"),
}

# верхние границы (нужны только там, где значение — доля)
MAX_VALUES = {
    "move_smoothing": 1.0,
    "margin": min(center_x, center_y),   # больше — область слежения вылезет за край экрана
    "camera_gain": 5.0,
    "status_hz": 60,
}


def parse_bool(raw):
    low = raw.lower()
    if low in BOOL_TRUE:
        return True
    if low in BOOL_FALSE:
        return False
    raise ValueError(f"ожидалось on/off, а не {raw!r}")


def coerce_setting(name, raw):
    """Приводит значение к типу текущей настройки. Бросает ValueError.

    Раньше в 'set' стоял безусловный float(): 'set denoise_kernel 3' записывал
    3.0, и np.ones((3.0, 3.0)) падал с TypeError прямо в цикле детекции.
    Тем же путём идут значения из файла конфига — его правят руками, так что
    доверия к нему не больше, чем к вводу с клавиатуры.
    """
    current = getattr(cfg, name)
    if isinstance(current, bool):          # bool наследуется от int — проверяем его первым
        return parse_bool(raw) if isinstance(raw, str) else bool(raw)
    if isinstance(current, str):
        value = str(raw).strip()
        allowed = ALLOWED_VALUES.get(name)
        if allowed is None:
            return value                   # свободный текст, например заголовок окна
        if value.lower() not in allowed:
            raise ValueError(f"допустимо: {', '.join(allowed)}")
        return value.lower()
    if isinstance(current, int):
        return int(float(raw))
    return float(raw)


def bounds_error(name, value):
    """Текст ошибки, если значение вне допустимого диапазона, иначе None."""
    if name in MIN_VALUES and value < MIN_VALUES[name]:
        return f"{name} не может быть меньше {MIN_VALUES[name]}"
    if name in MAX_VALUES and value > MAX_VALUES[name]:
        return f"{name} не может быть больше {MAX_VALUES[name]}"
    return None


def assign_setting(name, value):
    """Кладёт проверенное значение в cfg.

    overlay_enabled идёт только через сеттер: он останавливает и запускает
    фоновый грабер, и запись напрямую развела бы режимы между собой.
    """
    if name == "overlay_enabled":
        set_overlay_enabled(value)
    else:
        setattr(cfg, name, value)


def apply_setting(name, raw):
    if not hasattr(cfg, name):
        print(f"Нет такой настройки: {name}. Введи 'edit' для списка с описаниями.")
        return
    try:
        value = coerce_setting(name, raw)
    except ValueError as exc:
        print(f"Не могу разобрать значение для {name}: {exc}")
        return
    error = bounds_error(name, value)
    if error:
        print(error)
        return
    assign_setting(name, value)
    if name != "overlay_enabled":          # сеттер печатает своё сообщение
        print(f"{name} = {format_value(value)}")


# Описания переменных для меню 'edit'. Порядок групп — порядок вывода.
SETTINGS_GROUPS = [
    ("Детекция", (
        ("margin",            "отступ от центра до края области слежения в режиме Ctrl, px"),
        ("diff_threshold",    "порог разницы пикселей 0-255: ниже — чувствительнее, но больше шума"),
        ("min_blob_area",     "минимальная площадь пятна, px: всё мельче отбрасывается"),
        ("denoise_kernel",    "ядро фильтра точечного шума перед поиском пятен, px (0 = выкл)"),
    )),
    ("Прицел в игре", (
        ("aim_mode",          "auto = относительное движение, когда активно окно игры, иначе абсолютное"),
        ("window_title",      "ключевое слово в заголовке окна игры; пусто — не привязываться к окну"),
        ("camera_gain",       "доля ошибки за кадр в относительном режиме: больше — резче, но с перелётом"),
    )),
    ("Прицел на рабочем столе", (
        ("lock_radius",       "радиус залипания на прежнюю цель, px (0 = выкл, всегда крупнейшее пятно)"),
        ("lock_timeout",      "через сколько секунд без детекций прежняя цель забывается"),
        ("move_smoothing",    "доля пути до цели за кадр: 1.0 = мгновенно, 0.3 = плавно для записи"),
        ("dead_zone",         "не двигать курсор, если цель ближе стольких пикселей"),
        ("move_duration",     "длительность самого движения мыши, сек (0 = мгновенный прыжок)"),
    )),
    ("Скорость", (
        ("scan_interval",     "пауза между итерациями главного цикла, сек"),
        ("hide_settle_delay", "пауза после скрытия рамок перед захватом, сек (только режим с оверлеем)"),
    )),
    ("Режимы и вывод", (
        ("overlay_enabled",   "рамка оверлея: on = точнее и медленнее, off = быстрый режим (F7)"),
        ("auto_click",        "кликать после наведения (F6)"),
        ("click_interval",    "минимальная пауза между кликами, сек: 0 = на каждой детекции"),
        ("debug_log",         "подробный таймлог на каждую детекцию; обычно хватает строки состояния"),
        ("save_last_frame",   "сохранять last.jpg при детекции — запись на диск добавляет задержку"),
        ("autosave",          "сохранять активный профиль при выходе"),
        ("bench_on_boot",     "гонять бенчмарк один раз за загрузку компьютера, а не каждый старт"),
        ("pause_on_start",    "ждать Enter перед началом слежения, чтобы успеть прочитать вывод"),
    )),
]

SETTINGS_INFO = {name: desc for _, items in SETTINGS_GROUPS for name, desc in items}


def settings_groups_full():
    """Группы для меню плюс всё, что есть в Config, но забыто в SETTINGS_GROUPS.

    Так новая настройка не исчезнет из 'edit' молча, если про описание забыли.
    """
    forgotten = [n for n in vars(cfg) if n not in SETTINGS_INFO]
    if forgotten:
        return list(SETTINGS_GROUPS) + [("Без описания", tuple((n, "") for n in forgotten))]
    return list(SETTINGS_GROUPS)


def range_hint(name, value):
    if isinstance(value, bool):
        return "on/off"
    if isinstance(value, str):
        allowed = ALLOWED_VALUES.get(name)
        return "|".join(allowed) if allowed else "текст"
    low, high = MIN_VALUES.get(name), MAX_VALUES.get(name)
    if low is None and high is None:
        return ""
    if high is None:
        return f"от {low}"
    return f"{low}..{high}"


def format_value(value):
    if value is True:
        return "on"
    if value is False:
        return "off"
    if isinstance(value, str):
        return repr(value)                 # видно пробелы и пустую строку
    return str(value)


def print_settings_menu():
    """Печатает все переменные с описаниями.

    Описание идёт отдельной строкой под именем: в одну строку вместе со
    значением и границами выходило под 130 символов, и хвосты уезжали за
    край обычного окна консоли.
    """
    print("\nНастраиваемые переменные. 'set <имя> <значение>' — изменить,")
    print("'set <имя>' — показать одну с описанием.")
    wrap = max(40, min(console_width() - 1, 78)) - 8
    for title, items in settings_groups_full():
        print("\n  " + paint(title.upper(), "bold"))
        for name, desc in items:
            value = getattr(cfg, name)
            hint = range_hint(name, value)
            # ljust до paint: иначе ширину считали бы вместе с невидимыми кодами
            print(f"    {name:<18}{paint(format_value(value).ljust(14), 'green')}"
                  f"{paint(hint, 'dim')}")
            for line in textwrap.wrap(desc, wrap) or [""]:
                print(f"        {line}")
    print()


def describe_setting(name):
    if not hasattr(cfg, name):
        print(f"Нет такой настройки: {name}. Введи 'edit' для списка.")
        return
    value = getattr(cfg, name)
    hint = range_hint(name, value)
    print(f"  {name} = {format_value(value)}" + (f"   [{hint}]" if hint else ""))
    desc = SETTINGS_INFO.get(name)
    if desc:
        width = max(40, min(console_width() - 1, 78)) - 4
        for line in textwrap.wrap(desc, width):
            print(f"    {line}")
    long_text = SETTINGS_HELP.get(name)
    if long_text:
        print()
        wrap_help(long_text, "    ")
    else:
        print()
    print(f"    изменить: set {name} <значение>")


# Подробные объяснения: одной строки в 'edit' хватает, чтобы вспомнить, но не
# чтобы понять впервые. Это показывает 'set <имя>' без значения.
SETTINGS_HELP = {
    "overlay_enabled": """\
Выбор между двумя способами захвата экрана — это главная развилка в скрипте.

ВКЛ (рамка). Перед каждой парой скриншотов рамка синхронно прячется, кадры
снимаются подряд из главного потока, потом рамка рисуется заново. Так рамка
не попадает в собственный скриншот (иначе она считалась бы изменением и
разрасталась по кругу — это был настоящий баг). Зато каждый цикл ждёт
перерисовки окна, и реакция медленнее.

ВЫКЛ (быстрый). Рамки нет, зато отдельный поток непрерывно снимает экран, а
главный цикл просто берёт последний готовый кадр, не ожидая захвата. Это
быстрее, но съедает одно ядро процессора целиком, пока включено.

Коротко: рамка нужна, когда подбираешь diff_threshold и min_blob_area и
хочешь видеть, что скрипт считает целью. Для самой работы выключай.""",

    "aim_mode": """\
Как двигать мышь. Способов два, и в игре работает только один.

absolute — курсор ставится в точку экрана. Так делает pyautogui, и
на рабочем столе это работает. Но игра, захватившая курсор и читающая raw input
(Minecraft в их числе), такого движения не видит вообще: оно не проходит
через очередь ввода. Отсюда брался симптом "персонаж бьёт воздух, камера
стоит".

relative — мышь сдвигается на столько-то отсчётов, как настоящая мышь.
Это игра видит. Прицел при этом считается не от курсора (его позиция под
захватом ничего не значит), а от центра игрового окна.

auto — relative, пока активно окно игры, absolute в остальное время. Так
работает и в игре, и на рабочем столе, без переключений вручную.""",

    "camera_gain": """\
Насколько резко доворачивать камеру за кадр, от 0.01 до 5.

Скрипт не знает, на сколько градусов повернётся камера от одного отсчёта
мыши: это зависит от чувствительности и FOV в игре. Поэтому наводка идёт
замкнутым контуром — сдвинули на долю ошибки, сняли экран заново, повторили,
пока цель не придёт в прицел. Калибровать ничего не нужно, нужно только
попасть в разумный коэффициент.

Слишком много — камеру перебрасывает через цель и начинает раскачивать.
Слишком мало — доворачивает вяло, цель уходит быстрее, чем догоняем.
Подбирай так: начни с 0.5, при перелётах уменьшай вдвое, при вялости
увеличивай на треть.""",

    "lock_radius": """\
Радиус залипания на прежнюю цель, в пикселях. 0 — выключить.

Без залипания целью каждый кадр становится просто самое крупное пятно
изменений. Как только два пятна меняются площадями местами, курсор прыгает
между ними — на записи это выглядит как дёрганье.

С залипанием: если прежняя цель ещё свежая (прошло меньше lock_timeout) и в
этом радиусе от неё есть пятно, держимся за него, а не за самое большое.
Больше радиус — упорнее держимся за одну цель, но легче "прилипнуть" к
соседнему пятну. Меньше — охотнее переключаемся на новое.""",

    "diff_threshold": """\
Насколько сильно пиксель должен измениться, чтобы считаться изменением.
Шкала 0-255, сравнивается максимум разницы по каналам цвета.

Ниже порог — ловятся более слабые изменения, но вместе с ними анимация воды,
качание травы, рука с предметом. Выше — остаётся только явное, но слабые
частицы могут потеряться.

Если пятен находится слишком много, сначала попробуй denoise_kernel: он
убирает точечный шум, не повышая порог, и слабые цели при этом выживают.""",

    "denoise_kernel": """\
Размер фильтра точечного шума перед поиском пятен, в пикселях.
0 — выключить.

К маске изменений применяется морфологическое "открытие": одиночные шумные
пиксели пропадают, а пятна из нескольких пикселей остаются. Это дёшево и
делается ДО разметки связных областей.

Зачем: сцена с постоянным мелким шумом (вода, трава) давала сотни и тысячи
крошечных пятен, и сама разметка становилась дорогой — задержка подскакивала
до 75-100 мс. Фильтр убирает их заранее. 3 обычно достаточно, 5 — для очень
шумной сцены.""",

    "move_smoothing": """\
Только для absolute-режима (рабочий стол). Доля пути до цели за кадр.

1.0 — курсор прыгает в цель мгновенно. 0.3 — проходит треть пути, и за
несколько кадров подъезжает плавно. Для записи видео плавное движение
выглядит человеческим, а не телепортом.

В relative-режиме за это отвечает camera_gain, а эта настройка не
используется.""",

    "status_hz": """\
Как часто перерисовывать живую строку состояния, раз в секунду. 0 — не
рисовать вообще.

Живая строка пишется из главного цикла, то есть из пути реакции. Сама запись
в консоль дешёвая, но не бесплатная, и на Windows цену задаёт число
обращений, а не длина строки. 12 раз в секунду глаз воспринимает как
непрерывное обновление, а в бюджете кадра это незаметно.

Если выжимаешь минимальную задержку — ставь 4 или 0. Цвет на это не влияет:
лишние байты в уже совершаемой записи ничего не стоят.""",

    "color": """\
Цвет в выводе. auto | on | off.

auto — красить, если консоль умеет ANSI, вывод идёт в терминал (а не в файл)
и не задана переменная среды NO_COLOR. Это нужно, чтобы вместо цвета не
посыпался мусор вида ←[32m.

on — красить через силу. Пригодится, если терминал ANSI умеет, а определение
этого не увидело.

off — никогда. На скорость цвет не влияет.""",

    "click_interval": """\
Минимальная пауза между автокликами, в секундах. 0 — кликать на каждой
детекции.

Детекция идёт сотни раз в секунду, и автоклик без паузы столько же раз и
жмёт. В игре это почти всегда вхолостую: у атаки своя перезарядка. Для меча
в Minecraft осмысленное значение около 0.6.""",

    "margin": """\
Половина стороны квадрата, в котором идёт слежение по Ctrl, в пикселях.

Квадрат строится вокруг прицела: в полноэкранной игре это центр экрана, в
оконной — центр игрового окна. Меньше область — меньше кадр, быстрее
детекция и меньше постороннего шума, но и цель легче упустить за границей.

Режим Alt смотрит всё окно игры целиком и этой настройкой не ограничен.""",
}

# Темы для 'help <тема>': то, что не привязано к одной настройке.
HELP_TOPICS = {
    "режимы": ("Режимы захвата экрана", SETTINGS_HELP["overlay_enabled"]),
    "прицел": ("Как скрипт наводит мышь", SETTINGS_HELP["aim_mode"]),
    "детекция": ("Как находится цель", """\
Скрипт не понимает, что на экране. Он снимает два кадра подряд и ищет, что
между ними изменилось — поэтому и работает по частицам от разрушенного
блока, по летящим предметам, по любому движению.

Что происходит за один цикл:

1. Два снимка области (по Ctrl — квадрат margin вокруг прицела, по Alt — всё
   окно игры).
2. Поиск разницы по каждому пикселю; всё, что изменилось сильнее
   diff_threshold, попадает в маску.
3. denoise_kernel убирает из маски точечный шум.
4. Маска делится на связные области — "пятна". Пятна мельче min_blob_area
   отбрасываются.
5. Из оставшихся выбирается цель: самое крупное пятно, либо то, за которое
   держит залипание (lock_radius).
6. Мышь доворачивается к центру цели.

Если целью становится не то, что нужно, крути именно в этом порядке:
diff_threshold, потом denoise_kernel, потом min_blob_area. Включи рамку
(F7), чтобы видеть, что скрипт считает пятнами: зелёная — выбранная цель,
оранжевые — остальные."""),
    "профили": ("Профили настроек", """\
Настройки хранятся в aim_config.json рядом со скриптом, наборами под разные
сцены — у воды и у разрушенного блока разный шум, и пороги к ним нужны
разные.

  profiles               список: что сохранено, что активно, что грузится
  profile save <имя>     сохранить текущие настройки под этим именем
  profile load <имя>     загрузить
  profile default <имя>  какой профиль брать при запуске
  profile delete <имя>   удалить (спросит подтверждение)
  profile defaults       вернуть встроенные значения из кода
  reset_config           стереть файл целиком (спросит подтверждение)

Встроенные значения живут в самом коде, не в файле. Поэтому скрипт
запускается и без конфига, а 'profile defaults' всегда может вернуть
заведомо рабочий набор.

При выходе активный профиль сохраняется сам (настройка autosave). После
'profile defaults' и 'reset_config' — не сохраняется, иначе выход затёр бы
то, что ты только что осознанно сбросил."""),
    "скорость": ("Откуда берётся задержка", """\
Реакция складывается из захвата экрана, поиска пятен и движения мыши.
Команда 'bench' меряет первые два на твоём железе.

Обычно почти всё время уходит на захват: вызов снимка экрана стоит
фиксированно, почти независимо от размера области. Поиск пятен после
перехода на OpenCV занимает доли миллисекунды на небольшой области.

Важный предел: при мониторе 60 Гц между двумя РАЗНЫМИ кадрами проходит
16.6 мс, и быстрее этого реагировать физически не на что — новой картинки
просто ещё нет. Задержка в живой строке красится по этому порогу: зелёная —
с запасом, жёлтая — ещё успеваем, красная — в кадр не уложились.

Что реально помогает: выключить рамку (F7), уменьшить margin, поднять
denoise_kernel на шумной сцене, уменьшить status_hz."""),
}


LIST_ITEM = re.compile(r"^(\s*)((?:\d+\.|[-*])\s+)(.*)$")


def wrap_help(text, indent):
    """Печатает длинный текст, переложив его под ширину окна.

    Перенос делается на выводе, а не в самих строках: иначе любая правка
    текста норовит вылезти за край, а в узком окне всё равно поехало бы.

    Нумерованные и маркированные пункты переносятся с висячим отступом —
    продолжение встаёт под текстом пункта, а не под его номером. Выровненные
    по колонкам блоки (список команд) оставляем как есть: переложить их,
    не потеряв выравнивание, нельзя.
    """
    width = max(40, min(console_width() - 1, 78)) - len(indent)
    for block in text.split("\n\n"):
        lines = block.split("\n")
        if any(LIST_ITEM.match(line) for line in lines):
            for item in _join_list_items(lines):
                match = LIST_ITEM.match(item)
                if not match:
                    print(indent + item)
                    continue
                lead, marker, body = match.groups()
                hang = len(lead) + len(marker)
                for line in textwrap.wrap(body, width, initial_indent=lead + marker,
                                          subsequent_indent=" " * hang) or [lead + marker]:
                    print(indent + line)
        elif any(line[:1].isspace() for line in lines if line):
            for line in lines:                      # таблица команд: не трогаем
                print(indent + line if line else "")
        else:
            for line in textwrap.wrap(" ".join(lines), width) or [""]:
                print(indent + line)
        print()


def _join_list_items(lines):
    """Склеивает продолжения пунктов, чтобы перенос считал пункт целиком."""
    items = []
    for line in lines:
        if LIST_ITEM.match(line) or not items:
            items.append(line)
        else:
            items[-1] += " " + line.strip()
    return items


def print_topic_help(name):
    key = name.strip().lower()
    if key not in HELP_TOPICS:
        print(f"Нет такой темы: {name}. Доступны: {', '.join(HELP_TOPICS)}")
        return
    title, text = HELP_TOPICS[key]
    print("\n  " + paint(title.upper(), "bold"))
    wrap_help(text, "  ")


def print_window_info():
    """Диагностика привязки к окну: без неё непонятно, почему не наводится.

    Показывает и активное сейчас окно, и последнее совпавшее: пока ты набираешь
    команду в консоли, активна именно консоль, так что первое почти всегда
    будет "не совпадает" — смотреть надо на второе.
    """
    try:
        hwnd, title = foreground_window_title()
    except Exception as exc:               # noqa: BLE001 — диагностике падать незачем
        print(f"[Окно] Не удалось опросить активное окно: {exc}")
        return

    needle = cfg.window_title.strip()
    matched = bool(needle) and needle.lower() in title.lower()
    print(f"\n  Ключевое слово:  {needle!r}")
    print(f"  Активно сейчас:  {title!r}  ->  {'совпадает' if matched else 'не совпадает'}")

    last = _aim["last_match"]
    if last is None:
        print("  Окно игры ещё ни разу не попадалось.")
        print(f"  Прицел: центр экрана ({center_x}, {center_y}).")
    else:
        last_title, (left, top, width, height) = last
        print(f"  Последнее совпавшее: {last_title!r}")
        print(f"    клиентская область {width}x{height} в точке ({left}, {top})")
        print(f"    прицел ({left + width // 2}, {top + height // 2})")

    way = "относительное (SendInput)" if _aim["relative"] else "абсолютное (SetCursorPos)"
    print(f"  aim_mode={cfg.aim_mode} -> сейчас движение {way}")
    print("  Абсолютное движение игра с raw input не увидит.\n")


def print_settings():
    for name, value in vars(cfg).items():
        print(f"  {name} = {format_value(value)}")


# ---------- Конфиг: профили настроек ----------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aim_config.json")
DEFAULT_PROFILE_NAME = "default"

# data — содержимое файла, active — какой профиль сейчас правится в памяти.
# autosave_ok выключается там, где сохранение при выходе уничтожило бы то,
# что пользователь только что осознанно сделал: стёр конфиг или вернул
# встроенные значения. Явные 'profile save'/'profile load' включают обратно.
_config = {"data": None, "active": DEFAULT_PROFILE_NAME, "autosave_ok": True}

# Конфиг трогают два потока: консольный (команды profile/reset_config) и
# главный (автосохранение при выходе). RLock, потому что операции вложены —
# profile_delete внутри себя зовёт save_config_file.
_config_lock = threading.RLock()


def builtin_defaults():
    """Встроенные значения — из класса Config, а не из файла.

    Поэтому скрипт запускается и без aim_config.json, а 'profile defaults'
    всегда может вернуть заведомо рабочий набор, даже если в файле намешано.
    """
    return vars(Config())


def empty_config():
    return {"version": 1, "default_profile": DEFAULT_PROFILE_NAME, "profiles": {}, "bench": {}}


def load_config_file():
    """Читает конфиг. Возвращает dict, или None если файла нет / он битый.

    Битый файл не затирается, а переименовывается в .bak: там могут быть
    профили, которые пользователь подбирал руками.
    """
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
            raise ValueError("нет секции profiles")
    except (OSError, ValueError) as exc:
        print(f"[Конфиг] {CONFIG_PATH} не читается ({exc}).")
        try:
            os.replace(CONFIG_PATH, CONFIG_PATH + ".bak")
            print(f"[Конфиг] Отложен в {CONFIG_PATH}.bak, начинаю с чистого.")
        except OSError as exc2:
            print(f"[Конфиг] Переименовать тоже не вышло: {exc2}")
        return None
    data.setdefault("version", 1)
    data.setdefault("default_profile", DEFAULT_PROFILE_NAME)
    data.setdefault("bench", {})
    return data


def save_config_file():
    with _config_lock:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
                json.dump(_config["data"], handle, ensure_ascii=False, indent=2, sort_keys=True)
            return True
        except OSError as exc:
            print(f"[Конфиг] Не могу записать {CONFIG_PATH}: {exc}")
            return False


def current_values():
    return dict(vars(cfg))


def profile_names():
    return sorted(_config["data"]["profiles"])


def apply_values(values):
    """Загружает набор значений в cfg, пропуская всё, что не проходит проверку.

    Значения идут через те же coerce/bounds, что и команда 'set': файл
    правится руками, и одна опечатка не должна ронять запуск.
    """
    skipped = []
    for name, raw in values.items():
        if not hasattr(cfg, name):
            skipped.append(f"{name} (нет такой настройки)")
            continue
        try:
            value = coerce_setting(name, raw)
        except (ValueError, TypeError):
            skipped.append(f"{name} (не разобрать {raw!r})")
            continue
        error = bounds_error(name, value)
        if error:
            skipped.append(f"{name} ({error})")
            continue
        assign_setting(name, value)
    if skipped:
        print("[Конфиг] Пропущено: " + "; ".join(skipped))


def confirm(question):
    try:
        return input(f"{question} [y/N]: ").strip().lower() in ("y", "yes", "д", "да")
    except EOFError:
        return False


def profile_save(name=None):
    with _config_lock:
        name = name or _config["active"]
        _config["data"]["profiles"][name] = current_values()
        _config["active"] = name
        _config["autosave_ok"] = True
        if save_config_file():
            print(f"[Профиль] Сохранён: {name}")


def profile_load(name):
    with _config_lock:
        values = _config["data"]["profiles"].get(name)
        if values is None:
            print(f"[Профиль] Нет профиля {name!r}. 'profiles' — список.")
            return
        apply_values(values)
        _config["active"] = name
        _config["autosave_ok"] = True
        print(f"[Профиль] Загружен: {name}")


def profile_delete(name):
    # confirm() ждёт ввода, поэтому спрашиваем ДО взятия замка: иначе выход из
    # скрипта во время висящего вопроса заклинил бы автосохранение навсегда.
    with _config_lock:
        if name not in _config["data"]["profiles"]:
            print(f"[Профиль] Нет профиля {name!r}.")
            return
    if not confirm(f"Удалить профиль {name!r}?"):
        print("Отменено.")
        return
    with _config_lock:
        if name not in _config["data"]["profiles"]:
            return                      # успели удалить, пока мы спрашивали
        del _config["data"]["profiles"][name]
        if _config["data"]["default_profile"] == name:
            _config["data"]["default_profile"] = DEFAULT_PROFILE_NAME
        if _config["active"] == name:
            _config["active"] = _config["data"]["default_profile"]
        if save_config_file():
            print(f"[Профиль] Удалён: {name}")


def profile_set_default(name):
    with _config_lock:
        if name not in _config["data"]["profiles"]:
            print(f"[Профиль] Нет профиля {name!r}. Сначала сохрани: profile save {name}")
            return
        _config["data"]["default_profile"] = name
        if save_config_file():
            print(f"[Профиль] При запуске будет грузиться: {name}")


def profile_defaults():
    with _config_lock:
        apply_values(builtin_defaults())
        _config["autosave_ok"] = False    # иначе выход затёр бы профиль дефолтами
        print("[Профиль] Загружены встроенные значения из кода. Файл не тронут,")
        print(f"          и профиль {_config['active']!r} при выходе тоже не перезапишется.")
        print("          Чтобы закрепить — 'profile save'.")


def reset_config():
    if not confirm(f"Стереть {os.path.basename(CONFIG_PATH)} со всеми профилями?"):
        print("Отменено.")
        return
    with _config_lock:
        _config["data"] = empty_config()
        _config["active"] = DEFAULT_PROFILE_NAME
        apply_values(builtin_defaults())
        try:
            if os.path.exists(CONFIG_PATH):
                os.remove(CONFIG_PATH)
            _config["autosave_ok"] = False   # иначе выход тут же создал бы файл заново
            print("[Конфиг] Файл удалён, настройки вернулись к встроенным.")
            print("          При выходе он не будет создан заново — если передумаешь,")
            print("          сохрани явно: 'profile save'.")
        except OSError as exc:
            print(f"[Конфиг] Не могу удалить файл: {exc}")


def print_profiles_page():
    data = _config["data"]
    print(f"\nПрофили настроек — {CONFIG_PATH}\n")
    names = profile_names()
    if not names:
        print("  (ни одного не сохранено; работают встроенные значения из кода)")
    for name in names:
        marks = []
        if name == _config["active"]:
            marks.append("активный")
        if name == data["default_profile"]:
            marks.append("грузится при запуске")
        print(f"  {name}" + (f"   <- {', '.join(marks)}" if marks else ""))
    print(
        "\n  profile save [имя]     — сохранить текущие настройки в профиль\n"
        "  profile load <имя>     — загрузить профиль\n"
        "  profile delete <имя>   — удалить профиль (спросит подтверждение)\n"
        "  profile default <имя>  — какой профиль грузить при запуске\n"
        "  profile defaults       — вернуть встроенные значения из кода\n"
        "  reset_config           — стереть файл целиком (спросит подтверждение)\n"
    )


def run_profile_command(args):
    if not args:
        print_profiles_page()
        return
    sub_cmd = args[0].lower()
    name = args[1] if len(args) > 1 else None
    if sub_cmd == "save":
        profile_save(name)
    elif sub_cmd == "defaults":
        profile_defaults()
    elif sub_cmd in ("load", "delete", "default"):
        if not name:
            print(f"Формат: profile {sub_cmd} <имя>")
        elif sub_cmd == "load":
            profile_load(name)
        elif sub_cmd == "delete":
            profile_delete(name)
        else:
            profile_set_default(name)
    else:
        print("Не понял. 'profiles' — список профилей и доступные команды.")


def boot_id():
    """Метка текущей загрузки системы, чтобы бенчмарк шёл раз за загрузку.

    GetTickCount64 отдаёт аптайм в миллисекундах, значит момент загрузки =
    сейчас минус аптайм. Округляем до 10 секунд: иначе дрожание таймеров
    делало бы каждый запуск "новой загрузкой". None — API недоступен, тогда
    ориентируемся просто на наличие сохранённого замера.
    """
    try:
        uptime_seconds = ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except (AttributeError, OSError):
        return None
    return int((time.time() - uptime_seconds) / 10)


def maybe_run_startup_benchmark():
    with _config_lock:
        saved = _config["data"].get("bench") or {}
        have_saved = bool(saved.get("text"))
        current_boot = boot_id()

        if have_saved and not cfg.bench_on_boot:
            print("\nПоследний замер (bench_on_boot выключен, 'bench' — перемерить):")
            print(saved["text"])
            return
        if have_saved and (current_boot is None or saved.get("boot") == current_boot):
            print("\nЗамер с этой загрузки компьютера ('bench' — перемерить):")
            print(saved["text"])
            return

        _config["data"]["bench"] = {"boot": current_boot, "text": run_benchmark()}
        save_config_file()


def startup_load_config():
    """Готовит настройки к работе: файл -> профиль по умолчанию -> margin.

    Файла нет — это первый запуск: спрашиваем margin, как раньше, и сразу
    сохраняем профиль, чтобы больше не спрашивать.
    """
    data = load_config_file()
    if data is None:
        _config["data"] = empty_config()
        cfg.margin = ask_margin()
        _config["data"]["profiles"][DEFAULT_PROFILE_NAME] = current_values()
        if save_config_file():
            print(f"[Конфиг] Создан {CONFIG_PATH}")
        return

    _config["data"] = data
    name = data["default_profile"]
    if name not in data["profiles"]:
        available = profile_names()
        if not available:
            print("[Конфиг] В файле нет профилей — работаю на встроенных значениях.")
            return
        name = available[0]
        print(f"[Конфиг] Профиль {data['default_profile']!r} не найден, беру {name!r}.")
    apply_values(data["profiles"][name])
    _config["active"] = name
    # экран мог смениться с прошлого запуска — сохранённый margin может не влезать
    cfg.margin = max(MIN_VALUES["margin"], min(int(cfg.margin), MAX_VALUES["margin"]))
    print(f"[Конфиг] Профиль: {name}   margin={cfg.margin}")



def console_loop():
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        try:
            if cmd == "help":
                if len(parts) > 1:
                    print_topic_help(" ".join(parts[1:]))
                else:
                    print_help()
            elif cmd == "show":
                print_settings()
            elif cmd == "log":
                cfg.debug_log = parse_bool(parts[1])
            elif cmd == "overlay":
                set_overlay_enabled(parse_bool(parts[1]))
            elif cmd == "click":
                cfg.auto_click = parse_bool(parts[1])
            elif cmd == "save":
                cfg.save_last_frame = parse_bool(parts[1])
            elif cmd == "bench":
                run_benchmark()
            elif cmd == "edit":
                print_settings_menu()
            elif cmd == "window":
                print_window_info()
            elif cmd == "profiles":
                print_profiles_page()
            elif cmd == "profile":
                run_profile_command(parts[1:])
            elif cmd == "reset_config":
                reset_config()
            elif cmd == "set":
                if len(parts) == 1:
                    print("Формат: set <имя> <значение>. Введи 'edit', чтобы увидеть все переменные.")
                elif len(parts) == 2:
                    describe_setting(parts[1])          # без значения — показываем текущее и описание
                else:
                    # значение склеиваем: в заголовке окна бывают пробелы
                    apply_setting(parts[1], " ".join(parts[2:]))
            else:
                print("Неизвестная команда. Введи 'help'.")
        except (IndexError, ValueError):
            print("Неверный формат команды. Введи 'help'.")


def print_header():
    """Компактная шапка вместо прежних пятнадцати строк подсказок.

    Там всё шло одним весом — клавиши, настройки, поведение при ошибке, — и
    читать это было нечем. Подробности остались в 'help' и 'edit'.
    """
    width = max(60, min(console_width() - 1, 78))
    rule = paint("═" * width, "dim")
    print("\n" + rule)
    print("  " + paint("AimTracker", "bold"))
    print(rule)
    key = lambda k: paint(k, "cyan")           # noqa: E731 — локальный сокращатель
    print(f"  {key('Ctrl')}  слежение у прицела        {key(AUTOCLICK_TOGGLE_KEY.upper())}  автоклик")
    print(f"  {key('Alt')}   слежение по всему окну    {key(OVERLAY_TOGGLE_KEY.upper())}  рамка оверлея"
          f"     {key(PANIC_KEY.upper())}  {paint('СТОП', 'red')}")
    print()

    last = _aim["last_match"]
    if last is None:
        where = f"окно игры не найдено, прицел в центре экрана ({center_x}, {center_y})"
    else:
        title, (left, top, w, h) = last
        where = f"{title}  {w}x{h} @ ({left}, {top})"
    print(f"  профиль {paint(_config['active'], 'bold')}    {where}")
    mode = "рамка (точнее)" if cfg.overlay_enabled else "быстрый (без рамки)"
    click = paint("вкл", "yellow") if cfg.auto_click else "выкл"
    print(f"  захват {paint(mode, 'bold')}    автоклик {click}")
    print()
    print(f"  {paint('help', 'cyan')} — команды   {paint('edit', 'cyan')} — настройки   "
          f"{paint('window', 'cyan')} — привязка к окну")
    print(f"  {paint('help режимы', 'cyan')} — что такое захват   "
          f"{paint('set <имя>', 'cyan')} — разбор настройки")
    print(rule)


_vt["ok"] = enable_vt()     # до любого цветного вывода: иначе первые строки выйдут серыми
startup_load_config()
set_overlay_enabled(cfg.overlay_enabled)  # запускает грабер, если оверлей выключен
update_aim_context()        # чтобы шапка показала реальную привязку, а не "окно не найдено"
print_header()
# Бенчмарк — до запуска консоли: иначе команда из консоли (например
# reset_config) успевала отработать в середине замера, а завершавшийся
# следом бенчмарк своим save_config_file() воскрешал только что удалённый файл.
maybe_run_startup_benchmark()

if cfg.pause_on_start:
    # до старта консольного потока: иначе он перехватил бы этот же Enter
    try:
        input("\n[Пауза] Enter — начать слежение ('set pause_on_start off' уберёт паузу)...")
    except EOFError:
        pass

threading.Thread(target=console_loop, daemon=True).start()


_status_next = [0.0]
_was_tracking = [False]

try:
    while not _stop.is_set():
        found = False
        tracking_key = None
        # прицел и способ движения пересчитываем каждую итерацию: окно игры
        # можно свернуть, подвинуть или переключиться из него в другое
        viewport = update_aim_context()

        if keyboard.is_pressed(TRACK_KEY):
            tracking_key = "Ctrl"
            # снимок на итерацию: margin правится из консоли прямо во время работы
            margin = int(cfg.margin)
            cross_x, cross_y = _aim["crosshair"]
            left, top, width, height = clamp_region(
                cross_x - margin, cross_y - margin, margin * 2, margin * 2)
            found = track(left, top, width, height, offset_x=left, offset_y=top)
        elif keyboard.is_pressed(FULLSCREEN_KEY):
            tracking_key = "Alt"
            # когда игра идёт в окне, незачем разглядывать остальной рабочий стол
            if viewport is None:
                left, top, width, height = 0, 0, screen_width, screen_height
            else:
                left, top, width, height = clamp_region(*viewport)
            found = track(left, top, width, height, offset_x=left, offset_y=top)

        if not found and cfg.overlay_enabled:
            overlay.hide_all_and_wait()
        if not found:
            _stat["target"] = None

        # Живая строка идёт только пока держишь клавишу слежения. Иначе она
        # затирала бы приглашение "> ", на котором ты печатаешь команду: без
        # ANSI развести их по разным строкам нечем, а в момент слежения руки
        # всё равно на клавише и мыши, а не на клавиатуре.
        now = time.perf_counter()
        if tracking_key is not None and cfg.status_hz > 0:
            if now >= _status_next[0]:
                _status_next[0] = now + 1.0 / cfg.status_hz
                show_status(render_status(tracking_key))
            _was_tracking[0] = True
        elif _was_tracking[0]:
            _was_tracking[0] = False
            clear_status()
            _real_print("> ", end="", flush=True)   # возвращаем приглашение на место

        time.sleep(cfg.scan_interval)
except pyautogui.FailSafeException:
    # Пользователь сам увёл курсор в угол — это штатный аварийный тормоз,
    # а не ошибка, так что выходим без стектрейса.
    print(paint("\n[Стоп] Курсор в углу экрана — аварийная остановка pyautogui.", "red"))
except KeyboardInterrupt:
    print(paint("\n[Стоп] Прервано с клавиатуры.", "yellow"))
finally:
    clear_status()
    grabber.stop()
    if cfg.autosave and _config["data"] is not None and _config["autosave_ok"]:
        profile_save(_config["active"])
