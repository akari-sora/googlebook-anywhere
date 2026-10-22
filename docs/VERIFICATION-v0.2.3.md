# v0.2.3 軟體驗收紀錄

日期：2026-10-22。下表記錄 release-candidate 基準 `1309aeca689a62d4f1cfd4e390b635b978b3ed51` 的驗收；它不是最終發行 tag 的來源識別。發布前另同步公開主線已存在的兩個修正：`206978b` 的建置標頭檢查與 `libdw-dev` 提示，以及 `33edf46` 的新寫入 USB 擴大 `/data` 行為與測試。最終來源以 `v0.2.3` tag 為準，tag pipeline 會重新驗證整合後的版本。

| 驗證項目 | 結果與範圍 |
| --- | --- |
| 遠端 CI | Pipeline `2974513388` 的 lint、builder、stick、install-tools、harness 五項全數 success；執行於指定的 Linux CI runner，沒有在 Mac 建映像 |
| Harness | 221 項 unit／fake-device integration 測試、127 項 mutation baseline 測試均通過；130/130 個突變被抓到，0 survivor、0 error、0 timeout |
| 韌體檢查 | `tests/builder/firmware-test.py`：166 passed、0 failed、0 skipped；未修改基準亦為 166/0/0 |
| Profile | `tests/builder/profile-test.py`：523 passed、0 failed；發行樹不包含 git-only minigbm 建置來源的預期 skip 仍保留 |
| 未綁定名字 | `tests/builder/undefined-names-test.py`：11 passed、0 failed |
| 韌體突變驗證 | 49 個突變、56 次執行全部由預期 assertion 抓到；逐份檢查失敗原因，沒有把語法／匯入錯誤或逾時當成抓到突變 |
| 發行文件與匯出 | 發行狀態 gate、文件敘述檢查、公開匯出與 CI maintenance 檢查通過；發布格式的暫存樹也已通過 gate |
| 發行說明內容 | 重播 tag job 的 description 組合方式，確認目前版本全文原樣保留，再附上同 tag 的完整歷史連結；沒有混入舊版內容 |
| 獨立審查 | 原候選的 20 個變更檔案與上述測試證據經另一名 reviewer 審查，沒有待修項目；外部 reviewer 的額度／服務限制另有記錄，未算成通過 |

公開主線修正保留原本的限制：USB 擴大 `/data` 僅有磁碟映像檔與 loop 裝置測試，尚無真實隨身碟或擴大後開機證據；`--no-grow` 保留原映像大小，`--keep-data` 不改分割表。既有測試紀錄見 [tests/install/RESULT.md](../tests/install/RESULT.md)，操作說明見 [README](../README.md)。

同步後的本機檢查：安裝 stub 測試 104 passed／0 failed／0 skipped，GPT 暫存檔測試全部通過；Mac 缺少 `sgdisk`，其比較檢查跳過，Linux 裝置檢查交由遠端驗證。獨立審查另修正 `--keep-data` 預檢誤用 grow 計畫，加入預設 grow、`--no-grow` 與 `--keep-data` 的 CLI 計畫斷言；保留明確拒絕 `--no-grow --keep-data` 的規則。

本次驗證沒有建立新的 Googlebook 映像，也沒有重新執行 KANO 實機開機。先前來源 `7a7810c` 的 QEMU 35 PASS／0 FAIL／0 SKIP、測試金鑰映像與雜湊仍在 [發行說明](RELEASE-NOTES.md) 中，不能當成本輪新實機證據。

發行產物是安裝與建置腳本封存包及 SHA-256，沒有磁碟映像、韌體映像、Google 檔案或簽章私鑰。公開版本由 `tools/release/make-public.sh` 排除研究筆記與個人設定後掃描；tag pipeline 仍須通過測試、封裝及 release job，才會出現可下載產物。

硬體支援與尚未驗證項目見 [KANO 硬體狀態](STATUS-KANO.md)。韌體反向檢查仍是 advisory，`PASS` 的保證與模型限制見 [發行說明](RELEASE-NOTES.md) 和 [運作方式](HOW-IT-WORKS.md)。
