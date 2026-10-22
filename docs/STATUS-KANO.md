# KANO 硬體狀態（Acer Chromebook Spin 714，CP714-1WN／CP714-2WN，brya／kano）

2026-10-22 軟體驗收更新：v0.2.3 的 CI、韌體檢查與突變測試結果見 [軟體驗收紀錄](VERIFICATION-v0.2.3.md)。本輪沒有新增 KANO 實機開機或硬體測試；下方仍保留原實機紀錄的日期與範圍。

驗證日期 2026-10-12。**目前唯一在實機上驗證過的是一台 Alder Lake-P K0 工程樣機**（CPU Core i7-1260P，CPUID `06-9a-02`，iGPU PCI ID `8086:46a6`，ChromeOS CBI SKU `131073`；`131073` 是 CBI SKU，不是 CPU 型號），Googlebook 裝在內建 NVMe（SAMSUNG MZVLQ128HCHQ）。
下方功能表中的每一項都是在該台實機上量到的結果，不是推論；唯一的例外寫明在該列（休眠／喚醒的鍵盤喚醒問題，目前是依核心原始碼推得）。驗證方式見最右欄；用到的工具在 `tools/harness/`。

## KANO 主機板家族型號表（CP714-1WN 與 CP714-2WN）

Acer Chromebook Spin 714 的 12 代（`CP714-1WN`）與 13 代（`CP714-2WN`，ChromeOS 行銷代號 `Zydron`）共用同一張 `KANO` 主機板與 `Google_Kano.*` 韌體（見 coreboot `src/mainboard/google/brya/variants/kano/Kconfig.name` 與 MrChromebox 支援表）。v0.2.0 以前的 `profiles/kano/profile.toml` 只寫了驗證機的單一內顯 ID `xe.force_probe=46a6` 與 ADL SOF 韌體，導致其他 CPU 型號開機沒有畫面、13 代型號沒有聲音。

目前 `profiles/kano/profile.toml` 的 `xe.force_probe` 列 12 個內顯 PCI ID（12 代 ADL-P/U 與 13 代 RPL-U，含單通道記憶體變體），`46a6` 在第一個，不使用萬用字元 `*`。7 個對到零售 KANO SKU；5 個（`46a3`、`46b2`、`a721`、`a7ac`、`a7ad`）是平台候選（ADL-P/RPL-U 巨集成員），KANO 對照未確認，見 docs/COMPATIBILITY.md。零售對照寫在本頁與 docs/COMPATIBILITY.md（Acer 零售型號與 coreboot 的 KANO 清單）。這 12 個都在核心 `adl_p_desc` 裡（`drivers/gpu/drm/xe/xe_pci.c:366-369` 把 `INTEL_ADLP_IDS`、`INTEL_RPLU_IDS`、`INTEL_RPLP_IDS` 綁上去；`#define` 從 `include/drm/intel/pciids.h:669`、`:709`、`:717` 開始）。沒有 SKU 列的 ADL-P ID、ADL-N、RPL-S，以及 RPL-P 的 `a720`／`a7a0`／`a7a8`／`a7aa`／`a7ab`，都不在名單裡。13 代另補 `intel/sof/community/sof-rpl.ri` 與 `intel/sof-tplg/sof-rpl-max98373-nau8825.tplg`。

有零售型號的 CPU 與「coreboot 有列、零售對應未確認」的候選分開寫。iGPU 欄只寫該列自己的 ID。

