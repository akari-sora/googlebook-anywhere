# KANO 硬體狀態（Acer Chromebook Spin 714，CP714-1WN，brya／kano）

驗證日期 2026-10-12。驗證機是一台 Alder Lake-P K0 工程樣機，Googlebook 裝在內建 NVMe（SAMSUNG MZVLQ128HCHQ）。
每一項都是在實機上量到的結果，不是推論。驗證方式見最右欄；用到的工具在 `tools/harness/`。

表中的功能是在開發者設定的映像上量的（final26：serial-debug 預設、網路 adb、選單編輯器開啟）。`tests/builder/repro.sh` 證明
目前的 builder 用同樣設定產出的映像與它逐檔相同（只差刻意改過的 loader.conf 與 plain 選項名稱）。

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
| 上蓋開關 | 可用 | 闔上 1 秒內休眠，打開喚醒 |
| 休眠／喚醒（s2idle） | 可用 | 鍵盤（i8042）、電源鍵、上蓋都能喚醒 |
| 平板模式開關 | 可用 | `SW_TABLET_MODE` 0/1 事件正確 |
| 電池電量 | 可用 | ACPI BAT0 |
| 「充電中」顯示 | 不可用 | 充電器狀態要經過 EC（`cros_usbpd_charger`），目前封鎖；實際充電由 EC 自行處理，不受影響 |
| 鍵盤背光調整 | 不可用 | 這台的 EC 本身就調不動（連 EC console `kblight` 都無效），燈固定亮 |
| 自動旋轉、光感測 | 不可用 | 感測器在 EC 的 sensor hub 後面，目前封鎖 |
| 內建相機（IPU6 + ov2740） | 不可用 | Googlebook 的相機 HAL 是給 IPU7 用的，需要移植 IPU6 HAL；外接 USB 鏡頭走 `usb` provider |
| USB-A | 可用 | 隨身碟開機與讀寫 |
| USB-C 資料、外接螢幕 | 未驗證 | 需要實體裝置 |
| 指紋 | 不適用／封鎖 | GSPI 由 pci-stub 佔住 |
| TPM（Cr50） | 刻意封鎖 | 安全規則，見 SAFETY |

## 已知的系統現象

- `timberslide`、`gscd`、`udsattestation` 需要 EC／TPM，在這裡會被 vendor 端 `stop`（否則每次開機崩潰數十次、拖慢 init）。
- 休眠時 Wi-Fi 會斷線（`DEAUTH_LEAVING`），解鎖後重新連線。
- `system_server` 的 watchdog 逾時設為 5 分鐘（給慢速儲存裝置的餘裕）。
