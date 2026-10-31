import atexit
import ctypes
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import tempfile
import time
import traceback
from functools import wraps

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
from aim_detection import DetectionOptions, TargetTracker, detect_changes
from aim_recording import SessionRecorder
from aim_panel import PanelBridge


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


def clear_screen():
    """Чистит консоль перед выводом страницы.

    Через ANSI, если консоль его разбирает: быстро и не плодит процесс.
    Иначе зовём штатную команду — os.system стоит заметно дороже, но
    срабатывает там, где VT включить не вышло.
    """
    if _vt["ok"]:
        _real_print("\x1b[2J\x1b[3J\x1b[H", end="", flush=True)
    else:
        os.system("cls" if os.name == "nt" else "clear")


def page(title, subtitle=""):
    """Шапка страницы — одинаковая у всех разделов."""
    clear_screen()
    with _out_lock:
        _status["shown"] = False      # экран очищен, прежней строки состояния на нём нет
    width = max(60, min(console_width() - 1, 78))
    rule = paint("═" * width, "dim")
    print(rule)
    print("  " + paint(title.upper(), "bold")
          + ("   " + paint(subtitle, "dim") if subtitle else ""))
    print(rule)


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

# Боковые кнопки мыши: keyboard их не видит, спрашиваем систему напрямую.
MOUSE_KEYS = {"mouse4": 0x05, "mouse5": 0x06}      # VK_XBUTTON1 / VK_XBUTTON2
KEY_SETTINGS = ("track_key", "fullscreen_key", "autoclick_key",
                "overlay_key", "panic_key", "dump_key")

TUNE_DIR = "tune"

PRIMARY_COLOR = "#00FF00"
SECONDARY_COLOR = "#FFA500"
REGION_COLOR = "#00BFFF"
CROP_PADDING = 15
MAX_BLOBS_SHOWN = 24
__version__ = "0.2.0"

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
        self.observe_only = True        # диагностика без ввода; включать действия явно
        self.require_game_window = True
        self.confirm_frames = 2
        self.max_blob_fraction = 0.15
        self.max_aspect_ratio = 8.0
        self.ignore_bottom_fraction = 0.0
        self.exclusion_rects = "[]"     # нормализованные области окна, JSON
        self.compensate_camera = False  # экспериментальная компенсация общего сдвига
        self.max_frame_age = 0.10
        self.pair_interval = 0.016
        self.max_mouse_step = 120
        self.aim_mode = "auto"          # auto | absolute | relative (см. move_and_maybe_click)
        self.window_exe = ""            # имя exe окна игры, задаётся через 'window pick'
        self.window_title = "Minecraft"  # запасной способ: ключевое слово в заголовке
        self.track_key = "ctrl"         # держать — слежение в области у прицела
        self.fullscreen_key = "alt"     # держать — слежение по всему окну
        self.autoclick_key = "f6"       # переключатель автоклика
        self.overlay_key = "f7"         # переключатель рамки оверлея
        self.panic_key = "f8"           # аварийная остановка
        self.camera_gain = 0.5          # доля ошибки, проходимая за кадр в относительном режиме
        self.settle_after_action = 0.05  # сколько не снимать кадры после своего движения/клика, сек
        self.global_motion_limit = 0.5   # доля изменившегося кадра, выше которой это разворот, а не цель
        self.click_tolerance = 8        # кликать, только когда цель ближе стольких пикселей к прицелу
        self.margin = 100               # отступ от центра до края области слежения (режим Ctrl), px
        self.scan_interval = 0.005      # пауза между итерациями главного цикла (сек)
        self.diff_threshold = 30        # порог чувствительности разницы пикселей (0-255)
        self.min_blob_area = 4          # мин. площадь пятна в пикселях (отсекает шум)
        self.move_duration = 0.0        # плавность движения мыши (0 = мгновенно, быстрее всего)
        self.hide_settle_delay = 0.02   # пауза после скрытия рамок (только режим с оверлеем)
        self.box_hold = 0.0             # сколько держать рамки на экране, сек (0 = как раньше)
        self.margin_preview = 2.0       # сколько показывать квадрат области после смены margin, сек
        self.denoise_kernel = 3         # размер ядра фильтра шума перед поиском пятен (0 = выкл)
        self.lock_radius = 120          # радиус залипания на цель в пикселях (0 = выкл)
        self.lock_timeout = 0.35        # через столько секунд без детекций цель забывается (сек)
        self.move_smoothing = 1.0       # доля пути до цели за кадр: 1.0 = мгновенно, 0.3 = плавно
        self.dead_zone = 0              # не двигать курсор, если цель ближе стольких пикселей
        self.debug_log = False          # подробный таймлог каждой детекции (основной вид — строка состояния)
        self.save_last_frame = False    # сохранять last.jpg (лишняя запись на диск = задержка)
        self.auto_click = False
        self.click_interval = 0.15      # минимальная пауза между автокликами, сек
        self.overlay_enabled = True     # True = точный, но медленный синхронный режим
                                         # False = быстрый режим с фоновым захватом экрана
        self.autosave = True            # сохранять активный профиль при выходе
        self.bench_on_boot = True       # прогонять бенчмарк один раз за загрузку компьютера
        self.pause_on_start = False      # ждать Enter после вывода настроек, перед слежением
        self.tune_mode = False          # отладочный режим: разметка блобов и кадры на диск
        self.tune_frames = 20           # сколько последних кадров держать для сброса
        self.dump_key = "f9"            # сбросить накопленные кадры на диск
        self.status_hz = 12             # как часто перерисовывать живую строку, раз/с (0 = не рисовать)
        self.color = "auto"             # auto | on | off


cfg = Config()
_state_lock = threading.RLock()


def state_locked(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with _state_lock:
            return function(*args, **kwargs)
    return locked


_panel = None
_preview_next = [0.0]
_gui_requested = "--gui" in sys.argv


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


# EnumWindows хочет функцию по соглашению stdcall; на не-Windows такого типа
# в ctypes нет, и подставляется обычный — туда мы всё равно не попадаем.
WNDENUMPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def configure_window_api():
    """Preserve pointer-sized HWND/HANDLE values on 64-bit Windows."""
    pointer, integer, dword = ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong
    signatures = {
        "user32": {
            "GetForegroundWindow": ([], pointer),
            "GetParent": ([pointer], pointer),
            "IsWindowVisible": ([pointer], integer),
            "GetWindowTextLengthW": ([pointer], integer),
            "GetWindowTextW": ([pointer, ctypes.c_wchar_p, integer], integer),
            "GetClientRect": ([pointer, ctypes.POINTER(RECT)], integer),
            "ClientToScreen": ([pointer, ctypes.POINTER(POINT)], integer),
            "GetWindowThreadProcessId": ([pointer, ctypes.POINTER(dword)], dword),
            "EnumWindows": ([WNDENUMPROC, ctypes.c_ssize_t], integer),
            "GetWindowLongW": ([pointer, integer], ctypes.c_long),
            "SetWindowLongW": ([pointer, integer, ctypes.c_long], ctypes.c_long),
        },
        "kernel32": {
            "OpenProcess": ([dword, integer, dword], pointer),
            "QueryFullProcessImageNameW": (
                [pointer, dword, ctypes.c_wchar_p, ctypes.POINTER(dword)], integer),
            "CloseHandle": ([pointer], integer),
        },
    }
    for library, functions in signatures.items():
        dll = getattr(ctypes.windll, library)
        for name, (arguments, result) in functions.items():
            function = getattr(dll, name, None)
            if function is not None:
                try:
                    function.argtypes, function.restype = arguments, result
                except AttributeError:
                    # Python methods in the Linux test adapters have no ABI.
                    pass


def process_name(hwnd):
    """Имя исполняемого файла окна.

    Берём именно его, а не заголовок: заголовок переписывают лаунчеры и моды,
    а exe у игры один и тот же.
    """
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(260)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value.replace("\\", "/").rsplit("/", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)


def list_windows():
    """Visible titled top-level windows, including small/minimized windows.

    Process metadata is optional: access denied must not hide an entire window.
    Exceptions cannot escape a ctypes callback; retain them for diagnostics.
    """
    found, errors = [], []
    own_pids = {os.getpid()}
    panel_process = getattr(_panel, "process", None)
    if panel_process is not None:
        own_pids.add(panel_process.pid)

    def _collect(hwnd, _lparam):
        try:
            user32 = ctypes.windll.user32
            if not user32.IsWindowVisible(hwnd):
                return True
            title = window_text(hwnd)
            if not title.strip():
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in own_pids:
                return True
            rect = client_rect_on_screen(hwnd) or (0, 0, 0, 0)
            try:
                exe = process_name(hwnd)
            except (OSError, ValueError, OverflowError):
                exe = ""
            found.append((int(hwnd), title, exe, rect))
        except Exception as exc:
            errors.append(str(exc))
        return True

    if not ctypes.windll.user32.EnumWindows(WNDENUMPROC(_collect), 0):
        raise OSError("Windows не удалось перечислить окна")
    _runtime["window_errors"] = errors
    if errors and not found:
        raise OSError("Не удалось прочитать окна: " + errors[0])
    return sorted(found, key=lambda item: (item[1].casefold(), item[0]))


def window_matches(title, exe):
    """Подходит ли окно под текущую привязку."""
    wanted_exe = cfg.window_exe.strip().lower()
    if wanted_exe:
        return exe.lower() == wanted_exe
    needle = cfg.window_title.strip().lower()
    return bool(needle) and needle in title.lower()


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
    "last_seen": 0.0,       # когда его видели в последний раз
    "viewport": None,
    "hwnd": 0,
}
_selected_hwnd = [None]


