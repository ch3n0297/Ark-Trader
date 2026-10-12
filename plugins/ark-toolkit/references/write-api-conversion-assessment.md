# 寫入操作轉 API 評估

本文評估把 ark-toolkit 目前走 Accessibility(AX) 的 **ARK 寫入操作** 轉成直接呼叫
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

## 3. 證據與未知數

| 項目 | 狀態 |
|---|---|
| 端點路徑 | **已觀察**（二進位字串） |
| HTTP method | 推斷為 POST（同讀取端點慣例，待實測） |
| 請求 body 形狀 | **未知**。URL 快取只留查詢／可快取回應，寫入 POST 的 body 沒被快取 |
| `checksum` / `checksumEnabled` | **未知是否卡寫入**。二進位有這些字串且連結 CryptoSwift，寫入可能需要正確 checksum／簽章，演算法未還原 |
| token scope | 未知。讀取用的 App token 是否足以寫入，待實測 |

**結論**：端點知道了，但「怎麼合法送出一筆寫入」尚未驗證。盲目照讀取慣例送，很可能
被 checksum 或 body 格式擋下（回 4xx），甚至靜默不生效。

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

## 6. 設計提案（尚未接線）

保持 `call()` 唯讀不變，另立寫入通道，預設關閉、需顯式開啟：

```python
# arkapi.py —— 提案，未接線
WRITE_ENDPOINTS = {
    "api/myStock/insert", "api/myStock/update/{member_id}", "api/myStock/delete/{id}",
    "api/calculateRisk/memberMoney/update",
}

def write(sess, path, body, *, verify, confirm=False, fetch=_fetch, now=None):
    """送出一次寫入。預設拒絕：必須 confirm=True 且 ARK_API_WRITES=1。
    verify(sess) 是寫入後讀回驗證的 callable，回 True 才算成功。"""
    if not (confirm and os.environ.get("ARK_API_WRITES") == "1"):
        raise PermissionError("寫入預設關閉：需 confirm=True 且 ARK_API_WRITES=1")
    template = _templatize(path)
    if template not in WRITE_ENDPOINTS:
        raise ValueError(f"不在寫入白名單：{path}")
    if CHECKSUM_REQUIRED:
        body = {**body, "checksum": _checksum(body)}   # 演算法待還原
    resp = call_write(sess, path, body, fetch=fetch, now=now)   # POST
    if resp.get("code") != "0000":
        raise ArkApiError(f"寫入未成功 code={resp.get('code')}")
    if not verify(sess):                 # read-after-write：讀回不符即失敗
        raise ArkApiError("寫入後讀回與目標不符，視為未生效")
    return resp
```

要點：寫入白名單與讀取白名單分離；顯式 opt-in（旗標＋`confirm`）；checksum 內建但待
填演算法；**read-after-write 當獨立見證**；`sync` 仍可保留 AX 寫入當回退與對照。

## 7. 落地前必須先做的事（順序）

1. **抓一筆真實寫入**：用 ArkAPI_Catch 的 mitm 代理，在 App 裡實際改一檔庫存與改一次
   現金，攔下 `myStock/update` 與 `memberMoney/update` 的完整 body 與 header，確認
   是否帶 checksum 與其組成。這是唯一能把「未知」變「已知」的步驟。
2. **還原 checksum**（若步驟 1 顯示需要）：從 CryptoSwift 用法與 body 對照推出演算法，
   或在授權範圍內以實測樣本反推。
3. **先做現金**（單欄位、最好驗證）：`write()` + `arkapi.cash()` 讀回驗證，旗標後。
4. **再做庫存** insert/update/delete，沿用 read-after-write 與現有安全閘（來源讀空拒絕、
   刪除過半拒絕、dry-run）。
5. **保留 AX 寫入**當回退與第二見證；券商下單維持 Shioaji 不動。

## 8. 目前建議

在步驟 1（實測 body＋checksum）完成前，**維持 AX 寫入**：端點已知但送出格式未驗證，
盲轉會被擋下或靜默失敗，而「總成本」見證是已證可靠的。本評估已備妥設計與驗證方式，
一旦攔到真實寫入樣本即可按第 6、7 節落地。
