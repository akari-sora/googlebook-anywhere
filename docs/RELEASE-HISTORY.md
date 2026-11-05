# Googlebook Anywhere release history

# Googlebook Anywhere v0.3.0


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

# Googlebook Anywhere v0.2.3


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

# Googlebook Anywhere v0.2.2


v0.2.2 把已核准的 AMD HX 370 變更接到 v0.2.1 上（v0.2.1 與更早的內容見完整版本紀錄）。`amd-hx370` 仍是實驗 profile，只在 QEMU 驗證過，沒有在實體硬體上跑過。

- AX210 的 Wi-Fi 與藍牙韌體釘在 linux-firmware 20260810。
- `[modules]` 在 early-boot 依序載入 `bluetooth`、`btusb`、`snd_hda_codec_alc269`、`iwlmvm`、`snd_hda_intel`。allowlist 在 `profiles/_safety.toml`，kano 與 generic-x86 沒有這些列。
- `libminigbm_gralloc.so` 依 `tools/minigbm` 的配方重建，含 `backend_amdgpu`，並釘住 sha256、硬化旗標與 `tools/minigbm/NOTICES.txt`。
- CI 的 builder job 先安裝 `gcc` 與 `libc6-dev`，再以 `tester` 使用者執行測試。
- `tools/minigbm/build-libminigbm-gralloc.sh` 寫出的 `SOURCES.txt` 帶有 `output` 區塊，家目錄路徑寫成字面 `$HOME`。
- 第 1 階段在換上 ESP initrd 之前，以 `builder/kmod/check-firmware.py` 讀取全部模組 `.modinfo` 的 `firmware=` 宣告，並寫出 `<GBOS_OUT>/firmware-report.json`；這個檢查只對 profile 選用或 builder 加入的必要模組把關，這些模組缺韌體、metadata 讀不出來或不存在時會停止建置。目前 `kano`、`generic-x86` 與 `amd-hx370` 三個 profile 的必要模組都沒有宣告韌體，真正宣告韌體的驅動（`iwlwifi`、`btintel`、`xe`、`amdgpu`、SOF）都只記錄不攔截（report-only），所以 `PASS` 不代表這些韌體釘選已被這個檢查核對。

## 驗證紀錄

- 最終 head 以 CI 的全套指令驗收：私有樹與公開匯出樹各跑 py_compile、`tests/builder/run.sh`、probe、report、stick、update、make-maint、public-export、release-notes-gate、install 與 harness（harness 兩次），再加上從 `.gitlab-ci.yml` 取出的 package gate，分別用 mawk 與 gawk。程式碼的最後一個 commit `c4cad72`（之後只改文件）在 Linux 測試主機（`/bin/sh` 是 dash）實跑的結果：私有樹與公開匯出樹各 15/15 步驟 rc=0（builder 265 passed、probe 60、report 467、stick selftest 72/72、update selftest OK、update mutations 25/25 killed、make-maint 271、install-sh 102、kano-check 15、harness 兩次）；mawk 與 gawk 的 parsed package gate 在 notes 維持開發狀態時 rc=1，第 1 行與第 3 行翻成這一版的正式標題與 released 狀態後 rc=0；`make-public` 匯出 528 個檔案。
- `amd-hx370` 的 safe 選項載入重建的 gralloc（`backend_amdgpu`，sha256 釘在 `profiles/amd-hx370/profile.toml` 的 `[gralloc]`）。QEMU 自我測試開這個 safe 選項，畫面到設定精靈。native 選項沒有在 QEMU 開，因為 QEMU 沒有 amdgpu。QEMU 自我測試用的映像建自 v0.2.2 rebase 之前的 AMD 分支（映像 sha256 `65de4a4a3e1ed7b2372a26adf73a5b7b518bb0a3c53fd1d39fe0eeec6e0a5c5c`）；之後 builder 只多了建置時的模組韌體檢查（不改映像內容）與 minigbm 建置腳本的小修，`amd-hx370` 的 profile 與 gralloc 沒有改，v0.2.2 的 head 沒有重建這個映像。
- `kano` 與 `generic-x86` 的 profile 檔與 v0.2.1 相同（沒有 `[gralloc]`）；新的模組韌體檢查是以真實映像解開的分割區實測，沒有為這兩個 profile 重跑完整建置（`e2e`）。

