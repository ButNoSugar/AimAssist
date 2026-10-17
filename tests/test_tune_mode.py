"""Пункт 10: разметка кадров, кольцевой буфер, пауза и очистка папки."""
import sys, io, os, re, glob, tempfile, ctypes
import sitecustomize  # noqa
import keyboard
import numpy as np
import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "aim_tracker.py")

_SRC = open(SCRIPT, encoding='utf-8').read()
work = tempfile.mkdtemp(); os.chdir(work)
open('aim_tracker.py', 'w', encoding='utf-8').write(_SRC)

fails = []
def check(label, ok, extra=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{extra}")
    if not ok: fails.append(label)

keyboard.PRESSES['ctrl'] = 0; keyboard.EOF[0] = True
sys.stdin = io.StringIO('120\n')
G = {'__name__': '__main__', '__file__': 'aim_tracker.py'}
b = io.StringIO(); real = sys.stdout; sys.stdout = b
exec(compile(_SRC, 'aim_tracker.py', 'exec'), G)
sys.stdout = real
cfg = G['cfg']; cfg.debug_log = False; cfg.color = "off"

def quiet(fn, *a, **k):
    out = io.StringIO(); keep = sys.stdout; sys.stdout = out
    try: fn(*a, **k)
    finally: sys.stdout = keep
    return out.getvalue()

print("== отсеянные блобы отдаются только в отладочном режиме ==")
base = np.zeros((200, 200, 4), np.uint8); base[:, :, 3] = 255
shot = base.copy()
shot[20:40, 20:40, :3] = 255        # крупное пятно
shot[100:102, 100:102, :3] = 255    # мелкое, ниже порога площади
cfg.tune_mode = False
blobs, _ = G['find_blobs_arr'](base, shot, 30, 50, 0)
check("в боевом режиме отсеянные не собираются", G['_detect']["rejected"] == [])
cfg.tune_mode = True
blobs, _ = G['find_blobs_arr'](base, shot, 30, 50, 0)
check("в отладочном собираются", len(G['_detect']["rejected"]) == 1,
      f"  (кандидатов {len(blobs)}, отсеяно {len(G['_detect']['rejected'])})")
check("кандидат остался один", len(blobs) == 1)

print("== панель повторяет макет ==")
lines = G['tune_panel_lines']("Fullscreen", (741, 574), 225, 5, 23.7)
check("заголовок с версией и режимом",
      lines[0].startswith("AimAssist Ver.") and "Fullscreen" in lines[0], f"  {lines[0]!r}")
check("пустая строка после заголовка", lines[1] == "")
check("размеры экрана", lines[2] == f"Screen - {G['screen_width']}x{G['screen_height']}")
check("три порога на месте",
      lines[3].startswith("margin - ") and lines[4].startswith("diff_threshold - ")
      and lines[5].startswith("min_blob_area - ") and lines[6].startswith("denoise_kernel - "))
check("пустая строка и заголовок Detection", lines[7] == "" and lines[8] == "Detection")
check("цель, площадь, пятна, время",
      lines[9] == "Target - 741,574" and lines[10] == "Area - 225 px"
      and lines[11].startswith("Blobs - 5") and lines[12] == "Reaction Time - 23.7 ms",
      f"\n      {lines[9:]}")
check("счётчик отсеянных дописан", "(+1 ignored)" in lines[11], f"  {lines[11]!r}")
check("вся панель латиницей (cv2 не умеет кириллицу)",
      all(ord(ch) < 128 for line in lines for ch in line))

print("== разметка кадра ==")
frame = np.zeros((300, 400, 4), np.uint8); frame[:, :, 3] = 255
primary = {"bbox": (10, 10, 50, 50), "area": 1600, "center": (30, 30)}
other = {"bbox": (100, 100, 130, 130), "area": 900, "center": (115, 115)}
G['_detect']["rejected"] = [{"bbox": (200, 200, 205, 205), "area": 25, "center": (202, 202)}]
canvas = G['annotate_tune_frame'](frame, (0, 0), [primary, other], primary,
                                  (5, 5, 200, 200), "Margin", 12.3)
check("кадр стал трёхканальным", canvas.shape == (300, 400, 3), f"  {canvas.shape}")
def has_colour(img, bgr):
    return bool(np.any(np.all(img == np.array(bgr, np.uint8), axis=2)))
check("цель зелёная", has_colour(canvas, G['TUNE_TARGET_COLOR']))
check("второй кандидат жёлтый", has_colour(canvas, G['TUNE_CANDIDATE_COLOR']))
check("отсеянный серый", has_colour(canvas, G['TUNE_REJECTED_COLOR']))
check("граница области голубая", has_colour(canvas, G['TUNE_REGION_COLOR']))
check("панель красная", has_colour(canvas, G['TUNE_TEXT_COLOR']))

print("== кольцевой буфер ==")
cfg.tune_frames = 5
G['_tune']["frames"].clear()
for _ in range(12):
    G['remember_tune_frame'](canvas[:, :, :3])
check("держим только последние tune_frames", len(G['_tune']["frames"]) == 5,
      f"  ({len(G['_tune']['frames'])})")
check("кадры лежат в JPEG", G['_tune']["frames"][0][:2] == b"\xff\xd8")

print("== сброс на диск ==")
quiet(G['dump_tune_frames'])
folders = glob.glob(os.path.join(work, "tune", "*"))
check("создана папка со сбросом", len(folders) == 1, f"  {folders}")
files = sorted(glob.glob(os.path.join(folders[0], "*.jpg"))) if folders else []
check("в ней все кадры буфера", len(files) == 5, f"  ({len(files)})")
check("файлы читаются как картинки", bool(files) and cv2.imread(files[0]) is not None)
quiet(G['dump_tune_frames'])
check("второй сброс в отдельную папку, не поверх",
      len(glob.glob(os.path.join(work, "tune", "*"))) == 2)

print("== клавиша стоп в отладке: пауза, потом очистка ==")
G['_tune']["paused"] = False
out = quiet(G['panic_stop'])
check("первое нажатие — пауза, не выход",
      G['_tune']["paused"] is True and not G['_stop'].is_set(), f"  {out.strip()[:60]!r}")
check("сказано, что копировать надо самому", "Скопируй нужное" in out)
out = quiet(G['panic_stop'])
check("второе нажатие сняло паузу", G['_tune']["paused"] is False)
check("папка отладки очищена", not os.path.isdir(os.path.join(work, "tune")),
      f"  {glob.glob(os.path.join(work, 'tune', '*'))}")

print("== в боевом режиме клавиша стоп по-прежнему останавливает ==")
cfg.tune_mode = False
G['_tune']["paused"] = False
quiet(G['panic_stop'])
check("скрипт получил команду на выход", G['_stop'].is_set() is True)
G['_stop'].clear()

print("\nПРОВАЛОВ: %d" % len(fails))
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
