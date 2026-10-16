# Changelog

本檔記錄 ark-trader（原名 ark-toolkit）的版本沿革。版本語意：主版號＝正式里程碑，
次版號＝行為或語意變更，修訂號＝bug 修正。

## [Unreleased]

- **改名：plugin 與 marketplace 統一為 `ark-trader`**（原 plugin `ark-toolkit`、marketplace
  `ark-tools`），與 GitHub repo `Ark-Trader` 一致。安裝指令改為
  `/plugin install ark-trader@ark-trader`；舊 README 寫的 `ark-toolkit@Ark-Trader` 拿 repo
  名稱當 marketplace 名稱，新機器照做必定失敗。舊安裝需移除後重裝（見 README「從舊名稱遷移」）。
  資料目錄 `~/.ark-toolkit/` 不改名，既有帳戶、憑證與決策日誌照常沿用。
- **一個指令完成安裝：`install.sh`**（plugin 根目錄）。使用者在終端機執行，流程寫死：
  確認 macOS → 安裝 uv（brew，失敗改官方安裝程式）→ 預熱各腳本的 Python 與套件 →
  檢查輔助使用權限（未授權時開系統設定並等待）→ 執行 ark-setup。取代過去由各 skill
  讓 agent 自行判斷「沒有 uv 就裝」的做法，每台機器結果一致。
- **SKILL.md 腳本改用 `${CLAUDE_PLUGIN_ROOT}` 絕對路徑**：原本寫 `uv run skills/...`
  相對路徑，plugin 裝好後 agent 的工作目錄是使用者專案，必定 `Failed to spawn: No such
  file`。另附給 Codex／其他 agent 的換算說明；找不到 uv 時改為請使用者跑 `install.sh`。
- **launchd 排程改為範本＋產生器**：`launchd/*.plist.in` 以佔位符取代寫死的個人路徑、
  `/opt/homebrew/bin/uv` 與個人 label；`install.sh schedule` 依這台機器的 uv 路徑、plugin
  連結（`~/.ark-toolkit/plugin`）與 `HOME` 產生並載入，label 改為 `local.ark-trader.*`，
  `install.sh unschedule` 移除。送單後端只由 `--live` 決定（預設 simulation，agent.env 的
  `ARK_BACKEND` 不影響排程），agent.env 其他設定一併寫入 plist；偵測到其他名稱的
  ark-agent 舊排程即拒絕安裝並印出移除指令。新增 `schedule.py`／`test_schedule.py`。
- **正式下單的觸發點交還使用者**：ark-agent 的「開始交易」改為 agent 只給出終端機指令、
  監看 log 並回報；agent 在對話中不執行 `execute.py`、`daily.py decide`／`trade`、
  `phase0.py --execute`，也不代為安裝排程。
- **ark-agent：決策層可改用 Codex CLI（`ARK_DECISION_CLI=codex`）**。預設仍是 `claude -p`；
  設成 `codex` 時 decide／trade 第 3 步改呼叫 `codex exec --sandbox read-only
  --ignore-user-config --skip-git-repo-check -c web_search="live"
  -c model_reasoning_effort="xhigh"`（不讀個人設定時推理強度預設是 none，實測），
  `ARK_DECISION_MODEL` 未設就不帶 `-m`（用 Codex 預設模型）、不做額度降級。stdin 一律接
  `/dev/null`——實測 `codex exec` 遇到不會關的 stdin pipe 會一直等下去。唯讀沙箱寫不了檔，決策 JSON 由最終訊息
  （stdout，進度在 stderr）交回、`daily.py` 代寫決策檔後照常交 journal 驗證；輸出不是單一 JSON
  物件就視為失敗、不送單。指令組成抽成純函式 `model_command`，測試守住「codex 分支不帶
  workspace-write／danger-full-access／免沙箱旗標」。`daily.py check` 與 `schedule` 跟著切換
  找對應的 CLI 並帶進 launchd PATH；週複盤要改準則檔，固定走 claude（codex 模式下用 `fable`）。
  不認得的 `ARK_DECISION_CLI` 在任何一步之前擋下。`ARK_DECISION_MODEL` 設為空字串時改視同未設
  （過去會帶 `--model ""`）。

## [2.0.1] - 2026-10-14

- **ark-sync：API 寫成庫存後重啟 App，讓畫面跟上伺服器**（`sync.screen_stale_after`／
  `refresh_screen`）。API 直達伺服器，App 要冷啟動才重抓庫存列表；畫面落後時下一場的
  畫面見證必與伺服器不符而整場停手。重啟失敗只警告、不推翻已讀回驗證的寫入。
  實例：2026-10-13 14:30 以 API 新增 2317，排程重啟（14:25）在寫入之前，10/14 10:00
  決策包讀到畫面 17 檔 vs 伺服器 18 檔，執行邊界全面阻擋。

