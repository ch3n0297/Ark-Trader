---
name: ark-agent
description: 自動交易決策軌：盤中產生決策包、Agent 依 ARK 紀律做出買賣決策並鎖定、自動下單、成交對回、滾動評估；也可由人觸發一場做完（對帳 → 決策送單 → 等成交 → 當日結算）。當使用者說「開始交易」「跑今天的決策」「產生決策包」「結算」「看評估成績」時使用。僅限 macOS。
---

# ark-agent

> **通用性原則**：本 plugin 是通用工具，會發佈給其他使用者。個人化內容
> （帳戶設定、排程參數、專案協定、一次性調整）放執行資料夾 `~/.ark-toolkit/`
> 或專案自己的 `docs/`，**不要直接改 plugin**；只有通用的修復與功能
> （附測試）才動 plugin 本身。

一條**自動化的每日交易決策軌**：AI Agent 讀取 ARK 讀數與帳戶事實，依 ARK 紀律
做出買賣決策並鎖定，自動下單，盤後對回成交並滾動評估。

**買賣建議只存在於這裡的 journal。** ark-analyze 的設計不變量（只陳述事實、
不產出買賣建議，有測試把關）不受影響，也不可反向污染。

## 運作節奏

- **節奏**：每個工作日 **10:00 盤中決策並鎖定**、**14:30 盤後結算**，由 launchd 自動觸發。
  取 10:00 是因為官方教學明載「指標約每小時重算、**台股盤中算出調台股**」，
  盤前讀到的是前一日盤後的舊值；10:00 也避開 09:00–09:30 的開盤波動
- **執行**：系統自動下單，無人否決（安全由硬性上限與熔斷保證，不由人工把關）
- **評估**：每筆決策的 5/20/60 日前瞻報酬、勝率、對照同期 0050 的超額報酬、ARK 紀律遵循度
- **複盤**：每週五 15:00 自動（`daily.py review`）：本週案例 → 複盤層檢討「當初為什麼、
  結果如何、買哪一檔對不對」→ 更新準則檔 → 驗準則檔完整 → commit。詳見 ark-review skill

## 資訊邊界（Agent 必讀，半開放）

**允許**：
- 當日決策包（packet）的全部內容
- 對 packet `news_scope.codes` 清單內的標的**與其產業**做新聞深度／廣度搜尋

**禁止**：
- 全市場新聞掃描、不在清單內的個股研究
- 盤中即時資訊——決策在盤前鎖定，之後不得修改

違反邊界做出的決策視為無效。搜尋過的新聞要在 decision JSON 的
`news_used` 裡留下摘要與來源，供事後稽核。

## 紀律限制（完全比照 ARK，語意出處：`../../references/ark-app-map.md`）

- 只做現股；**檔數公式**：(持股市值＋閒錢) ÷ 10 萬，90 萬以上封頂 9 檔。
  `ARK_NAMES_FLOOR` 可抬高下限（預設 1＝純公式）——這是對紀律的明示偏離，
  `discipline.names_floor` 會記下來源，紀律報告看得出來。
  `ARK_NAMES_CAP=advisory` 可把上限改成參考值（預設 `hard`）：journal 不再因檔數
  拒單，`envelope.core.names_cap` 讓決策層知道要自己判斷檔數；餵給 App 位階
  運算機的檔數不受影響
- **調節＝賣出側**：挑幾檔把調節金額加總**覆蓋運算頁「參考調節金額」**即可，
  不必每檔照做；**獲利才調節**（虧損賣出侵蝕本金，App 紀律不做）；升溫區優先
- 個股桶燈號：`envelope.satellite.lamps` 依兩個來源（調節庫存列位階、產業價值區／升溫區名單）
  判 `value_only`／`value_heating`／`heating_only`／`none`／`unknown`；任一來源缺失即 `unknown`
  ——缺值不等於無燈號。`ARK_LAMP_EXIT`（`all` 或逗號分隔代號；預設不設＝關閉）開啟後，
  範圍內「確認無燈號」的部位列入 `lamp_exits`，journal 要求整筆賣出
- 國定假日：packet 先查基準當日行情，休市離開碼 3，daily 視為今日不交易
- **布局＝買進側**：只挑價值區標的
- **App 顯示參考調節（>0）時，先調節才可買**

