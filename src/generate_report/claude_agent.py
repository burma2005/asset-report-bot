"""AI 資料彙整策略（OpenRouter 免費模型）

step1_summarize：彙整價格數據，計算各資產 TWD 市值、占比、退休試算（數字最終由純 Python 重算）。
其餘三處 AI 呼叫：操作建議、新聞精選、花費規劃。

所有 AI 呼叫經 openrouter_client.call_llm，內含三模型 fallback（429 自動換下一個）。
"""

import os
import json
from datetime import datetime, timezone, timedelta

from openrouter_client import call_llm

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
    "其他": "其他部位",
}


def _guess_category(symbol: str, api: str) -> str:
    s = symbol.upper()
    if api == "other":
        return "其他"
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
            source   = "手動輸入" if data.get("price_usd") else "Binance"
        elif api == "yahoo":
            currency = "TWD" if s.endswith((".TW", ".TWO")) else "JPY" if s.endswith(".T") else "USD"
            user_px  = data.get("price_twd") or data.get("price_usd") or data.get("price_jpy")
            source   = "手動輸入" if user_px else "Yahoo Finance"
        elif api == "bond":
            currency = "USD"
            source   = "手動市價" if data.get("market_price_usd") else "面額計價"
        elif api == "other":
            currency = data.get("currency", "TWD")
            source   = "手動輸入"
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


def step1_haiku_summarize(assets: dict, prices: dict, allow_paid: bool = True) -> dict:
    """Haiku：計算每項資產市值、類別彙總、4% 退休試算。
    免費全失敗時回傳空殼（數字由 generate_portfolio_data 純 Python 重算）。"""
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

    try:
        raw = call_llm(_HAIKU_SYSTEM, prompt, max_tokens=2048, allow_paid=allow_paid)
        return _extract_json(raw)
    except Exception:
        return {"assets": [], "categories": [], "retirement": {}}


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
    elif progress_pct >= 76:
        prog, ctx = "near_goal", f"達成率 {progress_pct:.0f}%，就快到了"
    elif progress_pct >= 26:
        prog, ctx = "grind", f"達成率 {progress_pct:.0f}%，還在路上"
    else:
        prog, ctx = "poor", f"達成率僅 {progress_pct:.0f}%"
    RETIRE_MEME_MAP = {
        "poor": ("poor2.jpg", "只要是我能做的，我什麼都願意做"),
        "grind": ("grind1.jpg", "我還是會繼續下去"),
        "near_goal": ("neutral1.jpg", "普通和理所當然什麼呢"),
        "victory": ("victory2.jpg", "謝謝大家"),
        "neutral": ("crash4.jpg", "為什麼不設定退休目標"),
    }
    retire_file, retire_caption = RETIRE_MEME_MAP.get(prog, ("neutral1.jpg", ""))
    try:
        with open(os.path.join(MEME_DIR, retire_file), "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        memes.append({
            "mood": prog, "slot": "retirement", "context": ctx,
            "caption": retire_caption,
            "data_uri": f"data:image/jpeg;base64,{b64}",
        })
        if used is not None:
            used.add(retire_file)
    except Exception:
        pass

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
    allow_paid: bool = True,
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
        raw = call_llm(_ADVICE_SYSTEM, prompt, max_tokens=1024, allow_paid=allow_paid)
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start == -1:
            return []
        recs = json.loads(raw[start:end])
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


def select_news(news_raw: list[dict], allow_paid: bool = True) -> list[dict]:
    if not news_raw:
        return []
    candidates = news_raw[:20]
    prompt = "新聞列表：\n" + json.dumps(candidates, ensure_ascii=False, indent=1)
    try:
        raw = call_llm(_NEWS_SYSTEM, prompt, max_tokens=1024, allow_paid=allow_paid)
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
    allow_paid: bool = True,
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
        raw  = call_llm(_SPENDING_SYSTEM, prompt, max_tokens=1024, allow_paid=allow_paid)
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


# ── AI 深度分析函式（5 項新增）─────────────────────────

_SUMMARY_SYSTEM = """你是一位資深財富管理顧問，正在撰寫投資組合健康檢查報告。請用繁體中文回答。
只輸出純文字段落（3~5 句），不要 JSON、不要標題、不要條列。語氣溫和但專業。"""


