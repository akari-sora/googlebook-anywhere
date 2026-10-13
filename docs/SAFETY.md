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
   這是預設映像。開發者選項 `GBOS_EC`（預設關閉）會放行一組經過稽核的 EC 驅動，見下面「EC 功能變體」。
   原映像的 `spi-intel.writeable=1` 也拿掉了。清單在機型設定（`profiles/kano/profile.toml`）；全專案一定要封鎖的模組與參數在
   `profiles/_safety.toml`（`blacklist.required` 就是上面這整份 57 個模組，每個機型都一樣），機型設定只能加、不能減（`builder/lib/devprofile.py` 拒絕），`builder/image.py` 再對每個開機選項逐一檢查，並確認 ESP 上只有 quiet 與 serial-debug 兩個選項
   （base ESP 原本那個沒有封鎖的 `lapis.conf` 會被刪掉）。
3. **`efi=noruntime`**：Linux 不能呼叫 EFI runtime services，所以 OS 寫不了 EFI 變數，也送不出 capsule 更新。
4. **裝置節點**：實機上沒有 `/dev/mem`、`/dev/port`、`/dev/mtd*`、`/dev/spidev*`。
5. **USB 映像的 `initcall_blacklist=nvme_init`**：從 USB 開機時看不到內建碟（裝到內建碟的版本才放行 NVMe）。
6. **開機選單編輯器預設關閉（`editor no`）**：在 systemd-boot 選單上無法臨時刪掉上面的封鎖參數。開發用映像可用
   `GBOS_LOADER_EDITOR=yes` 打開。
7. **開發用看守程式**（`tools/harness/kano-gbos-watch*.py`）：序列埠出現 NVMe、SPI、EC、TPM、gsmi、fwupd、rollforward
   等字串就透過 EC 斷電。它只是補充偵測：只在 serial-debug 開機選項下有序列埠輸出可看，看不到紀錄時會回報「無法觀測」，
   不保證在任何情況下都能及時攔下。它拿 KANO 機型設定的整份 `module_blacklist` 核對這次開機的參數，不知道 `GBOS_EC`：
   EC 變體開機時，被放行的 `cros_ec*` 會被當成「少了防護參數」而直接斷電（fail-closed）。用 EC 變體時不要開看守程式。

## EC 功能變體（`GBOS_EC`，預設關閉）

`GBOS_EC=sensors,typec,buttons`（可以只選其中幾項；只稽核過 KANO，其他機型的設定會被拒絕）是給想要自動旋轉、USB-C 的 USB3／DisplayPort、側邊音量鍵的人用的。
它會改寫上面第 2 層的保證：從「沒有任何 EC 裝置被註冊，送不出 EC 指令」，變成
「**只有下面這組經過稽核的核心驅動會送 EC 指令，其中沒有任何指令會寫 EC 或周邊的 flash、CBI、I²C，或讓 EC 重開**」。
這組包括放行的模組，以及每個選項本來就放行、在變體下也會走 EC 的 `cros_kbd_led_backlight`（見稽核表）。
它們送出的寫入類指令只改 EC 的 RAM 狀態（EC 重開就恢復）。

**放行的模組**（其餘全部維持封鎖）：

| 功能 | 模組 | 作用 |
|---|---|---|
| 共同 | `cros_ec`、`cros_ec_lpcs`、`cros_ec_dev` | EC 核心、LPC/eSPI 傳輸（ACPI GOOG0004）、EC 裝置 |
| `sensors` | `cros_ec_sensorhub`、`cros_ec_sensors_core`、`cros_ec_sensors`、`cros_ec_lid_angle` | 上蓋與底座的加速度計、陀螺儀、開合角度（IIO） |
| `typec` | `cros_ec_typec`、`cros_usbpd_notify` | USB-C 的模式切換（USB3、DP alt mode），mux 由已載入的 `intel_pmc_mux` 設定 |
| `buttons` | `cros_ec_keyb` | `cros_ec_buttons`：音量鍵、上蓋與平板模式開關 |

**永遠封鎖**（`builder/image.py` 的 `EC_NEVER`，是 `profiles/_safety.toml` `blacklist.required` 的一部分；每個機型、每個變體的每個開機選項都逐一檢查）：`cros_ec_chardev`（`/dev/cros_ec`）、`cros_ec_debugfs`、
`cros_ec_sysfs`（`reboot`、`kb_wake_angle`）、`cros_ec_vbc`、`cros_ec_lightbar`、`i2c_cros_ec_tunnel`（經 EC 的 I²C）、
`cros_charge_control`、`leds_cros_ec`、`cros_usbpd_charger`（可寫輸入電流上限）、`cros_usbpd_logger`、`cros_peripheral_charger`、
`cros_typec_switch`、`cros_ec_ucsi`、`cros_ec_spi`、`cros_ec_i2c`、`cros_ec_ishtp`、`cros_ec_usb`，以及 TPM、SPI flash（`spi_intel`、`spi_intel_pci`、`mtd`、`spi_nor`、`spidev`）、`thunderbolt`
（USB4 retimer 的 NVM 更新路徑）、gsmi、nvram、`chromeos_acpi`。變體另外封鎖這份 lapis 沒有、但將來的 vendor_dlkm 可能加進來的
`gpio_cros_ec`、`cros_ec_hwmon`、`cros_ec_wdt`、`rtc_cros_ec`、`cros_ec_uart`、`cros_ec_rpmsg`。
`cros_ec_dev` 仍然會建立 chardev、sysfs、debugfs 等子裝置，但對應的驅動被封鎖，所以 **`/dev/cros_ec`、`/dev/cros_fp`、
`/dev/cros_ish` 永遠不存在**。vendor 裡會寫 CBI、改防寫、做 I²C passthru、切電池的 `ec-executor` 等程式只走這些 chardev，
所以仍然一個指令都送不出去。

