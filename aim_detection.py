"""Screen-change detection and target association without desktop side effects.

Frames are equal-sized uint8 BGR/BGRA arrays. Coordinates in detection results
are local to the frames, bboxes have exclusive right/bottom bounds, and centers
are rounded component centroids. ``ignore_rects`` uses normalized coordinates.
The returned mask contains only non-excluded, denoised changes; ``blobs`` and
``rejected`` partition its components by the configured geometry filters.

Camera compensation is optional and deliberately conservative: a weak or large
translation estimate suppresses candidates instead of trusting a misalignment.
It compensates translation only, not perspective, zoom or parallax.

TargetTracker never generates input. Only a candidate observed consistently in
consecutive calls can be returned. Times are monotonic seconds supplied by the
caller; call reset when the frame source, tracking mode or active window changes.
"""

from dataclasses import dataclass, field
import math

import cv2
import numpy as np


@dataclass
class DetectionOptions:
    threshold: int = 30
    min_area: int = 4
    denoise_kernel: int = 3
    max_area_fraction: float = 0.15
    max_aspect_ratio: float = 8.0
    ignore_bottom_fraction: float = 0.0
    ignore_rects: list = field(default_factory=list)
    compensate_camera: bool = False

    def validate(self):
        if not 0 <= self.threshold <= 255:
            raise ValueError("threshold must be between 0 and 255")
        if self.min_area < 1 or int(self.min_area) != self.min_area:
            raise ValueError("min_area must be a positive integer")
        if not 0 <= self.denoise_kernel <= 63 or int(self.denoise_kernel) != self.denoise_kernel:
            raise ValueError("denoise_kernel must be an integer between 0 and 63")
        if not 0 <= self.max_area_fraction <= 1:
            raise ValueError("max_area_fraction must be between 0 and 1 (0 disables it)")
        if self.max_aspect_ratio < 1 or not math.isfinite(self.max_aspect_ratio):
            raise ValueError("max_aspect_ratio must be finite and at least 1")
        if not 0 <= self.ignore_bottom_fraction <= 1:
            raise ValueError("ignore_bottom_fraction must be between 0 and 1")
        for rect in self.ignore_rects:
            if len(rect) != 4:
                raise ValueError("ignore rectangles must have four normalized coordinates")
            x1, y1, x2, y2 = rect
            if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
                raise ValueError("ignore rectangles must be ordered within [0, 1]")


@dataclass
class DetectionResult:
    blobs: list
    rejected: list
    mask: np.ndarray
    changed_fraction: float
    reason: str
    camera_shift: tuple = (0.0, 0.0)
    camera_reliable: bool = False


def _components(mask):
    count, _, stats, centers = cv2.connectedComponentsWithStats(mask, connectivity=8)
    for label in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[label])
        yield {
            "bbox": (x, y, x + width, y + height),
            "area": area,
            "center": tuple(int(round(float(value))) for value in centers[label]),
        }


def _estimate_translation(previous, current):
    old_gray = cv2.cvtColor(previous[:, :, :3], cv2.COLOR_BGR2GRAY).astype(np.float32)
    new_gray = cv2.cvtColor(current[:, :, :3], cv2.COLOR_BGR2GRAY).astype(np.float32)
    height, width = old_gray.shape
    if np.array_equal(old_gray, new_gray):
        return (0.0, 0.0), True
    if min(height, width) < 16 or min(float(old_gray.std()), float(new_gray.std())) < 2:
        return (0.0, 0.0), False
    window = cv2.createHanningWindow((width, height), cv2.CV_32F)
    shift, response = cv2.phaseCorrelate(old_gray, new_gray, window)
    dx, dy = (float(value) for value in shift)
    reliable = (math.isfinite(response) and response >= 0.25
                and math.isfinite(dx) and math.isfinite(dy)
                and abs(dx) <= width * 0.2 and abs(dy) <= height * 0.2)
    return (dx, dy), reliable