這些已算成 packet 的 `discipline` 區塊（機器可查的邊界），`journal.py record`
會以硬規則驗證：違規拒絕寫入，`--override` 可強制寫入但 `violations` 照記、紀律報告照列。
自由度（賣哪幾檔、股數與建議的偏離、限價區間）不擋，事後由 evaluate 量測。

## 執行環境（uv）與腳本路徑

所有指令以 `uv run` 執行：依賴宣告在腳本開頭（PEP 723），uv 會自動準備 Python 與套件，
機器不需要預先安裝 Python。

- **腳本路徑**：指令中的 `${CLAUDE_PLUGIN_ROOT}` 是本 plugin 的根目錄（Claude Code 會自動代入，
  即本 SKILL.md 往上兩層）。Codex 或其他 agent 若沒有這個變數：腳本在本 SKILL.md 所在資料夾
  （其他 skill 的腳本在同層的 `../<skill>/`），請換算成絕對路徑執行。**不要用相對路徑**——
  agent 的工作目錄是使用者的專案，`uv run skills/...` 會 `Failed to spawn: No such file`。
- **找不到 uv**（`command -v uv` 與 `~/.local/bin/uv` 都沒有）：不要自行安裝，請使用者在
  終端機執行一次安裝腳本（路徑換成絕對路徑給使用者），跑完再繼續：

  ```bash
  bash "${CLAUDE_PLUGIN_ROOT}/install.sh"
  ```

## 每日流程

整條流程由 `daily.py` 依序驅動，**順序寫死、不可跳過**。Agent 只佔第 3 步，
而且那一步**不給 Bash 工具**——它產出 decision.json 之後就沒有話語權。若讓 LLM
自己編排，它可能「忘記」跑風控或驗證，硬規則就不再是硬的。

> **多帳戶注意**：帳戶清單含檔案帳戶（CSV/Excel）時，第 1 步的自動 sync
> 需要**當日已跑過 ark-collect**（快照僅當日有效），否則會停下要求先收集。
> 全自動排程建議帳戶清單只留永豐 Shioaji。

下面是 `daily.py` 內部依序執行的步驟，列出來是為了讓你看得懂 log、知道每步在做什麼，
**不是要你在對話中逐一執行**。會送出委託的步驟（`execute.py`、`daily.py decide`／`trade`、
`phase0.py --execute`）一律由排程或使用者在終端機觸發，見下方「下單觸發點」。

```bash
# 10:00 決策執行（daily.py decide）
uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-agent/packet.py"            # 1. 事實（自動含：對帳不一致先 sync）
uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-agent/risk.py"              # 2. 執行邊界 envelope
#                                              3. ★ Agent 讀 packet+envelope → decision JSON
uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-agent/journal.py" record /tmp/decision.json   # 4. 紀律驗證＋鎖定
uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-agent/execute.py" --backend "$ARK_BACKEND"   # 5. 送單（僅 daily.py 呼叫）

# 14:30 盤後結算（daily.py settle 依序跑這五步，每步盡力做完再回報）
uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-agent/journal.py" settle    # 1. 成交對回
uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-agent/equity.py"            # 2. 淨值與熔斷狀態
uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-agent/dividends.py"         # 3. 除權息資料（報酬校正用，須每日累積）
uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-sync/sync.py" --allow-delete --with-cash   # 4. 庫存與現金同步回 ARK
uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-agent/record_return.py"     # 5. 當日已實現獲利記進離職倒數（僅獲利日）

# 不用排程、由人說「開始交易」才動：一場做完（盤中 09:00–13:00 開始；使用者在終端機執行）
uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-agent/daily.py" trade       # 對帳統一（sync --with-cash）→ 決策執行五步
#                                              → execute --wait 30（等成交，逾時刪單並記 cancel）
#                                              → 當日結算五步（journal settle --same-day 起）
#                                              當日已有決策即拒絕；--wait 可調等待分鐘

# 週五 15:00 複盤（daily.py review 依序跑：案例 → 複盤層 → 驗準則檔 → commit）
uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-agent/review.py" --out ~/.ark-toolkit/agent/reviews/<date>.txt
#                                              ★ 複盤層讀案例 → 寫檢討記錄、更新準則檔
#                                              rules_intact 驗編號一條不少，壞了從備份還原

# 隨時看成績
uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-agent/evaluate.py"          # --json 出機器格式；--offline 不連線
```

