# 做了哪些修改、為什麼

每一項都在 KANO 實機上驗證過；括號內是 `builder/vendor.sh` 開頭註解裡的版本代號（safeN）。

## 建置流程

1. `base/build-base.sh`：官方 recovery 映像 → 基礎 GPT 映像（中間產物，不能直接開機；ESP 放 systemd-boot、`kernel.efi`、`initrd.img`），
   vendor 換成軟體 KeyMint／Gatekeeper，`vbmeta_a` 用你的金鑰重簽。用原本的金鑰重建時，輸出與 2026-10-11 手工做出的映像逐位元組相同
   （`base/RESULT.md`）。
2. `builder/fetch-inputs.sh`：機型設定（`profiles/kano/profile.toml` 的 `[[firmware]]`）列的韌體（linux-firmware 20260810、
   sof-bin v2025.12.2）與 ChromeOS 音效設定（board-overlays 固定 commit），每個檔案都核對 SHA-256。
3. `builder/vendor.sh`：解開 vendor EROFS，套用下面所有修改（機型相關的部分來自機型設定與它的 `vendor.py`），用 otatools 的 mkfs.erofs 重建（固定時間戳與 UUID，uid／gid／mode／
   SELinux 標籤從原映像逐一抄寫），再比對新舊 vendor 樹：新增、刪除、變更的檔案必須剛好是預期的清單。接著加 hashtree、重簽 vbmeta，
   更新 ESP initrd 的 bootconfig。
4. `builder/image.py`：ESP 的 initrd 與兩個開機選項（quiet、serial-debug；vendor_boot 的原廠參數＋機型設定＋`profiles/_safety.toml`，由
   `builder/lib/devprofile.py` 組合），然後兩個 QEMU 自我測試：
   - 功能測試：把 kernel、initrd 交給 QEMU，但先拿掉 debug 選項裡的 `console=`／`earlycon=`，改成 COM1 的
     `console=ttyS0` 與 I/O `earlycon`。QEMU 沒有 KANO 的 MMIO UART。這是 Android 功能測試，不是原樣 debug 開機，也不能代替實機 UART。
   - 開機鏈測試：不給 kernel，讓韌體從映像自己的 ESP 開 systemd-boot，跑預設選項，選項不做改寫。只在預設是 quiet 時跑。
     quiet 沒有序列埠輸出，所以改看磁碟：QEMU 在時限結束時仍在執行、整顆隨身碟累計寫入 16 MiB 以上，而且最高的寫入位置落在 userdata 內
     （只有 Android 掛上 /data 才會寫到那裡）。
   全部通過才把 `<name>.partial` 改名成正式檔名。