## 已知問題與限制

- `amd-hx370` 的 native 項目從未在實機上跑過。出現黑畫面時改選 safe。
- `GBOS_MINIGBM` 要從 git repo 的 `tools/minigbm/build-libminigbm-gralloc.sh` 建。發佈包沒有這支腳本，只有 `tools/minigbm/NOTICES.txt`。
- `tools/minigbm/SOURCES.txt` 只記錄這一台建置機的重建，沒有第二台主機的比對。

# Googlebook Anywhere v0.2.1

這一版多了一條安裝碟流程：從你自己建的 Googlebook 映像做出**一個**檔，開機後可以選「Install Googlebook to the internal disk」，把 Googlebook 裝到 KANO 的內建碟。另外，KANO 家族的內顯放行從 1 個 ID 擴到 12 個，新增一個 AMD 實驗 profile。測試映像自動關機（`GBOS_TEST_AUTOOFF`）已核准。硬體回報是 round-13 已核准的 `install/gbos-report.sh` 與 `tests/report/report-test.sh`。

## 安裝碟

- `install/make-installer-stick.sh` 只寫一個普通檔（旁邊有 `.sha256`），不開區塊裝置，也不需要 root。檔案含 Googlebook（到最後一個分割區為止）、512 MiB 的 XBOOTLDR（維護核心、initramfs，以及安裝、保留資料更新、兩個內建碟選項；沒有普通 Maintenance 殼層）、7936 MiB（8321499136 bytes）的 ext4 `kano-maint`（擁有者 uid 0：`gbos-install` 與服務、`gbos-update.py`、pristine、`module-blacklist.txt`、`builtin-initcalls.txt`）。
- 成功時印 `minimum_stick_sectors`。預設檔案就是這個大小（512-byte 磁區），可以稀疏存放。Googlebook 複製到 LBA 37244880 的代表映像是 54546417 磁區、27927765504 bytes。報 28.0 GB 的隨身碟是 28000000000 bytes（28×10^9），裝得下這份檔。`write-usb.sh` 拒絕比映像檔還小的碟。別的映像用它自己印出的 `minimum_stick_sectors`。
- 寫進隨身碟仍用 `install/write-usb.sh`：只接受 USB 整顆碟、要輸入序號、寫完回讀，備份 GPT 在碟尾。不要加 `--keep-data`。
- 維護系統已經在檔案裡。開機後選安裝項目即可，不必再跑 `setup-stick.sh`。
- `install.sh` 再 `write-usb.sh` 寫出的仍是沒有維護系統的 Googlebook 隨身碟。裝到內建碟要用這個安裝碟檔。見 README 與 docs/INSTALL-INTERNAL.md。
- Linux 測試主機上用 QEMU 測試 `make-installer-stick.sh` 做出的檔（沒有把 ext4 注入那份檔）：`tests/stick/qemu-install.py --asis` 對開機副本本身核對 sha256、以 `snapshot=on` 原樣開機（不改選單或 loader default），印出 `AS-IS ACCEPTANCE OK (40/40)`；`tests/stick/qemu-install.py --produced` 則在核對交付選單後，把開機副本的 loader default 改指額外序列埠測試項目，分開印出 `STICK INSTALLER TEST OK (98/98)`。來源檔案的 sha256 在兩次測試前後皆與公布的 `.sha256` 相同。那不是在真的 KANO 上安裝。

## KANO 家族：內顯放行從 1 個 ID 擴到 12 個

