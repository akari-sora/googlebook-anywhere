# Googlebook Anywhere v0.3.1

<!-- release-status: released -->

這版整理 KANO 的 USB 容量與機型識別、首次設定、背光及相機實驗功能，並推進共用維護媒體與更新防護。各功能的實機範圍不同，詳見 [驗收紀錄](VERIFICATION-v0.3.1.md)；v0.3.0 與更早的內容見 [完整版本紀錄](RELEASE-HISTORY.md)。

## KANO 預設開啟 USB-C（typec）

- KANO 建置沒有設定 `GBOS_EC` 時等於 `GBOS_EC=typec`：放行 `cros_ec`、`cros_ec_lpcs`、`cros_ec_dev`、`cros_ec_typec`、`cros_usbpd_notify`，讓 USB-C 做 USB3 與 DisplayPort alt mode。`sensors`、`buttons`、`drain` 仍須明確選用，而且 `drain` 不能和其他功能並用。其他機型維持不放行；對它們設 `typec` 仍會拒絕。
- 關掉：`GBOS_EC=none`，或把 `GBOS_EC` 設成空字串。`thunderbolt`、`cros_typec_switch`、`cros_ec_ucsi`、`cros_usbpd_charger` 維持封鎖。
- 載入 EC transport 之後，休眠失敗的復原仍可能重設 AP 並丟失進行中的寫入。硬體 S0ix 尚未量到。這是維護者接受的安全模型變更，不是新的實機驗收。
- USB-C 外接畫面與底座能用，是 issue #12 的使用者回報（零售 CP714-2W、v0.3.0、stock 韌體加 MrChromebox RW_LEGACY）。維護者尚未在實機確認。已標 v0.3.0 的映像仍是當時的預設（EC 封鎖）。
- 套用 typec 之後，serial-debug 命令列是 1892 bytes、quiet 是 1800 bytes，都在 builder 的 1909 bytes 限制內。drain 的 serial-debug 仍是 1909 bytes。

## KANO 首次使用與背光

- R6 測試 USB 的 F2FS 資料區讀得 42,919,243,776 bytes，型號顯示 Acer Chromebook Spin 714；一次正常關機再開機後容量與型號仍相符。資料與設定完整保留、冷開機連線穩定性仍待驗證。
- R6 首次設定未輸入 PIN，選擇 Skip、再選 Skip anyway 後抵達桌面；一般 Not now 路徑仍有問題。略過 PIN 的建置選項預設關閉，不能推論所有首次設定路徑都已修好。
- KANO 預設鍵盤背光曲線已調整，以處理先前最後一級亮度跳升；調整後尚未實機驗證。新的鍵盤模組為選用、預設關閉；曲線與模組是不同的變更。反覆睡醒、闔蓋與冷開機的背光表現仍待實測。

## 相機

- KANO 的 `camera.libcamera` 仍是預設關閉的實驗選項。來源候選 0006–0009 包含 CPU NV12、JPEG 容量與 stride、原生 buffer 及縮圖 stride 防護。相關候選通過 Android NDK 編譯與新映像離線檢查；這不是相機拍攝驗收。
- 先前 R8 原生診斷顯示相機 total／normal 均為 0。後續 0013 測試可辨識相機，但預覽仍是黑畫面；後續開發中的 0014 測試 USB 已到桌面並核對執行期身分；實機相機串流驗證已通過，但 CPU 影像處理器拒絕兩條輸出串流，預覽仍黑、拍照未通過，睡醒恢復仍待實測；詳見 [KANO 狀態](STATUS-KANO.md)。

## 共用 USB 與更新

- 已加入機型辨識、資料清單檢查、共用維護媒體診斷與預覽等開發功能。共用介面目前不授權寫入；內部已有受限的安裝與更新後端，但尚未接入共用維護啟動流程，不能據此宣稱同一支 USB 已能替 KANO 與 HX370 安裝、更新或復原。
- 更新工具加強來源、目標與寫入階段的檢查，調整舊版更新器的檔案描述符與資料傳輸，並補充測試及 CI 整理。模擬與離線檢查不能代替實體裝置的保留資料更新、失敗回復及備份還原驗收；操作與限制見 [共用安裝器](UNIVERSAL-INSTALLER.md) 和 [更新說明](UPGRADE.md)。

## 發行與驗證範圍

- 發行產物為安裝／建置腳本封存包與 SHA-256，不包含 Google 系統映像、韌體或簽章私鑰。
- 實驗室候選映像完成離線檢查，並非完整 Android 開機或硬體驗收；KANO QEMU 無 GPU，完整 Android 開機及 `system_server` 檢查維持 NOT_RUN。
- 相機、聲音與輸入周邊、睡眠／S0ix／拔電耗電、備份還原、內建碟更新回退及雙機共用 USB 的完成標準見 [剩餘目標](KANO-REMAINING-GOALS.md)。