`journal.py settle` 現在會寫 `satellite_exits.jsonl`（衛星軌停損連續冷卻的依據），
並把成交的個股桶買單自動指派進 `tracks.json` 的 satellite 軌；週五複盤可在
`gates.py` 的區間與步幅內調整 `gates.json`，超出硬邊界由 `daily.py review` 自動還原。

## 下單觸發點（硬規則）

**你在對話中不執行任何會送出委託的指令**：`execute.py`、`daily.py decide`、`daily.py trade`、
`phase0.py --execute`，不論 `ARK_BACKEND` 是 simulation 還是 live 都一樣——後端由使用者的
`~/.ark-toolkit/agent.env` 與排程決定，對話中無法保證當下不是真錢。也不代為安裝或改動排程
（`install.sh schedule`）。正式下單只有兩個觸發點，都在使用者手上：

1. **排程**：使用者在終端機執行 `install.sh schedule`（見下方「排程安裝」）
2. **手動一場**：使用者在終端機執行 `daily.py trade`（見下一節）

只讀與驗證類指令照常可以由你執行：`packet.py`、`risk.py`、`evaluate.py`、`review.py`、
`journal.py show`、`daily.py check`、`phase0.py`（不帶 `--execute`）、ark-start 的 `verify.py`
（寫死 simulation）。

## 手動一場（使用者說「開始交易」）

不用排程、由人觸發時走 `daily.py trade`。順序由程式寫死；你**只負責給指令、監看、回報**：
不自己執行、不自己送單、不改 journal、不重跑第二場。

1. **先看時間**：交易日 **09:00–13:00** 才能開始（留 30 分等單＋結算，13:30 收盤前做完）。
   不在窗內就告訴使用者現在幾點、為什麼不跑；**不要**建議用 `ARK_FORCE_WINDOW` 繞過。
2. **把指令交給使用者，請他在終端機執行**。先把 `${CLAUDE_PLUGIN_ROOT}` 換成實際的絕對路徑
   再給（使用者的終端機沒有這個變數）。指令會先載入 `~/.ark-toolkit/agent.env` 的個人化設定
   （`ARK_RULES`、`ARK_NAMES_FLOOR`、`ARK_NAMES_CAP`、`ARK_DECISION_CLI`、`ARK_DECISION_MODEL`、
   `ARK_BACKEND`、`ARK_LAMP_EXIT`；沒有就走預設，`ARK_BACKEND` 預設 simulation），並以 `caffeinate` 防睡眠：

   ```bash
   [ -f ~/.ark-toolkit/agent.env ] && { set -a; source ~/.ark-toolkit/agent.env; set +a; }; caffeinate -dis uv run "${CLAUDE_PLUGIN_ROOT}/skills/ark-agent/daily.py" trade
   ```

   `--wait N` 可改等待成交的分鐘數。一場約 5–40 分（決策層約 5 分＋等成交最多 30 分）。
   給完指令先問清楚使用者的 `ARK_BACKEND` 是 simulation 還是 live，回報時要講明。
3. **使用者說已經開始後，監看** `~/.ark-toolkit/agent/daily.log` 尾段（只讀），三個檢查點：
   - packet 摘要行「準則 N 條」須等於 `grep -c '^### R-' "$ARK_RULES"`
     （未設時為 `~/.ark-toolkit/decision-rules.md`）；不等代表 env 沒載到，**立刻請使用者在
     該終端機按 Ctrl-C 中止**——這時決策尚未鎖定，重來不留痕
   - envelope 行的熔斷等級與可買／可賣
   - execute 每腿的最終狀態（`→ Filled` 或 `（已刪單）`）——那就是券商回報
4. **結束後回報**：讀 journal 當日的 `decision`／`execution`／`cancel`／`fill` 記錄，列出每筆單
   （動作、代號、股數、限價、成交價）、決策理由摘要、未成交或刪單的腿、結算五步有沒有哪步失敗。
5. **`daily.py trade` 拒絕時**（當日已有決策、不在時窗、對帳同步失敗）那是正確行為，照實回報
   原因，不要建議繞過。同日只認第一筆決策。

`trade` 與 `decide`／`settle` 排程互斥：兩者都會做決策，同日第二筆會被 `trade` 拒絕、
被排程標 `amended`。要改用 `trade`，請使用者先在終端機執行 `install.sh unschedule` 移除排程。

## 排程安裝（launchd）