def generate_portfolio_summary(
    validated: dict,
    signals: dict,
    monthly_income_twd: float,
    goal_monthly_twd: float,
    allow_paid: bool = True,
) -> str:
    """AI 產出投資組合總覽摘要（3-5 句）"""
    groups_str = json.dumps(validated.get("groups", []), ensure_ascii=False)
    cats_str = json.dumps(validated.get("categories", []), ensure_ascii=False)
    retirement = validated.get("retirement", {})
    progress = retirement.get("progress_pct", "未設定")

    prompt = f"""以下是用戶的投資組合現況，請分析整體配置健康度、集中風險、區域分散性、距離退休目標的距離，以及主要隱憂。

總資產（TWD）：{validated.get('total_twd', 0):,}
四大部位分布：{groups_str}
子類別分布：{cats_str}
退休目標進度：{progress}%
月收入：NT$ {round(monthly_income_twd):,}
月生活費：NT$ {round(goal_monthly_twd):,}
市場訊號（24h漲跌）：{json.dumps(signals, ensure_ascii=False)}"""

    try:
        return call_llm(_SUMMARY_SYSTEM, prompt, max_tokens=1024, allow_paid=allow_paid).strip()
    except Exception:
        return ""


_COMMENTARY_SYSTEM = """你是投資組合分析師，為每項重要持倉提供逐項點評。請用繁體中文回答。
輸出 JSON 陣列，每個元素 {"symbol": "XXX", "commentary": "1~2 句分析"}。
重點：該資產在同類別中的集中度、24h 波動、成長/風險展望。
只輸出 JSON 陣列，不要任何說明文字。"""


def generate_asset_commentary(
    validated: dict,
    signals: dict,
    allow_paid: bool = True,
) -> list[dict]:
    """AI 產出逐資產點評"""
    top_assets = validated.get("assets", [])[:10]
    assets_info = []
    for a in top_assets:
        sig = signals.get(a["symbol"], {})
        assets_info.append({
            "symbol": a["symbol"],
            "name": a.get("name", a["symbol"]),
            "category": a.get("category", ""),
            "value_twd": a.get("value_twd", 0),
            "pct": a.get("pct", 0),
            "change_24h_pct": sig.get("change_24h_pct"),
        })

    prompt = f"""以下是用戶持倉中市值最高的資產，請逐項點評：

總資產（TWD）：{validated.get('total_twd', 0):,}
資產列表：{json.dumps(assets_info, ensure_ascii=False)}"""

    try:
        raw = call_llm(_COMMENTARY_SYSTEM, prompt, max_tokens=2048, allow_paid=allow_paid)
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start == -1:
            return []
        return json.loads(raw[start:end])
    except Exception:
        return []


_RETIREMENT_NARRATIVE_SYSTEM = """你是退休規劃顧問，請用繁體中文撰寫退休可行性分析。
只輸出純文字段落（2~3 句），不要 JSON、不要標題、不要條列。
聚焦：以目前資產與月支出，分析可持續性、最可能的提領情境、可行建議。"""


def generate_retirement_narrative(
    validated: dict,
    monthly_income_twd: float,
    goal_monthly_twd: float,
    allow_paid: bool = True,
) -> str:
    """AI 產出退休敘事分析（2-3 句），利用耐久試算數據"""
    retirement = validated.get("retirement", {})
    drawdown = validated.get("drawdown")
    drawdown_summary = ""
    if drawdown:
        for sc in drawdown.get("scenarios", []):
            drawdown_summary += f"  {sc['label']}（年報酬 {sc['return_pct']}%）：可撐 {sc['years']} 年\n"

    prompt = f"""用戶退休規劃現況：

總資產（TWD）：{validated.get('total_twd', 0):,}
每月生活費：NT$ {round(goal_monthly_twd):,}
月收入：NT$ {round(monthly_income_twd):,}
4% 法則月可提領：NT$ {retirement.get('monthly_4pct_twd', 0):,}
退休目標達成率：{retirement.get('progress_pct', '未設定')}%
耐久試算結果（通膨 2%/年）：
{drawdown_summary if drawdown_summary else '無試算數據'}

請分析此投資組合的退休可持續性、最可能出現的提領情境、以及一項可行建議。"""

    try:
        return call_llm(_RETIREMENT_NARRATIVE_SYSTEM, prompt, max_tokens=1024, allow_paid=allow_paid).strip()
    except Exception:
        return ""


