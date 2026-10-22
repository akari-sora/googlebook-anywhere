# Googlebook Anywhere release history

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
