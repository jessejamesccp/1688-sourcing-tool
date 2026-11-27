#!/usr/bin/env python3
"""Step 2: Find matching 1688 products for ASINs in raw_products using 1688-product-find skill.

Strategy:
1. Query `raw_products` (either pending ASINs, `--force` all, or `--asins` specific ASINs).
2. Over-fetch candidates (`min(limit * 3, 30)`) so we can deduplicate by `supplier`.
3. Try `image_search` sequentially across chosen images (`./image/<ASIN>.jpg` and/or extra URLs).
4. Validate results (non-empty + title overlaps with core Chinese terms from `keyword_zh`).
5. If image_search returns 0 results or fails validation (off-topic), fallback to `text_search` with `keyword_zh`.
6. Deduplicate by `supplier` (keeping highest sold_count / similarity_score per supplier), keep top `limit`.
7. Visit detail pages in Phase 2 to populate `shop_origin` & `factory_url` into SQLite (`data/state.db`).
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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import sync_playwright

from runtime_config import build_skill_env, get_python_bin
from step3_get_contacts import (
    PROFILE_DIR,
    check_human_intervention,
    extract_detail_metadata,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "state.db"
SKILL_CLI = BASE_DIR / "skills" / "1688-product-find" / "cli.py"
SUPPLIER_SKILL_CLI = BASE_DIR / "skills" / "1688-source-suppliers" / "cli.py"


def init_matched_offers_table(conn: sqlite3.Connection) -> None:
    """Create matched_offers and suppliers tables if they do not exist."""
    conn.execute(
        """
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
    )
    mo_cols = {row[1] for row in conn.execute("PRAGMA table_info(matched_offers)").fetchall()}
    if "shop_origin" not in mo_cols:
        conn.execute("ALTER TABLE matched_offers ADD COLUMN shop_origin TEXT DEFAULT ''")
    if "factory_url" not in mo_cols:
        conn.execute("ALTER TABLE matched_offers ADD COLUMN factory_url TEXT DEFAULT ''")
    if "detail_checked" not in mo_cols:
        conn.execute("ALTER TABLE matched_offers ADD COLUMN detail_checked INTEGER DEFAULT 0")
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
    conn.commit()


def call_skill(cmd_args: list[str], cli_path: Path = SKILL_CLI) -> dict:
    """Run a 1688 skill cli.py and parse its JSON output."""
    python_bin = get_python_bin()
    cmd = [python_bin, str(cli_path), *cmd_args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=build_skill_env(),
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"Skill CLI failed ({proc.returncode}): {proc.stderr.strip()}")
    payload = json.loads(proc.stdout)
    if payload.get("success") is False:
        md = str(payload.get("markdown") or "")
        err_code = str(payload.get("error_code") or "")
        action = str((payload.get("data") or {}).get("action") or "")
        if (
            action == "run_get_ak"
            or "AK 未就绪" in md
            or "AK 未配置" in md
            or "AK_INVALID" in err_code
            or "InvalidAccessKey" in md
        ):
            print("[AK错误] 1688 AK 未配置或已失效，请重新授权配置", flush=True)
            raise RuntimeError("AK 未就绪或已失效，请重新授权配置 1688 AK")
    return payload


def enrich_supplier(conn: sqlite3.Connection, supplier_name: str) -> None:
    """Look up supplier profile badges via 1688-source-suppliers without trusting unverified company_url."""
    supplier_name = (supplier_name or "").strip()
    if not supplier_name or not SUPPLIER_SKILL_CLI.exists():
        return
    cached = conn.execute("SELECT location, factory_level FROM suppliers WHERE supplier = ?", (supplier_name,)).fetchone()
    if cached is not None and (cached[0] or cached[1]):
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    query_name = re.sub(r"[\(（].*?[\)）]", "", supplier_name).strip() or supplier_name
    try:
        res = call_skill(["ali_1688_source_suppliers", "--query", query_name], cli_path=SUPPLIER_SKILL_CLI)
        factories = res.get("data", {}).get("factories", [])
        match = next((f for f in factories if (f.get("companyName") or "").strip() == supplier_name), None)
        if not match and factories:
            match = next((f for f in factories if query_name and query_name in (f.get("companyName") or "")), None)
        if match:
            conn.execute(
                """
                INSERT INTO suppliers
                (supplier, company_url, location, factory_level, monthly_orders, rec_tags, updated_at)
                VALUES (?, '', ?, ?, ?, ?, ?)
                ON CONFLICT(supplier) DO UPDATE SET
                    location = excluded.location,
                    factory_level = excluded.factory_level,
                    monthly_orders = excluded.monthly_orders,
                    rec_tags = excluded.rec_tags,
                    updated_at = excluded.updated_at
                """,
                (
                    supplier_name,
                    match.get("location", ""),
                    match.get("factoryLevel", ""),
                    int(match.get("monthlyOrders") or 0),
                    ", ".join(match.get("recTags") or []),
                    now_str,
                ),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO suppliers (supplier, company_url, updated_at) VALUES (?, '', ?)",
                (supplier_name, now_str),
            )
        conn.commit()
    except Exception:
        pass


