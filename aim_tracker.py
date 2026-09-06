import ctypes
import json
import os
import sys
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
        self.debug_log = True           # печатать таймлоги каждой детекции
        self.save_last_frame = False    # сохранять last.jpg (лишняя запись на диск = задержка)
        self.auto_click = False
        self.overlay_enabled = True     # True = точный, но медленный синхронный режим
                                         # False = быстрый режим с фоновым захватом экрана
        self.autosave = True            # сохранять активный профиль при выходе
        self.bench_on_boot = True       # прогонять бенчмарк один раз за загрузку компьютера


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
    print(f"[Оверлей] {'ВКЛ (точнее, но медленнее)' if enabled else 'ВЫКЛ (быстрый режим, фоновый захват)'}")


def toggle_autoclick():
    cfg.auto_click = not cfg.auto_click
    print(f"[Автоклик] {'включен' if cfg.auto_click else 'выключен'}")


keyboard.add_hotkey(AUTOCLICK_TOGGLE_KEY, toggle_autoclick)
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


def move_and_maybe_click(x, y):
    """Ведёт курсор к цели (x, y) в экранных координатах.

    move_smoothing < 1.0 — экспоненциальное сглаживание: за кадр проходим
    только эту долю пути до цели. Поскольку цикл слежения крутится непрерывно,
    курсор подъезжает к цели за несколько кадров вместо телепорта — движение
    получается похожим на человеческое. dead_zone гасит микродёрганье на
    один-два пикселя.

    Оба выключены по умолчанию (1.0 и 0), потому что включённая мёртвая зона
    добавляет в горячий путь вызов pyautogui.position().
    """
    if cfg.move_smoothing < 1.0 or cfg.dead_zone > 0:
        cur_x, cur_y = pyautogui.position()
        dx, dy = x - cur_x, y - cur_y
        if dx * dx + dy * dy <= cfg.dead_zone * cfg.dead_zone:
            if cfg.auto_click:
                pyautogui.click()
            return
        if cfg.move_smoothing < 1.0:
            x = cur_x + dx * cfg.move_smoothing
            y = cur_y + dy * cfg.move_smoothing

    x, y = clamp_to_safe_area(x, y)
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

    if cfg.debug_log:
        print(
            f"[sync] пятен={len(blobs)}{' [lock]' if _lock['held'] else ''} "
            f"area={primary['area']}px -> ({screen_cx},{screen_cy}) | "
            f"захват={((t1 - t0) * 1000):.1f}мс diff={((t2 - t1) * 1000):.1f}мс "
            f"оверлей={((t3 - t2) * 1000):.1f}мс move={((t4 - t3) * 1000):.1f}мс "
            f"| итого={((t4 - t0) * 1000):.1f}мс"
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

    if cfg.debug_log:
        print(
            f"[fast] пятен={len(blobs)}{' [lock]' if _lock['held'] else ''} "
            f"area={primary['area']}px -> ({screen_cx},{screen_cy}) | "
            f"diff={((t1 - t0) * 1000):.1f}мс move={((t2 - t1) * 1000):.1f}мс "
            f"| захват фоном ~{grabber.capture_time_ms:.1f}мс/кадр"
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
        "  log on|off                   — вкл/выкл подробные логи по каждой детекции\n"
        "  overlay on|off               — вкл/выкл рамку (F7 делает то же самое)\n"
        "  click on|off                 — вкл/выкл автоклик (F6 делает то же самое)\n"
        "  save on|off                  — сохранять last.jpg при детекции (тратит время!)\n"
        "  bench                        — замерить скорость захвата/детекции сейчас\n"
        "  profiles                     — страница профилей: список и команды\n"
        "  profile save|load|delete|default|defaults\n"
        "  reset_config                 — стереть файл конфига (спросит подтверждение)\n"
        "  help                         — эта справка\n"
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
}

# верхние границы (нужны только там, где значение — доля)
MAX_VALUES = {
    "move_smoothing": 1.0,
    "margin": min(center_x, center_y),   # больше — область слежения вылезет за край экрана
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
    ("Прицел", (
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
        ("debug_log",         "печатать таймлог каждой детекции"),
        ("save_last_frame",   "сохранять last.jpg при детекции — запись на диск добавляет задержку"),
        ("autosave",          "сохранять активный профиль при выходе"),
        ("bench_on_boot",     "гонять бенчмарк один раз за загрузку компьютера, а не каждый старт"),
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
    return str(value)


def print_settings_menu():
    print("\nНастраиваемые переменные. 'set <имя> <значение>' — изменить,")
    print("'set <имя>' — показать одну с описанием.\n")
    for title, items in settings_groups_full():
        print(f"  {title}")
        for name, desc in items:
            value = getattr(cfg, name)
            hint = range_hint(name, value)
            hint = f"[{hint}]" if hint else ""
            print(f"    {name:<18}= {format_value(value):<7} {hint:<11} {desc}")
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
        print(f"    {desc}")
    print(f"    изменить: set {name} <значение>")


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
                    apply_setting(parts[1], parts[2])
            else:
                print("Неизвестная команда. Введи 'help'.")
        except (IndexError, ValueError):
            print("Неверный формат команды. Введи 'help'.")


startup_load_config()
set_overlay_enabled(cfg.overlay_enabled)  # запускает грабер, если оверлей выключен
# Бенчмарк — до запуска консоли: иначе команда из консоли (например
# reset_config) успевала отработать в середине замера, а завершавшийся
# следом бенчмарк своим save_config_file() воскрешал только что удалённый файл.
maybe_run_startup_benchmark()
threading.Thread(target=console_loop, daemon=True).start()

print("\nЗажми Ctrl — слежение в области у центра экрана.")
print("Зажми Alt — слежение по всему экрану.")
print(f"{AUTOCLICK_TOGGLE_KEY.upper()} / 'click on|off' — автоклик.")
print(f"{OVERLAY_TOGGLE_KEY.upper()} / 'overlay on|off' — рамка оверлея (выключи для максимальной скорости).")
print("Для плавного движения на записи: 'set move_smoothing 0.3' и 'set dead_zone 2'.")
print("'edit' — все настройки, 'profiles' — профили. Отступ Ctrl меняется на лету: 'set margin 150'.")

try:
    while True:
        found = False
        if keyboard.is_pressed(TRACK_KEY):
            # снимок на итерацию: margin правится из консоли прямо во время работы
            margin = int(cfg.margin)
            found = track(
                center_x - margin, center_y - margin, margin * 2, margin * 2,
                offset_x=center_x - margin, offset_y=center_y - margin,
            )
        elif keyboard.is_pressed(FULLSCREEN_KEY):
            found = track(0, 0, screen_width, screen_height, offset_x=0, offset_y=0)

        if not found and cfg.overlay_enabled:
            overlay.hide_all_and_wait()

        time.sleep(cfg.scan_interval)
except pyautogui.FailSafeException:
    # Пользователь сам увёл курсор в угол — это штатный аварийный тормоз,
    # а не ошибка, так что выходим без стектрейса.
    print("\n[Стоп] Курсор в углу экрана — аварийная остановка pyautogui.")
except KeyboardInterrupt:
    print("\n[Стоп] Прервано с клавиатуры.")
finally:
    grabber.stop()
    if cfg.autosave and _config["data"] is not None and _config["autosave_ok"]:
        profile_save(_config["active"])
