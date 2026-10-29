# Googlebook Anywhere v0.3.0

<!-- release-status: released -->

v0.3.0 接上 AVX-IFMA 建置期修補、選用的機型顯示密度（issue #5）、唯讀的新版本提示（issue #7）、停用 Google 系統更新（issue #6），並加入 lapis 16658739 的釘選與實驗性 KANO EC drain。v0.2.3 與更早的內容見 [完整版本紀錄](RELEASE-HISTORY.md)。

## 顯示大小

- 新的選用機型設定 `[display] density`（120–480）：寫入 vendor `build.prop` 的 `ro.sf.lcd_density`，讓顯示大小可以調得更小（issue #5）。沒設定時保留既有的顯示密度；內建機型都不改變預設的顯示設定。

## 新版本提示

- 新增 `install/check-updates.py`（唯讀）：檢查 Google 有沒有比選定的 `GBOS_LAPIS_BUILD`（預設 16452207）更新的 lapis build，以及這個專案有沒有更新的 release（issue #7）。`update.sh` 與 `install.sh` 只在有新版本時印一行提示，不會讓安裝或更新失敗；`--no-update-check` 或 `GBOS_NO_UPDATE_CHECK=1` 關閉。見 `docs/UPGRADE.md`「怎麼知道有新版本」。

## CPU 需求

- 17 個 host ELF 含經 manifest 精確核對的 Rust `zmij` 浮點格式化 IFMA pattern；三個內建機型設定（kano、amd-hx370、generic-x86）都預設啟用 `patch_ifma`，啟用時會在建置期精確修補。CPU 限制、殘留指令與掃描邊界見 `docs/COMPATIBILITY.md`；Android 17 keep-data 更新的未驗證項目與金鑰備份要求見 `docs/UPGRADE.md`。

## 建置期 IFMA 檢查與修補

- 建置期 IFMA 檢查：修補 17 個已知目標 ELF，並以 fail-closed scanner 檢查整個分割區（全部 x86 ELF 與巢狀容器），任何未釘選的命中都會停止建置。
- 現代 zstd／lz4 frame（不含 legacy LZ4）只要解碼失敗，一律停止建置，不能用 pin 放行；主機上沒有或無法啟動 `zstd`／`lz4` 解碼器時同樣停止建置；legacy LZ4 失敗時若已寫出位元組，也一律停止建置。
- 可 pin 白名單（v0.3.0）：`unsupported_allowlist` 的精確 pin 只能放行完全沒有解出任何位元組的內容：以 magic 辨識的未知或不支援映像（例如 AVB0 vbmeta）、超過巢狀深度、缺少 erofs／sparse／ext4 工具或被該工具拒絕且沒有寫出任何輸出的映像、前導 NUL 之後、其餘全是 NUL 的 0 輸出 gzip／xz frame、依 0 輸出規則的 legacy LZ4、central directory 無法解析的 ZIP，以及 payload 從未解碼的 bzImage（沒有 HdrS、payload 起點在檔外，或 payload 範圍超出檔尾而檔內剩餘位元組解不出任何內容（解得出就先掃描再以不能 pin 的錯誤停止）、payload magic 認不得；另有主機上沒有對應 zstd／LZ4 frame 解碼器的情況）。永遠不能 pin 的格式（LZO、legacy LZ4）優先於「payload 範圍超出檔尾且解不出任何內容」這條：這兩種格式即使超出檔尾而解不出內容，也停止建置、不能 pin。legacy LZ4 的 bzImage payload 比照 Linux `unlz4()`（中段遇到 magic 繼續同一串流、0-size 區塊或剩不到 4 bytes 正常結束），任何失敗都先掃描已解出的部分再停止建置，永遠不能 pin；LZO kernel（`CONFIG_KERNEL_LZO`）以 `lzop -dc` 解碼後掃描，沒有或無法使用 `lzop` 時停止建置，不能 pin；bzImage 解碼器有逾時（30 秒加每 4 MiB 1 秒）與 512 MiB 輸出上限，超過時先掃描已產生的部分再停止建置。bzImage payload 只要解出過位元組，之後才在 checksum／CRC／尾端資料失敗，也一樣先掃描再以不能 pin 的錯誤停止建置；gzip 比照 kernel 的 `__gunzip`，CRC32／ISIZE 不符不算錯誤，照常解碼並掃描。內容只要曾被解出或部分解出（例如 bzImage 解出後尾端有多餘資料、`.ko.gz`／`.ko.xz`／`.ko.zst` 解出的不是 ELF、ZIP 有讀不出的成員），scanner 會先掃描已解出的位元組（IFMA 命中優先回報），再以不能 pin 的錯誤停止建置；其他非預期的例外一律轉成停止建置的錯誤，不會讓 scanner 崩潰。
- `install.sh` 會把 APEX 金鑰資訊記進 `build/params`，金鑰組改變時不會沿用快取的建置；`--update` 會把舊建置改名為 `build.prev` 再重建。