v0.2.0 的 KANO profile 只放行一個內顯 ID（`xe.force_probe=46a6`），所以只在 `8086:46a6` 的 KANO SKU 上能用（i7-1260P，以及 coreboot 列出的 i5-1240P、i7-1280P）。其他 KANO SKU 的內顯在 `xe_pci_probe()` 被以 `-ENODEV` 拒絕，而 lapis 沒有備援的 DRM/fbdev 驅動，結果是黑畫面或凍結。v0.2.1 把 `profiles/kano/profile.toml` 的 `xe.force_probe` 換成 12 個 PCI ID：

`46a6,46a3,46a8,4626,4628,46b2,46b3,a721,a7a1,a7a9,a7ac,a7ad`

- `46a6` 排在最前面，不使用萬用字元 `*`。
- 這 12 個涵蓋 12 代 ADL-P/U 與 13 代 RPL-U，含單通道記憶體變體（`4626`、`4628`）。
- 7 個 ID（`46a6`、`46a8`、`4626`、`4628`、`46b3`、`a7a1`、`a7a9`）由 SKU 表直接對到 KANO 零售型號；另外 5 個（`46a3`、`46b2`、`a721`、`a7ac`、`a7ad`）只是核心 ADL-P／RPL-U ID 巨集裡的平台候選，KANO 零售對照尚未確認。
- 沒有 SKU 列的 ADL-P ID、ADL-N、RPL-S，以及 RPL-P 的 ID 都不在名單裡。
- 13 代 CP714-2WN（Zydron）共用 KANO 主機板，但音效走 Raptor Lake 拓撲，另補 `intel/sof/community/sof-rpl.ri` 與 `intel/sof-tplg/sof-rpl-max98373-nau8825.tplg`。

只有驗證機 Acer Chromebook Spin 714（KANO）的 Alder Lake-P 工程樣機、內顯 `8086:46a6` 是實機驗過的。**新放行的 11 個 ID 與 13 代音效都還沒在任何實機上驗證過**，預期能修好原本黑畫面的 SKU。

## AMD HX 370（實驗性，`amd-hx370`）

- 新的實驗 profile，從來沒有在實體硬體上跑過。
- 提供兩個開機項目：預設「safe」不載入 `amdgpu`；另一個「native amdgpu (experimental)」載入原生 `amdgpu`。
- 用自建核心時（有設 `GBOS_DEV_KERNEL_DIR`），builder 會先檢查核心設定；只要設定讓系統可以寫入 EFI 變數或韌體，就停下來拒絕，不會做出映像。
- 只在 QEMU 驗證過。釘選核心 `kernel/common` `3938c8d97142d5cf31543aa96f864a2cf832dda0`。
- 若機器開著 Secure Boot，改設定前先備份 BitLocker 復原金鑰，否則可能無法解密。
- 合成的壓縮尾（gzip 尾端沒有 `IKCFG_ED`、bzip2 尾、xz 的 `"none"` 夾具，以及「最多少 1 byte」）不在這個閘門的威脅模型裡。見 docs/SAFETY.md。

## 新工具

- **硬體回報**（round-13 已核准，`install/gbos-report.sh`，測試 `tests/report/report-test.sh`）：`install/gbos-report.sh` 產生可以直接貼上回報單的報告，不含自由文字，不含序號。
- **測試映像自動關機**（`GBOS_TEST_AUTOOFF`，開發者用，已核准）：v2g 起，查詢錯誤不再當成 nonce 不存在；刪除失敗就停在 `resize.f2fs`、`fsck.f2fs` 與 ESP 寫入之前。v2g 之後的這一版再比對複製後的 p1：核心看到的起點與長度必須等於新的 userdata，讀不到或不相等就停止，不進檔案系統。新的 p1 比要複製的 userdata 短，也在 userdata 複製之前停止。`install.sh` 不接受這個開關。

## 相容性

哪些機器可以裝、各機型的支援程度，以及還沒實機驗證的項目，見 docs/COMPATIBILITY.md（英文 docs/COMPATIBILITY.en.md）。排錯見 docs/TROUBLESHOOTING.md。

