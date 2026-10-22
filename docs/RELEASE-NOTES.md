# Googlebook Anywhere v0.2.3

<!-- release-status: released -->

v0.2.3 改發行說明的版面，並補上建置時的韌體檢查與 stage 2 自我測試用的名字。v0.2.2 與更早的內容見完整版本紀錄。

## 發行說明的版面

- `docs/RELEASE-NOTES.md` 只放目前這一版。發版時把上一版整段原樣移到 `docs/RELEASE-HISTORY.md` 標題下方的最前面，只刪掉該段的狀態註解行。
- 打 tag 的 pipeline 把整個 `docs/RELEASE-NOTES.md` 原樣當成發行說明發布（不切段），再空一行補上指向同一個 tag 的 `docs/RELEASE-HISTORY.md` 絕對連結。

## 韌體檢查

- `builder/kmod/check-firmware.py` 的任何輸出都不得含本機建置路徑。韌體名稱、釘選的 `path`、模組標籤或命中的宣告若含建置根目錄文字，檢查以 `firmware name contains a local build path` 失敗，只以 repr 印遮罩後的名稱。`firmware-report.json` 的診斷欄位先把建置路徑 scrub 成邏輯名稱，資料欄位不做路徑改寫；所有 JSON 字串在最後 guard 時若仍含建置根目錄文字，整段換成 `diagnostic withheld: it contained a local build path`，而且這次執行失敗。沒有殘留建置根目錄文字的欄位照原樣寫出。`PINNED firmware:` 與 `ADVISORY firmware pin:` 若仍含建置根目錄文字，整次檢查失敗。
- 反向檢查把 profile 每一筆 `[[firmware]]` 釘選對上載入閉包內模組的 `firmware=` 宣告。v0.2.3 這個反向檢查是 advisory：對不上時建置 log 印 `ADVISORY firmware pin:`，並記入報告的 `advisories`，不改變退出碼。`requested_by` 與 `loaded_by` 的格式仍由 profile 驗證，格式錯誤仍是 profile 錯誤。
- `PASS` 證明正向檢查的必要韌體都在、必要模組的 metadata 檢查通過、沒有本機路徑外洩。它不證明每筆釘選都被某個實際會載入的模組請求，也不證明 advisory 為 0 的釘選在真機上一定被載入。目前 `kano`、`generic-x86` 與 `amd-hx370` 三個 profile 的必要模組都沒有宣告韌體，真正宣告韌體的驅動（`iwlwifi`、`btintel`、`xe`、`amdgpu`、SOF）都只記錄不攔截（report-only），所以 `PASS` 不代表這些韌體釘選已被這個檢查核對。

## stage 2 的 DROP 與未綁定名字

- `builder/image.py` 再次設定 `DROP = devprofile.first_stage_drop(PROF)`。QEMU 自我測試用它確認 first stage 沒有載入 `cmdline.first_stage_drop` 列出的模組（KANO：`8250_dw.ko`、`intel-lpss-pci.ko`、`intel-lpss.ko`；檢查訊息寫 LPSS UART）。這個名字被拿掉時，`NameError` 只在真正的 stage 2 建置才出現。initrd 的 `modules.load` 仍由 `devprofile.stage2_modules_load()` 刪掉同一份清單。`generic-x86` 與 `amd-hx370` 的 `first_stage_drop` 是空的。
- `tests/builder/undefined-names-test.py` 用標準庫 `symtable` 檢查 `builder/*.py`、`builder/lib/*.py`、`builder/kmod/*.py`、`builder/ifma/*.py`：執行期從全域讀到的名字都要在模組範圍有綁定，或是 builtin。用 `exec`、對 `globals()` 動態賦值，或 star import 的腳本必須列在 `ALLOW` 並寫理由（目前沒有這種腳本）。兩個底線開頭且結尾的名字都當成已綁定，所以沒有定義的 dunder 不會被這項檢查抓到。

## 保留公開主線的安裝修正

- 同步公開主線 `206978b`：建置前先檢查 libelf、libdw 與 OpenSSL 標頭，補上 `libdw-dev`／`elfutils-devel` 安裝提示，Docker 建置環境加入 `libdw-dev`。
- 同步公開主線 `33edf46`：新寫入 USB 時預設把剩餘空間給 `/data`；`--no-grow` 保留映像大小，`--keep-data` 不改 GPT。此行為僅在磁碟映像檔與 loop 裝置測過，沒有真實隨身碟或擴大後開機驗證，操作限制見 [README](../README.md)。
- `--keep-data` 的預檢改用不擴大分割區的計畫，避免顯示不適用的 userdata 擴大／寫零提示；加入三種 CLI 模式的計畫檢查。

## 驗證紀錄

- 2026-10-22：來源 `1309aec` 的五項遠端 CI 全數通過；harness 為 221 項測試、127 項 mutation baseline 測試及 130/130 個突變被抓到，沒有失敗、survivor、錯誤或逾時。另有 166 項韌體測試、11 項未綁定名字測試與 49 個韌體突變（56 次執行）通過驗證；範圍與限制見 [v0.2.3 軟體驗收紀錄](VERIFICATION-v0.2.3.md)。以下映像與 QEMU 結果是先前的建置紀錄。
- 建置來源 commit `7a7810c`（程式碼與 `acd0463` 相同，之後只改發行說明）；在建置主機上以 KANO profile 建置並跑 QEMU 自我測試：35 PASS、0 FAIL、0 SKIP；`IMAGE OK`。
- 映像 `googlebook-kano.img` 的 sha256：`191804eab8dd283ba8501c39f95326b9af4e411dd6e5d37cee00cc060fdc4a85`。這份映像用的是測試簽章金鑰的副本，只用來驗證，不是發行映像，不可寫進使用者的內建碟。
- 韌體檢查那一行：`firmware check: PASS; required: 4 modules, 0 firmware declarations; 645 modules; report: firmware-report.json; pins: 11, declared: 5, runtime: 6, conditional: 0, advisory: 0`；`firmware-report.json` 裡沒有建置主機路徑。
- 沒有新的實機（KANO）開機紀錄。
- 未綁定名字的檢查接在 `tests/builder/run.sh`，預期那一行是 `undefined-name tests:` 接通過數與 `0 failed`。

## 已知問題與限制

- 反向檢查只報告。退出碼取決於正向檢查、必要模組的 metadata 錯誤、載入閉包的輸入／I/O 錯誤、名稱裡的建置路徑（D1）、output guard、報告寫入失敗與參數錯誤。`ADVISORY` 行若被 output guard 扣下，檢查仍會失敗。
- `docs/HOW-IT-WORKS.md` 列出、模型尚未涵蓋的缺口：first-stage init 會套用 initrd 自己的 `modules.blocklist`，checker 不讀這份檔案；父模組載入失敗時已載入的依賴不會回滾，checker 視為整組都沒載；newc 裡先當硬連結、之後變成目錄；stage 2 改寫清單與 newc 解開的先後順序；`modules.dep` 缺少 head 的依賴仍被當作可載入；softdep、ueventd 的 modalias 載入、腳本條件、續行與 here-doc 都不建模。
- 文件寫日後（v0.2.4 之後）模型補齊才把反向檢查升為閘門。`advisory` 不為 0 時要逐行看 `ADVISORY firmware pin:`。