| 機型／子型號（SKU） | CPU（微架構／CPUID） | 內顯 PCI ID（`8086:xxxx`） | 音效韌體與拓樸 | 預期狀態 |
|---|---|---|---|---|
| **CP714-1WN（開發驗證機）** | Alder Lake-P K0 工程樣機，Core i7-1260P（`06-9a-02`）；CBI SKU `131073` | `46a6` | `sof-adl.ri` + `sof-adl-max98373-nau8825.tplg` | **已實機驗證（verified）**（注意：工程樣機不可刷公開韌體） |
| **CP714-1WN-34BQ / 38M1 / 35M7 / 37H0**（12 代 i3 零售） | Core i3-1215U（Alder Lake-U，`06-9a-04`） | `46b3`（平台候選 `46a3`、`46b2`，KANO 對照未確認） | `sof-adl.ri` + `sof-adl-max98373-nau8825.tplg` | **應可用，尚未實機測試 (should work, untested)** |
| **CP714-1WN-53M9 / 574L / 52HT / 552D**（12 代 i5 零售） | Core i5-1235U（Alder Lake-U，`06-9a-04`） | `46a8`（單通道 `4628`） | `sof-adl.ri` + `sof-adl-max98373-nau8825.tplg` | **應可用，尚未實機測試 (should work, untested)** |
| **CP714-1WN-76B7 / 73F4 / 763T / 74F8 / 7988**（12 代 i7 零售；`7988` 為 2560×1600 WQXGA） | Core i7-1260P（Alder Lake-P，`06-9a-03`） | `46a6`（單通道 `4626`） | `sof-adl.ri` + `sof-adl-max98373-nau8825.tplg` | **應可用，尚未實機測試 (should work, untested)** |
| **coreboot 有列、零售出貨未確認**（無公開 MPN 對到這些 CPU） | Core i5-1240P、Core i7-1280P（Alder Lake-P）；Core i7-1255U（Alder Lake-U） | i5-1240P／i7-1280P：`46a6`（單通道 `4626`）；i7-1255U：`46a8`（單通道 `4628`） | `sof-adl.ri` + `sof-adl-max98373-nau8825.tplg` | **應可用，尚未實機測試 (should work, untested)** |
| **CP714-2W-356B / CP714-2WN-320J / 3184**（13 代 i3 零售，Zydron / KANO） | Core i3-1315U（Raptor Lake-U，`06-ba`） | `a7a9`（平台候選 `a7ac`、`a7ad`，KANO 對照未確認；`INTEL_RPLU_IDS`） | `sof-rpl.ri` + `sof-rpl-max98373-nau8825.tplg` | **應可用，尚未實機測試 (should work, untested)** |
| **CP714-2W-56B2 / CP714-2WN-5596 / 5806 / 56B6**（13 代 i5 零售，Zydron / KANO） | Core i5-1335U / i5-1345U（Raptor Lake-U，`06-ba`） | `a7a1`（平台候選 `a721`，KANO 對照未確認；`INTEL_RPLU_IDS`，不是 `INTEL_RPLP_IDS`） | `sof-rpl.ri` + `sof-rpl-max98373-nau8825.tplg` | **應可用，尚未實機測試 (should work, untested)** |
| **CP714-2W-732F / CP714-2WN-71M6 / 77X9**（13 代 i7 零售，Zydron / KANO） | Core i7-1355U / i7-1365U（Raptor Lake-U，`06-ba`） | `a7a1`（平台候選 `a721`，KANO 對照未確認；`INTEL_RPLU_IDS`） | `sof-rpl.ri` + `sof-rpl-max98373-nau8825.tplg` | **應可用，尚未實機測試 (should work, untested)** |

### 各未測 SKU 首次開機建議檢查項目

