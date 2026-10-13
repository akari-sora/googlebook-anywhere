# 支援新的機型

目前只有 KANO 完整驗證過。同世代（Intel Alder Lake / Raptor Lake，brya 家族）的其他 Chromebook 大概率能開機；
一般 Intel 筆電與 AMD 機器還需要額外的工作（見最後一節）。每個型號都要逐項實機驗證。

## 前提

- 機器已經換成 **UEFI 韌體**（原廠 ChromeOS 韌體只能開簽章過的 ChromeOS）。換韌體需要拆寫入保護、刷 flash，
  有變磚風險，這個專案不做這一步，也不提供工具。請參考 MrChromebox 官方文件，並先確認你的型號在支援清單上。
- 目前的 lapis 核心只有 Intel 的 xe GPU 驅動；沒有 SATA（只看得到 NVMe、eMMC 與 USB 碟）。

## 機型設定：`profiles/<型號>/`

builder 裡和機型有關的值都在機型設定目錄，換 `GBOS_PROFILE`（或 `install.sh` 前設定 `GBOS_PROFILE=profiles/<型號>`）
就換機型；KANO 是 `profiles/kano/`，也是預設值。

沒有 lapis GPU 驅動的機器（AMD、較舊的 Intel、QEMU）用實驗中的 `profiles/generic-x86/`：`display.stack = "gop-swiftshader"`，
畫面走韌體的 GOP framebuffer（`builder/kmod/gbos_efifb.c`，從 base 映像自己的核心標頭、用 GKI 自己的 Android clang r584948c 建置，第一次約下載 1 GB、解開 3.5 GB；輸出以 SHA-256 釘住，每台 Linux 主機建出來都一樣），繪圖用 Cuttlefish 的 SwiftShader
（`builder/fetch-generic-inputs.sh` 下載並核對 SHA-256），沒有 GPU 加速、沒有機型韌體與音效設定。只在 QEMU 開到歡迎畫面過
（`research/amd/PLAN.md` 第 8 節）；實機的注意事項（BitLocker、Secure Boot、一次性開機選單）也在那裡。

| 檔案 | 內容 |
|---|---|
| `profile.toml` | 機型名稱與映像檔名、選單標題、開機參數（要拿掉的原廠參數、加上的參數、pci-stub ID、first stage 不載入的模組）、`module_blacklist`、韌體清單（來源與 SHA-256）、音效卡、Wi-Fi 要載入的模組、顯示設定、鍵盤背光 LED 路徑、電源鍵來源 |
| `vendor.py` | 需要寫文字的 vendor 修改（`profile.toml` 的 `hooks.vendor`）：DRAS 音效設定檔、鍵盤背光的 idc／ueventd／SELinux、電源鍵、hwcomposer 設定。新機型從 KANO 的這份複製再改 |
| `audio-src.list` | 音效設定的來源檔（ChromeOS board-overlays，建置時才下載，固定 SHA-256） |

`builder/lib/devprofile.py` 讀取並嚴格檢查設定：不認得的鍵、缺少的鍵、型別錯誤都會拒絕。全專案的安全規則在
`profiles/_safety.toml`，機型只能「加」不能「減」：

- 每個機型的 `module_blacklist` 都必須包含 `_safety.toml` 的 `blacklist.required`：v0.1 的整份硬封鎖清單（57 個模組：
  每種傳輸的 TPM 與 ChromeOS EC、EC 的 chardev 與周邊驅動、SPI flash、GSMI、NVRAM、chromeos_acpi、i2c-dev、AT24、Thunderbolt、
  觸控控制器的 QuickI2C／QuickSPI 等）。機器沒有的模組封鎖了也沒有代價。
- 機型自己的參數不可以用 `module_blacklist=`、`initcall_blacklist=`、`efi=`、`efi_pstore.`、`spi-intel.`、`lockdown=`
  開頭（這些由 builder 統一加上，放在後面會蓋掉前面的）；也不可以拿掉安全參數（`efi=noruntime` 等）。
- 每個開機選項最後都有 `initcall_blacklist=nvme_init,mei_me_driver_init efi=noruntime …`（USB 映像看不到內建 NVMe）。

`python3 builder/lib/devprofile.py check profiles/<型號>` 只做檢查；`tests/builder/profile-test.py` 證明 KANO 的設定
產生的開機參數與重構前的程式逐項相同。實際建出來的 KANO 映像與重構前逐檔相同（`tests/builder/repro.sh`）。

## 第一步：在目標機器上跑 probe

在目標機器上開任何 live Linux（USB），從這個 repo 執行：

```sh
install/gbos-probe.sh --out gbos-probe     # 不需要 root；只讀 /sys 與 /proc，不執行其他程式，只寫 --out 目錄
```

| 輸出 | 內容 |
|---|---|
| `report.txt` | 機型、韌體（UEFI、coreboot、Secure Boot）、CPU、GPU、內建碟、Wi-Fi、音效、輸入裝置，以及警告 |
| `coverage.txt` | 每個 PCI／USB／ACPI 裝置在 lapis 上有沒有驅動：lapis 的模組（`install/probe/lapis-modules.tsv`）或編進核心的驅動 |
| `profile.draft.toml` | `profiles/<型號>/profile.toml` 的起點（`status = "probe-only"`），能通過 `devprofile.py` 檢查，但每個值都要確認 |
| `probe.json` | 原始資料 |

