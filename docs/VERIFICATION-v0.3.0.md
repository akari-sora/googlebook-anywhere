# v0.3.0 軟體與 KANO 驗收紀錄

日期：2026-10-29。狀態：unreleased。候選為修正 APK／APEX 對齊後的 `f17069da`（產品來源 `fee74fe`，基於 `dd647e8`），本次在 `release/next` 更新文件；最終來源、公開匯出 commit 與 tag 尚未確定。本次沒有連線至遠端、建置完整映像、接觸硬體或發布。

## 軟體驗證

本節的「先前本機」表保留較早一輪 macOS 的結果，各列均標為「先前 macOS」，本輪沒有重跑這些軟體 suite。此次文件 gates 單獨記錄；發布前仍須在最終來源與公開匯出樹重跑 Linux CI。失敗、跳過與缺少工具不能算成通過。

### 本輪文件 gates（macOS arm64，Python 3.14.4）

| 驗證項目 | 結果與範圍 |
| --- | --- |
| `python3 -I tests/stick/release-notes-gate.py` | PASS（macOS，#17 合併後的 release/next）：`release-notes-gate: clean`。先前因暫留的 `<!-- needs #17 merge -->` 提醒退出 1，該提醒已移除；gate 規則未改 |
| `python3 -I tests/stick/docs-story-test.py` | PASS，退出 0，`docs-story: clean` |
| `GBOS_REQUIRE_PRIVATE_SCAN=1 python3 -I tests/stick/public-export-test.py` | PASS，退出 0，`public-export: clean`；本機匯出 582 個檔案，個資掃描乾淨；此 gate 驗證乾淨的 HEAD，範圍只限公開匯出樹，不包含安裝映像 |
| `git diff --check` | PASS，退出 0 |
| 修改文件相對連結 | PASS，113 個本機目標存在，0 個缺失；排除程式碼範例，不檢查外網 URL 與 fragment |

### 先前本機 macOS 結果（本輪未重跑）

| 驗證項目 | 結果與範圍 |
| --- | --- |
| 先前 macOS：發行說明與歷史 | v0.2.3 段落已在歷史頂端，與原 tag 的全文只刪除第 3 行狀態註解後完全一致；v0.3.0 第 3 行維持 `unreleased` |
| 先前 macOS：Shellcheck／Python | 非 research 的 tracked shell 全數通過 warning 級 shellcheck；Python 全數 py_compile 通過 |
| 先前 macOS：發行說明 gate | `release-notes-gate.py` 通過；真實 package gate 對目前 unreleased notes 退出 1，對暫存正式格式退出 0，符合預期 |
| 先前 macOS：文件／CI 敘述 | docs-story、ci-maint 通過；修改文件 120 個相對連結目標存在，未查外網 URL 或 fragment；plan 的 17 段 shell 通過 `bash -n`，4 段 Python 通過語法解析 |
| 先前 macOS：個資與公開匯出 | private-patterns 文件掃描與強制 private public-export test 通過；PNG 檢查 32 passed。文件提交後另做最終 commit 的匯出／封存讀回，結果保留在本機準備報告，不把尚未執行的檢查標為通過 |
| 先前 macOS：EC drain／模組檢查 | ec-drain 47 passed；check-ko 42 passed；均為軟體檢查，沒有實機 EC 證據 |
| 先前 macOS：Probe／stick | probe 60 passed；stick 合成碟 73/73 通過 |
| 先前 macOS：更新提示 | 離線測試 36 passed、0 failed、1 skipped（預設不做 live 外網查詢） |
| 先前 macOS：更新 selftest／突變 | FAIL／環境阻擋：缺少 mtools，selftest 在預檢退出 1，25 個突變均未取得有效基準，不能算通過 |
| 先前 macOS：安裝工具 | write-usb 18 passed、0 failed、2 skipped：缺少 sgdisk 比對，以及 macOS 的 Linux guard 區段；沒有真實裝置操作 |
| 先前 macOS：安裝腳本／kano-check | stub 測試 128 passed、0 failed、0 skipped；kano-check 15 passed |
| 先前 macOS：Maintenance | FAIL／平台阻擋：271 passed、1 failed；NVMe confirm gate 需要 Bash ≥4.4 或已存在的 Linux container，本機沒有，未下載或安裝 |
| 先前 macOS：Report | 469 passed、0 failed（含 5088 個合成 schema 案例） |
| 先前 macOS：Builder | FAIL／平台阻擋：lapis-base-pins 的 strict final image 案例用 `declare -A`，本機 Bash 3.2 拒絕。已通過 archive 建立、內容／IFMA 匯入與 archive profile 檢查；確認失敗後停止後續重複子樹 suite，沒有完整通過的總結 |
| 先前 macOS：Harness | 未完成：30 分鐘上限，退出 124。unit／fake-device integration 227 tests 與 mutation baseline 127 tests 通過；中止前觀察到 66 個 killed，沒有完整 mutation／suite 總結，不能算整體通過；本次子程序已停止 |
| 先前 macOS：遠端 CI／tag pipeline | NOT_RUN；本輪禁止遠端操作 |
| 先前 macOS：公開發布與下載讀回 | NOT_RUN；本輪禁止 push、tag、MR 與 release |