- **ark-sync：API 寫入時畫面不再是必經關卡**。`ARK_API_WRITES=1` 且 API 讀得到時，
  喚醒／殭屍探測／翻頁讀整段改為見證、限時 120 秒（`sync.read_ark_side`），報錯或
  超時就略過見證、只靠 API 繼續；畫面是寫入後端或唯一來源時照舊失敗即中止。
  實例：2026-10-12 結算碰上 App 更新（`ensure_responsive` 失敗、整場同步放棄）、
  2026-10-13 兩次 dry-run 各卡在 `ensure_ready`／`ensure_responsive` 五分鐘。

- 測試：`test_daily` 以模組層 `setUpModule` 把 `daily.STATE`／`LOG` 指到暫存目錄，
  `guard_gates` 等會寫日誌的路徑不再落到使用者的 `~/.ark-toolkit/agent/daily.log`
  （自 2026-09-09 起正式日誌已混進 42 行 `[test]` 還原訊息）；`TestLogIsolation` 守住。

## [2.0.0] - 2026-10-13

- **ark-sync 的 ARK 現況主來源改為 API**。原本以調節庫存頁翻頁（`ark.read_holdings`）
  讀 ARK 現有庫存，再拿 `arkapi.positions` 當交叉驗證；本版對調：主來源改為 API
  （唯讀、無畫面、不受 1.8.0／1.8.1 那類翻頁漏列或列格式改版影響），畫面讀取降為
  **獨立見證**——API 在時仍讀一次畫面比對，讀失敗不擋（API 不再依賴列解析器）；
  兩來源不符仍一律停手不寫入，維持 1.6.1 以來「兩個獨立來源」的安全網。API 不可用
  （App 沒開／token 過期／沒網路）時自動回退為原本的畫面主讀，含 App 宣告檔數交叉
  驗證。取捨邏輯收斂到純函式 `sync.choose_current`，附五個案例的回歸測試。
- **arkapi 補齊五個唯讀端點**（端點、body、回應欄位均取自 App 1.9.3 自己呼叫過、留在
  URL 快取裡的紀錄，非猜測）：`points`（目前點數）、`exchange_rate`（美元兌台幣匯率）、
  `asset_daily`（每日總資產序列）、`stock_data`（一批代號的盤中 K9／淨值／折溢價…）、
  `common_config`（共用設定），外加 `strategy_ids`（策略分區清單）。全部走既有唯讀
  白名單與 `{code,message,title,data}` 解析。
- **analyze／packet 維持畫面(AX)讀取**：它們用的是調節庫存頁的完整列（位階、建議調節、
  損益），API 的 `api/myStock` 只回代號／股數／均價，沒有這些欄位，改 API 會遺失決策
  依據，故不轉。
- **寫入與下單維持 AX 不變**：改庫存（新增／修改／刪除）與運算頁現金欄仍走畫面，保留
  App 自算「總成本」當獨立見證；相應的寫入 API 端點未經驗證，且寫入／下單不可逆，
  不從唯讀層走——`api/auth`（換發 token，body 帶 appId／appSecret）亦刻意排除於白名單外。
- **arkapi 寫入層（預設關閉）**：依 mitm 實測格式加入 `set_cash`／`insert_holding`／
  `update_holding`／`delete_holding`。五個寫入端點（`myStock/insert`、`update/{…}`、
  `avgPrice/upsert`、`delete/{…}`、`calculateRisk/memberMoney/update`）全部實測確認、
  認證同讀取、**無 checksum**。雙重開關（`confirm=True` ＋ 環境變數 `ARK_API_WRITES=1`），
  每筆帶 read-after-write 讀回驗證取代 AX 的「總成本」見證；`set_cash` 省略 usd 時保留原
  美元現金避免歸零。預設完全不作用，`ark-sync` 實際寫入仍走 AX。評估、格式、擷取工具
  見 `references/write-api-conversion-assessment.md`（以 `ArkAPI_Catch/files/ark_capture.py`
  擷取、可 `diff` 回歸）。釐清「ARK 寫入≠下單」：真正買賣走 Shioaji，不轉 ARK 端點。
- **ark-sync 可切 API 寫入**：設環境變數 `ARK_API_WRITES=1` 時，庫存（insert／update／delete）
  與現金改走上述 `arkapi` 寫入函式（帶 read-after-write 驗證、不碰編輯頁），否則維持畫面(AX)。
  切換邏輯收斂在 `sync.apply_write`／`sync.sync_cash`，安全閘（來源讀空拒絕、刪除過半拒絕、
  dry-run）與重試一輪兩種後端都照舊。預設仍走 AX；dry-run 不論旗標都不寫入。

