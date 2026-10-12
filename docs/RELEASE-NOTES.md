# Googlebook Anywhere v0.1.0

第一個版本：把 Google 官方 Googlebook（lapis，build 16452207）轉成 Acer Chromebook Spin 714（KANO）可開機的隨身碟映像。

**這個下載檔只有腳本與文件，不含任何 Google 映像或韌體。** 執行 `install/install.sh`（Linux，或 Windows 的 WSL2）或
`install/docker-install.sh`（macOS 的 Docker Desktop；Windows 上也要在 WSL 裡執行），會從 Google 官方下載映像並在你的電腦上建置。
開發用的 `tools/`（實機 harness、發版腳本）與 `tests/harness/` 不在這個下載檔裡，只在 git repo。

## 驗證紀錄（發版前在開發機上跑過）

- 新使用者完整流程（官方 zip → 新 AVB 金鑰 → 映像 → 兩個 QEMU 自我測試）：全部 PASS。
- 預設映像（quiet、無 adb）寫到隨身碟、空白 /data，在 KANO 實機開到歡迎畫面（SetupWizard、BOOT_COMPLETED）。
- 可重現性：開發者設定重建的映像與實機驗證過的版本，每個分割區與 ESP 檔案相同（除了刻意變更的開機選單設定）。
- 開了 adb 的開發者 build（KANO 實機）：`ro.adb.secure=1`、只聽 TCP 5555；沒登記的金鑰被拒（`unauthorized`，沒有授權對話框），
  登記的金鑰是 `uid=2000(shell)`。builder 寫進 vendor 的 `ro.debuggable=1` 沒有生效，實機讀到 `ro.debuggable=0`（見 SAFETY.md）。
- CI（每次 push）：shellcheck／py_compile、builder 快速測試、`install.sh` 參數測試、write-usb 的拒絕測試與部分防呆（用 loop 假碟的
  案例在 CI 容器裡 SKIP，在 Linux 開發機上跑過）、韌體檢查、安裝碟核對邏輯的 selftest、harness 的防呆測試與 mutation 檢查。

## 這一版的防呆

- `write-usb.sh`：寫入前就要有 `sgdisk`；備份 GPT 由寫入程式在同一個獨占開啟的裝置上搬到碟尾並核對；沒做過 QEMU 自我測試的映像要多輸入 `untested`。
- `install.sh`：重用工作目錄前比對建置設定（含 `GBOS_DEFAULT_ENTRY`、`GBOS_LOADER_EDITOR`）與原始碼內容摘要，不同就停止。
- `docker-install.sh`：macOS 內建的 bash 3.2 可用；路徑不能含 `:`；Linux 主機的 KVM 群組會帶進容器。
- `kano-check.sh`：認不出 CPU 或韌體時是 `UNKNOWN`，不會給 `GO`。

## 已知限制

見 README「現況」與 docs/STATUS-KANO.md：自動旋轉、光感測、充電中圖示、鍵盤背光調整、內建相機不可用；觸控螢幕點擊、
耳機孔、USB-C 資料／DisplayPort 未在實機驗證。只支援 KANO（量產版）。