@state_locked
def update_aim_context():
    """Определяет точку прицела и способ движения на эту итерацию.

    Окно ищем только среди активных: целиться в неактивную игру бессмысленно,
    а перебор всех окон давал бы ложные совпадения — браузер со словом
    Minecraft в заголовке, папка с таким же именем.

    Возвращает область игры или None (обычный рабочий стол).
    """
    viewport = None
    if cfg.require_game_window and (cfg.window_exe.strip() or cfg.window_title.strip()):
        hwnd, title = foreground_window_title()
        # exe спрашиваем только когда он задан: это обращение к чужому процессу
        exe = process_name(hwnd) if (hwnd and cfg.window_exe.strip()) else ""
        if hwnd and window_matches(title, exe) and (_selected_hwnd[0] is None or hwnd == _selected_hwnd[0]):
            viewport = client_rect_on_screen(hwnd)
            if viewport is not None:
                _aim["last_match"] = (title, viewport)
                _aim["last_seen"] = time.perf_counter()

    identity = (hwnd if viewport is not None else 0, viewport)
    if identity != (_aim["hwnd"], _aim["viewport"]):
        reset_tracking()
    _aim["hwnd"], _aim["viewport"] = identity
    if viewport is None:
        _aim["crosshair"] = (center_x, center_y)
        _aim["relative"] = cfg.require_game_window and cfg.aim_mode == "relative"
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
        # отдельный прямоугольник под область слежения: он не блоб, и цвет у него свой
        self.region_id = self.canvas.create_rectangle(
            -10, -10, -10, -10, outline=REGION_COLOR, width=self.thickness)

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
            try:
                draw_fn()
                self.root.update_idletasks()
            except Exception as exc:
                self.error = exc
            finally:
                done.set()

        self.root.after(0, _job)
        if not done.wait(timeout=0.2):
            self.error = RuntimeError("тайм-аут скрытия/отрисовки оверлея")

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
            self.canvas.coords(self.region_id, -10, -10, -10, -10)
        self._run_synced(_hide)

    def show_region_and_wait(self, box):
        """Рисует границу области слежения — чтобы видеть, куда достаёт детекция."""
        def _draw():
            self.canvas.coords(self.region_id, *box)
        self._run_synced(_draw)
        with self._shown_lock:
            self._boxes_shown = True

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
        self._halt = threading.Event()
        self.error = None

    def start(self):
        if self._running:
            return
        self._halt.clear()
        self.error = None
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._halt.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("поток захвата не завершился; перезапустите программу")

    def set_region(self, region):
        """Переключает снимаемую область. Кадры от прежней сразу выбрасываются."""
        with self.lock:
            if region == self.region:
                return
            self.region = region
            self.latest = None
            self.latest_ts = 0.0

    def _run(self):
        try:
            self._capture_loop()
        except Exception as exc:
            self.error = str(exc)
        finally:
            self._running = False

    def _capture_loop(self):
        with mss.MSS() as sct:
            while not self._halt.is_set():
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
                    self.latest_ts = t0   # возраст считается от начала захвата
                    self.capture_time_ms = (t1 - t0) * 1000
                self._halt.wait(max(0, cfg.pair_interval - (t1 - t0)))

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
configure_window_api()

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
_target_tracker = TargetTracker()
_recorder = SessionRecorder(max_frames=cfg.tune_frames)
_baseline_region = [None]
_baseline_after = [0.0]
_previous_ts = [0.0]
_runtime = {"paused": False, "reason": "ожидание клавиши", "dirty": False,
            "preview": None, "windows": [], "error": "", "frame_age_ms": 0.0}
_engine_commands = queue.Queue(maxsize=128)


def reset_lock():
    _lock["pos"] = None
    _lock["ts"] = 0.0
    _lock["held"] = False
    _target_tracker.reset()


def reset_frame_history():
    global _last_used_ts
    _prev_holder["frame"] = None
    _last_used_ts = 0.0
    _previous_ts[0] = 0.0
    _baseline_after[0] = time.perf_counter()


def reset_tracking():
    reset_frame_history()
    reset_lock()
    if "_move_remainder" in globals():
        _move_remainder.update(x=0.0, y=0.0)


def enqueue_command(command):
    try:
        _engine_commands.put_nowait(command)
    except queue.Full:
        _runtime["error"] = "Слишком много команд; повторите изменение."


@state_locked
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
    reset_tracking()
    print(paint("[Оверлей]", "cyan"),
          "ВКЛ — рамка рисуется, захват синхронный (с ожиданием рамок)" if enabled
          else "ВЫКЛ — быстрый режим, кадры берутся из фонового потока")


def key_is_valid(name):
    low = name.strip().lower()
    if low in MOUSE_KEYS:
        return True
    try:
        keyboard.key_to_scan_codes(low)
        return True
    except (ValueError, KeyError):
        return False


def is_key_held(name):
    """Зажата ли клавиша. Понимает и mouse4/mouse5."""
    low = name.strip().lower()
    code = MOUSE_KEYS.get(low)
    if code is not None:
        return bool(ctypes.windll.user32.GetAsyncKeyState(code) & 0x8000)
    try:
        return keyboard.is_pressed(low)
    except (ValueError, KeyError):
        return False


_hotkeys = []


def rebind_hotkeys():
    """Перевешивает переключатели после смены настроек клавиш.

    Кнопки мыши на переключатели не вешаются: keyboard их не перехватывает,
    а опрашивать состояние в цикле ради переключателя смысла нет.
    """
    global _hotkeys
    for handle in _hotkeys:
        try:
            keyboard.remove_hotkey(handle)
        except (KeyError, ValueError):
            pass
    _hotkeys = []
    for name, action in ((cfg.autoclick_key, lambda: enqueue_command({"command": "toggle_click"})),
                         (cfg.overlay_key, lambda: enqueue_command({"command": "toggle_overlay"})),
                         (cfg.panic_key, panic_stop),
                         (cfg.dump_key, lambda: enqueue_command({"command": "record"}))):
        low = name.strip().lower()
        if low in MOUSE_KEYS:
            print(paint("[Клавиши]", "yellow"),
                  f"{name} — кнопка мыши, переключателем быть не может, пропущено")
            continue
        try:
            _hotkeys.append(keyboard.add_hotkey(low, action))
        except (ValueError, KeyError) as exc:
            print(paint("[Клавиши]", "yellow"), f"не удалось повесить {name}: {exc}")


def toggle_autoclick():
    cfg.auto_click = not cfg.auto_click
    print(paint("[Автоклик]", "cyan"), "включен" if cfg.auto_click else "выключен")


def panic_stop():
    """F8 always stops. Recording/pause never alter the emergency action."""
    _stop.set()
    print(paint(f"\n[Стоп] Аварийная остановка по {cfg.panic_key.upper()}.", "red"))




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
def is_global_motion(changed_fraction):
    """Похоже ли, что кадр сменился целиком, а не из-за цели.

    Когда скрипт сам поворачивает камеру, сдвигается вся картинка: меняется
    почти каждый пиксель, пятен находятся сотни, и скрипт начинает гоняться
    за собственным разворотом — персонаж крутится без остановки. Окно
    оседания (settle_after_action) закрывает основной случай, а эта проверка
    ловит остальное: чужое движение камеры, вспышку, смену сцены.
    """
    if cfg.global_motion_limit <= 0 or cfg.global_motion_limit >= 1:
        return False
    if changed_fraction <= cfg.global_motion_limit:
        return False
    _stat["motion"] = changed_fraction
    return True


def detection_options(region=None):
    rects = json.loads(cfg.exclusion_rects)
    # User masks are relative to the game client, not the moving Ctrl crop.
    if region is not None and _aim["viewport"] is not None:
        vx, vy, vw, vh = _aim["viewport"]
        rx, ry, rw, rh = region
        mapped = []
        if cfg.ignore_bottom_fraction > 0:
            rects = list(rects) + [[0, 1 - cfg.ignore_bottom_fraction, 1, 1]]
        for x1, y1, x2, y2 in rects:
            bounds = (max(0, (vx + x1 * vw - rx) / rw),
                      max(0, (vy + y1 * vh - ry) / rh),
                      min(1, (vx + x2 * vw - rx) / rw),
                      min(1, (vy + y2 * vh - ry) / rh))
            if bounds[0] < bounds[2] and bounds[1] < bounds[3]:
                mapped.append(bounds)
        rects = mapped
    return DetectionOptions(threshold=cfg.diff_threshold, min_area=cfg.min_blob_area,
                            denoise_kernel=cfg.denoise_kernel,
                            max_area_fraction=cfg.max_blob_fraction,
                            max_aspect_ratio=cfg.max_aspect_ratio,
                            ignore_bottom_fraction=(0.0 if region is not None and
                                _aim["viewport"] is not None else cfg.ignore_bottom_fraction),
                            ignore_rects=rects, compensate_camera=cfg.compensate_camera)


def find_blobs_arr(arr1_bgra, arr2_bgra, threshold, min_area, denoise_kernel=0):
    """Compatibility API, backed by the independently testable detector."""
    options = detection_options()
    options.threshold, options.min_area, options.denoise_kernel = threshold, min_area, denoise_kernel
    result = detect_changes(arr1_bgra, arr2_bgra, options)
    _detect["rejected"] = result.rejected
    return result.blobs, result.changed_fraction

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
    primary = _target_tracker.choose(blobs, (offset_x, offset_y), _aim["crosshair"],
                                     time.perf_counter(), radius=cfg.lock_radius,
                                     timeout=cfg.lock_timeout, confirm_frames=cfg.confirm_frames)
    _lock["held"] = _target_tracker.locked
    _lock["ts"] = time.perf_counter()
    _lock["pos"] = ((offset_x + primary["center"][0], offset_y + primary["center"][1])
                    if primary else None)
    if primary is not None:
        # Keep identity for annotation and avoid dict equality on array members.
        for i, blob in enumerate(blobs):
            if blob["bbox"] == primary["bbox"]:
                primary = blobs.pop(i)
                blobs.insert(0, primary)
                break
    return primary

# Побочные данные детекции: нужны только отладочному режиму, в боевом пустые.
_detect = {"rejected": [], "mode": "Margin"}   # mode заполняет главный цикл

# Что показывает живая строка. Заполняется детекцией, читается главным циклом.
_stat = {"target": None, "blobs": 0, "locked": False, "area": 0, "ms": 0.0,
         "motion": 0.0}