class _DetailBrowserSession:
    """Lazy persistent Chrome session reused across matched offers to extract detail-page links."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None

    def get_page(self) -> Any:
        if self._page is None:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self._pw = sync_playwright().start()
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass


def _lookup_cached_links(conn: sqlite3.Connection, product_ids: list[str]) -> dict[str, tuple[str, str]]:
    """Return cached {product_id: (shop_origin, factory_url)} already verified via detail-page logic."""
    cached: dict[str, tuple[str, str]] = {}
    if not product_ids:
        return cached
    placeholders = ",".join("?" for _ in product_ids)
    for pid, shop_origin, factory_url in conn.execute(
        f"""
        SELECT product_id, shop_origin, factory_url
        FROM matched_offers
        WHERE product_id IN ({placeholders})
          AND COALESCE(detail_checked, 0) = 1
          AND COALESCE(shop_origin, '') != ''
        """,
        product_ids,
    ).fetchall():
        cached[str(pid)] = (shop_origin or "", factory_url or "")

    try:
        for pid, shop_origin, factory_url in conn.execute(
            f"""
            SELECT offer_id, shop_origin, factory_url
            FROM contacts
            WHERE offer_id IN ({placeholders})
              AND COALESCE(shop_origin, '') != ''
            """,
            product_ids,
        ).fetchall():
            if str(pid) not in cached:
                cached[str(pid)] = (shop_origin or "", factory_url or "")
    except sqlite3.OperationalError:
        pass
    return cached


def _save_offer_detail_links(
    conn: sqlite3.Connection,
    product_id: str,
    supplier_name: str,
    shop_origin: str,
    factory_url: str,
    now_str: str,
) -> None:
    """Persist detail-page verified shop_origin & factory_url into matched_offers, suppliers, and contacts."""
    conn.execute(
        "UPDATE matched_offers SET shop_origin = ?, factory_url = ?, detail_checked = 1 WHERE product_id = ?",
        (shop_origin, factory_url, product_id),
    )
    if supplier_name:
        conn.execute(
            """
            INSERT INTO suppliers (supplier, company_url, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(supplier) DO UPDATE SET
                company_url = excluded.company_url,
                updated_at = excluded.updated_at
            """,
            (supplier_name, factory_url, now_str),
        )
    try:
        conn.execute(
            """
            UPDATE contacts
            SET shop_origin = CASE WHEN ? != '' THEN ? ELSE shop_origin END,
                factory_url = ?,
                updated_at = ?
            WHERE offer_id = ?
            """,
            (shop_origin, shop_origin, factory_url, now_str, product_id),
        )
    except sqlite3.OperationalError:
        pass
    conn.commit()


def resolve_detail_links_for_products(
    conn: sqlite3.Connection,
    products: list[dict],
    browser: _DetailBrowserSession,
    *,
    cached_links: dict[str, tuple[str, str]],
    force_links: bool = False,
) -> None:
    """Visit each unmatched detail_url and extract shop_origin & factory_url using detail-page logic."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pending: list[tuple[str, str, str]] = []
    cached_hits: list[tuple[str, str]] = []
    for item in products:
        pid = str(item.get("product_id", ""))
        detail_url = (item.get("detail_url") or "").strip()
        supplier_name = (item.get("supplier") or "").strip()
        if not force_links and pid in cached_links:
            s_origin, f_url = cached_links[pid]
            _save_offer_detail_links(conn, pid, supplier_name, s_origin, f_url, now_str)
            cached_hits.append((pid, supplier_name))
            continue
        if detail_url:
            pending.append((pid, detail_url, supplier_name))

    total = len(cached_hits) + len(pending)
    done_base = len(cached_hits)
    print(f"[详情开始] total={total} done={done_base}", flush=True)
    if not pending:
        return

    print(f"  [详情页] 开始识别 {len(pending)} 家店铺主页与工厂档案...", flush=True)
    page = browser.get_page()
    for idx, (pid, detail_url, supplier_name) in enumerate(pending, start=1):
        current_done = done_base + idx
        print(f"[详情进度] [{current_done}/{total}] {supplier_name or pid}", flush=True)
        try:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            check_human_intervention(page)
            meta = extract_detail_metadata(page)
            shop_origin = meta.get("shop_origin", "")
            factory_url = meta.get("factory_url", "")
            _save_offer_detail_links(conn, pid, supplier_name, shop_origin, factory_url, now_str)
        except Exception as exc:
            print(f"  [详情页跳过] {supplier_name or pid}: {exc}", flush=True)
        if idx < len(pending):
            time.sleep(random.uniform(0.6, 1.4))


def extract_zh_grams(keyword_zh: str) -> set[str]:
    """Extract 2-char Chinese n-grams from the primary noun phrase for topic validation."""
    if not keyword_zh:
        return set()
    first_part = keyword_zh.strip().split()[0]
    zh_chunks = re.findall(r"[\u4e00-\u9fff]+", first_part)
    grams: set[str] = set()
    for chunk in zh_chunks:
        if len(chunk) == 1:
            grams.add(chunk)
        else:
            for i in range(len(chunk) - 1):
                grams.add(chunk[i : i + 2])
            if len(chunk) >= 3:
                grams.add(chunk[-1])
    return grams


def is_image_result_relevant(products: list[dict], keyword_zh: str) -> bool:
    """Check whether at least 2 (or >=40%) of returned titles match the expected Chinese category."""
    if not products:
        return False
    grams = extract_zh_grams(keyword_zh)
    if not grams:
        return True

    hits = 0
    for item in products:
        title = item.get("title") or ""
        if any(g in title for g in grams):
            hits += 1
    needed = min(2, len(products))
    return hits >= needed


def deduplicate_by_supplier(products: list[dict], limit: int) -> list[dict]:
    """Keep at most one listing per supplier (preferring higher sold_count, then similarity_score)."""
    best_by_supplier: dict[str, dict] = {}
    ordered_keys: list[str] = []

    for item in products:
        supplier = (item.get("supplier") or "").strip()
        key = supplier if supplier else f"__pid_{item.get('product_id', '')}"
        sold = item.get("sold_count") or 0
        score = float(item.get("similarity_score") or 0.0)

        if key not in best_by_supplier:
            best_by_supplier[key] = item
            ordered_keys.append(key)
        else:
            prev = best_by_supplier[key]
            prev_sold = prev.get("sold_count") or 0
            prev_score = float(prev.get("similarity_score") or 0.0)
            if (sold, score) > (prev_sold, prev_score):
                best_by_supplier[key] = item

    deduped = [best_by_supplier[k] for k in ordered_keys]
    return deduped[:limit]


def process_asin(
    conn: sqlite3.Connection,
    asin: str,
    local_image_path: str,
    keyword_zh: str,
    clean_keyword: str,
    limit: int,
    mode: str = "auto",
    custom_query: str = "",
    progress_base: int = 0,
    progress_total: int = 0,
    image_sources: Optional[list[str]] = None,
) -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """Find matching 1688 offers for one ASIN, deduplicate by supplier, and store in matched_offers."""
    fetch_limit = min(max(limit * 3, 15), 30)
    raw_products: list[dict] = []
    method_used = ""
    search_error = ""

    # 1. Image search (used in 'auto' or 'image' mode; supports 1 or more selected images sequentially)
    if mode in ("auto", "image"):
        sources = [s.strip() for s in (image_sources or []) if s and s.strip()]
        if not sources and local_image_path:
            sources = [local_image_path]

        valid_img_batches = 0
        for img_idx, img_src in enumerate(sources, start=1):
            if img_src.startswith(("http://", "https://")):
                cli_target = img_src
            else:
                img_abs = BASE_DIR / img_src if not Path(img_src).is_absolute() else Path(img_src)
                if not img_abs.exists():
                    continue
                cli_target = str(img_abs)

            print(f"  [图搜 {img_idx}/{len(sources)}] 使用图片: {img_src} (模式={mode}, 召回 {fetch_limit} 条去重)", flush=True)
            try:
                res = call_skill([
                    "image_search",
                    "-i", cli_target,
                    "-l", str(fetch_limit),
                    "--score-level", "medium",
                    "--tags", "",
                ])
                candidates = res.get("data", {}).get("data", {}).get("similar_products", [])
                if mode == "image":
                    raw_products.extend(candidates)
                    valid_img_batches += 1
                    print(f"  [图搜结果] 原始 {len(candidates)} 条", flush=True)
                elif is_image_result_relevant(candidates, keyword_zh):
                    raw_products.extend(candidates)
                    valid_img_batches += 1
                    print(f"  [图搜通过] 原始 {len(candidates)} 条", flush=True)
                else:
                    print(f"  [图搜过滤] 返回 {len(candidates)} 条但与「{keyword_zh}」品类不符", flush=True)
            except Exception as exc:
                search_error = str(exc)
                print(f"  [图搜异常] {exc}", flush=True)

        if raw_products:
            suffix = f"·{len(sources)}图" if len(sources) > 1 else ""
            method_used = f"图搜(强制{suffix})" if mode == "image" else f"图搜(image_search{suffix})"
            search_error = ""
        elif mode == "auto" and sources:
            print(f"  [图搜回退] 所选图片均未命中「{keyword_zh}」，切换中文关键词搜索...", flush=True)

    # 2. Text search (used in 'text' mode, or as fallback in 'auto' mode)
    if (mode == "text") or (mode == "auto" and not raw_products):
        query = (custom_query or keyword_zh or clean_keyword or "").strip()
        if query:
            print(f"  [词搜] 使用关键词: {query} (召回 {fetch_limit} 条去重)")
            try:
                res = call_skill([
                    "text_search",
                    "-q", query,
                    "-l", str(fetch_limit),
                    "--tags", "",
                ])
                raw_products = res.get("data", {}).get("data", {}).get("similar_products", [])
                method_used = f"词搜({query})"
                search_error = ""
                print(f"  [词搜结果] 原始 {len(raw_products)} 条")
            except Exception as exc:
                search_error = str(exc)
                print(f"  [词搜异常] {exc}")

    products = deduplicate_by_supplier(raw_products, limit)
    print(f"  [供应商去重] 保留 {len(products)} 家不同店铺")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cached_links = _lookup_cached_links(conn, [str(item.get("product_id", "")) for item in products])

    if products:
        total_target = progress_total or limit
        # Clear previous offers for this ASIN so re-runs replace stale duplicates cleanly
        conn.execute("DELETE FROM matched_offers WHERE asin = ?", (asin,))
        for p_idx, item in enumerate(products, start=1):
            conn.execute(
                """
                INSERT INTO matched_offers (
                    asin, product_id, title, detail_url, image_url, price,
                    sku_title, quantity_begin, sold_count, yx_index, supplier,
                    similarity_score, search_method, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asin,
                    str(item.get("product_id", "")),
                    item.get("title", ""),
                    item.get("detail_url", ""),
                    item.get("image_url", ""),
                    item.get("price"),
                    item.get("sku_title", ""),
                    item.get("quantity_begin"),
                    item.get("sold_count"),
                    item.get("yx_index"),
                    item.get("supplier", ""),
                    item.get("similarity_score"),
                    method_used,
                    now_str,
                ),
            )
            enrich_supplier(conn, item.get("supplier", ""))
            label = item.get("supplier") or item.get("product_id") or ""
            print(f"[同款进度] done={progress_base + p_idx} total={total_target} label={asin} · {label}", flush=True)
        conn.execute(
            "UPDATE raw_products SET match_status = ?, updated_at = ? WHERE asin = ?",
            (f"已匹配({len(products)}家店)", now_str, asin),
        )
        conn.commit()
    elif search_error:
        conn.execute(
            "UPDATE raw_products SET match_status = ?, updated_at = ? WHERE asin = ?",
            ("搜索接口异常", now_str, asin),
        )
        conn.commit()
    else:
        conn.execute("DELETE FROM matched_offers WHERE asin = ?", (asin,))
        conn.execute(
            "UPDATE raw_products SET match_status = ?, updated_at = ? WHERE asin = ?",
            ("未找到同款", now_str, asin),
        )
        conn.commit()
    return products, cached_links


def main() -> int:
    def _handle_sigterm(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    parser = argparse.ArgumentParser(description="Step 2: 调用 1688-product-find 批量找同款并按供应商去重")
    parser.add_argument("--limit", type=int, default=10, help="每个 ASIN 最多保留几家不同供应商 (默认 10)")
    parser.add_argument("--delay", type=float, default=1.5, help="每个 ASIN 之间的间隔秒数")
    parser.add_argument("--force", action="store_true", help="忽略已有状态，重跑全部 ASIN")
    parser.add_argument("--asins", default="", help="指定重跑的 ASIN，英文逗号分隔 (如 B0DNSXFJTV,B0CVVWR4M1)")
    parser.add_argument("--mode", choices=["auto", "image", "text"], default="auto", help="搜索模式: auto / image / text")
    parser.add_argument("--query", default="", help="手动指定搜索关键词（覆盖默认 keyword_zh）")
    parser.add_argument("--images-file", default="", help="JSON 文件路径，指定要用于图搜的图片路径/URL 列表")
    parser.add_argument("--skip-detail-links", action="store_true", help="仅找同款，跳过打开详情页写入店铺主页与工厂档案")
    parser.add_argument("--force-links", action="store_true", help="忽略已有缓存，强制重新打开详情页更新店铺主页与工厂档案")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"未找到数据库 {DB_PATH}，请先运行 step1_import_excel.py")
        return 1

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    init_matched_offers_table(conn)

    if args.asins.strip():
        target_asins = [a.strip().upper() for a in args.asins.split(",") if a.strip()]
        placeholders = ",".join("?" for _ in target_asins)
        rows = conn.execute(
            f"SELECT asin, local_image_path, keyword_zh, clean_keyword FROM raw_products WHERE asin IN ({placeholders}) ORDER BY COALESCE(id, rowid) ASC",
            target_asins,
        ).fetchall()
    elif args.force:
        rows = conn.execute(
            "SELECT asin, local_image_path, keyword_zh, clean_keyword FROM raw_products ORDER BY COALESCE(id, rowid) ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT asin, local_image_path, keyword_zh, clean_keyword FROM raw_products WHERE match_status = '待找同款' ORDER BY COALESCE(id, rowid) ASC"
        ).fetchall()

    if not rows:
        print("没有需要匹配的 ASIN（如需重跑可加 --force 或 --asins）。")
        conn.close()
        return 0

    custom_images: Optional[list[str]] = None
    if args.images_file and Path(args.images_file).exists():
        try:
            loaded_imgs = json.loads(Path(args.images_file).read_text(encoding="utf-8"))
            if isinstance(loaded_imgs, list):
                custom_images = [str(u).strip() for u in loaded_imgs if str(u).strip()]
        except Exception:
            pass

    print(f"共找到 {len(rows)} 个待匹配 ASIN (每款保留最多 {args.limit} 家不同供应商, 模式={args.mode})...", flush=True)
    all_products_by_pid: dict[str, dict] = {}
    all_cached_links: dict[str, tuple[str, str]] = {}
    browser = _DetailBrowserSession()
    try:
        # Phase 1: Complete 1688 search matching for all target ASINs first
        total_slots = len(rows) * args.limit
        for idx, (asin, local_img, kw_zh, kw_en) in enumerate(rows, start=1):
            print(f"\n--- [{idx}/{len(rows)}] {asin} ({args.query or kw_zh or kw_en}) ---", flush=True)
            products, cached_links = process_asin(
                conn,
                asin,
                local_img or "",
                kw_zh or "",
                kw_en or "",
                args.limit,
                mode=args.mode,
                custom_query=args.query.strip(),
                progress_base=(idx - 1) * args.limit,
                progress_total=total_slots,
                image_sources=custom_images if len(rows) == 1 else None,
            )
            for item in products:
                pid = str(item.get("product_id", ""))
                if pid and pid not in all_products_by_pid:
                    all_products_by_pid[pid] = item
            all_cached_links.update(cached_links)
            print(f"[同款完成] done={idx * args.limit} total={total_slots} asin={asin} count={len(products)}", flush=True)
            if idx < len(rows):
                time.sleep(args.delay)

        # Phase 2: Visit detail pages to populate shop_origin & factory_url
        if not args.skip_detail_links and all_products_by_pid:
            resolve_detail_links_for_products(
                conn,
                list(all_products_by_pid.values()),
                browser,
                cached_links=all_cached_links,
                force_links=args.force_links,
            )
    finally:
        browser.close()
        conn.close()

    print(f"\n全部完成！结果已保存到数据库: {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