## [1.8.1] - 2026-10-12

- **修：翻頁讀取等畫面靜止再收集**。`lib/ark.py` 的 `_scroll_until_stable` 每次捲動後
  先等連續兩次探測結果相同才收集；翻頁動畫中途 AX tree 只暴露兩三列，那時收集會整段
  跳列（2026-10-10 實機 17 檔只讀到 13～14 檔，且沒有解析失敗樣本，`check_parsed`
  攔不到）。到上限仍在變就照當下畫面走，交給 `check_parsed` 把關。所有經此翻頁讀取
  ARK 庫存列的路徑（ark-sync、ark-agent 對帳）同受影響。
- `.codex-plugin/plugin.json` 版號自 1.4.0 起未隨版更新，本版補齊。
- 測試：`test_journal` 當日結算案例改塞假 `shioaji` 模組，乾淨環境依 README 跑不再
  `ModuleNotFoundError`；README 的 ark-agent 測試行補上 1.5.0 起新增的 `test_gates`。

## [1.8.0] - 2026-10-08

- **suggest_qty 語意寫進決策提示**。它是調節庫存頁的建議調節（賣出）股數，不是持有目標：
  建議 0 不得推論為「App 不建議持有」，「持有 − suggest_qty」不得稱為 App 建議。
  2026-10-02～10-08 決策層連續 7 筆以「持有 − 建議」當賣量；提示也註明前幾天的
  決策檔是未經複盤的案例，不是準則。「可提早減」只適用減碼，不適用停損。
- **個股桶燈號判定**：packet 的 `industry` 保存完整 `value_codes`／`heating_codes`
  （讀不到是 null）；`risk.lamp_state` 以調節庫存列位階與兩份名單判出
  `envelope.satellite.lamps`，來源缺失、型別不對或讀取失敗一律 `unknown`。
- **可設定的燈號必賣**：`ARK_LAMP_EXIT`（預設關閉；`all` 或代號清單）開啟後，範圍內
  確認無燈號的個股桶部位列入 `lamp_exits`，journal 要求整筆賣出（不能賣的日子不強制）。
- **休市跳過**：packet 在碰 App 前先查基準 0050 的當日日K與快照，休市離開碼 3，
  daily 視為今日不交易，不拿前一交易日的行情做決策。
- **複盤抓 K 線有界降級**：逐檔失敗改用區間內快取、連續失敗 3 檔即停止嘗試，報告
  開頭標示「行情不完整」，未到期者照列 pending（2026-10-02 複盤因單次逾時整支中斷）。
- **複盤區間從上次成功複盤隔天起算**（最多回看 28 天）：某次複盤失敗時，那幾天的
  案例不再永遠掉出 7 天視窗。「成功」以 `~/.ark-toolkit/agent/reviews/completed.jsonl`
  的成功紀錄為準——只在模型零離開、準則完整、檢討檔存在之後才寫；複盤層可能先寫出
  檢討檔才失敗，檔案存在不算數。成功紀錄上線（2026-10-08）前的檢討檔視為已完成。`prompts/review.md` 要求逐項處理準則檔的「待複盤事項」。
- 另含 54e1fe7：編輯庫存頁貼底列先捲動再按、重啟後就緒探測在時限內重試。

## [1.7.0] - 2026-09-28

- **準則歸因改為逐筆委託**。決策的每筆委託可帶自己的 `rules_applied`，
  `review.rule_performance` 只把那筆單的損益歸給它列的準則。原本準則掛在決策層，
  當天每筆單都記到當天每條準則上；決策若每天把準則全列，各條的數字就一模一樣
  （實例：R-003 與 R-004 的已評筆數、勝率、平均報酬完全相同），歸因表分不出功過。
  委託上沒有這個欄位的舊紀錄退回決策層清單，歷史數字不變；明示 `[]` 代表這筆單
  沒有準則驅動，不退回。決策層的 `rules_applied` 保留，記當天用到的全部準則。
  `prompts/decide.md` 同步要求逐筆填寫；journal 不對委託層編號加驗證。
- **歸因表加超額**：`rule_performance` 多回 `avg_excess`、`excess_win_rate`，報表多
  「平均超額」「超額為正」兩欄。報酬為正但落後基準的準則不算被損益支持。
- 複盤案例的每筆單印出它引用的準則。

## [1.6.1] - 2026-09-27

