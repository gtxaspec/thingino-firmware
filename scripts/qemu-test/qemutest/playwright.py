"""Browser test runner."""

import os
import subprocess

from .config import SCRIPT_DIR


def run_playwright(res, report_dir, mode, urls, timeout=120,
                  check_name="playwright_tests"):
    print("\n── Playwright ──")
    script_dir = SCRIPT_DIR
    env = os.environ.copy()
    env["REPORT_DIR"] = report_dir
    env.update(urls)
    if os.geteuid() == 0:
        env["CHROMIUM_NO_SANDBOX"] = "1"
        # browsers live in the invoking user's cache, not root's
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user and "PLAYWRIGHT_BROWSERS_PATH" not in env:
            import pwd
            home = pwd.getpwnam(sudo_user).pw_dir
            env["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
                home, ".cache", "ms-playwright")
    npx = os.environ.get("NPX_BIN") or "npx"
    pw = subprocess.run(
        [npx, "playwright", "test",
         "--config", os.path.join(script_dir, "playwright.config.js")],
        cwd=script_dir, env=env, capture_output=True, text=True,
        timeout=timeout)
    print(pw.stdout)
    if pw.stderr:
        print(pw.stderr[:500])
    res.check(check_name, pw.returncode == 0, f"exit {pw.returncode}")
    return pw.returncode == 0
