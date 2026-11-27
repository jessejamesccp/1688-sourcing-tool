#!/usr/bin/env python3
"""Step 3: Extract seller contact phone numbers from 1688 product detail URLs.

Strategy:
1. Reuse a persistent local Chrome profile (./browser_profile) so you only scan QR to log in once.
2. Open each 1688 offer page -> extract product title, shop name, shop URL (deduplicate by shop).
3. Priority 1: Find the 1688 Factory page (f.1688.com/factory/...) -> click "电话联系" -> extract phone.
4. Priority 2: Fallback to <shop_origin>/page/contactinfo.htm -> extract public phone/contact person,
   filtering out placeholder numbers like 000860000.
5. Priority 3: Mark remaining shops without public phone as "公司页无公开实号" / "无工厂档案".
6. Save progress in SQLite (data/state.db).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from runtime_config import build_skill_env, get_python_bin

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser_profile"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "state.db"
SUPPLIER_SKILL_CLI = BASE_DIR / "skills" / "1688-source-suppliers" / "cli.py"

PLACEHOLDER_PHONES = {"000860000", "00000000", "00000000000", "0000", "暂无"}
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9}|0\d{2,3}-?\d{7,8})(?!\d)")
OFFER_ID_RE = re.compile(r"/offer/(\d+)\.html")


def init_db() -> sqlite3.Connection:
    """Initialize SQLite state database for resume & shop deduplication."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            offer_id TEXT NOT NULL UNIQUE,
            offer_url TEXT NOT NULL,
            product_title TEXT,
            shop_name TEXT,
            shop_origin TEXT,
            factory_url TEXT,
            contact_person TEXT,
            phone TEXT,
            virtual_phone TEXT DEFAULT '',
            virtual_expires_at TEXT DEFAULT '',
            real_phone TEXT DEFAULT '',
            phone_source TEXT,
            status TEXT,
            note TEXT,
            follow_status TEXT DEFAULT '待跟进',
            follow_note TEXT DEFAULT '',
            updated_at TEXT
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(contacts)").fetchall()}
    if "virtual_phone" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN virtual_phone TEXT DEFAULT ''")
    if "virtual_expires_at" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN virtual_expires_at TEXT DEFAULT ''")
    if "real_phone" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN real_phone TEXT DEFAULT ''")
    conn.commit()
    return conn


def upsert_contact(
    conn: sqlite3.Connection,
    offer_id: str,
    offer_url: str,
    *,
    product_title: str = "",
    shop_name: str = "",
    shop_origin: str = "",
    factory_url: str = "",
    contact_person: str = "",
    virtual_phone: str = "",
    virtual_expires_at: str = "",
    real_phone: str = "",
    phone_source: str = "",
    status: str = "",
    note: str = "",
) -> None:
    """Insert or update contact without overwriting existing virtual_phone or real_phone."""
    prev = conn.execute(
        """
        SELECT product_title, shop_name, shop_origin, factory_url, contact_person,
               virtual_phone, virtual_expires_at, real_phone, phone_source, status, note
        FROM contacts WHERE offer_id = ?
        """,
        (offer_id,),
    ).fetchone()

    if prev:
        product_title = product_title or prev[0] or ""
        shop_name = shop_name or prev[1] or ""
        shop_origin = shop_origin or prev[2] or ""
        factory_url = factory_url or prev[3] or ""
        contact_person = contact_person or prev[4] or ""
        if not virtual_phone:
            virtual_phone = prev[5] or ""
            virtual_expires_at = prev[6] or ""
        if not real_phone:
            real_phone = prev[7] or ""
        phone_source = phone_source or prev[8] or ""
        if not status or (status != "已获取电话" and (virtual_phone or real_phone)):
            status = "已获取电话" if (virtual_phone or real_phone) else (status or prev[9] or "")
        note = note or prev[10] or ""

    primary_phone = real_phone or virtual_phone
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute(
        """
        INSERT INTO contacts (
            offer_id, offer_url, product_title, shop_name, shop_origin, factory_url,
            contact_person, phone, virtual_phone, virtual_expires_at, real_phone,
            phone_source, status, note, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(offer_id) DO UPDATE SET
            offer_url = excluded.offer_url,
            product_title = excluded.product_title,
            shop_name = excluded.shop_name,
            shop_origin = excluded.shop_origin,
            factory_url = excluded.factory_url,
            contact_person = excluded.contact_person,
            phone = excluded.phone,
            virtual_phone = excluded.virtual_phone,
            virtual_expires_at = excluded.virtual_expires_at,
            real_phone = excluded.real_phone,
            phone_source = excluded.phone_source,
            status = excluded.status,
            note = excluded.note,
            updated_at = excluded.updated_at
        """,
        (
            offer_id,
            offer_url,
            product_title,
            shop_name,
            shop_origin,
            factory_url,
            contact_person,
            primary_phone,
            virtual_phone,
            virtual_expires_at,
            real_phone,
            phone_source,
            status,
            note,
            now_str,
        ),
    )
    conn.commit()


