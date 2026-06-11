"""並行抓取各資產即時報價（純 Python，不使用 AI）

支援 API：
  - Binance REST：加密貨幣
  - Yahoo Finance（yfinance）：台股、美股、日股
  - Frankfurter API：匯率（USD→TWD、JPY→TWD）
  - 美債：使用者輸入市價（選填），未填以面額計算
"""

import asyncio
import httpx
from datetime import datetime, timezone


# ── 匯率（Frankfurter，歐洲央行官方）────────────────────

async def fetch_fx_rates(client: httpx.AsyncClient) -> dict[str, float]:
    resp = await client.get(
        "https://api.frankfurter.app/latest",
        params={"from": "TWD", "to": "USD,JPY"},
        timeout=8,
    )
    resp.raise_for_status()
    rates = resp.json()["rates"]
    return {
        "USD_TWD": 1.0 / rates["USD"],
        "JPY_TWD": 1.0 / rates["JPY"],
    }


# ── 加密貨幣（Binance 24hr ticker：含現價與 24h 漲跌%）──

async def fetch_binance_prices(
    client: httpx.AsyncClient,
    symbols: list[str],
) -> dict[str, dict | None]:
    tasks = [
        client.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": f"{sym.upper()}USDT"},
            timeout=8,
        )
        for sym in symbols
    ]
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    results: dict[str, dict | None] = {}
    for sym, resp in zip(symbols, responses):
        try:
            resp.raise_for_status()
            j = resp.json()
            results[sym] = {
                "price": float(j["lastPrice"]),
                "change_24h_pct": float(j["priceChangePercent"]),
            }
        except Exception:
            results[sym] = None
    return results


# ── 股票（Yahoo Finance chart API，純 httpx 不依賴 yfinance）──

