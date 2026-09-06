import ctypes
import threading
import time

import cv2
import keyboard
import mss
import numpy as np
import pyautogui
import tkinter as tk
from PIL import Image

pyautogui.FAILSAFE = True  # двинь мышь в угол экрана, чтобы аварийно остановить pyautogui
pyautogui.PAUSE = 0  # ВАЖНО: по умолчанию pyautogui сам добавляет 0.1с паузы после КАЖДОГО
                     # вызова (moveTo, click...) — это и было основным источником задержки

TRACK_KEY = "ctrl"
FULLSCREEN_KEY = "alt"
AUTOCLICK_TOGGLE_KEY = "f6"
OVERLAY_TOGGLE_KEY = "f7"

PRIMARY_COLOR = "#00FF00"
SECONDARY_COLOR = "#FFA500"
CROP_PADDING = 15
MAX_BLOBS_SHOWN = 24


# ---------- Настройки, изменяемые на лету через консоль ----------
class Config:
    def __init__(self):
        self.scan_interval = 0.005      # пауза между итерациями главного цикла (сек)
        self.diff_threshold = 30        # порог чувствительности разницы пикселей (0-255)
        self.min_blob_area = 4          # мин. площадь пятна в пикселях (отсекает шум)
        self.move_duration = 0.0        # плавность движения мыши (0 = мгновенно, быстрее всего)
        self.hide_settle_delay = 0.02   # пауза после скрытия рамок (только режим с оверлеем)
        self.denoise_kernel = 3         # размер ядра фильтра шума перед поиском пятен (0 = выкл)
        self.debug_log = True           # печатать таймлоги каждой детекции
        self.save_last_frame = False    # сохранять last.jpg (лишняя запись на диск = задержка)
        self.auto_click = False
        self.overlay_enabled = True     # True = точный, но медленный синхронный режим
                                         # False = быстрый режим с фоновым захватом экрана


cfg = Config()


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
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self._ready.wait()

    def _run(self):
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
        self._ready.set()
        self.root.mainloop()

    def _make_click_through(self):
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        styles = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, styles | WS_EX_LAYERED | WS_EX_TRANSPARENT)

    def _run_synced(self, draw_fn):
        done = threading.Event()

        def _job():
            draw_fn()
            self.root.update_idletasks()
            self.root.update()
            done.set()

        self.root.after(0, _job)
        done.wait(timeout=0.2)

    def hide_all_and_wait(self):
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


# ---------- Фоновый непрерывный захват экрана (используется когда оверлей выключен) ----------
class FrameGrabber:
    """
    Крутится в отдельном потоке и без остановки грабит весь экран через mss,
    складывая последний кадр в self.latest. Главный цикл просто читает уже
    готовые кадры вместо того, чтобы блокирующе ждать новый скриншот —
    это и даёт основной прирост скорости в режиме без оверлея.

    ВАЖНО: занимает одно ядро CPU практически полностью, пока запущен.
    Поэтому стартует только когда оверлей выключен, и останавливается,
    когда его включают обратно.
    """

    def __init__(self, width, height):
        self.monitor = {"left": 0, "top": 0, "width": width, "height": height}
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

    def _run(self):
        with mss.MSS() as sct:
            while self._running:
                t0 = time.perf_counter()
                shot = sct.grab(self.monitor)
                arr = np.array(shot, dtype=np.uint8)
                t1 = time.perf_counter()
                with self.lock:
                    self.latest = arr
                    self.latest_ts = t1
                    self.capture_time_ms = (t1 - t0) * 1000

    def get_latest(self):
        with self.lock:
            return self.latest, self.latest_ts


# ---------- Инициализация ----------
screen_width, screen_height = pyautogui.size()
center_x, center_y = screen_width // 2, screen_height // 2
overlay = Overlay(screen_width, screen_height)
grabber = FrameGrabber(screen_width, screen_height)
main_sct = mss.MSS()  # для синхронного режима (используется только из главного потока)

_prev_holder = {"frame": None}
_last_used_ts = 0.0


def set_overlay_enabled(enabled):
    global _last_used_ts
    cfg.overlay_enabled = enabled
    if enabled:
        grabber.stop()
    else:
        overlay.hide_all_and_wait()
        _prev_holder["frame"] = None
        _last_used_ts = 0.0
        grabber.start()
    print(f"[Оверлей] {'ВКЛ (точнее, но медленнее)' if enabled else 'ВЫКЛ (быстрый режим, фоновый захват)'}")


def toggle_autoclick():
    cfg.auto_click = not cfg.auto_click
    print(f"[Автоклик] {'включен' if cfg.auto_click else 'выключен'}")