## 已上機候選與來源

下列識別與實機結果依據本次交付的 2026-10-29 去識別化證據（工作封包 `EVIDENCE.md`）；它是實驗室以既有簽章身分建置、已用於實機更新的映像。本輪只更新文件，未讀取映像、未重算雜湊、未獨立核對簽章，也沒有重新建置或重新簽章：

- SHA-256：`f17069da8659d400ae636bfb25e224aaced9a146539157639bf657a02ddac62c`。
- 大小：`19069493760` bytes。
- fingerprint：`ASUS/lapis/lapis:17/CL3B.260622.276.R4/16658739:user/release-keys`。
- 產品來源：`fee74fe`（`fix/apex-zipalign`，基於 `dd647e8`），`GBOS_LAPIS_BUILD=16658739 GBOS_EC=drain`；實驗室另設 `GBOS_ADB_PUBKEY`，公開建置不設。後續 `aa33bd2`／`d7f26c0` 只修改 alignment gate，不改映像位元組；交付證據記錄此映像重跑對齊檢查 PASS，291/291。
- AVB 公鑰 SHA-256：`2f5babf9df83a218ae9ccd567426673261b0e97126aac69e119bc1d636393c23`，交付證據確認與既有安裝相同。
- APEX payload 公鑰 SHA-256：`5a686a643d71e2f359f91c09f16b88ee5336d070d2235880db79214e7f46c881`；APEX container 憑證 SHA-256：`045f1b98af0985d4a1f9dcff91c4bb9fb84b4cf7461139ae910292f9dbdc9803`；交付證據記錄沿用既有身分。
- 16658739 與 `GBOS_EC=drain` 都須明確選用。一般建置預設仍為 16452207、封鎖 EC。

先前候選 `20a4cf52` 在實機因重簽 APEX container 未做 ZIP 對齊，被 16658739 的 PackageManager 拒絕解析，造成 `system_server` crash-loop。`builder/lib/ifma.py` 補上 padding，並新增 `builder/lib/container_alignment.py` 的強制全分割區 APK／APEX 對齊 gate，因此改用 `f17069da`；失敗後舊系統仍完整，曾由 previous-version 選項啟動兩次並保留資料，之後也恢復選單預設。

交付的 QEMU 紀錄有 44 個檢查 PASS；KANO QEMU 沒有 GPU，`boot_completed` 與 `system_server` 重啟檢查不能作完整 Android 開機驗收，候選是記錄此限制後人工放行。issue #17 的 NOT_RUN 回報修正另行合併，本輪未執行 QEMU。

這份映像是實驗室 KANO 驗收候選。公開產物是安裝與建置腳本封存包，不含磁碟映像、Google 檔案、韌體或私鑰。`published-images.sha256` 是文件 PNG 的清單。

## KANO 實機證據

以下 PASS 僅引用交付證據，並非本輪重新操作硬體；範圍不超出實際讀回與觀察：

