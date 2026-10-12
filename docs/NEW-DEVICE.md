# 支援新的 Chromebook 型號

目前只有 KANO 完整驗證過。同世代（Intel Alder Lake / Raptor Lake，brya 家族）的其他 Chromebook 大概率能開機，
但下列項目每個型號都不一樣，要逐一確認、逐台實機驗證。

## 前提

- 機器已經換成 **UEFI 韌體**（原廠 ChromeOS 韌體只能開簽章過的 ChromeOS）。換韌體需要拆寫入保護、刷 flash，
  有變磚風險，這個專案不做這一步，也不提供工具。請參考 MrChromebox 官方文件，並先確認你的型號在支援清單上。
- Intel GPU（xe 驅動）；AMD 與 ARM 的 Chromebook 不支援。

## 目前的程度：只有 KANO

builder 現在是 **KANO 專用**。`profiles/<型號>/` 裡真正由資料驅動的只有音效來源清單（`audio-src.list`）與從
`card_settings` 轉出的音量曲線；換一個 `GBOS_PROFILE` 產出的仍然是 KANO 的映像。其餘機型相關的值寫在程式裡：

| 值 | 位置 |
|---|---|
| pci-stub 的 PCI ID、開機參數、模組封鎖清單 | `builder/image.py`（`debug`、`BL`、`tail`） |
| 韌體檔名與固定的 SHA-256 | `builder/vendor.sh`（`fw` 區段）、`builder/fetch-inputs.sh` |
| 音效卡名稱、PCM 編號、mixer 控制項 | `builder/vendor.sh`（safe19 區段，人工從 HiFi.conf 翻譯） |
| 鍵盤背光 LED 路徑、電源鍵設定 | `builder/vendor.sh`（idc／ueventd／genfscon、`excluded-input-devices.xml`） |

適配新機型時，先把這些值搬進 `profiles/<型號>/`，再逐項實機驗證。

## 每個型號要建立的設定（`profiles/<型號>/`）

| 項目 | 怎麼查 | KANO 的值 |
|---|---|---|
| LPSS UART／GSPI 的 PCI ID（給 pci-stub） | `lspci -nn` 找 `00:1e.x` | `8086:51a8 51a9 51aa 51ab` |
| 序列埠 MMIO 位址（earlycon，僅開發用） | coreboot log／`/proc/iomem` | `0xfe03e000` |
| 音效卡名稱與 UCM | Linux live 下 `cat /proc/asound/cards`；UCM 取自 chromiumos board-overlays 的 `files/<型號>/audio/ucm-config/` | `sof-nau8825`、`sof-nau8825.kano/HiFi.conf` |
| SOF 韌體與 topology | `dmesg | grep sof` 會印出要的檔名 | `sof-adl.ri`、`sof-adl-max98373-nau8825.tplg`（IPC3） |
| Wi-Fi／藍牙韌體 | `dmesg | grep -E "iwlwifi|Bluetooth"` | AX211：`iwlwifi-so-a0-gf-a0-89`、`ibt-0040-0041` |
| 觸控裝置 | `/proc/bus/input/devices` | SYNA0000、ELAN90FC（i2c-hid） |
| 鍵盤背光 LED 路徑 | `ls /sys/class/leds/` 載入 cros_kbd_led_backlight 後 | `platform/GOOG0004:00/GOOG0002:00/leds/chromeos::kbd_backlight` |
| 電源鍵來源 | 有沒有 `cros_ec_buttons`；沒有就放行 ACPI Power Button | ACPI |

## 驗證順序（照 KANO 走過的順序最省時間）

1. QEMU 自我測試（`builder/build.sh` 會自動跑）。
2. 寫到 USB，用 serial-debug 選項開機（需要序列埠；沒有的話用 quiet，靠 `/data/kano-diag` 事後讀取）。
3. 顯示 → OOBE → Wi-Fi → 觸控 → 音效 → 電源鍵／上蓋／休眠，對照 `docs/STATUS-KANO.md` 的方法逐項驗證。
4. 任何需要放寬安全封鎖的功能，先讀 `docs/SAFETY.md`。