`vendor.sh` 在完成映像前執行 `builder/kmod/check-firmware.py`：離線讀取最後 vendor 樹（含額外／自建模組）、
原廠 vendor_dlkm／system_dlkm 與 ESP initrd 的每個 `.ko` 的 `.modinfo`，逐一核對 `firmware=`（也就是
`modinfo -F firmware`／`MODULE_FIRMWARE`）與映像實際安裝的 `/vendor/firmware`，不查主機或下載快取。
必要模組是 profile 的 `kmods`、`wifi.modprobe`、`[modules].load/unblock`，以及 builder 新增／替換的 vendor 模組
（與原始 vendor 樹比對，含 `builder/kmod` 與 hook 新增的模組；`GBOS_DEV_KERNEL_DIR` 的 `lib/dev-modules` 則依 profile 選取）、啟用 `pci_stub_ids` 時要加入 initrd 的 `pci-stub`。
這些模組缺少必要檔案會停止建置，訊息列出 profile、模組與韌體名稱；萬用字元至少要找到一個非空檔案。
其餘原廠模組只報告，缺韌體或模組 metadata 錯誤都不會擋住建置；不因檔案存在、黑名單或間接依賴就提升為必要。
數字版本不會自動視為相容：若驅動確實會嘗試一段版本，profile 可加 `[[firmware_check.alternatives]]`，指定
`module`（精確名稱，連字號改底線）、`firmware`（宣告名稱或 glob）、`files`（可接受路徑／有限版本 glob 的陣列）
與 `reason`。`[[firmware_check.optional]]` 用同樣的 `module`、`firmware`、`reason` 明列可缺少的檔案；空白理由不接受。
例如 `firmware = "iwlwifi-so-a0-gf-a0-99.ucode"` 可在有驅動版本範圍依據時，配
`files = ["iwlwifi-so-a0-gf-a0-8[3-9].ucode", "iwlwifi-so-a0-gf-a0-9[0-9].ucode"]`。
optional／alternatives 規則只套用必要模組；若規則點名了屬於 report-only 的模組，不會把該模組升為必要，但會在建置 log 印出一行警告並記入報告的 `warnings`。結果（每個模組的 `scope`、`required_by`、來源樹相對路徑、逐檔狀態，含 optional 理由及替代檔）
寫入 `<GBOS_OUT>/firmware-report.json`；optional、未使用規則警告與一行 `firmware report-only:` 原廠模組缺檔摘要也印在建置 log，`firmware check:` 結果行並同時印出必要模組數與必要韌體宣告數（例如 `required: 4 modules, 0 firmware declarations`）。
目前的 `kano`、`generic-x86` 與 `amd-hx370` 三個 profile 中，必要模組都沒有宣告任何韌體；真正宣告韌體的驅動（`iwlwifi`、`btintel`、`xe`、`amdgpu`、SOF 等，含間接依賴或開機腳本載入的模組）目前都屬於 report-only，因此 `PASS` 不代表那些釘選已被這個檢查核對。
這只檢查必要模組靜態宣告的韌體，不保證硬體可用，也不涵蓋驅動未宣告、執行時才組合出的檔名；既有 profile 的 `[[firmware]]` 韌體 SHA-256 釘選仍由 `fetch-inputs.sh` 與 `vendor.sh` 核對。

## 開機與顯示

- **自帶 systemd-boot 的 USB 映像**：base 階段把官方 recovery 映像轉成 GPT 映像，ESP 放 `kernel.efi`、`initrd.img` 與 loader
  entries。這個中間產物**還不能開機**（開機選項是原廠參數、initrd 的 vbmeta digest 是舊的），只給 builder 用；builder 重寫 ESP 的
  initrd 與開機選項之後，才是給 UEFI 韌體開機的映像。
- **vbmeta 用你自己的 AVB 金鑰重簽**：vendor 改了，頂層 vbmeta 必須重簽；bootconfig 的 `androidboot.vbmeta.digest/size`
  同步更新，`androidboot.boot_part_uuid` 指向這顆碟的 boot_a。
- **軟體 KeyMint／Gatekeeper**：KANO 沒有 pKVM protected VM，改用 `rust_nonsecure` / `gatekeeper.nonsecure` APEX。
- **`xe.enable_dpt=0 xe.enable_psr=0 xe.enable_fbc=0`**：這版 xe（6.18.21）透過 stolen memory 裡的 DPT 掃描 tiled
  framebuffer 會出錯，畫面全黑並出現 plane fault（CVE-2026-80915，上游修正 `fc648757908304aedbad74f74bf58192aec383db`
  「drm/xe: Fix DPT allocation paths」，6.18.y 的 backport 是 `491c499295fb`），關掉 DPT 就正常。
- **pci-stub 先於 intel-lpss 載入**（initrd 第一個模組；`pci-stub.ids=8086:51a8,8086:51a9,8086:51aa,8086:51ab`）：
  LPSS UART（序列埠）與指紋 GSPI 交給 pci-stub，intel-lpss 只接 I²C，觸控板、觸控螢幕、音效 codec 才能用，序列埠也不會被重設。