def render_status(key_name):
    """Собирает живую строку как куски (текст, стиль).

    Задержка красится по бюджету кадра, залипание на цель — отдельно: это два
    числа, на которые смотрят в первую очередь, когда подбирают настройки.
    """
    way = "наблюдение" if cfg.observe_only else ("SendInput" if _aim["relative"] else "курсор")
    if _stat["motion"]:
        return [(f"[{key_name}]", "cyan"),
                (f" разворот камеры — кадр пропущен ({_stat['motion'] * 100:.0f}% изменилось)",
                 "yellow")]
    if _stat["target"] is None:
        return [(f"[{key_name}]", "cyan"), (" ищу цель…   ", None),
                (_runtime["reason"] + "   ", "dim"),
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


# До этого момента кадрам нельзя верить: мы сами только что дёрнули мышь или
# кликнули, и картинка ещё меняется от нашего же действия.
_settle_until = [0.0]


# Пока не истекло, рамки остаются на экране, а съёмка не идёт: иначе рамка
# попала бы в собственный скриншот, а разглядеть её всё равно не успеваешь —
# при обычном цикле она жила один такт, пару миллисекунд.
_overlay_hold = [0.0]


def hold_overlay(seconds):
    if seconds > 0:
        _overlay_hold[0] = max(_overlay_hold[0], time.perf_counter() + seconds)


def overlay_is_held():
    return time.perf_counter() < _overlay_hold[0]


def preview_region():
    """Показывает квадрат области слежения на margin_preview секунд."""
    if cfg.margin_preview <= 0 or not cfg.overlay_enabled or overlay.error is not None:
        return
    margin = int(cfg.margin)
    cross_x, cross_y = _aim["crosshair"]
    left, top, width, height = clamp_region(cross_x - margin, cross_y - margin,
                                            margin * 2, margin * 2)
    overlay.show_region_and_wait((left, top, left + width, top + height))
    hold_overlay(cfg.margin_preview)


def hold_after_action():
    """Запрещает съёмку, пока не осядет картинка после нашего действия."""
    if cfg.settle_after_action > 0:
        _settle_until[0] = time.perf_counter() + cfg.settle_after_action
    reset_frame_history()
    _baseline_after[0] = max(_baseline_after[0], _settle_until[0])


def frames_are_trustworthy():
    return time.perf_counter() >= _settle_until[0]


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
    length = math.hypot(dx, dy)
    if length > cfg.max_mouse_step:
        scale = cfg.max_mouse_step / length
        dx, dy = int(dx * scale), int(dy * scale)
        step_x, step_y = float(dx), float(dy)
    _move_remainder["x"] = step_x - dx
    _move_remainder["y"] = step_y - dy
    _aim["last_delta"] = (dx, dy)
    send_relative_mouse_move(dx, dy)


_last_click = {"ts": time.perf_counter()}   # не 0: иначе первый же клик проходил всегда


def target_is_centred(x, y):
    """Цель уже в прицеле — можно бить.

    Раньше автоклик жал на каждой детекции, в том числе пока камера ещё
    разворачивалась: удар уходил в воздух, его анимация сама становилась
    изменением на экране, и клики шли без остановки. Теперь бьём, только
    когда цель действительно у прицела.
    """
    if cfg.click_tolerance <= 0:
        return True
    cross_x, cross_y = _aim["crosshair"] if _aim["relative"] else pyautogui.position()
    return (x - cross_x) ** 2 + (y - cross_y) ** 2 <= cfg.click_tolerance ** 2


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
    if cfg.observe_only or not input_allowed():
        return False
    # Camera motion needs a new frame; desktop cursor position can be verified
    # immediately after an absolute move, including partial/smoothed moves.
    aimed = cfg.auto_click and target_is_centred(x, y)

    if _aim["relative"]:
        move_relative_to_target(x, y)
    else:
        move_absolute_to_target(x, y)
        aimed = cfg.auto_click and target_is_centred(x, y)
    hold_after_action()

    if aimed and not _stop.is_set() and input_allowed() and click_is_due():
        pyautogui.click()
        hold_after_action()      # замах и частицы — тоже наше изменение экрана
    return True


def input_allowed():
    if _stop.is_set() or _runtime["paused"] or _tune["paused"]:
        return False
    held_key = _runtime.get("held_key")
    if held_key is not None and not is_key_held(held_key):
        return False
    if cfg.require_game_window:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        return bool(hwnd and hwnd == _aim["hwnd"] and
                    (_selected_hwnd[0] is None or hwnd == _selected_hwnd[0]) and
                    window_matches(window_text(hwnd), process_name(hwnd) if cfg.window_exe else ""))
    return True


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


def process_pair(prev, current, region, prev_ts, current_ts):
    """One shared pipeline; decisions/recordings refer to these exact inputs."""
    started = time.perf_counter()
    _stat["motion"] = 0.0
    options = detection_options(region)
    result = detect_changes(prev, current, options)
    blobs = result.blobs
    _detect["rejected"] = result.rejected
    _runtime["frame_age_ms"] = max(0.0, (started - current_ts) * 1000)
    primary = None
    if result.reason == "camera_unreliable":
        reason = "ненадёжная оценка движения камеры"
        reset_lock()
    elif is_global_motion(result.changed_fraction):
        reason = "движение камеры — кадр пропущен"
        reset_lock()
    else:
        primary = select_target(blobs, region[0], region[1])
        reason = {"searching": "нет подходящей цели", "confirming": "кандидат ещё не подтверждён",
                  "locked": "цель подтверждена"}.get(_target_tracker.state, result.reason)
        if not blobs and result.rejected:
            reason = "изменения отфильтрованы: " + str(result.rejected[0].get("reason", "фильтр"))
    target = ((region[0] + primary["center"][0], region[1] + primary["center"][1])
              if primary else None)
    _runtime["reason"] = reason
    elapsed = (time.perf_counter() - prev_ts) * 1000
    _stat.update(target=target, blobs=len(blobs), locked=_lock["held"],
                 area=primary["area"] if primary else 0, ms=elapsed)
    # Rendering and optional disk output happen before the final input gate.
    # A diagnostic failure pauses the engine rather than terminating capture.
    try:
        capture_tune_frame(blobs, primary, region, _detect["mode"], elapsed, frame=current,
                           mask=result.mask)
        if primary is not None:
            if cfg.overlay_enabled:
                boxes = [(region[0] + x1, region[1] + y1, region[0] + x2, region[1] + y2)
                         for x1, y1, x2, y2 in [b["bbox"] for b in blobs[:MAX_BLOBS_SHOWN]]]
                overlay.show_boxes_and_wait(boxes, primary_index=0)
                if getattr(overlay, "error", None) is not None:
                    raise RuntimeError(str(overlay.error))
                hold_overlay(cfg.box_hold)
            maybe_save_last_frame(current, primary["bbox"], region[2], region[3])
    except Exception as exc:  # diagnostics must not crash the input engine
        _runtime["paused"] = True
        _runtime["error"] = f"Диагностика: {exc}"

    def action_decision():
        if _runtime.get("paused"):
            return False, "paused", "пауза — ввод пропущен"
        if primary is None:
            return False, "no_target", reason
        if cfg.observe_only:
            return False, "observe_only", "наблюдение: " + reason
        if not input_allowed():
            return False, "input_not_allowed", "окно неактивно или остановка — ввод пропущен"
        if time.perf_counter() - current_ts > cfg.max_frame_age:
            return False, "frame_stale", "кадр устарел — ввод пропущен"
        return True, "", reason

    allowed, skip_reason, decision_reason = action_decision()
    recorded, record_token = False, None
    # Raw arrays are copied for every detection, including negative decisions,
    # independently of the preview throttle. The decision is finalized below
    # because even copying a large pair can make it too old for input.
    if cfg.tune_mode:
        try:
            _recorder.max_frames = cfg.tune_frames
            record_token = _recorder.remember(
                prev, current, region,
                {"options": vars(options), "prev_ts": prev_ts, "current_ts": current_ts,
                 "reason": decision_reason, "detection_reason": reason, "target": target,
                 "observe_only": cfg.observe_only, "tracker": _target_tracker.state,
                 "action_allowed": allowed, "skip_reason": skip_reason,
                 "input_requested": False, "decision_pending": True}, mask=result.mask)
            recorded = True
        except Exception as exc:
            _runtime["paused"] = True
            _runtime["error"] = f"Запись кадров: {exc}"
    allowed, skip_reason, decision_reason = action_decision()
    input_requested = False
    if allowed:
        input_requested = True
        # This function rechecks focus/stop again immediately before movement.
        if not move_and_maybe_click(*target):
            allowed, skip_reason = False, "input_not_allowed"
            decision_reason = "ввод отменён — окно неактивно или остановка"
    _runtime["reason"] = decision_reason
    _runtime["frame_age_ms"] = max(0.0, (time.perf_counter() - current_ts) * 1000)
    if recorded:
        try:
            finalize = getattr(_recorder, "update_last_metadata", None)
            if finalize is not None:
                finalize({"reason": decision_reason, "action_allowed": allowed,
                          "skip_reason": skip_reason, "input_requested": input_requested,
                          "decision_pending": False,
                          "frame_age_ms": _runtime["frame_age_ms"]}, token=record_token)
        except Exception as exc:
            _runtime["paused"] = True
            _runtime["error"] = f"Запись решения: {exc}"
    _stat["ms"] = (time.perf_counter() - prev_ts) * 1000
    if cfg.debug_log:
        print(f"[detect] {decision_reason}; {len(blobs)} пятен; {_stat['ms']:.1f} мс; "
              f"возраст кадра {_runtime['frame_age_ms']:.1f} мс")
    return primary is not None


def track_synced(region_left, region_top, region_w, region_h, offset_x, offset_y):
    if overlay_is_held() or not frames_are_trustworthy():
        return False
    overlay.hide_all_and_wait(force=True)
    if overlay.error is not None:
        _runtime["paused"] = True
        _runtime["error"] = str(overlay.error)
        return False
    time.sleep(cfg.hide_settle_delay)
    region = (region_left, region_top, region_w, region_h)
    spec = dict(zip(("left", "top", "width", "height"), region))
    prev_ts = time.perf_counter()
    prev = np.array(main_sct.grab(spec), dtype=np.uint8)
    # Two captures in the same rendered frame are not useful evidence.
    if _stop.wait(cfg.pair_interval):
        return False
    if not input_allowed():
        reset_tracking()
        return False
    current_ts = time.perf_counter()
    current = np.array(main_sct.grab(spec), dtype=np.uint8)
    return process_pair(prev, current, region, prev_ts, current_ts)


def track_fast(region_left, region_top, region_w, region_h, offset_x, offset_y):
    global _last_used_ts
    region = (region_left, region_top, region_w, region_h)
    if _baseline_region[0] != region:
        reset_tracking()
        _baseline_region[0] = region
    if not frames_are_trustworthy():
        _prev_holder["frame"] = None
        return False
    grabber.set_region(region)
    current, ts_now = grabber.get_latest(region)
    now = time.perf_counter()
    if (current is None or ts_now <= _last_used_ts or ts_now < _baseline_after[0]):
        return False
    if now - ts_now > cfg.max_frame_age:
        reset_frame_history()
        _runtime["reason"] = "ожидание свежего кадра"
        return False
    prev, prev_ts = _prev_holder["frame"], _previous_ts[0]
    _prev_holder["frame"], _previous_ts[0] = current, ts_now
    _last_used_ts = ts_now
    if prev is None or prev.shape != current.shape or ts_now - prev_ts > cfg.max_frame_age:
        return False
    return process_pair(prev, current, region, prev_ts, ts_now)

@state_locked
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

    def ms(value):
        return paint(f"{value:.1f} мс", latency_style(value))

    text = "\n".join([
        f"  Захват всего экрана ({screen_width}x{screen_height}): {ms(full_avg)}",
        f"  Захват области {margin*2}x{margin*2} (режим Ctrl):    {ms(small_avg)}",
        f"  Поиск пятен на области {margin*2}x{margin*2} (режим Ctrl): {ms(diff_small)}",
        f"  Поиск пятен на всём экране (режим Alt):        {ms(diff_full)}",
        paint(f"  Зелёное укладывается в кадр 60 Гц ({FRAME_BUDGET_MS:.1f} мс), красное — нет.",
              "dim"),
    ])
    print(text)
    return text


# ---------- Консольное управление настройками ----------
COMMANDS = (
    ("help", "эта страница: команды и все настройки"),
    ("help <имя>", "разбор одной настройки целиком"),
    ("set <имя> <знач>", "изменить настройку"),
    ("window", "к какому окну привязан прицел и как двигается мышь"),
    ("window pick", "выбрать окно игры из списка открытых"),
    ("bench", "замерить скорость захвата и детекции"),
    ("profile", "профили настроек: список и управление"),
    ("reset_config", "стереть файл конфига (спросит подтверждение)"),
)


def print_help():
    """Одна страница вместо прежних help, edit и show.

    Три команды показывали по сути одно и то же разными способами, а вдобавок
    были log/overlay/click/save — каждая дублировала обычный set. Теперь
    список команд и таблица настроек живут на одной странице.
    """
    page("AimTracker", f"v{__version__}")
    print()
    for name, what in COMMANDS:
        print(f"  {paint(name.ljust(18), 'cyan')} {what}")
    print_settings_menu()


BOOL_TRUE = {"1", "on", "true", "yes", "да"}

BOOL_FALSE = {"0", "off", "false", "no", "нет"}

# нижние границы для настроек, которые иначе уронят cv2 (отрицательное ядро и т.п.)
MIN_VALUES = {
    "confirm_frames": 1,
    "max_blob_fraction": 0.001,
    "max_aspect_ratio": 1.0,
    "ignore_bottom_fraction": 0.0,
    "max_frame_age": 0.01,
    "pair_interval": 0.001,
    "max_mouse_step": 1,
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
    "tune_frames": 1,
    "box_hold": 0.0,
    "margin_preview": 0.0,
    "settle_after_action": 0.0,
    "global_motion_limit": 0.0,
    "click_tolerance": 0,
}

# значения-строки: что вообще можно вписать
ALLOWED_VALUES = {
    "aim_mode": ("auto", "absolute", "relative"),
    "color": ("auto", "on", "off"),
}

# верхние границы (нужны только там, где значение — доля)
MAX_VALUES = {
    "diff_threshold": 255,
    "denoise_kernel": 31,
    "confirm_frames": 30,
    "max_blob_fraction": 1.0,
    "max_aspect_ratio": 100.0,
    "ignore_bottom_fraction": 0.9,
    "max_frame_age": 1.0,
    "pair_interval": 0.5,
    "max_mouse_step": 1000,
    "tune_frames": 60,
    "scan_interval": 1.0,
    "click_interval": 60.0,
    "settle_after_action": 5.0,
    "move_smoothing": 1.0,
    "margin": min(center_x, center_y),   # больше — область слежения вылезет за край экрана
    "camera_gain": 5.0,
    "status_hz": 60,
    "global_motion_limit": 1.0,
}


def parse_bool(raw):
    low = raw.lower()
    if low in BOOL_TRUE:
        return True
    if low in BOOL_FALSE:
        return False
    raise ValueError(f"ожидалось on/off, а не {raw!r}")


def coerce_setting(name, raw, key_bindings=None):
    """Приводит значение к типу текущей настройки. Бросает ValueError.

    Раньше в 'set' стоял безусловный float(): 'set denoise_kernel 3' записывал
    3.0, и np.ones((3.0, 3.0)) падал с TypeError прямо в цикле детекции.
    Тем же путём идут значения из файла конфига — его правят руками, так что
    доверия к нему не больше, чем к вводу с клавиатуры.
    """
    current = getattr(cfg, name)
    if isinstance(current, bool):          # bool наследуется от int — проверяем его первым
        if isinstance(raw, str):
            return parse_bool(raw)
        if type(raw) is not bool:
            raise ValueError("ожидалось логическое значение")
        return raw
    if isinstance(current, str):
        value = str(raw).strip()
        if name in KEY_SETTINGS:
            if not key_is_valid(value):
                raise ValueError(f"{value!r} — неизвестная клавиша "
                                 f"(примеры: ctrl, alt, f9, x, mouse4)")
            value = value.lower()
            if name not in ("track_key", "fullscreen_key") and value in MOUSE_KEYS:
                raise ValueError("кнопка мыши переключателем быть не может")
            for other in KEY_SETTINGS:
                bindings = vars(cfg) if key_bindings is None else key_bindings
                if other != name and str(bindings.get(other, "")).lower() == value:
                    raise ValueError(f"клавиша уже используется: {other}")
            return value
        if name == "exclusion_rects":
            rects = json.loads(value)
            if not isinstance(rects, list) or len(rects) > 32:
                raise ValueError("ожидался список из не более 32 прямоугольников")
            for rect in rects:
                if (not isinstance(rect, list) or len(rect) != 4 or
                    any(type(v) not in (int, float) or not math.isfinite(v) for v in rect) or
                    not (0 <= rect[0] < rect[2] <= 1 and 0 <= rect[1] < rect[3] <= 1)):
                    raise ValueError("области: [[лево,верх,право,низ], ...], координаты 0..1")
            return json.dumps(rects)
        allowed = ALLOWED_VALUES.get(name)
        if allowed is None:
            return value                   # свободный текст, например заголовок окна
        if value.lower() not in allowed:
            raise ValueError(f"допустимо: {', '.join(allowed)}")
        return value.lower()
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("число должно быть конечным")
    if isinstance(current, int):
        if not value.is_integer():
            raise ValueError("ожидалось целое число")
        return int(value)
    return value


def bounds_error(name, value):
    """Текст ошибки, если значение вне допустимого диапазона, иначе None."""
    if isinstance(value, (int, float)) and not math.isfinite(value):
        return f"{name}: число должно быть конечным"
    if name in MIN_VALUES and value < MIN_VALUES[name]:
        return f"{name} не может быть меньше {MIN_VALUES[name]}"
    if name in MAX_VALUES and value > MAX_VALUES[name]:
        return f"{name} не может быть больше {MAX_VALUES[name]}"
    return None


@state_locked
def assign_setting(name, value):
    """Кладёт проверенное значение в cfg.

    overlay_enabled идёт только через сеттер: он останавливает и запускает
    фоновый грабер, и запись напрямую развела бы режимы между собой.
    """
    _runtime["dirty"] = True
    if name == "overlay_enabled":
        set_overlay_enabled(value)
        return
    if name == "require_game_window" and value != cfg.require_game_window:
        cfg.observe_only = True
        _runtime["preview"] = None
        _runtime["reason"] = "режим изменён — наблюдение без ввода"
    setattr(cfg, name, value)
    if name in ("window_exe", "window_title"):
        _selected_hwnd[0] = None
    reset_tracking()
    if name in KEY_SETTINGS:
        rebind_hotkeys()        # переключатели должны переехать на новую клавишу
    if name == "margin":
        preview_region()        # наглядно, куда теперь достаёт детекция


@state_locked
def apply_settings_batch(values):
    """Validate a panel submission completely before applying any field."""
    if not isinstance(values, dict) or not values or len(values) > len(vars(cfg)):
        raise ValueError("ожидался непустой набор настроек")
    parsed = {}
    for name, raw in values.items():
        if name not in vars(cfg):
            raise ValueError(f"неизвестная настройка: {name}")
        value = coerce_setting(name, raw, key_bindings={} if name in KEY_SETTINGS else None)
        error = bounds_error(name, value)
        if error:
            raise ValueError(error)
        parsed[name] = value
    bindings = {name: parsed.get(name, getattr(cfg, name)) for name in KEY_SETTINGS}
    if len(set(bindings.values())) != len(bindings):
        raise ValueError("клавиши не должны повторяться")
    changed_keys = any(name in parsed for name in KEY_SETTINGS)
    if changed_keys:
        for name, value in bindings.items():
            setattr(cfg, name, value)
        rebind_hotkeys()
    # Enabling mouse input is the final step after other settings apply.
    for name, value in parsed.items():
        if name not in KEY_SETTINGS and name != "observe_only":
            assign_setting(name, value)
    if "observe_only" in parsed:
        assign_setting("observe_only", parsed["observe_only"])
    _runtime["dirty"] = True
    reset_tracking()


def apply_setting(name, raw):
    if name not in vars(cfg):
        print(f"Нет такой настройки: {name}. Введи 'help' для списка с описаниями.")
        return
    try:
        value = coerce_setting(name, raw)
    except (ValueError, TypeError, OverflowError) as exc:
        print(paint("не принято:", "red"), f"{name} — {exc}")
        return
    error = bounds_error(name, value)
    if error:
        print(paint("не принято:", "red"), error)
        return
    assign_setting(name, value)
    if name != "overlay_enabled":          # сеттер печатает своё сообщение
        print(paint("изменено:", "green"), f"{name} = {format_value(value)}")


# Описания переменных для меню 'edit'. Порядок групп — порядок вывода.
SETTINGS_GROUPS = [
    ("Детекция", (
        ("margin",            "отступ от центра до края области слежения в режиме Ctrl, px"),
        ("diff_threshold",    "порог разницы пикселей 0-255: ниже — чувствительнее, но больше шума"),
        ("min_blob_area",     "минимальная площадь пятна, px: всё мельче отбрасывается"),
        ("denoise_kernel",    "ядро фильтра точечного шума перед поиском пятен, px (0 = выкл)"),
    )),
    ("Клавиши", (
        ("track_key",         "держать для слежения в области у прицела"),
        ("fullscreen_key",    "держать для слежения по всему окну игры"),
        ("autoclick_key",     "переключатель автоклика"),
        ("overlay_key",       "переключатель рамки оверлея"),
        ("panic_key",         "аварийная остановка"),
    )),
    ("Прицел в игре", (
        ("aim_mode",          "auto = относительное движение, когда активно окно игры, иначе абсолютное"),
        ("window_exe",        "имя exe окна игры; задаётся командой 'window pick'"),
        ("window_title",       "запасной способ: ключевое слово в заголовке окна"),
        ("camera_gain",       "доля ошибки за кадр в относительном режиме: больше — резче, но с перелётом"),
        ("settle_after_action", "сколько не снимать кадры после своего движения или клика, сек"),
        ("global_motion_limit", "доля изменившегося кадра, выше которой это разворот камеры, а не цель"),
    )),
    ("Прицел на рабочем столе", (
        ("lock_radius",       "радиус сопоставления с прежней целью, px (0 = без удержания)"),
        ("lock_timeout",      "через сколько секунд без детекций прежняя цель забывается"),
        ("move_smoothing",    "доля пути до цели за кадр: 1.0 = мгновенно, 0.3 = плавно для записи"),
        ("dead_zone",         "не двигать курсор, если цель ближе стольких пикселей"),
        ("move_duration",     "длительность самого движения мыши, сек (0 = мгновенный прыжок)"),
    )),
    ("Скорость", (
        ("scan_interval",     "пауза между итерациями главного цикла, сек"),
        ("hide_settle_delay", "пауза после скрытия рамок перед захватом, сек (только режим с оверлеем)"),
        ("box_hold",          "сколько держать рамки на экране, сек: 0 = мелькают один такт"),
        ("margin_preview",    "сколько показывать квадрат области после смены margin, сек"),
    )),
    ("Режимы и вывод", (
        ("overlay_enabled",   "рамка оверлея: on = синхронно, off = фоновый захват (F7)"),
        ("auto_click",        "кликать после наведения (F6)"),
        ("click_interval",    "минимальная пауза между кликами, сек: 0 = на каждой детекции"),
        ("click_tolerance",   "кликать, только когда цель ближе стольких пикселей к прицелу"),
        ("debug_log",         "подробный таймлог на каждую детекцию; обычно хватает строки состояния"),
        ("status_hz",         "как часто перерисовывать живую строку, раз/с (0 = не рисовать)"),
        ("color",             "цвет в выводе: auto подстраивается под терминал"),
        ("save_last_frame",   "сохранять last.jpg при детекции — запись на диск добавляет задержку"),
        ("autosave",          "сохранять активный профиль при выходе"),
        ("bench_on_boot",     "гонять бенчмарк один раз за загрузку компьютера, а не каждый старт"),
        ("pause_on_start",    "ждать Enter перед началом слежения, чтобы успеть прочитать вывод"),
        ("tune_mode",         "отладочный режим: разметка блобов и кадры на диск"),
        ("tune_frames",       "сколько последних размеченных кадров держать для сброса"),
        ("dump_key",          "клавиша сброса накопленных кадров на диск"),
    )),
]

SETTINGS_GROUPS.append(
    ('Проверка и фильтры',
     (('observe_only', 'наблюдение без движений и кликов'),
      ('require_game_window', 'on = выбранное окно, off = рабочий стол с обычным курсором'),
      ('confirm_frames', 'сколько последовательных детекций подтверждают цель'),
      ('max_blob_fraction', 'максимальная доля полезной области, занятая пятном'),
      ('max_aspect_ratio', 'максимальное отношение длинной стороны пятна к короткой'),
      ('ignore_bottom_fraction', 'исключённая нижняя доля окна игры, 0..0.9'),
      ('exclusion_rects', 'исключённые области окна: JSON-прямоугольники 0..1'),
      ('compensate_camera', 'экспериментальная компенсация общего сдвига'),
      ('max_frame_age', 'предельный возраст кадра для ввода, сек'),
      ('pair_interval', 'интервал захвата кадров, сек'),
      ('max_mouse_step', 'максимальная длина относительного движения за действие')))
)

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
    print("\n  " + paint("НАСТРОЙКИ", "bold")
          + paint("   имя · значение · допустимое", "dim"))
    wrap = max(40, min(console_width() - 1, 78)) - 8
    # ширина колонки от самого длинного имени: при зашитой 18 настройки вроде
    # settle_after_action выходили за неё и сдвигали колонку значений
    name_width = max(len(n) for n in vars(cfg)) + 2
    for title, items in settings_groups_full():
        print("\n  " + paint(title.upper(), "bold"))
        for name, desc in items:
            value = getattr(cfg, name)
            hint = range_hint(name, value)
            # ljust до paint: иначе ширину считали бы вместе с невидимыми кодами
            print(f"    {name:<{name_width}}{paint(format_value(value).ljust(14), 'green')}"
                  f"{paint(hint, 'dim')}")
            for line in textwrap.wrap(desc, wrap) or [""]:
                print(f"        {line}")
    print()


def describe_setting(name):
    if name not in vars(cfg):
        print(f"Нет такой настройки: {name}. Введи 'help' для списка.")
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
    'overlay_enabled': 'ВКЛ (рамка): синхронный захват с ожиданием скрытия рамок и интервалом pair_interval между кадрами. ВЫКЛ (быстрый): фоновые кадры с ограничением частоты захвата. Оба режима используют один детектор. Рамки удобны для настройки, но добавляют задержку. Предпросмотр в панели доступен в обоих режимах.',

    'aim_mode': 'Способ движения внутри выбранного окна: auto/relative используют относительные отсчёты мыши, absolute — обычный курсор. В режиме рабочего стола всегда используется абсолютное движение независимо от этой настройки. observe_only запрещает весь ввод.',

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

    'lock_radius': 'Верхняя граница расстояния для сопоставления новой детекции с прежней целью, px. Учитываются также размер и площадь пятна. Новая цель выбирается по близости к прицелу с ограниченным бонусом площади. 0 отключает удержание, но подтверждение confirm_frames сохраняется.',

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

    "box_hold": """\
Сколько секунд держать рамки найденных пятен на экране. 0 — прежнее
поведение.

При нуле рамка живёт ровно один такт цикла — на обычных настройках это пара
миллисекунд, и разглядеть её невозможно. Дольше держать нельзя просто так:
пока рамка на экране, снимать кадры запрещено, иначе она попадёт в
собственный скриншот, будет засчитана за изменение и начнёт разрастаться.
Поэтому удержание рамок ПРИОСТАНАВЛИВАЕТ детекцию на это время.

Для настройки ставь 0.3-0.5 и смотри, что скрипт считает целью. Для работы
возвращай 0.""",

    "margin_preview": """\
Сколько секунд показывать голубой квадрат области слежения после смены
margin. 0 — не показывать.

Нужно, чтобы видеть глазами, куда достаёт детекция в режиме Ctrl, а не
прикидывать по числу. Как и box_hold, на это время приостанавливает
детекцию: квадрат тоже нельзя снимать вместе с экраном.""",


    "window_exe": """\
Имя исполняемого файла окна игры, например javaw.exe. Пусто — искать по
заголовку (window_title).

Задаётся командой window pick: она показывает открытые окна списком, и ты
выбираешь номером. Лезть в диспетчер задач не нужно — и это важно, потому
что там видны ПРОЦЕССЫ, а у одной программы их бывает несколько, и какой из
них рисует картинку, по списку процессов не понять.

Имя exe надёжнее заголовка: заголовок переписывают лаунчеры и моды, а exe
у игры один и тот же. Если exe задан, window_title не используется.""",

    "window_title": """\
Ключевое слово, по которому ищется окно игры. Пусто — не привязываться к
окну вообще, тогда прицел считается центром экрана.

Это именно подстрока, а не полный заголовок: "Minecraft" подойдёт к
"Minecraft* 1.20.1 - Singleplayer". Лаунчеры и моды часто дописывают к
заголовку своё, поэтому бери короткий кусок, который точно не меняется.

Ищется только среди АКТИВНЫХ окон: целиться в неактивную игру бессмысленно,
а перебор всех окон давал бы ложные совпадения — браузер со словом Minecraft
во вкладке, папка с таким именем. Команда window покажет, что нашлось.""",

    "fullscreen_key": """\
Клавиша, которую держат для слежения по всему окну игры.

В отличие от track_key, область не ограничена квадратом вокруг прицела:
смотрится весь игровой кадр. Детекция от этого заметно дороже, зато цель не
теряется, уйдя за край маленькой области.

Принимает имена клавиш и боковые кнопки мыши (mouse4, mouse5). Про конфликты
с игровыми биндами — см. help track_key.""",

    "autoclick_key": """\
Клавиша-переключатель автоклика.

Это переключатель, а не удержание: нажал — включилось, нажал ещё раз —
выключилось. Кнопки мыши сюда повесить нельзя, только клавиатуру.""",

    "overlay_key": """\
Клавиша-переключатель рамки оверлея, то есть способа захвата экрана.
Подробно про сами режимы — help overlay_enabled.

Переключатель, кнопки мыши не принимает.""",

    "panic_key": """\
Аварийная остановка: скрипт завершает работу.

Нужна отдельно, потому что штатный тормоз pyautogui — увести курсор в угол
экрана — в игре не работает: там курсор по экрану не ездит, мышь двигается
относительными смещениями. Если наводка сошла с ума, это единственный
быстрый способ её прекратить.""",

    "scan_interval": """\
Пауза между итерациями главного цикла, в секундах.

Без паузы цикл сожрал бы ядро процессора целиком, ничего не выигрывая:
узкое место — захват экрана, а не скорость опроса. 0.005 — это 200 проверок
в секунду, чего с запасом хватает при мониторе 60 Гц.

Увеличивать имеет смысл, только если скрипт мешает самой игре.""",

    "min_blob_area": """\
Минимальная площадь пятна в пикселях. Всё мельче отбрасывается.

Работает в паре с diff_threshold и denoise_kernel, но по-другому: порог
решает, СЧИТАЕТСЯ ли пиксель изменившимся, фильтр убирает одиночные шумные
точки, а эта настройка отсекает уже собранные пятна по размеру.

Поднимать, когда скрипт цепляется за мелочь вроде отдельных частиц.
Опускать, если теряет настоящую, но далёкую (а значит мелкую) цель.""",

    "move_duration": """\
Длительность самого движения мыши в absolute-режиме, в секундах.
0 — мгновенный перенос курсора.

Это не сглаживание: pyautogui растягивает ОДИН вызов moveTo на это время,
блокируя цикл. Для плавности есть move_smoothing, который делит путь на
кадры и ничего не блокирует. Держи здесь 0, если нет причины иначе.""",

    "hide_settle_delay": """\
Пауза после скрытия рамок перед захватом кадра, в секундах. Только для
режима с рамкой.

Рамку прячет поток tkinter, и возврат из функции не значит, что окно уже
перерисовалось. Если снять кадр слишком рано, рамка попадёт в скриншот,
будет засчитана за изменение и начнёт разрастаться. Эта пауза — страховка
на случай, если система не успевает.

Если рамки в кадр не попадают, можно пробовать уменьшать: это чистая
прибавка к задержке.""",

    "lock_timeout": """\
Через сколько секунд без детекций прежняя цель забывается.

Работает с lock_radius: пока цель "жива", скрипт держится за неё, а не за
самое крупное пятно. Этот таймер решает, когда считать её потерянной.

Больше — упорнее держится за цель, пережидая паузы в движении. Меньше —
быстрее переключается на новое.""",

    "dead_zone": """\
Не двигать мышь, если цель ближе стольких пикселей.

Гасит микродёрганье, когда цель уже практически в прицеле: без этого
наводка бесконечно подправляет себя на один-два пикселя, и на записи
курсор мелко дрожит.

Не путать с click_tolerance: эта настройка про ДВИЖЕНИЕ, та — про клик.""",

    "debug_log": """\
Печатать подробную строку на каждую детекцию: сколько пятен, площадь цели,
разбивка времени по этапам.

По умолчанию выключено, потому что детекция идёт сотни раз в секунду и лог
заливает консоль так, что работать в ней невозможно. Обычная обратная связь
— живая строка состояния, которая перерисовывается на месте.

Включать, когда надо увидеть, на что именно уходит время.""",

    "save_last_frame": """\
Сохранять last.jpg с вырезанной целью при каждой детекции.

Запись на диск происходит внутри цикла реакции и стоит заметного времени,
поэтому по умолчанию выключено. Для разбора того, что видит скрипт, лучше
подходит отладочный режим — он пишет кадры целиком и с разметкой.""",

    "auto_click": """\
Кликать после наведения. Переключается и клавишей (autoclick_key).

Клик проходит, только когда цель уже в прицеле (click_tolerance) и с
момента прошлого прошло не меньше click_interval. Без этих двух проверок
автоклик входил в самоподдерживающийся цикл: удар по воздуху менял картинку,
изменение засчитывалось за цель, следовал новый удар.""",

    "autosave": """\
Сохранять активный профиль при выходе.

Чтобы подобранное за сессию не пропало. Не срабатывает после
'profile defaults' и 'reset_config': там ты осознанно сбросил настройки, и
сохранение затёрло бы профиль дефолтами или воскресило удалённый файл.""",

    "bench_on_boot": """\
Прогонять замер скорости один раз за загрузку компьютера, а не при каждом
запуске.

Результат сохраняется в конфиг вместе с меткой загрузки системы; следующие
запуски до перезагрузки показывают сохранённые цифры. Выключи, если замер
мешает; перемерить вручную всегда можно командой bench.""",

    'tune_mode': 'Запись точных исходных пар кадров, маски и решений, включая отказы. F9 сохраняет буфер для офлайн-повтора. F8 всегда останавливает программу и ничего не удаляет. Запись может увеличить задержку; предел буфера 256 МиБ.',

    'tune_frames': 'Максимальное число исходных пар кадров в буфере записи, от 1 до 60. Пары сохраняются без потерь. Дополнительно действует предел 256 МиБ, поэтому при большом разрешении история может быть короче.',

    'dump_key': 'F9 сохраняет накопленные исходные пары кадров в новую папку tune/. Если запись выключена, первое нажатие включает её, следующее сохраняет. Старые записи автоматически не удаляются.',

    "pause_on_start": """\
Ждать Enter после стартового вывода, перед началом слежения.

Нужно, если запускаешь двойным кликом и не успеваешь прочитать, что скрипт
написал о конфиге, окне и замерах.""",

    "settle_after_action": """\
Сколько не доверять кадрам после собственного движения мыши или клика,
в секундах. 0 — проверка выключена.

Когда скрипт поворачивает камеру, на экране сдвигается ВСЁ. Если снять кадр
в этот момент, разницы будут везде, скрипт найдёт "цели" по всему экрану и
довернёт ещё сильнее — персонаж начинает крутиться без остановки. Клик даёт
то же самое в меньшем масштабе: анимация замаха сама становится изменением
экрана, и автоклик уходит в самоподдерживающийся цикл.

Поэтому после каждого своего действия скрипт пережидает, пока картинка
осядет. При мониторе 60 Гц один кадр — 16.6 мс, так что осмысленные значения
начинаются примерно с 0.03. Больше значение — надёжнее, но реже наводка.""",

    "global_motion_limit": """\
Доля изменившегося кадра, выше которой изменение считается разворотом
камеры, а не целью. От 0 до 1, 0 — проверка выключена.

Это страховка к settle_after_action. Окно оседания закрывает наши
собственные движения, а эта проверка ловит остальное: ты сам крутанул мышью,
сменилась сцена, рвануло взрывом. При повороте камеры меняется почти каждый
пиксель, у настоящей цели — доли процента.

0.5 значит "больше половины экрана". Ставить сильно ниже опасно: на людной
сцене настоящие изменения тоже бывают обширными, и порог начнёт глотать
полезные детекции.""",

    "click_tolerance": """\
Кликать, только когда цель уже ближе стольких пикселей к прицелу.
0 — бить на каждой детекции, как раньше.

Раньше автоклик жал в тот же момент, когда скрипт только НАЧИНАЛ
разворачиваться к цели. Удар уходил в воздух, а его анимация становилась
новым изменением на экране, которое скрипт принимал за цель — и клики шли
без остановки.

Значение подбирается под размер цели на экране: слишком мало — будешь
ждать идеального наведения и пропускать, слишком много — вернёшься к ударам
в воздух.""",

    "track_key": """\
Клавиша, которую надо держать для слежения в области у прицела.

Принимает имена клавиш (ctrl, alt, shift, f9, x) и боковые кнопки мыши
(mouse4, mouse5).

Про конфликты: по умолчанию стоит ctrl, а в играх он часто забинден на
присед. Тогда при каждом нажатии персонаж садится, экран меняется, и скрипт
принимает это за цель. Если у тебя так — перевесь на то, что в игре не
занято. Боковая кнопка мыши для этого удобнее всего: держать её во время
прицеливания естественно, и с игровыми биндами она почти никогда не
спорит.""",

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



SETTINGS_HELP.update(
    {'observe_only': 'При запуске, загрузке профиля и смене рабочей области включено '
                     'наблюдение. Проверь рамки и маску; set observe_only off разрешает '
                     'ввод в выбранном активном окне или на рабочем столе в зависимости от режима.',
     'require_game_window': 'on: работа в выбранном активном окне; Alt+Tab приостанавливает ввод. off: рабочий стол основного экрана, обычный курсор и абсолютное движение независимо от aim_mode. Переключение включает наблюдение и сбрасывает историю кадров.',
     'confirm_frames': 'Два подтверждения уменьшают одиночные ложные срабатывания. Для '
                       'коротких частиц можно поставить 1 после проверки на записях; большее '
                       'значение повышает задержку.',
     'max_blob_fraction': 'Крупные изменения часто связаны со сценой или рукой. 0.15 '
                          'отбрасывает пятна больше 15% полезной области. Настрой под размер '
                          'нужного события.',
     'max_aspect_ratio': 'Отсекает длинные полосы и границы. Большее значение пропускает '
                         'вытянутые цели. Минимум 1.',
     'ignore_bottom_fraction': 'Например 0.15 скрывает от детекции нижние 15% окна. Маска '
                               'относится к окну игры, а не к квадрату Ctrl; по умолчанию 0.',
     'exclusion_rects': 'Формат [[лево,верх,право,низ], ...]. Например [[0.6,0.6,1,1]] '
                        'исключает правый нижний участок с рукой. Координаты относительны к '
                        'клиентской области игры.',
     'compensate_camera': 'Оценивает общий перенос изображения. При ненадёжной оценке ввод '
                          'запрещён. Не компенсирует перспективу, параллакс и масштаб. По '
                          'умолчанию off; сравнивай на записях.',
     'max_frame_age': 'Кадры старше этого значения не управляют мышью. 0.1 означает 100 мс. '
                      'Отладочная запись и обработка тоже учитываются перед вводом.',
     'pair_interval': '0.016 соответствует примерно 60 парам отсчётов в секунду в быстром '
                      'режиме. Синхронный режим дополнительно ждёт скрытия рамок. Частота '
                      'проверок не равна частоте новых кадров игры.',
     'max_mouse_step': 'Ограничивает резкие развороты. Это отсчёты мыши, не угол камеры; '
                       'зависит от чувствительности игры.'}
)

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


def pick_window():
    """Показывает список открытых окон и даёт выбрать нужное номером.

    Так не приходится лезть в диспетчер задач и гадать, какой из процессов
    игры рисует картинку. Запоминается имя exe: оно переживает смену
    заголовка лаунчером или модом.
    """
    try:
        windows = list_windows()
    except Exception as exc:              # noqa: BLE001 — выбору падать незачем
        print(paint("[Окно]", "red"), f"не удалось перечислить окна: {exc}")
        return
    if not windows:
        print(paint("[Окно]", "yellow"), "открытых окон не найдено; доступен рабочий стол.")

    print()
    for number, (_hwnd, title, exe, (left, top, width, height)) in enumerate(windows, 1):
        short = title if len(title) <= 42 else title[:41] + "…"
        print(f"  {paint(str(number).rjust(2), 'cyan')}  {short:<43}"
              f"{paint(exe or '?', 'green'):<22}{paint(f'{width}x{height}', 'dim')}")
    print(f"\n  {paint('0', 'cyan')}   рабочий стол (обычный курсор, основной экран)")

    try:
        answer = input("\n  Номер окна: ").strip()
    except EOFError:
        return
    if not answer.isdigit() or not 0 <= int(answer) <= len(windows):
        print(paint("[Окно]", "yellow"), "не понял номер, ничего не меняю.")
        return

    choice = int(answer)
    if choice == 0:
        with _state_lock:
            assign_setting("require_game_window", False)
            cfg.window_exe = ""
            cfg.window_title = ""
            _selected_hwnd[0] = None
            reset_tracking()
        print(paint("[Окно]", "green"), "привязка снята, прицел в центре экрана.")
        return

    hwnd, title, exe, _rect = windows[choice - 1]
    with _state_lock:
        if not cfg.require_game_window:
            assign_setting("require_game_window", True)
        _selected_hwnd[0] = hwnd
        cfg.window_exe = exe
        cfg.window_title = title
        _runtime["dirty"] = True
        _window_cache["ts"] = 0.0
        reset_tracking()
        update_aim_context()
    print(paint("[Окно]", "green"), f"выбрано окно {title!r}")


def print_window_info():
    """Диагностика привязки к окну: без неё непонятно, почему не наводится.

    Показывает и активное сейчас окно, и последнее совпавшее: пока ты набираешь
    команду в консоли, активна именно консоль, так что первое почти всегда
    будет "не совпадает" — смотреть надо на второе.
    """
    if cfg.window_exe.strip():
        print(f"\n  Привязка по exe: {paint(cfg.window_exe, 'green')}")
    elif cfg.window_title.strip():
        print(f"\n  Ключевое слово:  {cfg.window_title!r}")
    else:
        print("\n  " + paint("Привязки нет: прицел считается центром экрана.", "yellow"))
    print(f"  {paint('window pick', 'cyan')} — выбрать окно из списка")

    last = _aim["last_match"]
    if not cfg.require_game_window:
        print(f"  Рабочий стол: основной экран {screen_width}x{screen_height}, обычный курсор.")
    elif last is None:
        print("  " + paint("Окно игры ещё ни разу не попадалось.", "yellow"))
        print(f"  Прицел: центр экрана ({center_x}, {center_y}).")
    else:
        # Активное сейчас окно не показываем: пока набираешь команду, активна
        # консоль, и ответ был бы всегда один и тот же. Смысл несёт последнее
        # совпадение и то, насколько оно свежее.
        last_title, (left, top, width, height) = last
        ago = time.perf_counter() - _aim["last_seen"]
        when = "прямо сейчас" if ago < 1.5 else f"{ago:.0f} с назад"
        print(f"  Привязано к:     {last_title!r}")
        print(f"  Видели:          {when}")
        print(f"  Область игры:    {width}x{height} в точке ({left}, {top})")
        print(f"  Прицел:          ({left + width // 2}, {top + height // 2})")

    way = "относительное (SendInput)" if _aim["relative"] else "абсолютное (SetCursorPos)"
    print(f"  Движение мыши:   {way}   (aim_mode={cfg.aim_mode})")
    print("  " + paint("Абсолютное движение игра с raw input не увидит.", "dim") + "\n")




# ---------- Отладочный режим ----------
# Разметка идёт прямо на кадре через OpenCV, а не оверлеем: кадр уже в памяти,
# подписи латиницей (cv2.putText кириллицу не умеет), и результат — файл,
# который можно разглядывать сколько угодно, в отличие от мелькнувшей рамки.

TUNE_TARGET_COLOR = (0, 255, 0)        # BGR: цель
TUNE_CANDIDATE_COLOR = (0, 255, 255)   # подходящие, но не выбранные
TUNE_REJECTED_COLOR = (180, 180, 180)  # отсеянные по min_blob_area
TUNE_REGION_COLOR = (255, 191, 0)      # граница области слежения
TUNE_TEXT_COLOR = (0, 0, 255)
TUNE_JPEG_QUALITY = 80

_tune = {"frames": [], "paused": False, "dumps": 0}


def tune_panel_lines(mode_name, target, area, blobs, reaction_ms):
    """Строки отладочной панели. Только латиница: cv2.putText иначе не нарисует."""
    aim_way = "relative" if _aim["relative"] else "absolute"
    lines = [
        f"AimAssist Ver. {__version__} - {mode_name} - {aim_way}",
        "",
        f"Screen - {screen_width}x{screen_height}",
        f"margin - {int(cfg.margin)}px",
        f"diff_threshold - {cfg.diff_threshold}",
        f"min_blob_area - {cfg.min_blob_area}",
        f"denoise_kernel - {cfg.denoise_kernel}",
        "",
        "Detection",
        f"Target - {target[0]},{target[1]}" if target else "Target - none",
        f"Area - {area} px",
        f"Blobs - {blobs}" + (f" (+{len(_detect['rejected'])} ignored)"
                              if _detect["rejected"] else ""),
        f"Reaction Time - {reaction_ms:.1f} ms",
    ]
    return lines


def annotate_tune_frame(frame_bgra, offset, blobs, primary, region, mode_name, reaction_ms):
    """Рисует разметку на копии кадра и возвращает её.

    offset — куда смещены координаты пятен относительно кадра: детекция может
    смотреть маленькую область, а кадр мы сохраняем целиком.
    """
    canvas = cv2.cvtColor(frame_bgra, cv2.COLOR_BGRA2BGR)
    off_x, off_y = offset

    for blob in _detect["rejected"]:
        x1, y1, x2, y2 = blob["bbox"]
        cv2.rectangle(canvas, (x1 + off_x, y1 + off_y), (x2 + off_x, y2 + off_y),
                      TUNE_REJECTED_COLOR, 1)
    for blob in blobs:
        x1, y1, x2, y2 = blob["bbox"]
        colour = TUNE_TARGET_COLOR if blob is primary else TUNE_CANDIDATE_COLOR
        cv2.rectangle(canvas, (x1 + off_x, y1 + off_y), (x2 + off_x, y2 + off_y), colour, 2)

    if region is not None:
        left, top, width, height = region
        cv2.rectangle(canvas, (left, top), (left + width, top + height),
                      TUNE_REGION_COLOR, 1)

    target = None
    if primary is not None:
        target = (off_x + primary["center"][0], off_y + primary["center"][1])
    lines = tune_panel_lines(mode_name, target,
                             primary["area"] if primary else 0, len(blobs), reaction_ms)
    for row, text in enumerate(lines):
        if not text:
            continue
        cv2.putText(canvas, text, (12, 26 + row * 17), cv2.FONT_HERSHEY_PLAIN,
                    1.0, TUNE_TEXT_COLOR, 1, cv2.LINE_AA)
    return canvas


def remember_tune_frame(canvas):
    """Кладёт размеченный кадр в кольцевой буфер.

    Держим в памяти в JPEG, а не на диске: иначе интересный момент теряется
    среди сотен кадров в секунду, а так ты досматриваешь до странности и
    сбрасываешь ровно её.
    """
    ok, encoded = cv2.imencode(".jpg", canvas,
                               [int(cv2.IMWRITE_JPEG_QUALITY), TUNE_JPEG_QUALITY])
    if not ok:
        return
    _tune["frames"].append(encoded.tobytes())
    limit = max(1, int(cfg.tune_frames))
    del _tune["frames"][:-limit]


def capture_tune_frame(blobs, primary, region, mode_name, reaction_ms, frame=None, mask=None):
    """Render exact detector inputs at at most 10 Hz; recording is separate."""
    if frame is None or not (cfg.tune_mode or _panel is not None):
        return True
    now = time.perf_counter()
    if now < _preview_next[0]:
        return True
    _preview_next[0] = now + 0.1
    try:
        local_region = (0, 0, frame.shape[1], frame.shape[0])
        canvas = annotate_tune_frame(frame, (0, 0), blobs, primary, local_region,
                                     mode_name, reaction_ms)
        if cfg.tune_mode:
            remember_tune_frame(canvas)
        if _panel is None:
            return True
        # Right-hand pane shows the exact binary mask; no PNG work without GUI.
        if mask is not None:
            shown_mask = cv2.cvtColor((mask > 0).astype(np.uint8) * 255, cv2.COLOR_GRAY2BGR)
            canvas = np.concatenate((canvas, shown_mask), axis=1)
        ratio = min(1.0, 900 / canvas.shape[1], 450 / canvas.shape[0])
        if ratio < 1:
            canvas = cv2.resize(canvas, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".png", canvas)
        if not ok:
            raise RuntimeError("кодирование предпросмотра не удалось")
        import base64
        _runtime["preview"] = base64.b64encode(encoded).decode("ascii")
        return True
    except Exception as exc:
        _runtime["paused"] = True
        _runtime["error"] = f"Предпросмотр: {exc}"
        return False

def dump_tune_frames():
    """Пишет накопленные кадры на диск, каждый сброс — в свою папку."""
    if len(_recorder):
        try:
            folder = _recorder.dump(os.path.join(SCRIPT_DIR, TUNE_DIR))
            print(f"[Запись] Исходные пары кадров -> {folder}")
        except (OSError, ValueError) as exc:
            _runtime["error"] = str(exc)
            print(f"[Запись] Не удалось сохранить: {exc}")
        return
    frames = list(_tune["frames"])
    if not frames:
        print(paint("[Отладка]", "yellow"), "кадров пока нет.")
        return
    # отметка времени идёт до секунд, а два сброса подряд укладываются в одну
    # секунду — тогда дописываем номер, иначе второй затёр бы первый
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = os.path.join(SCRIPT_DIR, TUNE_DIR, stamp)
    suffix = 2
    while os.path.exists(folder):
        folder = os.path.join(SCRIPT_DIR, TUNE_DIR, f"{stamp}-{suffix}")
        suffix += 1
    try:
        os.makedirs(folder)
        for number, data in enumerate(frames, 1):
            with open(os.path.join(folder, f"{number:02d}.jpg"), "wb") as handle:
                handle.write(data)
    except OSError as exc:
        print(paint("[Отладка]", "red"), f"не удалось записать: {exc}")
        return
    _tune["dumps"] += 1
    print(paint("[Отладка]", "green"), f"{len(frames)} кадров -> {folder}")


def clear_tune_dir():
    """Стирает всё, что накопилось в папке отладочных кадров."""
    root = os.path.join(SCRIPT_DIR, TUNE_DIR)
    if not os.path.isdir(root):
        print(paint("[Отладка]", "dim"), "папка пуста.")
        return
    try:
        shutil.rmtree(root)
        print(paint("[Отладка]", "green"), f"содержимое {root} удалено.")
    except OSError as exc:
        print(paint("[Отладка]", "red"), f"не удалось удалить: {exc}")


def tune_pause_toggle():
    """Pause/resume never deletes evidence. F8 always stops independently."""
    _tune["paused"] = not _tune["paused"]
    reset_tracking()
    print("[Отладка] " + ("пауза" if _tune["paused"] else "продолжаю"))

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
_config_lock = _state_lock



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
        if data.get("version", 1) != 1:
            raise ValueError("неподдерживаемая версия конфига")
        valid = {}
        for name, values in data["profiles"].items():
            if isinstance(name, str) and name.strip() and isinstance(values, dict):
                valid[name] = values
            else:
                print(f"[Конфиг] Некорректный профиль {name!r} пропущен.")
        data["profiles"] = valid
        if not isinstance(data.get("default_profile", DEFAULT_PROFILE_NAME), str):
            data["default_profile"] = DEFAULT_PROFILE_NAME
        if not isinstance(data.get("bench", {}), dict):
            data["bench"] = {}
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
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(CONFIG_PATH),
                                             prefix=".aim_config-", suffix=".tmp", delete=False) as handle:
                temporary = handle.name
                json.dump(_config["data"], handle, ensure_ascii=False, indent=2, sort_keys=True,
                          allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            if os.path.isfile(CONFIG_PATH):
                shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak")
            os.replace(temporary, CONFIG_PATH)
            return True
        except (OSError, ValueError, TypeError) as exc:
            _runtime["error"] = str(exc)
            print(f"[Конфиг] Не могу записать {CONFIG_PATH}: {exc}")
            return False
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)