- **PASS：keep-data 更新與開機。** 2026-10-29 15:10–15:22（+08），透過安裝碟的「Update Googlebook on the internal disk (keep data)」，由 16452207 slot a 更新到 16658739 slot b；更新器回報 UPDATE OK，新版預設、舊版 slot a 保留。userdata／metadata／misc／分割表未寫入。實際 slot `_b`，fingerprint 與上述一致，`sys.boot_completed=1`、開機原因 `reboot,ota`，30 分鐘以上 uptime 時 `system_server start_count=1`，沒有重啟。
- **來源與身分：交付證據已記錄。** 大小、SHA-256、來源、參數與公開指紋如上；本輪獨立重算映像雜湊及簽章比對 **NOT_RUN**（文件封包未提供映像，且禁止接觸硬體／網路／私鑰）。
- **PASS：限定範圍的資料保留。** 兩個使用者逐一比對：安裝套件清單、帳號數、夜間顯示／亮度／亮度模式、鎖定狀態，以及主要使用者的 `/sdcard` 檔名均相同，結果 RETENTION_OK。另一使用者的個人檔案需本人解鎖；檔案內容、逐項 App 資料與帳號登入有效性未驗證。使用者於 10-29 回報更新後機器可用。
- **PASS：`update_engine` 讀回。** 更新後 uptime 65 分鐘時為 stopped。全程未曾啟動或重啟 **未驗證**；全新安裝的設定精靈／GMS 行為 **NOT_RUN**（本次是 keep-data 更新）。
- **PASS：drain 模組與有限時長待機。** `cros_ec`、`cros_ec_lpcs`、`cros_ec_proto`、`cros_kbd_led_backlight` 載入，沒有其他 `cros_ec` consumers 載入；`gbos_wakefilter`、`ac` 載入。這是載入狀態，不等於逐一驗證六個額外模組的封鎖。Android 約 15:54 因 timeout 入睡，約 33 分鐘沒有螢幕自行喚醒，直到 16:27 鍵盤喚醒（`WAKE_KEY`）；舊版約每 78 秒自行亮屏。鍵盤喚醒可用，不能由此宣稱永久解決自行亮屏。
- **PASS：有限電源／網路狀態。** AC 供電、電池存在且 99%；`wlan0` up 且有 default route。睡眠時 Wi-Fi ADB 慢或間歇，屬實驗室工具限制，沒有據此宣稱完整網路驗收。
- **NOT_RUN／未驗證：其餘硬體與復原。** S0ix residency、真實電源鍵、闔蓋／開蓋、音訊、超過 33 分鐘的持續黑畫面、休眠失敗復原中的 AP reset，以及本候選的 EC／AML／模組相依核對與 USB 先決驗證，交付證據均未提供結果。不得以 QEMU 或 `PM: suspend entry (s2idle)` 代替硬體 S0ix。
- **NOT_RUN：成功更新後退回與備份還原。** previous-version 選項仍在，但 16658739 可能已升級 `/data`，舊版不保證能開機；更新器有提示。先前失敗候選的兩次舊版開機不能證明成功更新後可退回；交付證據沒有備份還原測試結果。

## 發布狀態與限制

使用者於 2026-10-29 決定發布 v0.3.0；本輪只完成文件與本機 gates，notes 維持 `unreleased`，交由 lead 完成最終合併與發布。16658739 與 drain 仍須明確選用；drain 仍為實驗選項。載入 EC transport 後，休眠失敗復原可能重設 AP 並造成資料遺失；先決條件與限制見 [安全說明](SAFETY.md)。

最終來源／公開樹 Linux CI、封裝、checksum、release 與鏡像下載讀回，本輪均 **NOT_RUN**（禁止網路、push、tag、MR 與 release）。正式發布前由 lead 核對 issue #17 合併及移除 notes 旁的合併提醒。發行工具與規則見原始碼中的 `tools/release/README.md`；更新方法見 [更新說明](UPGRADE.md)，硬體範圍見 [KANO 狀態](STATUS-KANO.md)。
