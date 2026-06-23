"""Lambda — 資產報告產生器

接收 HTTP POST（來自前端），流程：
  1. 驗證 Google ID Token（JWT）
  2. DynamoDB 查用戶層級與額度
  3. 並行抓取各 API 即時報價
  4. AI 彙整 + 純 Python 計算
  5. 回傳 HTML 報告
"""

import os
import time
import math
import json
import asyncio
from datetime import datetime, timezone, timedelta

import hashlib
import boto3
from botocore.exceptions import ClientError
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from price_fetcher import fetch_all_prices
from claude_agent import generate_portfolio_data

TEMPLATE_PATH    = os.path.join(os.path.dirname(__file__), "report_template.html")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
ADMIN_EMAIL      = os.environ.get("ADMIN_EMAIL", "")
USERS_TABLE      = os.environ.get("USERS_TABLE", "asset-report-users")
REPORT_BUCKET    = os.environ.get("REPORT_BUCKET", "")
MAX_REPORTS_PER_USER = 2
MAX_ASSETS = 60                  # 單次請求資產筆數上限（防成本放大 / DoS）
# 量級上限：核心防護是擋 NaN/Inf（math.isfinite），這個只是軟性防呆，擋明顯的垃圾/攻擊值。
# 設 1e18 而非更小，是為了不誤殺高供應量代幣的合法持有數量（單價極小、總值正常）。
MAX_MONEY_TWD = 1e18


def _safe_float(val, default: float = 0.0, allow_negative: bool = False) -> float:
    """安全解析使用者輸入的數字：拒絕 NaN/Inf/離譜量級。

    allow_negative=False 時負值歸 default（如生活費不可能為負）；
    allow_negative=True 時保留負值（如月收入可為淨負現金流：房貸>收入）。
    """
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f) or abs(f) > MAX_MONEY_TWD:
        return default
    if f < 0 and not allow_negative:
        return default
    return f


# 資產中所有數值欄位（須過濾 NaN/Inf/負值/離譜值，防 Lambda 計算崩潰 / DoS）
_ASSET_NUMERIC_FIELDS = (
    "amount", "shares", "price", "price_usd", "price_twd", "price_jpy",
    "face_value_usd", "market_price_usd",
)


def _sanitize_assets(assets: dict) -> dict:
    """逐筆資產淨化：數值欄位拒絕 NaN/Inf/負/離譜（→None），字串欄位截長。

    json.loads 預設會把 "1e999" 解析成 inf、接受 NaN，故需在入口攔截，
    避免污染後續市值/退休/耐久試算或讓 Lambda 崩潰。
    """
    clean: dict = {}
    for sym, a in list(assets.items())[:MAX_ASSETS]:
        if not isinstance(a, dict):
            continue
        item: dict = {}
        for k, v in a.items():
            if k in _ASSET_NUMERIC_FIELDS:
                if v is None:
                    item[k] = None
                    continue
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    item[k] = None
                    continue
                # 允許負值（負現金/負債/做空部位皆合理）；只擋 NaN/Inf 與離譜量級
                item[k] = f if (math.isfinite(f) and abs(f) <= MAX_MONEY_TWD) else None
            elif isinstance(v, str):
                item[k] = v[:120]
            else:
                item[k] = v
        clean[str(sym)[:60]] = item
    return clean

dynamodb = boto3.resource("dynamodb")
users_table = dynamodb.Table(USERS_TABLE)
s3 = boto3.client("s3")

CORS_HEADERS: dict = {}


def _load_template() -> str:
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        return f.read()


def _build_html(portfolio_data: dict) -> str:
    template  = _load_template()
    data_json = json.dumps(portfolio_data, ensure_ascii=False)
    # 防止使用者輸入的字串（如資產名稱含 </script>）跳脫出 <script> 區塊造成 XSS。
    # 將 < > & 轉成等價的 JSON unicode 跳脫，仍是合法 JSON，但無法在 HTML 解析層提早收尾。
    data_json = (data_json.replace("<", "\\u003c")
                          .replace(">", "\\u003e")
                          .replace("&", "\\u0026"))
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
        "monthly_limit": 12,
        "used_this_month": 0,
        "reset_month": now,
    }
    users_table.put_item(Item=user)
    return user