@state_locked
def current_values():
    return dict(vars(cfg))


def profile_names():
    return sorted(_config["data"]["profiles"])


@state_locked
def apply_values(values):
    """Загружает набор значений в cfg, пропуская всё, что не проходит проверку.

    Значения идут через те же coerce/bounds, что и команда 'set': файл
    правится руками, и одна опечатка не должна ронять запуск.
    """
    if not isinstance(values, dict):
        print("[Конфиг] Ожидался объект настроек; профиль пропущен.")
        return
    skipped = []
    # Validate hotkeys against their FINAL mapping so profiles may swap keys.
    bindings = {name: getattr(cfg, name) for name in KEY_SETTINGS}
    supplied_keys = False
    for name in KEY_SETTINGS:
        if name in values:
            supplied_keys = True
            try:
                bindings[name] = coerce_setting(name, values[name], key_bindings={})
            except (ValueError, TypeError):
                skipped.append(f"{name} (неподдерживаемая клавиша)")
    if supplied_keys:
        if len(set(bindings.values())) != len(bindings):
            skipped.append("клавиши (повторяющиеся назначения)")
        else:
            for name, value in bindings.items():
                setattr(cfg, name, value)
            rebind_hotkeys()
            reset_tracking()
    for name, raw in values.items():
        if name in KEY_SETTINGS:
            continue
        if name not in vars(cfg):
            skipped.append(f"{name} (нет такой настройки)")
            continue
        try:
            value = coerce_setting(name, raw)
        except (ValueError, TypeError, OverflowError):
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
            _runtime["dirty"] = False
            print(f"[Профиль] Сохранён: {name}")