**稽核：放行的驅動會送的 EC 指令**（來源：kernel/common `3938c8d97142`，即 lapis 核心的 commit；檔名:行號）

| 模組 | 指令 | 性質 |
|---|---|---|
| `cros_ec` | `HOST_SLEEP_EVENT`（`cros_ec.c:183`，休眠／喚醒時）、`RWSIG_ACTION(CONTINUE)`（`cros_ec_proto.c:306`，註冊時一次） | 休眠通知，見下面「休眠」；RWSIG 在 brya 的 EFS2 EC 上回 `INVALID_COMMAND` |
| `cros_ec_proto`（已放行的函式庫） | `HELLO`、`GET_PROTOCOL_INFO`、`GET_CMD_VERSIONS`、`GET_FEATURES`、`GET_NEXT_EVENT`、`HOST_EVENT_GET_WAKE_MASK`、`MKBP_STATE`、`READ_MEMMAP`、`GET_COMMS_STATUS`、`MOTION_SENSE_CMD(DUMP)` | 只讀 |
| `cros_ec_lpcs` | 只做傳輸；收到 EC panic 通知時關機（`cros_ec_lpc.c:458`） | — |
| `cros_ec_dev` | `PCHG_COUNT`（`cros_ec_dev.c:317`） | 只讀 |
| `cros_ec_sensorhub` | `MOTIONSENSE`：`INFO`、`FIFO_INFO`、`FIFO_READ`、`FIFO_INT_ENABLE`（`cros_ec_sensorhub_ring.c:126`） | 讀，或改 EC RAM 的 FIFO 中斷設定 |
| `cros_ec_sensors_core`、`cros_ec_sensors`、`cros_ec_lid_angle` | `MOTIONSENSE`：`DATA`、`EC_RATE`、`SENSOR_ODR`、`SENSOR_RANGE`、`SENSOR_OFFSET`、`SENSOR_SCALE`、`PERFORM_CALIB`、`FIFO_FLUSH`、`LID_ANGLE` | 改取樣率、量程、校正值，存在 EC RAM，不碰 flash |
| `cros_ec_typec` | `TYPEC_STATUS`、`TYPEC_DISCOVERY`、`USB_PD_PORTS`、`USB_PD_MUX_INFO`、`USB_PD_MUX_ACK`、`TYPEC_CONTROL`（`ENTER_MODE`、`EXIT_MODES`、`CLEAR_EVENTS`、`SEND_VDM_REQ`）、`USB_PD_CONTROL`（使用者寫 typec 的 `data_role`／`power_role` 時做角色交換）、`TYPEC_VDM_RESPONSE`（`cros_typec_vdm.c:32,70`，讀對接裝置回的 VDM） | PD 的執行期動作：進出 DP 模式、角色交換、送 DP 的 VDM 給對接的裝置；沒有韌體更新 |
| `cros_usbpd_notify` | `PD_HOST_EVENT_STATUS`（`cros_usbpd_notify.c:77`） | 只讀 |
| `cros_ec_keyb` | `MKBP_INFO`（`cros_ec_keyb.c:368`） | 只讀 |
| `cros_kbd_led_backlight`（每個選項都放行，給鍵盤背光用） | `PWM_GET_KEYBOARD_BACKLIGHT`、`PWM_SET_KEYBOARD_BACKLIGHT`（`cros_kbd_led_backlight.c:146,164`）。只在變體下：EC 回報 `EC_FEATURE_PWM_KEYB` 時，`cros_ec_dev` 會建立 `cros-keyboard-leds` 子裝置（`cros_ec_dev.c:106,150-152`），這個驅動也綁上它 | 讀、改 EC RAM 裡的背光 PWM，不碰 flash。這個子裝置沒辦法單獨封鎖；預設映像沒有 EC 裝置，背光只走 ACPI GOOG0002。同一個 LED 名稱會註冊兩次（第二個自動改名），idc 用的是 GOOG0002 那條路徑；變體下沒有在實機驗證過 |

