"""Бутстрап проверяем настоящими запусками: подсовываем питон без модулей."""

import os, sys, subprocess, tempfile, shutil, textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "aim_tracker.py")
STUBS = os.path.join(HERE, "stubs")

SRC = open(SCRIPT, encoding='utf-8').read()

# Проверка подменяет интерпретатор shell-скриптом, а на Windows интерпретатором
# может быть только настоящий .exe — там этот набор пропускается. Логику выбора
# venv на Windows косвенно покрывает test_startup (бутстрап там молчит, потому
# что модули на месте).
if os.name != "posix":
    print("ПРОПУЩЕНО: набор работает только на POSIX (нужен shim-интерпретатор)")
    raise SystemExit(0)

fails = []
def check(label, ok, extra=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{extra}")
    if not ok: fails.append(label)

work = tempfile.mkdtemp()
os.makedirs(os.path.join(work, "proj"))
proj = os.path.join(work, "proj")
open(os.path.join(proj, "aim_tracker.py"), "w", encoding="utf-8").write(SRC)

# "venv" рядом со скриптом: bin/python3 -> наш рабочий интерпретатор с модулями
venv_bin = os.path.join(proj, ".venv", "bin")
os.makedirs(venv_bin)
shim = os.path.join(venv_bin, "python3")
open(shim, "w").write(textwrap.dedent(f"""\
    #!/bin/sh
    # эмулирует интерпретатор venv: у него модули есть
    PYTHONPATH="{STUBS}" exec "{sys.executable}" "$@"
    """))
os.chmod(shim, 0o755)

def run(argv, env_extra=None, stdin=""):
    env = dict(os.environ, **(env_extra or {}))
    env.pop("PYTHONPATH", None)
    env["AIMTRACKER_TEST_EXIT_AFTER"] = "2"
    p = subprocess.run(argv, cwd=proj, input=stdin, capture_output=True,
                       text=True, timeout=40, env=env)
    return p

print("== без модулей скрипт сам перезапускается через venv ==")
# системный python3 модулей не видит (PYTHONPATH снят, numpy/cv2 нет)
p = run([sys.executable, "aim_tracker.py"], stdin="120\n")
out = p.stdout + p.stderr
check("сообщил о перезапуске", "перезапускаюсь через" in out, f"\n      {out[:300]}")
check("назвал путь к venv", ".venv/bin/python3" in out)
check("дошёл до работы скрипта (спросил margin)", "Введите отступ" in out,
      f"\n      {out[-400:]}")

print("== venv нет -> понятное объяснение, а не стектрейс ==")
bare = os.path.join(work, "bare")
os.makedirs(bare)
open(os.path.join(bare, "aim_tracker.py"), "w", encoding="utf-8").write(SRC)
p2 = subprocess.run([sys.executable, "aim_tracker.py"], cwd=bare, input="", capture_output=True,
                    text=True, timeout=40, env={**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
                         "AIMTRACKER_TEST_EXIT_AFTER": "2"})
out2 = p2.stdout + p2.stderr
check("нет стектрейса ImportError", "Traceback" not in out2, f"\n      {out2[:400]}")
check("объяснил, что venv не найден", "venv рядом со скриптом не найден" in out2)
check("показал команду создания venv", "-m venv .venv" in out2)
check("показал путь скрипта и текущий питон", bare in out2 and sys.executable in out2)
check("код выхода 1", p2.returncode == 1, f"  ({p2.returncode})")

print("== AIMTRACKER_PYTHON перекрывает поиск ==")
p3 = run([sys.executable, "aim_tracker.py"], env_extra={"AIMTRACKER_PYTHON": shim}, stdin="120\n")
out3 = p3.stdout + p3.stderr
check("использован указанный интерпретатор", shim in out3, f"\n      {out3[:300]}")

print("== второго круга перезапуска не бывает ==")
p4 = subprocess.run([sys.executable, "aim_tracker.py"], cwd=proj, input="", capture_output=True,
                    text=True, timeout=40,
                    env={**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
                         "AIMTRACKER_BOOTSTRAPPED": "1", "AIMTRACKER_TEST_EXIT_AFTER": "2"})
out4 = p4.stdout + p4.stderr
check("не перезапускается повторно", "перезапускаюсь через" not in out4)
check("сказал, что и в venv модулей нет", "Даже в venv не хватает модулей" in out4,
      f"\n      {out4[:300]}")

print("== ошибка не улетает: печатается и не висит без консоли ==")
broken = os.path.join(work, "broken")
os.makedirs(broken)
open(os.path.join(broken, "aim_tracker.py"), "w", encoding="utf-8").write(
    SRC.replace('require_windows()\n', 'require_windows()\nraise RuntimeError("тестовый сбой")\n', 1))
shim_broken = os.path.join(broken, ".venv", "bin")
os.makedirs(shim_broken)
sh2 = os.path.join(shim_broken, "python3")
open(sh2, "w").write(open(shim).read()); os.chmod(sh2, 0o755)
p5 = subprocess.run([sh2, "aim_tracker.py"], cwd=broken, input="",
                    capture_output=True, text=True, timeout=40,
                    env={**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
                         "AIMTRACKER_TEST_EXIT_AFTER": "2"})
out5 = p5.stdout + p5.stderr
check("стектрейс напечатан", "тестовый сбой" in out5 and "Traceback" in out5)
check("дописал пояснение", "Скрипт остановился из-за ошибки выше" in out5)
check("не завис без tty", True)   # дошли сюда — значит timeout не сработал

print("== pause_on_start ждёт Enter перед слежением ==")
p6 = run([shim, "aim_tracker.py"], stdin="120\n")       # первый запуск создаёт конфиг
import json as _json
cfgpath = os.path.join(proj, "aim_config.json")
data = _json.load(open(cfgpath, encoding="utf-8"))
data["profiles"]["default"]["pause_on_start"] = True
_json.dump(data, open(cfgpath, "w", encoding="utf-8"), ensure_ascii=False)
p7 = run([shim, "aim_tracker.py"], stdin="\n")
out7 = p7.stdout + p7.stderr
check("показал паузу", "[Пауза] Enter — начать слежение" in out7, f"\n      {out7[-300:]}")
check("шапка была до паузы", out7.index("AimTracker") < out7.index("[Пауза]"))
check("после Enter пошёл дальше — появилось приглашение",
      "> " in out7.split("[Пауза]", 1)[1])

shutil.rmtree(work, ignore_errors=True)
print("\nПРОВАЛОВ: %d" % len(fails))
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
