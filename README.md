# Ark-Trader

方舟運算(ARK) App 的持倉同步、分析與自動交易工具，可同時安裝到 **Claude Code** 與 **Codex**。

## 架構：API 優先、AX 兜底

自 2.0.0 起，所有讀寫操作**預設走 ARK 後端 API**（`ark-api.my-galaxy.com.tw`），
不再依賴畫面抓取。Accessibility(AX) 操作降為**最終手段**——僅在 API 不可用
（App 未開、token 過期、無網路）時自動回退。

| 層級 | 讀取 | 寫入 |
|---|---|---|
| **主要：API** | `arkapi.positions()`、`cash()` 等 11 個端點 | `set_cash`、`insert/update/delete_holding`（需 `ARK_API_WRITES=1`） |
| **回退：AX** | 翻頁畫面抓取（API 不可用時自動啟用） | AX 畫面操作（預設，或 API 寫入失敗時回退） |

API 與 AX 同時可用時，兩邊交叉比對；不一致則停手不寫入。

## 帳戶來源

「真實持倉」由帳戶清單組成，兩類帳戶可任意搭配、多帳戶自動合併：

- **券商 API 帳戶** — 直接串接券商查詢即時持倉。目前支援永豐 Shioaji，
  架構上可擴充其他券商
- **檔案帳戶** — 任何券商匯出的 CSV/Excel 持股清單，無 API 也能對帳

## Skills

| Skill | 用途 |
|---|---|
| `ark-setup` | 帳戶清單設定精靈：管理券商 API 與檔案帳戶，原生視窗安全收集憑證 |
| `ark-start` | 初次上手驗證軌：模擬環境送單驗證委託路徑（不碰真錢），通過後自動串接實際看單 |
| `ark-read` | 靜默讀取各帳戶即時持倉與合併結果（不碰 ARK、不彈視窗、只讀不寫） |
| `ark-collect` | 兩階段同步的階段一：收集各帳戶持倉，確認後寫入當日快照 |
| `ark-sync` | 兩階段同步的階段二：以真實持倉同步 ARK 庫存（API 讀寫為主，AX 為回退） |
| `ark-analyze` | 集中度、與 App 建議部位的偏離、風控檢查、歷史快照復盤 |
| `ark-explore` | 探索全 App 結構，產出功能地圖（以版本號當快取鍵，版本沒變就跳過） |
| `ark-agent` | 自動交易決策軌：盤中決策包、依 ARK 紀律決策並鎖定、自動下單、成交對回、滾動評估 |
| `ark-review` | 決策複盤：把決策、當時理由、成交與前瞻報酬組成案例，依損益維護決策準則 |

| 參考資料 | 用途 |
|---|---|
| `references/ark-app-map.md` | **全 App 功能地圖** — 每一頁在幹嘛、讀得到什麼、什麼時候該用它 |
| `references/ark-app-ux.svg` | UX 流程圖 — 核心資料流與六個 tab 的視覺化 |
| `references/write-api-conversion-assessment.md` | 寫入轉 API 評估 — 端點格式、mitm 實測、read-after-write 設計 |

## 安裝

### Claude Code

```bash
/plugin marketplace add ch3n0297/Ark-Trader
/plugin install ark-trader@ark-trader
```

`@` 後面是 marketplace 名稱（`marketplace.json` 的 `name`，即 `ark-trader`），不是 GitHub repo 名稱。

裝好 plugin 後，**在終端機執行一次安裝腳本**（見下方「一個指令完成安裝」）。

### Codex

```bash
codex plugin marketplace add ch3n0297/Ark-Trader
codex plugin add ark-trader
```

本機開發改用 checkout 路徑：把上面的 `ch3n0297/Ark-Trader` 換成本機路徑。

### 一個指令完成安裝（macOS 終端機）

```bash
bash ~/.claude/plugins/marketplaces/ark-trader/plugins/ark-trader/install.sh
```

Codex 或本機 checkout 改用該處的 `plugins/ark-trader/install.sh`。腳本流程寫死、不需要
coding agent 介入：

