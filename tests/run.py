"""Прогоняет все наборы тестов. Запуск: python tests/run.py

Заглушки Windows/GUI-зависимостей подкладываются через PYTHONPATH, поэтому
наборы идут и на Linux, где ни одного нужного WinAPI нет.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ["test_bootstrap.py", "test_startup.py", "test_console.py",
          "test_color_and_help.py", "test_feedback_and_keys.py",
          "test_overlay_hold.py", "test_tune_mode.py"]
PURE_SUITES = ["test_detection.py", "test_recording.py", "test_ui_model.py", "test_panel_bridge.py"]
SUITES += ["test_regressions.py"] + PURE_SUITES


def main():
    env = dict(os.environ)
    root = os.path.dirname(HERE)
    original_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([os.path.join(HERE, "stubs"), root, original_path])
    env.pop("NO_COLOR", None)
    env["COLUMNS"] = "78"

    verbose = "-v" in sys.argv
    failed = []
    for suite in SUITES:
        print(f"\n=== {suite} ===")
        suite_env = env.copy()
        if suite in PURE_SUITES:
            suite_env["PYTHONPATH"] = os.pathsep.join([root, original_path])
        result = subprocess.run([sys.executable, os.path.join(HERE, suite)],
                                env=suite_env, capture_output=not verbose, text=True)
        if not verbose:
            tail = (result.stdout or "").splitlines()
            for line in tail:
                if line.startswith(("  FAIL", "ПРОПУЩЕНО", "ПРОВАЛОВ")) or line.startswith("=="):
                    print(line)
            if result.returncode and result.stderr:
                print(result.stderr.strip()[-800:])
            elif suite in PURE_SUITES or suite == "test_regressions.py":
                print((result.stderr or result.stdout or "").strip()[-500:])
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