_RISK_NARRATIVE_SYSTEM = """你是風險分析師，撰寫投資組合風險評估。請用繁體中文回答。
只輸出純文字段落（2~4 句），不要 JSON、不要標題、不要條列。
聚焦：資產集中風險、區域集中（美/台/日/加密）、幣別曝險、相關性風險、尾部風險。"""


def generate_risk_narrative(
    validated: dict,
    signals: dict,
    allow_paid: bool = True,
) -> str:
    """AI 產出風險敘事分析（2-4 句）"""
    groups_str = json.dumps(validated.get("groups", []), ensure_ascii=False)
    cats_str = json.dumps(validated.get("categories", []), ensure_ascii=False)
    top3 = validated.get("assets", [])[:3]
    top3_str = json.dumps(
        [{"symbol": a["symbol"], "pct": a.get("pct", 0)} for a in top3],
        ensure_ascii=False)

    prompt = f"""用戶投資組合風險概況：

總資產（TWD）：{validated.get('total_twd', 0):,}
四大部位分布：{groups_str}
子類別分布：{cats_str}
前三大持倉（佔比）：{top3_str}
市場訊號（24h漲跌/脫鉤）：{json.dumps(signals, ensure_ascii=False)}
現有警示：{json.dumps(validated.get('alerts', []), ensure_ascii=False)}

請分析組合層級的風險：資產集中、區域集中、幣別曝險、相關性風險、以及基於目前訊號的尾部風險。"""

    try:
        return call_llm(_RISK_NARRATIVE_SYSTEM, prompt, max_tokens=1024, allow_paid=allow_paid).strip()
    except Exception:
        return ""


_REALLOCATION_SYSTEM = """你是資產配置策略師。根據用戶的投資組合，為三種風險偏好提供再配置建議。請用繁體中文回答。
只輸出 JSON，不要任何說明文字。

輸出格式：
{
  "profiles": [
    {
      "name": "積極型",
      "description": "追求高報酬，承受高波動",
      "target_allocation": {"加密部位": 15, "股票部位": 55, "債券部位": 15, "現金部位": 15},
      "regional_mix": {"美國": 40, "台灣": 25, "日本": 10, "加密貨幣": 15, "其他": 10},
      "advice": "2-3 句具體建議"
    },
    {
      "name": "穩健型",
      "description": "平衡報酬與風險",
      "target_allocation": {"加密部位": 10, "股票部位": 50, "債券部位": 25, "現金部位": 15},
      "regional_mix": {"美國": 35, "台灣": 25, "日本": 15, "加密貨幣": 10, "其他": 15},
      "advice": "2-3 句具體建議"
    },
    {
      "name": "保守型",
      "description": "重視資本保全與穩定現金流",
      "target_allocation": {"加密部位": 5, "股票部位": 30, "債券部位": 40, "現金部位": 25},
      "regional_mix": {"美國": 30, "台灣": 30, "日本": 15, "加密貨幣": 5, "其他": 20},
      "advice": "2-3 句具體建議"
    }
  ],
  "current_assessment": "1-2 句評估目前配置最接近哪種風格",
  "regional_risk_note": "1-2 句關於地理集中風險"
}

target_allocation 各項加總必須 = 100，regional_mix 各項加總必須 = 100。
advice 要具體提到用戶該如何調整（增減哪些部位、增減多少百分比）。"""


def generate_reallocation_advice(
    validated: dict,
    signals: dict,
    monthly_income_twd: float,
    goal_monthly_twd: float,
    allow_paid: bool = True,
) -> dict:
    """AI 產出三種風險偏好的再配置建議"""
    groups_str = json.dumps(validated.get("groups", []), ensure_ascii=False)
    cats_str = json.dumps(validated.get("categories", []), ensure_ascii=False)
    retirement = validated.get("retirement", {})

    prompt = f"""用戶投資組合現況：

總資產（TWD）：{validated.get('total_twd', 0):,}
四大部位分布（目前）：{groups_str}
子類別分布（目前）：{cats_str}
退休目標達成率：{retirement.get('progress_pct', '未設定')}%
月收入：NT$ {round(monthly_income_twd):,}
月生活費：NT$ {round(goal_monthly_twd):,}
市場訊號：{json.dumps(signals, ensure_ascii=False)}

請針對三種風險偏好（積極/穩健/保守），各提供建議的目標配置比例與區域配置比例，
並具體說明用戶從目前配置要如何調整。同時評估目前配置最接近哪種風格。"""

    try:
        raw = call_llm(_REALLOCATION_SYSTEM, prompt, max_tokens=2048, allow_paid=allow_paid)
        return _extract_json(raw)
    except Exception:
        return {}


