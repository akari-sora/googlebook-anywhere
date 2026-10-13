# KANO 硬體狀態（Acer Chromebook Spin 714，CP714-1WN，brya／kano）

驗證日期 2026-10-12。驗證機是一台 Alder Lake-P K0 工程樣機，Googlebook 裝在內建 NVMe（SAMSUNG MZVLQ128HCHQ）。
每一項都是在實機上量到的結果，不是推論；唯一的例外寫明在該列（休眠／喚醒的鍵盤喚醒問題，目前是依核心原始碼推得）。驗證方式見最右欄；用到的工具在 `tools/harness/`。

表中的功能是在開發者設定的映像上量的（final26：serial-debug 預設、網路 adb、選單編輯器開啟）。`tests/builder/repro.sh` 證明
目前的 builder 用同樣設定產出的映像與它逐檔相同（只差刻意改過的 loader.conf，以及之後拿掉的 plain 選項 `lapis.conf`）。

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
| 休眠／喚醒（s2idle） | 可用（v0.2），鍵盤喚醒有已知問題 | 會進 s2idle。v0.1 待機留不住：每次暗中喚醒（RTC 鬧鐘、EC 事件）後，GKI 的 ACPI 電源鍵驅動都會送 `KEY_WAKEUP`，Android 當成喚醒鍵而亮螢幕，所以關螢幕或闔上蓋子後 7～76 秒就自己醒來（2026-10-13：關螢幕休眠 74 秒、闔蓋休眠 17.5 秒後醒，喚醒裝置都是 `Power Button`）。v0.2 起 KANO 映像帶 `gbos_wakefilter.ko`（`builder/kmod/build-gbos_wakefilter.sh`），電源鍵沒有喚醒系統時就丟掉這個 `KEY_WAKEUP`。2026-10-13 在隨身碟上的 v0.2 映像實測：闔蓋 300 秒期間暗中喚醒 10 次、160 秒期間 9 次，螢幕全程沒亮（觀察鏡頭畫面）；螢幕關閉時按電源鍵會亮。**已知問題**：螢幕關著時按內建鍵盤可能叫不醒螢幕：喚醒那一下按鍵在 i8042 resume 時被讀掉，模組判斷時還看不到任何按鍵事件，於是 `KEY_WAKEUP` 被丟掉（審查依核心原始碼推得，實機還沒測）；請用電源鍵或開合上蓋，下一版修正。也沒有進到 S0ix：LPIT 的系統 residency 一直是 0，CPU package residency 則跟著休眠時間增加 |
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
