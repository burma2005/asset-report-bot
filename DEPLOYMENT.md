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
   把這個專案完整部署到我的 AWS 帳號（Lambda + DynamoDB + S3 + CloudFront）。
   遇到「人類介入點」時暫停並告訴我該做什麼，其餘步驟全部由你執行並驗證。
   ```

3. 接下來 Agent 會自己跑。過程中需要你配合的事項見下表。

---

## 人類需要提供的事項（Agent 無法代勞）

| # | 事項 | 時機 | 原因 |
|---|------|------|------|
| 1 | AWS 帳號 + `aws login` | Step 2 | 互動式登入需人類操作 |
| 2 | OpenRouter API key（https://openrouter.ai/keys） | Step 3 | 金鑰屬使用者帳號 |
| 3 | 建立 Google OAuth 2.0 Client ID | Step 4 | Google Cloud Console 無公開 API，需人工建立 |
| 4 | 部署後把 **CloudFront 域名**加進 Google「已授權 JavaScript 來源」 | Step 8 | 域名要 deploy 後才有 |
| 5 | OAuth 同意畫面**發布成正式版**（要開放任意 Google 帳號試用時） | Step 4 | 測試模式只有測試使用者能登入 |

---

## Step 1：檢查並安裝工具鏈（Agent 執行）

```powershell
aws --version    # 預期 aws-cli/2.x
sam --version    # 預期 SAM CLI 1.1xx
python --version # 預期 3.11+
```

缺哪個裝哪個（`winget install Amazon.AWSCLI`、`winget install Amazon.SAM-CLI`）。

## Step 2：AWS 憑證（人類介入點）

```powershell
aws configure set region ap-northeast-1
aws sts get-caller-identity   # 成功 → 繼續
```

若回 `NoCredentials` 或 token 過期：請人類執行 `aws login`，瀏覽器授權後回報。

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

Agent 無法代勞（Google 不開放 API 建立 OAuth 憑證）。請人類：

1. 開 https://console.cloud.google.com/apis/credentials
2. 設定 OAuth 同意畫面 → 使用者類型選**外部 (External)**
3. ＋建立憑證 → OAuth 用戶端 ID → 網頁應用程式
4. 「已授權的 JavaScript 來源」先加 `http://localhost:8000`（本地開發用；CloudFront 域名待 Step 8 補）
5. 把 Client ID（`xxx.apps.googleusercontent.com`）貼給 Agent
6. **若要開放任意 Google 帳號試用**：到 OAuth 同意畫面點「發布應用程式 (PUBLISH APP)」改成**正式版**。
   測試模式 (Testing) 下只有「測試使用者」清單內的帳號能登入，其他帳號（含企業 Workspace 帳號）一律被擋。

## Step 5：建立部署設定（Agent 執行）

從 `samconfig.toml.example` 複製成 `samconfig.toml`（已 gitignore）：

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

## Step 6：部署（Agent 執行，首次 ~15 分鐘）

```powershell
sam build    # 純 Python 依賴（httpx, google-auth, requests）
sam deploy
```

- **首次部署 CloudFront Distribution 約 15 分鐘**，`sam deploy` 會卡在 `CREATE_IN_PROGRESS`，屬正常。
- 成功訊號：`Successfully created/updated stack - asset-report-bot`

## Step 7：取得 outputs（Agent 執行）

```powershell
aws cloudformation describe-stacks --stack-name asset-report-bot `
  --query "Stacks[0].Outputs" --output table
```

記下：`ReportEndpointUrl`、`CloudFrontDomain`、`FrontendBucketName`、`CloudFrontDistId`、`ReportBucketName`。

## Step 8：把 CloudFront 域名加進 Google origins（人類介入點）

把 Step 7 的 `CloudFrontDomain` 給人類，請他到 https://console.cloud.google.com/apis/credentials
編輯 OAuth Client ID → 「已授權的 JavaScript 來源」新增 `https://<CloudFrontDomain>`（**含 https、無結尾斜線**）→ 儲存（生效約數分鐘）。

## Step 9：注入前端設定並上傳 S3（Agent 執行）

git 版的 `index.html` 維持 `__YOUR_LAMBDA_FUNCTION_URL__` / `__YOUR_GOOGLE_CLIENT_ID__` 佔位符。
部署時複製成 `dist/index.html`、替換真實值再上傳（兩者皆公開值，無安全疑慮）。

> **注意編碼**：PowerShell 5.1 的 `Get-Content`/`Set-Content` 會把 UTF-8 中文轉成亂碼，
> 必須用 `[System.IO.File]::ReadAllText/WriteAllText` 明確指定 UTF-8（無 BOM）。