排程由使用者在終端機執行安裝腳本的子指令，依這台機器的 uv 路徑、plugin 路徑與 `HOME`
從 `launchd/*.plist.in` 範本產生 plist 並載入（label 前綴 `local.ark-trader.`）。你只把指令
（換成絕對路徑後）交給使用者，不代為執行：

```bash
bash "${CLAUDE_PLUGIN_ROOT}/install.sh" schedule          # decide／settle／review／check，後端 simulation
bash "${CLAUDE_PLUGIN_ROOT}/install.sh" schedule --live   # 自動送真錢委託（切 live 前先跑 ark-start）
bash "${CLAUDE_PLUGIN_ROOT}/install.sh" unschedule        # 移除全部排程
```

- 送單後端只由 `--live` 決定；`~/.ark-toolkit/agent.env` 的其他設定（`ARK_RULES`、
  `ARK_DECISION_CLI`、`ARK_DECISION_MODEL` 等）在產生 plist 時一併寫入，改了 agent.env 要重跑 `schedule`
- PATH 帶入決策層 CLI（claude 或 codex）所在目錄；有排 review 時另帶 claude。找不到時
  `schedule` 會警告，裝好後重跑

## 決策層 CLI（`ARK_DECISION_CLI`）

decide／trade 第 3 步呼叫哪支模型 CLI，由 `~/.ark-toolkit/agent.env` 寫死，不由對話中的
agent 判斷。兩種都**只產出決策 JSON、不送單**——紀律驗證與送單仍是後面的 journal／execute：

| `ARK_DECISION_CLI` | 指令 | 權限 | `ARK_DECISION_MODEL` 未設時 |
|---|---|---|---|
| `claude`（預設） | `claude -p` | 只給 Read／Write／WebSearch／WebFetch，不給 Bash；模型自己寫決策檔 | `fable`（額度耗盡改 `ARK_DECISION_FALLBACK`，預設 `opus`） |
| `codex` | `codex exec --sandbox read-only --ignore-user-config --skip-git-repo-check -c web_search="live" -c model_reasoning_effort="xhigh"` | 唯讀沙箱：寫不了檔、指令連不了網；決策 JSON 由最終訊息（stdout）交回，`daily.py` 代寫決策檔 | 不帶 `-m`，用 Codex 預設模型；不做額度降級 |

```bash
# ~/.ark-toolkit/agent.env
ARK_DECISION_CLI=codex
# ARK_DECISION_MODEL=<codex 的模型名>   # 不設就用 Codex 預設
```

- **用視窗選**：`install.sh` 初次安裝的第 6 步會跳出 macOS 視窗選 CLI 與模型（claude：fable／opus；codex：gpt-6-astra／Codex 預設），寫進 agent.env；已設定就不打擾。之後要換執行 `bash "${CLAUDE_PLUGIN_ROOT}/install.sh" decision`（交給使用者在終端機跑）。推理強度不在視窗選，codex 固定 xhigh
- `--ignore-user-config`：不讀 `$CODEX_HOME/config.toml`，排程結果不隨個人設定漂移
  （登入仍沿用 `CODEX_HOME` 的憑證，排程前先在終端機登入 codex）。個人設定裡的沙箱、
  核准、模型與推理強度都不會帶進來
- 推理強度寫死 `xhigh`：不讀個人設定時預設會落到 `none`
- stdin 一律接 `/dev/null`：`codex exec` 見到非 TTY 的 stdin 會等讀到 EOF 才開始，繼承到
  不會關的 pipe 就永遠卡住
- codex 的最終訊息必須是單一 JSON 物件（允許整段包在一個 json code fence 裡）；夾雜說明文字就
  視為決策失敗、不寫檔、不送單
- **週複盤（review）固定走 claude**：它要 Edit 準則檔，codex 決策層的唯讀沙箱做不到。
  codex 模式下複盤用 claude 的預設模型 `fable`；`daily.py check` 缺 claude 只記警告、不擋決策
- 其他值（例如拼錯）`daily.py` 在 decide／trade／check 任何一步之前就擋下，`schedule` 拒絕安裝
- plist 經由 `~/.ark-toolkit/plugin`（指向 plugin 根目錄的連結）呼叫 `daily.py`，plugin 更新後
  重跑一次 `install.sh` 即可讓排程跟上
