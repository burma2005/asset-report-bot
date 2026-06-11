# DEPLOYMENT.md — Agent 部署 SOP

> **本文件寫給 AI Agent（Claude Code 等）執行**，不是人類操作手冊。
> Agent 應依序執行各步驟、比對預期輸出、依故障對照表自行排除問題。
> 系統架構與工作流程見 [README.md](README.md)。

---

## 人類需要提供的事項（Agent 無法代勞）

| # | 事項 | 時機 | 原因 |
|---|------|------|------|
| 1 | 一個 AWS 帳號 | 開始前 | — |
| 2 | 在自己的終端機執行 `aws login` 並在瀏覽器按「Allow」 | Step 2 | `aws login` 需要互動式 console，Agent 的 shell 無法開啟；憑證屬人類身分 |
| 3 | （若 Step 3 檢查失敗）到 Bedrock Console 點擊啟用模型 | Step 3 | Model Access 申請表單需人類確認用途 |
| 4 | 部署 region 偏好（預設東京 ap-northeast-1） | 開始前 | 資料主權偏好屬使用者決策 |

除上述四項，**其餘全部由 Agent 執行**。

---

## Step 1：檢查並安裝工具鏈（Agent 執行）

```powershell
# 檢查三項工具，缺哪個裝哪個
aws --version    # 預期 aws-cli/2.x；缺 → winget install Amazon.AWSCLI --silent
sam --version    # 預期 SAM CLI 1.1xx；缺 → winget install Amazon.SAM-CLI --silent
python --version # 預期 3.11+
```

**注意**：winget 安裝後，目前 shell 的 PATH 不會自動更新。每個新指令前需重載：

```powershell
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
```

## Step 2：AWS 憑證（人類介入點）

```powershell
aws configure set region ap-northeast-1
aws sts get-caller-identity   # 成功 → 跳到 Step 3
```

若回 `NoCredentials`：**請人類開新的 PowerShell 視窗執行 `aws login`**，瀏覽器授權後回報。
- 人類視窗若報 `無法辨識 'aws'` → 給他上面的 PATH 重載指令，或完整路徑 `& "C:\Program Files\Amazon\AWSCLIV2\aws.exe" login`
- Agent **不可**嘗試在自己的 shell 跑 `aws login`——會報 `No Windows console found`

## Step 3：確認 Bedrock 模型可用（Agent 執行）

```powershell
aws bedrock list-inference-profiles --region ap-northeast-1 `
  --query "inferenceProfileSummaries[?starts_with(inferenceProfileId,'jp.anthropic')].inferenceProfileId"
```

預期包含 `jp.anthropic.claude-haiku-4-5-20251001-v1:0` 與 `jp.anthropic.claude-sonnet-4-6`。

實測驗證（用實際 invoke 確認權限，而非只看列表）：

```powershell
$body = '{"anthropic_version":"bedrock-2023-05-31","max_tokens":20,"messages":[{"role":"user","content":"hi"}]}'
[System.IO.File]::WriteAllText("$env:TEMP\bedrock_test.json", $body)
aws bedrock-runtime invoke-model --region ap-northeast-1 `
  --model-id "jp.anthropic.claude-haiku-4-5-20251001-v1:0" `
  --body fileb://$env:TEMP\bedrock_test.json --content-type application/json "$env:TEMP\out.json"
```

- 成功回傳 JSON → 繼續
- `AccessDeniedException` → **請人類**到 Bedrock Console（東京區）→ Model access 啟用兩個 Claude 模型，完成後 Agent 重試

## Step 4：產生 API Key、建立部署設定（Agent 執行）

```powershell
# PowerShell 5.1 注意：RandomNumberGenerator::Fill 不存在，用 RNGCryptoServiceProvider
$rng = [System.Security.Cryptography.RNGCryptoServiceProvider]::new()
$b = New-Object byte[] 32; $rng.GetBytes($b)
$key = ([Convert]::ToBase64String($b) -replace '[+/=]','').Substring(0,40)
```

建立 `samconfig.toml`（從 `samconfig.toml.example` 複製，填入 `$key`）。
**第一行必須是 `version = 0.1`**，否則 `sam build` 報 `SamConfigVersionException`。

## Step 5：部署（Agent 執行）

```powershell
sam build    # 純 Python 依賴（boto3/httpx），Windows 打包 Linux ARM64 無相容問題
sam deploy   # confirm_changeset=false 已設定，不會卡互動
```

成功訊號：`Successfully created/updated stack - asset-report-bot`
從輸出取得 `ReportEndpointUrl`（或事後查）：

```powershell
aws cloudformation describe-stacks --stack-name asset-report-bot `
  --query "Stacks[0].Outputs[?OutputKey=='ReportEndpointUrl'].OutputValue" --output text
```