def profile_load(name):
    with _config_lock:
        values = _config["data"]["profiles"].get(name)
        if values is None:
            print(f"[Профиль] Нет профиля {name!r}. 'profiles' — список.")
            return
        apply_values(builtin_defaults())
        apply_values(values)
        cfg.observe_only = True
        reset_tracking()
        _config["active"] = name
        _config["autosave_ok"] = True
        _runtime["dirty"] = False
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
            replacement = _config["data"]["default_profile"]
            if replacement not in _config["data"]["profiles"]:
                replacement = next(iter(sorted(_config["data"]["profiles"])), DEFAULT_PROFILE_NAME)
            apply_values(builtin_defaults())
            apply_values(_config["data"]["profiles"].get(replacement, {}))
            cfg.observe_only = True
            _config["active"] = replacement
            if replacement not in _config["data"]["profiles"]:
                _config["data"]["profiles"][replacement] = current_values()
            _runtime["dirty"] = False
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
        label = paint(name, "green" if marks else None)
        print(f"  {label}" + (paint(f"   <- {', '.join(marks)}", "dim") if marks else ""))
    print()
    for name, what in (
        ("profile save [имя]", "сохранить текущие настройки в профиль"),
        ("profile load <имя>", "загрузить профиль"),
        ("profile delete <имя>", "удалить профиль (спросит подтверждение)"),
        ("profile default <имя>", "какой профиль грузить при запуске"),
        ("profile defaults", "вернуть встроенные значения из кода"),
        ("reset_config", "стереть файл целиком (спросит подтверждение)"),
    ):
        print(f"  {paint(name.ljust(22), 'cyan')} {what}")
    print()