- `check` 載入時立即跑一次前置檢查（真正的 launchd 環境下驗 AX 權限與券商連線），結果看
  `~/.ark-toolkit/agent/launchd.check.out`；之後要重驗：
  `launchctl kickstart -k gui/$(id -u)/local.ark-trader.check`
- `~/Library/LaunchAgents/` 裡若有其他名稱的 ark-agent 舊排程，`schedule` 會拒絕安裝並印出
  移除指令——兩套排程同時跑會在同一天做兩次決策

⚠️ **launchd 的進入點必須是 `uv`，不能是 `/bin/bash`。** macOS 封鎖系統二進位檔
在 launchd 下存取 `~/Documents` 等 TCC 保護目錄（防止用腳本繞過權限）——實測
`/bin/bash` 連讀取 `daily.py` 都會 `Operation not permitted`（離開碼 126）。
`uv` 這類第三方 binary 有自己的 TCC 身分，讀得到。AX（輔助使用）權限在 launchd
下則正常，實測 `AXIsProcessTrusted() = True`。

`decide` 不補跑：10:00 的判斷在 13:00 執行是另一回事，盤中價格早已不同（`daily.py` 的
`in_window()` 守 ±60 分）。`settle` 會補跑——那是對既成事實的記錄，
晚幾小時做結果一樣，漏做卻會讓熔斷基準斷掉。

## 軌道模型

`~/.ark-toolkit/agent/tracks.json` 指定每個部位屬於哪一軌，決定它適用哪套規則。
個股桶新倉由 ARK 候選供應，成交後 settle 自動寫進 tracks.json；`inherited`／`frozen`
的例外仍由人手動指定。

| 軌道 | ARK 紀律 | 停損 | 說明 |
|---|---|---|---|
| `core` | 完全適用 | 無 | 只做 ETF。預設軌，evaluate 只統計這一軌 |
| `satellite` | **豁免** | 依 `gates.json`（起始 −12%） | 個股桶：候選來自 ARK 產業價值區（唯讀 API），配額＝總資源×建議持股比×佔投入比（起始 50%），單檔／同產業硬上限（起始 12%／25%），超限列減碼候選 |
| `inherited` | — | — | 虧損中且 ARK 不可賣，凍結至轉正；**不佔衛星軌配額** |
| `frozen` | — | — | 低於最小可交易金額，不進出場計算；已離開價值區的碎片可**整筆清掉**（不論盈虧，`track: "frozen"` 賣單） |

**「先調節才可買」兩軌都適用**：那是 ARK 的帳戶級曝險訊號，若衛星軌能在被要求
調節時加碼，帳戶總曝險就失控了。

## 失效保護：預設永遠是「不交易」

任一條成立即跳過當日交易並記錄原因，**絕不猜測**：ARK App 未開／AX 讀取失敗／
`check_parsed` 不過／**運算頁讀不到（隱私眼睛開啟會讓 `read_posture` 靜默回
`None`，紀律邊界隨即退化成現金為零、不需調節）**／對帳不一致／Shioaji 登入或
CA 啟用失敗／`packet_hash` 不符／熔斷 L2 已觸發／當日已有決策。

看到 `news_scope` 標的的除權息公告時，補一行到 `~/.ark-toolkit/agent/dividends.jsonl`：
`{"code": "0056", "ex_date": "2026-08-19", "cash": 1.2, "stock_ratio": 0}`
（0050 的配息也要記——它是基準，1 月與 7 月除息，60 日視窗大概率遇到。）

## decision JSON 格式

```json
{
  "date": "2026-08-10",
  "packet_hash": "sha256:…（抄當日 packet 的 hash，綁定決策依據）",
  "no_trade": false,
  "orders": [
    {"action": "sell", "code": "0057", "qty": 76,
     "limit_low": 305.0, "limit_high": 310.0,
     "ark_basis": {"signal": "suggest_qty", "value": 76, "tiers": ["價值", "升溫"]},
     "rules_applied": ["R-001"],
     "reason": "升溫且獲利，單檔即覆蓋參考調節金額的一半"}
  ],
  "rationale": "整體判斷理由，引用 posture.gap 與位階警示",
  "rules_applied": ["R-001", "R-003"],
  "news_used": [{"code": "0057", "summary": "…", "source": "…"}]
}
```

無操作也要記：`"no_trade": true, "orders": []`——「今天不動」也是一筆決策。

