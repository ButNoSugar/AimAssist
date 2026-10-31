"""Bounded recordings of the exact frames used by detection, and offline replay.

This module never imports the application, captures a desktop, or sends input.
Only the supplied frames and JSON diagnostic metadata are stored. Call remember
before moving the mouse so replay observes exactly what the detector observed.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from typing import Iterator
import zipfile

import numpy as np


MAX_FRAMES = 120
MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_SESSION_BYTES = 256 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
FORMAT = "AimAssist frame pairs"
VERSION = 1
OPTION_NAMES = frozenset({
    "threshold", "min_area", "denoise_kernel", "max_area_fraction",
    "max_aspect_ratio", "ignore_bottom_fraction", "ignore_rects",
    "compensate_camera",
})


def _json_copy(value):
    if not isinstance(value, dict):
        raise ValueError("diagnostic metadata must be a JSON object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("diagnostic metadata must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("diagnostic metadata is too large")
    return json.loads(encoded)


def _validate_arrays(prev, current, mask):
    for frame in (prev, current):
        if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8:
            raise ValueError("frames must be uint8 arrays")
        if (frame.ndim != 3 or frame.shape[2] not in (3, 4)
                or min(frame.shape[:2]) < 1 or max(frame.shape[:2]) > 8192
                or frame.nbytes > MAX_FRAME_BYTES):
            raise ValueError("unsupported frame dimensions")
    if prev.shape != current.shape:
        raise ValueError("frame pair shapes must match")
    if mask is not None:
        if (not isinstance(mask, np.ndarray) or mask.dtype != np.uint8
                or mask.shape != prev.shape[:2]):
            raise ValueError("mask must be a uint8 array matching the frames")


def _validate_region(region, shape):
    if (not isinstance(region, (tuple, list)) or len(region) != 4
            or any(type(n) is not int for n in region)):
        raise ValueError("region must be four integer coordinates")
    if (tuple(region[2:]) != (shape[1], shape[0])
            or any(abs(n) > 1_000_000 for n in region[:2])):
        raise ValueError("region dimensions must match the frame pair")
    return tuple(region)


class SessionRecorder:
    """A frame-count and byte-limited ring buffer with lossless snapshots."""

    def __init__(self, max_frames=20):
        self._lock = threading.RLock()
        self._frames = deque()
        self._bytes = 0
        self._sequence = 0
        self._last_token = None
        self.max_frames = max_frames

    @property
    def max_frames(self):
        return self._max_frames

    @max_frames.setter
    def max_frames(self, value):
        if type(value) is not int or not 1 <= value <= MAX_FRAMES:
            raise ValueError(f"max_frames must be between 1 and {MAX_FRAMES}")
        with self._lock:
            self._max_frames = value
            self._trim()

    def __len__(self):
        with self._lock:
            return len(self._frames)

    def _trim(self):
        while self._frames and (len(self._frames) > self.max_frames
                                or self._bytes > MAX_SESSION_BYTES):
            item = self._frames.popleft()
            self._bytes -= item[-1]

    def clear(self):
        with self._lock:
            self._frames.clear()
            self._bytes = 0
            self._last_token = None

    def remember(self, prev, current, region, metadata, mask=None):
        _validate_arrays(prev, current, mask)
        region = _validate_region(region, prev.shape)
        metadata = _json_copy(metadata)
        size = prev.nbytes + current.nbytes + (mask.nbytes if mask is not None else 0)
        with self._lock:
            self._frames.append((prev.copy(), current.copy(), region, metadata,
                                 mask.copy() if mask is not None else None, size))
            self._bytes += size
            self._trim()
            self._sequence += 1
            self._last_token = self._sequence
            return self._last_token

    def update_last_metadata(self, updates, token=None):
        """Finalize a recorded decision without recopying its frame arrays.

        ``remember`` returns a token; supplying it prevents a late decision from
        updating a newer frame pair. Existing dump snapshots retain their prior
        metadata instead of sharing a mutable dict with this update.
        """
        updates = _json_copy(updates)
        with self._lock:
            if not self._frames or (token is not None and token != self._last_token):
                return False
            prev, current, region, metadata, mask, size = self._frames[-1]
            updated = _json_copy({**metadata, **updates})
            self._frames[-1] = prev, current, region, updated, mask, size
            return True

    def dump(self, root_path):
        """Create a new session directory; never overwrite earlier recordings.

        An empty buffer raises ValueError. The buffer is retained after a dump,
        allowing a second snapshot without losing the recent diagnostic history.
        """
        with self._lock:
            if not self._frames:
                raise ValueError("no frame pairs have been recorded")
            frames = list(self._frames)
        root = Path(root_path)
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        folder = root / stamp
        suffix = 1
        while True:
            try:
                folder.mkdir()
                break
            except FileExistsError:
                suffix += 1
                folder = root / f"{stamp}-{suffix}"
        entries = []
        for index, (prev, current, region, metadata, mask, _) in enumerate(frames):
            filename = f"{index:04d}.npz"
            arrays = {"prev": prev, "current": current}
            if mask is not None:
                arrays["mask"] = mask
            np.savez_compressed(folder / filename, **arrays)
            entries.append({"file": filename, "region": region, "metadata": metadata})
        manifest = {"format": FORMAT, "version": VERSION, "frames": entries}
        (folder / "session.json").write_text(
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2),
            encoding="utf-8")
        return folder


def _reject_json_constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


def _archive_size(path):
    """Check NPY headers before np.load can allocate attacker-sized arrays."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (len(names) != len(set(names)) or not {"prev.npy", "current.npy"} <= set(names)
                    or not set(names) <= {"prev.npy", "current.npy", "mask.npy"}):
                raise ValueError("invalid frame archive members")
            total = 0
            shapes = {}
            for entry in entries:
                if entry.file_size > MAX_FRAME_BYTES + 16384 or entry.flag_bits & 1:
                    raise ValueError("frame archive exceeds limits")
                with archive.open(entry) as handle:
                    version = np.lib.format.read_magic(handle)
                    if version == (1, 0):
                        shape, _, dtype = np.lib.format.read_array_header_1_0(handle)
                    elif version == (2, 0):
                        shape, _, dtype = np.lib.format.read_array_header_2_0(handle)
                    else:
                        raise ValueError("unsupported array format")
                    if dtype != np.dtype("uint8") or not shape or any(
                            type(n) is not int or n < 1 or n > 8192 for n in shape):
                        raise ValueError("unsupported array shape or dtype")
                    size = 1
                    for dimension in shape:
                        size *= dimension
                    if size > MAX_FRAME_BYTES or size != entry.file_size - handle.tell():
                        raise ValueError("invalid array size")
                    total += size
                    shapes[entry.filename] = shape
            frame_shape = shapes["prev.npy"]
            if (len(frame_shape) != 3 or frame_shape[-1] not in (3, 4)
                    or shapes["current.npy"] != frame_shape
                    or ("mask.npy" in shapes and shapes["mask.npy"] != frame_shape[:2])):
                raise ValueError("invalid frame pair shapes")
            return total
    except (OSError, EOFError, zipfile.BadZipFile) as exc:
        raise ValueError("unreadable frame archive") from exc