- **兩個開機選項**：quiet（預設，不輸出序列埠）與 serial-debug（開發用）。serial-debug 留在 UART 上的是
  `earlycon=uart8250,mmio32,0xfe03e000,115200n8` 加上 `keep_bootcon`（printk 的 bootconsole，不是可登入的 tty）；
  `console=ttyS0`／`ttyS4`／`uart8250` 沒有放回去。quiet 本來就沒有這三個 `console=`。兩邊還差 log level。
  `image.py` 會檢查命令列長度（lapis 的 x86 上限再扣 64 bytes，給 harness 附加除錯參數）。base ESP 原本的 `lapis.conf`
  （Google 的參數，沒有 KANO 的封鎖）會被刪掉，`image.py` 也確認 ESP 上只剩這兩個選項。早期版本另有一個沒有 `pci-stub.ids`
  的 plain 選項，在 KANO 上面板一直是暗的，已不再產生。

## 儲存與首次開機

- **/data 用 f2fs、拿掉 FBE 與 `checkpoint=block`**：慢速 USB 碟上 ext4 的 fsync 會拖過 watchdog（safe7、safe10）。
- **`ro.cp_system_other_odex=0`**：首次開機複製 odex 太久（safe8）。
- **`persist.vold.dont_commit_checkpoint=1`**（`kano_bootfix` 服務）：vold 的 checkpoint commit 會呼叫 boot HAL 的
  markBootSuccessful → crossystem（韌體 NVRAM，已封鎖）而失敗重開（safe12/13）。
- **移除 IWeaver、ISecretkeeper 的 VINTF 宣告**：它們需要 protected VM，LockSettings 會一直等（safe11）。
- **停掉 timberslide、gscd、udsattestation**：需要 EC／Cr50，只會崩潰循環（safe21/23）。
- **system_server watchdog 5 分鐘**（`Settings.Global.system_server_watchdog_timeout_ms`）。

## 硬體

- **Wi-Fi／藍牙／音效韌體**：AX211（`iwlwifi-so-a0-gf-a0-89`、pnvm、`ibt-0040-0041`）、SOF `sof-adl.ri`／`sof-rpl.ri` 與
  `sof-adl-max98373-nau8825.tplg`／`sof-rpl-max98373-nau8825.tplg`，加上 ADL-P／RPL-P 共用的 GPU 韌體（DMC、GuC、HuC），全部只載入 RAM、不燒錄；載入 `iwlmvm`。
  內容固定在 linux-firmware 20260810 與 sof-bin v2025.12.2，別的版本會被拒絕（`GBOS_ALLOW_FIRMWARE_DRIFT=1` 才放行，未經實機測試）。
- **音效設定（DRAS）**：ChromeOS 官方 kano UCM（board-overlays 的 `ucm-config/sof-nau8825.kano/HiFi.conf`）是**人工翻譯**的參考來源：
  `audio_config.xml` 的 PCM 編號與 `mixer_paths.xml` 的控制項寫在 `profiles/kano/vendor.py` 裡，build 時不會讀 HiFi.conf。
  只有音量曲線從 `sof-nau8825.card_settings` 讀取並轉換。`hal_configs/audio_default.xml` 指向 `sof-nau8825`。
  官方 DSM 喇叭保護參數沒有公開，喇叭音量曲線整體降 4.75 dB（100% = -10 dB）。
- **鍵盤背光路徑**：KANO 的 GOOG0002 在 EC 節點底下（`platform/GOOG0004:00/GOOG0002:00/leds`），idc／ueventd／genfscon
  一起改（這台 EC 本身不調亮度，見 STATUS）。
- **電源鍵**：lapis 排除 ACPI「Power Button」（它的電源鍵來自 cros_ec_buttons）；KANO 的 cros_ec_keyb 被封鎖，所以要放行
  ACPI 電源鍵（safe25）。

## 開發用元件

除了 `kano_adb` 之外，這些元件目前在每個 build 都會包含，沒有開關（安全上的影響見 SAFETY.md「資料安全取捨」）：

