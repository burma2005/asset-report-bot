"""Lambda — 資產報告產生器

接收 HTTP POST（來自本地 index.html），流程：
  1. 解析 request body 中的 assets JSON
  2. 並行抓取各 API 即時報價
  3. Haiku 計算彙整 → Sonnet 驗證
  4. 注入 HTML 模板
  5. 回傳 HTML 字串（含 CORS headers）
"""

import os
import json
import asyncio

from price_fetcher import fetch_all_prices
from claude_agent import generate_portfolio_data

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "report_template.html")
API_KEY       = os.environ.get("API_KEY", "")

# CORS 由 Lambda Function URL 服務層處理（template.yaml FunctionUrlConfig.Cors）。
# 程式內不可再加 CORS 標頭，否則 Access-Control-Allow-Origin 重複，瀏覽器會拒收。
CORS_HEADERS: dict = {}


def _load_template() -> str:
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        return f.read()


def _build_html(portfolio_data: dict) -> str:
    template  = _load_template()
    data_json = json.dumps(portfolio_data, ensure_ascii=False)
    return template.replace("__PORTFOLIO_DATA__", data_json)


def _ok(html: str) -> dict:
    return {
        "statusCode": 200,
        "headers": {**CORS_HEADERS, "Content-Type": "text/html; charset=utf-8"},
        "body": html,
    }


def _err(msg: str, code: int = 400) -> dict:
    return {
        "statusCode": code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps({"error": msg}, ensure_ascii=False),
    }


async def run(
    assets: dict,
    retirement_goal_monthly_twd: float = 0,
    monthly_income_twd: float = 0,
    advice_model: str = "haiku",
    report_note: str = "",
) -> str:
    prices         = await fetch_all_prices(assets)
    portfolio_data = generate_portfolio_data(
        assets, prices, retirement_goal_monthly_twd, monthly_income_twd,
        advice_model)
    if report_note:
        portfolio_data["report_note"] = report_note
    return _build_html(portfolio_data)


def lambda_handler(event, _context):
    # OPTIONS preflight（CORS）
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    # API Key 驗證（標頭名稱在 API Gateway 會轉小寫）
    headers = event.get("headers") or {}
    client_key = headers.get("x-api-key", "")
    if not API_KEY or client_key != API_KEY:
        return _err("Unauthorized: invalid or missing API key", 401)

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err("Request body must be valid JSON")

    assets = body.get("assets")
    if not assets or not isinstance(assets, dict):
        return _err("Missing or invalid 'assets' field")

    if len(assets) == 0:
        return _err("Assets list is empty")

    goal   = float(body.get("retirement_goal_monthly_twd") or 0)
    income = float(body.get("monthly_income_twd") or 0)
    advice_model = body.get("advice_model", "haiku")
    if advice_model not in ("haiku", "sonnet"):
        advice_model = "haiku"
    # 選填：報告封面標註（如「虛構持倉範例」），限 40 字
    report_note = str(body.get("report_note") or "")[:40]
    html = asyncio.run(run(assets, goal, income, advice_model, report_note))
    return _ok(html)