def run_profile_command(args):
    if not args:
        page("Профили настроек")
        print_profiles_page()
        return
    page("Профили настроек")
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
        cfg.margin = cfg.margin if _gui_requested else ask_margin()
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
                    page("Настройка", parts[1])
                    describe_setting(parts[1])
                else:
                    print_help()
            elif cmd == "bench":
                page("Замер скорости")
                run_benchmark()
            elif cmd == "window":
                if len(parts) > 1 and parts[1].lower() == "pick":
                    page("Выбор окна")
                    pick_window()
                else:
                    page("Привязка к окну")
                    print_window_info()
            elif cmd in ("profile", "profiles"):
                if len(parts) > 1:
                    run_profile_command(parts[1:])
                else:
                    page("Профили настроек")
                    print_profiles_page()
            elif cmd == "reset_config":
                reset_config()
            elif cmd == "set":
                if len(parts) == 1:
                    print("Формат: set <имя> <значение>. 'help' покажет все настройки.")
                else:
                    page("Настройка", parts[1])
                    if len(parts) == 2:
                        describe_setting(parts[1])      # без значения — текущее и описание
                    else:
                        # значение склеиваем: в заголовке окна бывают пробелы
                        apply_setting(parts[1], " ".join(parts[2:]))
                        describe_setting(parts[1])
            else:
                print(f"Неизвестная команда: {cmd}. Введи 'help'.")
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
    print(f"  {key(cfg.track_key.upper()):<22} слежение у прицела"
          f"      {key(cfg.autoclick_key.upper())}  автоклик")
    print(f"  {key(cfg.fullscreen_key.upper()):<22} слежение по окну"
          f"        {key(cfg.overlay_key.upper())}  рамка"
          f"     {key(cfg.panic_key.upper())}  {paint('СТОП', 'red')}")
    print()

    last = _aim["last_match"]
    if not cfg.require_game_window:
        where = f"рабочий стол, основной экран {screen_width}x{screen_height}, обычный курсор"
    elif last is None:
        where = f"окно игры не найдено, прицел в центре экрана ({center_x}, {center_y})"
    else:
        title, (left, top, w, h) = last
        where = f"{title}  {w}x{h} @ ({left}, {top})"
    print(f"  профиль {paint(_config['active'], 'bold')}    {where}")
    mode = "с рамками" if cfg.overlay_enabled else "быстрый (без рамки)"
    click = paint("вкл", "yellow") if cfg.auto_click else "выкл"
    print(f"  захват {paint(mode, 'bold')}    автоклик {click}")
    print()
    print(f"  {paint('help', 'cyan')} — команды   {paint('set <имя>', 'cyan')} — настройки   "
          f"{paint('window', 'cyan')} — привязка к окну")
    print(f"  {paint('help режимы', 'cyan')} — что такое захват   "
          f"{paint('set <имя>', 'cyan')} — разбор настройки")
    print(rule)


