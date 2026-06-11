"""Bedrock 雙模型策略

Step 1：Claude Haiku — 彙整價格數據，計算各資產 TWD 市值、占比、退休試算
Step 2：Claude Sonnet — 驗證 Step 1 的 JSON，補全欄位，確保格式正確後注入模板
"""

import os
import json
import boto3
from datetime import datetime, timezone, timedelta

_bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("BEDROCK_REGION", "ap-northeast-1"))
HAIKU_MODEL  = os.environ.get("BEDROCK_HAIKU_MODEL",  "jp.anthropic.claude-haiku-4-5-20251001-v1:0")
SONNET_MODEL = os.environ.get("BEDROCK_SONNET_MODEL", "jp.anthropic.claude-sonnet-4-6")

# ── 資產分類（依用戶定義的部位結構）──
#   現金部位：鏈上現金（穩定幣）/ 鏈下現金（法幣活存）
#   加密部位：BTC/ETH 等
#   股票部位：台股 / 日股 / 美股
#   債券部位
STABLECOINS = {"RWUSD", "USDT", "USDC", "FDUSD", "DAI", "TUSD", "BUSD"}
SUFFIX_CATEGORY = {".TW": "台股", ".TWO": "台股", ".T": "日股", ".HK": "港股"}

# 子類別 → 部位群組
GROUP_MAP = {
    "鏈上現金": "現金部位", "鏈下現金": "現金部位",
    "加密部位": "加密部位",
    "台股": "股票部位", "日股": "股票部位", "美股": "股票部位", "港股": "股票部位",
    "債券": "債券部位",
}


def _guess_category(symbol: str, api: str) -> str:
    s = symbol.upper()
    if s in STABLECOINS:
        return "鏈上現金"
    if s.endswith("_CASH") or api == "cash":
        return "鏈下現金"
    for suffix, cat in SUFFIX_CATEGORY.items():
        if s.endswith(suffix):
            return cat
    if api == "binance":
        return "加密部位"
    if api == "bond":
        return "債券"
    return "美股"


