# DEPLOYMENT.md — Agent 部署 SOP

> **本文件寫給 AI Agent（Claude Code 等）執行**，不是人類操作手冊。
> Agent 應依序執行各步驟、比對預期輸出、依故障對照表自行排除問題。
> 系統架構與工作流程見 [README.md](README.md)。

---

## 如何使用本文件（人類看這段就夠了）

1. Clone 本專案並在專案目錄啟動 Claude Code：

   ```powershell
   git clone https://github.com/burma2005/asset-report-bot.git
   cd asset-report-bot
   claude
   ```

2. 貼上這段 prompt：

   ```
   請閱讀 DEPLOYMENT.md 並依照其中的 Agent 部署 SOP，
   把這個專案完整部署到我的 AWS 帳號。
   遇到「人類介入點」時暫停並告訴我該做什麼，
   其餘步驟全部由你執行並驗證。
   ```

3. 接下來 Agent 會自己跑。過程中需要你配合的事項見下表。

---

## 人類需要提供的事項（Agent 無法代勞）

| # | 事項 | 時機 | 原因 |
|---|------|------|------|
| 1 | AWS 帳號 + `aws login` | Step 2 | 互動式登入需人類操作 |
| 2 | OpenRouter API key（https://openrouter.ai/keys） | Step 3 | 金鑰屬使用者帳號 |
| 3 | Google OAuth 2.0 Client ID | Step 4 | Google Cloud Console 無公開 API，需人工建立 |
| 4 | 部署 region 偏好（預設東京 ap-northeast-1） | 開始前 | 使用者決策 |

---

## Step 1：檢查並安裝工具鏈（Agent 執行）

```powershell
aws --version    # 預期 aws-cli/2.x
sam --version    # 預期 SAM CLI 1.1xx
python --version # 預期 3.11+
```

缺哪個裝哪個（`winget install Amazon.AWSCLI`、`winget install Amazon.SAM-CLI`）。
安裝後需重載 PATH：

```powershell
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
```

## Step 2：AWS 憑證（人類介入點）

```powershell
aws configure set region ap-northeast-1
aws sts get-caller-identity   # 成功 → 繼續
```

若回 `NoCredentials`：請人類開 PowerShell 執行 `aws login`，瀏覽器授權後回報。

## Step 3：確認 OpenRouter 金鑰可用（Agent 執行）

```powershell
curl -s https://openrouter.ai/api/v1/chat/completions `
  -H "Authorization: Bearer <OpenRouter金鑰>" `
  -H "Content-Type: application/json" `
  -d '{"model":"google/gemma-4-31b-it:free","messages":[{"role":"user","content":"OK"}],"max_tokens":10}'
```

- 回傳含 `choices` → 繼續
- `401` → 金鑰錯誤
- `429`（限流）→ 正常，雲端會自動 fallback

## Step 4：Google OAuth Client ID（人類介入點）

Agent 無法代勞此步驟（Google 不開放 API 建立 OAuth 憑證）。請人類：

1. 開 https://console.cloud.google.com/apis/credentials
2. 如需設定 OAuth 同意畫面 → 選外部 → 應用名稱隨意 → 儲存 → 發布應用程式
3. ＋建立憑證 → OAuth 用戶端 ID → 網頁應用程式
4. 已授權的 JavaScript 來源加 `http://localhost:8000`
5. 把 Client ID（`xxx.apps.googleusercontent.com`）貼給 Agent

## Step 5：建立部署設定（Agent 執行）

建立 `samconfig.toml`（從 `samconfig.toml.example` 複製）：

```toml
version = 0.1

[default.deploy.parameters]
stack_name = "asset-report-bot"
region = "ap-northeast-1"
confirm_changeset = false
capabilities = "CAPABILITY_IAM"
resolve_s3 = true
parameter_overrides = "OpenRouterKeyParam=<key> GoogleClientIdParam=<client-id> AdminEmailParam=<admin-email>"
```

## Step 6：部署（Agent 執行）

```powershell
sam build    # 純 Python 依賴（httpx, google-auth, requests）
sam deploy
```

成功訊號：`Successfully created/updated stack - asset-report-bot`
取得端點 URL：

```powershell
aws cloudformation describe-stacks --stack-name asset-report-bot `
  --query "Stacks[0].Outputs[?OutputKey=='ReportEndpointUrl'].OutputValue" --output text
```

## Step 7：設定前端（Agent 執行）

```powershell
Copy-Item index.html index.local.html   # 已 gitignore
```

編輯 `index.local.html`：
- `LAMBDA_ENDPOINT` = Step 6 的 URL
- `GOOGLE_CLIENT_ID` = Step 4 的 Client ID

**不可把真實值寫進 `index.html`**（會進版控）。

## Step 8：本地測試

```powershell
python -m http.server 8000
```

開 `http://localhost:8000/index.local.html` → Google 登入 → 產報告。

驗證項目：

| 測試 | 預期 |
|------|------|
| 無 token POST | 401 |
| 偽造 token | 401 |
| admin 登入產報告 | 200，無限次 |
| general 首次登入 | DynamoDB 自動建立 general（limit=4） |
| general 超額 | 429 |
| general 報告 | AI 走免費、不掉付費 |

## Step 9：（選填）新增 invited 用戶

```bash
aws dynamodb put-item --table-name asset-report-users --region ap-northeast-1 \
  --item '{"email":{"S":"friend@gmail.com"},"role":{"S":"invited"},"monthly_limit":{"N":"30"},"used_this_month":{"N":"0"},"reset_month":{"S":"2026-06"}}'
```

---

## 故障對照表

| 症狀 | 根因 | 處置 |
|------|------|------|
| 瀏覽器 `Failed to fetch` | CORS `AllowHeaders` 未包含 `authorization` | 確認 `template.yaml` 的 `AllowHeaders` 含 `authorization` |
| Google 登入按鈕不出現 | GIS 不支援 `file://` | 必須用 `python -m http.server 8000` + `http://localhost:8000` |
| 登入後無法產報告（401） | Google Client ID 不符或 token 過期 | 確認 Lambda 環境變數 `GOOGLE_CLIENT_ID` 與前端一致 |
| AI 區塊全空 | 免費模型限流（general）或 OpenRouter 金鑰問題 | general 屬正常（免費限制）；admin 空白則檢查金鑰 |
| AI 區塊顯示藍色提示卡 | 免費模型額度限制，非 bug | 重新產報告即可嘗試；admin/invited 有付費兜底不受影響 |
| `sam build` 失敗 | requirements.txt 含原生二進位套件 | 只允許純 Python 套件 |
| DynamoDB 權限錯誤 | IAM policy 缺少 DynamoDBCrudPolicy | 確認 `template.yaml` 的 Policies 區塊 |
| Lambda 逾時 | AI 呼叫過多，Timeout 不夠 | `template.yaml` Timeout 已設 300 秒 |
