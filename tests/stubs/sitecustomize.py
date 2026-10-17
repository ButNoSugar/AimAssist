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
keyboard.add_hotkey = lambda *a, **k: None
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
    def GetForegroundWindow(self): return WIN["hwnd"]
    def GetWindowTextLengthW(self, h): return len(WIN["title"]) if h == WIN["hwnd"] else 0
    def GetWindowTextW(self, h, buf, n):
        buf.value = (WIN["title"] if h == WIN["hwnd"] else "")[:n - 1]
        return len(buf.value)
    def GetClientRect(self, h, ref):
        if h != WIN["hwnd"]: return 0
        r = ref._obj; r.left = r.top = 0
        r.right, r.bottom = WIN["client"][2], WIN["client"][3]
        return 1
    def ClientToScreen(self, h, ref):
        if h != WIN["hwnd"]: return 0
        pt = ref._obj
        pt.x += WIN["client"][0]; pt.y += WIN["client"][1]
        return 1
class _K32:
    TICKS = [3600_000]
    VT_OK = [True]              # тест может сделать консоль "не умеющей ANSI"
    MODE = [0]
    def GetTickCount64(self): return self.TICKS[0]
    def GetStdHandle(self, which): return 7
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
