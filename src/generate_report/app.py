"""Lambda — 資產報告產生器

接收 HTTP POST（來自前端），流程：
  1. 驗證 Google ID Token（JWT）
  2. DynamoDB 查用戶層級與額度
  3. 並行抓取各 API 即時報價
  4. AI 彙整 + 純 Python 計算
  5. 回傳 HTML 報告
"""

import os
import json
import asyncio
from datetime import datetime, timezone, timedelta

import boto3
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from price_fetcher import fetch_all_prices
from claude_agent import generate_portfolio_data

TEMPLATE_PATH    = os.path.join(os.path.dirname(__file__), "report_template.html")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
ADMIN_EMAIL      = os.environ.get("ADMIN_EMAIL", "")
USERS_TABLE      = os.environ.get("USERS_TABLE", "asset-report-users")

dynamodb = boto3.resource("dynamodb")
users_table = dynamodb.Table(USERS_TABLE)

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


def _current_month() -> str:
    taipei = timezone(timedelta(hours=8))
    return datetime.now(taipei).strftime("%Y-%m")


def _verify_google_token(token: str) -> dict:
    """驗證 Google ID Token，回傳 {email, name}。失敗拋 ValueError。"""
    idinfo = google_id_token.verify_oauth2_token(
        token, google_requests.Request(), GOOGLE_CLIENT_ID)
    if not idinfo.get("email_verified"):
        raise ValueError("Email 未驗證")
    return {"email": idinfo["email"], "name": idinfo.get("name", "")}


def _get_or_create_user(email: str) -> dict:
    """從 DynamoDB 取得用戶，不存在則自動建立 general 層級。"""
    if email == ADMIN_EMAIL:
        return {"email": email, "role": "admin", "monthly_limit": 999999,
                "used_this_month": 0, "reset_month": _current_month()}

    resp = users_table.get_item(Key={"email": email})
    user = resp.get("Item")
    if user:
        return user

    now = _current_month()
    user = {
        "email": email,
        "role": "general",
        "monthly_limit": 4,
        "used_this_month": 0,
        "reset_month": now,
    }
    users_table.put_item(Item=user)
    return user


def _check_and_bump_quota(user: dict) -> str | None:
    """檢查額度，通過則 +1。回傳 None 表示通過，否則回傳錯誤訊息。"""
    if user["role"] == "admin":
        return None

    email = user["email"]
    now = _current_month()

    if user.get("reset_month") != now:
        users_table.update_item(
            Key={"email": email},
            UpdateExpression="SET used_this_month = :zero, reset_month = :m",
            ExpressionAttributeValues={":zero": 0, ":m": now},
        )
        user["used_this_month"] = 0
        user["reset_month"] = now

    limit = int(user.get("monthly_limit", 4))
    used = int(user.get("used_this_month", 0))
    if used >= limit:
        return f"本月額度已用完（{used}/{limit}），下月重置"

    users_table.update_item(
        Key={"email": email},
        UpdateExpression="SET used_this_month = used_this_month + :one",
        ExpressionAttributeValues={":one": 1},
    )
    return None


def _save_portfolio(email: str, assets: dict, goal: float, income: float):
    """順手把持倉存進 DynamoDB，供階段 4 排程重用。"""
    taipei = timezone(timedelta(hours=8))
    now_str = datetime.now(taipei).isoformat()
    try:
        users_table.update_item(
            Key={"email": email},
            UpdateExpression="SET portfolio = :p, updated_at = :t",
            ExpressionAttributeValues={
                ":p": json.dumps({
                    "assets": assets,
                    "retirement_goal_monthly_twd": goal,
                    "monthly_income_twd": income,
                }, ensure_ascii=False),
                ":t": now_str,
            },
        )
    except Exception:
        pass


async def run(
    assets: dict,
    retirement_goal_monthly_twd: float = 0,
    monthly_income_twd: float = 0,
    allow_paid: bool = True,
    report_note: str = "",
) -> str:
    prices         = await fetch_all_prices(assets)
    portfolio_data = generate_portfolio_data(
        assets, prices, retirement_goal_monthly_twd, monthly_income_twd,
        allow_paid)
    if report_note:
        portfolio_data["report_note"] = report_note
    return _build_html(portfolio_data)


def lambda_handler(event, _context):
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    # ── Google ID Token 驗證 ──
    headers = event.get("headers") or {}
    auth = headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return _err("Unauthorized: missing Bearer token", 401)
    token = auth[7:]

    try:
        id_info = _verify_google_token(token)
    except Exception as e:
        return _err(f"Unauthorized: {e}", 401)

    email = id_info["email"]

    # ── 用戶層級 + 額度 ──
    user = _get_or_create_user(email)
    quota_err = _check_and_bump_quota(user)
    if quota_err:
        return _err(quota_err, 429)

    allow_paid = user["role"] in ("admin", "invited")

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
    report_note = str(body.get("report_note") or "")[:40]

    html = asyncio.run(run(assets, goal, income, allow_paid, report_note))

    _save_portfolio(email, assets, goal, income)

    return _ok(html)
