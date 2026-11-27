#!/usr/bin/env python3
"""Lightweight local Web UI for viewing raw Amazon products (with local images & A+ links) and 1688 contacts."""

from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from openpyxl import Workbook

import step1_import_excel
from runtime_config import (
    BASE_DIR,
    DATA_DIR,
    PROFILE_DIR,
    build_skill_env,
    build_utf8_env,
    find_system_chrome,
    get_effective_ak,
    get_python_bin,
    is_browser_profile_locked,
    load_config,
    save_config,
    terminate_process_tree,
)

DB_PATH = DATA_DIR / "state.db"
IMAGE_DIR = BASE_DIR / "image"
HTML_PATH = BASE_DIR / "index.html"
STEP2_SCRIPT = BASE_DIR / "step2_find_matches.py"
STEP3_SCRIPT = BASE_DIR / "step3_get_contacts.py"
STEP4_SCRIPT = BASE_DIR / "step4_wangwang_chat.py"
AK_SKILL_CLI = BASE_DIR / "skills" / "1688-product-find" / "cli.py"
SERVER_META_PATH = DATA_DIR / "server.json"

ACTIVE_SERVER: ThreadingHTTPServer | None = None

MATCH_LOCK = threading.Lock()
MATCH_STATE = {
    "running": False,
    "message": "",
    "need_ak": False,
    "total": 0,
    "done": 0,
    "match_total": 0,
    "match_done": 0,
    "match_note": "",
    "detail_total": 0,
    "detail_done": 0,
    "detail_note": "",
}
MATCH_PROC: subprocess.Popen | None = None
MATCH_MANUAL_STOP = False
IMPORT_LOCK = threading.Lock()
IMPORT_CANCEL_EVENT = threading.Event()
IMPORT_STATE = {
    "running": False,
    "message": "",
    "dl_done": 0,
    "dl_total": 0,
    "dl_note": "",
    "tr_done": 0,
    "tr_total": 0,
    "tr_note": "",
}
CONTACT_LOCK = threading.Lock()
CONTACT_STATE = {"running": False, "message": "", "total": 0, "done": 0}
CONTACT_PROC: subprocess.Popen | None = None
CONTACT_MANUAL_STOP = False
CHAT_PROC: subprocess.Popen | None = None
CHAT_STARTED_AT = 0.0
CHAT_QUEUE_PATH = DATA_DIR / "chat_queue.jsonl"

AK_AUTH_LOCK = threading.Lock()
AK_AUTH_STATE = {"running": False, "message": "", "success": False}
AK_AUTH_PROC: subprocess.Popen | None = None

LOGIN_LOCK = threading.Lock()
LOGIN_STATE = {"running": False, "message": "", "logged_in": False}
LOGIN_PROC: subprocess.Popen | None = None


def _mask_ak(ak: str) -> str:
    ak = (ak or "").strip()
    if not ak:
        return ""
    if len(ak) >= 8:
        return f"{ak[:4]}****{ak[-4:]}"
    return "****"


def _terminate_proc(proc: subprocess.Popen | None) -> None:
    terminate_process_tree(proc)


def _stop_all_child_procs() -> None:
    """Terminate all running worker/browser child processes during server shutdown."""
    IMPORT_CANCEL_EVENT.set()
    with MATCH_LOCK:
        global MATCH_PROC, MATCH_MANUAL_STOP
        MATCH_MANUAL_STOP = True
        mp = MATCH_PROC
        MATCH_PROC = None
        MATCH_STATE["running"] = False
    _terminate_proc(mp)

    with CONTACT_LOCK:
        global CONTACT_PROC, CHAT_PROC, CONTACT_MANUAL_STOP
        CONTACT_MANUAL_STOP = True
        cp, chp = CONTACT_PROC, CHAT_PROC
        CONTACT_PROC = None
        CHAT_PROC = None
        CONTACT_STATE["running"] = False
    _terminate_proc(cp)
    _terminate_proc(chp)

    with AK_AUTH_LOCK:
        global AK_AUTH_PROC
        ap = AK_AUTH_PROC
        AK_AUTH_PROC = None
        AK_AUTH_STATE["running"] = False
    _terminate_proc(ap)

    with LOGIN_LOCK:
        global LOGIN_PROC
        lp = LOGIN_PROC
        LOGIN_PROC = None
        LOGIN_STATE["running"] = False
    _terminate_proc(lp)


def _is_chat_browser_alive_locked() -> bool:
    """Return True if WangWang chat Chrome is genuinely running; clean up stale Playwright wrappers."""
    global CHAT_PROC
    if CHAT_PROC is None:
        return False
    if CHAT_PROC.poll() is not None:
        CHAT_PROC = None
        return False
    lock_exists = is_browser_profile_locked()
    if not lock_exists and (time.monotonic() - CHAT_STARTED_AT) > 3.0:
        stale_proc = CHAT_PROC
        CHAT_PROC = None
        try:
            stale_proc.terminate()
            stale_proc.wait(timeout=1.5)
        except Exception:
            try:
                stale_proc.kill()
                stale_proc.wait(timeout=1.0)
            except Exception:
                pass
        if not CONTACT_STATE["running"]:
            CONTACT_STATE["message"] = "旺旺聊天窗口已关闭"
        return False
    return True


def _migrate_id_first(
    conn: sqlite3.Connection,
    table: str,
    create_sql: str,
    ordered_cols: list[str],
) -> None:
    """Rebuild table if `id` is missing, not primary key, or not the 1st column (preserving all data)."""
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not info:
        return
    col_names = [r[1] for r in info]
    pk_cols = [r[1] for r in info if r[5] > 0]
    if col_names[0] == "id" and pk_cols == ["id"]:
        return

    tmp_table = f"{table}__migr_new"
    conn.execute(f"DROP TABLE IF EXISTS {tmp_table}")
    conn.execute(create_sql.replace(f"CREATE TABLE IF NOT EXISTS {table}", f"CREATE TABLE {tmp_table}", 1))

    existing_set = set(col_names)
    has_old_id = "id" in existing_set
    copy_cols = [c for c in ordered_cols if c != "id" and c in existing_set]
    if has_old_id:
        select_exprs = ["COALESCE(id, rowid) AS id", *copy_cols]
        insert_cols = ["id", *copy_cols]
        order_expr = "COALESCE(id, rowid) ASC"
    else:
        select_exprs = copy_cols
        insert_cols = copy_cols
        order_expr = "rowid ASC"

    conn.execute(
        f"INSERT INTO {tmp_table} ({', '.join(insert_cols)}) SELECT {', '.join(select_exprs)} FROM {table} ORDER BY {order_expr}"
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp_table} RENAME TO {table}")


