# 寫入操作轉 API 評估

本文評估把 ark-trader 目前走 Accessibility(AX) 的 **ARK 寫入操作** 轉成直接呼叫
ARK API 的可行性、證據、未知數與落地設計。對應 1.9.0 已完成的讀取轉 API。

> 定位：這是**評估與設計**，不是已接線的功能。在下方「落地前必須先做的事」完成前，
> 寫入仍走 AX；本文不提供可直接送出的寫入程式。

## 1. 範圍澄清：ARK 寫入 ≠ 下單

真正的買賣**在券商端（永豐 Shioaji）送出**（`skills/ark-agent/execute.py` 的
`place_legs` → `api.place_order`）。ARK 是分析／建議 App：

- `sync` 把 ARK 台股庫存改成與券商一致——資料輸入，不是交易。
- 運算頁現金欄影響 ARK 算出的建議持股比與參考調節——輸入，不是金流。

因此「把 ARK 寫入轉 API」不會送出任何委託，最壞情況是把這個建議 App 的顯示資料寫錯，
而那可由重跑 `ark-sync` 復原。**券商下單不在本評估範圍，也不應轉成任何 ARK 端點。**

## 2. 現行 AX 寫入 ↔ 逆向對到的 API 端點

端點取自 App 1.9.3 主程式字串（shipped-artifact，靜態觀察）。

| 現行 AX 操作 | 檔案 | 候選 API 端點 | 備註 |
|---|---|---|---|
| `add_holding` | sync.py | `api/myStock/insert` | 新增一檔台股 |
| `update_holding` | sync.py | `api/myStock/update/`、`api/myStock/avgPrice/upsert` | 改股數／均價 |
| `delete_holding` | sync.py | `api/myStock/delete/` | 刪一檔 |
| （排序） | — | `api/myStock/updateSort` | 目前 AX 未動排序 |
| `write_posture_cash` | ark.py | `api/calculateRisk/memberMoney/update`（或 `/insert`） | 運算頁台幣／美元現金 |
| （自選布局） | — | `api/watchlistUpdate` | 若日後要同步自選 |

這些與 `test_arkapi` 唯讀守衛測試裡被拒絕的範例（`api/myStock/update/...`、
`api/calculateRisk/memberMoney/update`）一致——它們是真實存在的寫入端點。

## 3. 證據（2026-10-12 mitm 實測）

用 mitmproxy 分幾次攔到**全部五個**寫入（改股數、改均價、新增、刪除、改現金），皆回
`{"code":"0000","message":"操作成功"}`、**無 checksum**、認證同讀取。擷取用 `files/ark_capture.py`
（ArkAPI_Catch）完成，基準存在該專案的 `ark_api_baseline.json`。

| 寫入 | 送法（實測） |
|---|---|
| 改股數／均價 | `POST api/myStock/update/{member_id}/{code}/{kind}/{qty}/{avgPrice}` — **參數全在 path**、body `{}`、query 帶 device |
| 連帶更新均價 | `POST api/myStock/avgPrice/upsert`，body `{member_id, stock_code, data_date:"YYYYMMDD", avg_price, device…}` |
| 新增 | `POST api/myStock/insert`，body `{member_id, stock_code, type, market, totle_count, avg_price, device…}`（欄位拼法是 `totle_count`） |
| 刪除 | `POST api/myStock/delete/{member_id}/{code}/{kind}` — **參數全在 path**、query 帶 device |
| 現金 | `POST api/calculateRisk/memberMoney/update`，body `{member_id, amount_twd, amount_usd, device…}`（同時帶台幣與美元） |

共通：

| 項目 | 結果 |
|---|---|
| `checksum` / 簽章 | **不需要**。寫入 header 無任何 checksum／sign；`checksumEnabled` 不卡這些寫入。（二進位的 MD5 字串來自 Firebase 效能監控，非請求簽章） |
| 認證 | 與讀取相同：`Authorization: Bearer <app token>` ＋ `deviceType=I&deviceToken=…` |
| 回應 | `{"code":"0000","message":"操作成功"}`（只回成功，不回寫入後狀態，故需 read-after-write） |
| User-Agent | `Arkeration/1.9.3 (com.galaxy.ark; build:1.9.3.0; iOS 26.6.0) Alamofire/5.9.0`（已比照帶上） |

