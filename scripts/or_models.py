#!/usr/bin/env python3
"""OpenRouter 模型健檢 + 熱抽換（免重新部署）

免費模型常被 OpenRouter 下架/限流，本工具讓你隨時：
  1) 健檢：對一組模型逐一發最小請求，回 OK / HTTP 狀態，判斷還活著沒。
  2) 熱抽換：只覆寫 Lambda 的 OPENROUTER_RACE_MODELS / OPENROUTER_MODELS 兩個
     環境變數（其餘變數、金鑰原封不動），約 10 秒生效，不必 sam deploy。

金鑰只從本地 env.json 讀取（健檢用），絕不列印。Lambda 環境變數的讀寫在
boto3 行程內完成，同樣不列印任何機密。

用法：
  # 健檢線上目前設定的模型
  python scripts/or_models.py check --deployed

  # 健檢自訂清單
  python scripts/or_models.py check openai/gpt-oss-20b:free google/gemma-4-31b-it

  # 只列出 OpenRouter 目前所有 :free 模型（挑替代品用）
  python scripts/or_models.py list-free

  # 熱抽換（只改這兩個變數，其餘不動）
  python scripts/or_models.py set \
    --race "openai/gpt-oss-20b:free,google/gemma-4-31b-it:free" \
    --backstop "google/gemma-4-31b-it,openai/gpt-oss-120b"
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

FUNCTION = os.environ.get("REPORT_FN", "asset-report-generator")
BASE = "https://openrouter.ai/api/v1"


def _load_key() -> str:
    """從 env.json（可能有巢狀）取 OPENROUTER_API_KEY。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "env.json"), encoding="utf-8") as fp:
        d = json.load(fp)
    flat = {}
    for k, v in d.items():
        flat.update(v) if isinstance(v, dict) else flat.__setitem__(k, v)
    key = str(flat.get("OPENROUTER_API_KEY", ""))
    if not key:
        sys.exit("env.json 內找不到 OPENROUTER_API_KEY")
    return key


def _ping(model: str, key: str) -> str:
    body = json.dumps({
        "model": model, "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=60)
        p = json.load(r)
        return "OK" if p.get("choices") else "空回應"
    except urllib.error.HTTPError as e:
        try:
            msg = (json.load(e).get("error") or {}).get("message", "")
        except Exception:
            msg = ""
        return f"HTTP {e.code} {msg}".strip()
    except Exception as e:  # noqa: BLE001
        return f"EXC {e!r}"


def _lambda_client():
    try:
        import boto3
    except ImportError:
        sys.exit("需要 boto3：pip install boto3")
    return boto3.client("lambda")


def _deployed_models() -> tuple[list[str], list[str]]:
    lam = _lambda_client()
    cfg = lam.get_function_configuration(FunctionName=FUNCTION)
    env = cfg.get("Environment", {}).get("Variables", {})
    race = [m for m in env.get("OPENROUTER_RACE_MODELS", "").split(",") if m]
    back = [m for m in env.get("OPENROUTER_MODELS", "").split(",") if m]
    return race, back


def cmd_check(args):
    key = _load_key()
    if args.deployed:
        race, back = _deployed_models()
        print("== 線上 OPENROUTER_RACE_MODELS（免費競速）==")
        for m in race:
            print(f"  {m:<45} {_ping(m, key)}")
        print("== 線上 OPENROUTER_MODELS（付費兜底）==")
        for m in back:
            print(f"  {m:<45} {_ping(m, key)}")
    else:
        if not args.models:
            sys.exit("請給模型清單，或用 --deployed")
        for m in args.models:
            print(f"  {m:<45} {_ping(m, key)}")


def cmd_list_free(args):
    key = _load_key()
    req = urllib.request.Request(f"{BASE}/models", headers={"Authorization": "Bearer " + key})
    data = json.load(urllib.request.urlopen(req, timeout=60))["data"]
    free = sorted(m["id"] for m in data if m["id"].endswith(":free"))
    print(f"目前 :free 模型共 {len(free)} 支：")
    for m in free:
        print(" ", m)


def cmd_set(args):
    if not args.race and not args.backstop:
        sys.exit("至少指定 --race 或 --backstop 其一")
    lam = _lambda_client()
    cfg = lam.get_function_configuration(FunctionName=FUNCTION)
    env = dict(cfg.get("Environment", {}).get("Variables", {}))  # 保留其餘變數（含金鑰）
    if args.race:
        env["OPENROUTER_RACE_MODELS"] = args.race
    if args.backstop:
        env["OPENROUTER_MODELS"] = args.backstop
    lam.update_function_configuration(
        FunctionName=FUNCTION, Environment={"Variables": env})
    print("已更新（其餘環境變數維持不變）：")
    if args.race:
        print("  OPENROUTER_RACE_MODELS =", args.race)
    if args.backstop:
        print("  OPENROUTER_MODELS      =", args.backstop)
    print("約 10 秒後新叫用生效。建議接著跑： python scripts/or_models.py check --deployed")


def main():
    ap = argparse.ArgumentParser(description="OpenRouter 模型健檢 + 熱抽換")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="健檢模型是否可用")
    c.add_argument("models", nargs="*", help="要健檢的模型（留空需搭配 --deployed）")
    c.add_argument("--deployed", action="store_true", help="改為健檢線上目前設定的模型")
    c.set_defaults(func=cmd_check)

    lf = sub.add_parser("list-free", help="列出 OpenRouter 目前所有 :free 模型")
    lf.set_defaults(func=cmd_list_free)

    s = sub.add_parser("set", help="只覆寫 race/backstop 兩個環境變數（免重新部署）")
    s.add_argument("--race", help="OPENROUTER_RACE_MODELS，逗號分隔")
    s.add_argument("--backstop", help="OPENROUTER_MODELS，逗號分隔")
    s.set_defaults(func=cmd_set)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