1. 確認 macOS
2. 安裝 uv（有 Homebrew 用 brew，否則用官方安裝程式，落在 `~/.local/bin`）
3. 預熱各腳本的 Python 與套件（第一次較久，之後 `uv run` 直接啟動）
4. 檢查終端機的「輔助使用」權限，未授權時打開系統設定並等你授權
5. 執行 ark-setup 設定帳戶（原生視窗，憑證不經對話）

可重複執行；plugin 更新後再跑一次即可。`--skip-setup` 略過第 5 步。

### 每日自動決策排程（選用）

```bash
bash ~/.claude/plugins/marketplaces/ark-trader/plugins/ark-trader/install.sh schedule          # 後端 simulation
bash ~/.claude/plugins/marketplaces/ark-trader/plugins/ark-trader/install.sh schedule --live   # 自動送真錢委託
bash ~/.claude/plugins/marketplaces/ark-trader/plugins/ark-trader/install.sh unschedule        # 移除
```

依這台機器的 uv 路徑、plugin 路徑與 `HOME` 從範本產生 launchd 排程（`local.ark-trader.*`：
平日 10:00 decide、14:30 settle、週五 15:00 review、載入時跑一次 check）。送單後端只由
`--live` 決定；`~/.ark-toolkit/agent.env` 的其他設定一併寫入，改了要重跑 `schedule`。

決策層預設呼叫 `claude -p`；在 agent.env 寫 `ARK_DECISION_CLI=codex` 改用 Codex CLI 的
`codex exec`（唯讀沙箱、不讀個人 `config.toml`、推理強度寫死 `xhigh`，`ARK_DECISION_MODEL`
未設就用 Codex 預設模型）。兩者都只產出決策 JSON，送單仍由排程的 execute.py 負責；週五的複盤層固定走 claude。
細節見 ark-agent 的 SKILL.md「決策層 CLI」。
`schedule --dry-run` 只印出產生的 plist。切 live 前先用 ark-start 驗證委託路徑。

**正式下單只由你觸發**：排程，或你自己在終端機執行 `daily.py trade`（對 agent 說「開始交易」
時，它會給你指令並監看回報，不會在對話中自己送單）。

### 更新

```bash
/plugin marketplace update ark-trader          # Claude Code
codex plugin marketplace upgrade ark-trader    # Codex
```

### 從舊名稱遷移（2.0.1 以前為 `ark-toolkit@ark-tools`）

plugin 與 marketplace 已統一改名為 `ark-trader`，舊安裝不會自動跟上，需移除後重裝：

```bash
/plugin uninstall ark-toolkit@ark-tools
/plugin marketplace remove ark-tools
/plugin marketplace add ch3n0297/Ark-Trader
/plugin install ark-trader@ark-trader
```

設定與資料仍在 `~/.ark-toolkit/`（帳戶、憑證、決策日誌），路徑不變，重裝後直接沿用。

## 前置需求

