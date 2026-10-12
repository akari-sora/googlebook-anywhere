# 安全模型

Googlebook（lapis）映像本來是給另一台機器用的，裡面帶著那台機器的韌體與更新程式。KANO 的韌體一旦被寫壞就會變磚，
所以第一原則是：**從 Android 這一側，任何東西都不能寫入 AP SPI flash、EC、Cr50、觸控／PD／retimer 韌體或 EFI 變數。**
啟用任何硬體之前，先證明沒有更新程式碰得到它。下面寫清楚每一層擋住什麼、沒擋住什麼。

## 多層防護

1. **移除 vendor 分割區裡已知的更新程式與燒錄工具**：fwupd（含 LVFS 設定與離線 cab）、flashrom、libflashrom、futility、
   futility-executor、ectool、crossystem、`firmware/ap-ec-fw.zip`。build 時刪除，刪除清單（`removed.txt`）逐項比對。
   範圍只有 vendor：system、system_ext、product 與其他 APEX 保持 Google 原樣，沒有逐一掃描。真正的兜底是下一層的核心封鎖。
2. **核心模組封鎖（`module_blacklist=`）**：TPM（tpm、tpm_tis*、tpm_tis_i2c_cr50）、EC 傳輸與核心（cros_ec、cros_ec_lpcs、
   cros_ec_dev、cros_ec_chardev、cros_ec_spi/i2c/ishtp、cros_ec_sysfs、cros_ec_vbc、cros_ec_debugfs …，共 30 多個）、
   SPI flash（spi_intel、spi_intel_pci、mtd、spi_nor、spidev）、gsmi、chromeos_acpi、nvram、i2c_dev、at24、thunderbolt。
   只放行 `cros_ec_proto`（純函式庫）給 ACPI 鍵盤背光驅動用；沒有任何 EC 裝置會被註冊，所以送不出 EC 指令。
   原映像的 `spi-intel.writeable=1` 也拿掉了。`builder/image.py` 對三個開機選項逐一檢查這些條件。
3. **`efi=noruntime`**：Linux 不能呼叫 EFI runtime services，所以 OS 寫不了 EFI 變數，也送不出 capsule 更新。
4. **裝置節點**：實機上沒有 `/dev/mem`、`/dev/port`、`/dev/mtd*`、`/dev/spidev*`。
5. **USB 映像的 `initcall_blacklist=nvme_init`**：從 USB 開機時看不到內建碟（裝到內建碟的版本才放行 NVMe）。
6. **開機選單編輯器預設關閉（`editor no`）**：在 systemd-boot 選單上無法臨時刪掉上面的封鎖參數。開發用映像可用
   `GBOS_LOADER_EDITOR=yes` 打開。
7. **開發用看守程式**（`tools/harness/kano-gbos-watch*.py`）：序列埠出現 NVMe、SPI、EC、TPM、gsmi、fwupd、rollforward
   等字串就透過 EC 斷電。它只是補充偵測：只在 serial-debug 開機選項下有序列埠輸出可看，看不到紀錄時會回報「無法觀測」，
   不保證在任何情況下都能及時攔下。

## 沒有涵蓋的範圍

- **韌體與開機程式自己的 EFI 變數寫入**：`efi=noruntime` 只約束 Linux。UEFI 韌體本身會更新 `Boot####`、`BootOrder` 等變數；
  systemd-boot 選單上的按鍵 `d`／`D`（預設項目）、`+`／`-`／`t`／`T`（等待時間）會寫入 NV 變數，`f`、`F2`、`F10`、`Del`、`Esc`
  會重開進韌體設定（寫入 `OsIndications`）。loader.conf 設了 `auto-firmware no`，選單裡不會出現進韌體的項目，但按鍵本身仍有效。在 MrChromebox UEFI 上，這些變數存在
  AP SPI flash 的 SMMSTORE 區。不要在選單上按這些鍵。ESP 裡沒有 `loader/random-seed`，所以 systemd-boot 不會寫
  `LoaderSystemToken`。
- **i2c-hid 裝置的韌體**：觸控板、觸控螢幕的韌體理論上可以由 root 透過 hidraw 更新。vendor 裡唯一的更新程式（fwupd 與
  Elan 的 cab）已移除，但核心層沒有封鎖 hidraw。