def load_urls(input_path: Path) -> list[str]:
    """Load 1688 offer URLs from a .txt file (one URL per line)."""
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    urls: list[str] = []
    for line in input_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "1688.com/offer/" in line:
            urls.append(line)
    return urls


def extract_offer_id(url: str) -> str:
    match = OFFER_ID_RE.search(url)
    return match.group(1) if match else url


def check_human_intervention(page: Page) -> None:
    """Pause and wait for user if a login wall or slider CAPTCHA is detected."""
    while True:
        current_url = page.url
        body_text = ""
        try:
            body_text = page.locator("body").inner_text(timeout=3000)
        except Exception:
            pass

        needs_login = "login.1688.com" in current_url or "login.taobao.com" in current_url
        has_slider = any(
            kw in body_text
            for kw in ("拖动下方滑块完成验证", "请按住滑块", "滑动验证", "请完成安全验证")
        )
        if not needs_login and not has_slider:
            return

        reason = "需要登录 1688" if needs_login else "触发了滑块/安全验证"
        if not sys.stdin.isatty():
            # Launched from Web UI (no interactive terminal): wait up to 5 min for user to scan QR or slide CAPTCHA.
            print(f"[等待人工] {reason}，请在弹出的 Chrome 窗口扫码登录或完成验证（完成后自动继续）...", flush=True)
            for _ in range(150):
                time.sleep(2)
                try:
                    u = page.url
                    t = page.locator("body").inner_text(timeout=2000)
                except Exception:
                    raise RuntimeError("浏览器窗口已关闭")
                if (
                    "login.1688.com" not in u
                    and "login.taobao.com" not in u
                    and not any(kw in t for kw in ("拖动下方滑块完成验证", "请按住滑块", "滑动验证", "请完成安全验证"))
                ):
                    page.wait_for_timeout(1000)
                    return
            raise RuntimeError(f"{reason}超时（超过5分钟），请手动完成后重试")
        print(f"\n[暂停] {reason}！请在弹出的 Chrome 窗口中手动完成操作。")
        input("完成后请回到终端按【回车】继续...")
        time.sleep(2)


def clean_phone(raw: str) -> Optional[str]:
    """Extract and validate a Chinese mobile or landline number."""
    if not raw:
        return None
    for match in PHONE_RE.findall(raw):
        normalized = match.strip()
        if normalized not in PLACEHOLDER_PHONES and not set(normalized.replace("-", "")) <= {"0"}:
            return normalized
    return None


def find_factory_url(page: Page) -> Optional[str]:
    """Locate f.1688.com/factory/... link on the detail page (including hover shop card)."""
    # 1. Direct link already in DOM
    direct = page.locator('a[href*="f.1688.com/factory"]').first
    if direct.count() > 0:
        href = direct.get_attribute("href")
        if href:
            return ("https:" + href) if href.startswith("//") else href

    # 2. Hover over the shop name area to trigger the floating shop/factory card
    shop_selectors = [
        ".shop-company-name",
        '[class*="shopName"]',
        'a[href*=".1688.com"][href*="offerId="]',
        'a[href*=".1688.com"]',
    ]
    for sel in shop_selectors:
        loc = page.locator(sel).first
        if loc.count() > 0:
            try:
                loc.hover(timeout=2000)
                page.wait_for_timeout(1200)
                hover_link = page.locator('a[href*="f.1688.com/factory"]').first
                if hover_link.count() > 0:
                    href = hover_link.get_attribute("href")
                    if href:
                        return ("https:" + href) if href.startswith("//") else href
            except Exception:
                continue
    return None


