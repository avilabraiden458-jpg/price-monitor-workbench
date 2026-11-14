#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价格数据自动抓取器（本地 / GitHub Actions 共用）

数据源：
  1. Apple 官网（全新 + 官翻）—— 稳定，服务端渲染，可直接解析          [主力源]
  2. 京东 p.3.cn 当前价 —— 非官方接口，社区确认可用，随时可能失效      [增量积累]
  3. 慢慢买历史价格 —— 无官方 API，需用户提供 token，实验性，仅 --backfill [一次性回填]

输出：data/prices.json（按 match+platform+date 幂等合并，累积追加）
工作台「数据管理 → ☁️ 自动同步」按 match（本地商品名）拉取合并。

用法：
  python sync/fetch_prices.py               # 日常快照（Apple + 京东）
  python sync/fetch_prices.py --backfill    # 额外尝试慢慢买历史回填（实验性）
  python sync/fetch_prices.py --out x.json  # 指定输出文件
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SKUS_PATH = ROOT / "sync" / "skus.json"
DATA_PATH = ROOT / "data" / "prices.json"
MMB_TOKEN_PATH = ROOT / "sync" / "manmanbuy_token.txt"

# 北京时间（无夏令时，固定 +8）
BJ = timezone(timedelta(hours=8))
today_bj = lambda: datetime.now(BJ).strftime("%Y-%m-%d")
now_bj_iso = lambda: datetime.now(BJ).isoformat(timespec="seconds")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

APPLE_PAGES = [
    ("apple_refurb", "https://www.apple.com.cn/shop/refurbished/mac/macbook-pro"),
    ("apple_new",    "https://www.apple.com.cn/shop/buy-mac/macbook-pro"),
]

PLATFORM_APPLE_NEW = "apple_new"
PLATFORM_APPLE_REFURB = "apple_refurb"
PLATFORM_JD = "jd"