沒有任何放行的驅動會送 `FLASH_*`、`REBOOT_EC`、`I2C_PASSTHRU`、`SET_CROS_BOARD_INFO`、`VBNV_CONTEXT`、`BATTERY_CUT_OFF`、
`CHARGE_CONTROL`、`USB_PD_FW_UPDATE`、`EXTERNAL_POWER_LIMIT` 或 `PWM_SET_FAN_DUTY`。vendor_dlkm 的這些 `.ko` 是另一棵樹編的
（scmversion `ga3bbc4527b7b`，不公開），稽核依據的是同一個核心版本的公開原始碼；這些模組的字串裡沒有原始碼以外的 EC 功能
（`cros-ec-proto.ko` 裡的整張指令名稱表是 tracepoint 用的）。換 vendor_dlkm 或 lapis 版本時要重新稽核。

**休眠（最重要的使用風險）**：載入 `cros_ec_lpcs` 後，核心每次休眠都會告訴 EC「進入 S0ix」。EC 等 10 秒；如果 SoC 沒有真的進入
S0ix（SLP_S0# 沒拉起），EC 會把 AP 叫醒；AP 醒來後 10 秒內沒有回報「已喚醒」，EC 就會強制重設 AP（`power/host_sleep.c`）。
AP 正常醒來時只會多一次喚醒，核心記一行 `EC detected sleep transition timeout`；只有 AP 卡住時才會被重設。
所以 **這個變體要在「目前的映像量到 S0ix」之後才使用**，並且先從隨身碟測試，不要直接裝到內建碟。核心沒有參數可以關掉這個偵測
（`suspend_timeout_ms` 只在被封鎖的 `cros_ec_debugfs` 裡）。

**沒有在 build 時驗證的部分**：`sensors` 為了自動旋轉，就地改了 vendor 的 framework RRO（`config_supportAutoRotation`）。
PackageManager 是否仍啟用這個 overlay，build 與 QEMU 都沒有檢查；在實機上用 `cmd overlay list` 確認。如果它被停用，同一個
overlay 裡的自動亮度、power_profile 等設定也會一起消失。

**其他行為改變**：EC panic 時 AP 會正常關機（ChromeOS 的行為）；USB-C 的角色交換只在使用者或 USB HAL 寫 typec sysfs 時發生；
電源鍵維持 ACPI Power Button，`cros_ec_buttons` 的電源鍵沒有對應（`usr/keylayout/cros_ec_buttons.kl`），所以不會按一下觸發兩次。
`thunderbolt` 仍封鎖，所以 TBT／USB4 的 PCIe 通道與 TBT 底座不能用。

## 沒有涵蓋的範圍

- **韌體與開機程式自己的 EFI 變數寫入**：`efi=noruntime` 只約束 Linux。UEFI 韌體本身會更新 `Boot####`、`BootOrder` 等變數；
  systemd-boot 選單上的按鍵 `d`／`D`（預設項目）、`+`／`-`／`t`／`T`（等待時間）會寫入 NV 變數，`f`、`F2`、`F10`、`Del`、`Esc`
  會重開進韌體設定（寫入 `OsIndications`）。loader.conf 設了 `auto-firmware no`，選單裡不會出現進韌體的項目，但按鍵本身仍有效。在 MrChromebox UEFI 上，這些變數存在
  AP SPI flash 的 SMMSTORE 區。不要在選單上按這些鍵。ESP 裡沒有 `loader/random-seed`，所以 systemd-boot 不會寫
  `LoaderSystemToken`。
- **i2c-hid 裝置的韌體**：觸控板、觸控螢幕的韌體理論上可以由 root 透過 hidraw 更新。vendor 裡唯一的更新程式（fwupd 與
  Elan 的 cab）已移除，但核心層沒有封鎖 hidraw。
- **指紋 MCU（FPMCU）的燒錄路徑**：vendor 的 `com.android.hardware.fingerprint.crosfp` APEX 沒有移除（指紋功能要用它），
  裡面的指紋服務有 FPMCU 韌體的更新路徑。這條路徑碰不到，**只**因為 `/dev/cros_fp` 永遠不會存在：建立它需要 FPMCU 的 SPI
  傳輸 `cros_ec_spi` 與 `cros_ec_chardev`，兩者在每個開機選項（包括每個 EC 變體）都封鎖（`EC_NEVER`，也在
  `blacklist.required` 裡）。這是已知的殘留風險：開發用映像打開選單編輯器（`GBOS_LOADER_EDITOR=yes`）後，可以在選單上臨時
  拿掉封鎖；將來要放行 FPMCU 的傳輸，必須先處理這個 APEX 的更新程式。
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
- 不要放行 TPM、EC chardev、SPI 驅動，即使某個功能看起來需要。`GBOS_EC` 只放行上面稽核過的驅動，要多放行任何 EC 模組都必須先重新稽核。
- 不要在 systemd-boot 選單按 `d`、`D`、`+`、`-`、`t`、`T`、`f`、`F2`、`F10`、`Del`、`Esc`；用方向鍵和 Enter 選項目就好。