`rules_applied` 有兩層：委託上的只列決定了那筆單的準則，複盤據此逐筆歸因損益；
決策層的是當天用到的全部（含沒產生委託的）。委託上沒有這個欄位的舊紀錄，複盤退回
決策層清單。

## 鎖定機制

- journal 為 **append-only** JSONL；每筆決策帶 `lock`（內容雜湊）與 `packet_hash`
- **first-decision-wins**：同日第二筆標 `amended`，evaluate 只認第一筆；
  修訂不會被隱藏，紀律報告照列
- 缺席（沒跑 packet 或沒做決策）由隔日的 settle 自動記 `missed`，不回填

## 邊界情況

- **週末**：packet 拒跑（`--force` 可強制）；**國定假日**：settle 以 0050 當日
  有無行情判定休市，不會誤記缺席
- **停牌**：evaluate 標 `insufficient_bars`（退用視窗內最後可用價）或
  `unevaluable`，剔除彙總但筆數照報
- **未成交**：退用決策日收盤價評估並標 `no_fill`；滑價照錄

## 統計誠實條款

決策日樣本少、前瞻視窗互相重疊（自相關），**不做統計檢定、不宣稱優劣的定論**。
evaluate 只輸出點估計＋樣本數＋pending 筆數；60 日前瞻報酬要等決策後再
60 個交易日才齊，`pending` 常態存在。
測試以 `test_評估輸出不含顯著性宣稱用語` 把關。

## 檔案

- `packet.py` — 盤前決策包（ARK 讀數＋帳戶＋行情＋紀律邊界 → JSON）
- `journal.py` — 決策鎖定（record）、成交對回（settle）、日誌（show）
- `evaluate.py` — 四類指標滾動評估
- `market.py` — Shioaji 行情層（單一 session、分K→日K 聚合、快取）
- `tracks.py` — 軌道歸屬（core／satellite／inherited／frozen），決定每個部位適用哪套規則
- `equity.py` — 每日淨值曲線與熔斷判定（基準為歷史峰值，非期初本金）
- `risk.py` — 風控層：熔斷、軌道配額、衛星軌停損、金額上限 → 執行邊界 envelope
- `gates.py` — 個股桶閘門（預設、區間、步幅、檔案驗證）；閘門檔 gates.json 預設放在準則檔旁
- `execute.py` — 下單層：股數→張數換算、價格檔位對齊、送出前檢查、`place_order`
- `daily.py` — 每日編排（decide／settle／trade／review／check），順序寫死；launchd 的進入點
- `schedule.py` — 由 `launchd/*.plist.in` 範本產生並載入／移除排程（`install.sh schedule` 呼叫）
- `preflight.py` — 前置檢查（AX 權限、券商連線），無副作用只讀
- `phase0.py` — 開局整理（一次性、有人監督、預設 dry-run）。**豁免金額上限**，
  因為那些上限是為了限制無人值守時的爆炸半徑；紀律照守，繼承軌一股不賣。
  刻意獨立成一支腳本而非在 execute.py 開繞過旗標——那種旗標日後一定會被誤用
- `prompts/decide.md`／`prompts/review.md` — 決策層與複盤層的提示模板（codex 模式另在
  `daily.py` 接上「改由最終訊息交回 JSON」的段落）；`launchd/` — 四份排程範本（`*.plist.in`）
- 委託路徑實機驗證在 **ark-start** skill（`skills/ark-start/verify.py`，寫死 simulation，
  碰不到真錢）——切 live 前先跑它
- 持久化：`~/.ark-toolkit/agent/`（packets/、envelopes/、prices/、journal.jsonl、
  equity.jsonl、tracks.json、satellite_exits.jsonl、dividends.jsonl、sectors.json）

測試（純邏輯，不需要 ARK 或 Shioaji，任何平台可跑）：

```bash
cd skills/ark-agent && PYTHONPATH=../../lib uv run --no-project --python 3.13 python -m unittest test_market test_packet test_journal test_evaluate test_risk test_tracks test_equity test_execute test_daily test_phase0 test_gates test_review test_schedule test_choose_cli -v
```

## 前置需求

與 ark-sync 相同：macOS、方舟運算已安裝並登入（未開啟會代為啟動）、
終端機已取得「輔助使用」權限、已執行 `ark-setup` 設定 Shioaji 來源。
執行 packet 期間 App 需保持前景。