def refresh_panel_windows():
    try:
        windows = list_windows()
    except (OSError, ValueError, OverflowError) as exc:
        _runtime["windows_error"] = str(exc)
        return
    _runtime["windows"] = [{"id": hwnd, "title": title, "exe": exe}
                           for hwnd, title, exe, _rect in windows]
    errors = _runtime.get("window_errors", [])
    _runtime["windows_error"] = (f"Не удалось прочитать окон: {len(errors)}" if errors else "")


@state_locked
def handle_engine_command(command):
    """UI and hotkeys enqueue work; changes apply between detection iterations."""
    kind = command.get("command")
    if kind != "refresh":
        _runtime["error"] = ""
    try:
        if kind == "set_many":
            apply_settings_batch(command.get("settings"))
        elif kind == "set":
            name = command.get("name")
            if name not in vars(cfg):
                raise ValueError("неизвестная настройка")
            value = coerce_setting(name, command.get("value"))
            error = bounds_error(name, value)
            if error:
                raise ValueError(error)
            assign_setting(name, value)
        elif kind == "set_mode":
            mode = command.get("mode")
            if mode not in ("window", "desktop"):
                raise ValueError("режим должен быть window или desktop")
            apply_settings_batch({"require_game_window": mode == "window", "observe_only": True})
            update_aim_context()
        elif kind == "select_window":
            hwnd = command.get("id")
            window = next((w for w in _runtime["windows"] if str(w["id"]) == str(hwnd)), None)
            if window is None:
                raise ValueError("окно уже недоступно; обновите список")
            if not cfg.require_game_window:
                assign_setting("require_game_window", True)
            _selected_hwnd[0] = window["id"]
            cfg.window_exe, cfg.window_title = window["exe"], window["title"]
            _runtime["dirty"] = True
            reset_tracking()
        elif kind == "profile_load":
            name = str(command.get("name", ""))
            if name not in profile_names():
                raise ValueError("профиль не найден")
            profile_load(name)
        elif kind == "profile_save":
            name = str(command.get("name", "")).strip()
            if not name or len(name) > 80 or any(c in name for c in "\r\n"):
                raise ValueError("имя профиля: от 1 до 80 символов")
            profile_save(name)
        elif kind in ("pause", "resume"):
            _runtime["paused"] = kind == "pause"
            _tune["paused"] = False
            reset_tracking()
        elif kind == "stop":
            panic_stop()
        elif kind == "record":
            if not cfg.tune_mode:
                cfg.tune_mode = True
                _runtime["dirty"] = True
                _runtime["reason"] = "запись включена; F9 — сохранить буфер"
            else:
                dump_tune_frames()
        elif kind == "toggle_click":
            toggle_autoclick()
            _runtime["dirty"] = True
        elif kind == "toggle_overlay":
            set_overlay_enabled(not cfg.overlay_enabled)
        elif kind == "refresh":
            refresh_panel_windows()
        else:
            raise ValueError("неизвестная команда панели")
    except (ValueError, TypeError, OSError, OverflowError) as exc:
        _runtime["error"] = str(exc)