def _call_bedrock(model_id: str, system: str, user: str, max_tokens: int = 4096) -> str:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    resp = _bedrock.invoke_model(
        modelId=model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(resp["body"].read())["content"][0]["text"].strip()


def _extract_json(text: str) -> dict:
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1:
        raise ValueError("回應中找不到 JSON")
    return json.loads(text[start:end])


# ── Step 1：Haiku 彙整 ───────────────────────────────

_HAIKU_SYSTEM = """你是財務數據彙整助手。根據輸入的資產持有量與報價，計算各資產的新台幣市值、佔比與退休試算。
只輸出 JSON，格式嚴格遵守範例，不要任何說明。數字四捨五入到整數。"""

def _build_asset_lines(assets: dict, prices: dict) -> list[dict]:
    """以純程式碼計算每項資產的數量、原幣報價、TWD 市值與報價來源"""
    asset_lines = []
    for sym, data in assets.items():
        api      = data.get("api", "yahoo")
        price    = prices.get("prices_twd", {}).get(sym)
        p_native = prices.get("prices_native", {}).get(sym)
        field = "shares" if "shares" in data else "amount" if "amount" in data else "face_value_usd"
        qty   = float(data.get(field, 0))
        cat   = _guess_category(sym, api)
        name  = data.get("name", sym)
        unit  = "股" if field == "shares" else sym.upper() if api == "binance" else ""

        # 原幣幣別與報價來源
        s = sym.upper()
        if api == "binance":
            currency = "USD"
            source   = "Binance"
        elif api == "yahoo":
            currency = "TWD" if s.endswith((".TW", ".TWO")) else "JPY" if s.endswith(".T") else "USD"
            user_px  = data.get("price_twd") or data.get("price_usd") or data.get("price_jpy")
            source   = "手動輸入" if user_px else "Yahoo Finance"
        elif api == "bond":
            currency = "USD"
            source   = "手動市價" if data.get("market_price_usd") else "面額計價"
        else:  # cash
            currency = data.get("currency", "TWD")
            source   = "Frankfurter匯率" if currency != "TWD" else "—"

        if api == "bond":
            # 債券的 prices_twd 已是整筆部位 TWD 價值，不可再乘面額
            value      = round(price) if price else 0
            unit_price = round(price / qty, 4) if price and qty else None
            unit       = "USD面額"
            p_native   = p_native  # 整筆部位 USD 價值
        elif api == "cash":
            value      = round(qty * price) if price else 0
            unit_price = round(price, 4) if price else None
            p_native   = 1.0       # 現金原幣單價恆為 1
        else:
            value      = round(qty * price) if price else 0
            unit_price = round(price, 4) if price else None

        # API 查無此代號 → 標示來源，報告中由警示區提醒
        if api in ("binance", "yahoo") and p_native is None:
            source = "查無報價"

        asset_lines.append({
            "symbol": sym, "name": name, "category": cat, "api": api,
            "amount": qty, "unit": unit, "field": field,
            "price_native": round(p_native, 6) if p_native is not None else None,
            "currency": currency,
            "source": source,
            "price_twd": unit_price,
            "value_twd": value,
        })
    return asset_lines


def step1_haiku_summarize(assets: dict, prices: dict) -> dict:
    """Haiku：計算每項資產市值、類別彙總、4% 退休試算"""
    asset_lines = _build_asset_lines(assets, prices)
    total_twd = sum(a["value_twd"] for a in asset_lines)

    prompt = f"""計算以下資產的佔比與退休試算，輸出 JSON。

總資產（TWD）: {total_twd}
資產列表: {json.dumps(asset_lines, ensure_ascii=False)}

輸出格式（範例）:
{{
  "generated_at": "2026-06-11 14:32",
  "total_twd": 3850000,
  "assets": [
    {{"symbol":"BTC","name":"Bitcoin","category":"加密貨幣",
      "amount":0.5,"unit":"BTC","price_twd":3150000,
      "value_twd":1575000,"pct":40.91,
      "note":"純現貨無利息"}}
  ],
  "categories": [
    {{"name":"加密貨幣","value_twd":800100,"pct":20.78}}
  ],
  "retirement": {{
    "annual_4pct_twd": 154000,
    "monthly_4pct_twd": 12833,
    "notes": "簡短分析（30字內）"
  }}
}}

note 欄位規則（每筆資產一句 8 字以內的中文說明）：
- 加密貨幣現貨 → "純現貨無利息"
- 穩定幣/鏈上現金 → "活期鎖定"
- 台幣活存 → "實體活存"
- 外幣活存 → "外幣活存"
- 美債 → "面額計價" 或 "市價計價"
- 股票/ETF → ""（空字串）

categories 必須使用以下子類別名稱（依實際持有產生）：
鏈上現金、鏈下現金、加密部位、台股、日股、美股、債券"""

    raw = _call_bedrock(HAIKU_MODEL, _HAIKU_SYSTEM, prompt, max_tokens=2048)
    return _extract_json(raw)


# 註：原 Step 2「Sonnet 數字驗證」已移除——所有數字（市值/占比/類別彙總）
# 改由 generate_portfolio_data 內純 Python 計算，更便宜也更可靠。


# ── 異常示警（確定性 Python 邏輯，不依賴 AI）─────────

def _build_alerts(assets: dict, prices: dict) -> list[dict]:
    """依官方 API 訊號產生示警：脫鉤、暴跌等"""
    alerts: list[dict] = []
    signals = prices.get("signals", {})

    for sym, sig in signals.items():
        name = assets.get(sym, {}).get("name", sym)

        # 穩定幣脫鉤偵測
        depeg = sig.get("depeg_pct")
        if depeg is not None and abs(depeg) >= 0.5:
            level = "danger" if abs(depeg) >= 3 else "warning"
            alerts.append({
                "level": level, "symbol": sym,
                "title": f"{name} 疑似脫鉤",
                "detail": f"目前與 1 USD 偏離 {depeg:+.2f}%，請確認資產安全（脫鉤/凍結/駭客事件風險）",
            })

        # 24h 暴跌偵測
        chg = sig.get("change_24h_pct")
        if chg is not None:
            api = assets.get(sym, {}).get("api", "")
            threshold_danger  = -10 if api == "binance" else -7
            threshold_warning = -5  if api == "binance" else -4
            if chg <= threshold_danger:
                alerts.append({
                    "level": "danger", "symbol": sym,
                    "title": f"{name} 24h 重挫 {chg:.1f}%",
                    "detail": "跌幅異常，請留意黑天鵝事件或市場系統性風險",
                })
            elif chg <= threshold_warning:
                alerts.append({
                    "level": "warning", "symbol": sym,
                    "title": f"{name} 24h 下跌 {chg:.1f}%",
                    "detail": "波動偏大，建議關注後續走勢",
                })

    return alerts


# ── 梗圖引擎（本地圖庫，依資產狀態挑三張：市場/配置/進度）──

import base64

MEME_DIR = os.path.join(os.path.dirname(__file__), "memes")

# mood → [(檔名, 台詞), ...]，同 mood 多張隨機挑
MEME_LIBRARY: dict[str, list[tuple[str, str]]] = {
    "depeg":     [("depeg.jpg",     "那當然是騙人的啊")],
    "crash":     [("crash1.jpg",    "（初華跪地落淚）"),
                  ("crash2.jpg",    "（小祥雨中摔倒）"),
                  ("crash3.jpg",    "不要再看了"),
                  ("crash4.jpg",    "為什麼要演奏春日影！")],
    "dip":       [("dip1.jpg",      "是又怎樣"),
                  ("dip2.jpg",      "不要")],
    "flipflop":  [("flipflop.jpg",  "昨天還很喜歡，今天就膩了")],
    "pump_big":  [("pump_big.jpg",  "太扯了吧，怎麼會這麼辣啊")],
    "pump":      [("pump.jpg",      "現在正是復權的時刻")],
    "unhealthy": [("unhealthy.jpg", "對健康不好喔")],
    "victory":   [("victory1.jpg",  "記得要強調是史上最快喔"),
                  ("victory2.jpg",  "謝謝大家"),
                  ("victory3.jpg",  "（海玲：購物袋滿載）")],
    "near_goal": [("near_goal.jpg", "那大家得多注意健康才行了")],
    "happy":     [("happy1.jpg",    "滿腦子都只想到自己呢"),
                  ("happy2.jpg",    "（愛音：比愛心）")],
    "grind":     [("grind1.jpg",    "我還是會繼續下去"),
                  ("grind2.jpg",    "人生這麼漫長，會撐不住的喔")],
    "poor":      [("poor1.jpg",     "收入實在太少了"),
                  ("poor2.jpg",     "只要是我能做的，我什麼都願意做")],
    "neutral":   [("neutral1.jpg",  "普通和理所當然什麼呢"),
                  ("neutral2.jpg",  "是這樣沒錯，但不是這樣")],
}


def _load_meme(
    mood: str, slot: str, context: str,
    used: set | None = None,
) -> dict | None:
    """slot：插入報告的段落位置（alerts/allocation/retirement/drawdown）
    context：該段落要被俏皮呼應的字眼（顯示在圖旁）
    used：同一份報告已用過的檔名集合——優先挑未用過的，避免整份報告重複同圖"""
    candidates = MEME_LIBRARY.get(mood, [])
    if not candidates:
        return None

    pool = candidates
    if used is not None:
        unused = [c for c in candidates if c[0] not in used]
        if not unused:
            # 同 mood 圖片用罄 → 從備用 mood 補（grind → neutral）
            for fb in ("grind", "neutral"):
                unused = [c for c in MEME_LIBRARY.get(fb, []) if c[0] not in used]
                if unused:
                    break
        pool = unused or candidates   # 全部用過則允許重複

    filename, caption = pool[0]       # 取序確定性：清單順序即優先序
    try:
        with open(os.path.join(MEME_DIR, filename), "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception:
        return None
    if used is not None:
        used.add(filename)
    return {"mood": mood, "slot": slot, "context": context, "caption": caption,
            "data_uri": f"data:image/jpeg;base64,{b64}"}


def _pick_memes(
    alerts: list[dict],
    progress_pct: float | None,
    signals: dict,
    crypto_pct: float,
    used: set | None = None,
) -> list[dict]:
    """依情境挑圖，每張綁定報告段落（slot）與呼應字眼（context）"""
    memes: list[dict] = []

    # 1) 風險示警區：台詞呼應警報內容
    changes = [s.get("change_24h_pct") for s in signals.values()
               if s.get("change_24h_pct") is not None]
    has_depeg = any(abs(s.get("depeg_pct", 0)) >= 0.5 for s in signals.values())
    max_up   = max(changes) if changes else 0
    max_down = min(changes) if changes else 0

    if has_depeg:
        m = _load_meme("depeg", "alerts", "穩定幣聲稱錨定 1 USD？", used)
    elif max_down <= -10:
        m = _load_meme("crash", "alerts", f"24h 最大跌幅 {max_down:.1f}%", used)
    elif max_down <= -5 and max_up >= 5:
        m = _load_meme("flipflop", "alerts", "昨漲今跌，市場變臉", used)
    elif max_down <= -5:
        m = _load_meme("dip", "alerts", f"24h 下跌 {max_down:.1f}%", used)
    elif max_up >= 10:
        m = _load_meme("pump_big", "alerts", f"24h 大漲 {max_up:.1f}%", used)
    elif max_up >= 5:
        m = _load_meme("pump", "alerts", f"24h 上漲 {max_up:.1f}%", used)
    else:
        m = None
    if m:
        memes.append(m)

    # 2) 配置分析區：加密佔比過高 →「對健康不好喔」
    if crypto_pct >= 40:
        m = _load_meme("unhealthy", "allocation",
                       f"加密部位佔 {crypto_pct:.0f}%，波動集中", used)
        if m:
            memes.append(m)

    # 3) 退休試算區：台詞呼應進度數字
    if progress_pct is None:
        prog, ctx = "neutral", "尚未設定退休目標"
    elif progress_pct >= 100:
        prog, ctx = "victory", f"達成率 {progress_pct:.0f}%，財務自由"
    elif progress_pct >= 75:
        prog, ctx = "near_goal", f"達成率 {progress_pct:.0f}%，就快到了"
    elif progress_pct >= 50:
        prog, ctx = "happy", f"達成率 {progress_pct:.0f}%，過半了"
    elif progress_pct >= 10:
        prog, ctx = "grind", f"達成率 {progress_pct:.0f}%，還在路上"
    else:
        prog, ctx = "poor", f"達成率僅 {progress_pct:.0f}%"
    m = _load_meme(prog, "retirement", ctx, used)
    if m:
        memes.append(m)

    return memes


# ── 操作建議（Sonnet 依配置與訊號生成）────────────────

_ADVICE_SYSTEM = """你是穩健的資產配置顧問。根據用戶的資產配置、市場訊號與退休進度，給出 3~5 條簡短操作建議。

規則：
- action 只能是：加碼 / 減碼 / 不動 / 觀察 / 再平衡
- target 為部位或資產名稱（如「加密部位」「台股」「0050.TW」）
- reason 為一句話理由（25 字以內），務實、不誇大、不保證報酬
- 原則：分散風險、單一部位過度集中應建議減碼、退休目標導向、長期投資
- 只輸出 JSON 陣列，不要任何說明文字

輸出格式：
[{"action":"減碼","target":"加密部位","reason":"佔比過高，波動風險集中"},
 {"action":"不動","target":"台股","reason":"配置合理，續抱即可"}]"""


def generate_recommendations(
    validated: dict,
    signals: dict,
    monthly_income_twd: float = 0,
    goal_monthly_twd: float = 0,
    advice_model: str = "haiku",
) -> list[dict]:
    income_line = (
        f"月收入 NT$ {round(monthly_income_twd):,}，月生活費 NT$ {round(goal_monthly_twd):,}，"
        f"月儲蓄力約 NT$ {round(monthly_income_twd - goal_monthly_twd):,}（可考慮定期定額投入）"
        if monthly_income_twd > 0 else
        f"已退休無工作收入，月生活費 NT$ {round(goal_monthly_twd):,}（建議應偏向防禦與現金流）"
    )

    prompt = f"""用戶資產現況：
總資產（TWD）：{validated.get('total_twd')}
四大部位：{json.dumps(validated.get('groups', []), ensure_ascii=False)}
子類別：{json.dumps(validated.get('categories', []), ensure_ascii=False)}
退休進度：{validated.get('retirement', {}).get('progress_pct', '未設定')}%
收支狀況：{income_line}
市場訊號（24h漲跌/脫鉤）：{json.dumps(signals, ensure_ascii=False)}
風險示警：{json.dumps(validated.get('alerts', []), ensure_ascii=False)}

請給出操作建議（需考量收支狀況：有儲蓄力可建議定期定額標的與金額；已退休則重視防禦）。"""

    try:
        model_id = SONNET_MODEL if advice_model == "sonnet" else HAIKU_MODEL
        raw = _call_bedrock(model_id, _ADVICE_SYSTEM, prompt, max_tokens=1024)
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start == -1:
            return []
        recs = json.loads(raw[start:end])
        # 只保留合法 action
        valid = {"加碼", "減碼", "不動", "觀察", "再平衡"}
        return [r for r in recs if r.get("action") in valid][:5]
    except Exception:
        return []


# ── 持倉相關新聞精選（Haiku 從 Yahoo 官方新聞中挑 1~3 則）──

_NEWS_SYSTEM = """你是財經新聞編輯。從給定的新聞列表中，挑出對該用戶持倉最重要的 1~3 則。

挑選原則：
- 優先：重大訊息（財報、併購、監管、駭客事件）、美聯儲利率決策、影響整體市場的總經新聞
- 排除：行情流水帳、廣告性質、與持倉無關的新聞
- title_zh 為繁體中文一句話摘要（30 字內），保留 url 原樣

只輸出 JSON 陣列，不要任何說明：
[{"title_zh":"...","source":"...","url":"...","related":"BTC"}]"""


def select_news(news_raw: list[dict]) -> list[dict]:
    if not news_raw:
        return []
    # 最多給 Haiku 20 則候選，控制 token
    candidates = news_raw[:20]
    prompt = "新聞列表：\n" + json.dumps(candidates, ensure_ascii=False, indent=1)
    try:
        raw = _call_bedrock(HAIKU_MODEL, _NEWS_SYSTEM, prompt, max_tokens=1024)
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start == -1:
            return []
        picked = json.loads(raw[start:end])
        return [n for n in picked if n.get("title_zh")][:3]
    except Exception:
        return []


# ── 花費規劃（與台灣官方統計基準比較）─────────────────

# 台灣官方統計基準（行政院主計總處，定期手動更新）
TW_BENCHMARKS = [
    {"name": "台灣官方最低生活費", "amount": 15515, "year": "2025"},
    {"name": "全國平均每人月消費支出",   "amount": 24574, "year": "2023"},
    {"name": "受僱員工月薪中位數",       "amount": 43167, "year": "2023"},
]

_SPENDING_SYSTEM = """你是務實的家庭理財顧問。根據用戶的預計每月生活費與台灣官方統計基準，給出花費評級與規劃建議。

規則：
- assessment 只能是：過於拮据 / 節儉型 / 標準型 / 寬裕型 / 高消費型
- summary 為一句話總評（35 字內），具體說明該花費水準在台灣的相對位置
- breakdown 為建議的支出分配（4~6 項，加總 = 用戶月花費），項目如：飲食、居住、交通、保險醫療、娛樂教養、雜支
- tips 為 2~4 條具體建議（各 25 字內）：若低於平均消費支出要提醒「生活需要節省」與實際可行做法；若寬裕則提醒可加大投資
- 只輸出 JSON，不要任何說明文字

輸出格式：
{"assessment":"節儉型","summary":"...",
 "breakdown":[{"item":"飲食","amount":8000},{"item":"居住","amount":6000}],
 "tips":["...","..."]}"""


def generate_spending_plan(
    goal_monthly_twd: float,
    monthly_income_twd: float = 0,
) -> dict | None:
    """依用戶月花費、月收入與台灣官方基準，生成花費規劃。未填目標回傳 None"""
    if goal_monthly_twd <= 0:
        return None

    income_line = (
        f"用戶目前月收入：NT$ {round(monthly_income_twd):,}"
        f"（月儲蓄力 = 收入 - 花費 = NT$ {round(monthly_income_twd - goal_monthly_twd):,}）"
        if monthly_income_twd > 0 else "用戶已退休，無工作收入"
    )

    prompt = f"""用戶預計每月生活費：NT$ {round(goal_monthly_twd):,}
{income_line}

台灣官方統計基準（主計總處）：
{json.dumps(TW_BENCHMARKS, ensure_ascii=False, indent=1)}

請給出花費評級、支出分配建議與規劃提示。
若有收入：評估儲蓄率（與薪資中位數比較），建議每月可投入投資的金額。
若已退休：聚焦控制花費與提領節奏。"""

    try:
        raw  = _call_bedrock(HAIKU_MODEL, _SPENDING_SYSTEM, prompt, max_tokens=1024)
        plan = _extract_json(raw)
        # 程式碼端強制補上基準數據與用戶數字（不信任模型）
        plan["monthly_twd"] = round(goal_monthly_twd)
        plan["benchmarks"]  = TW_BENCHMARKS
        # breakdown 加總若偏離用戶金額 5% 以上，按比例修正
        bd = plan.get("breakdown", [])
        bd_sum = sum(b.get("amount", 0) for b in bd)
        if bd and bd_sum > 0 and abs(bd_sum - goal_monthly_twd) / goal_monthly_twd > 0.05:
            scale = goal_monthly_twd / bd_sum
            for b in bd:
                b["amount"] = round(b["amount"] * scale)
        return plan
    except Exception:
        return None


# ── 資產耐久試算（確定性數學，首年提領後逐年通膨調整）──

INFLATION_RATE = 0.02   # 台灣長期 CPI 約 2%（主計總處）
RETURN_SCENARIOS = [
    {"label": "保守", "rate": 0.03},
    {"label": "中性", "rate": 0.05},
    {"label": "樂觀", "rate": 0.07},
]
MAX_SIM_YEARS = 60


def simulate_drawdown(total_twd: float, monthly_first_year: float) -> dict | None:
    """逐年模擬：年初提領（通膨調整）→ 剩餘資產按報酬率成長。

    回傳 scenarios（各報酬率可撐年數）與 table（中性情境逐年明細）。
    """
    if total_twd <= 0 or monthly_first_year <= 0:
        return None

    annual_w0 = monthly_first_year * 12

    def run(rate: float) -> tuple[int, list[dict]]:
        assets = total_twd
        rows = []
        for year in range(1, MAX_SIM_YEARS + 1):
            withdrawal = annual_w0 * (1 + INFLATION_RATE) ** (year - 1)
            start = assets
            if assets < withdrawal:
                rows.append({"year": year, "withdrawal": round(withdrawal),
                             "start": round(start), "end": 0})
                return year - 1, rows   # 該年度提領不足，前一年為最後完整年
            assets = (assets - withdrawal) * (1 + rate)
            rows.append({"year": year, "withdrawal": round(withdrawal),
                         "start": round(start), "end": round(assets)})
        return MAX_SIM_YEARS, rows      # 撐滿上限

    scenarios = []
    neutral_table: list[dict] = []
    for sc in RETURN_SCENARIOS:
        years, rows = run(sc["rate"])
        scenarios.append({
            "label": sc["label"],
            "return_pct": round(sc["rate"] * 100, 1),
            "years": years,
            "sustainable": years >= MAX_SIM_YEARS,
        })
        if sc["label"] == "中性":
            neutral_table = rows

    return {
        "inflation_pct": round(INFLATION_RATE * 100, 1),
        "first_year_withdrawal": round(annual_w0),
        "scenarios": scenarios,
        "table": neutral_table[:40],   # 最多顯示 40 年
        "table_scenario": "中性（年報酬 5%）",
    }


# ── 主入口 ───────────────────────────────────────────

def generate_portfolio_data(
    assets: dict,
    prices: dict,
    retirement_goal_monthly_twd: float = 0,
    monthly_income_twd: float = 0,
    advice_model: str = "haiku",
) -> dict:
    """
    回傳可注入 HTML 模板的 PORTFOLIO_DATA dict。
    advice_model：操作建議使用的模型（"haiku" 預設 / "sonnet" 品質較佳）
    """
    # Haiku 只負責 note 標註與退休短評；所有數字由下方純 Python 計算
    summary   = step1_haiku_summarize(assets, prices)
    validated = summary

    # ── 數字全部由程式碼決定，不信任模型輸出 ──

    # 真相來源：數量/原幣報價/來源/市值（API 原始數據計算）
    truth_lines = _build_asset_lines(assets, prices)
    total_twd   = sum(a["value_twd"] for a in truth_lines)
    validated["total_twd"] = round(total_twd)

    # 以 truth 為準重建 assets（只保留 Haiku 的 note 欄位）
    notes = {a.get("symbol"): a.get("note", "")
             for a in summary.get("assets", [])}
    rebuilt = []
    for t in truth_lines:
        item = dict(t)
        item["pct"]  = round(t["value_twd"] / total_twd * 100, 2) if total_twd else 0
        item["note"] = notes.get(t["symbol"], "")
        rebuilt.append(item)
    validated["assets"] = sorted(rebuilt, key=lambda a: a["value_twd"], reverse=True)

    # 子類別彙總（純 Python，由 truth 的 category 分組）
    cats: dict[str, float] = {}
    for t in truth_lines:
        cats[t["category"]] = cats.get(t["category"], 0) + t["value_twd"]
    validated["categories"] = sorted(
        [{"name": c, "value_twd": round(v),
          "pct": round(v / total_twd * 100, 2) if total_twd else 0}
         for c, v in cats.items()],
        key=lambda x: x["value_twd"], reverse=True)

    # 部位群組彙總（現金/加密/股票/債券四大部位）
    groups: dict[str, float] = {}
    for c in validated["categories"]:
        g = GROUP_MAP.get(c.get("name", ""), "其他")
        groups[g] = groups.get(g, 0) + c.get("value_twd", 0)
    validated["groups"] = sorted(
        [{"name": g, "value_twd": v,
          "pct": round(v / total_twd * 100, 2) if total_twd else 0}
         for g, v in groups.items()],
        key=lambda x: x["value_twd"], reverse=True)

    # 退休目標進度（4% 法則反推：所需總資產 = 月生活費 × 300）
    retirement = validated.setdefault("retirement", {})
    retirement["annual_4pct_twd"]  = round(total_twd * 0.04)
    retirement["monthly_4pct_twd"] = round(total_twd * 0.04 / 12)
    progress_pct = None
    if retirement_goal_monthly_twd > 0:
        target_total = retirement_goal_monthly_twd * 300
        progress_pct = round(total_twd / target_total * 100, 1)
        retirement["goal_monthly_twd"] = round(retirement_goal_monthly_twd)
        retirement["target_total_twd"] = round(target_total)
        retirement["progress_pct"]     = progress_pct

    # 異常示警（確定性邏輯）
    validated["alerts"] = _build_alerts(assets, prices)

    # 查無報價警示：用戶輸入的代號 API 找不到（打錯字或不支援），避免靜默歸零
    for t in truth_lines:
        if t["api"] in ("binance", "yahoo") and t.get("price_native") is None:
            validated["alerts"].append({
                "level": "warning", "symbol": t["symbol"],
                "title": f"{t['symbol']} 查無報價",
                "detail": ("API 查不到此代號，市值以 0 計入。請確認拼字是否正確"
                           "（加密貨幣需 Binance 有 USDT 交易對；股票需 Yahoo Finance 支援，"
                           "台股加 .TW、日股加 .T 等市場後綴）"),
            })

    # 梗圖（市場情緒 / 配置健康 / 退休進度，最多三張）
    signals = prices.get("signals", {})
    crypto_pct = next(
        (g["pct"] for g in validated["groups"] if g["name"] == "加密部位"), 0)
    used_memes: set = set()
    validated["memes"] = _pick_memes(
        validated["alerts"], progress_pct, signals, crypto_pct, used_memes)

    # 三個獨立 AI 呼叫平行執行（建議 / 新聞精選 / 花費規劃），縮短總延遲
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_recs = pool.submit(
            generate_recommendations,
            validated, signals, monthly_income_twd, retirement_goal_monthly_twd,
            advice_model)
        fut_news = pool.submit(select_news, prices.get("news_raw", []))
        fut_plan = pool.submit(
            generate_spending_plan,
            retirement_goal_monthly_twd, monthly_income_twd)
        recs      = fut_recs.result()
        news      = fut_news.result()
        plan      = fut_plan.result()
    REC_MEMES = {
        "減碼":   ("unhealthy.jpg", "對健康不好喔"),
        "加碼":   ("pump.jpg",      "現在正是復權的時刻"),
        "不動":   ("grind1.jpg",    "我還是會繼續下去"),
        "觀察":   ("neutral2.jpg",  "是這樣沒錯，但不是這樣"),
        "再平衡": ("flipflop.jpg",  "昨天還很喜歡，今天就膩了"),
    }
    seen_actions: set[str] = set()
    meme_count = 0
    for r in recs:
        action = r.get("action", "")
        if action in seen_actions or action not in REC_MEMES or meme_count >= 3:
            continue
        seen_actions.add(action)
        filename, caption = REC_MEMES[action]
        try:
            with open(os.path.join(MEME_DIR, filename), "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            r["meme"] = {"caption": caption,
                         "data_uri": f"data:image/jpeg;base64,{b64}"}
            used_memes.add(filename)
            meme_count += 1
        except Exception:
            pass
    validated["recommendations"] = recs

    # 持倉相關新聞（平行結果）
    validated["news"] = news

    # 花費規劃（平行結果，納入月收入）
    if plan is not None:
        plan["monthly_income_twd"] = round(monthly_income_twd)
    validated["spending_plan"] = plan

    # 資產耐久試算（首年提領 = 月花費×12，逐年通膨 2% 調整）
    drawdown = simulate_drawdown(total_twd, retirement_goal_monthly_twd)
    if drawdown:
        # 各情境依可撐年數配梗圖：<25年 poor / <40年 grind / <60年 happy / 永續 victory
        for sc in drawdown["scenarios"]:
            if sc["sustainable"]:
                mood = "victory"
            elif sc["years"] >= 40:
                mood = "happy"
            elif sc["years"] >= 25:
                mood = "grind"
            else:
                mood = "poor"
            m = _load_meme(mood, "drawdown", "", used_memes)
            if m:
                sc["meme"] = {"caption": m["caption"], "data_uri": m["data_uri"]}
    validated["drawdown"] = drawdown

    # 波動資產日線 K 線（近 90 日，模板渲染蠟燭圖）
    validated["klines"] = prices.get("klines", {})

    # 產生時間（台北時間）
    taipei = timezone(timedelta(hours=8))
    validated["generated_at"] = datetime.now(taipei).strftime("%Y-%m-%d %H:%M (UTC+8)")

    return validated
