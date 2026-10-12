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

## 3. 證據（2026-10-12 mitm 實測）

用 mitmproxy 攔到一筆真實的改庫存（00935 股數 32→33→32，淨零），確認了格式：

| 項目 | 結果 |
|---|---|
| 改股數／均價 | `POST api/myStock/update/{member_id}/{code}/{kind}/{qty}/{avgPrice}` — **參數全在 URL path**；query 只帶 `?deviceType=I&deviceToken=…`；**body 為空 `{}`** |
| 連帶更新均價 | `POST api/myStock/avgPrice/upsert`，body `{member_id, deviceType, deviceToken, stock_code, data_date:"YYYYMMDD", avg_price}` |
| `checksum` / 簽章 | **不需要**。寫入 header 無任何 checksum／sign；`checksumEnabled` 不卡這些寫入。（二進位的 MD5 字串來自 Firebase 效能監控，非請求簽章） |
| 認證 | 與讀取相同：`Authorization: Bearer <app token>` ＋ `deviceType=I&deviceToken=…`。讀取用的 App token 足以寫入 |
| 回應 | `{"code":"0000","message":"操作成功"}`（只回成功，不回寫入後狀態） |
| User-Agent | `Arkeration/1.9.3 (com.galaxy.ark; build:1.9.3.0; iOS 26.6.0) Alamofire/5.9.0`（建議比照，未確認是否必需） |

**結論**：改庫存的完整送法已驗證，且**沒有 checksum 障礙**——這是先前最大的未知數，現已清除。
`insert`／`delete`／`memberMoney/update`（現金）尚未各攔一筆，格式可由此類推，但落地前建議
比照各實測一次（見第 7 節）。

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

保持 `call()` 唯讀不變，另立寫入通道，預設關閉、需顯式開啟。因實測確認**不需 checksum**，
通道比原先設想簡單——送出 ＋ read-after-write 讀回驗證即可：

```python
# arkapi.py —— 提案，未接線
WRITE_ENDPOINTS = {
    "api/myStock/update/{member_id}/{code}/{kind}/{qty}/{avgPrice}",  # 改股數/均價（path 參數、body {}）
    "api/myStock/avgPrice/upsert",                                    # 連帶更新均價（body 帶欄位）
    "api/myStock/insert", "api/myStock/delete/{member_id}/{code}",    # 新增/刪除（格式待各攔一次確認）
    "api/calculateRisk/memberMoney/update",                           # 現金（待實測）
}

def write(sess, path, *, body=None, query=None, verify, confirm=False, fetch=_fetch, now=None):
    """送出一次寫入。預設拒絕：必須 confirm=True 且環境變數 ARK_API_WRITES=1。
    verify(sess) 是寫入後讀回驗證的 callable，回 True 才算成功。無 checksum。"""
    if not (confirm and os.environ.get("ARK_API_WRITES") == "1"):
        raise PermissionError("寫入預設關閉：需 confirm=True 且 ARK_API_WRITES=1")
    if _templatize(path) not in WRITE_ENDPOINTS:
        raise ValueError(f"不在寫入白名單：{path}")
    if not token_valid(sess.token, now=now):
        raise ArkApiError("登入憑證已過期")
    params = {"deviceType": "I", "deviceToken": sess.device_token, **(query or {})}
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {sess.token}", "Accept": "*/*",
               "Content-Type": "application/json"}
    resp = fetch(url, headers, body if body is not None else {})   # 空 body 也送 {}
    if resp.get("code") != "0000":
        raise ArkApiError(f"寫入未成功 code={resp.get('code')}：{resp.get('message')}")
    if not verify(sess):                 # read-after-write：讀回不符即失敗
        raise ArkApiError("寫入後讀回與目標不符，視為未生效")
    return resp
```

要點：寫入白名單與讀取白名單分離；顯式 opt-in（旗標＋`confirm`）；**無 checksum**；
**read-after-write 當獨立見證**；`sync` 仍可保留 AX 寫入當回退與對照。

改一檔庫存 = 兩支呼叫（實測）：`myStock/update/{...}/{qty}/{avgPrice}`（path 參數、body `{}`）
＋ `myStock/avgPrice/upsert`（body 帶 `member_id, stock_code, data_date, avg_price`）。
verify 用 `arkapi.positions()` 讀回該檔比對股數／均價。

## 7. 落地前必須先做的事（順序）

1. ~~**抓一筆真實寫入**~~ ✅ **已完成（2026-10-12）**：mitm 攔到 `myStock/update` ＋
   `myStock/avgPrice/upsert`，格式見第 3 節，確認**無 checksum**。
2. **補攔 `insert`／`delete`／`memberMoney/update` 各一筆**（同法，各做一次真實小改動再還原），
   確認剩下三個寫入的 path／body。
3. **先做現金**（單欄位、最好驗證）：`write()` + `arkapi.cash()` 讀回驗證，旗標後。
4. **再做庫存** update/insert/delete，沿用 read-after-write 與現有安全閘（來源讀空拒絕、
   刪除過半拒絕、dry-run）。
5. **保留 AX 寫入**當回退與第二見證；券商下單維持 Shioaji 不動。

## 8. 目前建議

最大障礙（checksum）已排除，改庫存送法也實測確認，**技術上可落地**。建議：補攔其餘三個
寫入端點（第 7.2）後，依第 6 節在旗標後先接現金、再接庫存，一律帶 read-after-write 驗證，
並保留 AX 寫入當回退。在接線完成並通過測試前，預設仍走 AX。