keyboard.add_hotkey(AUTOCLICK_TOGGLE_KEY, toggle_autoclick)
keyboard.add_hotkey(OVERLAY_TOGGLE_KEY, lambda: set_overlay_enabled(not cfg.overlay_enabled))

margin = int(input("Введите отступ от центра экрана (для режима Ctrl): "))
set_overlay_enabled(cfg.overlay_enabled)  # запускает грабер, если оверлей изначально выключен


# ---------- Детекция ----------
def find_blobs_arr(arr1_bgr, arr2_bgr, threshold, min_area, denoise_kernel=0):
    """То же самое, что раньше, но принимает готовые numpy-массивы (без PIL).

    denoise_kernel > 0 применяет морфологическое "открытие" к маске перед
    разметкой связных областей — убирает единичные шумные пиксели ДО того,
    как cv2 начнёт размечать их по одному. Без этого сцена с постоянным
    мелким шумом (анимация воды, рука с предметом и т.п.) может давать
    сотни-тысячи крошечных пятен, и сама разметка становится дорогой —
    именно это было причиной скачков diff до 75-100мс.
    """
    diff = np.abs(arr1_bgr.astype(np.int16) - arr2_bgr.astype(np.int16)).max(axis=2)
    mask = (diff > threshold).astype(np.uint8)
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


def move_and_maybe_click(x, y):
    pyautogui.moveTo(x, y, duration=cfg.move_duration)
    if cfg.auto_click:
        pyautogui.click()


def maybe_save_last_frame(arr_bgr, bbox, region_w, region_h):
    if not cfg.save_last_frame:
        return
    x1, y1, x2, y2 = bbox
    crop = arr_bgr[
        max(y1 - CROP_PADDING, 0):min(y2 + CROP_PADDING, region_h),
        max(x1 - CROP_PADDING, 0):min(x2 + CROP_PADDING, region_w),
    ]
    Image.fromarray(crop[:, :, ::-1]).save("last.jpg")  # BGR -> RGB для PIL


def track_synced(region_left, region_top, region_w, region_h, offset_x, offset_y):
    """Точный, но медленный режим: рамка синхронно прячется/показывается вокруг захвата."""
    overlay.hide_all_and_wait()
    time.sleep(cfg.hide_settle_delay)

    region = {"left": region_left, "top": region_top, "width": region_w, "height": region_h}
    t0 = time.perf_counter()
    arr1 = np.array(main_sct.grab(region), dtype=np.uint8)[:, :, :3]
    arr2 = np.array(main_sct.grab(region), dtype=np.uint8)[:, :, :3]
    t1 = time.perf_counter()

    blobs = find_blobs_arr(arr1, arr2, cfg.diff_threshold, cfg.min_blob_area, cfg.denoise_kernel)
    t2 = time.perf_counter()

    if not blobs:
        return False

    blobs.sort(key=lambda b: b["area"], reverse=True)
    primary = blobs[0]
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

    if cfg.debug_log:
        print(
            f"[sync] пятен={len(blobs)} area={primary['area']}px -> ({screen_cx},{screen_cy}) | "
            f"захват={((t1 - t0) * 1000):.1f}мс diff={((t2 - t1) * 1000):.1f}мс "
            f"оверлей={((t3 - t2) * 1000):.1f}мс move={((t4 - t3) * 1000):.1f}мс "
            f"| итого={((t4 - t0) * 1000):.1f}мс"
        )
    return True


def track_fast(region_left, region_top, region_w, region_h, offset_x, offset_y):
    """Быстрый режим: без оверлея, кадры уже лежат готовые от фонового FrameGrabber."""
    global _last_used_ts

    current, ts_now = grabber.get_latest()
    if current is None or ts_now == _last_used_ts:
        return False  # фоновый поток ещё не успел снять новый кадр

    prev = _prev_holder["frame"]
    _prev_holder["frame"] = current
    _last_used_ts = ts_now
    if prev is None or prev.shape != current.shape:
        return False

    t0 = time.perf_counter()
    crop1 = prev[region_top:region_top + region_h, region_left:region_left + region_w, :3]
    crop2 = current[region_top:region_top + region_h, region_left:region_left + region_w, :3]
    blobs = find_blobs_arr(crop1, crop2, cfg.diff_threshold, cfg.min_blob_area, cfg.denoise_kernel)
    t1 = time.perf_counter()

    if not blobs:
        return False

    blobs.sort(key=lambda b: b["area"], reverse=True)
    primary = blobs[0]
    local_cx, local_cy = primary["center"]
    screen_cx, screen_cy = offset_x + local_cx, offset_y + local_cy
    maybe_save_last_frame(crop2, primary["bbox"], region_w, region_h)
    move_and_maybe_click(screen_cx, screen_cy)
    t2 = time.perf_counter()

    if cfg.debug_log:
        print(
            f"[fast] пятен={len(blobs)} area={primary['area']}px -> ({screen_cx},{screen_cy}) | "
            f"diff={((t1 - t0) * 1000):.1f}мс move={((t2 - t1) * 1000):.1f}мс "
            f"| захват фоном ~{grabber.capture_time_ms:.1f}мс/кадр"
        )
    return True