def _check_and_bump_quota(user: dict) -> str | None:
    """原子檢查並 +1 額度。回傳 None 表示通過，否則回傳錯誤訊息。

    用 DynamoDB ConditionExpression 一次完成「檢查 + 遞增」，避免併發請求各自
    讀到舊值同時通過檢查造成超額（race condition）。
    """
    if user["role"] == "admin":
        return None

    email = user["email"]
    now = _current_month()
    limit = int(user.get("monthly_limit", 12))

    # 跨月重置（冪等：條件為 reset_month 不等於本月才歸零）
    if user.get("reset_month") != now:
        try:
            users_table.update_item(
                Key={"email": email},
                UpdateExpression="SET used_this_month = :zero, reset_month = :m",
                ConditionExpression="reset_month <> :m",
                ExpressionAttributeValues={":zero": 0, ":m": now},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise  # 其他人已重置，忽略

    # 原子遞增：僅當 used_this_month < limit（或屬性不存在）才 +1
    try:
        users_table.update_item(
            Key={"email": email},
            UpdateExpression="SET used_this_month = if_not_exists(used_this_month, :zero) + :one",
            ConditionExpression="attribute_not_exists(used_this_month) OR used_this_month < :limit",
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":limit": limit},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return f"本月額度已用完（{limit}/{limit}），下月重置"
        raise
    return None


SHARE_EXPIRY = 86400   # 分享連結效期（秒）= 24 小時（使用者主動分享才產生）
SHARE_COOLDOWN = 60    # 每帳號產生分享連結的冷卻秒數（防亂按）


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()[:12]


def _owns_key(email: str, key: str) -> bool:
    """驗證報告物件 key 屬於此使用者（防 IDOR 讀別人報告）。"""
    if not isinstance(key, str) or ".." in key:
        return False
    return key.startswith(f"reports/{_email_hash(email)}/") and key.endswith(".html")


def _share_url(key: str) -> str:
    """為報告物件簽發 24h 分享用 presigned URL。"""
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": REPORT_BUCKET, "Key": key},
        ExpiresIn=SHARE_EXPIRY,
    )


def _new_report_key(email: str) -> str:
    taipei = timezone(timedelta(hours=8))
    ts = datetime.now(taipei).strftime("%Y%m%d-%H%M%S")
    return f"reports/{_email_hash(email)}/{ts}.html"


def _put_report(email: str, key: str, html: str):
    """上傳報告 HTML 到私有 S3，並只保留最新 2 份。"""
    s3.put_object(
        Bucket=REPORT_BUCKET, Key=key,
        Body=html.encode("utf-8"),
        ContentType="text/html; charset=utf-8",
    )
    prefix = f"reports/{_email_hash(email)}/"
    resp = s3.list_objects_v2(Bucket=REPORT_BUCKET, Prefix=prefix)
    objects = sorted(resp.get("Contents", []), key=lambda o: o["Key"], reverse=True)
    for old in objects[MAX_REPORTS_PER_USER:]:
        s3.delete_object(Bucket=REPORT_BUCKET, Key=old["Key"])


def _self_endpoint(event: dict) -> str:
    """從請求推導本 Lambda Function URL（供報告內的分享按鈕回呼）。"""
    dn = event.get("requestContext", {}).get("domainName", "")
    return f"https://{dn}/" if dn else ""


def _get_previous_summary(email: str) -> dict | None:
    """從 DynamoDB 讀取上次報告摘要。"""
    try:
        resp = users_table.get_item(Key={"email": email})
        raw = resp.get("Item", {}).get("report_summary")
        if not raw:
            return None
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None


def _list_reports(email: str) -> list[dict]:
    """列出使用者報告的 metadata（key + 產生時間，最新在前）。不簽 URL。"""
    if not REPORT_BUCKET:
        return []
    try:
        prefix = f"reports/{_email_hash(email)}/"
        resp = s3.list_objects_v2(Bucket=REPORT_BUCKET, Prefix=prefix)
        objects = sorted(resp.get("Contents", []), key=lambda o: o["Key"], reverse=True)
        out = []
        for o in objects:
            # key 形如 reports/<hash>/YYYYMMDD-HHMMSS.html
            stamp = o["Key"].rsplit("/", 1)[-1].replace(".html", "")
            out.append({"key": o["Key"], "stamp": stamp})
        return out
    except Exception:
        return []


def _save_portfolio(email: str, assets: dict, goal: float, income: float,
                    portfolio_data: dict | None = None):
    """把持倉與報告摘要存進 DynamoDB。"""
    taipei = timezone(timedelta(hours=8))
    now_str = datetime.now(taipei).isoformat()
    try:
        update_expr = "SET portfolio = :p, updated_at = :t"
        expr_values = {
            ":p": json.dumps({
                "assets": assets,
                "retirement_goal_monthly_twd": goal,
                "monthly_income_twd": income,
            }, ensure_ascii=False),
            ":t": now_str,
        }
        if portfolio_data:
            summary = {
                "generated_at": portfolio_data.get("generated_at", now_str),
                "total_twd": portfolio_data.get("total_twd", 0),
                "categories": portfolio_data.get("categories", []),
                "groups": portfolio_data.get("groups", []),
                "assets": [
                    {"symbol": a["symbol"], "name": a.get("name", ""),
                     "value_twd": a.get("value_twd", 0), "pct": a.get("pct", 0),
                     "category": a.get("category", "")}
                    for a in portfolio_data.get("assets", [])
                ],
                "retirement": portfolio_data.get("retirement", {}),
            }
            update_expr += ", report_summary = :s"
            expr_values[":s"] = json.dumps(summary, ensure_ascii=False)
        users_table.update_item(
            Key={"email": email},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )
    except Exception:
        pass