```powershell
$ep  = "<ReportEndpointUrl>"
$cid = "<GoogleClientId>"
$bkt = "<FrontendBucketName>"
$dist= "<CloudFrontDistId>"

New-Item -ItemType Directory -Force dist | Out-Null
$c = [System.IO.File]::ReadAllText("$PWD\index.html", [System.Text.Encoding]::UTF8)
$c = $c -replace '__YOUR_LAMBDA_FUNCTION_URL__', $ep -replace '__YOUR_GOOGLE_CLIENT_ID__', $cid
[System.IO.File]::WriteAllText("$PWD\dist\index.html", $c, (New-Object System.Text.UTF8Encoding($false)))

aws s3 cp dist/index.html "s3://$bkt/index.html" --content-type "text/html; charset=utf-8"
aws cloudfront create-invalidation --distribution-id $dist --paths "/index.html" "/"
```

> 日後改 `index.html` 重新上線：重跑本步驟（注入 + 上傳 + invalidation），CDN 約 1 分鐘更新。

## Step 10：端到端測試（開 `https://<CloudFrontDomain>`）

| 測試 | 預期 |
|------|------|
| 開 CloudFront 網址 | 顯示輸入頁 + Google 登入按鈕 |
| HTTP 開同網址 | 自動 302 跳 HTTPS |
| Google 登入 | 成功（origin 已加白名單；若 `origin_mismatch` 檢查含 `https://`、無尾斜線） |
| 產報告（第一次） | 報告以 blob 私有開啟，含「下載/分享」按鈕 |
| 產報告（第二次） | 出現「與上次報告對比」區塊（觸發 comparison 路徑） |
| 報告內「分享」 | 風險確認 → 24h 公開連結；無痕視窗可開、去掉簽章參數則 403 |
| general 首次登入 | DynamoDB 自動建立 general（limit=12） |
| general 超額 | 429 |
| 直接開報告 S3 物件 | 403（私有，僅 Lambda 簽名/驗身分可讀） |

## Step 11：（選填）新增 invited 用戶（額度 60/月）

```bash
aws dynamodb update-item --table-name asset-report-users --region ap-northeast-1 \
  --key '{"email":{"S":"friend@gmail.com"}}' \
  --update-expression "SET #r = :role, monthly_limit = :v, used_this_month = if_not_exists(used_this_month, :z), reset_month = if_not_exists(reset_month, :m)" \
  --expression-attribute-names '{"#r":"role"}' \
  --expression-attribute-values '{":role":{"S":"invited"},":v":{"N":"60"},":z":{"N":"0"},":m":{"S":"2026-06"}}'
```

---

## 故障對照表

| 症狀 | 根因 | 處置 |
|------|------|------|
| 登入按鈕點了沒反應、非測試帳號無法登入 | OAuth 同意畫面在「測試」模式 | 發布成正式版（Step 4-6）；企業帳號另可能被該公司 Workspace 管理員封鎖，無解 |
| `origin_mismatch` / 登入失敗 | Google origins 未含 CloudFront 域名或格式錯 | 加 `https://<CloudFrontDomain>`，含 https、無尾斜線 |
| 報告中文變亂碼 | PowerShell `Get/Set-Content` 編碼問題 | 用 `[System.IO.File]::ReadAllText/WriteAllText` + UTF-8（Step 9） |
| 改版後 CDN 仍是舊頁 | CloudFront 快取 | `aws cloudfront create-invalidation`（每月 1000 次免費） |
| 第二次產報告 502 | comparison 程式錯誤 | 已修；若復發拉 CloudWatch 日誌看 Traceback |
| AI 區塊顯示藍色提示卡 | 模型額度限制，非 bug | 重產即可；admin/invited 有付費兜底不受影響 |
| `sam build` 失敗 | requirements.txt 含原生二進位套件 | 只允許純 Python 套件 |
| DynamoDB 權限錯誤 | IAM 缺對應動作 | `template.yaml` 已明列 `GetItem/PutItem/UpdateItem`，確認 stack 已更新 |
| CloudFront 首次 deploy 卡很久 | Distribution 全球部署 | 正常，約 15 分鐘 |

---

## 本地開發（選填，不經 CloudFront）

```powershell
Copy-Item index.html index.local.html   # 已 gitignore
# 編輯 index.local.html 填入真實 LAMBDA_ENDPOINT / GOOGLE_CLIENT_ID
python -m http.server 8000
# 開 http://localhost:8000/index.local.html（GIS 不支援 file://，且 origin 需在 Google 白名單）
```
