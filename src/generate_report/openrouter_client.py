"""OpenRouter 模型呼叫：免費競速 + 付費兜底

策略：
  1. 前段「競速」：同時對多個免費模型發請求，誰先成功回應就用誰（壓低延遲、互為備援）。
  2. 後段「兜底」：競速全部失敗（429/逾時/空回）時，才依序呼叫付費模型保證有結果。

環境變數（皆逗號分隔，免改碼可調）：
  OPENROUTER_RACE_MODELS  競速用的免費模型（同時發、取最快成功）
  OPENROUTER_MODELS       兜底用的付費模型（依序 fallback）

金鑰只從環境變數讀取（本地 env.json / 雲端 samconfig），絕不進程式碼或 git。
"""

import os
import json
import httpx
from concurrent.futures import ThreadPoolExecutor, as_completed

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT = 90.0   # 單一模型逾時（秒）；卡住即視為失敗換手

# 預設：免費競速組 + 付費兜底（可由環境變數覆寫）
_DEFAULT_RACE = [
    "openai/gpt-oss-120b:free",
    "google/gemma-4-31b-it:free",
]
_DEFAULT_BACKSTOP = [
    "google/gemini-2.5-flash-lite",
]


def _race_models() -> list[str]:
    raw = os.environ.get("OPENROUTER_RACE_MODELS", "")
    models = [m.strip() for m in raw.split(",") if m.strip()]
    return models if raw else _DEFAULT_RACE


def _backstop_models() -> list[str]:
    raw = os.environ.get("OPENROUTER_MODELS", "")
    models = [m.strip() for m in raw.split(",") if m.strip()]
    return models if raw else _DEFAULT_BACKSTOP


def _is_rate_limited(status_code: int, payload: dict) -> bool:
    """OpenRouter 的 429 可能以 HTTP 狀態碼，或 200 body 內 error.code 回傳。"""
    if status_code == 429:
        return True
    err = payload.get("error") if isinstance(payload, dict) else None
    return bool(err) and err.get("code") == 429


def _call_one(model: str, headers: dict, system: str, user: str, max_tokens: int) -> str:
    """呼叫單一模型，成功回傳純文字；限流/錯誤/空回一律拋例外。"""
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    resp = httpx.post(OPENROUTER_URL, headers=headers, json=body, timeout=TIMEOUT)
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError):
        payload = {}

    if _is_rate_limited(resp.status_code, payload):
        raise RuntimeError(f"{model} 限流 (429)")
    if resp.status_code != 200 or payload.get("error"):
        msg = (payload.get("error") or {}).get("message", resp.text[:160])
        raise RuntimeError(f"{model} 錯誤: {msg}")
    text = (payload["choices"][0]["message"]["content"] or "").strip()
    if not text:
        raise RuntimeError(f"{model} 回傳空內容")
    return text


def call_llm(system: str, user: str, max_tokens: int = 4096, allow_paid: bool = True) -> str:
    """免費競速 → 付費兜底。allow_paid=False 時跳過付費兜底。"""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY 未設定")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: Exception | None = None

    # ── 1) 免費競速：同時發，取最快成功 ──
    race = _race_models()
    if race:
        ex = ThreadPoolExecutor(max_workers=len(race))
        try:
            futures = [
                ex.submit(_call_one, m, headers, system, user, max_tokens)
                for m in race
            ]
            for fut in as_completed(futures):
                try:
                    return fut.result()        # 第一個成功者勝出
                except Exception as e:
                    last_error = e             # 該模型失敗，續等其他競速者
        finally:
            ex.shutdown(wait=False)            # 不等落敗（仍在跑）的那個，避免拖慢

    # ── 2) 付費兜底：依序呼叫，保證有結果（general 層跳過）──
    if allow_paid:
        for m in _backstop_models():
            try:
                return _call_one(m, headers, system, user, max_tokens)
            except Exception as e:
                last_error = e

    raise RuntimeError(f"所有模型皆失敗（競速{'+兜底' if allow_paid else ''}）: {last_error}")