## 驗證紀錄

- KANO profile 與 builder 測試（`tests/builder/profile-test.py`、`tests/builder/run.sh`）要 0 failed。profile 測試會擋下 12 個 ID 少列／多列、重複、大小寫不符、`46a6` 不在第一個，以及出現萬用字元 `*` 的漂移。
- KANO 開機命令列：核心上限 1973 bytes，builder 再保留 64 bytes 餘裕（限制 1909 bytes）。debug 1879、quiet 1787；三個 `console=` 還原後是 1969 bytes，超過限制，所以維持關閉。
- 硬體回報：`bash tests/report/report-test.sh`，0 failed。
- 自動關機與 harness：`tests/harness/run.sh` 用 strict runner，0 survived、0 errors。
- 安裝碟：`AS-IS ACCEPTANCE OK (40/40)`、`STICK INSTALLER TEST OK (98/98)`。

## 已知問題與限制

- **KANO 待機**：仍是 v0.2.0 的 `gbos_wakefilter.ko`（改寫未核准，不在這一版）。電源鍵沒有喚醒系統時丟掉 `KEY_WAKEUP`。KANO 實機上闔蓋 300 秒期間暗中喚醒 10 次，螢幕都沒亮；螢幕關閉時按電源鍵會亮、開蓋會亮螢幕。**已知問題**：螢幕關著時按鍵盤可能叫不醒螢幕（喚醒那一下按鍵送到 Android 之前，模組已經把 `KEY_WAKEUP` 丟掉了），請用電源鍵或開合上蓋。v0.2.0 發行說明寫的「預計 v0.2.1」沒有進這一版。
- KANO：沒有進到 S0ix（待機時 CPU 已進最深的省電狀態，但平台沒有），所以自動旋轉、光感測、USB-C 的 EC 功能仍關閉。
- **內建相機**：開發中，預設關，Android 上未驗證（in development, off by default, not verified on Android）。
- KANO 的內建碟「保留資料更新」還沒在實機上跑過，只在 QEMU 驗證；更新前請照 docs/INSTALL-INTERNAL.md 備份。
- KANO 新放行的 11 個內顯 ID（`46a6` 以外）與 13 代音效都還沒在實機上驗證過。v0.2.0 只放行內顯 `46a6`；其他內顯 ID 的 SKU 會黑畫面（未在這台開過的零售與 coreboot 列出的 SKU 為推論）。
- **CPU 需求**：先前寫的「只需要 AVX」不正確。lapis 的 `/init` 和許多系統函式庫不先檢查 CPU 就用 AVX2、GFNI、AVX-VNNI、SHA-NI（`libcrypto` 另用 256-bit VAES），預期只有 Intel 第 12 代及更新、AMD Zen 5 及更新能用（第 11 代及更早、Zen 4 及更早預期會失敗），而且只在 KANO（Intel 第 12 代）實機驗證過。另有 16 個含 Rust 程式碼的 ELF 檔（其中 `libzmij` 3 份、`ectool` 2 份）含 KANO 和 Zen 5 都沒有的 AVX-IFMA 指令（只在把浮點數轉成文字時用到），碰到時那個程式會閃退，目前沒遇過。見 docs/COMPATIBILITY.md「CPU 最低需求」。
- generic-x86 還沒在任何實體電腦上開過；沒有 GPU 加速；Wi-Fi、音效等需要機型韌體的功能大多不能用。內建碟是 eMMC 的電腦，Googlebook 看得到那顆碟（不要對它格式化）。
- `amd-hx370` 從未在實體硬體上跑過。
- 其他見 README「現況」與 docs/STATUS-KANO.md。

---

# Googlebook Anywhere v0.2.0

第二個版本：**修好新使用者建置會失敗的問題**，加上通用 x86 的實驗版、機型 profile、保留資料的更新，以及 KANO 的充電顯示與待機修正。

