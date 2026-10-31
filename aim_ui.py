"""Desktop controls for AimAssist; the tracker remains authoritative.

Run as a separate process. Read state objects as UTF-8 JSON lines from stdin;
write commands as JSON lines to stdout. Tk and image updates stay on the main
thread. Importing this module does not create a window or import GUI libraries.
"""
from __future__ import annotations

import json
import math
import queue
import sys
import threading
from typing import Any


MAIN_SETTINGS = (
    "observe_only", "diff_threshold", "min_blob_area", "margin", "camera_gain",
    "confirm_frames", "click_interval", "auto_click",
)
LABELS = {
    "observe_only": "Наблюдать без управления мышью",
    "diff_threshold": "Порог изменения пикселей (0–255)",
    "min_blob_area": "Минимальная площадь, px",
    "margin": "Радиус области у прицела, px",
    "camera_gain": "Сила доводки камеры",
    "confirm_frames": "Кадров для подтверждения цели",
    "click_interval": "Пауза между кликами, с",
    "auto_click": "Автоклик",
    "aim_mode": "Режим движения мыши",
    "window_exe": "Процесс выбранного окна",
    "window_title": "Слово в заголовке окна",
    "track_key": "Клавиша слежения у прицела",
    "fullscreen_key": "Клавиша слежения по окну",
    "panic_key": "Аварийная остановка",
    "overlay_key": "Клавиша рамки",
    "autoclick_key": "Клавиша автоклика",
    "dump_key": "Клавиша записи кадров",
    "settle_after_action": "Ожидание после своего действия, с",
    "global_motion_limit": "Предел доли изменившегося кадра",
    "click_tolerance": "Допуск для клика, px",
    "scan_interval": "Пауза между циклами, с",
    "denoise_kernel": "Размер фильтра шума, px",
    "lock_radius": "Радиус удержания цели, px",
    "lock_timeout": "Время удержания цели, с",
    "move_smoothing": "Сглаживание движения",
    "dead_zone": "Мёртвая зона движения, px",
    "move_duration": "Длительность движения, с",
    "hide_settle_delay": "Ожидание скрытия рамки, с",
    "box_hold": "Время показа рамки, с",
    "margin_preview": "Время показа области, с",
    "debug_log": "Подробный журнал",
    "save_last_frame": "Сохранять последний кадр",
    "overlay_enabled": "Показывать рамки поверх игры",
    "autosave": "Сохранять профиль при выходе",
    "bench_on_boot": "Замер скорости после загрузки ПК",
    "pause_on_start": "Пауза при запуске",
    "tune_mode": "Записывать отладочные кадры",
    "tune_frames": "Кадров в буфере записи",
    "status_hz": "Частота строки состояния, Гц",
    "color": "Цвет консоли",
    "exclusion_rects": "Исключённые области (JSON)",
    "compensate_camera": "Компенсировать движение камеры",
}
MINIMUMS = {
    "margin": 1, "diff_threshold": 0, "min_blob_area": 1, "camera_gain": 0.01,
    "confirm_frames": 1, "click_interval": 0, "scan_interval": 0,
    "settle_after_action": 0, "global_motion_limit": 0, "click_tolerance": 0,
    "denoise_kernel": 0, "lock_radius": 0, "lock_timeout": 0,
    "move_smoothing": 0.01, "dead_zone": 0, "move_duration": 0,
    "hide_settle_delay": 0, "box_hold": 0, "margin_preview": 0,
    "tune_frames": 1, "status_hz": 0,
}
MAXIMUMS = {
    "diff_threshold": 255, "camera_gain": 5, "move_smoothing": 1,
    "global_motion_limit": 1, "status_hz": 60,
}
CHOICES = {"aim_mode": ("auto", "absolute", "relative"), "color": ("auto", "on", "off")}