它不讀序號、MAC 位址或碟上的資料。`lapis-modules.tsv` 只有 lapis（build 16452207）vendor_dlkm 與 system_dlkm 的模組
名稱與它們宣告的裝置比對字串（核心的 `MODULE_DEVICE_TABLE`），由 `install/probe/gen-module-list.py` 從映像產生。

常見的結論：

- GPU 是 Intel Gen12 但 xe 要 `xe.force_probe=<ID>`：probe 會放進草稿的 `cmdline.add`（KANO 是 `46a6`）。
- GPU 沒有 lapis 驅動（AMD、舊 Intel）：用 `profiles/generic-x86`（UEFI GOP framebuffer + SwiftShader，沒有 GPU 加速；
  只在 QEMU 驗證過，`display.stack = "gop-swiftshader"`）。
- 內建碟只有 SATA：lapis 看不到，只能從 USB 跑。
- 有 eMMC：USB 版映像要像 `nvme_init` 一樣把它藏起來，目前的安全規則還沒有這一項。
- `ACPI0003`（AC 變壓器）：lapis 沒有這個驅動。KANO 用 `kmods = ["ac"]` 帶上釘選建置的 `ac.ko`（`builder/kmod/build-ac.sh`），
  顯示「充電中」；其他機型 probe 看到 ACPI0003 時可以同樣加上。
- 電源鍵走 ACPI（`input.power_button = "acpi"`）的機型：GKI 的 ACPI 電源鍵驅動每次恢復都送 `KEY_WAKEUP`，暗中喚醒
  （RTC 鬧鐘、EC 事件）也會亮螢幕。加上 `kmods` 的 `gbos_wakefilter`（`builder/kmod/build-gbos_wakefilter.sh`）就只在電源鍵
  真的喚醒系統時才放行；`devprofile.py` 要求它和 `power_button = "acpi"` 一起用。

## 每個型號要確認的值

| 項目 | 怎麼查 | KANO 的值（`profiles/kano/profile.toml`） |
|---|---|---|
| LPSS UART／GSPI 的 PCI ID（給 pci-stub） | probe 的 `coverage.txt`，或 `lspci -nn` 找 `00:1e.x` | `cmdline.pci_stub_ids`：`8086:51a8 51a9 51aa 51ab` |
| 序列埠（serial-debug 選項，僅開發用） | coreboot log／`/proc/iomem` | `cmdline.add`：`earlycon=uart8250,mmio32,0xfe03e000,…` |
| GPU 參數 | probe 的 `report.txt`（`profile.draft.toml` 的 `display.stack`：xe 接得上是 `native`，否則 `gop-swiftshader`） | `display.stack = "native"`；`cmdline.add`：`xe.force_probe=46a6`；`cmdline.tail`：`xe.enable_dpt=0` 等 |
| 音效卡名稱與 UCM | `cat /proc/asound/cards`；UCM 取自 chromiumos board-overlays 的 `files/<型號>/audio/ucm-config/` | `audio.card = "sof-nau8825"`，DRAS 檔在 `vendor.py` 的 `audio()` |
| SOF 韌體與 topology | `dmesg | grep sof` 會印出要的檔名 | `[[firmware]]`：`sof-adl.ri`、`sof-adl-max98373-nau8825.tplg`（IPC3） |
| Wi-Fi／藍牙韌體與模組 | `dmesg | grep -E "iwlwifi|Bluetooth"` | `[[firmware]]` 的 AX211 檔、`wifi.modprobe = ["iwlmvm"]` |
| 觸控裝置 | probe 的 `report.txt`（`/proc/bus/input/devices`） | ELAN 觸控板與觸控螢幕（i2c-hid） |
| 鍵盤背光 LED 路徑 | probe 的 `profile.draft.toml` 註解（`/sys/class/leds/`） | `input.kbd_backlight_leds` |
| 電源鍵來源 | 有沒有 `cros_ec_buttons`；沒有就放行 ACPI Power Button | `input.power_button = "acpi"` |

## 驗證順序（照 KANO 走過的順序最省時間）

1. `GBOS_PROFILE=profiles/<型號> builder/build.sh`：QEMU 自我測試會自動跑（設定檢查在最前面）。
2. 寫到 USB，用 serial-debug 選項開機（需要序列埠；沒有的話用 quiet，靠 `/data/kano-diag` 事後讀取）。
3. 顯示 → OOBE → Wi-Fi → 觸控 → 音效 → 電源鍵／上蓋／休眠，對照 `docs/STATUS-KANO.md` 的方法逐項驗證。
4. 任何需要放寬安全封鎖的功能，先讀 `docs/SAFETY.md`。驗證通過後才把 `model.status` 改成 `verified`。

## 還沒做的

- `base/` 仍是 KANO 那份基礎映像的逐位元組重建（`kano-real.img`），它 ESP 裡的開機選項（含 `xe.force_probe=46a6`）
  builder 已經不再使用：開機參數一律由 vendor_boot 的原廠參數、機型設定與安全規則產生。
- vendor 裡的服務與腳本名稱仍是 `kano-*`（功能通用；改名會讓 KANO 映像與已驗證的版本不同）。
- 一般 PC 的通用顯示 fallback 已是 `profiles/generic-x86`（只在 QEMU 驗證過）；AMD 的自建核心（amdgpu）：見 `research/amd/PLAN.md`。