- **system 分割區的 `gscutil.rc`**：原映像開機時會跑 `perform-os-startup`（GSC／Cr50 韌體更新）。system 分割區維持原樣，
  這條路徑只靠 TPM 模組（`tpm`、`tpm_tis*`、`tpm_tis_i2c_cr50`）被封鎖而失敗；放行 TPM 之前必須先處理它。
- **system 分割區的 OS 更新**（update_engine）維持原樣。它更新的是 Android 分割區，不是韌體；套用 Google 的 OTA 會蓋掉這裡的修改，結果未測試。

## 資料安全取捨（日常使用前要知道）

為了在 KANO 上開得起來，這個映像放寬了幾項 Android 的資料保護，每一項都在每次 build 中固定包含：

- **`/data` 沒有加密**：拿掉了 FBE 與 metadata encryption（沒有 pKVM／Trusty，金鑰沒有安全的存放處）。Google 帳號的權杖與
  使用者資料以明文存在碟上，**筆電遺失就等於資料外洩**。
- **`ro.debuggable`**：builder 會在 vendor 的 build.prop 加上 `ro.debuggable=1`（原本給序列埠 shell 用），但這一行**沒有生效**：
  2026-10-13 在 KANO 實機（開發者自己的 build）讀到 `ro.debuggable=0`、`ro.force.debuggable=0`、`ro.build.type=user`。
  原因推測是 vendor 的 build.prop 不能設定 system 擁有的屬性（`/vendor/build.prop` 從 adb shell 讀不到，沒有直接確認）。
  所以 app 不能被任意除錯，adbd 也是一般 user build 的權限，`adb root` 不能用。哪天這一行生效了，所有 app 都會變成可除錯，要重新評估。
- **以 root 執行的 permissive 服務**：`kano_bootfix`（設定 `persist.vold.dont_commit_checkpoint=1`）、`kano_kmsg_logcat`
  （只在 serial-debug 選項把 logcat 鏡像到序列埠）、`kano_diag`（定期把負載與 dmesg／logcat 片段寫到 `/data/kano-diag`）、
  `kano_wifi`（只有 `/data/kano-wifi.conf` 存在時才動作，該檔只由開發用維護系統建立，內容是明文 Wi-Fi 密碼）。
- **網路 adb 預設不包含**：只有 build 時給 `GBOS_ADB_PUBKEY` 才會開 TCP 5555，而且只接受那一把公鑰（`ro.adb.secure` 維持 1）。
  builder 會拒絕不像 adb 公鑰的檔案，例如誤傳的私鑰。2026-10-13 在 KANO 實機上的開發者 build（開了 adb）確認過：
  `ro.adb.secure=1`、`service.adb.listen_addrs=tcp:5555`、`/adb_keys` 指向 `/product/etc/security/adb_keys`；
  一把新產生、沒登記的金鑰經 TCP 5555 連線被拒（`unauthorized`，shell 不能用，螢幕上也沒有跳出授權對話框），
  登記的那把金鑰拿到的是 `uid=2000(shell)`。

## 實際攔下過的寫入嘗試

- 設定精靈的 `ActivationDateWorker` 會讀寫 RW VPD（AP flash 上的啟用日期），經由 `vpd-executor` 執行 flashrom。
  因為 flashrom 已被移除、也沒有 `/dev/mem`，紀錄是 `Failed to execute flashrom`，什麼都沒寫。
- `adbd-killer` 用 `futility gbb --get --flash` 讀 GBB 旗標；futility 已移除。

## 不能做的事

- 不要從 Googlebook 或任何非官方工具更新 KANO 的韌體。量產機要更新 UEFI 韌體時，只用 MrChromebox 的官方腳本、照官方文件做
  （見 FIRMWARE.md；這個專案沒有測試過韌體更新）。**工程樣機**（CPU stepping 小於 3 或 HWID 含 TEST）連 MrChromebox 的腳本
  都不要跑：公開韌體沒有它的 microcode，開發這個專案的那台的韌體帶有手動補的 K0 microcode。
- 不要放行 TPM、EC chardev、SPI 驅動，即使某個功能看起來需要。
- 不要在 systemd-boot 選單按 `d`、`D`、`+`、`-`、`t`、`T`、`f`、`F2`、`F10`、`Del`、`Esc`；用方向鍵和 Enter 選項目就好。