def value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def parse_value(name: str, raw: Any, current: Any) -> Any:
    """Validate local edits before sending them; the tracker validates again."""
    text = str(raw).strip()
    if isinstance(current, bool):
        if isinstance(raw, bool):
            return raw
        if text.lower() in ("on", "true", "1", "да"):
            return True
        if text.lower() in ("off", "false", "0", "нет"):
            return False
        raise ValueError("ожидается on/off")
    if name == "exclusion_rects":
        value = json.loads(text) if isinstance(raw, str) else raw
        if not isinstance(value, list):
            raise ValueError("ожидается список областей")
        for rect in value:
            if not isinstance(rect, (list, tuple)) or len(rect) != 4:
                raise ValueError("область: [слева, сверху, справа, снизу]")
            if any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) for v in rect):
                raise ValueError("координаты должны быть конечными числами")
            left, top, right, bottom = rect
            if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
                raise ValueError("координаты от 0 до 1; справа/снизу больше слева/сверху")
        return json.dumps(value, ensure_ascii=False) if isinstance(current, str) else value
    if isinstance(current, (int, float)):
        try:
            value = float(text)
        except (ValueError, OverflowError) as exc:
            raise ValueError("ожидается число") from exc
        if not math.isfinite(value):
            raise ValueError("число должно быть конечным")
        if isinstance(current, int):
            if not value.is_integer():
                raise ValueError("ожидается целое число")
            value = int(value)
        if name in MINIMUMS and value < MINIMUMS[name]:
            raise ValueError(f"минимум {MINIMUMS[name]}")
        if name in MAXIMUMS and value > MAXIMUMS[name]:
            raise ValueError(f"максимум {MAXIMUMS[name]}")
        return value
    if name in CHOICES and text.lower() not in CHOICES[name]:
        raise ValueError("допустимо: " + ", ".join(CHOICES[name]))
    return text.lower() if name in CHOICES else str(raw)


class SettingsModel:
    """Keep unfinished edits stable while frequent tracker snapshots arrive."""

    def __init__(self) -> None:
        self.settings: dict[str, Any] = {"observe_only": True}
        self.edits: dict[str, Any] = {}
        self.pending: dict[str, Any] = {}
        self.state: dict[str, Any] = {}

    def receive(self, state: dict[str, Any]) -> None:
        settings = state.get("settings", {})
        if isinstance(settings, dict):
            self.settings.update(settings)
            for name in list(self.pending):
                if name in settings and settings[name] == self.pending[name]:
                    del self.pending[name]
        if state.get("error"):
            # Rejected commands must not leave the UI showing an applied value.
            self.pending.clear()
        self.state.update(state)

    def edit(self, name: str, raw: Any) -> None:
        if name not in self.settings:
            raise ValueError("неизвестная настройка")
        if value_text(raw) == value_text(self.pending.get(name, self.settings[name])):
            self.edits.pop(name, None)
        else:
            self.edits[name] = raw

    def displayed(self, name: str) -> Any:
        return self.edits.get(name, self.pending.get(name, self.settings[name]))

    def commands(self) -> list[dict[str, Any]]:
        parsed = {}
        for name, raw in self.edits.items():
            try:
                parsed[name] = parse_value(name, raw, self.settings[name])
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{LABELS.get(name, name)}: {exc}") from exc
        # Validate the entire batch before clearing edits or emitting anything.
        commands = [{"command": "set", "name": name, "value": value}
                    for name, value in parsed.items()]
        self.pending.update(parsed)
        self.edits.clear()
        return commands

    def discard(self) -> None:
        self.edits.clear()