@state_locked
def publish_panel():
    if _panel is None:
        return
    status = ("пауза" if _runtime["paused"] or _tune["paused"] else
              "окно неактивно" if cfg.require_game_window and _aim["viewport"] is None else
              "наблюдение" if cfg.observe_only else "слежение")
    _panel.publish({"status": status, "profile": _config["active"],
                    "profiles": profile_names(), "settings": current_values(),
                    "settings_info": SETTINGS_INFO,
                    "windows": _runtime["windows"], "dirty": _runtime["dirty"],
                    "window_id": _selected_hwnd[0] or _aim["hwnd"],
                    "work_mode": "window" if cfg.require_game_window else "desktop",
                    "windows_error": _runtime.get("windows_error", ""),
                    "target": _stat["target"], "reason": _runtime["reason"],
                    "latency_ms": _stat["ms"], "frame_age_ms": _runtime["frame_age_ms"],
                    "preview": _runtime["preview"], "error": _runtime["error"]})


def intersect_region(region, viewport):
    if viewport is None:
        return region
    x, y, w, h = region
    vx, vy, vw, vh = viewport
    x1, y1 = max(x, vx), max(y, vy)
    x2, y2 = min(x + w, vx + vw), min(y + h, vy + vh)
    return (x1, y1, max(1, x2 - x1), max(1, y2 - y1))


_vt["ok"] = enable_vt()     # до любого цветного вывода: иначе первые строки выйдут серыми
startup_load_config()
cfg.observe_only = True  # every launch starts without mouse input, including old profiles
_runtime["dirty"] = False
set_overlay_enabled(cfg.overlay_enabled)  # запускает грабер, если оверлей выключен
rebind_hotkeys()            # переключатели вешаются на клавиши из настроек
update_aim_context()        # чтобы шапка показала реальную привязку, а не "окно не найдено"
print_header()
# Бенчмарк — до запуска консоли: иначе команда из консоли (например
# reset_config) успевала отработать в середине замера, а завершавшийся
# следом бенчмарк своим save_config_file() воскрешал только что удалённый файл.
maybe_run_startup_benchmark()

if cfg.pause_on_start and not _gui_requested:
    # до старта консольного потока: иначе он перехватил бы этот же Enter
    try:
        input("\n[Пауза] Enter — начать слежение ('set pause_on_start off' уберёт паузу)...")
    except EOFError:
        pass

if _gui_requested:
    _panel = PanelBridge(os.path.join(SCRIPT_DIR, "aim_ui.py"), sys.executable).start()
    refresh_panel_windows()
    publish_panel()
else:
    threading.Thread(target=console_loop, daemon=True).start()


_status_next = [0.0]
_was_tracking = [False]
_panel_next = 0.0
_was_active = False

try:
    while not _stop.is_set():
        if _panel is not None:
            for command in _panel.drain():
                handle_engine_command(command)
            if not _panel.alive:
                panic_stop()
        for _ in range(32):
            try:
                handle_engine_command(_engine_commands.get_nowait())
            except queue.Empty:
                break
        if _stop.is_set():
            break
        found = False
        tracking_key = None
        _runtime["held_key"] = None
        # прицел и способ движения пересчитываем каждую итерацию: окно игры
        # можно свернуть, подвинуть или переключиться из него в другое
        viewport = update_aim_context()

        if _runtime["paused"] or _tune["paused"]:
            _runtime["reason"] = "пауза"
            tracking_key = None
        elif cfg.require_game_window and viewport is None:
            _runtime["reason"] = "выбранное окно игры неактивно"
            tracking_key = None
        elif is_key_held(cfg.track_key):
            tracking_key = cfg.track_key.capitalize()
            _runtime["held_key"] = cfg.track_key
            _detect["mode"] = "Margin"
            # снимок на итерацию: margin правится из консоли прямо во время работы
            margin = int(cfg.margin)
            cross_x, cross_y = _aim["crosshair"]
            left, top, width, height = clamp_region(
                cross_x - margin, cross_y - margin, margin * 2, margin * 2)
            left, top, width, height = intersect_region((left, top, width, height), viewport)
            found = track(left, top, width, height, offset_x=left, offset_y=top)
        elif is_key_held(cfg.fullscreen_key):
            tracking_key = cfg.fullscreen_key.capitalize()
            _runtime["held_key"] = cfg.fullscreen_key
            _detect["mode"] = "Fullscreen"
            # когда игра идёт в окне, незачем разглядывать остальной рабочий стол
            if viewport is None:
                left, top, width, height = 0, 0, screen_width, screen_height
            else:
                left, top, width, height = clamp_region(*viewport)
            found = track(left, top, width, height, offset_x=left, offset_y=top)

        if tracking_key is None:
            if _was_active:
                reset_tracking()
            _was_active = False
            _stat["motion"] = 0.0
        else:
            _was_active = True
        if grabber.error:
            _runtime["paused"] = True
            _runtime["error"] = "Захват экрана: " + grabber.error
        if _panel is not None and time.perf_counter() >= _panel_next:
            _panel_next = time.perf_counter() + 0.15
            publish_panel()
        if not found and cfg.overlay_enabled and not overlay_is_held():
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
    try:
        grabber.stop()
    except RuntimeError as exc:
        print(f"[Захват] {exc}")
    if _panel is not None:
        _panel.close()
    close_capture = getattr(main_sct, "close", None)
    if close_capture is not None:
        close_capture()
    if cfg.autosave and _config["data"] is not None and _config["autosave_ok"]:
        profile_save(_config["active"])