- **修：運算頁現金欄重複計算今日交割款**。`sync.posture_cash` 取
  `balance + today + t1 + t2`，但 `t_money` 在交割日當天已入帳 `acc_balance`——
  與 1.2.1 在 `equity.py` 修掉的是同一種錯，當時沒一併修到 ark-sync。以 25 份歷史
  決策包對帳：相鄰交易日的餘額變動 16 次全等於當日 `t_money`。小額買進日誤差約
  1–2%，大額調節後的交割日才顯形：2026-09-24 餘額 55,828 已含入帳的賣出款 33,893，
  現金欄卻寫成 80,560，ARK 據此算出的持股比由實際約 70% 變成 58%（hold 判成 buy），
  個股桶配額同步虛增。改取 `balance + t1 + t2`，口徑同 `equity.tradable_cash`，
  附 9/24 的回歸測試；輸出的「未交割」分項同步只計 t1／t2。
- **修：App 被登出時就緒探測誤判為殭屍態**。登入頁沒有 tab bar 也沒有 back，
  `ark.ensure_responsive` 會重啟 App 再空等 90 秒，最後報「等待逾時：重啟後 tab bar
  出現」——訊息指向冷啟太慢，真正原因是 session 過期。新增 `ArkLoggedOut`：探測失敗
  後與重啟後的等待中都先認登入頁（「會員登入」），認到就直接報已登出、不重啟。
- **修：ARK 視窗被蓋住時現金欄寫不進**。運算頁的數字鍵盤只能座標點擊，而座標點擊
  走全域滑鼠事件、落在該座標最上層的視窗；ARK 在背景且被別的視窗蓋住時，點擊全落
  在那個視窗上，欄位維持原值、對帳回報失敗（先前記為「現金欄偶爾第一次寫不進」）。
  `write_posture_cash` 在開鍵盤前先 `ax.activate()`；其餘操作仍可在背景進行。

## [1.6.0] - 2026-09-23

- **`daily.py trade`（ark-agent「手動一場」）**：不用排程、由人說「開始交易」才動，
  順序寫死：對帳統一（`sync --allow-delete --with-cash`）→ decide 的五步 → 等成交 → 當日
  結算五步。開始時段 09:00–13:00（`TRADE_WINDOW`），當日已有決策即拒絕（execute 送的是
  第一筆決策，再跑等於重送）。`decide`／`settle` 原樣保留給用排程的人；SKILL.md 新增
  啟動／監看／回報的 SOP 一節。
- **execute `--wait MINUTES`**：送單後留在線上輪詢，全部終態即回；逾時仍掛著的腿刪單並在
  journal 追加 `cancel` 記錄。execution 記錄改在等待前落地，途中行程死掉也看得到哪幾腿送出。
- **journal `settle --same-day`**：`settle_previous` 抽成 `settle_day(api, day, today)`，
  成交後立刻結算當日決策（fill 的 `date` 與 `decision_ref` 同日）。

## [1.5.0] - 2026-09-09

- **個股桶（兩桶制）**：衛星軌改為由 ARK 產業價值區供應候選（`lib/arkapi.zone()`，
  唯讀，含營收、利潤、負債、產業別、K9），配額改為總資源×ARK 建議持股比×佔投入比
  （起始 50%），單檔／同產業硬上限（起始 12%／25%）由 `journal` 拒單，超限列
  `envelope.satellite.trim_candidates`；閘門在 `gates.json`（新模組 `gates.py`），
  每週複盤可在區間內調整，超出由 `daily.py review` 還原。
- **packet**：`industry` 區塊（候選、升溫標記、產業別累積 `sectors.json`），候選納入 `news_scope`。
- **evaluate**：個股桶分開統計前瞻報酬，主軌口徑不變。
- **journal settle**：寫入 `satellite_exits.jsonl`，連續停損冷卻自此生效。
- **evaluate**：修 `evaluate_all` 迴圈的 `fills` 變數遮蔽——第二個決策日起成交對回被靜默
  丟掉、改用決策日收盤與 no_fill 評估；修後所有第 2 日以後的 exec_price 與紀律分會變動。
- **arkapi**：錯誤訊息不再帶查詢字串（deviceToken 曾可能流入 `packet.industry.error`）。

## [1.4.0] - 2026-09-04

- **ark-agent 每週自動複盤**：`daily.py review`（launchd review 排程，
  週五 15:00）依序跑 `review.py --out` 組本週案例 → 複盤層（`prompts/review.md`，只給
  Read／Write／Edit、不上網）寫檢討記錄到準則檔旁的 `decision-reviews/<date>.md` 並更新
  準則檔 → `rules_intact` 驗原有編號一條不少（改壞從 `~/.ark-toolkit/agent/reviews/
  <date>.rules.bak` 還原）→ git commit 這兩個檔（不在 repo 內或 git 失敗只記 log，準則
  已生效）→ 通知一行摘要。1.2.0 設計的學習迴路先前沒有觸發點，準則的損益證據到期了
  也沒人看；複盤 prompt 明確要求檢討「買哪一檔、為什麼不是其他候選」
