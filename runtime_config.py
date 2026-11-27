#!/usr/bin/env python3
"""Shared runtime path, AK configuration, and cross-platform helper utilities."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
PROFILE_DIR = BASE_DIR / "browser_profile"

DEFAULT_CONFIG: dict[str, str] = {
    "ak": "",
    "wangwang_greeting": "老板你好，想长期拿货，方便留个电话直接沟通吗",
}


def get_python_bin() -> str:
    """Resolve the portable runtime Python executable (Mac/Win) before falling back to .venv or sys.executable."""
    candidates = [
        BASE_DIR / "runtime" / "bin" / "python3",
        BASE_DIR / "runtime" / "python3",
        BASE_DIR / "runtime" / "python.exe",
        BASE_DIR / ".venv" / "Scripts" / "python.exe",
        BASE_DIR / ".venv" / "bin" / "python",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def load_config() -> dict[str, str]:
    """Load local config.json merged with defaults (creating a blank template if missing)."""
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, str):
                        cfg[k] = v
        except Exception:
            pass
    else:
        save_config(cfg)
    return cfg


def save_config(updates: dict[str, str]) -> dict[str, str]:
    """Persist updates to config.json cleanly."""
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, str):
                        cfg[k] = v
        except Exception:
            pass
    for k, v in updates.items():
        if isinstance(v, str):
            cfg[k] = v.strip()
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cfg


def get_effective_ak() -> str:
    """Return configured AK from config.json (or fallback to ALI_1688_AK env var)."""
    cfg = load_config()
    ak = (cfg.get("ak") or "").strip()
    if ak:
        return ak
    return (os.environ.get("ALI_1688_AK") or "").strip()


def build_utf8_env() -> dict[str, str]:
    """Return process environment forcing UTF-8 stdio (prevents GBK errors on Windows)."""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def build_skill_env(extra_ak: str | None = None) -> dict[str, str]:
    """Build subprocess environment with isolated AGENT_WORK_ROOT, UTF-8 stdio, and user AK."""
    env = build_utf8_env()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    env["AGENT_WORK_ROOT"] = str(DATA_DIR)
    ak = (extra_ak if extra_ak is not None else get_effective_ak()).strip()
    if ak:
        env["ALI_1688_AK"] = ak
    return env


def terminate_process_tree(proc: subprocess.Popen | None, timeout: float = 3.0) -> None:
    """Terminate a child process and (on Windows) its Playwright/Chrome descendants cleanly."""
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=creationflags,
                timeout=5,
            )
            proc.wait(timeout=2)
            return
        except Exception:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def is_browser_profile_locked() -> bool:
    """Check whether Chrome currently holds a lock on browser_profile across macOS/Linux/Windows."""
    if sys.platform == "win32":
        # Windows Chrome does not create SingletonLock symlink; check lockfile if present.
        return (PROFILE_DIR / "lockfile").exists()
    return os.path.lexists(PROFILE_DIR / "SingletonLock")


def find_system_chrome() -> str | None:
    """Return path to installed Google Chrome on macOS/Windows if detectable."""
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        for p in candidates:
            if Path(p).exists():
                return p
    elif sys.platform == "win32":
        candidates = [
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
        for p in candidates:
            if p and Path(p).exists():
                return p
    return None