class ControlPanel:
    def __init__(self, root: Any) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk, self.root = tk, ttk, root
        self.model = SettingsModel()
        self.incoming: queue.Queue = queue.Queue(maxsize=2)
        self.variables: dict[str, Any] = {}
        self.window_ids: list[Any] = []
        self.updating = False
        self.profile_editing = False
        self.last_preview = None
        self.preview_image = None
        root.title("AimAssist — управление")
        root.geometry("1050x720")
        root.minsize(850, 580)
        root.protocol("WM_DELETE_WINDOW", self.close)

        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        self.status = tk.StringVar(value="Ожидаю связь с программой…")
        ttk.Label(outer, textvariable=self.status, font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, sticky="w")
        self.mode_hint = tk.StringVar(value="Наблюдение: мышь и автоклики отключены. F8 — стоп; F9 — кадры.")
        ttk.Label(outer, textvariable=self.mode_hint).grid(
            row=1, column=0, sticky="w", pady=(3, 10))

        selectors = ttk.Frame(outer)
        selectors.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        selectors.columnconfigure(1, weight=1)
        ttk.Label(selectors, text="Окно игры").grid(row=0, column=0, padx=(0, 8))
        self.windows = ttk.Combobox(selectors, state="readonly", width=45)
        self.windows.grid(row=0, column=1, sticky="ew")
        self.windows.bind("<<ComboboxSelected>>", self.select_window)
        ttk.Button(selectors, text="Обновить", command=lambda: self.send("refresh")).grid(
            row=0, column=2, padx=6)
        ttk.Label(selectors, text="Профиль").grid(row=1, column=0, sticky="w", pady=6)
        self.profile = tk.StringVar()
        self.profiles = ttk.Combobox(selectors, textvariable=self.profile, width=25)
        self.profiles.grid(row=1, column=1, sticky="ew", pady=6)
        self.profiles.bind("<KeyRelease>", lambda event: setattr(self, "profile_editing", True))
        self.profiles.bind("<<ComboboxSelected>>", lambda event: setattr(self, "profile_editing", True))
        actions = ttk.Frame(selectors)
        actions.grid(row=1, column=2, sticky="w")
        ttk.Button(actions, text="Загрузить", command=lambda: self.profile_command("profile_load")).pack(side="left")
        ttk.Button(actions, text="Сохранить", command=lambda: self.profile_command("profile_save")).pack(side="left", padx=4)

        body = ttk.Panedwindow(outer, orient="horizontal")
        body.grid(row=3, column=0, sticky="nsew")
        settings = ttk.Frame(body)
        body.add(settings, weight=4)
        notebook = ttk.Notebook(settings)
        notebook.pack(fill="both", expand=True)
        self.main_fields = self.scrollable_tab(notebook, "Основное")
        self.advanced_fields = self.scrollable_tab(notebook, "Дополнительно")
        self.dirty = tk.StringVar()
        ttk.Label(settings, textvariable=self.dirty).pack(anchor="w", pady=(8, 3))
        edit_actions = ttk.Frame(settings)
        edit_actions.pack(fill="x")
        ttk.Button(edit_actions, text="Применить настройки", command=self.apply).pack(side="left")
        ttk.Button(edit_actions, text="Отменить ввод", command=self.discard).pack(side="left", padx=5)

        preview = ttk.LabelFrame(body, text="Что видит программа", padding=8)
        body.add(preview, weight=5)
        self.preview = ttk.Label(preview, text="Кадр появится после захвата области игры", anchor="center")
        self.preview.pack(fill="both", expand=True)
        self.reason = tk.StringVar(value="Причина решения: —")
        self.target = tk.StringVar(value="Цель: —    Задержка: —")
        ttk.Label(preview, textvariable=self.reason, wraplength=420).pack(anchor="w", pady=(8, 3))
        ttk.Label(preview, textvariable=self.target, wraplength=420).pack(anchor="w")
        ttk.Label(preview, text="Зелёная рамка — цель. Остальные кандидаты и причины\nотсева отображаются на диагностическом кадре.",
                  wraplength=420).pack(anchor="w", pady=(8, 0))

        bottom = ttk.Frame(outer)
        bottom.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(bottom, text="Пауза", command=lambda: self.send("pause")).pack(side="left")
        ttk.Button(bottom, text="Продолжить", command=lambda: self.send("resume")).pack(side="left", padx=5)
        ttk.Button(bottom, text="СТОП / выход", command=self.close).pack(side="left")
        ttk.Button(bottom, text="Сохранить кадры (F9)", command=lambda: self.send("record")).pack(side="right")
        self.message = tk.StringVar()
        ttk.Label(outer, textvariable=self.message, wraplength=950, foreground="#a02b20").grid(
            row=5, column=0, sticky="w", pady=(6, 0))

        root.bind("<F8>", lambda event: self.close())
        root.bind("<F9>", lambda event: self.send("record"))
        root.bind("<Control-s>", lambda event: self.profile_command("profile_save"))
        self.sync_fields()
        threading.Thread(target=self.read_states, daemon=True).start()
        root.after(50, self.poll)
        self.send("refresh")

    def scrollable_tab(self, notebook: Any, title: str) -> Any:
        frame = self.ttk.Frame(notebook)
        notebook.add(frame, text=title)
        canvas = self.tk.Canvas(frame, highlightthickness=0)
        scrollbar = self.ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = self.ttk.Frame(canvas, padding=8)
        handle = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.columnconfigure(0, weight=1)
        inner.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(handle, width=event.width))
        return inner

    def send(self, command: str, **values: Any) -> None:
        try:
            sys.stdout.write(json.dumps({"command": command, **values}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            self.message.set("Связь с программой потеряна.")

    def read_states(self) -> None:
        for line in sys.stdin:
            try:
                state = json.loads(line)
                if not isinstance(state, dict):
                    continue
            except (ValueError, UnicodeError):
                continue
            try:
                self.incoming.put_nowait(state)
            except queue.Full:
                try:
                    self.incoming.get_nowait()
                except queue.Empty:
                    pass
                self.incoming.put_nowait(state)
        self.incoming.put({"status": "Связь с программой закрыта"})

    def poll(self) -> None:
        try:
            while True:
                state = self.incoming.get_nowait()
                self.model.receive(state)
                self.update_state(state)
        except queue.Empty:
            pass
        self.root.after(75, self.poll)

    def update_state(self, state: dict[str, Any]) -> None:
        self.sync_fields()
        self.status.set(str(state.get("status", self.model.state.get("status", "Подключено"))))
        if "profiles" in state:
            self.profiles.configure(values=state["profiles"])
        if "profile" in state and not self.profile_editing:
            self.profile.set(str(state["profile"]))
        if "windows" in state:
            current = self.windows.current()
            previous = self.window_ids[current] if 0 <= current < len(self.window_ids) else None
            windows = state["windows"]
            self.window_ids = [window["id"] for window in windows]
            self.windows.configure(values=[f"{window.get('title', 'Окно')}  ·  {window.get('exe', '')}"
                                           for window in windows])
            selected = state.get("window_id", state.get("selected_window", previous))
            if selected in self.window_ids:
                self.windows.current(self.window_ids.index(selected))
            else:
                self.windows.set("")
        observing = self.model.settings.get("observe_only", True)
        self.mode_hint.set("Наблюдение: мышь и автоклики отключены. F8 — стоп; F9 — кадры."
                           if observing else "Управление мышью включено. F8 — стоп; F9 — сохранить кадры.")
        self.reason.set("Причина решения: " + str(self.model.state.get("reason") or "—"))
        target = self.model.state.get("target")
        latency = self.model.state.get("latency_ms")
        latency_text = f"{latency:.1f} мс" if isinstance(latency, (int, float)) else "—"
        self.target.set(f"Цель: {target if target is not None else '—'}    Задержка: {latency_text}")
        if "error" in state:
            self.message.set(str(state["error"] or ""))
        if state.get("preview") and state["preview"] != self.last_preview:
            self.show_preview(state["preview"])

    def sync_fields(self) -> None:
        self.updating = True
        try:
            for name in sorted(self.model.settings, key=lambda n: (n not in MAIN_SETTINGS,
                                                                     MAIN_SETTINGS.index(n) if n in MAIN_SETTINGS else n)):
                value = self.model.settings[name]
                if name not in self.variables:
                    frame = self.main_fields if name in MAIN_SETTINGS else self.advanced_fields
                    row = self.ttk.Frame(frame)
                    row.pack(fill="x", pady=4)
                    if isinstance(value, bool):
                        variable = self.tk.BooleanVar()
                        self.ttk.Checkbutton(row, text=LABELS.get(name, name), variable=variable).pack(anchor="w")
                    else:
                        self.ttk.Label(row, text=LABELS.get(name, name)).pack(anchor="w")
                        variable = self.tk.StringVar()
                        if name in CHOICES:
                            self.ttk.Combobox(row, textvariable=variable, values=CHOICES[name], state="readonly").pack(fill="x")
                        else:
                            self.ttk.Entry(row, textvariable=variable).pack(fill="x")
                        if name == "exclusion_rects":
                            self.ttk.Label(row, text="[[слева, сверху, справа, снизу], …], доли 0–1",
                                           wraplength=350).pack(anchor="w")
                    variable.trace_add("write", lambda *args, key=name: self.edited(key))
                    self.variables[name] = variable
                displayed = self.model.displayed(name)
                shown = displayed if isinstance(value, bool) else value_text(displayed)
                if self.variables[name].get() != shown:
                    self.variables[name].set(shown)
        finally:
            self.updating = False
        self.update_dirty()

    def edited(self, name: str) -> None:
        if not self.updating:
            self.model.edit(name, self.variables[name].get())
            self.update_dirty()

    def update_dirty(self) -> None:
        if self.model.edits:
            text = f"Изменено полей: {len(self.model.edits)} — нажмите «Применить»."
        elif self.model.pending:
            text = "Ожидаю подтверждение настроек…"
        elif self.model.state.get("dirty"):
            text = "Профиль изменён — сохраните, чтобы закрепить настройки."
        else:
            text = "Настройки синхронизированы."
        self.dirty.set(text)

    def apply(self) -> bool:
        try:
            commands = self.model.commands()
        except ValueError as exc:
            self.message.set(str(exc))
            return False
        for command in commands:
            self.send(**command)
        self.message.set("")
        self.update_dirty()
        return True

    def discard(self) -> None:
        self.model.discard()
        self.message.set("")
        self.sync_fields()

    def profile_command(self, command: str) -> None:
        name = self.profile.get().strip()
        if not name:
            self.message.set("Укажите имя профиля.")
            return
        if command == "profile_save" and not self.apply():
            return
        if command == "profile_load":
            self.model.discard()
            self.model.pending.clear()
        self.profile_editing = False
        self.send(command, name=name)

    def select_window(self, event: Any = None) -> None:
        index = self.windows.current()
        if 0 <= index < len(self.window_ids):
            self.send("select_window", id=self.window_ids[index])

    def show_preview(self, data: str) -> None:
        try:
            original_data = data
            if data.startswith("data:"):
                data = data.split(",", 1)[1]
            try:
                import base64
                import io
                from PIL import Image, ImageTk
                image = Image.open(io.BytesIO(base64.b64decode(data)))
                image.thumbnail((600, 430))
                photo = ImageTk.PhotoImage(image)
            except ImportError:
                photo = self.tk.PhotoImage(data=data, format="png")
                factor = max(1, math.ceil(photo.width() / 600), math.ceil(photo.height() / 430))
                if factor > 1:
                    photo = photo.subsample(factor)
            self.preview.configure(image=photo, text="")
            self.preview_image = photo
            self.last_preview = original_data
        except Exception as exc:
            self.message.set(f"Не удалось показать кадр: {exc}")

    def close(self) -> None:
        self.send("stop")
        self.root.destroy()


def main() -> None:
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    import tkinter as tk
    root = tk.Tk()
    ControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