def detect_changes(previous, current, options=None):
    """Return changed components, with no capture, display, I/O or mouse calls.

    Raises ValueError for mismatched/invalid frame arrays or invalid settings.
    ``max_area_fraction=0`` disables the upper area limit. Fractional area is
    measured against the usable (non-excluded, overlapping) frame region.
    ``reason`` is candidates, no_changes, all_rejected, no_usable_region or
    camera_unreliable. Rejections contain a machine-readable ``reason``.
    """
    options = options or DetectionOptions()
    options.validate()
    if (not isinstance(previous, np.ndarray) or not isinstance(current, np.ndarray)
            or previous.shape != current.shape or current.ndim != 3
            or current.shape[2] not in (3, 4) or min(current.shape[:2]) == 0
            or previous.dtype != np.uint8 or current.dtype != np.uint8):
        raise ValueError("frames must be equal-sized, nonempty uint8 BGR/BGRA arrays")

    height, width = current.shape[:2]
    usable = np.ones((height, width), dtype=np.uint8)
    if options.ignore_bottom_fraction:
        usable[int(height * (1 - options.ignore_bottom_fraction)):, :] = 0
    for x1, y1, x2, y2 in options.ignore_rects:
        usable[int(y1 * height):math.ceil(y2 * height),
               int(x1 * width):math.ceil(x2 * width)] = 0

    shift, reliable = (0.0, 0.0), False
    baseline = previous
    if options.compensate_camera:
        shift, reliable = _estimate_translation(previous, current)
        if reliable and (abs(shift[0]) > 0.1 or abs(shift[1]) > 0.1):
            transform = np.float32([[1, 0, shift[0]], [0, 1, shift[1]]])
            baseline = cv2.warpAffine(previous, transform, (width, height),
                                      flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
            overlap = cv2.warpAffine(np.ones((height, width), np.float32), transform,
                                     (width, height), flags=cv2.INTER_LINEAR)
            usable[overlap < 0.999] = 0

    difference = cv2.absdiff(baseline[:, :, :3], current[:, :, :3])
    channel_max = cv2.max(cv2.max(difference[:, :, 0], difference[:, :, 1]), difference[:, :, 2])
    mask = (channel_max > options.threshold).astype(np.uint8)
    rejected = []
    excluded = mask & (1 - usable)
    if np.any(excluded):
        for component in _components(excluded):
            component["reason"] = "ignored_region"
            rejected.append(component)
    mask &= usable
    if options.denoise_kernel > 1:
        kernel = np.ones((int(options.denoise_kernel), int(options.denoise_kernel)), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask &= usable
    usable_count = int(cv2.countNonZero(usable))
    changed_fraction = cv2.countNonZero(mask) / usable_count if usable_count else 0.0

    blobs = []
    for component in _components(mask):
        x1, y1, x2, y2 = component["bbox"]
        width_, height_ = x2 - x1, y2 - y1
        if options.compensate_camera and not reliable:
            reason = "camera_unreliable"
        elif component["area"] < options.min_area:
            reason = "too_small"
        elif options.max_area_fraction and component["area"] > usable_count * options.max_area_fraction:
            reason = "too_large"
        elif max(width_, height_) / min(width_, height_) > options.max_aspect_ratio:
            reason = "too_elongated"
        else:
            blobs.append(component)
            continue
        component["reason"] = reason
        rejected.append(component)
    blobs.sort(key=lambda blob: blob["area"], reverse=True)
    if not usable_count:
        reason = "no_usable_region"
    elif options.compensate_camera and not reliable:
        reason = "camera_unreliable"
    elif blobs:
        reason = "candidates"
    elif cv2.countNonZero(mask):
        reason = "all_rejected"
    else:
        reason = "no_changes"
    return DetectionResult(blobs, rejected, mask, changed_fraction, reason, shift, reliable)


class TargetTracker:
    """Prefer nearby changes, confirm observations, and resist target hopping.

    A target's screen-space centroid and geometry associate detections between
    calls. A bounded area bonus cannot overwhelm distance to the crosshair.
    Challengers must improve the score by switch_margin and persist for the
    configured confirmation count before replacing a visible confirmed target.
    Missing observations never return extrapolated/stale coordinates. Radius=0
    disables retention hysteresis; confirmation still uses a small spatial gate.
    """

    def __init__(self, radius=120, timeout=0.35, confirm_frames=2, switch_margin=0.2):
        self._options = {}
        self.update(radius=radius, timeout=timeout, confirm_frames=confirm_frames,
                    switch_margin=switch_margin)
        self.reset()

    def update(self, **options):
        unknown = set(options) - {"radius", "timeout", "confirm_frames", "switch_margin"}
        if unknown:
            raise ValueError("unknown tracker options: " + ", ".join(sorted(unknown)))
        updated = {**self._options, **options}
        for name, value in updated.items():
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(name + " must be a finite nonnegative number")
        if updated.get("confirm_frames", 1) < 1 or int(updated["confirm_frames"]) != updated["confirm_frames"]:
            raise ValueError("confirm_frames must be a positive integer")
        self._options = updated

    def reset(self):
        self._target = None
        self._challenger = None
        self._hits = 0
        self._challenger_hits = 0
        self._last_seen = None
        self.state = "searching"
        self.reason = "no_target"
        self.locked = False
        self.confidence = 0.0

    @staticmethod
    def _position(blob, origin):
        return origin[0] + blob["center"][0], origin[1] + blob["center"][1]

    @classmethod
    def _record(cls, blob, origin):
        x1, y1, x2, y2 = blob["bbox"]
        return {"position": cls._position(blob, origin), "area": blob["area"],
                "size": max(x2 - x1, y2 - y1)}

    @classmethod
    def _match(cls, record, blobs, origin, radius):
        if record is None:
            return None
        # Bounding geometry tightens the otherwise very permissive lock radius.
        gate = min(radius if radius > 0 else 32, max(16, record["size"] * 2.5))
        best = None
        distance = gate * gate
        for index, blob in enumerate(blobs):
            ratio = blob["area"] / max(1, record["area"])
            if not 0.25 <= ratio <= 4:
                continue
            x, y = cls._position(blob, origin)
            dx, dy = x - record["position"][0], y - record["position"][1]
            squared = dx * dx + dy * dy
            if squared <= distance:
                best, distance = index, squared
        return best

    @classmethod
    def _score(cls, blob, origin, crosshair, radius):
        x, y = cls._position(blob, origin)
        distance = math.hypot(x - crosshair[0], y - crosshair[1])
        proximity = 1.0 / (1.0 + distance / max(32, radius))
        size_bonus = min(1.0, math.sqrt(max(0, blob["area"])) / 32.0)
        return 0.9 * proximity + 0.1 * size_bonus

    def choose(self, blobs, origin, crosshair, now, radius=None, timeout=None,
               confirm_frames=None, switch_margin=None):
        """Return a current confirmed blob, or None while searching/confirming.

        Optional per-call settings override constructor/update defaults. Returned
        blobs are shallow copies with an added bounded ``score``; input lists
        and dictionaries are never changed. Inspect state/reason even on None.
        """
        overrides = {key: value for key, value in {
            "radius": radius, "timeout": timeout, "confirm_frames": confirm_frames,
            "switch_margin": switch_margin}.items() if value is not None}
        settings = {**self._options, **overrides}
        radius, timeout, confirm_frames, switch_margin = (settings[name] for name in
            ("radius", "timeout", "confirm_frames", "switch_margin"))
        if overrides:
            # Validate per-call settings without changing persistent defaults.
            original = self._options
            try:
                self.update(**overrides)
            finally:
                self._options = original
        if not math.isfinite(now):
            raise ValueError("now must be finite monotonic seconds")
        if self._last_seen is not None and (now < self._last_seen or now - self._last_seen > timeout):
            self.reset()
        self.locked = False
        if not blobs:
            self._hits = 0
            self._challenger = None
            self._challenger_hits = 0
            self.state, self.reason = "searching", "target_missing"
            self.confidence = 0.0
            return None

        scores = [self._score(blob, origin, crosshair, radius) for blob in blobs]
        best = max(range(len(blobs)), key=lambda index: scores[index])
        associated = self._match(self._target, blobs, origin, radius)
        was_confirmed = self._hits >= confirm_frames
        chosen = associated if associated is not None else best
        if associated is None:
            self._hits = 1
            self._challenger = None
            self._challenger_hits = 0
        else:
            self._hits += 1

        reason = "target_retained" if was_confirmed and associated is not None else "target_locked"
        if (was_confirmed and associated is not None and best != associated
                and (radius <= 0 or scores[best] > scores[associated] + switch_margin)):
            challenger_match = self._match(self._challenger, [blobs[best]], origin, radius)
            self._challenger_hits = self._challenger_hits + 1 if challenger_match is not None else 1
            self._challenger = self._record(blobs[best], origin)
            if self._challenger_hits >= confirm_frames:
                chosen = best
                self._hits = self._challenger_hits
                self._challenger = None
                self._challenger_hits = 0
                reason = "target_switched"
            else:
                reason = "switch_pending"
        else:
            self._challenger = None
            self._challenger_hits = 0
        self._target = self._record(blobs[chosen], origin)
        self._last_seen = now
        self.confidence = min(1.0, self._hits / confirm_frames)
        if self._hits < confirm_frames:
            self.state, self.reason = "confirming", "confirmation_pending"
            return None
        self.state, self.reason, self.locked = "locked", reason, True
        return {**blobs[chosen], "score": scores[chosen]}