def load_session(folder) -> Iterator[tuple]:
    """Yield validated (previous, current, region, metadata, mask) tuples."""
    root = Path(folder).resolve()
    manifest_path = root / "session.json"
    try:
        if manifest_path.is_symlink() or manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("session manifest exceeds limits or is a symlink")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"),
                              parse_constant=_reject_json_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("unreadable session manifest") from exc
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or type(manifest.get("version")) is not int or manifest["version"] != VERSION):
        raise ValueError("unsupported recording format")
    entries = manifest.get("frames")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FRAMES:
        raise ValueError("invalid recording frame count")
    seen = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid recording entry")
        filename = entry.get("file")
        if (not isinstance(filename, str) or not re.fullmatch(r"[0-9]{4}\.npz", filename)
                or filename in seen):
            raise ValueError("invalid or duplicate recording path")
        seen.add(filename)
        path = root / filename
        if path.is_symlink() or path.resolve().parent != root:
            raise ValueError("recording path escapes session directory")
        total += _archive_size(path)
        if total > MAX_SESSION_BYTES:
            raise ValueError("session exceeds memory limits")
        metadata = _json_copy(entry.get("metadata"))
        try:
            with np.load(path, allow_pickle=False) as arrays:
                prev, current = arrays["prev"], arrays["current"]
                mask = arrays["mask"] if "mask" in arrays.files else None
        except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
            raise ValueError("invalid frame data") from exc
        _validate_arrays(prev, current, mask)
        region = _validate_region(entry.get("region"), prev.shape)
        yield prev, current, region, metadata, mask


def replay_session(folder, threshold=None):
    """Replay detection only; return JSON-safe per-frame results and totals."""
    from aim_detection import DetectionOptions, detect_changes

    if threshold is not None and (type(threshold) is not int or not 0 <= threshold <= 255):
        raise ValueError("threshold must be an integer between 0 and 255")
    frames = []
    for index, (prev, current, region, metadata, _) in enumerate(load_session(folder)):
        raw_options = metadata.get("options", {})
        if not isinstance(raw_options, dict):
            raise ValueError("recorded options must be a JSON object")
        options = {name: value for name, value in raw_options.items() if name in OPTION_NAMES}
        if threshold is not None:
            options["threshold"] = threshold
        detected = detect_changes(prev, current, DetectionOptions(**options))
        frames.append({"frame": index, "region": list(region),
                       "candidates": len(detected.blobs), "rejected": len(detected.rejected),
                       "changed_fraction": float(detected.changed_fraction),
                       "reason": detected.reason,
                       "recorded_reason": metadata.get("reason"),
                       "prev_ts": metadata.get("prev_ts"),
                       "current_ts": metadata.get("current_ts")})
    return {"frames": frames, "totals": {
        "pairs": len(frames),
        "pairs_with_candidates": sum(item["candidates"] > 0 for item in frames),
        "candidates": sum(item["candidates"] for item in frames),
        "rejected": sum(item["rejected"] for item in frames),
    }}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Replay AimAssist recordings without sending input")
    parser.add_argument("folder", type=Path, help="directory containing session.json")
    parser.add_argument("--threshold", type=int, help="override recorded pixel threshold (0–255)")
    args = parser.parse_args(argv)
    try:
        result = replay_session(args.folder, args.threshold)
    except (ValueError, OSError, TypeError) as exc:
        parser.exit(1, f"Recording error: {exc}\n")
    for frame in result["frames"]:
        print(json.dumps(frame, ensure_ascii=False, allow_nan=False))
    print(json.dumps({"totals": result["totals"]}, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
