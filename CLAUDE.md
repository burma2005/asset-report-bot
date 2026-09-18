# CLAUDE.md — 給 AI Agent 的專案工作規範

> 架構與功能看 [README.md](README.md)，部署步驟看 [DEPLOYMENT.md](DEPLOYMENT.md)。
> 本文件只寫「在這個 repo 動手時必須遵守的規則」，不重複那兩份的內容。

## ⚠️ 這是 PUBLIC repo

`github.com/burma2005/asset-report-bot` **完全公開**。動任何 commit 前確認沒有機密進入 git。

以下檔案存在於本機、已被 `.gitignore` 擋住，**永遠不要提交、不要在對話中印出其內容**：

| 檔案 | 內容 |
|------|------|
| `env.json` | `OPENROUTER_API_KEY`、`API_KEY` |
| `samconfig.toml` | OpenRouter 金鑰 / Google Client ID / admin email |
| `client_secret_*.json` | Google OAuth client secret |
| `p-*.html` | 真實管理後台頁（隨機檔名，不公開；repo 內僅 `admin.html` 展示版） |
| `index.local.html`、`dist/` | 含真實端點的本地/部署產物 |

git 歷史目前零金鑰（已稽核全部 commit），請維持這個狀態。若不慎提交，改金鑰比改歷史優先。

## 動程式碼的準則

**數字歸 Python，敘述歸 AI。** 報價、匯率、市值、占比、退休試算、耐久模擬、脫鉤/暴跌示警、梗圖挑選一律純 Python 計算，不容 AI 幻覺介入；AI 只負責質化分析與建議，且輸出區塊必須保留「僅供參考，不構成投資建議」警語。新增功能時先判斷它屬於哪一邊。

**前端佔位符不可寫死。** git 版 `index.html` 永遠保持 `__YOUR_LAMBDA_FUNCTION_URL__` / `__YOUR_GOOGLE_CLIENT_ID__`，真實值只在 DEPLOYMENT.md Step 9 注入 `dist/` 時替換。

**改完 `index.html` 一定要重跑 Step 9**（注入 → 上傳 S3 → CloudFront invalidation），否則線上還是舊頁。

**`sam build` 只吃純 Python 套件**，`requirements.txt` 不能加含原生二進位的依賴（Lambda ARM64 會炸）。

**IAM 維持最小權限**：Lambda 只有 DynamoDB `GetItem/PutItem/UpdateItem`（刻意不給 `Scan`/`Query`，防整張用戶表被拖庫）+ 單一報告桶 S3 權限。要加動作前先想清楚必要性。

## 環境陷阱（這台機器踩過的）

- **PowerShell 5.1 的 `Get-Content`/`Set-Content` 會把 UTF-8 中文寫成亂碼。** 處理含中文的檔案一律用 `[System.IO.File]::ReadAllText/WriteAllText` 並明確指定 UTF-8（無 BOM）。
- PowerShell 5.1 沒有 `&&`、`||`、三元運算子，改用 `;` 與 `if ($?) { }`。
- AWS：region `ap-northeast-1`、stack 名 `asset-report-bot`（帳號 ID 見本機 `~/.aws/config`，不寫進公開 repo）。憑證用互動式 `aws login`，是人類介入點，Agent 無法代勞。

## 模型維護

免費模型常被 OpenRouter 下架，`scripts/or_models.py` 可熱抽換 Lambda 環境變數（約 10 秒生效，免 `sam deploy`）。

⚠️ **熱抽換是暫時的** —— 下次 `sam deploy` 會被 `samconfig.toml` 的 `parameter_overrides` 覆蓋回去。要永久生效，同一組值必須一併寫進 `samconfig.toml`。

付費兜底 slug **切勿**帶 `:free`（否則等於沒兜底，全限流時 AI 區塊會整片空白）。

## 改動後的驗證

沒有自動化測試。改 Lambda 後至少手動走一次 DEPLOYMENT.md Step 10 的端到端表，其中兩項最容易漏：

- **連產兩次報告**（第二次才會走 comparison 路徑，這裡出過 502）
- **直接開報告 S3 物件應得 403**（確認報告仍是私有）