- **檔數上限可設為參考值**：`ARK_NAMES_CAP=advisory`（預設 `hard`，行為不變）時
  journal 不再因檔數拒單，`envelope.core.names_cap` 讓決策層知道要自己判斷檔數並在
  rationale 說明；餵給 App 位階運算機的檔數不受影響（那是「App 對這筆閒錢分幾檔」的
  輸入，改了會扭曲每檔建議金額）。`ARK_NAMES_FLOOR` 抬高檔數下限（預設 1＝純公式），
  `discipline.names_floor` 記下來源；risk.py 在遲滯**之後**套下限，否則要連續 N 日才生效
- **修：成交對回的買進價反推混用均價口徑**。1.3.0 起 packet 的持倉均價依 ARK 口徑換算
  （對帳兩邊口徑必須相同），但 settle 反推買價時事後用券商原值，口徑差被舊持股數放大
  （實例：持股 185 股只買 21 股，每股 0.07 元的差被放大成 0.6 元，反推價低於當日最低價、
  每天 −0.9% 的假滑價）。packet 持倉多存 `raw_avg_price`，`journal.raw_positions` 讓對回
  兩邊都用券商原值；`evaluate.fills_by_ref` 改認同一決策的**最後**一筆對回，算錯的對回
  以 `corrected` 條目補正（journal 只能附加）
- **`execute.py --latest`**：送同日最後一筆（amended）決策；預設仍送第一筆
- **ARK App 1.8.x 版面**：調節庫存頁種類後多了「股價、漲跌幅」兩欄再接可選的建議調節，
  依漲跌幅帶括號分辨新舊版面；布局自選頁整列變成單段文字（`parse_layout_line`／
  `collect_layout_rows`），解析不到回 None 而非讀成空。`ax.ensure_ready` 不搶焦點、
  `ark.ensure_responsive` 殭屍態自癒；軟鍵盤是獨立的 CGWindow（layer 101），
  `ax.keyboard_up`／`ark.wait_keyboard`／`ark.dismiss_keyboard` 以「真的收掉了」為準
  （1.8.x 起前景點「完成」座標會留下殘留的輸入 session，之後的 keystroke 全被吃掉）；
  位階運算機以 `tier_calc_done` 判定完成。`ax.find` 帶 `seen` 防循環參照
- **`lib/arkapi.py` 唯讀查詢層**：運算頁五指標與持股水位建議是純圖形儀表板、AX 讀不到，
  改走伺服器 `calculateRisk`；重用 App 自己的 token（只在記憶體流動）、`call()` 白名單
  讓寫入端點打不出去。**ark-sync 同步前以伺服器庫存交叉檢查 AX 解析結果**
  （`cross_check`）：parser 被改版弄壞時當場中止、一個字都不寫，而不是把少讀的幾檔
  判成「ARK 少了」去新增；伺服器讀不到（token 過期、沒網路）只是跳過這道檢查
- `source.merge_positions` 均價四捨五入到分（浮點殘差 62.28000000000001 曾整串打進
  ARK 的均價欄）

## [1.3.1] - 2026-08-22

- **修：`ark-collect`／`ark-read` 沒把均價口徑傳給永豐讀取，快照仍是券商原值**
  （使用者回報）。1.3.0 把口徑接在 `source.read_positions()`，但 collect 與 read 是
  逐帳戶呼叫 `read_account_positions(account)`，新參數 `basis` 有 `None` 預設，
  既有呼叫點就靜默退回原值——多帳戶使用者的 sync 只讀快照，選的口徑從沒生效。
  現在 `basis` **必填**（漏傳當場報錯），collect／read 都帶 `cost_basis(cfg)`
- **第一次詢問口徑搬到 collect 也會問**：多帳戶的資料在 collect 階段就定案，
  只在 sync 問等於問晚了（問完還得再跑一次 collect）。`ensure_cost_basis` 移到
  `lib/dialogs.py` 供 sync／collect 共用；ark-read 維持不彈視窗，只套用既有設定
- 更正 1.3.0 文案「口徑影響 collect」的不實陳述；新增 `test_collect.py`、
  `test_read.py`、`lib/test_dialogs.py`

## [1.3.0] - 2026-08-22

- **ARK 均價口徑可選（含息／不含息 × 含手續費／不含手續費）**。券商 API 的
  `list_positions().price` 實測是**不含息、不含手續費**的純買進均價，1.2.1 以前
  ARK 的均價就是它的鏡像，已領股息從不反映在成本上。現在 `source.read_shioaji_positions`
  可依 `config.json` 的 `cost_basis` 從每筆買進分錄（`list_position_detail`）加總
  換算：(Σ金額 ± 手續費 ∓ 已領股息) ÷ 股數。**口徑是使用者的投資觀點，工具不代選**：
  有 Shioaji 帳戶且 config 還沒有 `cost_basis` 時，`ark-sync` 第一次啟動開原生視窗
  問一次、寫進 config 後永久沿用；`ark-setup` 按「完成」時可改。缺欄位＝券商原值，
  舊設定不必遷移、輸出逐位元與 1.2.1 相同