def extract_shop_info(page: Page) -> tuple[str, str]:
    """Extract shop name and shop origin (https://shopxxx.1688.com) from detail page."""
    shop_name = ""
    shop_origin = ""

    links = page.locator('a[href*=".1688.com"]').all()
    for link in links[:40]:
        try:
            href = link.get_attribute("href") or ""
            if href.startswith("//"):
                href = "https:" + href
            parsed = urlparse(href)
            host = parsed.netloc.lower()
            if host.endswith(".1688.com") and host not in {
                "detail.1688.com",
                "www.1688.com",
                "s.1688.com",
                "f.1688.com",
                "login.1688.com",
                "pages.1688.com",
            }:
                text = link.inner_text(timeout=500).strip()
                if text and len(text) >= 2 and "进入" not in text:
                    shop_name = text.splitlines()[0].strip()
                shop_origin = f"https://{host}"
                if shop_name and shop_origin:
                    break
        except Exception:
            continue

    return shop_name, shop_origin


def try_factory_phone(page: Page, factory_url: str) -> tuple[Optional[str], str, str]:
    """Open f.1688.com/factory/... or sale.1688.com/factory/card.html, click '电话联系', and read the phone modal."""
    page.goto(factory_url, wait_until="domcontentloaded", timeout=15000)
    page.wait_for_timeout(2500)
    check_human_intervention(page)

    contact_person = ""
    name_loc = page.locator(".row1-name").first
    if name_loc.count() > 0:
        try:
            if name_loc.is_visible():
                contact_person = name_loc.inner_text(timeout=1000).strip()
        except Exception:
            pass

    btn_selectors = [
        '.header-right-row3 img.row3-btn',
        'button:has-text("电话联系")',
        'a:has-text("电话联系")',
        ':text("电话联系")',
    ]
    btn = None
    for sel in btn_selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                btn = loc
                break
        except Exception:
            continue

    if btn is None:
        return None, contact_person, "工厂页未开通电话联系按钮"

    btn.click(timeout=3000)
    page.wait_for_timeout(2000)
    check_human_intervention(page)

    # Check modal / dialog or updated page text for phone number
    modal_selectors = [
        ".next-dialog-body",
        '[role="dialog"]',
        ".ant-modal-body",
        '[class*="modal"]',
        "body",
    ]
    for sel in modal_selectors:
        loc = page.locator(sel).first
        if loc.count() > 0:
            try:
                text = loc.inner_text(timeout=1500)
                phone = clean_phone(text)
                if phone:
                    note = "平台隐私虚拟号" if ("虚拟号码" in text or "隐私" in text) else "工厂页电话联系"
                    return phone, contact_person, note
            except Exception:
                continue

    return None, contact_person, "点击电话联系后未识别到有效号码"


def find_factory_from_shop(page: Page) -> Optional[str]:
    """On a shop page (e.g. contactinfo.htm), click '工厂档案' tab to capture the factory card URL."""
    tab = page.locator('a:has-text("工厂档案")').first
    if tab.count() == 0:
        return None
    href = tab.get_attribute("href")
    if href and "1688.com" in href:
        return ("https:" + href) if href.startswith("//") else href
    try:
        with page.context.expect_page(timeout=6000) as new_page_info:
            tab.click(timeout=3000)
        new_page = new_page_info.value
        new_page.wait_for_load_state("domcontentloaded", timeout=15000)
        factory_url = new_page.url
        new_page.close()
        if "factory" in factory_url:
            return factory_url
    except Exception:
        pass
    return None


