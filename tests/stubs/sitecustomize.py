"""Заглушки Windows/GUI-зависимостей, чтобы прогнать aim_tracker.py на Linux."""
import sys, types, ctypes, io, os

# заглушки эмулируют именно Windows — иначе скрипт справедливо откажется стартовать
sys.platform = "win32"

# --- pyautogui ---
pyautogui = types.ModuleType("pyautogui")
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1
pyautogui.size = lambda: (1680, 1050)
_cursor = [840, 525]
MOVES = []
class FailSafeException(Exception): pass
pyautogui.FailSafeException = FailSafeException
def _moveTo(x, y, duration=0.0):
    if (int(x), int(y)) in {(0,0),(1679,0),(0,1049),(1679,1049)}:
        raise FailSafeException("corner!")
    _cursor[0], _cursor[1] = int(x), int(y)
    MOVES.append((int(x), int(y)))
pyautogui.moveTo = _moveTo
pyautogui.position = lambda: (_cursor[0], _cursor[1])
pyautogui.click = lambda *a, **k: None
pyautogui._cursor = _cursor
pyautogui.MOVES = MOVES
sys.modules["pyautogui"] = pyautogui

# --- keyboard ---
keyboard = types.ModuleType("keyboard")
keyboard.HOTKEYS = []
def _add_hotkey(name, action, *a, **k):
    keyboard.HOTKEYS.append(name)
    return name
keyboard.add_hotkey = _add_hotkey
def _remove_hotkey(handle):
    if handle in keyboard.HOTKEYS: keyboard.HOTKEYS.remove(handle)
keyboard.remove_hotkey = _remove_hotkey
VALID_KEYS = {"ctrl", "alt", "shift", "tab", "space", "x", "z", "v"} | {f"f{i}" for i in range(1, 13)}
def _key_to_scan_codes(name):
    if name not in VALID_KEYS: raise ValueError(f"unknown key {name!r}")
    return (1,)
keyboard.key_to_scan_codes = _key_to_scan_codes
keyboard.HELD = set()          # какие клавиши тест считает зажатыми
keyboard.PRESSES = {"ctrl": 0, "alt": 0}
keyboard.LIMIT = [0]
keyboard.EOF = [False]
# Для тестов в отдельном процессе: выйти из главного цикла после N опросов.
# Внутрипроцессные тесты обходятся флагом EOF, но подпроцесс так не остановить.
_EXIT_AFTER = os.environ.get("AIMTRACKER_TEST_EXIT_AFTER")


def _is_pressed(key):
    """Держит главный цикл живым, пока поток консоли не дочитает stdin."""
    if _EXIT_AFTER is not None:
        keyboard.LIMIT[0] += 1
        if keyboard.LIMIT[0] > int(_EXIT_AFTER):
            raise KeyboardInterrupt
        return key == "ctrl"
    if key != "ctrl":
        return False
    if keyboard.LIMIT[0] < keyboard.PRESSES["ctrl"]:
        keyboard.LIMIT[0] += 1
        return True
    if keyboard.EOF[0]:
        raise KeyboardInterrupt
    return False
keyboard.is_pressed = _is_pressed
class EofFlagStdin(io.StringIO):
    """stdin, который поднимает флаг, когда всё прочитано."""
    def readline(self, *a):
        line = super().readline(*a)
        if line == "":
            keyboard.EOF[0] = True
        return line
keyboard.EofFlagStdin = EofFlagStdin
sys.modules["keyboard"] = keyboard