def log(msg):
    print(f"[{datetime.now(BJ).strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get(url, timeout=25, headers=None):
    """GET，返回文本；失败返回 None（抓取失败安全跳过，不阻塞其他源）"""
    try:
        req = urllib.request.Request(url, headers={**HEADERS, **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except Exception as e:
        log(f"⚠️ GET 失败 {url} → {type(e).__name__}: {e}")
        return None


def http_post(url, form, timeout=25):
    """POST 表单，返回文本；失败返回 None"""
    try:
        data = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={**HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://app.manmanbuy.com/",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except Exception as e:
        log(f"⚠️ POST 失败 {url} → {type(e).__name__}: {e}")
        return None


# ============================================================
# 源 1：Apple 官网（服务端渲染，纯 HTML 可解析）
# ============================================================
PRICE_LINE = re.compile(r"^(?:RMB|￥|¥)\s*([0-9][0-9,]*)\s*$")
SIZE_RE = re.compile(r"(\d+)\s*英寸")
CHIP_RE = re.compile(r"M\d+(?:\s*(?:Pro|Max))?(?=\s*芯片)")
CORES_RE = re.compile(r"(\d+)\s*核中央处理器")


def html_to_lines(html):
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "\n", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"<[^>]+>", "\n", text)
    import html as _h
    text = _h.unescape(text)
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def extract_macbook_pro_items(lines):
    """行配对：最近一条含 'MacBook Pro' 的标题行 × 紧随的 RMB 价格行"""
    items, last_title = [], None
    for ln in lines:
        if ("MacBook Pro" in ln) and ("英寸" in ln) and ("芯片" in ln):
            last_title = ln
        m = PRICE_LINE.match(ln)
        if m and last_title:
            items.append({"title": last_title, "price": int(m.group(1).replace(",", ""))})
            last_title = None
    return items


def parse_size_chip(title):
    ms = SIZE_RE.search(title)
    mc = CHIP_RE.search(title)
    if not ms or not mc:
        return None, None
    size = int(ms.group(1))
    chip = re.sub(r"\s+", " ", mc.group(0)).strip()  # "M4 Pro" / "M4" / "M4 Max"
    return size, chip


def parse_cores(title):
    """CPU 核数：同芯片同尺寸有多个配置档（如 M4 Pro 12核 vs 14核），必须区分"""
    mc = CORES_RE.search(title)
    return int(mc.group(1)) if mc else None


def fetch_apple(products):
    """抓取 Apple 页面，按 (尺寸, Pro芯片, CPU核数) 取最低价映射到本地商品。

    核数校验：官翻同芯片同尺寸常有多档配置（如 M4 Pro 12核/14核），
    skus.json 中定义了 cpu_cores 的商品只接受对应核数的报价，
    避免高配价格错记到入门款 SKU 上；未定义核数则取该组合最低价。
    """
    raw_items, entries = [], []
    for platform, url in APPLE_PAGES:
        html = http_get(url)
        if not html:
            log(f"⏭️ Apple 源跳过：{url}")
            continue
        items = extract_macbook_pro_items(html_to_lines(html))
        raw_items.extend((platform, it) for it in items)
        log(f"Apple {platform}: 解析出 {len(items)} 个报价条目")
    if not raw_items:
        log("⚠️ Apple 源未解析到任何条目（页面结构可能变更），跳过")
        return entries

    # (platform, size, chip, cores) → 最低价条目
    best = {}
    for platform, it in raw_items:
        size, chip = parse_size_chip(it["title"])
        if not size or not chip or not chip.endswith("Pro"):
            continue  # 只关心 Pro 芯片款；M4/M5 基础款与 Max 款无对应监控商品
        cores = parse_cores(it["title"])
        key = (platform, size, chip, cores)
        if key not in best or it["price"] < best[key]["price"]:
            best[key] = it

    # 映射到本地商品
    matched = 0
    for (platform, size, chip, cores), it in best.items():
        prod = next((p for p in products
                     if p.get("size") == size and p.get("chip") == chip
                     and (p.get("cpu_cores") in (None, cores))), None)
        if not prod:
            continue
        entries.append({
            "match": prod["name"], "platform": platform, "date": today_bj(),
            "price": it["price"], "title": it["title"], "source": "apple",
        })
        matched += 1
    log(f"Apple 源产出 {matched} 条（Pro 款 × 配置校验后）")
    return entries


# ============================================================
# 源 2：京东 p.3.cn 当前价（非官方接口，随时可能失效）
# ============================================================
def fetch_jd(products):
    entries = []
    targets = [p for p in products if p.get("jd_sku")]
    if not targets:
        log("⏭️ 京东源跳过：skus.json 中未配置任何 jd_sku（在 item.jd.com 商品页 URL 中找到数字 ID 后填入）")
        return entries
    sku_ids = [str(p["jd_sku"]) for p in targets]
    url = "http://p.3.cn/prices/mgets?skuIds=" + ",".join("J_" + s for s in sku_ids)
    body = http_get(url, timeout=20)
    if not body:
        log("⚠️ 京东源请求失败（接口可能失效或被风控），跳过")
        return entries
    try:
        arr = json.loads(body)
    except Exception as e:
        log(f"⚠️ 京东源返回非 JSON：{e}，跳过")
        return entries
    price_by_id = {}
    for row in arr if isinstance(arr, list) else []:
        try:
            pid = str(row.get("id", "")).removeprefix("J_")
            p = float(row.get("p") or 0)
            if pid and p > 0:
                price_by_id[pid] = p
        except Exception:
            continue
    for p in targets:
        v = price_by_id.get(str(p["jd_sku"]))
        if not v:
            continue
        entries.append({
            "match": p["name"], "platform": PLATFORM_JD, "date": today_bj(),
            "price": int(v), "title": f"京东 SKU {p['jd_sku']}（p.3.cn）", "source": "p3cn",
        })
    log(f"京东源产出 {len(entries)} 条")
    return entries


# ============================================================
# 源 3（实验性）：慢慢买历史价格回填
# 说明：慢慢买无官方 API。社区方案（github.com/PPsteven/manmanbuy_js_crack
#       、github.com/DaizeDong/shopping-aggregator）需要逆向签名或抓包 token。
# 本模块采用「用户自供 token」策略：从慢慢买 App/网页抓包获取 token 后存入
# sync/manmanbuy_token.txt（一行一个 token，格式见 README）。接口参数或签名
# 变更会导致失败，失败安全跳过，不影响主流程。
# ============================================================
MMB_API = "https://apapia-history.manmanbuy.com/ChronicleService/getHistoryTrend.aspx"


def fetch_manmanbuy(products):
    entries = []
    token = ""
    if MMB_TOKEN_PATH.exists():
        token = MMB_TOKEN_PATH.read_text(encoding="utf-8").strip()
    if not token:
        log("⏭️ 慢慢买回填跳过：未配置 sync/manmanbuy_token.txt（获取方式见 README「慢慢买实验源」）")
        return entries
    targets = [p for p in products if p.get("chip", "").endswith("Pro")]
    for p in targets:
        form = {
            "methodName": "getHistoryTrend",
            "p_zid": "-1", "p_yhid": "0", "p_mobile": "",
            "token": token,
            "p_keyword": p["name"],
            "appid": "7",
        }
        body = http_post(MMB_API, form, timeout=30)
        if not body:
            continue
        try:
            obj = json.loads(body)
        except Exception:
            log(f"⚠️ 慢慢买返回非 JSON（token 可能失效或参数变更）：{body[:120]}")
            continue
        # 返回结构未知度较高：尝试常见的趋势字段
        trend = obj.get("chartPrient") or obj.get("trend") or []
        n = 0
        if isinstance(trend, list):
            for point in trend:
                try:
                    d = str(point.get("date") or point.get("time") or "")[:10]
                    v = float(point.get("price") or point.get("value") or 0)
                    if re.match(r"^\d{4}-\d{2}-\d{2}$", d) and v > 0:
                        entries.append({
                            "match": p["name"], "platform": PLATFORM_JD, "date": d,
                            "price": int(v), "title": "慢慢买历史回填", "source": "manmanbuy",
                        })
                        n += 1
                except Exception:
                    continue
        log(f"慢慢买「{p['name']}」回填 {n} 条")
    log(f"慢慢买回填共产出 {len(entries)} 条（实验性源，失败不影响主流程）")
    return entries


# ============================================================
# 合并写入
# ============================================================
def load_data():
    if DATA_PATH.exists():
        try:
            obj = json.loads(DATA_PATH.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and isinstance(obj.get("series"), list):
                return obj
        except Exception as e:
            log(f"⚠️ 既有数据文件损坏，将重建：{e}")
    return {"updated": None, "series": []}


def merge(data, entries):
    series = data["series"]
    index = {(r.get("match"), r.get("platform"), r.get("date")): i for i, r in enumerate(series)}
    added = updated = 0
    for e in entries:
        key = (e["match"], e["platform"], e["date"])
        if key in index:
            series[index[key]] = e
            updated += 1
        else:
            series.append(e)
            index[key] = len(series) - 1
            added += 1
    return added, updated


def main():
    ap = argparse.ArgumentParser(description="价格数据自动抓取器")
    ap.add_argument("--backfill", action="store_true", help="额外运行慢慢买历史回填（实验性）")
    ap.add_argument("--out", default=str(DATA_PATH), help="输出文件路径")
    args = ap.parse_args()

    if not SKUS_PATH.exists():
        log(f"❌ 未找到 {SKUS_PATH}")
        return 1
    cfg = json.loads(SKUS_PATH.read_text(encoding="utf-8"))
    products = cfg.get("products", [])
    log(f"载入 {len(products)} 个监控商品定义")

    all_entries = []
    all_entries += fetch_apple(products)
    all_entries += fetch_jd(products)
    if args.backfill:
        all_entries += fetch_manmanbuy(products)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = load_data() if out_path == DATA_PATH else {"updated": None, "series": []}
    added, updated = merge(data, all_entries)
    data["updated"] = now_bj_iso()
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"✅ 写入 {out_path}：新增 {added} 条，覆盖 {updated} 条，总计 {len(data['series'])} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