1. **內顯驅動與螢幕點亮（所有非 `46a6` 的 12 代與 13 代型號，以及 `CP714-1WN-7988` 的 2560×1600 WQXGA 面板）**：確認 `xe` 驅動依 `xe.force_probe=` 綁定 `00:02.0` 內顯、eDP 內建螢幕正常顯示桌面且無破圖或黑畫面（`xe.enable_dpt=0`）、背光亮度可調；`CP714-1WN-7988` 另確認 2560×1600 解析度與 UI 縮放正常。
2. **13 代（CP714-2WN / Zydron）SOF 音效**：13 代的 PCH cAVS（PCI ID `8086:51ca` / `51cb`）在核心裡匹配 `rpl_desc`（`sound/soc/sof/intel/pci-tgl.c:252-283,295-296`）與 `rpl_nau8825_def`（`sound/soc/intel/common/soc-acpi-intel-rpl-match.c:461-467`、`sound/soc/sof/intel/hda.c:1661-1714`、`sound/soc/intel/boards/sof_nau8825.c:160-171,299-314`），開機時請求 `intel/sof/community/sof-rpl.ri` 與 `intel/sof-tplg/sof-rpl-max98373-nau8825.tplg`。`sof-rpl.ri` 在固定的 sof-bin v2025.12.2 裡是指向 `sof-tgl.ri` 的 symlink；profile 釘的是實體檔 `sof-bin-2025.12.2/sof/community/sof-tgl.ri`（SHA-256 `e3489dcb277e87eee8bc091ab7d51163a63c85541c22522775122b54263769f3`）。sof-bin 的 CalVer 不是 SOF 原始碼 tag：`thesofproject/sof` 沒有 `v2025.12` 或 `v2025.12.2`。該發布配方裡的 IPC3 拓樸元件是 `sof-tplg-v2.2.13`。在 SOF tag [v2.2.13](https://github.com/thesofproject/sof/blob/v2.2.13/tools/topology/topology1/platform/intel/rpl.m4) 的 `tools/topology/topology1/platform/intel/rpl.m4` 讀到的是註解 “no difference yet, use Alder Lake definitions” 加上 `include(\`platform/intel/adl.m4')`。核心 `rpl_nau8825_def` 與 `adl_nau8825_def` 的 SSP／HDMI／BT offload 旗標相同。sof-bin v2025.12.2 沒有 `sof-rpl-max98373-nau8825.tplg`，所以映像把 ADL 那份（SHA-256 `4beb29400d9beed0c80bcc11b3f4e82aad70a54d6e851638c81eb087a10d4de5`）裝成 RPL 檔名。ChromiumOS `sof-topology` ebuild 是把拓樸包裡的 `*.tplg` 裝進 `/lib/firmware/intel/sof-tplg`（`doins ./*.tplg`），那一步本身沒有比對 ADL 與 RPL 的位元組。equivalent per SOF/ChromiumOS sources, untested on 13th-gen hardware。首次在 CP714-2WN 開機時請檢查：雙顆 MAX98373 喇叭播放、內建 DMIC 收音、NAU8825 耳機孔音訊與插拔偵測。
3. **二供觸控板（`ELAN0000`）、觸控螢幕、Wi-Fi／藍牙與二供鏡頭（`Hi-556`）**：KANO 主機板的 LPSS I²C 與 AX211 CNVi 設計相同，但部分量產機採用二供觸控板 `ELAN0000`（I²C `0x15`，由 `vendor_dlkm` 的 `elan_i2c.ko` 驅動，alias `acpi*:ELAN0000:*`；驗證機的 `SYNA0000` `0x2c` 則走 `i2c_hid_acpi.ko`）。`CONFIG_I2C_HID_ACPI` 與 `CONFIG_MOUSE_ELAN_I2C` 在 lapis vmlinuz 都沒有編成內建，兩者都是 vendor_dlkm 模組。請確認觸控板游標、ELAN90FC 觸控螢幕與 Wi-Fi／藍牙連線；此外 `CP714-2WN` 部分批次搭載二供 MIPI 鏡頭 `Hi-556`（`INT3537:00`），開發者若開啟實驗性 `camera.libcamera` 需留意目前僅內建 `ov2740.yaml`。
4. **刷機前的 `kano-check.sh`（13 代 CP714-2WN）**：`install/kano-check.sh` 只對 12 代 Alder Lake-P 量產 stepping（`06-9a-03`／`06-9a-04`）印出 `RESULT: GO`；在 13 代 Raptor Lake（`06-ba`）上會回報 `RESULT: UNKNOWN`，請自行對照 MrChromebox 官方文件與支援表。

## 驗證機（`46a6` 工程樣機）實測紀錄

表中的功能是在開發者設定的映像上量的（final26：serial-debug 預設、網路 adb、選單編輯器開啟），量測對象是那台 `46a6` 工程樣機。`tests/builder/repro.sh` 當時證明那個 builder 與 final26 逐檔相同（只差刻意改過的 loader.conf，以及之後拿掉的 plain 選項 `lapis.conf`）。v0.2.1 的家族設定之後這句不再成立：開機命令列、兩個 RPL SOF 韌體檔，以及它們帶動的 vbmeta／super／initrd hash 鏈都和 final26 不同。功能表仍是較早那張映像在 `46a6` 上量到的，不是這次命令列的實機紀錄。

### serial-debug 還留著什麼

`lapis-debug.conf` 不帶 `console=ttyS0,115200n8`、`console=ttyS4,115200n8`、`console=uart8250,mmio32,0xfe03e000,115200n8`。這三個 token 在 KANO 上註冊不起 Linux tty：lapis 6.18.21 的 `CONFIG_SERIAL_8250_RUNTIME_UARTS=0`（`research/amd/lapis-kernel.config:3177`），debug 選項又拿掉 `8250.nr_uarts=4`，並用 pci-stub 佔住 LPSS UART `8086:51a8`。這不是「ttynull 先註冊、所以其他 console 類型都不會註冊」——Linux 允許不同類型的 console 同時輸出。在這張設定裡，正式 console 是內建的 `console=ttynull`；UART 上的 printk 來自 `earlycon=uart8250,mmio32,0xfe03e000,115200n8`，加上只在 debug 選項出現的 `keep_bootcon`（bootconsole 在 ttynull 註冊之後還留著）。`printk.devkmsg=on` 讓轉進 kmsg 的 logcat 從同一條 bootconsole 送出。earlycon 是單向輸出，不是可登入的 tty，也不是 getty。

QEMU 的功能測試會先拿掉映像上的 `console=`／`earlycon=`，再改成 COM1 的 `console=ttyS0` 與 I/O `earlycon`（`builder/image.py`）。原樣 ESP 開機鏈測試只在預設 quiet 時跑，quiet 本來就沒有序列埠。這兩項都不能代替 KANO MMIO UART 的實機紀錄。`tools/harness/kano-gbos-watch.py` 讀的是 AP UART 上的 printk（字面 `Kernel command line`），不開 `/dev/ttyS0`。

三個 `console=` 加回這 12 個 ID 的 debug 命令列是 1969 bytes。核心上限是 1973（`COMMAND_LINE_SIZE` 2048 − 1 − 73 bytes 的 `CONFIG_CMDLINE` − 1 byte 分隔空白），再扣 64 bytes 給 `kano-boot.sh` 之後 builder 只接到 1909。1969 放不進去，所以維持拿掉。目前 debug 是 1879 bytes（距核心上限還有 94 bytes），quiet 是 1787 bytes；兩者都含 systemd-boot 的 `initrd=\initrd.img ` 前綴。quiet 本來就沒有這三個 token。

**新使用者的預設映像也在實機上開過**（2026-10-12）：用全新 AVB 金鑰、quiet 預設、沒有 adb 的映像，模擬剛寫好的隨身碟
（所有分割區等於映像、/data 開頭清空、GPT 屬性為映像原始值），不按任何鍵開機：約 5 分鐘到 `BOOT_COMPLETED`，畫面停在
歡迎畫面（SetupWizard）；序列埠沒有任何輸出（quiet）；dmesg 顯示 `spi_intel`、`gsmi` 等被封鎖，沒有 kernel panic 或
system_server watchdog。首次開機在慢速隨身碟上有幾個 app（GMS、Play 商店）的 ANR，屬已知現象。這次開機 Android 只寫了
userdata、metadata、misc 三個分割區。

| 功能 | 狀態 | 證據 |
|---|---|---|
| 開機到 Googlebook 桌面 | 可用 | 內建 NVMe 開機 `boot_completed` 約 60 秒；OOBE、帳號登入、桌面都走完 |
| 螢幕 | 可用 | 需要 `xe.enable_dpt=0`（見 HOW-IT-WORKS） |
| 螢幕亮度 | 可用 | 攝影機固定曝光量測：亮度 0.05 → 0.9 時面板讀值 30 → 58 |
| 鍵盤 | 可用 | EC 模擬按鍵 → Android `getevent` 收到 `KEY_ESC` |
| 觸控板 | 可用 | Synaptics SYNA0000 06CB:CE92，Android 分類為 `TOUCHPAD`，實際操作游標移動 |
| 觸控螢幕、觸控筆 | 偵測到（未做點擊測試） | ELAN90FC 04F3:2FDB（含 Stylus），分類為 `TOUCH_MT`；需要有人實際觸碰螢幕才能驗證 |
| 喇叭 | 可用 | 兩顆 MAX98373；房間麥克風錄到 1 kHz 測試音（+70 dB）與鬧鐘音效預覽（+21～27 dB）；YouTube 有聲音 |
| 內建麥克風（DMIC） | 可用 | 喇叭 → DMIC 回授測試，1 kHz 從 -121 dB 升到 -42 dB |
| 耳機孔 | 未驗證 | NAU8825、jack 偵測已註冊，需要插入耳機測試 |
| Wi-Fi（AX211） | 可用 | 連上家用網路，ping 正常 |
| 藍牙（AX211） | 可用 | 3 秒開啟、0 次崩潰（在慢速 USB 隨身碟上，開機 I/O 高峰時藍牙堆疊會逾時崩潰，I/O 平穩後可開啟，見 JOURNAL） |
| 電源鍵 | 可用 | 亮螢幕時按下 → 關螢幕並 s2idle；休眠時按下 → 喚醒並亮螢幕 |
| 上蓋開關 | 可用（v0.2） | 闔上 1 秒內休眠，打開喚醒。v0.1 闔著時會在幾十秒內自己亮起；v0.2 映像闔著 300 秒、160 秒都沒亮（見下一列，2026-10-13 實測） |
| 休眠／喚醒（s2idle） | 可用（v0.2），鍵盤喚醒有已知問題 | 會進 s2idle。v0.1 待機留不住：每次暗中喚醒（RTC 鬧鐘、EC 事件）後，GKI 的 ACPI 電源鍵驅動都會送 `KEY_WAKEUP`，Android 當成喚醒鍵而亮螢幕，所以關螢幕或闔上蓋子後 7～76 秒就自己醒來（2026-10-13：關螢幕休眠 74 秒、闔蓋休眠 17.5 秒後醒，喚醒裝置都是 `Power Button`）。v0.2 起 KANO 映像帶 `gbos_wakefilter.ko`（`builder/kmod/build-gbos_wakefilter.sh`），電源鍵沒有喚醒系統時就丟掉這個 `KEY_WAKEUP`。2026-10-13 在隨身碟上的 v0.2 映像實測：闔蓋 300 秒期間暗中喚醒 10 次、160 秒期間 9 次，螢幕全程沒亮（觀察鏡頭畫面）；螢幕關閉時按電源鍵會亮。**已知問題**：螢幕關著時按內建鍵盤可能叫不醒螢幕：喚醒那一下按鍵在 i8042 resume 時被讀掉，模組判斷時還看不到任何按鍵事件，於是 `KEY_WAKEUP` 被丟掉（審查依核心原始碼推得，實機還沒測）；請用電源鍵或開合上蓋。這是已知問題，v0.2.1 沒有修。也沒有進到 S0ix：LPIT 的系統 residency 一直是 0，CPU package residency 則跟著休眠時間增加 |
| 平板模式開關 | 可用 | `SW_TABLET_MODE` 0/1 事件正確 |
| 電池電量 | 可用 | ACPI BAT0 |
| 「充電中」顯示 | 可用（v0.2） | v0.2 起 KANO 映像帶 ACPI 充電器驅動 `ac.ko`（`builder/kmod/build-ac.sh` 從固定版本的核心原始碼建置），只讀 ACPI `_PSR`，不送 EC 指令。QEMU 裡 `AC Adapter [AC] (on-line)`、healthd `chg=a`；KANO 實機（2026-10-13，隨身碟上的 v0.2 映像）接著充電器時 `BATTERY_CHANGED plugged=1`、狀態列顯示充電中。實際充電一直由 EC 自行處理，不受影響 |
| 鍵盤背光調整 | 不可用 | 這台的 EC 本身就調不動（連 EC console `kblight` 都無效），燈固定亮 |
| 自動旋轉、光感測 | 不可用 | 感測器在 EC 的 sensor hub 後面，目前封鎖 |
| 內建相機（IPU6 + ov2740） | 開發中 | 硬體路徑已在維護 Linux 實測出畫面；Googlebook 原本的相機 HAL 只支援 IPU7，IPU6 用的上游 libcamera HAL 已做成 KANO 機型設定的選項（`camera.libcamera`，預設關；開發者用 `GBOS_CAMERA_OVERLAY` 建置），Android 上的實機驗證未完成。外接 USB 鏡頭走 `usb` provider |
| USB-A | 可用 | 隨身碟開機與讀寫 |
| USB-C 資料、外接螢幕 | 未驗證 | 需要實體裝置 |
| 指紋 | 不適用／封鎖 | GSPI 由 pci-stub 佔住 |
| TPM（Cr50） | 刻意封鎖 | 安全規則，見 SAFETY |

## 已知的系統現象

- `timberslide`、`gscd`、`udsattestation` 需要 EC／TPM，在這裡會被 vendor 端 `stop`（否則每次開機崩潰數十次、拖慢 init）。
- 休眠時 Wi-Fi 會斷線（2026-10-13 實測）：lapis 的 Wi-Fi 模組在進入休眠時主動斷線（`disconnect isUserTriggered=true`、
  `DEAUTH_LEAVING` reason 3），醒來後 5～7 秒重新連上。所以螢幕關閉後連不到網路 adb，要先喚醒（harness 用 EC 電源鍵）。
- `system_server` 的 watchdog 逾時設為 5 分鐘（給慢速儲存裝置的餘裕）。