- **API 實測發現（設計依據）**：分錄的 `price` 欄是**該筆總金額**不是單價、零股的
  `quantity` 一律回 0（單位是張）；00911 三筆分錄各自帶 `ex_dividends`，券商是
  按分錄記股息，所以不能拿除權息表 × 持股數去推。整張持倉的分錄語意沒有樣本，
  換算前一律驗算 Σ分錄金額 ÷ 股數 ≈ 券商均價（容差 0.01），對不上指名代號中止——
  與 `check_parsed` 驗總成本同一哲學，寧可中止也不把錯值寫進 ARK
- **口徑只影響 `read_positions()` 這條路**（ark-sync／ark-collect／ark-analyze）。
  ark-agent 的 equity／journal／packet 仍用券商原值：journal 靠「均價 diff」反推
  買進成交價，均價若含息，除息日就會反推出錯的價格
- **排程一律 `--no-prompt`**：`daily.py` 呼叫 sync 的參數抽成 `SYNC_ARGS`，缺設定時
  不開視窗、沿用券商原值——無人值守沒人按視窗，問了只會卡死結算
- `sync-log.jsonl` 每筆多記 `cost_basis`；`source.describe()` 在有 Shioaji 時附上口徑
- 新增 `skills/ark-setup/test_setup.py`

## [1.2.1] - 2026-08-17

- **修：交割日重複計算今日交割款，造出永久鎖死熔斷的幽靈峰值**。`equity.py`
  的現金原取 `acc_balance + t + t1 + t2`，但 `t_money`（今日交割款）在交割日
  當天就已入帳 `acc_balance`，等於同一筆錢算兩次。實測 2026-08-13：餘額由
  15,633 跳到 95,307（恰為前一日 t1 的 79,674 入帳），而該筆仍列在 t 欄，
  淨值被灌成 278,515。**傷害不只是當天記錯**——熔斷基準取歷史峰值而
  `peak()` 取 max，假高點會永久留在曲線上：三個交易日後帳戶實際僅回撤
  -0.9%，卻被判 L2 全面停機、當日決策軌整條中止。改取 `balance + t1 + t2`，
  抽為純函式 `tradable_cash()` 並附 8/13 的回歸測試。
  **這個 bug 源自一次正確的修補**：0.6.0 為了修「只看已交割餘額造成假回撤」
  而把應收付加回來，但 T+2 三個欄位裡只有 t1／t2 是「尚未入帳」，`t` 的語意
  不同，一併加進去就成了方向相反的同一種錯。
  ⚠️ **既有使用者請檢查 `~/.ark-toolkit/agent/equity.jsonl`**：升級只修往後的
  計算，過去大額交割日留下的假高點仍在檔案裡，需手動把該日 `cash` 減去當日
  `t_money` 並同步修正 `total`，否則熔斷仍會被舊資料鎖住

## [1.2.0] - 2026-08-14

- **新增 ark-review skill（決策學習迴路）**：`review.py` 把一段期間的決策、
  當時理由、實際成交與前瞻報酬組成逐筆案例，供複盤討論。與 `evaluate.py`
  分工——evaluate 回答「成績如何」（聚合統計，服務實驗協定），review 回答
  「該改什麼」（逐筆案例）。前瞻報酬與紀律分復用 evaluate 的函式，不重算
- **複盤看全部委託**：`evaluate_all` 新增 `orders_of` 參數（預設仍只取主軌，
  實驗統計行為不變），review 傳入取全部的版本——衛星軌不進實驗數據，但那些
  單同樣花掉資金、同樣是判斷的產物，濾掉的話「為什麼買它」永遠不會被檢討
- **缺席日也是案例**：連續缺席代表系統有問題，只收有決策的日子會造成倖存者偏差
- **決策準則注入 packet**：`packet.rules` 帶入準則檔原文與編號清單
  （`ARK_RULES` 指定路徑，預設 `~/.ark-toolkit/decision-rules.md`）。準則
  一併進 `packet_hash`，而 journal 每筆決策都綁 hash——等於鎖住「當天用的是
  哪一版準則」，事後才能把損益歸因回當時的規則。**準則檔讀不到時為空欄位、
  決策照常進行**，新功能故障不該讓整天不交易
