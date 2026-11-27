#!/usr/bin/env python3
"""Step 4 (Semi-Auto Multi-Tab): Maintain one persistent Chrome window and open a new Web IM tab per queued supplier.

- Reads JSONL requests from `data/chat_queue.jsonl`: {"offer_url": "...", "greeting": "..."}
- For each request:
  1. Opens a temporary tab for `offer_url`.
  2. Clicks `od-text[i18n="wangwang"]` to open the Web IM tab (`air.1688.com/.../def_cbu_web_im`).
  3. Closes the temporary detail tab so Chrome only keeps clean chat tabs.
  4. Locates `pre.edit[contenteditable="true"]` inside `def_cbu_web_im_core` and types the greeting with real keystrokes.
- Keeps Chrome open until all tabs are closed so the user can open multiple supplier chats side-by-side.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from runtime_config import PROFILE_DIR, is_browser_profile_locked

BASE_DIR = Path(__file__).resolve().parent
QUEUE_PATH = BASE_DIR / "data" / "chat_queue.jsonl"
DEFAULT_GREETING = "老板你好，想长期拿货，方便留个电话直接沟通吗"


def open_chat_tab(context, offer_url: str, greeting: str, on_im_opened=None) -> None:
    """Open offer_url, click WangWang, close detail tab, and type greeting into the Web IM tab."""
    detail_page = context.new_page()
    try:
        detail_page.goto(offer_url, wait_until="domcontentloaded", timeout=25000)
        detail_page.wait_for_timeout(2500)

        btn = detail_page.locator('od-text[i18n="wangwang"]').first
        if btn.count() == 0:
            print(f"[跳过] 未找到客服按钮: {offer_url}", flush=True)
            return

        with context.expect_page(timeout=15000) as new_page_info:
            btn.click(timeout=5000)
        im_page = new_page_info.value
        im_page.wait_for_load_state("domcontentloaded", timeout=20000)

        # Close intermediate detail tab and initial blank page so the IM tab keeps clean focus
        try:
            detail_page.close()
        except Exception:
            pass
        if on_im_opened is not None:
            on_im_opened()
        try:
            im_page.bring_to_front()
        except Exception:
            pass

        # Wait for the Web IM core iframe & editor box to finish mounting (must pump Playwright events!)
        filled = False
        anchor = greeting[: min(4, len(greeting))]
        for _ in range(80):
            if im_page.is_closed():
                break
            target_box = None
            for frame in im_page.frames:
                if "def_cbu_web_im_core" in frame.url:
                    box = frame.locator('pre.edit[contenteditable="true"]').first
                    try:
                        if box.count() > 0 and box.is_visible():
                            target_box = box
                            break
                    except Exception:
                        pass
            if target_box is not None:
                im_page.wait_for_timeout(1200)
                for _attempt in range(3):
                    try:
                        im_page.bring_to_front()
                        target_box.click(timeout=3000)
                        try:
                            target_box.focus()
                        except Exception:
                            pass
                        current = (target_box.inner_text() or "").strip()
                        if anchor and anchor in current:
                            filled = True
                            break
                        if current:
                            im_page.keyboard.press("ControlOrMeta+A")
                            im_page.keyboard.press("Backspace")
                        im_page.keyboard.type(greeting, delay=15)
                        im_page.wait_for_timeout(600)
                        after = (target_box.inner_text() or "").strip()
                        if not anchor or anchor in after:
                            filled = True
                            break
                    except Exception:
                        pass
                    im_page.wait_for_timeout(800)
                if filled:
                    print(f"[已开新Tab并填入话术] {offer_url}", flush=True)
                    break
            im_page.wait_for_timeout(500)

        if not filled:
            print(f"[已开新Tab但未自动填词] {offer_url}", flush=True)
    except Exception as exc:
        print(f"[异常] 打开聊天失败 ({offer_url}): {exc}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="1688 多Tab半自动发起旺旺聊天")
    parser.add_argument("--queue", default=str(QUEUE_PATH), help="JSONL 任务队列文件路径")
    args = parser.parse_args()

    queue_file = Path(args.queue)
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    if not queue_file.exists():
        queue_file.write_text("", encoding="utf-8")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    processed_lines = 0
    stop_requested = False

    def handle_signal(signum, frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context_closed = False

        def on_context_close() -> None:
            nonlocal context_closed
            context_closed = True

        context.on("close", on_context_close)
        # Close initial blank page after opening first real chat tab
        initial_page = context.pages[0] if context.pages else None
        seen_profile_lock = is_browser_profile_locked()

        def is_browser_gone() -> bool:
            if stop_requested or context_closed:
                return True
            try:
                browser = context.browser
                if browser is not None and not browser.is_connected():
                    return True
                if len(context.pages) == 0:
                    return True
            except Exception:
                return True
            if seen_profile_lock and not is_browser_profile_locked():
                return True
            return False

        try:
            while not is_browser_gone():
                if not seen_profile_lock and is_browser_profile_locked():
                    seen_profile_lock = True

                lines = queue_file.read_text(encoding="utf-8").splitlines()
                while processed_lines < len(lines) and not is_browser_gone():
                    raw = lines[processed_lines].strip()
                    processed_lines += 1
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    url = (item.get("offer_url") or "").strip()
                    greeting = (item.get("greeting") or DEFAULT_GREETING).strip()
                    if url:
                        def close_initial() -> None:
                            nonlocal initial_page
                            if initial_page is not None and not initial_page.is_closed() and len(context.pages) > 1:
                                try:
                                    initial_page.close()
                                except Exception:
                                    pass
                                initial_page = None

                        open_chat_tab(context, url, greeting, on_im_opened=close_initial)
                        close_initial()

                if is_browser_gone():
                    break

                # Pump Playwright's event dispatcher so tab/window closes are observed promptly on macOS.
                pumped = False
                pages = list(context.pages)
                if pages:
                    try:
                        pages[0].wait_for_timeout(400)
                        pumped = True
                    except Exception:
                        pass
                if not pumped:
                    time.sleep(0.25)
        except Exception:
            pass
        finally:
            try:
                if not context_closed:
                    context.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