def track(region_left, region_top, region_w, region_h, offset_x, offset_y):
    if cfg.overlay_enabled:
        return track_synced(region_left, region_top, region_w, region_h, offset_x, offset_y)
    return track_fast(region_left, region_top, region_w, region_h, offset_x, offset_y)


# ---------- Бенчмарк ----------
def run_benchmark(samples=30):
    print("Замер производительности (может занять секунду)...")
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

    dummy1 = np.random.randint(0, 255, (margin * 2, margin * 2, 3), dtype=np.uint8)
    dummy2 = np.random.randint(0, 255, (margin * 2, margin * 2, 3), dtype=np.uint8)
    t0 = time.perf_counter()
    for _ in range(samples):
        find_blobs_arr(dummy1, dummy2, cfg.diff_threshold, cfg.min_blob_area, cfg.denoise_kernel)
    diff_avg = (time.perf_counter() - t0) / samples * 1000

    print(f"  Захват всего экрана ({screen_width}x{screen_height}): {full_avg:.1f} мс")
    print(f"  Захват области {margin*2}x{margin*2} (режим Ctrl):    {small_avg:.1f} мс")
    print(f"  Поиск пятен (cv2) на области {margin*2}x{margin*2}:    {diff_avg:.1f} мс")
    print("  Это ориентировочный потолок скорости на твоём железе для этих операций.")


# ---------- Консольное управление настройками ----------
def print_help():
    print(
        "\nКоманды консоли:\n"
        "  show                         — показать текущие настройки\n"
        "  set <имя> <значение>         — изменить настройку (см. show)\n"
        "  log on|off                   — вкл/выкл подробные логи по каждой детекции\n"
        "  overlay on|off               — вкл/выкл рамку (F7 делает то же самое)\n"
        "  click on|off                 — вкл/выкл автоклик (F6 делает то же самое)\n"
        "  save on|off                  — сохранять last.jpg при детекции (тратит время!)\n"
        "  bench                        — замерить скорость захвата/детекции сейчас\n"
        "  help                         — эта справка\n"
    )


def print_settings():
    for name, value in vars(cfg).items():
        print(f"  {name} = {value}")


def console_loop():
    print_help()
    while True:
        try:
            line = input().strip()
        except EOFError:
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        try:
            if cmd == "help":
                print_help()
            elif cmd == "show":
                print_settings()
            elif cmd == "log":
                cfg.debug_log = parts[1].lower() == "on"
            elif cmd == "overlay":
                set_overlay_enabled(parts[1].lower() == "on")
            elif cmd == "click":
                cfg.auto_click = parts[1].lower() == "on"
            elif cmd == "save":
                cfg.save_last_frame = parts[1].lower() == "on"
            elif cmd == "bench":
                run_benchmark()
            elif cmd == "set":
                name, value = parts[1], float(parts[2])
                if hasattr(cfg, name):
                    setattr(cfg, name, value)
                    print(f"{name} = {value}")
                else:
                    print(f"Нет такой настройки: {name}. Введи 'show' для списка.")
            else:
                print("Неизвестная команда. Введи 'help'.")
        except (IndexError, ValueError):
            print("Неверный формат команды. Введи 'help'.")


threading.Thread(target=console_loop, daemon=True).start()
run_benchmark()

print("\nЗажми Ctrl — слежение в области у центра экрана.")
print("Зажми Alt — слежение по всему экрану.")
print(f"{AUTOCLICK_TOGGLE_KEY.upper()} / 'click on|off' — автоклик.")
print(f"{OVERLAY_TOGGLE_KEY.upper()} / 'overlay on|off' — рамка оверлея (выключи для максимальной скорости).")

while True:
    found = False
    if keyboard.is_pressed(TRACK_KEY):
        found = track(
            center_x - margin, center_y - margin, margin * 2, margin * 2,
            offset_x=center_x - margin, offset_y=center_y - margin,
        )
    elif keyboard.is_pressed(FULLSCREEN_KEY):
        found = track(0, 0, screen_width, screen_height, offset_x=0, offset_y=0)

    if not found and cfg.overlay_enabled:
        overlay.hide_all_and_wait()

    time.sleep(cfg.scan_interval)