**這個下載檔只有腳本與文件，不含任何 Google 映像或韌體。** 使用方式與 v0.1.0 相同，見 README。

## 先看這個：v0.1.0 的新使用者建置會失敗

ci.android.com 把下載轉到一個 300 秒後失效的簽章網址，而 `install.sh` 第 2 步要從那裡下載 15 個檔案，一般網路要超過 5 分鐘，
於是在第 2 步以 `HTTP Error 400` 停止（2026-10-13 重現兩次）。開發機因為有快取一直沒遇到。v0.2.0 每 120 秒重新取得簽章網址，
被拒時再取一次（`base/gbos_base.py`，測試 `tests/builder/fetch-zip-test.py`）。**用 v0.1.0 的人請直接改用 v0.2.0。**

## 新功能

- **一般 x86 電腦（實驗性）**：`profiles/generic-x86`，給 Googlebook 核心沒有 GPU 驅動的 x86-64 UEFI 電腦（AMD、較舊的 Intel）（v0.2.1 更正：較舊的 Intel 預期因 CPU 指令不足而無法開機，見 docs/COMPATIBILITY.md「CPU 最低需求」）。
  畫面用韌體的 framebuffer（`gbos_efifb.ko`，釘選建置），繪圖用 CPU（SwiftShader，從 Cuttlefish 公開建置取得並核對 SHA-256）。
  建置：`GBOS_PROFILE=profiles/generic-x86 install/install.sh --work ~/gbos-generic`。**只在 QEMU 驗證過**，見 README「其他 x86-64 電腦」。
- **唯讀相容性檢查** `install/gbos-probe.sh`：在目標電腦上的任何 Linux 執行，列出 Googlebook 有沒有它的驅動，並產生 profile 草稿。
- **機型 profile**：`profiles/<機型>/profile.toml`；全專案的安全底線在 `profiles/_safety.toml`（每個 profile 都必須封鎖的 57 個模組）。
- **保留資料的更新**：隨身碟使用者 `install/update.sh`（`write-usb.sh --keep-data`）；安裝到內建碟的人用安裝碟的
  「Update Googlebook on the internal disk (keep data)」。新版本寫進另一個 A/B slot，`/data` 保留，舊版本留在選單上。
  見 docs/UPGRADE.md。
- **KANO**：
  - 「充電中」圖示：ACPI 充電器驅動 `ac.ko`（只讀 ACPI，不送 EC 指令）。KANO 實機驗證過。
  - 待機：`gbos_wakefilter.ko` 讓暗中喚醒不再亮螢幕。KANO 實機上闔蓋 300 秒期間暗中喚醒 10 次，螢幕都沒亮；螢幕關閉時按電源鍵會亮、開蓋會亮螢幕。
  - 內建相機：做成機型選項（`camera.libcamera`，預設關），開發中。
  - 拿掉了 plain 開機選項。

## 驗證紀錄（發版前在開發機上跑過）

- 新使用者完整流程：KANO 與 generic-x86 各一次（官方 zip → 新 AVB 金鑰 → 映像 → 兩個 QEMU 自我測試），全部 PASS。
- 更新：從 v0.1.0 的使用者映像保留資料更新到 v0.2，再更新第二次（QEMU 29/29）；安裝碟的更新情境，用真隨身碟的維護系統（QEMU 55/55）；
  更新引擎的 selftest 與 mutation 檢查（每個列出的 mutant 都被測試抓到）。
- KANO 實機（2026-10-13，隨身碟上的 v0.2 映像）：充電顯示、闔蓋待機（暗中喚醒不亮螢幕）、螢幕關閉時電源鍵與開蓋會亮螢幕。
- CI（每次 push）：shellcheck／py_compile、builder 快速測試、安裝與寫入的防呆測試、安裝碟核對邏輯、harness 防呆與 mutation 檢查。

## 已知問題與限制