async def _fetch_yahoo_one(client: httpx.AsyncClient, symbol: str) -> dict | None:
    try:
        resp = await client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}",
            params={"range": "1d", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        resp.raise_for_status()
        meta  = resp.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        if not price:
            return None
        prev   = meta.get("chartPreviousClose") or meta.get("previousClose")
        change = (float(price) / float(prev) - 1) * 100 if prev else None
        return {"price": float(price), "change_24h_pct": change}
    except Exception:
        return None


async def fetch_yahoo_prices(
    client: httpx.AsyncClient,
    symbols: list[str],
) -> dict[str, float | None]:
    if not symbols:
        return {}
    results = await asyncio.gather(*(_fetch_yahoo_one(client, s) for s in symbols))
    return dict(zip(symbols, results))


# ── 日線 K 線（近 90 日 OHLC，嵌入報告蠟燭圖）───────────

STABLE_USD = {"RWUSD", "USDT", "USDC", "FDUSD", "DAI", "TUSD"}


async def _fetch_binance_klines(client: httpx.AsyncClient, sym: str) -> list | None:
    try:
        resp = await client.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": f"{sym.upper()}USDT", "interval": "1d", "limit": 90},
            timeout=8,
        )
        resp.raise_for_status()
        # [openTime, open, high, low, close, ...] → {t,o,h,l,c}
        return [
            {"t": k[0], "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4])}
            for k in resp.json()
        ]
    except Exception:
        return None


async def _fetch_yahoo_klines(client: httpx.AsyncClient, sym: str) -> list | None:
    try:
        resp = await client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{sym.upper()}",
            params={"range": "3mo", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        ts = result["timestamp"]
        q  = result["indicators"]["quote"][0]
        out = []
        for i, t in enumerate(ts):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c):
                continue
            out.append({"t": t * 1000, "o": round(o, 4), "h": round(h, 4),
                        "l": round(l, 4), "c": round(c, 4)})
        return out or None
    except Exception:
        return None


async def fetch_klines(client: httpx.AsyncClient, assets: dict) -> dict:
    """波動資產（加密非穩定幣 + 股票）的近 90 日日線，現金/債券/穩定幣跳過"""
    tasks: dict[str, object] = {}
    for sym, d in assets.items():
        api = d.get("api", "")
        if api == "binance" and sym.upper() not in STABLE_USD:
            tasks[sym] = _fetch_binance_klines(client, sym)
        elif api == "yahoo":
            tasks[sym] = _fetch_yahoo_klines(client, sym)
    if not tasks:
        return {}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    return {
        sym: r for sym, r in zip(tasks.keys(), results)
        if r and not isinstance(r, Exception)
    }


# ── 持倉相關新聞（Yahoo Finance 官方搜尋/新聞 API）──────

async def _fetch_news_one(client: httpx.AsyncClient, query: str) -> list[dict]:
    try:
        resp = await client.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": query, "newsCount": 5, "quotesCount": 0},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        resp.raise_for_status()
        items = resp.json().get("news", [])
        out = []
        for n in items:
            ts = n.get("providerPublishTime")
            out.append({
                "query":     query,
                "title":     n.get("title", ""),
                "publisher": n.get("publisher", ""),
                "link":      n.get("link", ""),
                "published": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else "",
            })
        return out
    except Exception:
        return []


async def fetch_news(client: httpx.AsyncClient, assets: dict) -> list[dict]:
    """為每個持倉組查詢字串 + 總經（Fed），抓官方新聞列表"""
    queries: list[str] = []
    for sym, d in assets.items():
        api = d.get("api", "")
        if api == "binance":
            s = sym.upper()
            if s not in ("RWUSD",):                  # 理財產品 Yahoo 查不到
                queries.append(f"{s}-USD")
        elif api == "yahoo":
            queries.append(sym.upper())
        elif api == "bond":
            pass                                       # 由總經 query 涵蓋
    queries.append("Federal Reserve")                  # 美聯儲升降息等總經訊息

    results = await asyncio.gather(*(_fetch_news_one(client, q) for q in queries))
    flat: list[dict] = []
    seen_titles: set[str] = set()
    for lst in results:
        for n in lst:
            if n["title"] and n["title"] not in seen_titles:
                seen_titles.add(n["title"])
                flat.append(n)
    return flat


# ── 主入口 ───────────────────────────────────────────────

async def fetch_all_prices(assets: dict) -> dict:
    """
    並行抓取所有資產即時報價，回傳 prices dict。

    assets 格式（來自 HTTP POST body）:
    {
      "BTC":     {"api": "binance", "amount": 0.5},
      "0050.TW": {"api": "yahoo",   "shares": 1000, "price_twd": 185.5},
      "VOO":     {"api": "yahoo",   "shares": 10,   "price_usd": 535.0},
      "1306.T":  {"api": "yahoo",   "shares": 100,  "price_jpy": 2345},
      "US30Y":   {"api": "bond",    "face_value_usd": 10000, "market_price_usd": 9500},
      "TWD_CASH":{"api": "cash",    "currency": "TWD", "amount": 100000},
    }
    """
    binance_syms = [s for s, d in assets.items() if d.get("api") == "binance"]
    yahoo_syms   = [s for s, d in assets.items() if d.get("api") == "yahoo"]

    async with httpx.AsyncClient() as client:
        fx, binance_px, yahoo_px, news_raw, klines = await asyncio.gather(
            fetch_fx_rates(client),
            fetch_binance_prices(client, binance_syms) if binance_syms else _empty(),
            fetch_yahoo_prices(client, yahoo_syms),
            fetch_news(client, assets),
            fetch_klines(client, assets),
            return_exceptions=True,
        )
    if isinstance(news_raw, Exception):
        news_raw = []
    if isinstance(klines, Exception):
        klines = {}

    usd_twd = fx["USD_TWD"] if not isinstance(fx, Exception) else 32.0
    jpy_twd = fx["JPY_TWD"] if not isinstance(fx, Exception) else 0.22

    prices_native: dict[str, float | None] = {}
    prices_twd:    dict[str, float | None] = {}
    signals:       dict[str, dict]         = {}   # 異常偵測訊號

    # 加密貨幣（USD 計價）；穩定幣/理財產品 Binance 無交易對時視為 1 USD
    STABLE_USD = {"RWUSD", "USDT", "USDC", "FDUSD", "DAI", "TUSD"}
    for sym in binance_syms:
        info = binance_px.get(sym) if not isinstance(binance_px, Exception) else None
        is_stable = sym.upper() in STABLE_USD
        if info is None:
            p = 1.0 if is_stable else None
            chg = None
        else:
            p   = info["price"]
            chg = info["change_24h_pct"]
        prices_native[sym] = p
        prices_twd[sym]    = p * usd_twd if p else None
        sig: dict = {}
        if chg is not None:
            sig["change_24h_pct"] = round(chg, 2)
        if is_stable and p is not None:
            sig["depeg_pct"] = round((p - 1.0) * 100, 3)   # 與 1 USD 的偏離%
        if sig:
            signals[sym] = sig

    # 股票：優先用使用者輸入的現價，否則用 Yahoo API
    for sym in yahoo_syms:
        data = assets[sym]
        yinfo = yahoo_px.get(sym) if not isinstance(yahoo_px, Exception) else None
        if yinfo and yinfo.get("change_24h_pct") is not None:
            signals[sym] = {"change_24h_pct": round(yinfo["change_24h_pct"], 2)}

        if data.get("price_twd"):
            p_twd = float(data["price_twd"])
            prices_native[sym] = p_twd
            prices_twd[sym]    = p_twd
        elif data.get("price_usd"):
            p_usd = float(data["price_usd"])
            prices_native[sym] = p_usd
            prices_twd[sym]    = p_usd * usd_twd
        elif data.get("price_jpy"):
            p_jpy = float(data["price_jpy"])
            prices_native[sym] = p_jpy
            prices_twd[sym]    = p_jpy * jpy_twd
        else:
            # Yahoo API 查詢；依市場後綴決定原幣別
            p = yinfo["price"] if yinfo else None
            prices_native[sym] = p
            s = sym.upper()
            if p is None:
                prices_twd[sym] = None
            elif s.endswith(".TW") or s.endswith(".TWO"):   # 台股：已是 TWD
                prices_twd[sym] = p
            elif s.endswith(".T"):                            # 日股：JPY
                prices_twd[sym] = p * jpy_twd
            else:                                             # 美股等：USD
                prices_twd[sym] = p * usd_twd

    # 美債：用戶輸入市價，否則以面額計算
    for sym, data in assets.items():
        if data.get("api") != "bond":
            continue
        face  = float(data.get("face_value_usd", 10000))
        mkt   = data.get("market_price_usd")
        p_usd = float(mkt) if mkt else face
        prices_native[sym] = p_usd
        prices_twd[sym]    = p_usd * usd_twd

    # 現金
    for sym, data in assets.items():
        if data.get("api") != "cash":
            continue
        ccy  = data.get("currency", "TWD")
        rate = {"TWD": 1.0, "USD": usd_twd, "JPY": jpy_twd}.get(ccy, 1.0)
        prices_native[sym] = rate
        prices_twd[sym]    = rate

    return {
        "prices_native": prices_native,
        "prices_twd":    prices_twd,
        "fx":            {"USD_TWD": usd_twd, "JPY_TWD": jpy_twd},
        "signals":       signals,
        "news_raw":      news_raw,
        "klines":        klines,
        "fetched_at":    datetime.now(timezone.utc).isoformat(),
    }


async def _empty() -> dict:
    return {}
