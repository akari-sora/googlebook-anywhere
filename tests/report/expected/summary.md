# Googlebook 機器回報

日期：2026-10-14
工具：install/gbos-report.sh（唯讀。預設報告沒有自由文字，只有整數、固定格式的十六進位識別碼、列舉（含 card0–card99、renderD0–renderD999，沒有前導零），以及這個專案自己的版本字串。只有與映像釘選完全相同的核心版本才原樣印出。設 GBOS_DEV_KERNEL_DIR 為核心樹，且執行中的 release 等於該樹的 include/config/kernel.release 時，寫 dev-kernel。該樹的 .config 是一般檔時，其 sha256 等於釘選的 GKI 設定寫 config pass，否則寫 config fail。開發核心不印 release。機型名稱、BIOS 版本和其他描述請在 issue 裡自行填寫。不含 dumpsys／logcat 原文；缺值或檢查不過的值寫成「未知」。辨識到硬體不代表聲音、觸控、Wi-Fi 或休眠已經驗證過。這個腳本不附原始 log；若需要請直接向使用者索取。）
輸出防火牆：遮罩 0 行
來源：fixture、probe

| 項目 | 結果 | 來源 |
|---|---|---|
| 執行中的 profile | 未知（沒有映像標記） | — |
| profile 草稿 | kano（probe-only） | probe |
| 機型 | Google | fixture |
| 板名 | Google | fixture |
| 韌體 | coreboot | fixture |
| 韌體旗標 | UEFI yes, Secure Boot off | probe |
| 建置版本 | CL3B.260622.271.R1 | fixture |
| 核心 | other | fixture |
| 顯示／GPU 驅動 | 已綁定（xe） | fixture |
| GPU PCI | 8086:46a6 | fixture |
| DRM 卡 | card0 driver xe pci 8086:46a6 real-gpu | fixture |
| CPU | GenuineIntel family 6 model 154 stepping 3 | fixture |
| 音效卡 | 未知 | fixture |
| Wi-Fi | 已連線，驅動 iwlwifi | fixture |
| 藍牙 | 已開啟，驅動 btusb | fixture |
| 觸控／觸控板 | 觸控板：04f3:323c（i2c）；觸控螢幕：04f3:2d5b（i2c） | fixture |
| 休眠／喚醒 | 成功 4、失敗 1 | fixture |
| DRM 節點 | card0 renderD128 | fixture |
| live Linux GPU | 8086:46a6 xe | probe |
| live Linux 音效卡 | 未知 | probe |
| live Linux Wi-Fi | 探測到 wlan0（沒有連線狀態） | probe |
| live Linux 藍牙 | 探測到 hci0（沒有開關狀態） | probe |
| live Linux 觸控／觸控板 | 觸控板：04f3:323c；觸控螢幕：04f3:2d5b | probe |
| 比對 | 一致：DMI、CPU、PCI | fixture、probe |
