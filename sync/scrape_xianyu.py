#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
闲鱼(goofish.com) Web 端低频抓取器 —— Playwright 版

用法：
  python sync/scrape_xianyu.py --login        # 首次/过期时：弹出浏览器窗口，扫码登录闲鱼
  python sync/scrape_xianyu.py                # 无头抓取：按 sync/skus.json 的 xianyu_keyword 逐个搜索
  python sync/scrape_xianyu.py --headful      # 有头模式抓取（调试用）

登录态保存于 sync/xianyu_state.json（个人凭据，已加入 .gitignore，绝不入库）。
搜索间隔 4~8 秒随机，单次运行约 1~2 分钟（10 个 SKU）。

诚实说明：
  - 闲鱼 Web 端允许匿名浏览一次搜索，连续访问会强制登录，因此需要登录态；
  - 页面为 JS 渲染，解析依赖当前 DOM 结构，闲鱼改版会导致解析失败（失败安全跳过）；
  - 本工具仅供个人比价，请勿高频运行或转售数据。
"""
import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parent.parent
SKUS_PATH = ROOT / "sync" / "skus.json"
STATE_PATH = ROOT / "sync" / "xianyu_state.json"
DATA_PATH = ROOT / "data" / "prices.json"

BJ = timezone(timedelta(hours=8))
today_bj = lambda: datetime.now(BJ).strftime("%Y-%m-%d")
SEARCH_URL = "https://www.goofish.com/search?q="

# 标题中的干扰词（配件/回收/虚拟品），命中则剔除
EXCLUDE_WORDS = ["保护壳", "壳", "贴膜", "钢化膜", "键盘膜", "支架", "回收", "回收旧", "以旧换新",
                 "数据线", "充电器", "转接头", "扩展坞", "内胆包", "电脑包", "演示机", "模型", "纸"]


def log(msg):
    print(f"[{datetime.now(BJ).strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_price(text):
    """从卡片文本提取价格：第一个 ¥ 后的数字为现价（第二个通常是划线原价）"""
    m = re.search(r"¥\s*([0-9][0-9,]*)", text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def scrape_keyword(page, keyword, price_floor):
    """搜索一个关键词，返回条目列表 [(title, price, url)]"""
    url = SEARCH_URL + keyword
    entries = []
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        log(f"  ⚠️ 打开失败: {e}")
        return entries, "error"
    # 等商品卡片出现（登录墙/空结果/慢加载均最多等 12s）
    try:
        page.wait_for_selector("a[href*='item?id=']", timeout=12000)
    except PWTimeout:
        body = ""
        try:
            body = page.inner_text("body")
        except Exception:
            pass
        if "登录" in body[:2000] and "扫码" in body:
            return entries, "login_wall"
        return entries, "empty"
    # 轻微滚动触发懒加载
    try:
        page.mouse.wheel(0, random.randint(800, 1600))
        page.wait_for_timeout(random.randint(1200, 2200))
    except Exception:
        pass
    cards = page.query_selector_all("a[href*='item?id=']")
    seen = set()
    for card in cards[:40]:
        try:
            txt = (card.inner_text() or "").strip()
            href = card.get_attribute("href") or ""
            if not txt or href in seen:
                continue
            seen.add(href)
            price = parse_price(txt)
            if not price:
                continue
            # 卡片文本第一行为标题
            title = txt.split("\n")[0].strip()[:120]
            entries.append({"title": title, "price": price, "url": href})
        except Exception:
            continue
    return entries, "ok"


def filter_entries(entries, keyword_tokens, any_tokens, price_floor):
    """按标题关键词与价格下限过滤，返回通过筛选的条目。

    keyword_tokens: 标题必须全部命中；any_tokens: 任一命中即可（用于 14寸/16英寸 等变体）。
    """
    out = []
    for e in entries:
        t = e["title"]
        if any(w in t for w in EXCLUDE_WORDS):
            continue
        if price_floor and e["price"] < price_floor:
            continue
        tt = t.replace(" ", "").lower()
        if keyword_tokens and not all(tok.replace(" ", "").lower() in tt for tok in keyword_tokens):
            continue
        if any_tokens:
            norm = [g.replace(" ", "").lower() for g in any_tokens]
            if not any(g in tt for g in norm):
                continue
        out.append(e)
    return out


def do_login():
    """有头模式打开闲鱼，等待用户扫码登录，保存 storage state 并自动验证搜索可用性"""
    log("打开浏览器窗口，请在页面右上角点「登录」并扫码（短信登录亦可）…")
    LOGIN_COOKIES = {"unb", "lgc"}  # 仅登录后才会出现的淘宝用户 ID cookie
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width": 1280, "height": 850}, locale="zh-CN")
        page = ctx.new_page()
        page.goto("https://www.goofish.com/", timeout=30000)
        ok = False
        for _ in range(150):  # 最多 5 分钟
            page.wait_for_timeout(2000)
            try:
                names = {c["name"] for c in ctx.cookies("https://www.goofish.com")}
                if names & LOGIN_COOKIES:
                    ok = True
                    break
            except Exception:
                if page.is_closed():
                    break
        if ok:
            ctx.storage_state(path=str(STATE_PATH))
            log(f"✅ 检测到登录 cookie（unb/lgc），登录态已保存到 {STATE_PATH}")
            # 自动验证：带登录态打开搜索页，确认商品卡片能渲染
            try:
                page.goto(SEARCH_URL + "MacBook%20Pro", timeout=30000, wait_until="domcontentloaded")
                page.wait_for_selector("a[href*='item?id=']", timeout=15000)
                log("✅ 搜索页验证通过：商品列表可正常渲染")
            except PWTimeout:
                log("⚠️ 登录后搜索页仍未渲染商品（可能命中风控），可稍后重试或联系排查")
        else:
            log("⏰ 等待超时或窗口已关闭，未检测到登录成功（未保存）。可重试 python sync/scrape_xianyu.py --login")
        browser.close()
    return 0 if ok else 1


def run_scrape(headful=False):
    if not SKUS_PATH.exists():
        log(f"❌ 未找到 {SKUS_PATH}")
        return 1
    cfg = json.loads(SKUS_PATH.read_text(encoding="utf-8"))
    products = [p for p in cfg.get("products", []) if p.get("xianyu_keyword")]
    if not products:
        log("⏭️ 闲鱼源跳过：skus.json 中未配置任何 xianyu_keyword")
        return 0
    has_state = STATE_PATH.exists()
    if not has_state:
        log("⚠️ 未找到登录态文件，将以匿名模式尝试（连续访问可能触发登录墙）")

    all_entries = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        ctx = browser.new_context(viewport={"width": 1280, "height": 850}, locale="zh-CN",
                                  storage_state=str(STATE_PATH) if has_state else None)
        page = ctx.new_page()
        for prod in products:
            kw = prod["xianyu_keyword"]
            tokens = prod.get("xianyu_tokens") or [kw]
            any_tokens = prod.get("xianyu_any_tokens") or []
            floor = prod.get("xianyu_price_floor") or 0
            log(f"搜索: {kw}")
            entries, status = scrape_keyword(page, kw, floor)
            if status == "login_wall":
                log("⛔ 触发登录墙：请运行 python sync/scrape_xianyu.py --login 重新登录后重试")
                break
            passed = filter_entries(entries, tokens, any_tokens, floor)
            if entries:
                prices = sorted(e["price"] for e in entries)
                log(f"  抓到 {len(entries)} 条（过滤后 {len(passed)} 条）价格区间 {prices[0]}~{prices[-1]}")
            else:
                log(f"  抓到 0 条（状态 {status}）")
            if passed:
                best = min(passed, key=lambda e: e["price"])
                all_entries.append({
                    "match": prod["name"], "platform": "xianyu", "date": today_bj(),
                    "price": best["price"], "title": best["title"],
                    "source": "xianyu_web",
                    "url": "https://www.goofish.com" + best["url"] if best["url"].startswith("/") else best["url"],
                })
            # 低频礼貌间隔
            time.sleep(random.uniform(4, 8))
        browser.close()

    if not all_entries:
        log("⚠️ 闲鱼源未产出任何数据")
        return 0
    # 合并进 data/prices.json
    data = {"updated": None, "series": []}
    if DATA_PATH.exists():
        try:
            loaded = json.loads(DATA_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("series"), list):
                data = loaded
        except Exception as e:
            log(f"⚠️ 既有数据损坏，重建：{e}")
    series = data["series"]
    index = {(r.get("match"), r.get("platform"), r.get("date")): i for i, r in enumerate(series)}
    added = updated = 0
    for e in all_entries:
        key = (e["match"], e["platform"], e["date"])
        if key in index:
            series[index[key]] = e
            updated += 1
        else:
            series.append(e)
            index[key] = len(series) - 1
            added += 1
    data["updated"] = datetime.now(BJ).isoformat(timespec="seconds")
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✅ 写入 {DATA_PATH}：新增 {added} 条，覆盖 {updated} 条，总计 {len(series)} 条")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="闲鱼 Web 低频抓取器")
    ap.add_argument("--login", action="store_true", help="弹出浏览器扫码登录并保存登录态")
    ap.add_argument("--headful", action="store_true", help="有头模式抓取（调试）")
    args = ap.parse_args()
    sys.exit(do_login() if args.login else run_scrape(headful=args.headful))