- **KANO：螢幕關著時按鍵盤可能叫不醒螢幕**（喚醒那一下按鍵送到 Android 之前，`gbos_wakefilter` 已經把 `KEY_WAKEUP` 丟掉了）。
  請用電源鍵或開合上蓋。修正正在開發與審查（不在這一版裡），預計 v0.2.1。
- KANO：沒有進到 S0ix（待機時 CPU 已進最深的省電狀態，但平台沒有），所以自動旋轉、光感測、USB-C 的 EC 功能仍關閉。
- KANO 的內建碟「保留資料更新」還沒在實機上跑過，只在 QEMU 驗證；更新前請照 docs/INSTALL-INTERNAL.md 備份。
- generic-x86 還沒在任何實體電腦上開過；沒有 GPU 加速；Wi-Fi、音效等需要機型韌體的功能大多不能用。內建碟是 eMMC 的電腦，
  Googlebook 看得到那顆碟（不要對它格式化）。
- 其他見 README「現況」與 docs/STATUS-KANO.md。

---

# Googlebook Anywhere v0.1.0

第一個版本：把 Google 官方 Googlebook（lapis，build 16452207）轉成 Acer Chromebook Spin 714（KANO）可開機的隨身碟映像。

**這個下載檔只有腳本與文件，不含任何 Google 映像或韌體。** 執行 `install/install.sh`（Linux，或 Windows 的 WSL2）或
`install/docker-install.sh`（macOS 的 Docker Desktop；Windows 上也要在 WSL 裡執行），會從 Google 官方下載映像並在你的電腦上建置。
開發用的 `tools/`（實機 harness、發版腳本）與 `tests/harness/` 不在這個下載檔裡，只在 git repo。

## 驗證紀錄（發版前在開發機上跑過）

- 新使用者完整流程（官方 zip → 新 AVB 金鑰 → 映像 → 兩個 QEMU 自我測試）：全部 PASS。
- 預設映像（quiet、無 adb）寫到隨身碟、空白 /data，在 KANO 實機開到歡迎畫面（SetupWizard、BOOT_COMPLETED）。
- 可重現性：開發者設定重建的映像與實機驗證過的版本，每個分割區與 ESP 檔案相同（除了刻意變更的開機選單設定）。
- 開了 adb 的開發者 build（KANO 實機）：`ro.adb.secure=1`、只聽 TCP 5555；沒登記的金鑰被拒（`unauthorized`，沒有授權對話框），
  登記的金鑰是 `uid=2000(shell)`。builder 寫進 vendor 的 `ro.debuggable=1` 沒有生效，實機讀到 `ro.debuggable=0`（見 SAFETY.md）。
- CI（每次 push）：shellcheck／py_compile、builder 快速測試、`install.sh` 參數測試、write-usb 的拒絕測試與部分防呆（用 loop 假碟的
  案例在 CI 容器裡 SKIP，在 Linux 開發機上跑過）、韌體檢查、安裝碟核對邏輯的 selftest、harness 的防呆測試與 mutation 檢查。

## 這一版的防呆

- `write-usb.sh`：寫入前就要有 `sgdisk`；備份 GPT 由寫入程式在同一個獨占開啟的裝置上搬到碟尾並核對；沒做過 QEMU 自我測試的映像要多輸入 `untested`。
- `install.sh`：重用工作目錄前比對建置設定（含 `GBOS_DEFAULT_ENTRY`、`GBOS_LOADER_EDITOR`）與原始碼內容摘要，不同就停止。
- `docker-install.sh`：macOS 內建的 bash 3.2 可用；路徑不能含 `:`；Linux 主機的 KVM 群組會帶進容器。
- `kano-check.sh`：認不出 CPU 或韌體時是 `UNKNOWN`，不會給 `GO`。

## 已知限制

見 README「現況」與 docs/STATUS-KANO.md：自動旋轉、光感測、充電中圖示、鍵盤背光調整、內建相機不可用；觸控螢幕點擊、
耳機孔、USB-C 資料／DisplayPort 未在實機驗證。只支援 KANO（量產版）。