- **準則損益歸因**：決策層在 `rules_applied` 標記採用了哪幾條準則，
  `review.py` 依準則分組算前瞻報酬。沒有這條歸因鏈，「依損益調整準則」
  就只是換一種方式憑感覺：改了規則卻無從得知改對沒有
- **decide.md 補上選股準則**：既有提示只寫了買進的**資格**
  （`buy_candidates`）與**數量**（`tier_qty` 為錨），沒有任何一條講「多檔都
  合格時該挑哪一檔」。新增跨檔比較指引（位階金額、折溢價、升溫警示），並
  明示 `tier_qty` 是 `tier_amount ÷ price` 的結果、**不可跨檔比較**，用它
  選股會系統性偏袒低價標的
- **修好除權息校正（原本是空機制）**：`evaluate` 一直有股利校正邏輯，但
  `dividends.jsonl` 從未存在——`load_dividends` 對缺檔回空清單，於是 `cash`
  恆為 0、`adj` 恆等於 `raw`，校正**從未生效**，而 `dividend_affected` 旗標也
  永遠是 False，連「這個數字可能不可信」的提示都沒有。新增 `dividends.py`
  從 TWSE 除權除息預告表累積事件，接為結算第三步。
  **為什麼必須每天抓**：那是「預告」表，只含尚未發生的事件，除息日一過就從表上
  消失；等要算報酬時再查已來不及——與日K快取同一個模式，外部只給滑動視窗，
  本地負責留下歷史。**已知限制**：只涵蓋上市（TWSE），上櫃標的仍會少算配息；
  股票股利只記錄不校正，由既有的 `stock_dividend_unadjusted` 旗標提示
- **修：無除權息資料時仍宣稱「adj 已含現金股利校正」**。`api.kbars` 給的是
  未還原原始價（實證：0050 於 2026-07-21 除息 0.6 元，而 2026-07-01 的快取
  收盤仍是除息前的 109.35），沒有資料時該講清楚「未實際校正、跨除息日的報酬
  會低估」。這種靜默失真比明顯的錯誤更難發現，且影響是單邊的——續抱看起來
  比實際差、賣出看起來比實際好
- **複盤帶入決策當時的事實脈絡**：案例新增 `context`（posture 缺口與方向、
  主軌檔數上限的 App 值與生效值、賣出優先序、十檔候選的位階金額與折溢價比較）。
  決策理由裡的數字原本散在 `packets/` 與 `envelopes/`，回溯討論得同時翻四個
  檔案才拼得回當時情境——記錄完整卻無法自足。檔數上限特別分「App 給的」與
  「遲滯後生效的」，兩者不同時才解釋得了「為什麼滿檔只能換股」
- **修：`no_fill` 顯示會被誤讀為「沒成交」**。它的真正語意是「journal 裡還沒
  有這筆的成交紀錄」，當日複盤時多半只是還沒對回（成交對回要等隔日 settle）。
  改為「尚未對回成交紀錄（暫以決策日收盤 X 估算）」——在交易系統裡把「未對回」
  讀成「未成交」會導致誤判委託失敗
- **結算自動記錄當日已實現獲利**：新增 `record_return.py`，盤後結算第四步把
  券商 `list_profit_loss` 的 pnl 合計記進「運算 › 離職倒數」。**只記獲利日**
  （虧損由本金減少反映，App 紀律）；金額取券商數字而非自行推算，那個已含
  手續費與證交稅。**虧損日與只買不賣的日子離開碼為 0**——回報失敗會讓結算
  天天誤報「部分失敗」，真正的故障反而被雜訊淹沒。兩個寫 ARK App 的步驟排在
  最後且相鄰：AX 操作最脆弱，先把純資料的記錄做完，App 出問題不連累成交對回
  與淨值（熔斷基準的來源）
- **修：庫存無變更時 `--with-cash` 被跳過**（ark-sync）。`main` 在
  `if not plan` 分支直接返回，現金同步永遠不會執行。現金與庫存是兩件獨立的
  事：庫存一致不代表現金欄也對。實例（2026-08-14 換股當天）——庫存同步成功、
  現金欄寫入失敗，重跑時因庫存已一致而提早收工，現金停在舊值
- **decide.md 澄清檔數上限語意**：`max_names` 管的是「執行後同時持有幾檔」，
  不是「只能加碼手上那檔」。滿檔時賣光一檔換成另一檔仍合規
  （`journal.py` 的 `fully_sold` 正為此存在）——實測 2026-08-12、08-13
  連續兩日因誤解此條而略過換股選項