def try_contactinfo_page(page: Page, shop_origin: str) -> tuple[Optional[str], str, str, Optional[str]]:
    """Fallback: visit <shop_origin>/page/contactinfo.htm and parse public phone/contact person."""
    contact_url = f"{shop_origin}/page/contactinfo.htm"
    page.goto(contact_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    check_human_intervention(page)

    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return None, "", "联系方式页读取失败", None

    contact_person = ""
    person_match = re.search(r"联系人[:：\s]*([^\n\r]+)", body_text)
    if person_match:
        contact_person = person_match.group(1).strip()

    # Look specifically near 移动电话 / 电话 / 手机 labels first
    for label in ("移动电话", "手机", "电话"):
        m = re.search(rf"{label}[:：\s]*([0-9\-\s]{{5,20}})", body_text)
        if m:
            phone = clean_phone(m.group(1))
            if phone:
                return phone, contact_person, f"店铺联系方式页({label})", None

    factory_url = find_factory_from_shop(page)
    return None, contact_person, "店铺联系方式页无有效公开电话", factory_url


def lookup_factory_url_via_skill(conn: sqlite3.Connection, supplier_name: str) -> Optional[str]:
    """Look up factory card URL from `suppliers` cache or 1688-source-suppliers skill."""
    supplier_name = (supplier_name or "").strip()
    if not supplier_name:
        return None
    try:
        row = conn.execute("SELECT company_url FROM suppliers WHERE supplier = ?", (supplier_name,)).fetchone()
        if row is not None:
            return row[0] or None
    except sqlite3.OperationalError:
        pass

    if not SUPPLIER_SKILL_CLI.exists():
        return None
    python_bin = get_python_bin()
    query_name = re.sub(r"[\(（].*?[\)）]", "", supplier_name).strip() or supplier_name
    try:
        proc = subprocess.run(
            [python_bin, str(SUPPLIER_SKILL_CLI), "ali_1688_source_suppliers", "--query", query_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=build_skill_env(),
        )
        data = json.loads(proc.stdout or "{}")
        factories = data.get("data", {}).get("factories", [])
        match = next((f for f in factories if (f.get("companyName") or "").strip() == supplier_name), None)
        if not match and factories:
            match = next((f for f in factories if query_name and query_name in (f.get("companyName") or "")), None)
        url = (match or {}).get("companyUrl", "")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier TEXT NOT NULL UNIQUE,
                company_url TEXT DEFAULT '',
                location TEXT DEFAULT '',
                factory_level TEXT DEFAULT '',
                monthly_orders INTEGER DEFAULT 0,
                rec_tags TEXT DEFAULT '',
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO suppliers
            (supplier, company_url, location, factory_level, monthly_orders, rec_tags, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(supplier) DO UPDATE SET
                company_url = excluded.company_url,
                location = excluded.location,
                factory_level = excluded.factory_level,
                monthly_orders = excluded.monthly_orders,
                rec_tags = excluded.rec_tags,
                updated_at = excluded.updated_at
            """,
            (
                supplier_name,
                url,
                (match or {}).get("location", ""),
                (match or {}).get("factoryLevel", ""),
                int((match or {}).get("monthlyOrders") or 0),
                ", ".join((match or {}).get("recTags") or []),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        return url or None
    except Exception:
        return None


def extract_detail_metadata(page: Page) -> dict[str, str]:
    """Extract product_title, shop_name, shop_origin, and factory_url from detail.1688.com HTML."""
    product_title = ""
    try:
        product_title = page.title().replace(" - 阿里巴巴", "").strip()
    except Exception:
        pass

    shop_name, shop_origin = extract_shop_info(page)
    html = ""
    try:
        html = page.content()
    except Exception:
        pass

    if not shop_origin and html:
        m_shop = re.search(r"(?:https?:)?//(shop[a-zA-Z0-9]+\.1688\.com)", html)
        if m_shop:
            shop_origin = f"https://{m_shop.group(1)}"

    seller_member_id = ""
    is_factory_dealer = False
    if html:
        if '"isFactoryDealer":true' in html:
            is_factory_dealer = True
        m_seller = re.search(
            r'"(?:sellerMemberId|frontSellerMemberId)"\s*:\s*"(b2b-[a-zA-Z0-9]+)"',
            html,
        )
        if m_seller:
            seller_member_id = m_seller.group(1)

    # Only construct factory card URL when the seller is actually a factory (isFactoryDealer=true)
    if is_factory_dealer and seller_member_id:
        factory_url = f"https://sale.1688.com/factory/card.html?memberId={seller_member_id}"
    else:
        factory_url = find_factory_url(page) or ""
    return {
        "product_title": product_title,
        "shop_name": shop_name,
        "shop_origin": shop_origin,
        "factory_url": factory_url,
    }


def process_offer(
    page: Page,
    conn: sqlite3.Connection,
    offer_url: str,
    mode: str = "factory",
    supplier: str = "",
    factory_url: str = "",
    overwrite: bool = False,
) -> None:
    """Process a single 1688 offer URL and update SQLite without overwriting existing other phone type."""
    offer_id = extract_offer_id(offer_url)

    existing = conn.execute(
        "SELECT status, virtual_phone, real_phone, product_title, shop_origin, factory_url, shop_name FROM contacts WHERE offer_id = ?",
        (offer_id,),
    ).fetchone()
    if not overwrite:
        if mode == "factory" and existing and existing[1]:
            print(f"[跳过] 商品 {offer_id} 已有虚拟号: {existing[1]}")
            return
        if mode == "company" and existing and existing[2]:
            print(f"[跳过] 商品 {offer_id} 已有实号: {existing[2]}")
            return

    matched_row = None
    try:
        matched_row = conn.execute(
            "SELECT shop_origin, factory_url, detail_checked, title, supplier FROM matched_offers WHERE product_id = ? ORDER BY detail_checked DESC LIMIT 1",
            (offer_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        pass

    resolved_supplier = supplier or (existing[6] if existing else "") or (matched_row[4] if matched_row else "") or ""

    cached_shop_origin = (existing[4] if existing else "") or (matched_row[0] if matched_row else "") or ""
    cached_factory_url = factory_url or (existing[5] if existing else "") or (matched_row[1] if matched_row else "") or ""
    detail_already_checked = bool(matched_row and matched_row[2] == 1)

    # Mode 'factory' (获取虚拟号):
    # 1) If Step 2 already ran detail-page logic, reuse cached shop_origin & factory_url directly without reopening detail page.
    # 2) Otherwise fallback to skill or open detail page -> extract sellerMemberId -> construct factory card URL directly.
    if mode in ("factory", "auto"):
        detail_meta: dict[str, str] = {"product_title": (existing[3] if existing else "") or (matched_row[3] if matched_row else "") or ""}
        if detail_already_checked:
            resolved_factory_url = cached_factory_url
        else:
            resolved_factory_url = cached_factory_url or lookup_factory_url_via_skill(conn, resolved_supplier)

        if not resolved_factory_url and not detail_already_checked:
            print(f"\n[未命中缓存，进详情页判断工厂档案与店铺主页] {offer_url}")
            page.goto(offer_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            check_human_intervention(page)
            detail_meta = extract_detail_metadata(page)
            resolved_factory_url = detail_meta["factory_url"]
            if detail_meta["shop_origin"]:
                cached_shop_origin = detail_meta["shop_origin"]
            if detail_meta["shop_name"] and not resolved_supplier:
                resolved_supplier = detail_meta["shop_name"]
            # Cache detail-page result back into matched_offers & suppliers table
            try:
                conn.execute(
                    "UPDATE matched_offers SET shop_origin = ?, factory_url = ?, detail_checked = 1 WHERE product_id = ?",
                    (cached_shop_origin, resolved_factory_url, offer_id),
                )
            except sqlite3.OperationalError:
                pass
            if resolved_supplier:
                try:
                    conn.execute(
                        "INSERT INTO suppliers (supplier, company_url, updated_at) VALUES (?, ?, ?) ON CONFLICT(supplier) DO UPDATE SET company_url = excluded.company_url, updated_at = excluded.updated_at",
                        (resolved_supplier, resolved_factory_url, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    )
                    conn.commit()
                except sqlite3.OperationalError:
                    pass

        if resolved_factory_url:
            print(f"\n[访问工厂档案页] {resolved_supplier or offer_id} -> {resolved_factory_url}")
            try:
                v_phone, f_person, f_note = try_factory_phone(page, resolved_factory_url)
                if v_phone:
                    expires_at = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
                    print(f"  [成功] 拿到虚拟号: {v_phone} (截止: {expires_at}, 联系人: {f_person or '-'})")
                    upsert_contact(
                        conn,
                        offer_id,
                        offer_url,
                        product_title=detail_meta.get("product_title", ""),
                        shop_name=resolved_supplier,
                        shop_origin=cached_shop_origin,
                        factory_url=resolved_factory_url,
                        contact_person=f_person,
                        virtual_phone=v_phone,
                        virtual_expires_at=expires_at,
                        phone_source="工厂档案页(虚拟号)",
                        status="已获取电话",
                        note=f_note,
                    )
                    return
                if mode == "factory":
                    upsert_contact(
                        conn,
                        offer_id,
                        offer_url,
                        product_title=detail_meta.get("product_title", ""),
                        shop_name=resolved_supplier,
                        shop_origin=cached_shop_origin,
                        factory_url=resolved_factory_url,
                        contact_person=f_person,
                        status="工厂页无虚拟号",
                        note=f_note,
                    )
                    return
            except PlaywrightTimeoutError:
                print("  [超时] 工厂档案页超时")
                if mode == "factory":
                    upsert_contact(
                        conn,
                        offer_id,
                        offer_url,
                        product_title=detail_meta.get("product_title", ""),
                        shop_name=resolved_supplier,
                        shop_origin=cached_shop_origin,
                        factory_url=resolved_factory_url,
                        status="工厂页超时",
                        note="工厂档案页加载超时",
                    )
                    return
        elif mode == "factory":
            print(f"  [非工厂店] {resolved_supplier or offer_id} 无工厂档案页，跳过虚拟号抓取（可点获取实号）")
            upsert_contact(
                conn,
                offer_id,
                offer_url,
                product_title=detail_meta.get("product_title", ""),
                shop_name=resolved_supplier,
                shop_origin=cached_shop_origin,
                factory_url="",
                status="无工厂档案(请点获取实号)",
                note="经销/非工厂店铺，无工厂档案页",
            )
            return

    # Mode 'company' (获取实号):
    # Ensure we have shop_origin (use cached if already extracted, otherwise open detail page to get shop_origin + factory_url)
    product_title = (existing[3] if existing else "") or (matched_row[3] if matched_row else "") or ""
    shop_name = resolved_supplier
    shop_origin = cached_shop_origin

    if not shop_origin:
        print(f"\n[进详情页提取店铺主页] {offer_url}")
        page.goto(offer_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        check_human_intervention(page)
        meta = extract_detail_metadata(page)
        product_title = meta["product_title"] or product_title
        shop_name = meta["shop_name"] or shop_name
        shop_origin = meta["shop_origin"]
        cached_factory_url = meta["factory_url"]
        try:
            conn.execute(
                "UPDATE matched_offers SET shop_origin = ?, factory_url = ?, detail_checked = 1 WHERE product_id = ?",
                (shop_origin, cached_factory_url, offer_id),
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass
        print(f"  商品: {product_title[:40]}...")
        print(f"  店铺: {shop_name or '(未识别)'} | {shop_origin or '(未识别)'}")
    else:
        print(f"\n[直接访问公司联系方式页] {shop_name or offer_id} -> {shop_origin}/page/contactinfo.htm")

    if shop_origin:
        dup = conn.execute(
            "SELECT real_phone, contact_person, phone_source FROM contacts WHERE shop_origin = ? AND offer_id != ? AND real_phone != '' LIMIT 1",
            (shop_origin, offer_id),
        ).fetchone()
        if dup:
            print(f"  [店铺去重] 该店铺已有实号: {dup[0]}")
            upsert_contact(
                conn,
                offer_id,
                offer_url,
                product_title=product_title,
                shop_name=shop_name,
                shop_origin=shop_origin,
                factory_url=cached_factory_url,
                contact_person=dup[1] or "",
                real_phone=dup[0],
                phone_source=dup[2] or "店铺联系方式页(实号)",
                status="已获取电话",
                note="同店铺实号复用",
            )
            return

    r_phone: Optional[str] = None
    contact_person = ""
    phone_source = ""
    note = ""

    if shop_origin:
        try:
            r_phone, c_person, note, _ = try_contactinfo_page(page, shop_origin)
            if c_person:
                contact_person = c_person
            if r_phone:
                phone_source = "店铺联系方式页(实号)"
                print(f"  [成功] 拿到公司页实际电话: {r_phone} (联系人: {contact_person or '-'})")
        except PlaywrightTimeoutError:
            note = "联系方式页超时"
    else:
        note = "未识别到店铺主页"

    status = "已获取电话" if r_phone else "公司页无公开实号"
    upsert_contact(
        conn,
        offer_id,
        offer_url,
        product_title=product_title,
        shop_name=shop_name,
        shop_origin=shop_origin,
        factory_url=cached_factory_url,
        contact_person=contact_person,
        real_phone=r_phone or "",
        phone_source=phone_source,
        status=status,
        note=note,
    )


def main() -> int:
    def _handle_sigterm(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    parser = argparse.ArgumentParser(description="1688 详情页批量提取卖家联系方式")
    parser.add_argument("--login", action="store_true", help="打开 Chrome 扫码登录 1688 并保存登录态")
    parser.add_argument("--input", default="", help="可选：商品链接列表文件 (.txt)")
    parser.add_argument("--tasks", default="", help="JSON 任务文件 ([{offer_url, supplier, factory_url}])")
    parser.add_argument("--mode", choices=["factory", "company", "auto"], default="factory", help="抓取方式: factory(默认直达工厂档案页) / company(去公司页找实号) / auto")
    parser.add_argument("--overwrite", action="store_true", help="忽略已有同类型电话，强制重新抓取并覆盖上次结果")
    parser.add_argument("--min-delay", type=int, default=8, help="每条之间的最小间隔秒数")
    parser.add_argument("--max-delay", type=int, default=16, help="每条之间的最大间隔秒数")
    args = parser.parse_args()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    conn = init_db()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        if args.login:
            page.goto("https://login.1688.com/", wait_until="domcontentloaded")
            if sys.stdin.isatty():
                print("\n请在弹出的 Chrome 窗口中扫码登录 1688。")
                input("登录成功后，回到终端按【回车】保存登录态并退出...")
            else:
                print("[等待登录] 请在弹出的 Chrome 窗口中扫码登录 1688...", flush=True)
                for _ in range(150):
                    time.sleep(2)
                    try:
                        cur_url = page.url
                    except Exception:
                        break
                    if cur_url and "login.1688.com" not in cur_url and "login.taobao.com" not in cur_url and cur_url != "about:blank":
                        page.wait_for_timeout(1500)
                        print("[登录成功] 1688 登录态已保存", flush=True)
                        break
            context.close()
            print(f"登录态已保存到: {PROFILE_DIR}", flush=True)
            return 0

        if args.tasks:
            tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
        elif args.input:
            urls = load_urls(Path(args.input))
            tasks = [{"offer_url": u, "supplier": "", "factory_url": ""} for u in urls]
        else:
            tasks = []

        if not tasks:
            print("未找到有效的 1688 商品任务。")
            context.close()
            return 1

        print(f"共读取到 {len(tasks)} 个任务 (模式={args.mode})，开始处理...")
        for idx, t in enumerate(tasks, start=1):
            print(f"\n--- [{idx}/{len(tasks)}] ---")
            try:
                process_offer(
                    page,
                    conn,
                    t["offer_url"],
                    mode=args.mode,
                    supplier=t.get("supplier", ""),
                    factory_url=t.get("factory_url", ""),
                    overwrite=args.overwrite,
                )
            except Exception as exc:
                print(f"  [异常] 处理失败: {exc}")

            if idx < len(tasks):
                wait_s = random.randint(args.min_delay, max(args.min_delay, args.max_delay))
                print(f"  [冷却] 随机等待 {wait_s} 秒...")
                time.sleep(wait_s)

        context.close()

    print(f"\n全部完成！结果已保存到数据库: {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