**結論**：五個寫入的完整送法都已驗證，**沒有 checksum 障礙**。`arkapi` 的寫入層已依此實作
（見第 6 節），預設關閉。

## 4. 如何取代「總成本」這個獨立見證

現行 AX 寫入的安全核心：填完後用 App 自算的「總成本」當獨立見證，數字對不上就不儲存
（`sync._verify_and_save`）。API 寫入只回「操作成功」，這正是作者當初不走 API 寫入的理由。

1.9.0 把讀取轉成可靠的 API 後，這個缺口有了乾淨的補法——**寫入後即讀回驗證
（read-after-write）**：

- 改庫存後，`arkapi.positions()` 讀回該檔，斷言股數／均價等於目標值。
- 改現金後，`arkapi.cash()` 讀回，斷言台幣金額等於目標值。

讀與寫是 App 伺服器上的同一份狀態，讀回相符即確認寫入生效；不符就當失敗、不往下走。
這比「總成本」更直接（直接比對最終狀態，而非間接乘積）。

## 5. 風險分級

| 寫入 | 可逆性 | 風險 |
|---|---|---|
| myStock insert/update/delete | 可由 `ark-sync` 重跑復原 | 中：寫錯會讓 ARK 建議暫時失真 |
| memberMoney update | 可重寫 | 中：現金錯會讓建議方向反（官方明講） |
| updateSort / watchlistUpdate | 可重寫 | 低：僅顯示順序／自選 |
| 券商下單 | **不可逆** | 不在範圍——維持 Shioaji，永不轉 ARK 端點 |

沒有任何一項會送出委託；最壞是建議 App 顯示錯誤，read-after-write 會當場攔下。

## 6. 實作（已接線，預設關閉）

`lib/arkapi.py` 已加寫入層，`call()` 唯讀不變。雙重開關：需 `confirm=True` 且環境變數
`ARK_API_WRITES=1`，否則連送都不送（`WritesDisabled`）。因實測無 checksum，通道即
「送出 ＋ read-after-write 讀回驗證」。對外函式：

| 函式 | 端點 | 讀回驗證 |
|---|---|---|
| `set_cash(sess, twd, usd=None)` | `memberMoney/update` | `cash()` 比對台幣；`usd=None` 時先讀回保留美元，避免歸零 |
| `insert_holding(sess, code, qty, avg_price)` | `myStock/insert` | `positions()` 確認該檔股數 |
| `update_holding(sess, code, qty, avg_price)` | `myStock/update/{…}` ＋ `avgPrice/upsert` | `positions()` 比對股數／均價 |
| `delete_holding(sess, code)` | `myStock/delete/{…}` | `positions()` 確認已消失 |

要點：寫入與讀取白名單分離；顯式 opt-in（旗標＋`confirm`）；**無 checksum**；
**read-after-write 當獨立見證**（取代 AX 的「總成本」）；台股 `type/market` 預設 `"1"`；
券商下單不在此層。測試見 `lib/test_arkapi.py::TestWriteLayer`（假 fetch、不連網）。

## 7. 進度

1. ✅ 抓真實寫入（2026-10-12）：五個寫入全部實測，格式見第 3 節，確認**無 checksum**。
2. ✅ `arkapi` 寫入層已實作並通過測試（見第 6 節），預設關閉。
3. ✅ `ark-sync` 的寫入已可切到這些函式：設 `ARK_API_WRITES=1` 時庫存（insert／update／
   delete）與現金改走 `arkapi`，否則維持 AX。切換邏輯在 `sync.apply_write` 與 `sync.sync_cash`，
   沿用現有安全閘（來源讀空拒絕、刪除過半拒絕、dry-run），AX 保留當預設與回退，附測試
   `test_sync.py::TestWriteBackend`。
4. 券商下單維持 Shioaji 不動，永不轉 ARK 端點。

## 8. 目前狀態

寫入轉 API 已完成：格式實測、無 checksum、`arkapi` 函式與 `ark-sync` 的後端切換都就緒並
通過測試。**預設仍走 AX**（有「總成本」見證）；設 `ARK_API_WRITES=1` 才改走 API 寫入
（每筆帶 read-after-write 讀回驗證）。dry-run 不論旗標都不寫入；券商下單與此無關。
日後 App 改版要重驗端點：用 `files/ark_capture.py`（`up`→操作→`diff`）。