- `kano_diag`：每 120 秒把負載、I/O 寫到 `/data/kano-diag`（fsync）；開機約 4 分鐘時存一次 dmesg 與 logcat。
- `kano_logcat`：只在 serial-debug 選項下把 logcat 鏡像到序列埠，其他選項不會啟動。
- `kano_audiotest`：只有 `/data/kano-audiotest.enable` 存在時才播測試音。
- `kano_wifi`：只有 `/data/kano-wifi.conf` 存在時才連線（開發用維護系統寫入）。
- vendor build.prop 的 `ro.debuggable=1`：這一行實際上沒有生效，KANO 實機讀到的是 `ro.debuggable=0`（見 SAFETY.md）。
- `kano_adb`：只有 build 時提供 `GBOS_ADB_PUBKEY` 才會包含（金鑰驗證的 TCP 5555 adb）。

`kano_autooff` 不在上面這份清單。只有開發者的 TEST 映像（`GBOS_TEST_AUTOOFF`＝60..7200 秒）才會放進這個服務。它要斷電，`/vendor` 裡的 nonce 和 `/data/kano-autooff.nonce` 必須都在，而且是同一份 33 bytes。`/data` 那一份只由這次硬體 session 寫到已核對身份的 USB 隨身碟 userdata，用完就刪。沒有這份檔，碟上即使有 TEST vendor 也只會 staying on（QEMU 自我測試就是這個情況：`image.py` 的 `autooff_log_checks` 要求出現 `nonce gate closed, staying on`，而且同一份 log 不能有 `powering off`）。

斷電只能靠 `/data` 上的 nonce。會把隨身碟的 userdata 複製到內建碟的，只有 `tools/harness/kano-nvme-install.sh`。這條路的安全保證是：複製並掛上之後清掉 `kano-autooff.nonce`（一般檔、符號連結或目錄）。一般檔與符號連結用 `rm -f`，不跟隨符號連結。目錄在同一個遠端 python3 裡刪除：`os.lstat` 必須是真目錄而不是符號連結，`shutil.rmtree.avoids_symlink_attacks` 必須為真，`rmtree` 的錯誤會讓 helper 非 0；檔名不經過文字紀錄。卸載後再以唯讀重新掛上，同一個名字的 `lstat` 必須是 ENOENT，然後再卸載。掛載失敗、根目錄列不出來、查找錯誤（errno 不是 ENOENT）、刪不掉、重新掛載失敗、複查失敗或任一次卸載失敗，就以非 0 停止，不再 `resize.f2fs`、`fsck.f2fs`，也不再寫 ESP。只有 ENOENT 算這個名字不存在。任何 lstat 發生 ENOENT 以外的查找錯誤就停止；刪除後及重新掛載後的確認，必須得到 ENOENT，成功查到任何物件也停止。`dd` 完成之後、這次清除成功之前的任何停止（行程被殺掉、斷電、ssh 中斷）都可能留下檔案；清除失敗則不會繼續。之後只更新 super 的路不會清掉已經留下的 nonce。vendor 掃描是縱深防禦：寫入前拒絕解析得到 `etc/kano-autooff.nonce` 或 `etc/init/kano-autooff.rc` 的映像，讓 TEST 服務不要留在日常碟上。`blkdiscard` 前先確認 `/dev/sda1` 的 PARTLABEL 是 `userdata`，再以 `-o ro` 掛上；f2fs 即使唯讀掛載仍可能把先前的 fsync 補完。已有一般檔、符號連結或目錄 `kano-autooff.nonce`，或列出根目錄／`lstat` 失敗（不是 ENOENT），就拒絕。這個 repo 沒有寫 session nonce 的程式。