async def run(
    assets: dict,
    retirement_goal_monthly_twd: float = 0,
    monthly_income_twd: float = 0,
    allow_paid: bool = True,
    report_note: str = "",
    prev_summary: dict | None = None,
) -> tuple[str, dict]:
    prices         = await fetch_all_prices(assets)
    portfolio_data = generate_portfolio_data(
        assets, prices, retirement_goal_monthly_twd, monthly_income_twd,
        allow_paid, prev_summary)
    if report_note:
        portfolio_data["report_note"] = report_note
    return _build_html(portfolio_data), portfolio_data


def _handle_get_portfolio(email: str, user: dict) -> dict:
    """GET（list）：回傳持倉 + 報告清單 metadata（不簽 URL；查看須走 view 動作）。"""
    portfolio_raw = user.get("portfolio")
    portfolio = None
    if portfolio_raw:
        portfolio = json.loads(portfolio_raw) if isinstance(portfolio_raw, str) else portfolio_raw
    return {
        "statusCode": 200,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps({
            "portfolio": portfolio,
            "reports": _list_reports(email),
        }, ensure_ascii=False),
    }


def _handle_view_report(email: str, key: str) -> dict:
    """GET（view）：驗證擁有者後，由 Lambda 直接回傳報告 HTML（私有，無外洩窗）。"""
    if not _owns_key(email, key):
        return _err("Forbidden", 403)
    try:
        obj = s3.get_object(Bucket=REPORT_BUCKET, Key=key)
        html = obj["Body"].read().decode("utf-8")
    except Exception:
        return _err("報告不存在或已過期", 404)
    return {
        "statusCode": 200,
        "headers": {**CORS_HEADERS, "Content-Type": "text/html; charset=utf-8"},
        "body": html,
    }


def _handle_share_report(email: str, key: str) -> dict:
    """GET（share）：驗證擁有者後，簽發 24h 公開分享連結（使用者主動觸發）。

    以 DynamoDB 條件更新做每帳號 60 秒冷卻，原子地防止亂按狂產連結。
    """
    if not _owns_key(email, key):
        return _err("Forbidden", 403)
    now = int(time.time())
    try:
        users_table.update_item(
            Key={"email": email},
            UpdateExpression="SET last_share_at = :now",
            ConditionExpression="attribute_not_exists(last_share_at) OR last_share_at < :cutoff",
            ExpressionAttributeValues={":now": now, ":cutoff": now - SHARE_COOLDOWN},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _err(f"產生分享連結太頻繁，請 {SHARE_COOLDOWN} 秒後再試", 429)
        raise
    try:
        url = _share_url(key)
    except Exception:
        return _err("產生分享連結失敗", 500)
    return {
        "statusCode": 200,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps({"share_url": url, "expires_hours": SHARE_EXPIRY // 3600}),
    }


def lambda_handler(event, _context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    if method == "OPTIONS":
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

    # ── 用戶層級 ──
    user = _get_or_create_user(email)

    # ── GET：list（持倉+報告清單）/ view（私有看報告）/ share（產生分享連結）──
    if method == "GET":
        qs = event.get("queryStringParameters") or {}
        action = qs.get("action") or "list"
        key = qs.get("key") or ""
        if action == "view":
            return _handle_view_report(email, key)
        if action == "share":
            return _handle_share_report(email, key)
        return _handle_get_portfolio(email, user)

    # ── POST：產報告（扣額度）──
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

    if len(assets) > MAX_ASSETS:
        return _err(f"資產筆數上限為 {MAX_ASSETS}（目前 {len(assets)}）", 413)

    assets = _sanitize_assets(assets)
    if not assets:
        return _err("Assets list is empty")

    goal   = _safe_float(body.get("retirement_goal_monthly_twd"))
    income = _safe_float(body.get("monthly_income_twd"), allow_negative=True)
    report_note = str(body.get("report_note") or "")[:40]

    prev_summary = _get_previous_summary(email)

    html, portfolio_data = asyncio.run(
        run(assets, goal, income, allow_paid, report_note, prev_summary))

    _save_portfolio(email, assets, goal, income, portfolio_data)

    if REPORT_BUCKET:
        # 先算 key 與端點，注入報告（讓報告內的「分享」按鈕知道自己的 key 與回呼端點），再存
        report_key = _new_report_key(email)
        html = (html.replace("__REPORT_SHARE_KEY__", report_key)
                    .replace("__REPORT_SHARE_ENDPOINT__", _self_endpoint(event)))
        _put_report(email, report_key, html)
        # 回傳 HTML 供前端 blob 即時開啟（私有，不經公開 URL）
        return {
            "statusCode": 200,
            "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
            "body": json.dumps({"report_key": report_key, "html": html}, ensure_ascii=False),
        }

    return _ok(html)
