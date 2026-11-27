#!/usr/bin/env python3
"""Step 1: Stream-parse a large Amazon product Excel/CSV, clean key fields, download main images, and store into SQLite.

- Reads the first main sheet (`raw_products`) and optionally A+ image details (`product_images`).
- Maps columns by header name (safe against column reordering).
- Downloads main images concurrently into `./image/<ASIN>.<ext>` and stores relative paths.
- Auto-translates missing Chinese search keywords (`keyword_zh`) and saves into `data/state.db`.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Iterable

from openpyxl import load_workbook
import requests

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "state.db"
IMAGE_DIR = BASE_DIR / "image"

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def init_tables(conn: sqlite3.Connection) -> None:
    """Create raw_products and product_images tables with `id` as first PK column."""
    conn.execute(
        """
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
    )
    conn.execute(
        """
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
    )
    conn.commit()


def clean_price(raw_price: str) -> str:
    """Extract numeric USD price if present, otherwise return empty string."""
    if not raw_price:
        return ""
    m = re.search(r"(\d+(?:\.\d+)?)", str(raw_price))
    return m.group(1) if m and "没有当前售价" not in str(raw_price) else ""


def extract_clean_keyword(title: str, brand: str, category_small: str) -> str:
    """Build a concise search keyword by stripping brand name and trailing filler clauses."""
    kw = (title or "").strip()
    if brand:
        kw = re.sub(rf"\b{re.escape(brand.strip())}\b", "", kw, flags=re.IGNORECASE).strip()

    kw = re.split(r"[,|]|(?:\s+-\s+)", kw)[0].strip()
    kw = re.sub(
        r"\b(?:Suitable for|Ideal for|Perfect for|for Home|for Bedroom)\b.*$",
        "",
        kw,
        flags=re.IGNORECASE,
    ).strip()
    kw = re.sub(r"\s+", " ", kw).strip(" -–,")

    words = kw.split()
    if len(words) > 8:
        kw = " ".join(words[:8])

    if not kw and category_small:
        kw = category_small.strip()
    return kw


def clean_for_translation(text: str) -> str:
    """Strip size/volume noise so machine translation produces crisp 1688 search keywords."""
    cleaned = re.sub(
        r'\b\d+(?:\.\d+)?\s*["\'”″]?\s*[xX×*]\s*\d+(?:\.\d+)?\s*["\'”″]?(?:\s*[xX×*]\s*\d+(?:\.\d+)?\s*["\'”″]?)?',
        " ",
        text or "",
    )
    cleaned = re.sub(r"\b\d+(?:\.\d+)?\s*(?:cu\.?\s*ft\.?|inch(?:es)?|lbs?)\b", " ", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip(" -–,") or (text or "").strip()


def translate_en_to_zh(text: str) -> str:
    """Translate clean English product keyword to concise Chinese for 1688 search."""
    q = clean_for_translation(text)
    if not q:
        return ""
    if re.search(r"[\u4e00-\u9fff]", q):
        return q

    # 1. Google Translate lightweight endpoint (short timeout so China networks fail fast)
    try:
        resp = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": q},
            timeout=2.5,
        )
        if resp.status_code == 200:
            data = resp.json()
            zh = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
            zh = re.sub(r"[，。、；：“”\"']+", " ", zh)
            zh = re.sub(r"\s+", " ", zh).strip()
            if zh:
                return zh
    except Exception:
        pass

    # 2. Fallback: MyMemory public API
    try:
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": q, "langpair": "en|zh-CN"},
            timeout=6,
        )
        if resp.status_code == 200:
            zh = ((resp.json().get("responseData") or {}).get("translatedText") or "").strip()
            zh = re.sub(r"[，。、；：“”\"']+", " ", zh)
            zh = re.sub(r"\s+", " ", zh).strip()
            if zh and "MYMEMORY WARNING" not in zh.upper():
                return zh
    except Exception:
        pass

    return ""


def download_one_image(asin: str, url: str) -> tuple[str, str]:
    """Download main image into ./image/<ASIN>.jpg and return (asin, relative_path)."""
    if not url or not url.startswith("http"):
        return asin, ""

    ext = ".png" if ".png" in url.lower().split("?")[0] else ".jpg"
    rel_path = f"image/{asin}{ext}"
    dest = BASE_DIR / rel_path
    if dest.exists() and dest.stat().st_size > 100:
        return asin, rel_path

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            if len(data) > 100:
                dest.write_bytes(data)
                return asin, rel_path
    except Exception as exc:
        print(f"  [下载失败] {asin}: {exc}")
    return asin, ""


def iter_csv_sections(input_path: Path) -> list[tuple[list[str], list[list[str]]]]:
    """Split a concatenated CSV (if multiple header rows exist) into sections."""
    sections: list[tuple[list[str], list[list[str]]]] = []
    current_headers: list[str] | None = None
    current_rows: list[list[str]] = []

    with input_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            first = str(row[0]).strip()
            if first == "ASIN":
                if current_headers is not None:
                    sections.append((current_headers, current_rows))
                current_headers = [str(c).strip() for c in row]
                current_rows = []
            elif current_headers is not None:
                current_rows.append([str(c).strip() for c in row])

    if current_headers is not None:
        sections.append((current_headers, current_rows))
    return sections


def iter_xlsx_first_sheet(input_path: Path) -> tuple[list[str], Iterable[list[str]], object]:
    wb = load_workbook(filename=input_path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    row_gen = ws.iter_rows(values_only=True)
    headers: list[str] = []
    for raw in row_gen:
        cand = ["" if v is None else str(v).strip() for v in raw]
        if "ASIN" in cand:
            headers = cand
            break
    if not headers:
        raise ValueError("第 1 个 sheet 未找到包含 ASIN 的表头")

    def gen() -> Iterable[list[str]]:
        for row in row_gen:
            yield ["" if v is None else str(v).strip() for v in row]

    return headers, gen(), wb


def parse_extra_image_rows(headers: list[str], rows: Iterable[list[str]], conn: sqlite3.Connection) -> int:
    """Import A+ / extra image URLs keyed by ASIN into product_images (URL only, no download)."""
    col_idx = {name: idx for idx, name in enumerate(headers)}
    if "ASIN" not in col_idx or "图片链接" not in col_idx or "图片序号" not in col_idx:
        return 0

    parsed_rows: list[tuple[str, int, str, str, str]] = []
    seen_asins: set[str] = set()
    for row in rows:
        asin = row[col_idx["ASIN"]].upper() if col_idx["ASIN"] < len(row) else ""
        if not ASIN_RE.match(asin):
            continue
        seq_raw = row[col_idx["图片序号"]] if col_idx["图片序号"] < len(row) else "0"
        url = row[col_idx["图片链接"]] if col_idx["图片链接"] < len(row) else ""
        if not url.startswith("http"):
            continue
        ocr_status = row[col_idx["识别状态"]] if "识别状态" in col_idx and col_idx["识别状态"] < len(row) else ""
        ocr_text = row[col_idx["图中文字"]] if "图中文字" in col_idx and col_idx["图中文字"] < len(row) else ""
        try:
            seq = int(seq_raw)
        except ValueError:
            seq = 0
        seen_asins.add(asin)
        parsed_rows.append((asin, seq, ocr_status, ocr_text, url))

    if seen_asins:
        asin_list = list(seen_asins)
        for i in range(0, len(asin_list), 400):
            chunk = asin_list[i : i + 400]
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM product_images WHERE asin IN ({placeholders})", chunk)

    for item in parsed_rows:
        conn.execute(
            """
            INSERT INTO product_images (asin, seq, ocr_status, ocr_text, image_url)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(asin, seq) DO UPDATE SET
                ocr_status = excluded.ocr_status,
                ocr_text = excluded.ocr_text,
                image_url = excluded.image_url
            """,
            item,
        )
    return len(parsed_rows)


def import_file(
    input_path: Path,
    download_images: bool = True,
    workers: int = 8,
    progress_cb=None,
    auto_translate: bool = True,
    cancel_event=None,
) -> dict[str, int | bool]:
    """Parse input file, validate ASIN/image/keyword, download main images, save to SQLite."""
    def is_cancelled() -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    def emit_progress(
        dl_done: int = 0,
        dl_total: int = 0,
        dl_note: str = "",
        tr_done: int = 0,
        tr_total: int = 0,
        tr_note: str = "",
    ) -> None:
        if progress_cb is not None:
            progress_cb({
                "dl_done": dl_done,
                "dl_total": dl_total,
                "dl_note": dl_note,
                "tr_done": tr_done,
                "tr_total": tr_total,
                "tr_note": tr_note,
            })

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    emit_progress(0, 0, "正在解析表格与关联图片...", 0, 0, "等待主图下载完成后开始")

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    init_tables(conn)

    suffix = input_path.suffix.lower()
    wb_to_close = None
    extra_sections: list[tuple[list[str], list[list[str]]]] = []

    if suffix == ".csv":
        sections = iter_csv_sections(input_path)
        if not sections:
            print("文件为空！")
            return {"valid_count": 0, "downloaded_count": 0, "to_download_count": 0, "extra_img_count": 0}
        headers, main_rows = sections[0]
        extra_sections = sections[1:]
    elif suffix in (".xlsx", ".xlsm"):
        headers, main_rows, wb_to_close = iter_xlsx_first_sheet(input_path)
    else:
        raise ValueError(f"Unsupported file format: {suffix} (use .xlsx or .csv)")

    col_idx = {name: idx for idx, name in enumerate(headers)}
    required_cols = ["ASIN", "主图链接", "产品名称"]
    missing = [c for c in required_cols if c not in col_idx]
    if missing:
        raise ValueError(f"表头缺少必需列: {missing}，当前表头前10列为: {headers[:10]}")

    def get_val(row: list[str], col_name: str) -> str:
        idx = col_idx.get(col_name)
        if idx is None or idx >= len(row):
            return ""
        return str(row[idx]).strip()

    total_rows = 0
    valid_count = 0
    skipped_invalid = 0
    imported_asins: list[str] = []
    to_download: list[tuple[str, str]] = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for row in main_rows:
        if is_cancelled():
            if wb_to_close is not None:
                wb_to_close.close()
            conn.rollback()
            conn.close()
            return {"valid_count": 0, "downloaded_count": 0, "to_download_count": 0, "extra_img_count": 0, "translated_count": 0, "cancelled": True}
        total_rows += 1
        asin = get_val(row, "ASIN").upper()
        if not ASIN_RE.match(asin):
            skipped_invalid += 1
            continue

        amazon_url = get_val(row, "商品链接")
        image_url = get_val(row, "主图链接")
        if not image_url.startswith("http"):
            image_url = ""
        if image_url:
            to_download.append((asin, image_url))

        title = get_val(row, "产品名称")
        brand = get_val(row, "品牌")
        category_large = get_val(row, "大类目名称")
        rank_large = get_val(row, "大类目排名")
        if "未取得" in category_large:
            category_large = ""
        if "未取得" in rank_large:
            rank_large = ""
        category_small = get_val(row, "小类目名称")
        rank_small = get_val(row, "小类目排名")
        if "未取得" in category_small:
            category_small = ""
        if "未取得" in rank_small:
            rank_small = ""
        clean_kw = extract_clean_keyword(title, brand, category_small)
        keyword_zh_in = get_val(row, "中文关键词")
        price_usd = clean_price(get_val(row, "当前售价 USD"))
        dimensions = get_val(row, "产品尺寸")
        if "未取得" in dimensions:
            dimensions = ""
        rating = get_val(row, "星级（满分 5）")
        review_count = get_val(row, "评分人数")
        if "未取得" in review_count:
            review_count = ""

        # Check if matched_offers already exists for this ASIN (e.g. product was deleted & re-imported)
        existing_match_cnt = 0
        try:
            existing_match_cnt = conn.execute(
                "SELECT COUNT(*) FROM matched_offers WHERE asin = ?", (asin,)
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        synced_match_status = f"已匹配({existing_match_cnt}家店)" if existing_match_cnt > 0 else "待找同款"

        conn.execute(
            """
            INSERT INTO raw_products (
                asin, amazon_url, image_url, title, brand, clean_keyword, keyword_zh,
                price_usd, dimensions, category_large, rank_large,
                category_small, rank_small, rating, review_count, match_status, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asin) DO UPDATE SET
                amazon_url = excluded.amazon_url,
                image_url = excluded.image_url,
                title = excluded.title,
                brand = excluded.brand,
                clean_keyword = excluded.clean_keyword,
                keyword_zh = CASE WHEN excluded.keyword_zh != '' THEN excluded.keyword_zh ELSE raw_products.keyword_zh END,
                price_usd = excluded.price_usd,
                dimensions = excluded.dimensions,
                category_large = excluded.category_large,
                rank_large = excluded.rank_large,
                category_small = excluded.category_small,
                rank_small = excluded.rank_small,
                rating = excluded.rating,
                review_count = excluded.review_count,
                match_status = CASE WHEN ? > 0 THEN excluded.match_status ELSE raw_products.match_status END,
                updated_at = excluded.updated_at
            """,
            (
                asin,
                amazon_url,
                image_url,
                title,
                brand,
                clean_kw,
                keyword_zh_in,
                price_usd,
                dimensions,
                category_large,
                rank_large,
                category_small,
                rank_small,
                rating,
                review_count,
                synced_match_status,
                now_str,
                existing_match_cnt,
            ),
        )
        imported_asins.append(asin)
        valid_count += 1

    # Also import A+ / extra image URLs if present in CSV sections or xlsx sheets
    extra_img_count = 0
    for sec_headers, sec_rows in extra_sections:
        extra_img_count += parse_extra_image_rows(sec_headers, sec_rows, conn)

    if wb_to_close is not None:
        for ws in wb_to_close.worksheets[1:]:
            row_gen = ws.iter_rows(values_only=True)
            sec_headers: list[str] = []
            for raw in row_gen:
                cand = ["" if v is None else str(v).strip() for v in raw]
                if "ASIN" in cand and "图片链接" in cand and "图片序号" in cand:
                    sec_headers = cand
                    break
            if sec_headers:
                sec_rows = (["" if v is None else str(v).strip() for v in r] for r in row_gen)
                extra_img_count += parse_extra_image_rows(sec_headers, sec_rows, conn)
        wb_to_close.close()

    conn.commit()

    # Download main images into ./image/
    downloaded_count = 0
    total_dl = len(to_download) if download_images else 0
    done_dl = 0
    if download_images and to_download and not is_cancelled():
        emit_progress(0, total_dl, "正在下载...", 0, 0, "等待主图下载完成后开始")
        print(f"正在下载 {total_dl} 张主图到 {IMAGE_DIR} ...")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(download_one_image, asin, url) for asin, url in to_download]
            for fut in as_completed(futures):
                if is_cancelled():
                    for pending_f in futures:
                        pending_f.cancel()
                    break
                asin, rel_path = fut.result()
                done_dl += 1
                if rel_path:
                    downloaded_count += 1
                    conn.execute(
                        "UPDATE raw_products SET local_image_path = ? WHERE asin = ?",
                        (rel_path, asin),
                    )
                emit_progress(done_dl, total_dl, asin, 0, 0, "等待主图下载完成后开始")
        conn.commit()
    if is_cancelled():
        conn.close()
        return {
            "valid_count": valid_count,
            "downloaded_count": downloaded_count,
            "to_download_count": total_dl,
            "extra_img_count": extra_img_count,
            "translated_count": 0,
            "cancelled": True,
        }
    emit_progress(total_dl, total_dl, "已完成", 0, 0, "准备翻译中文关键词...")

    # Auto-translate missing Chinese keywords (keyword_zh) for newly imported rows
    translated_count = 0
    if auto_translate and imported_asins and not is_cancelled():
        need_trans: list[tuple[str, str]] = []
        unique_asins = list(dict.fromkeys(imported_asins))
        for i in range(0, len(unique_asins), 400):
            chunk = unique_asins[i : i + 400]
            ph = ",".join("?" for _ in chunk)
            for a, ckw, kzh in conn.execute(
                f"SELECT asin, clean_keyword, keyword_zh FROM raw_products WHERE asin IN ({ph})",
                chunk,
            ).fetchall():
                if not (kzh or "").strip() and (ckw or "").strip():
                    need_trans.append((a, ckw))

        if need_trans:
            total_tr = len(need_trans)
            done_tr = 0
            emit_progress(total_dl, total_dl, "已完成", 0, total_tr, "正在翻译...")
            print(f"正在自动翻译 {total_tr} 条中文关键词...")
            with ThreadPoolExecutor(max_workers=min(workers, 6)) as pool:
                future_map = {pool.submit(translate_en_to_zh, ckw): a for a, ckw in need_trans}
                for fut in as_completed(future_map):
                    if is_cancelled():
                        for pending_f in future_map:
                            pending_f.cancel()
                        break
                    a = future_map[fut]
                    done_tr += 1
                    try:
                        zh = fut.result()
                    except Exception:
                        zh = ""
                    if zh:
                        translated_count += 1
                        conn.execute(
                            "UPDATE raw_products SET keyword_zh = ? WHERE asin = ? AND COALESCE(keyword_zh, '') = ''",
                            (zh, a),
                        )
                    emit_progress(total_dl, total_dl, "已完成", done_tr, total_tr, f"{a} {zh or ''}".strip())
            conn.commit()
            if not is_cancelled():
                emit_progress(total_dl, total_dl, "已完成", total_tr, total_tr, "已完成")
        else:
            emit_progress(total_dl, total_dl, "已完成", 0, 0, "无需翻译（已有中文关键词）")

    emit_progress(total_dl, total_dl, "已完成", translated_count, translated_count, "正在写入数据库...")
    conn.close()

    if is_cancelled():
        return {
            "valid_count": valid_count,
            "downloaded_count": downloaded_count,
            "to_download_count": total_dl,
            "extra_img_count": extra_img_count,
            "translated_count": translated_count,
            "cancelled": True,
        }

    print(f"导入完成！源文件: {input_path.name}")
    print(f"  扫描总行数: {total_rows}")
    print(f"  有效 ASIN 入库数: {valid_count} (跳过无效行: {skipped_invalid})")
    print(f"  主图本地下载成功: {downloaded_count}/{len(to_download)} (目录: {IMAGE_DIR})")
    print(f"  附图/A+链接入库: {extra_img_count} 条 (表: product_images)")
    print(f"  自动翻译中文关键词: {translated_count} 条")
    print(f"  已写入数据库: {DB_PATH}")
    return {
        "valid_count": valid_count,
        "downloaded_count": downloaded_count,
        "to_download_count": len(to_download),
        "extra_img_count": extra_img_count,
        "translated_count": translated_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Step 1: 清洗亚马逊大表格关键信息并下载主图落库")
    parser.add_argument("--input", required=True, help="输入的 .xlsx 或 .csv 文件路径")
    parser.add_argument("--no-download-images", action="store_true", help="仅入库链接，不下载主图到本地")
    parser.add_argument("--no-translate", action="store_true", help="跳过自动翻译中文关键词")
    parser.add_argument("--workers", type=int, default=8, help="主图并发下载线程数")
    args = parser.parse_args()
    import_file(
        Path(args.input),
        download_images=not args.no_download_images,
        workers=args.workers,
        auto_translate=not args.no_translate,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