- **決策模型額度耗盡自動降級**：主模型撞到用量上限時，`daily.py decide` 改用
  接手模型重跑決策（`ARK_DECISION_FALLBACK`，預設 `opus`）。額度是跟著模型
  走的，撞牆那天會整日不交易，實驗數據就多一個與市場無關的缺口。降級**只在
  額度這一種失敗上成立**（認 CLI 輸出的 `/usage-credits` 指引）——prompt 寫錯
  或網路斷線換模型也是一樣的結果，不該白燒第二次決策；接手模型與現用相同時
  同樣不重試，那是拿同一個已耗盡的額度池再撞一次
- **claude 輸出改為先收進記憶體再寫入 daily.log**：額度耗盡與其他故障都是
  非零離開，要分辨只能看輸出內容。代價是決策執行期間 log 不再即時更新
- **修：離屏元素按不到**（`press_verified`）：`AXPress` 對捲出畫面的元素靜默
  失效、座標點擊則點在視窗外，兩種按法都救不了。按之前先 `AXScrollToVisible`
  （元素已可見時是 no-op）——App 改版把頁面內容推長就會踩到
- **App 冷啟動 timeout 20 → 60 秒**：wrapped iOS App 冷啟動要先做簽章驗證與
  wrapper 初始化，實測 20 秒不夠——`open -b` 已成功拉起 App，卻在它註冊進
  `runningApplications` 之前就逾時，整日決策因此落空

## [1.1.0] - 2026-08-11

- **新增 ark-start skill（初次上手驗證軌）**：把「初次測試驗證（模擬單）」
  與「實際串接看單」分成兩個明確步驟——模擬環境送單驗證委託路徑
  （原 ark-agent 的 `integration_execute_simulation.py` 移為
  `ark-start/verify.py`，寫死 simulation 碰不到真錢），通過後**自動串接**
  ark-read 實際看帳上持倉，不需使用者再下指令
- **文案通用化**：README 與 marketplace／plugin 描述改為
  「券商 API（目前支援永豐 Shioaji，架構可擴充）＋CSV/Excel 檔案帳戶」
  的表述，不再寫死單一券商
- README 測試清單補齊：ark-agent 全部 10 個測試模組、補上 ark-explore

## [1.0.0] - 2026-08-11 正式版

- **每日流程補全**：`daily.py decide` 盤前自動刷新布局自選（用 App 內建
  「重新整理」鈕，取代手動維護）並執行位階運算機，決策包帶入最新讀數
- **買進側以 App 建議股數為錨**：決策提示與風控 envelope 把布局自選的
  建議股數傳到決策層，買進不再只憑金額上限反推
- 新增本 CHANGELOG；版本定為 1.0.0

## [0.8.0] - 2026-08-11

- **plugin 通用化**：七個 SKILL.md 開頭加上通用性原則——個人化內容
  （帳戶設定、排程參數、專案協定、一次性調整）放執行資料夾
  `~/.ark-toolkit/` 或專案 `docs/`，不直接改 plugin
- 金額上限等註解改為通用理由表述，移除專案專屬脈絡

## [0.7.0] - 2026-08-11

- **殭屍態自癒**：App 最小化再喚醒後可能進入「AX 讀得到但所有輸入事件
  無效」的狀態，`ensure_responsive` 以實按 tab 驗證換頁、失效時自動
  終止重啟（登入態保留）
- **按壓驗證**：`AXPress` 假成功改以真實點擊＋結果驗證
- ark-sync 寫入失敗自動重試
- 新增離職倒數頁「記錄今日報酬」自動化（iOS 文字欄以 keystroke 輸入、
  提交後驗證）

## [0.6.0] - 2026-08-11

- **金額上限只管買進側**：賣出調節不設上限，「上限只限買進」的語意
  一路傳到決策層的風控 envelope
- **成交對回歸因修正**：結算時扣除 phase0（開局建倉）與計畫外執行，
  決策評估不再被非決策軌的成交污染
- **淨值現金計入未交割應收付**：T+2 交割中的金額納入淨值計算，
  大額調節日不再誤觸熔斷（假熔斷）

## [0.5.2] - 2026-08-11

- 修正 `fetch_daily` 快取：快取末日越過查詢區間起點時，會以
  起日大於迄日的區間打行情 API 造成 400 錯誤

## [0.5.1] - 2026-08-11

- ark-agent 去實驗化：實驗協定移至專案 `docs/`，plugin 保持通用的
  自動交易決策軌（盤中決策包、決策鎖定、自動下單、成交對回、滾動評估）

## [0.5.0] - 2026-08-10

- 初版發佈：7 個 skill（ark-setup／ark-read／ark-collect／ark-sync／
  ark-analyze／ark-explore／ark-agent）
- 多帳戶模型與兩階段同步：永豐 Shioaji＋任意個 CSV/Excel 檔案帳戶，
  ark-collect 收集確認 → ark-sync 套用
- 執行環境全面走 uv（PEP 723 inline metadata），launchd 排程以
  `uv run` 為進入點