## APK／APEX ZIP 對齊

- 修正重簽 APK／APEX 的 ZIP padding；16658739 的 PackageManager 會拒絕未對齊的 APEX，先前候選因此在 KANO 上造成 `system_server` crash-loop。新增強制全分割區 APK／APEX 對齊 gate，對齊或掃描失敗即停止；新候選的交付紀錄為 291/291 通過。

## 停用 Google 的系統更新（update_engine）

- vendor 新增 `etc/init/gbos-no-ota.rc`（每個 profile 都有，不在某一機型的 hooks 裡）：在 `update_engine.rc` 啟用 `update_engine` 之後，init 用同一個觸發條件執行 `stop update_engine`；QEMU 開機中它從未啟動。Google 的 OTA 簽章仍會過，但推測這個映像做不完那次更新（增量來源區塊對不上、完整包換不了 slot），首次設定會在同一步重來；開機選單不會跟著 Google boot-control 的 slot 切換，而 Google OTA 和 `install/gbos-update.py` 會爭用 inactive slot。Play 商店更新 app 不經過 `update_engine`。KANO keep-data 更新後 uptime 65 分鐘時讀回 `update_engine` 為 stopped；尚未證明它全程不曾啟動，首次設定能否走完與 GMS 的反應也未驗證。

## lapis 16658739 與保留資料更新

- 新增 build 16658739（CL3B.260622.276.R4）與對應 Microdroid IFMA 容器的精確釘選；用 `GBOS_LAPIS_BUILD=16658739` 選用。預設仍是 16452207，不能把專案版本與 Google build 當成同一個版號。
- 更新到內建碟要使用安裝碟的「Update Googlebook on the internal disk (keep data)」，不要對內建碟使用 `write-usb.sh --keep-data`。沿用同一組 AVB 與 APEX 簽章身分，先備份；跨 Google build 開機後，舊版不一定能讀取升級過的 `/data`。步驟見 [更新說明](UPGRADE.md)。
- 2026-10-29 已在 KANO 由 16452207 slot a 保留資料更新至 16658739 slot b，完成開機且 30 分鐘以上沒有 `system_server` 重啟。逐使用者的套件清單、帳號數、設定、鎖定狀態與主要使用者的檔名比對相同；個人檔案內容、逐項 App 資料及登入有效性未驗證。成功更新後退回舊版也未驗證，不能把失敗候選的舊版開機當成這項證明。詳見 [v0.3.0 驗收紀錄](VERIFICATION-v0.3.0.md)。

## KANO 螢幕自行亮起與 EC drain

- 新增獨立選項 `GBOS_EC=drain`，不能和 `sensors`、`typec`、`buttons` 並用。它只解除 transport pair `cros_ec`、`cros_ec_lpcs` 的封鎖，並使用原本放行的 `cros_ec_proto`，消費 EC 的 MKBP 事件佇列，處理螢幕自行亮起的問題。預設仍封鎖 EC；KANO 在 timeout 入睡後約 33 分鐘沒有螢幕自行喚醒，之後鍵盤喚醒成功（`WAKE_KEY`）。S0ix、真實電源鍵、闔蓋／開蓋與音訊未驗證，33 分鐘以上的待機也未驗證。
- drain 仍封鎖 `cros_ec_dev` 等 consumers，以及 `cros_ec_hwmon`、`cros_ec_rpmsg`、`cros_ec_uart`、`cros_ec_wdt`、`gpio_cros_ec`、`rtc_cros_ec` 六個額外模組；安裝碟的命令列模板也涵蓋它們。
- 載入 transport 後，休眠失敗的復原可能重設 AP，造成資料遺失。QEMU 沒有 EC，不能證明事件排空、真實電源鍵或硬體 S0ix 正常；drain 仍是實驗選項。風險與先決條件見 [安全說明](SAFETY.md)。

## 模組檢查

- `check-ko.py` 核對 allocated 版本表與 `module_layout`，格式或版本不合就停止，避免把不相容的核心模組當成有效輸入。這是靜態檢查，不能代替實機載入證據。

## 發行與驗證範圍

- 發行產物是安裝／建置腳本封存包與 SHA-256；不提供 Google 系統映像、韌體或簽章私鑰。`tools/release/published-images.sha256` 只釘選公開文件截圖，不是磁碟映像清單。
- KANO QEMU 沒有 GPU；自我測試對完整 Android 開機驗收（`boot_completed`、`system_server` 重啟檢查）回報 NOT_RUN，不能據此宣稱完整開機成功；其餘自我測試照常把關（issue #17）。
- 軟體 gate 與實機證據分開記錄；結果與未驗證範圍見 [v0.3.0 驗收紀錄](VERIFICATION-v0.3.0.md)。
