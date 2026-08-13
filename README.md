# DECK 護城河監控 · 長線價值儀表板

> 忽略股價日常波動，專注企業內在價值與護城河。

針對 Deckers Outdoor（NYSE: DECK）的個人長線投資監控儀表板。數據直接取自 **SEC EDGAR 官方 XBRL 申報**（10-Q / 10-K），由 GitHub Actions 每日自動檢查、偵測到新財報時自動更新，並以 GitHub Pages 免費託管靜態頁面。**頁面上不顯示任何當日股價或技術 K 線。**

## 四大監控項目

| # | 監控項目 | 指標 | 燈號邏輯 |
|---|---|---|---|
| 01 | 核心品牌力 | 單季毛利率（20 季折線 + YoY 同期對比） | 跌破 50% 警戒線 → 🔴 定價權警示 |
| 02 | 財務安全防禦力 | 現金＋短投 vs 有息負債＋租賃負債 | 現金 > 含租賃總負債 → 🟢 財務絕對安全，否則 🟡 |
| 03 | 股票回購進度 | 在外流通股數 + 當季縮減率 | 現金↓ 且 股數↓ → 💡 低檔高效回購提示 |
| 04 | 每股含金量 | 稀釋 EPS（左軸）× 淨利率（右軸） | 近四季 EPS 年增 > 營收年增 → 🟢 複利效應運轉中 |

## 專案結構

```
├── index.html                    # 儀表板（純靜態，Plotly.js CDN）
├── data.json                     # 財報數據（由 Actions 自動產出並 commit）
├── fetch_data.py                 # SEC EDGAR 抓取腳本（僅用標準函式庫，零依賴）
├── .github/workflows/update.yml  # 每日排程 + 手動觸發
└── README.md
```

## 🚀 部署步驟（約 5 分鐘）

1. **建立 repo**：在 GitHub 建立新 repository（建議名稱 `deck-moat-monitor`，需為 **Public** 才能免費使用 Pages 與不限量 Actions），把本專案所有檔案上傳（注意保留 `.github/workflows/` 目錄結構）。

2. **確認 User-Agent**：`fetch_data.py` 開頭的 `USER_AGENT` 已填入你的 email。SEC 要求所有 API 請求需附上可聯絡的身份資訊，日後若更換信箱請同步修改此處。

3. **允許 Actions 寫入 repo**：到 repo 的 `Settings → Actions → General → Workflow permissions`，勾選 **Read and write permissions** 並儲存。（沒做這步，自動 commit 會失敗。）

4. **首次執行**：到 `Actions` 分頁 → 選擇 `Update DECK data` → 按 **Run workflow**。約 1 分鐘後，範例 `data.json` 會被替換成 SEC 真實數據（可在 commit 歷史看到 `data: update to FY20XX QX ...`）。

5. **開啟 GitHub Pages**：`Settings → Pages → Source` 選 **Deploy from a branch**，Branch 選 `main` / 根目錄，儲存。幾分鐘後儀表板網址即為：
   `https://<你的帳號>.github.io/deck-moat-monitor/`

之後你只需要每季點開這個網址。

## ⚙️ 自動更新機制

- **每日輪詢**：Actions 每天 UTC 14:37 執行（cron 為 UTC 時間）。腳本先打輕量的「哨兵 API」（`/submissions/`）取得最新 10-Q/10-K 申報日，與 `data.json` 記錄比對——**有新申報才做完整抓取與 commit**，最大延遲 1 天。
- **每週心跳**：無新財報時，每 7 天更新一次 `last_checked` 時間戳並 commit（訊息為 `chore: heartbeat ...`）。這是為了規避 GitHub「public repo 60 天無活動即停用排程」的規則，同時讓你在頁面上看到系統最後檢查時間。
- **手動觸發**：財報週想立刻更新，到 Actions 頁按 `Run workflow` 即可。
- **正常的時間差**：Deckers 財報電話會議後，10-Q 正式提交 SEC 可能晚幾天到一週；XBRL 數據要等正式 filed 後才會出現。「新聞說財報出了、儀表板還沒更新」屬正常延遲。

## 📐 數據處理說明（重要）

- **Q4 反推**：SEC 只有 Q1–Q3 的獨立 10-Q，Q4 單季 = 10-K 全年 − 前三季，腳本已內建。
- **財年口徑**：Deckers 財年 3 月底結帳，圖表標籤採官方財年（例：2025 年 4–6 月 = FY2026 Q1）。
- **股票分割**：2024/9/17 六股拆一。分割前申報的股數 ×6、EPS ÷6，全部換算為分割後口徑，圖表不會出現斷崖。
- **負債口徑**：燈號採嚴格標準「現金＋短投 vs 有息負債＋租賃負債」。Deckers 幾乎零有息借款，主要負債為門市營運租賃——用嚴格口徑，綠燈才有監控意義。
- **重述處理**：同一期間被多次申報時（比較期重述），以申報日最新者為準。
- **Q4 EPS 為近似值**：以「全年 EPS − 前三季 EPS」反推，因各季加權股數不同會有小數點級誤差，對長線趨勢判讀無影響。

## 🖥️ 本機預覽

直接雙擊 `index.html` 時，瀏覽器會因 `file://` 協議擋掉 `data.json` 的讀取，此時頁面自動改用內嵌範例資料並顯示提示。要預覽真實行為請在專案目錄執行：

```bash
python -m http.server 8000
# 瀏覽 http://localhost:8000
```

也可直接在本機執行抓取腳本測試：

```bash
python fetch_data.py   # 零依賴，Python 3.9+ 即可
```

## 免責聲明

數據來自 SEC EDGAR 公開申報，經自動化處理可能存在解析誤差；本專案僅供個人研究紀錄，不構成任何投資建議。
