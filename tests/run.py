"""Прогоняет все наборы тестов. Запуск: python tests/run.py

Заглушки Windows/GUI-зависимостей подкладываются через PYTHONPATH, поэтому
наборы идут и на Linux, где ни одного нужного WinAPI нет.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ["test_bootstrap.py", "test_startup.py", "test_console.py",
          "test_color_and_help.py"]


def main():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(HERE, "stubs") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("NO_COLOR", None)
    env["COLUMNS"] = "78"

    verbose = "-v" in sys.argv
    failed = []
    for suite in SUITES:
        print(f"\n=== {suite} ===")
        result = subprocess.run([sys.executable, os.path.join(HERE, suite)],
                                env=env, capture_output=not verbose, text=True)
        if not verbose:
            tail = (result.stdout or "").splitlines()
            for line in tail:
                if line.startswith(("  FAIL", "ПРОПУЩЕНО", "ПРОВАЛОВ")) or line.startswith("=="):
                    print(line)
            if result.returncode and result.stderr:
                print(result.stderr.strip()[-800:])
        if result.returncode:
            failed.append(suite)

    print()
    if failed:
        print(f"ПРОВАЛИЛИСЬ: {', '.join(failed)}")
        print("Подробности: python tests/run.py -v")
        return 1
    print("Все наборы прошли.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
