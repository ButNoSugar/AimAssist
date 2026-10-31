"""Bounded JSON-line connection to the optional desktop control panel.

The capture loop calls ``publish`` and ``drain``; it never writes to a pipe or
waits for the GUI. Tk runs in its own process, and only the main capture thread
applies the commands received here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading


class PanelBridge:
    """Own one control-panel process. Instances are intentionally single-use."""

    MAX_LINE_BYTES = 1024 * 1024
    COMMAND_CAPACITY = 128

    def __init__(self, script_path, python_executable=None):
        self.script_path = str(Path(script_path).resolve())
        self.python_executable = python_executable or sys.executable
        self.process = None
        self.error = None
        self.eof = False
        self._commands = queue.Queue(maxsize=self.COMMAND_CAPACITY)
        self._condition = threading.Condition()
        self._pending = None
        self._closed = threading.Event()
        self._reader = None
        self._writer = None

    @property
    def alive(self):
        return (self.process is not None and not self._closed.is_set()
                and not self.eof and self.process.poll() is None)

    def start(self):
        if self.process is not None:
            return self
        if self._closed.is_set():
            raise RuntimeError("A closed panel bridge cannot be restarted")
        kwargs = {}
        if os.name == "nt":
            # Avoid a second console while keeping inherited environment/auth.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.process = subprocess.Popen(
                [self.python_executable, self.script_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                text=True, encoding="utf-8", errors="strict", bufsize=1,
                **kwargs,
            )
        except OSError as exc:
            self.error = f"Не удалось открыть панель: {exc}"
            return self
        self._reader = threading.Thread(target=self._read_commands,
                                        name="panel-commands", daemon=True)
        self._writer = threading.Thread(target=self._write_states,
                                        name="panel-states", daemon=True)
        self._reader.start()
        self._writer.start()
        return self

    def publish(self, state):
        """Queue a state snapshot, replacing an unsent older snapshot.

        Serialization happens before taking the short mailbox lock, preserving
        the snapshot even if the caller subsequently mutates its state dict.
        Invalid state is reported without interrupting capture.
        """
        if not self.alive:
            return False
        if not isinstance(state, dict):
            self.error = "Состояние панели должно быть JSON-объектом"
            return False
        try:
            message = json.dumps(state, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")) + "\n"
        except (TypeError, ValueError) as exc:
            self.error = f"Не удалось передать состояние панели: {exc}"
            return False
        if len(message.encode("utf-8")) > self.MAX_LINE_BYTES:
            self.error = "Состояние панели превышает 1 МиБ"
            return False
        with self._condition:
            if self._closed.is_set() or self.eof:
                return False
            self._pending = message
            self._condition.notify()
        return True

    def drain(self, limit=32):
        """Return at most ``limit`` received commands without waiting."""
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("Command limit must be a nonnegative integer")
        result = []
        for _ in range(min(limit, self.COMMAND_CAPACITY)):
            try:
                result.append(self._commands.get_nowait())
            except queue.Empty:
                break
        return result

    def _read_commands(self):
        try:
            while not self._closed.is_set():
                line = self.process.stdout.readline(self.MAX_LINE_BYTES + 1)
                if not line:
                    break
                if (len(line.encode("utf-8")) > self.MAX_LINE_BYTES
                        or not line.endswith("\n")):
                    self.error = "Панель отправила слишком длинную или неполную команду"
                    # Do not repeatedly allocate while draining an endless line.
                    self.process.terminate()
                    break
                try:
                    command = json.loads(line)
                except (ValueError, RecursionError):
                    self.error = "Панель отправила некорректный JSON"
                    continue
                if (not isinstance(command, dict)
                        or not isinstance(command.get("command"), str)):
                    self.error = "Команда панели должна содержать строку command"
                    continue
                try:
                    self._commands.put_nowait(command)
                except queue.Full:
                    # Prefer the newest UI intentions; never block stdout.
                    try:
                        self._commands.get_nowait()
                    except queue.Empty:
                        pass
                    self._commands.put_nowait(command)
                    self.error = "Очередь панели переполнена; старые команды пропущены"
        except (OSError, UnicodeError, ValueError) as exc:
            if not self._closed.is_set():
                self.error = f"Соединение с панелью закрыто: {exc}"
        finally:
            self.eof = True
            with self._condition:
                self._pending = None
                self._condition.notify_all()

    def _write_states(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._pending is not None
                        or self._closed.is_set() or self.eof)
                    if self._closed.is_set() or self.eof:
                        return
                    message, self._pending = self._pending, None
                self.process.stdin.write(message)
                self.process.stdin.flush()
        except (OSError, UnicodeError, ValueError) as exc:
            if not self._closed.is_set():
                self.error = f"Не удалось обновить панель: {exc}"

    def close(self):
        """Stop only our child, with bounded waits even if it ignores stdin."""
        self._closed.set()
        with self._condition:
            self._pending = None
            self._condition.notify_all()
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            except OSError:
                pass
        for thread in (self._reader, self._writer):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1)
        if process is not None:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