def ensure_schema() -> None:
    """Ensure SQLite tables exist with `id` as first primary-key column (Java convention)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    raw_products_ddl = """
        CREATE TABLE IF NOT EXISTS raw_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT NOT NULL UNIQUE,
            amazon_url TEXT,
            image_url TEXT,
            local_image_path TEXT DEFAULT '',
            title TEXT,
            brand TEXT,
            clean_keyword TEXT,
            keyword_zh TEXT DEFAULT '',
            price_usd TEXT,
            dimensions TEXT,
            category_large TEXT DEFAULT '',
            rank_large TEXT DEFAULT '',
            category_small TEXT,
            rank_small TEXT,
            rating TEXT,
            review_count TEXT,
            match_status TEXT DEFAULT '待找同款',
            updated_at TEXT
        )
    """
    product_images_ddl = """
        CREATE TABLE IF NOT EXISTS product_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT NOT NULL,
            seq INTEGER NOT NULL,
            ocr_status TEXT,
            ocr_text TEXT,
            image_url TEXT NOT NULL,
            UNIQUE(asin, seq)
        )
    """
    matched_offers_ddl = """
        CREATE TABLE IF NOT EXISTS matched_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT NOT NULL,
            product_id TEXT NOT NULL,
            title TEXT,
            detail_url TEXT,
            image_url TEXT,
            price REAL,
            sku_title TEXT,
            quantity_begin INTEGER,
            sold_count INTEGER,
            yx_index REAL,
            supplier TEXT,
            shop_origin TEXT DEFAULT '',
            factory_url TEXT DEFAULT '',
            detail_checked INTEGER DEFAULT 0,
            similarity_score REAL,
            search_method TEXT,
            updated_at TEXT,
            UNIQUE(asin, product_id)
        )
    """
    suppliers_ddl = """
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
    contacts_ddl = """
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
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(raw_products_ddl)
        conn.execute(product_images_ddl)
        conn.execute(matched_offers_ddl)
        conn.execute(suppliers_ddl)
        conn.execute(contacts_ddl)

        p_cols = {row[1] for row in conn.execute("PRAGMA table_info(raw_products)").fetchall()}
        if "keyword_zh" not in p_cols:
            conn.execute("ALTER TABLE raw_products ADD COLUMN keyword_zh TEXT DEFAULT ''")
        if "category_large" not in p_cols:
            conn.execute("ALTER TABLE raw_products ADD COLUMN category_large TEXT DEFAULT ''")
        if "rank_large" not in p_cols:
            conn.execute("ALTER TABLE raw_products ADD COLUMN rank_large TEXT DEFAULT ''")

        cols = {row[1] for row in conn.execute("PRAGMA table_info(contacts)").fetchall()}
        if "follow_status" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN follow_status TEXT DEFAULT '待跟进'")
        if "follow_note" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN follow_note TEXT DEFAULT ''")
        if "virtual_phone" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN virtual_phone TEXT DEFAULT ''")
        if "virtual_expires_at" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN virtual_expires_at TEXT DEFAULT ''")
        if "real_phone" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN real_phone TEXT DEFAULT ''")

        mo_cols = {row[1] for row in conn.execute("PRAGMA table_info(matched_offers)").fetchall()}
        if "shop_origin" not in mo_cols:
            conn.execute("ALTER TABLE matched_offers ADD COLUMN shop_origin TEXT DEFAULT ''")
        if "factory_url" not in mo_cols:
            conn.execute("ALTER TABLE matched_offers ADD COLUMN factory_url TEXT DEFAULT ''")
        if "detail_checked" not in mo_cols:
            conn.execute("ALTER TABLE matched_offers ADD COLUMN detail_checked INTEGER DEFAULT 0")

        _migrate_id_first(
            conn,
            "raw_products",
            raw_products_ddl,
            [
                "id", "asin", "amazon_url", "image_url", "local_image_path", "title", "brand",
                "clean_keyword", "keyword_zh", "price_usd", "dimensions", "category_large",
                "rank_large", "category_small", "rank_small", "rating", "review_count",
                "match_status", "updated_at",
            ],
        )
        _migrate_id_first(
            conn,
            "product_images",
            product_images_ddl,
            ["id", "asin", "seq", "ocr_status", "ocr_text", "image_url"],
        )
        _migrate_id_first(
            conn,
            "suppliers",
            suppliers_ddl,
            ["id", "supplier", "company_url", "location", "factory_level", "monthly_orders", "rec_tags", "updated_at"],
        )
        _migrate_id_first(
            conn,
            "contacts",
            contacts_ddl,
            [
                "id", "offer_id", "offer_url", "product_title", "shop_name", "shop_origin",
                "factory_url", "contact_person", "phone", "virtual_phone", "virtual_expires_at",
                "real_phone", "phone_source", "status", "note", "follow_status", "follow_note",
                "updated_at",
            ],
        )
        conn.commit()


def fetch_products_with_images() -> list[dict]:
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        products = [
            dict(r)
            for r in conn.execute("SELECT *, COALESCE(id, rowid) AS id FROM raw_products ORDER BY COALESCE(id, rowid) ASC").fetchall()
        ]
        img_rows = conn.execute(
            "SELECT asin, seq, ocr_status, ocr_text, image_url FROM product_images ORDER BY asin, seq"
        ).fetchall()
        img_map: dict[str, list[dict]] = {}
        for r in img_rows:
            img_map.setdefault(r["asin"], []).append(dict(r))

        match_rows = conn.execute(
            """
            SELECT m.*,
                   CASE
                     WHEN COALESCE(m.detail_checked, 0) = 1 THEN COALESCE(NULLIF(c.factory_url, ''), NULLIF(m.factory_url, ''), '')
                     ELSE COALESCE(NULLIF(s.company_url, ''), NULLIF(c.factory_url, ''), '')
                   END AS supplier_factory_url,
                   s.location AS supplier_location,
                   s.factory_level AS supplier_level,
                   s.monthly_orders AS supplier_monthly_orders,
                   COALESCE(NULLIF(c.shop_origin, ''), NULLIF(m.shop_origin, ''), '') AS shop_origin,
                   CASE
                     WHEN COALESCE(m.detail_checked, 0) = 1 THEN COALESCE(NULLIF(c.factory_url, ''), NULLIF(m.factory_url, ''), '')
                     ELSE COALESCE(NULLIF(c.factory_url, ''), NULLIF(m.factory_url, ''), '')
                   END AS factory_url,
                   c.contact_person,
                   c.phone,
                   c.virtual_phone,
                   c.virtual_expires_at,
                   c.real_phone,
                   c.phone_source,
                   c.status AS contact_status,
                   c.note AS contact_note,
                   c.follow_status,
                   c.follow_note
            FROM matched_offers m
            LEFT JOIN suppliers s ON m.supplier = s.supplier
            LEFT JOIN contacts c ON m.product_id = c.offer_id
            ORDER BY m.asin, m.id ASC
            """
        ).fetchall()
        match_map: dict[str, list[dict]] = {}
        for r in match_rows:
            match_map.setdefault(r["asin"], []).append(dict(r))

        for p in products:
            p["extra_images"] = img_map.get(p["asin"], [])
            p["matched_offers"] = match_map.get(p["asin"], [])
        return products


def fetch_matches() -> list[dict]:
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT m.*,
                   COALESCE(r.id, r.rowid) AS amazon_id,
                   r.keyword_zh,
                   r.local_image_path AS amazon_local_image,
                   r.image_url AS amazon_image_url,
                   r.title AS amazon_title,
                   r.category_small AS amazon_category_small,
                   r.clean_keyword AS amazon_clean_keyword,
                   CASE
                     WHEN COALESCE(m.detail_checked, 0) = 1 THEN COALESCE(NULLIF(c.factory_url, ''), NULLIF(m.factory_url, ''), '')
                     ELSE COALESCE(NULLIF(s.company_url, ''), NULLIF(c.factory_url, ''), '')
                   END AS supplier_factory_url,
                   s.location AS supplier_location,
                   s.factory_level AS supplier_level,
                   s.monthly_orders AS supplier_monthly_orders,
                   COALESCE(NULLIF(c.shop_origin, ''), NULLIF(m.shop_origin, ''), '') AS shop_origin,
                   CASE
                     WHEN COALESCE(m.detail_checked, 0) = 1 THEN COALESCE(NULLIF(c.factory_url, ''), NULLIF(m.factory_url, ''), '')
                     ELSE COALESCE(NULLIF(c.factory_url, ''), NULLIF(m.factory_url, ''), '')
                   END AS factory_url,
                   c.contact_person,
                   c.phone,
                   c.virtual_phone,
                   c.virtual_expires_at,
                   c.real_phone,
                   c.phone_source,
                   c.status AS contact_status,
                   c.note AS contact_note,
                   c.follow_status,
                   c.follow_note
            FROM matched_offers m
            LEFT JOIN raw_products r ON m.asin = r.asin
            LEFT JOIN suppliers s ON m.supplier = s.supplier
            LEFT JOIN contacts c ON m.product_id = c.offer_id
            ORDER BY COALESCE(r.id, r.rowid, 999999999) ASC, m.id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def fetch_table(table: str) -> list[dict]:
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid ASC").fetchall()
        return [dict(r) for r in rows]


def export_excel() -> bytes:
    rows = fetch_table("contacts")
    wb = Workbook()
    ws = wb.active
    ws.title = "1688卖家联系方式"
    headers = [
        "商品ID", "商品链接", "商品标题", "店铺名称", "店铺主页",
        "工厂档案链接", "联系人", "虚拟号", "虚拟号截止时间", "实际电话", "号码来源", "抓取状态",
        "备注", "跟进状态", "人工备注", "更新时间",
    ]
    ws.append(headers)
    for r in rows:
        ws.append([
            r.get("offer_id", ""),
            r.get("offer_url", ""),
            r.get("product_title", ""),
            r.get("shop_name", ""),
            r.get("shop_origin", ""),
            r.get("factory_url", ""),
            r.get("contact_person", ""),
            r.get("virtual_phone", ""),
            r.get("virtual_expires_at", ""),
            r.get("real_phone", ""),
            r.get("phone_source", ""),
            r.get("status", ""),
            r.get("note", ""),
            r.get("follow_status") or "待跟进",
            r.get("follow_note") or "",
            r.get("updated_at", ""),
        ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            html = HTML_PATH.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path.startswith("/image/"):
            rel_name = unquote(self.path[len("/image/"):]).split("?")[0]
            target = (IMAGE_DIR / rel_name).resolve()
            if not str(target).startswith(str(IMAGE_DIR.resolve())) or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            mime, _ = mimetypes.guess_type(str(target))
            data = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime or "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == "/api/products":
            self._send_json({"rows": fetch_products_with_images()})
            return

        if self.path == "/api/matches":
            self._send_json({"rows": fetch_matches()})
            return

        if self.path == "/api/match-status":
            with MATCH_LOCK:
                self._send_json(dict(MATCH_STATE))
            return

        if self.path == "/api/import-status":
            with IMPORT_LOCK:
                self._send_json(dict(IMPORT_STATE))
            return

        if self.path == "/api/contact-status":
            with CONTACT_LOCK:
                self._send_json(dict(CONTACT_STATE))
            return

        if self.path == "/api/config":
            cfg = load_config()
            ak = get_effective_ak()
            with AK_AUTH_LOCK:
                ak_auth = dict(AK_AUTH_STATE)
            with LOGIN_LOCK:
                login_state = dict(LOGIN_STATE)
            self._send_json({
                "has_ak": bool(ak),
                "ak_masked": _mask_ak(ak),
                "wangwang_greeting": cfg.get("wangwang_greeting", ""),
                "chrome_found": bool(find_system_chrome()),
                "ak_auth": ak_auth,
                "login_state": login_state,
            })
            return

        if self.path == "/api/contacts":
            self._send_json({"rows": fetch_table("contacts")})
            return

        if self.path == "/api/export":
            content = export_excel()
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            self.send_header("Content-Disposition", 'attachment; filename="3_seller_contacts.xlsx"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        global CHAT_PROC, CHAT_STARTED_AT, CONTACT_MANUAL_STOP, MATCH_MANUAL_STOP
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)

        if self.path == "/api/products/import":
            if not raw_body:
                self._send_json({"error": "未收到导入文件内容"}, status=400)
                return
            orig_name = unquote(self.headers.get("X-Filename") or "import.xlsx")
            suffix = Path(orig_name).suffix.lower()
            if suffix not in (".xlsx", ".xlsm", ".csv"):
                self._send_json({"error": f"不支持的文件格式: {suffix or '(空)'}（请上传 .xlsx / .csv）"}, status=400)
                return
            with IMPORT_LOCK:
                if IMPORT_STATE["running"]:
                    self._send_json({"error": "当前已有导入任务正在进行中，请稍候"}, status=409)
                    return
                IMPORT_CANCEL_EVENT.clear()
                IMPORT_STATE.update({
                    "running": True,
                    "message": "正在解析表格...",
                    "dl_done": 0,
                    "dl_total": 0,
                    "dl_note": "正在解析表格...",
                    "tr_done": 0,
                    "tr_total": 0,
                    "tr_note": "等待主图下载完成后开始",
                })

            upload_dir = BASE_DIR / "data" / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            upload_path = upload_dir / f"upload_{stamp}{suffix}"
            upload_path.write_bytes(raw_body)

            def import_worker() -> None:
                def on_progress(info: dict) -> None:
                    with IMPORT_LOCK:
                        IMPORT_STATE["dl_done"] = int(info.get("dl_done") or 0)
                        IMPORT_STATE["dl_total"] = int(info.get("dl_total") or 0)
                        IMPORT_STATE["dl_note"] = info.get("dl_note", "")
                        IMPORT_STATE["tr_done"] = int(info.get("tr_done") or 0)
                        IMPORT_STATE["tr_total"] = int(info.get("tr_total") or 0)
                        IMPORT_STATE["tr_note"] = info.get("tr_note", "")

                try:
                    summary = step1_import_excel.import_file(
                        upload_path,
                        download_images=True,
                        progress_cb=on_progress,
                        cancel_event=IMPORT_CANCEL_EVENT,
                    )
                    with IMPORT_LOCK:
                        if summary.get("cancelled"):
                            IMPORT_STATE["message"] = "已中断导入任务"
                        else:
                            IMPORT_STATE["dl_done"] = IMPORT_STATE["dl_total"]
                            IMPORT_STATE["tr_done"] = IMPORT_STATE["tr_total"]
                            IMPORT_STATE["dl_note"] = "完成"
                            IMPORT_STATE["tr_note"] = "完成"
                            IMPORT_STATE["message"] = (
                                f"导入完成：商品 {summary.get('valid_count', 0)} 条，"
                                f"主图 {summary.get('downloaded_count', 0)} 张，"
                                f"附图链接 {summary.get('extra_img_count', 0)} 条，"
                                f"中文词 {summary.get('translated_count', 0)} 条"
                            )
                except Exception as exc:
                    with IMPORT_LOCK:
                        IMPORT_STATE["message"] = f"导入失败: {exc}"
                finally:
                    with IMPORT_LOCK:
                        IMPORT_STATE["running"] = False

            threading.Thread(target=import_worker, daemon=True).start()
            self._send_json({"ok": True, "filename": orig_name})
            return

        data = json.loads(raw_body.decode("utf-8"))

        if self.path == "/api/products/import/stop":
            IMPORT_CANCEL_EVENT.set()
            with IMPORT_LOCK:
                IMPORT_STATE["running"] = False
                IMPORT_STATE["message"] = "已中断导入任务"
            self._send_json({"ok": True})
            return

        if self.path == "/api/products/delete":
            asins = list(dict.fromkeys(str(a).strip().upper() for a in (data.get("asins") or []) if str(a).strip()))
            if not asins:
                self._send_json({"error": "请先勾选至少一个 ASIN"}, status=400)
                return
            deleted_images = 0
            img_root = IMAGE_DIR.resolve()
            with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
                for i in range(0, len(asins), 400):
                    chunk = asins[i : i + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    for (rel_path,) in conn.execute(
                        f"SELECT local_image_path FROM raw_products WHERE asin IN ({placeholders})",
                        chunk,
                    ).fetchall():
                        if rel_path:
                            target = (BASE_DIR / rel_path).resolve()
                            if target.parent == img_root and target.exists():
                                try:
                                    target.unlink()
                                    deleted_images += 1
                                except OSError:
                                    pass
                    conn.execute(f"DELETE FROM product_images WHERE asin IN ({placeholders})", chunk)
                    conn.execute(f"DELETE FROM raw_products WHERE asin IN ({placeholders})", chunk)
                conn.commit()
            self._send_json({"ok": True, "deleted": len(asins), "deleted_images": deleted_images})
            return

        if self.path == "/api/matches/delete":
            asins = list(dict.fromkeys(str(a).strip().upper() for a in (data.get("asins") or []) if str(a).strip()))
            if not asins:
                self._send_json({"error": "请先勾选至少一个 ASIN"}, status=400)
                return
            with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
                affected_pids: set[str] = set()
                affected_suppliers: set[str] = set()
                for i in range(0, len(asins), 400):
                    chunk = asins[i : i + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    for pid, sup in conn.execute(
                        f"SELECT product_id, supplier FROM matched_offers WHERE asin IN ({placeholders})",
                        chunk,
                    ).fetchall():
                        if pid:
                            affected_pids.add(str(pid))
                        if sup:
                            affected_suppliers.add(str(sup))
                    conn.execute(f"DELETE FROM matched_offers WHERE asin IN ({placeholders})", chunk)
                    conn.execute(
                        f"UPDATE raw_products SET match_status = '待找同款' WHERE asin IN ({placeholders})",
                        chunk,
                    )
                if affected_pids:
                    pid_list = list(affected_pids)
                    for i in range(0, len(pid_list), 400):
                        p_chunk = pid_list[i : i + 400]
                        p_ph = ",".join("?" for _ in p_chunk)
                        conn.execute(
                            f"""
                            DELETE FROM contacts
                            WHERE offer_id IN ({p_ph})
                              AND offer_id NOT IN (SELECT product_id FROM matched_offers)
                            """,
                            p_chunk,
                        )
                if affected_suppliers:
                    sup_list = list(affected_suppliers)
                    for i in range(0, len(sup_list), 400):
                        s_chunk = sup_list[i : i + 400]
                        s_ph = ",".join("?" for _ in s_chunk)
                        conn.execute(
                            f"""
                            DELETE FROM suppliers
                            WHERE supplier IN ({s_ph})
                              AND supplier NOT IN (SELECT DISTINCT supplier FROM matched_offers WHERE supplier IS NOT NULL)
                            """,
                            s_chunk,
                        )
                conn.commit()
            self._send_json({"ok": True, "deleted": len(asins)})
            return

        if self.path == "/api/contacts/update":
            offer_id = str(data.get("offer_id") or "").strip()
            if not offer_id:
                self._send_json({"error": "missing offer_id"}, status=400)
                return
            with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
                exists = conn.execute("SELECT 1 FROM contacts WHERE offer_id = ?", (offer_id,)).fetchone()
                if not exists:
                    offer_url = str(data.get("offer_url") or f"https://detail.1688.com/offer/{offer_id}.html")
                    conn.execute(
                        "INSERT INTO contacts (offer_id, offer_url, shop_name, product_title, status) VALUES (?, ?, ?, ?, ?)",
                        (
                            offer_id,
                            offer_url,
                            str(data.get("shop_name") or ""),
                            str(data.get("product_title") or ""),
                            "待获取电话",
                        ),
                    )
                updates, params = [], []
                for field in ("follow_status", "follow_note", "phone", "real_phone", "virtual_phone", "contact_person"):
                    if field in data:
                        updates.append(f"{field} = ?")
                        params.append(str(data[field]).strip())
                if "real_phone" in data:
                    rp = str(data["real_phone"]).strip()
                    if rp:
                        updates.append("phone = ?")
                        params.append(rp)
                        updates.append("phone_source = CASE WHEN phone_source = '' OR phone_source IS NULL THEN '人工补录实号' ELSE phone_source END")
                        updates.append("status = '已获取电话'")
                if updates:
                    params.append(offer_id)
                    conn.execute(
                        f"UPDATE contacts SET {', '.join(updates)} WHERE offer_id = ?",
                        params,
                    )
                conn.commit()
            self._send_json({"ok": True})
            return

        if self.path == "/api/config":
            updates: dict[str, str] = {}
            if "ak" in data:
                ak_val = str(data.get("ak") or "").strip()
                if ak_val and len(ak_val) < 32:
                    self._send_json({"error": "AK 格式无效（长度需不少于 32 位）"}, status=400)
                    return
                updates["ak"] = ak_val
            if "wangwang_greeting" in data:
                updates["wangwang_greeting"] = str(data.get("wangwang_greeting") or "").strip()
            cfg = save_config(updates)
            ak = get_effective_ak()
            self._send_json({
                "ok": True,
                "has_ak": bool(ak),
                "ak_masked": _mask_ak(ak),
                "wangwang_greeting": cfg.get("wangwang_greeting", ""),
            })
            return

        if self.path == "/api/ak/authorize":
            if not AK_SKILL_CLI.exists():
                self._send_json({"error": "未找到内置 AK 授权脚本"}, status=500)
                return
            with AK_AUTH_LOCK:
                if AK_AUTH_STATE["running"]:
                    self._send_json({"ok": True, "already_running": True})
                    return
                AK_AUTH_STATE.update({
                    "running": True,
                    "success": False,
                    "message": "正在打开浏览器前往 1688 AK 授权页，请登录并确认授权...",
                })

            def ak_worker() -> None:
                global AK_AUTH_PROC
                try:
                    proc = subprocess.Popen(
                        [get_python_bin(), "-u", str(AK_SKILL_CLI), "get_ak", "--timeout", "300"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        env=build_skill_env(""),
                    )
                    with AK_AUTH_LOCK:
                        AK_AUTH_PROC = proc
                    out_text, _ = proc.communicate(timeout=315)
                    ak_found = ""
                    for line in reversed((out_text or "").splitlines()):
                        line = line.strip()
                        if line.startswith("{") and line.endswith("}"):
                            try:
                                parsed = json.loads(line)
                                ak_found = str(((parsed.get("data") or {}).get("ak")) or "").strip()
                                if ak_found:
                                    break
                            except Exception:
                                pass
                    if not ak_found:
                        ak_store = DATA_DIR / "workspace" / ".1688-AK" / ".ak_store.json"
                        if ak_store.exists():
                            try:
                                ak_found = str(json.loads(ak_store.read_text(encoding="utf-8")).get("ak") or "").strip()
                            except Exception:
                                pass
                    with AK_AUTH_LOCK:
                        if ak_found:
                            save_config({"ak": ak_found})
                            AK_AUTH_STATE.update({
                                "running": False,
                                "success": True,
                                "message": f"AK 授权成功并已保存（{_mask_ak(ak_found)}）",
                            })
                        else:
                            AK_AUTH_STATE.update({
                                "running": False,
                                "success": False,
                                "message": "未收到 1688 AK 授权结果，请确认已在浏览器完成授权或改用手动填入",
                            })
                except Exception as exc:
                    with AK_AUTH_LOCK:
                        AK_AUTH_STATE.update({
                            "running": False,
                            "success": False,
                            "message": f"AK 授权异常: {exc}",
                        })
                finally:
                    with AK_AUTH_LOCK:
                        AK_AUTH_PROC = None

            threading.Thread(target=ak_worker, daemon=True).start()
            self._send_json({"ok": True})
            return

        if self.path == "/api/browser/login":
            with MATCH_LOCK:
                if MATCH_STATE["running"]:
                    self._send_json({"error": "当前正在找同款，请等待完成后再扫码登录"}, status=409)
                    return
            with CONTACT_LOCK:
                if CONTACT_STATE["running"] or _is_chat_browser_alive_locked():
                    self._send_json({"error": "当前 Chrome 正在运行电话抓取或旺旺聊天，请先关闭或停止后再登录"}, status=409)
                    return
            with LOGIN_LOCK:
                if LOGIN_STATE["running"]:
                    self._send_json({"ok": True, "already_running": True})
                    return
                LOGIN_STATE.update({
                    "running": True,
                    "logged_in": False,
                    "message": "正在打开 Chrome，请扫码登录 1688（完成后自动保存并关闭窗口）...",
                })

            def login_worker() -> None:
                global LOGIN_PROC
                try:
                    proc = subprocess.Popen(
                        [get_python_bin(), "-u", str(STEP3_SCRIPT), "--login"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        env=build_utf8_env(),
                    )
                    with LOGIN_LOCK:
                        LOGIN_PROC = proc
                    out_text, _ = proc.communicate(timeout=320)
                    ok = "[登录成功]" in (out_text or "")
                    with LOGIN_LOCK:
                        LOGIN_STATE.update({
                            "running": False,
                            "logged_in": ok,
                            "message": "1688 登录态已保存，后续操作可直接使用" if ok else "登录窗口已关闭",
                        })
                except Exception as exc:
                    with LOGIN_LOCK:
                        LOGIN_STATE.update({
                            "running": False,
                            "logged_in": False,
                            "message": f"扫码登录异常: {exc}",
                        })
                finally:
                    with LOGIN_LOCK:
                        LOGIN_PROC = None

            threading.Thread(target=login_worker, daemon=True).start()
            self._send_json({"ok": True})
            return

        if self.path == "/api/shutdown":
            self._send_json({"ok": True, "message": "服务正在安全关闭..."})

            def _delayed_shutdown() -> None:
                time.sleep(0.15)
                _stop_all_child_procs()
                if ACTIVE_SERVER is not None:
                    ACTIVE_SERVER.shutdown()

            threading.Thread(target=_delayed_shutdown, daemon=True).start()
            return

        if self.path == "/api/contacts/chat":
            offer_url = str(data.get("offer_url") or "").strip()
            default_greeting = load_config().get("wangwang_greeting") or "老板你好，想长期拿货，方便留个电话直接沟通吗"
            greeting = str(data.get("greeting") or default_greeting).strip()
            if not offer_url:
                self._send_json({"error": "missing offer_url"}, status=400)
                return

            with MATCH_LOCK:
                if MATCH_STATE["running"]:
                    self._send_json({"error": "当前正在找同款（Chrome 正被详情页链接识别占用），请等待完成后再开旺旺聊天"}, status=409)
                    return

            with CONTACT_LOCK:
                if CONTACT_PROC is not None and CONTACT_PROC.poll() is None:
                    self._send_json({"error": "当前正在抓取电话，请等待完成或停止后再开旺旺聊天"}, status=409)
                    return

                line = json.dumps({"offer_url": offer_url, "greeting": greeting}, ensure_ascii=False) + "\n"
                if _is_chat_browser_alive_locked():
                    with CHAT_QUEUE_PATH.open("a", encoding="utf-8") as f:
                        f.write(line)
                    CONTACT_STATE["message"] = "已向当前 Chrome 窗口追加新的旺旺聊天 Tab 并填入话术"
                    self._send_json({"ok": True, "queued": True})
                    return

                CHAT_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
                CHAT_QUEUE_PATH.write_text(line, encoding="utf-8")
                python_bin = get_python_bin()
                chat_log_path = BASE_DIR / "data" / "step4_chat.log"
                with chat_log_path.open("a", encoding="utf-8") as log_f:
                    proc = subprocess.Popen(
                        [python_bin, "-u", str(STEP4_SCRIPT), "--queue", str(CHAT_QUEUE_PATH)],
                        stdin=subprocess.DEVNULL,
                        stdout=log_f,
                        stderr=log_f,
                    )
                CHAT_PROC = proc
                CHAT_STARTED_AT = time.monotonic()
                CONTACT_STATE["message"] = "旺旺聊天窗口已打开（可继续点击其他供应商在同窗口开新 Tab）"

                def wait_chat() -> None:
                    global CHAT_PROC
                    proc.wait()
                    with CONTACT_LOCK:
                        if CHAT_PROC is proc:
                            CHAT_PROC = None
                            if not CONTACT_STATE["running"]:
                                CONTACT_STATE["message"] = "旺旺聊天窗口已关闭"

                threading.Thread(target=wait_chat, daemon=True).start()
            self._send_json({"ok": True, "queued": False})
            return

        if self.path == "/api/contacts/fetch":
            items = data.get("items") or []
            if not items and data.get("urls"):
                items = [{"offer_url": str(u).strip(), "supplier": "", "factory_url": ""} for u in data["urls"] if str(u).strip()]
            mode = data.get("mode") if data.get("mode") in ("factory", "company", "auto") else "factory"
            if not items:
                self._send_json({"error": "没有可抓取的 1688 链接"}, status=400)
                return

            mode_label = "工厂档案直达" if mode == "factory" else "公司页面实号"
            with MATCH_LOCK:
                if MATCH_STATE["running"]:
                    self._send_json({"error": "当前正在找同款（Chrome 正被详情页链接识别占用），请等待完成后再抓取电话"}, status=409)
                    return

            with CONTACT_LOCK:
                if CONTACT_STATE["running"] or _is_chat_browser_alive_locked():
                    self._send_json({"error": "当前浏览器正被抓取或旺旺聊天占用，请先关闭浏览器或点击停止"}, status=409)
                    return
                CONTACT_MANUAL_STOP = False
                CONTACT_STATE.update({
                    "running": True,
                    "message": f"正在通过【{mode_label}】抓取 {len(items)} 个店铺联系方式...",
                    "total": len(items),
                    "done": 0,
                })

            def contact_worker() -> None:
                global CONTACT_PROC
                python_bin = get_python_bin()
                temp_tasks_path = BASE_DIR / "data" / "web_fetch_tasks.json"
                try:
                    temp_tasks_path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
                    with CONTACT_LOCK:
                        CONTACT_STATE["message"] = f"正在通过【{mode_label}】抓取 {len(items)} 个店铺电话..."
                    proc = subprocess.Popen(
                        [
                            python_bin,
                            "-u",
                            str(STEP3_SCRIPT),
                            "--tasks",
                            str(temp_tasks_path),
                            "--mode",
                            mode,
                            "--overwrite",
                            "--min-delay",
                            "3",
                            "--max-delay",
                            "6",
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        env=build_utf8_env(),
                    )
                    with CONTACT_LOCK:
                        CONTACT_PROC = proc
                    if proc.stdout is not None:
                        for raw_line in proc.stdout:
                            line = raw_line.strip()
                            if not line:
                                continue
                            m_idx = re.match(r"^---\s*\[(\d+)/(\d+)\]\s*---$", line)
                            if m_idx:
                                with CONTACT_LOCK:
                                    CONTACT_STATE["done"] = int(m_idx.group(1))
                                    CONTACT_STATE["message"] = f"正在通过【{mode_label}】抓取 [{m_idx.group(1)}/{m_idx.group(2)}]..."
                            elif "[成功]" in line or "[跳过]" in line or "[非工厂店]" in line or "[等待人工]" in line:
                                with CONTACT_LOCK:
                                    CONTACT_STATE["message"] = f"【{mode_label}】{line}"
                    proc.wait(timeout=600)
                    with CONTACT_LOCK:
                        if CONTACT_MANUAL_STOP or proc.returncode in (-15, -9, 143, 137):
                            CONTACT_STATE["message"] = "已手动停止抓取"
                        elif proc.returncode == 0:
                            CONTACT_STATE["message"] = f"完成！已抓取 {len(items)} 个店铺联系方式（{mode_label}）"
                        else:
                            CONTACT_STATE["message"] = f"抓取结束（退出码 {proc.returncode}）"
                except Exception as exc:
                    with CONTACT_LOCK:
                        CONTACT_STATE["message"] = f"抓取异常: {exc}"
                finally:
                    with CONTACT_LOCK:
                        CONTACT_PROC = None
                        CONTACT_STATE["running"] = False

            threading.Thread(target=contact_worker, daemon=True).start()
            self._send_json({"ok": True})
            return

        if self.path == "/api/contacts/stop":
            procs_to_wait = []
            with CONTACT_LOCK:
                CONTACT_MANUAL_STOP = True
                if CONTACT_PROC is not None and CONTACT_PROC.poll() is None:
                    procs_to_wait.append(CONTACT_PROC)
                if CHAT_PROC is not None and CHAT_PROC.poll() is None:
                    procs_to_wait.append(CHAT_PROC)
                CONTACT_STATE["running"] = False
                CONTACT_STATE["message"] = "已停止当前浏览器任务"
            for p in procs_to_wait:
                terminate_process_tree(p)
            self._send_json({"ok": True})
            return

        if self.path == "/api/products/update":
            asin = data.get("asin")
            if not asin:
                self._send_json({"error": "missing asin"}, status=400)
                return
            updates, params = [], []
            for field in ("clean_keyword", "keyword_zh", "match_status"):
                if field in data:
                    updates.append(f"{field} = ?")
                    params.append(data[field])
            if updates:
                params.append(asin)
                with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
                    conn.execute(
                        f"UPDATE raw_products SET {', '.join(updates)} WHERE asin = ?",
                        params,
                    )
                    conn.commit()
            self._send_json({"ok": True})
            return

        if self.path == "/api/products/match":
            asins = [str(a).strip().upper() for a in (data.get("asins") or []) if str(a).strip()]
            limit = max(1, min(int(data.get("limit") or 10), 30))
            mode = data.get("mode") if data.get("mode") in ("auto", "image", "text") else "auto"
            custom_query = str(data.get("custom_query") or "").strip()
            save_query = bool(data.get("save_query"))
            selected_images = [str(u).strip() for u in (data.get("images") or []) if str(u).strip()]
            if not asins:
                self._send_json({"error": "请先勾选至少一个 ASIN"}, status=400)
                return

            with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
                placeholders = ",".join("?" for _ in asins)
                ordered_asins = [
                    r[0]
                    for r in conn.execute(
                        f"SELECT asin FROM raw_products WHERE asin IN ({placeholders}) ORDER BY COALESCE(id, rowid) ASC",
                        asins,
                    ).fetchall()
                ]
                if ordered_asins:
                    asins = ordered_asins
                if custom_query and save_query:
                    conn.execute(
                        f"UPDATE raw_products SET keyword_zh = ? WHERE asin IN ({placeholders})",
                        [custom_query, *asins],
                    )
                    conn.commit()

            with CONTACT_LOCK:
                if CONTACT_STATE["running"] or _is_chat_browser_alive_locked():
                    self._send_json({"error": "当前 Chrome 正被电话抓取或旺旺聊天占用，请先等待完成或关闭/停止后再找同款"}, status=409)
                    return

            if not get_effective_ak():
                self._send_json(
                    {"error": "尚未配置 1688 AK，请先点击「一键浏览器获取 AK」或手动填入 AK", "need_ak": True},
                    status=400,
                )
                return

            with MATCH_LOCK:
                if MATCH_STATE["running"]:
                    self._send_json({"error": "已有找同款任务正在运行中，请稍候"}, status=409)
                    return
                MATCH_MANUAL_STOP = False
                total_slots = len(asins) * limit
                MATCH_STATE.update({
                    "running": True,
                    "need_ak": False,
                    "message": f"正在为 {len(asins)} 个 ASIN 匹配同款（每款最多 {limit} 家店）...",
                    "total": total_slots,
                    "done": 0,
                    "match_total": total_slots,
                    "match_done": 0,
                    "match_note": "准备中...",
                    "detail_total": 0,
                    "detail_done": 0,
                    "detail_note": "等待查询同款完成后开始",
                })

            def worker() -> None:
                global MATCH_PROC
                python_bin = get_python_bin()
                try:
                    cmd = [
                        python_bin,
                        "-u",
                        str(STEP2_SCRIPT),
                        "--asins",
                        ",".join(asins),
                        "--limit",
                        str(limit),
                        "--mode",
                        mode,
                        "--delay",
                        "0",
                    ]
                    if custom_query:
                        cmd.extend(["--query", custom_query])
                    if selected_images and len(asins) == 1:
                        imgs_file = BASE_DIR / "data" / "match_selected_images.json"
                        imgs_file.write_text(json.dumps(selected_images, ensure_ascii=False), encoding="utf-8")
                        cmd.extend(["--images-file", str(imgs_file)])
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        env=build_utf8_env(),
                    )
                    with MATCH_LOCK:
                        MATCH_PROC = proc
                    if proc.stdout is not None:
                        for raw_line in proc.stdout:
                            line = raw_line.strip()
                            if not line:
                                continue
                            m_asin = re.match(r"^---\s+\[(\d+)/(\d+)\]\s+(\S+)", line)
                            if m_asin:
                                with MATCH_LOCK:
                                    MATCH_STATE["match_note"] = f"正在搜索 {m_asin.group(3)}..."
                                    MATCH_STATE["message"] = f"查询同款 {MATCH_STATE['match_done']}/{MATCH_STATE['match_total']}"
                                continue

                            m_prog = re.match(r"^\[同款进度\]\s+done=(\d+)\s+total=(\d+)\s+label=(.*)$", line)
                            if m_prog:
                                done_n = int(m_prog.group(1))
                                total_n = int(m_prog.group(2))
                                note_s = m_prog.group(3).strip()
                                with MATCH_LOCK:
                                    MATCH_STATE["match_done"] = done_n
                                    MATCH_STATE["match_total"] = total_n
                                    MATCH_STATE["done"] = done_n
                                    MATCH_STATE["total"] = total_n
                                    MATCH_STATE["match_note"] = note_s
                                    MATCH_STATE["message"] = f"查询同款 {done_n}/{total_n}"
                                continue

                            m_done = re.match(r"^\[同款完成\]\s+done=(\d+)\s+total=(\d+)\s+asin=(\S+)\s+count=(\d+)", line)
                            if m_done:
                                done_n = int(m_done.group(1))
                                total_n = int(m_done.group(2))
                                with MATCH_LOCK:
                                    MATCH_STATE["match_done"] = done_n
                                    MATCH_STATE["match_total"] = total_n
                                    MATCH_STATE["done"] = done_n
                                    MATCH_STATE["total"] = total_n
                                    MATCH_STATE["match_note"] = f"{m_done.group(3)} 已匹配 {m_done.group(4)} 家"
                                    MATCH_STATE["message"] = f"查询同款 {done_n}/{total_n}"
                                continue

                            m_dstart = re.match(r"^\[详情开始\]\s+total=(\d+)\s+done=(\d+)", line)
                            if m_dstart:
                                d_total = int(m_dstart.group(1))
                                d_done = int(m_dstart.group(2))
                                with MATCH_LOCK:
                                    MATCH_STATE["match_done"] = MATCH_STATE["match_total"]
                                    MATCH_STATE["detail_total"] = d_total
                                    MATCH_STATE["detail_done"] = d_done
                                    MATCH_STATE["detail_note"] = "全部命中缓存" if d_done >= d_total else "正在启动浏览器识别详情页..."
                                    MATCH_STATE["message"] = f"查询详情 {d_done}/{d_total}"
                                continue

                            if "[AK错误]" in line:
                                with MATCH_LOCK:
                                    MATCH_STATE["need_ak"] = True
                                    MATCH_STATE["match_note"] = "1688 AK 未配置或已失效，请重新授权"
                                    MATCH_STATE["message"] = "1688 AK 未配置或已失效，请重新授权"
                                continue

                            if "[等待人工]" in line:
                                with MATCH_LOCK:
                                    MATCH_STATE["detail_note"] = line
                                    MATCH_STATE["message"] = line
                                continue

                            m_dprog = re.match(r"^\[详情进度\]\s+\[(\d+)/(\d+)\]\s*(.*)$", line)
                            if m_dprog:
                                d_done = int(m_dprog.group(1))
                                d_total = int(m_dprog.group(2))
                                d_note = m_dprog.group(3).strip()
                                with MATCH_LOCK:
                                    MATCH_STATE["detail_done"] = d_done
                                    MATCH_STATE["detail_total"] = d_total
                                    MATCH_STATE["detail_note"] = d_note
                                    MATCH_STATE["message"] = f"查询详情 {d_done}/{d_total}"
                    proc.wait()
                    with MATCH_LOCK:
                        if MATCH_MANUAL_STOP or proc.returncode in (-15, -9, 143, 137):
                            MATCH_STATE["message"] = "已手动中断查询同款任务"
                        elif MATCH_STATE["need_ak"]:
                            MATCH_STATE["message"] = "1688 AK 未配置或已失效，请点击顶部「1688 AK 配置」完成授权"
                        elif proc.returncode == 0:
                            MATCH_STATE["match_done"] = MATCH_STATE["match_total"]
                            MATCH_STATE["done"] = MATCH_STATE["total"]
                            if MATCH_STATE["detail_total"] > 0:
                                MATCH_STATE["detail_done"] = MATCH_STATE["detail_total"]
                                MATCH_STATE["detail_note"] = "完成"
                            MATCH_STATE["match_note"] = "完成"
                            MATCH_STATE["message"] = f"完成！已更新 {len(asins)} 个 ASIN 的同款与店铺/工厂链接"
                        else:
                            MATCH_STATE["message"] = f"执行结束（返回码 {proc.returncode}），请查看已完成数据"
                except Exception as exc:
                    with MATCH_LOCK:
                        MATCH_STATE["message"] = f"执行异常: {exc}"
                finally:
                    with MATCH_LOCK:
                        MATCH_PROC = None
                        MATCH_STATE["running"] = False

            threading.Thread(target=worker, daemon=True).start()
            self._send_json({"ok": True})
            return

        if self.path == "/api/products/match/stop":
            proc_to_wait = None
            with MATCH_LOCK:
                MATCH_MANUAL_STOP = True
                if MATCH_PROC is not None and MATCH_PROC.poll() is None:
                    proc_to_wait = MATCH_PROC
                MATCH_STATE["running"] = False
                MATCH_STATE["message"] = "已手动中断查询同款任务"
            if proc_to_wait is not None:
                terminate_process_tree(proc_to_wait)
            self._send_json({"ok": True})
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        return


def _bind_server_with_fallback(host: str, base_port: int, max_tries: int = 10) -> tuple[ThreadingHTTPServer, int]:
    """Bind ThreadingHTTPServer to base_port or increment if already occupied."""
    last_err: OSError | None = None
    for offset in range(max_tries):
        candidate = base_port + offset
        try:
            return ThreadingHTTPServer((host, candidate), Handler), candidate
        except OSError as exc:
            last_err = exc
    raise last_err or OSError(f"无法绑定端口 {base_port}-{base_port + max_tries - 1}")


def main() -> int:
    global ACTIVE_SERVER
    import argparse

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    parser = argparse.ArgumentParser(description="1688 选品找货与卖家联系方式本地工作台")
    parser.add_argument("port", nargs="?", type=int, default=8000, help="默认监听端口 (8000)")
    parser.add_argument("--port", dest="opt_port", type=int, default=None, help="指定监听端口")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开默认浏览器")
    args = parser.parse_args()

    ensure_schema()
    load_config()

    target_port = args.opt_port if args.opt_port is not None else args.port
    server, actual_port = _bind_server_with_fallback("127.0.0.1", target_port)
    ACTIVE_SERVER = server
    url = f"http://127.0.0.1:{actual_port}"

    SERVER_META_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERVER_META_PATH.write_text(
        json.dumps({"pid": os.getpid(), "port": actual_port, "url": url}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    def _handle_exit(signum, frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_exit)
    signal.signal(signal.SIGINT, _handle_exit)

    print(f"本地工作台已启动: {url}")
    print("按 Ctrl+C 或运行停止脚本退出服务")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_all_child_procs()
        try:
            server.server_close()
        except Exception:
            pass
        ACTIVE_SERVER = None
        try:
            SERVER_META_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        print("\n服务已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