只寫 super 或開機分割、因此建不出 `/data` nonce 的路，不會因為裝上 TEST vendor 就斷電：`kano-nvme-update.sh` 在推送前會拒絕 TEST，但它不清 userdata；直接呼叫的 `kano-delta-push-nvme.py` 只寫 super 與 vbmeta_a/b（userdata 有變動就拒絕），不掃描 vendor。安裝碟的更新走 `gbos-update.py apply`：寫入的是目標 slot、super 裡的動態分割、LP metadata，以及 ESP 選單。`guard_ranges` 把 userdata 列進不可寫範圍，`pwrite` 碰到就拒絕。`rollback` 只改 ESP 的 loader 項目，不寫 userdata。`NEVER` 那一行是說明，擋住寫入的是上述範圍。安裝碟的全新安裝把內建碟的 userdata 建成新的，開頭 16 MiB 寫零，不複製隨身碟的 userdata。`install.sh` 與 `docker-install.sh` 不寫內建碟，它們在建置 USB 映像之前拒絕這個開關。`install/update.sh` 先呼叫 `install.sh`；若帶了 `--disk`，接著 `exec sudo install/write-usb.sh --keep-data`，目標是 USB 隨身碟，不是內建 NVMe。

Build 產生一組新的 128-bit nonce：32 個小寫十六進位再加一個 LF，共 33 bytes。同一份放進映像的 `/vendor/etc/kano-autooff.nonce`（mode 0444）以及 `<out>/image/autooff.nonce`（給這次 session 讀）。沒有這個開關時，這兩個檔案和服務都不存在。

開機腳本把兩個檔案當原始位元組讀。只有兩邊都是一般檔（不是符號連結）、都是那 33 bytes、而且逐 byte 相同，才開始等。等滿 N 秒（`sleep` 必須成功，而且 `/proc/uptime` 至少前進 N 秒）之後、斷電前再讀一次、再比一次，通過才執行一次 `reboot -p`。`/proc/uptime` 是 `CLOCK_BOOTTIME`（含 s2idle／S0ix 的時間）；`sleep` 用的 `CLOCK_MONOTONIC` 在休眠時暫停。正常睡滿 N 秒時，boottime 的增量不會小於 monotonic，閘門仍會過。缺檔、讀取失敗、長度不對、NUL、CR、多出來的位元組、大寫、兩邊不同，都在 kmsg 記 staying on 並以 exit 0 結束。符號連結即使指到正確內容也保持開機：`/data` 上的連結可以指回 vendor 檔或指到別顆碟。硬連結跨不過唯讀的 vendor erofs 與 userdata 的 f2fs，所以不能用連結把 vendor nonce 掛到 `/data`。

`/data/kano-autooff.nonce` 不在映像裡。和 `/data/kano-wifi.conf` 一樣，由這次 TEST 開機前的 session（維護系統）寫到**已核對身份的 USB 隨身碟** userdata（`install/write-usb.sh` 的目標是 `/dev/disk/by-id/usb-...`），而且必須是一般檔：內容是該次 build 的 `<out>/image/autooff.nonce` 原始 33 bytes。用完立刻刪掉。不要寫到內建 NVMe 的 userdata。不要把 TEST 映像裝成內建系統。若這些檢查不在，留下檔案再跑 harness 安裝會讓內建碟同時得到服務與 nonce，之後每次開機都會斷電；`kano-nvme-update.sh` 只推 super／vbmeta，不會清掉那個檔。目前 `kano-nvme-install.sh` 用 vendor 掃描當縱深防禦，在 `blkdiscard` 前確認 `sda1` 是 userdata 再以 `-o ro` 掛上並拒絕已有的 nonce，並在卸載複製過來的 userdata 之前清掉 `kano-autooff.nonce`（一般檔、符號連結或目錄；目錄由 python3 的 `shutil.rmtree` 刪除，不把檔名交回 shell）。卸載後再唯讀掛上複查，名字必須是 ENOENT，然後再卸載。那次清除或複查失敗（含查找錯誤（errno 不是 ENOENT），或確認時成功查到任何物件）就停止，不再 `resize.f2fs`、`fsck.f2fs`，也不再寫 ESP。`dd` 完成之後、清除成功之前的任何停止（行程被殺掉、斷電、ssh 中斷）都可能留下檔案。這一步才是這條複製路的安全保證。QEMU 沒有這份 `/data` 檔，自我測試會 staying on。
