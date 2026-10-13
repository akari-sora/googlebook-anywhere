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
   - 功能測試：直接把 kernel、initrd 與 serial-debug 的參數交給 QEMU，從序列埠紀錄檢查 Android 開到 system_server、/data 是 f2fs、
     沒有載入 EC 模組、fwupd 沒啟動等二十多項；
   - 開機鏈測試：不給 kernel，讓韌體從映像自己的 ESP 開 systemd-boot，跑預設選項。quiet 選項沒有序列埠輸出，所以改看磁碟：
     QEMU 在時限結束時仍在執行、整顆隨身碟累計寫入 16 MiB 以上，而且最高的寫入位置落在 userdata 內（只有 Android 掛上 /data 才會寫到那裡）。
   全部通過才把 `<name>.partial` 改名成正式檔名。

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
- **兩個開機選項**：quiet（預設，不輸出序列埠）與 serial-debug（開發用，序列埠 + logcat 鏡像），只差序列埠與 log level 參數
  （`image.py` 會檢查）。base ESP 原本的 `lapis.conf`（Google 的參數，沒有 KANO 的封鎖）會被刪掉，`image.py` 也確認 ESP 上
  只剩這兩個選項。早期版本另有一個沒有 `pci-stub.ids` 的 plain 選項，在 KANO 上面板一直是暗的，已不再產生。

## 儲存與首次開機

- **/data 用 f2fs、拿掉 FBE 與 `checkpoint=block`**：慢速 USB 碟上 ext4 的 fsync 會拖過 watchdog（safe7、safe10）。
- **`ro.cp_system_other_odex=0`**：首次開機複製 odex 太久（safe8）。
- **`persist.vold.dont_commit_checkpoint=1`**（`kano_bootfix` 服務）：vold 的 checkpoint commit 會呼叫 boot HAL 的
  markBootSuccessful → crossystem（韌體 NVRAM，已封鎖）而失敗重開（safe12/13）。
- **移除 IWeaver、ISecretkeeper 的 VINTF 宣告**：它們需要 protected VM，LockSettings 會一直等（safe11）。
- **停掉 timberslide、gscd、udsattestation**：需要 EC／Cr50，只會崩潰循環（safe21/23）。
- **system_server watchdog 5 分鐘**（`Settings.Global.system_server_watchdog_timeout_ms`）。

## 硬體

- **Wi-Fi／藍牙／音效韌體**：AX211（`iwlwifi-so-a0-gf-a0-89`、pnvm、`ibt-0040-0041`）、SOF `sof-adl.ri` 與
  `sof-adl-max98373-nau8825.tplg`，加上 ADL-P 的 GPU 韌體（DMC、GuC、HuC），全部只載入 RAM、不燒錄；載入 `iwlmvm`。
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