## Step 6：設定前端（Agent 執行）

```powershell
Copy-Item index.html index.local.html   # 已 gitignore
```

編輯 `index.local.html` 頂部常數：`LAMBDA_ENDPOINT` = Step 5 的 URL、`API_KEY` = Step 4 的 `$key`。
**禁止**把真實值寫進 `index.html`（會進版控）。

## Step 7：三段式驗證（Agent 執行）

| 測試 | 請求 | 預期 |
|------|------|------|
| 無金鑰 | POST 不帶 X-Api-Key | **401**（驗證生效、零 AI 費用） |
| 錯誤金鑰 | POST 帶 `X-Api-Key: wrong` | **401** |
| 正確金鑰 + 範例資產 | POST 帶正確金鑰與下方 payload | **200** + HTML（30~60s） |

```powershell
$payload = '{"assets":{"BTC":{"api":"binance","amount":0.1,"name":"Bitcoin"},"TWD_CASH":{"api":"cash","currency":"TWD","amount":10000,"name":"台幣"}},"retirement_goal_monthly_twd":25000,"monthly_income_twd":0}'
Invoke-WebRequest -Uri <ReportEndpointUrl> -Method POST `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) `
  -ContentType "application/json; charset=utf-8" `
  -Headers @{"X-Api-Key"="<金鑰>"} -TimeoutSec 180 -UseBasicParsing
```

200 之後解析回傳 HTML 中的 `const D = {...}`，抽查：`total_twd` 數量級合理、`assets[].price_native` 非 null、`assets[].source` 標示正確來源。

## 程式碼更新流程（Agent 執行）

```powershell
# 所有程式與模板都在 src/generate_report/（單一真相來源），改完直接部署
sam build; sam deploy
```

---

## 故障對照表（皆為實際部署時踩過的坑）

| 症狀 | 根因 | 處置 |
|------|------|------|
| 瀏覽器 `Failed to fetch`，但 curl 正常 | `Access-Control-Allow-Origin` 出現兩次（Lambda 程式 + Function URL 服務層各加一份），瀏覽器拒收 | `app.py` 的 `CORS_HEADERS` 必須是空 dict，CORS 全交給 `FunctionUrlConfig.Cors` |
| 503 Service Unavailable | 曾用 API Gateway HTTP API，30 秒硬上限被多次 AI 呼叫撞破 | 本專案已改 Lambda Function URL（上限 15 分）；勿改回 API Gateway |
| `SamConfigVersionException` | samconfig.toml 缺 `version = 0.1` | 補第一行 |
| `sam build` 後 Lambda 跑不起來（import error） | 引入了含原生二進位的套件（numpy/pandas/yfinance），Windows wheel 與 Lambda Linux 不符 | requirements.txt 只允許純 Python 套件；股價用 httpx 直呼 Yahoo chart API |
| 總資產數量級爆炸（億級） | 台股 `.TW` 被誤套 USD 匯率；債券面額被當單位數重複相乘 | 已修；改價格邏輯時務必跑 Step 7 抽查數量級 |
| AI 回傳數字與輸入不符 | LLM 會「順手修正」原始數字 | 不可信任：所有數量/報價/市值在 AI 驗證後由 `_build_asset_lines()` 強制回寫 |
| `aws login` 報 `No Windows console found` | Agent 的 shell 非互動式 | 這步必須由人類在自己的終端機執行 |
| winget 裝完找不到指令 | PATH 未重載 | 用 Step 1 的 PATH 重載指令 |