- **macOS**（Apple Silicon；依賴 iOS App 相容層）
- 方舟運算已安裝並登入（未開啟會自動代為啟動）
- 終端機/IDE 已取得「輔助使用」權限（系統設定 > 隱私權與安全性 > 輔助使用）
- [uv](https://docs.astral.sh/uv/) — 各腳本以 PEP 723 宣告依賴，`uv run` 會自動準備
  Python 與套件（由 `install.sh` 安裝）

## 快速開始

初次使用：`install.sh`（含 `ark-setup` 設定帳戶）> `ark-start` 驗證（模擬單通過後自動接實際看單）。
以下為從 repo checkout 根目錄手動執行的指令：

```bash
uv run plugins/ark-trader/skills/ark-setup/setup.py             # 帳戶清單設定
uv run plugins/ark-trader/skills/ark-start/verify.py            # 模擬單驗證（不碰真錢）
uv run plugins/ark-trader/skills/ark-read/read.py               # 靜默看各帳戶持倉
uv run plugins/ark-trader/skills/ark-sync/sync.py --dry-run     # 看 ARK 與真實持倉的差異
uv run plugins/ark-trader/skills/ark-analyze/analyze.py         # 看風險
```

### 寫入後端切換

ark-sync 寫入預設走 AX（有 App「總成本」見證）。切換到 API 寫入：

```bash
ARK_API_WRITES=1 uv run plugins/ark-trader/skills/ark-sync/sync.py --dry-run   # 先 dry-run 確認
ARK_API_WRITES=1 uv run plugins/ark-trader/skills/ark-sync/sync.py             # 實際執行
```

API 寫入每筆帶 read-after-write 驗證（寫入後讀回比對最終狀態），取代 AX 的「總成本」見證。

### API 端點回歸（App 更新後）

App 改版時用 `ark_capture.py`（需 mitmproxy）驗證端點是否變更：

```bash
python3 ark_capture.py up        # 啟動 mitm 攔截
# 在 App 上操作一輪讀寫
python3 ark_capture.py diff      # 比對基準，顯示新增/移除/變更的端點
python3 ark_capture.py down      # 關閉攔截、移除 CA
```

## 測試

純邏輯測試不需要 ARK 執行中，任何平台皆可跑：

```bash
cd plugins/ark-trader/lib                && uv run --no-project --python 3.13 --with openpyxl python -m unittest test_source test_dialogs test_arkapi
cd plugins/ark-trader/lib                && uv run --no-project --python 3.13 --with pyobjc-framework-cocoa --with pyobjc-framework-applicationservices --with pyobjc-framework-quartz python -m unittest test_ax
cd plugins/ark-trader/skills/ark-collect && PYTHONPATH=../../lib uv run --no-project --python 3.13 --with openpyxl --with python-dotenv python -m unittest test_collect
cd plugins/ark-trader/skills/ark-read    && PYTHONPATH=../../lib uv run --no-project --python 3.13 --with openpyxl --with python-dotenv python -m unittest test_read
cd plugins/ark-trader/skills/ark-sync    && PYTHONPATH=../../lib uv run --no-project --python 3.13 python -m unittest test_sync
cd plugins/ark-trader/skills/ark-analyze && PYTHONPATH=../../lib uv run --no-project --python 3.13 python -m unittest test_analyze
cd plugins/ark-trader/skills/ark-explore && PYTHONPATH=../../lib uv run --no-project --python 3.13 python -m unittest test_explore
cd plugins/ark-trader/skills/ark-setup   && PYTHONPATH=../../lib uv run --no-project --python 3.13 --with openpyxl --with python-dotenv python -m unittest test_setup
cd plugins/ark-trader/skills/ark-agent   && PYTHONPATH=../../lib uv run --no-project --python 3.13 python -m unittest test_market test_packet test_journal test_evaluate test_risk test_tracks test_equity test_execute test_daily test_phase0 test_review test_dividends test_gates test_schedule test_choose_cli
cd plugins/ark-trader/skills/ark-agent   && PYTHONPATH=../../lib uv run --no-project --python 3.13 --with pyobjc-framework-cocoa --with pyobjc-framework-applicationservices --with pyobjc-framework-quartz python -m unittest test_record_return
```

## 通用性原則（給使用本 plugin 的 coding agent）

本 plugin 會發佈給其他使用者，內容必須保持通用：

- **個人化內容不進 plugin** — 帳戶設定、排程參數、專案/實驗協定、一次性調整，
  一律放 `~/.ark-toolkit/` 或專案 `docs/`，不要直接修改 plugin。
- **只有通用的修復與功能才改 plugin 本身**，並附測試；註解寫通用理由，不寫個人脈絡。
- 判斷標準：**換一個使用者、換一個帳戶，這段內容還成立嗎？** 不成立就不屬於這裡。

## 設計要點

- **API 優先**：讀寫操作預設走 ARK 後端 API，不受 App UI 改版影響。API 不可用時
  自動回退 AX（Accessibility API 操作 UI），維持可用性。
- **交叉驗證**：API 與 AX 同時可用時交叉比對，不一致即停手不寫入。
- **寫入安全**：API 寫入帶 read-after-write 驗證；AX 寫入以 App「總成本」交叉驗算。
  雙重開關（`confirm=True` + `ARK_API_WRITES=1`）防止誤觸。
- **券商下單獨立**：真正的買賣走 Shioaji 券商 API，永不經 ARK 端點。ARK 寫入只更新
  建議 App 的顯示資料，不是交易。
- 分析只陳述事實與偏離量，不產出買賣建議。