# --- mss: синтетические кадры ---
import numpy as np
mss = types.ModuleType("mss")
FRAME_N = [0]
mss.GRABS = []
def _make_frame(w, h):
    """Кадр с двумя пятнами: мелкое дрожит на 3px, крупное скачет."""
    a = np.zeros((h, w, 4), dtype=np.uint8)
    n = FRAME_N[0]
    sx, sy = 60 + (n % 2) * 3, 60
    if sy + 8 <= h and sx + 8 <= w:
        a[sy:sy+8, sx:sx+8] = 255
    bx = 20 if (n // 2) % 2 == 0 else max(w - 40, 0)
    if h >= 40:
        a[h-40:h-10, bx:bx+30] = 255
    FRAME_N[0] += 1
    return a
class _MSS:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def grab(self, region):
        mss.GRABS.append((region["left"], region["top"], region["width"], region["height"]))
        return _make_frame(region["width"], region["height"])
mss.MSS = _MSS
mss.mss = _MSS
sys.modules["mss"] = mss

# --- tkinter: no-op окно ---
tk = types.ModuleType("tkinter")
class _Canvas:
    def __init__(self, *a, **k): pass
    def pack(self, *a, **k): pass
    def create_rectangle(self, *a, **k): return 1
    def coords(self, *a, **k): pass
    def itemconfig(self, *a, **k): pass
class _Tk:
    def overrideredirect(self, *a): pass
    def attributes(self, *a): pass
    def geometry(self, *a): pass
    def config(self, **k): pass
    def winfo_id(self): return 0
    def update_idletasks(self): pass
    def update(self): pass
    def after(self, ms, fn): fn()
    def mainloop(self):
        import time
        while True: time.sleep(0.05)
tk.Tk, tk.Canvas = _Tk, _Canvas
sys.modules["tkinter"] = tk

# --- ctypes.windll ---
WIN = {
    "hwnd": 4242,
    "title": "Minecraft* 1.20.1 - Singleplayer",
    "client": (100, 50, 800, 600),
    "sent": [],
    # список окон для EnumWindows: hwnd -> (заголовок, exe, left, top, w, h)
    "windows": {
        4242: ("Minecraft* 1.20.1 - Singleplayer", "javaw.exe", 100, 50, 800, 600),
        7: ("Блокнот", "notepad.exe", 0, 0, 400, 300),
        8: ("крошечное окно", "tray.exe", 0, 0, 50, 50),
        9: ("", "noname.exe", 0, 0, 900, 700),
    },
}
class _SendInput:
    """Вызывается как функция, но допускает присваивание argtypes/restype."""
    argtypes = None
    restype = None
    def __call__(self, count, pointer, size):
        mi = pointer._obj.union.mi
        WIN["sent"].append((mi.dx, mi.dy, mi.dwFlags))
        return count
class _U32:
    def __init__(self): self.SendInput = _SendInput()
    def GetParent(self, h): return 0
    def GetWindowLongW(self, h, i): return 0
    def SetWindowLongW(self, h, i, v): return 0
    MOUSE_DOWN = set()         # какие боковые кнопки мыши тест считает зажатыми
    def GetAsyncKeyState(self, code):
        return 0x8000 if code in self.MOUSE_DOWN else 0
    def GetForegroundWindow(self): return WIN["hwnd"]
    def IsWindowVisible(self, h): return 1 if h in WIN["windows"] else 0
    def EnumWindows(self, callback, lparam):
        for h in WIN["windows"]:
            if not callback(h, lparam): break
        return 1
    def GetWindowThreadProcessId(self, h, ref):
        ref._obj.value = 1000 + (h if isinstance(h, int) else 0)
        return 1
    def _title(self, h):
        if h == WIN["hwnd"]: return WIN["title"]
        entry = WIN["windows"].get(h)
        return entry[0] if entry else ""
    def _rect(self, h):
        if h == WIN["hwnd"]: return WIN["client"]
        entry = WIN["windows"].get(h)
        return entry[2:6] if entry else None
    def GetWindowTextLengthW(self, h): return len(self._title(h))
    def GetWindowTextW(self, h, buf, n):
        buf.value = self._title(h)[:n - 1]
        return len(buf.value)
    def GetClientRect(self, h, ref):
        rect = self._rect(h)
        if rect is None: return 0
        r = ref._obj; r.left = r.top = 0
        r.right, r.bottom = rect[2], rect[3]
        return 1
    def ClientToScreen(self, h, ref):
        rect = self._rect(h)
        if rect is None: return 0
        pt = ref._obj
        pt.x += rect[0]; pt.y += rect[1]
        return 1
class _K32:
    TICKS = [3600_000]
    VT_OK = [True]              # тест может сделать консоль "не умеющей ANSI"
    MODE = [0]
    def GetTickCount64(self): return self.TICKS[0]
    def GetStdHandle(self, which): return 7
    def OpenProcess(self, access, inherit, pid): return pid
    def CloseHandle(self, handle): return 1
    def QueryFullProcessImageNameW(self, handle, flags, buf, size):
        hwnd = handle - 1000
        entry = WIN["windows"].get(hwnd)
        if not entry: return 0
        buf.value = "C:\\games\\" + entry[1]
        return 1
    def GetConsoleMode(self, handle, ref):
        if not self.VT_OK[0]: return 0
        ref._obj.value = self.MODE[0]
        return 1
    def SetConsoleMode(self, handle, mode):
        if not self.VT_OK[0]: return 0
        self.MODE[0] = mode
        return 1
class _WinDLL:
    user32 = _U32()
    kernel32 = _K32()
    WIN = WIN
ctypes.windll = _WinDLL()

# --- PIL ---
PIL = types.ModuleType("PIL"); Image = types.ModuleType("PIL.Image")
class _Img:
    def save(self, *a, **k): pass
Image.fromarray = lambda *a, **k: _Img()
PIL.Image = Image
sys.modules["PIL"] = PIL; sys.modules["PIL.Image"] = Image
