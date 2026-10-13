# Googlebook Anywhere v0.2.0

第二個版本：**修好新使用者建置會失敗的問題**，加上通用 x86 的實驗版、機型 profile、保留資料的更新，以及 KANO 的充電顯示與待機修正。

**這個下載檔只有腳本與文件，不含任何 Google 映像或韌體。** 使用方式與 v0.1.0 相同，見 README。

## 先看這個：v0.1.0 的新使用者建置會失敗

ci.android.com 把下載轉到一個 300 秒後失效的簽章網址，而 `install.sh` 第 2 步要從那裡下載 15 個檔案，一般網路要超過 5 分鐘，
於是在第 2 步以 `HTTP Error 400` 停止（2026-10-13 重現兩次）。開發機因為有快取一直沒遇到。v0.2.0 每 120 秒重新取得簽章網址，
被拒時再取一次（`base/gbos_base.py`，測試 `tests/builder/fetch-zip-test.py`）。**用 v0.1.0 的人請直接改用 v0.2.0。**

## 新功能

- **一般 x86 電腦（實驗性）**：`profiles/generic-x86`，給 Googlebook 核心沒有 GPU 驅動的 x86-64 UEFI 電腦（AMD、較舊的 Intel）。
  畫面用韌體的 framebuffer（`gbos_efifb.ko`，釘選建置），繪圖用 CPU（SwiftShader，從 Cuttlefish 公開建置取得並核對 SHA-256）。
  建置：`GBOS_PROFILE=profiles/generic-x86 install/install.sh --work ~/gbos-generic`。**只在 QEMU 驗證過**，見 README「其他 x86-64 電腦」。
- **唯讀相容性檢查** `install/gbos-probe.sh`：在目標電腦上的任何 Linux 執行，列出 Googlebook 有沒有它的驅動，並產生 profile 草稿。
- **機型 profile**：`profiles/<機型>/profile.toml`；全專案的安全底線在 `profiles/_safety.toml`（每個 profile 都必須封鎖的 57 個模組）。
- **保留資料的更新**：隨身碟使用者 `install/update.sh`（`write-usb.sh --keep-data`）；安裝到內建碟的人用安裝碟的
  「Update Googlebook on the internal disk (keep data)」。新版本寫進另一個 A/B slot，`/data` 保留，舊版本留在選單上。
  見 docs/UPGRADE.md。
- **KANO**：
  - 「充電中」圖示：ACPI 充電器驅動 `ac.ko`（只讀 ACPI，不送 EC 指令）。KANO 實機驗證過。
  - 待機：`gbos_wakefilter.ko` 讓暗中喚醒不再亮螢幕。KANO 實機上闔蓋 300 秒期間暗中喚醒 10 次，螢幕都沒亮；螢幕關閉時按電源鍵會亮、開蓋會亮螢幕。
  - 內建相機：做成機型選項（`camera.libcamera`，預設關），開發中。
  - 拿掉了 plain 開機選項。

## 驗證紀錄（發版前在開發機上跑過）

- 新使用者完整流程：KANO 與 generic-x86 各一次（官方 zip → 新 AVB 金鑰 → 映像 → 兩個 QEMU 自我測試），全部 PASS。
- 更新：從 v0.1.0 的使用者映像保留資料更新到 v0.2，再更新第二次（QEMU 29/29）；安裝碟的更新情境，用真隨身碟的維護系統（QEMU 55/55）；
  更新引擎的 selftest 與 mutation 檢查（每個列出的 mutant 都被測試抓到）。
- KANO 實機（2026-10-13，隨身碟上的 v0.2 映像）：充電顯示、闔蓋待機（暗中喚醒不亮螢幕）、螢幕關閉時電源鍵與開蓋會亮螢幕。
- CI（每次 push）：shellcheck／py_compile、builder 快速測試、安裝與寫入的防呆測試、安裝碟核對邏輯、harness 防呆與 mutation 檢查。

## 已知問題與限制

- **KANO：螢幕關著時按鍵盤可能叫不醒螢幕**（喚醒那一下按鍵送到 Android 之前，`gbos_wakefilter` 已經把 `KEY_WAKEUP` 丟掉了）。
  請用電源鍵或開合上蓋。修正正在開發與審查（不在這一版裡），預計 v0.2.1。
- KANO：沒有進到 S0ix（待機時 CPU 已進最深的省電狀態，但平台沒有），所以自動旋轉、光感測、USB-C 的 EC 功能仍關閉。
- **一般使用者這一版只能從隨身碟執行。** 裝到內建碟的安裝程式需要隨身碟上的維護系統，目前沒有自動建立的腳本
  （只有開發者流程，見 docs/INSTALL-INTERNAL.md）；自動做出安裝碟排在下一版。
- KANO 的內建碟「保留資料更新」還沒在實機上跑過，只在 QEMU 驗證；更新前請照 docs/INSTALL-INTERNAL.md 備份。
- generic-x86 還沒在任何實體電腦上開過；沒有 GPU 加速；Wi-Fi、音效等需要機型韌體的功能大多不能用。內建碟是 eMMC 的電腦，
  Googlebook 看得到那顆碟（不要對它格式化）。
- 其他見 README「現況」與 docs/STATUS-KANO.md。

---

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