# ── K 線技術分析（依日線走勢給操作建議）──────────────

_KLINE_SYSTEM = """你是技術分析師，根據近 90 日的日線 K 線資料提供操作建議。請用繁體中文回答。
只輸出 JSON 陣列，不要任何說明文字。

每筆資產輸出一個物件：
[{
  "symbol": "BTC",
  "trend": "上升趨勢" | "下降趨勢" | "盤整" | "反彈中" | "回檔中",
  "support": "估計支撐價位（原幣）",
  "resistance": "估計壓力價位（原幣）",
  "signal": "偏多" | "中性" | "偏空",
  "analysis": "2~3 句技術面分析（趨勢、量能、關鍵價位、建議操作）"
}]

分析要點：
- 觀察近期高低點判斷趨勢方向
- 留意是否在支撐/壓力位附近
- 近期是否有突破或跌破關鍵價位
- 給出具體操作建議（持有/加碼/減碼/觀望）"""


def generate_kline_analysis(
    klines: dict,
    assets_info: list[dict],
    allow_paid: bool = True,
) -> list[dict]:
    """AI 依 K 線資料產出逐資產技術分析"""
    if not klines:
        return []

    kline_summary = {}
    for sym, candles in klines.items():
        if not candles:
            continue
        recent = candles[-30:] if len(candles) > 30 else candles
        closes = [c.get("c") or c.get("close", 0) for c in recent if c.get("c") or c.get("close")]
        if not closes:
            continue
        highs = [c.get("h") or c.get("high", 0) for c in recent if c.get("h") or c.get("high")]
        lows = [c.get("l") or c.get("low", 0) for c in recent if c.get("l") or c.get("low")]
        asset = next((a for a in assets_info if a["symbol"] == sym), {})
        kline_summary[sym] = {
            "name": asset.get("name", sym),
            "category": asset.get("category", ""),
            "data_points": len(candles),
            "recent_30d_high": round(max(highs), 2) if highs else None,
            "recent_30d_low": round(min(lows), 2) if lows else None,
            "latest_close": round(closes[-1], 2),
            "first_close_30d": round(closes[0], 2),
            "change_30d_pct": round((closes[-1] / closes[0] - 1) * 100, 1) if closes[0] else 0,
        }

    if not kline_summary:
        return []

    prompt = f"""以下是用戶持倉中有日線 K 線數據的資產（近 90 日），請逐項做技術分析：

{json.dumps(kline_summary, ensure_ascii=False, indent=1)}

請針對每項資產分析趨勢、支撐壓力、操作建議。"""

    try:
        raw = call_llm(_KLINE_SYSTEM, prompt, max_tokens=2048, allow_paid=allow_paid)
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start == -1:
            print(f"[kline_analysis] AI 回傳中找不到 JSON 陣列，raw[:200]={raw[:200]}")
            return []
        return json.loads(raw[start:end])
    except Exception as e:
        print(f"[kline_analysis] 例外: {e}")
        return []


# ── AI 風險警語 ──

AI_DISCLAIMER = ("⚠️ AI 分析僅供參考，不構成投資建議。所有技術分析、操作建議與配置建議均由 AI 模型生成，"
                 "可能存在錯誤或偏差。投資有風險，過往表現不代表未來收益。請依據個人風險承受能力與財務狀況，"
                 "諮詢專業理財顧問後再做決策。本報告產生者不對任何投資損失負責。")


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
    for sc in RETURN_SCENARIOS:
        years, rows = run(sc["rate"])
        scenarios.append({
            "label": sc["label"],
            "return_pct": round(sc["rate"] * 100, 1),
            "years": years,
            "sustainable": years >= MAX_SIM_YEARS,
            "table": rows[:40],   # 每個情境各自的逐年明細（最多 40 年）
        })

    return {
        "inflation_pct": round(INFLATION_RATE * 100, 1),
        "first_year_withdrawal": round(annual_w0),
        "scenarios": scenarios,
    }


# ── 主入口 ───────────────────────────────────────────

def generate_portfolio_data(
    assets: dict,
    prices: dict,
    retirement_goal_monthly_twd: float = 0,
    monthly_income_twd: float = 0,
    allow_paid: bool = True,
) -> dict:
    """回傳可注入 HTML 模板的 PORTFOLIO_DATA dict。"""
    summary   = step1_haiku_summarize(assets, prices, allow_paid=allow_paid)
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

    # 資產耐久試算（提前計算，讓退休敘事 AI 能使用結果）
    drawdown = simulate_drawdown(total_twd, retirement_goal_monthly_twd)
    if drawdown:
        # 三情境固定台詞：
        #   保守 3% →「收入實在太少了」
        #   中性 5% →「人生這麼漫長，會撐不住的喔」
        #   樂觀 7% →「那當然是騙人的啊」（7% 年報酬？）
        SCENARIO_MEMES = {
            "保守": ("poor1.jpg",  "收入實在太少了"),
            "中性": ("grind2.jpg", "人生這麼漫長，會撐不住的喔"),
            "樂觀": ("depeg.jpg",  "那當然是騙人的啊"),
        }
        for sc in drawdown["scenarios"]:
            filename, caption = SCENARIO_MEMES.get(sc["label"], (None, None))
            if not filename:
                continue
            try:
                with open(os.path.join(MEME_DIR, filename), "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                used_memes.add(filename)
                sc["meme"] = {"caption": caption,
                              "data_uri": f"data:image/jpeg;base64,{b64}"}
            except Exception:
                pass
    validated["drawdown"] = drawdown

    # 八個獨立 AI 呼叫平行執行，縮短總延遲
    klines = prices.get("klines", {})
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        fut_news = pool.submit(select_news, prices.get("news_raw", []), allow_paid)
        fut_plan = pool.submit(
            generate_spending_plan,
            retirement_goal_monthly_twd, monthly_income_twd, allow_paid)
        fut_summary = pool.submit(
            generate_portfolio_summary,
            validated, signals, monthly_income_twd, retirement_goal_monthly_twd,
            allow_paid)
        fut_commentary = pool.submit(
            generate_asset_commentary,
            validated, signals, allow_paid)
        fut_retirement = pool.submit(
            generate_retirement_narrative,
            validated, monthly_income_twd, retirement_goal_monthly_twd,
            allow_paid)
        fut_risk = pool.submit(
            generate_risk_narrative,
            validated, signals, allow_paid)
        fut_realloc = pool.submit(
            generate_reallocation_advice,
            validated, signals, monthly_income_twd, retirement_goal_monthly_twd,
            allow_paid)
        fut_kline = pool.submit(
            generate_kline_analysis,
            klines, validated.get("assets", []), allow_paid)
        news      = fut_news.result()
        plan      = fut_plan.result()
        summary_result       = fut_summary.result()
        commentary_result    = fut_commentary.result()
        retirement_narrative = fut_retirement.result()
        risk_narrative       = fut_risk.result()
        reallocation_result  = fut_realloc.result()
        kline_analysis       = fut_kline.result()
    # 持倉相關新聞（平行結果）
    validated["news"] = news

    # 花費規劃（平行結果，納入月收入）
    if plan is not None:
        plan["monthly_income_twd"] = round(monthly_income_twd)
    validated["spending_plan"] = plan

    # AI 深度分析結果（平行結果）
    validated["ai_summary"] = summary_result
    validated["asset_commentary"] = commentary_result
    validated["retirement_narrative"] = retirement_narrative
    validated["risk_narrative"] = risk_narrative
    validated["reallocation"] = reallocation_result
    validated["kline_analysis"] = kline_analysis
    validated["ai_disclaimer"] = AI_DISCLAIMER

    # 波動資產日線 K 線（近 90 日，模板渲染蠟燭圖）
    validated["klines"] = klines

    # 產生時間（台北時間）
    taipei = timezone(timedelta(hours=8))
    validated["generated_at"] = datetime.now(taipei).strftime("%Y-%m-%d %H:%M (UTC+8)")

    return validated
