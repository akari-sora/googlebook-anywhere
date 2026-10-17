# 第一步：把 KANO 換成 UEFI 韌體

原廠 ChromeOS 韌體只開簽章過的 ChromeOS，所以 Googlebook 需要 **MrChromebox 的 UEFI Full ROM**。這一步會改寫主機板上的
韌體晶片，**有變磚風險**。這個專案刻意不提供刷寫工具：刷寫一律交給 MrChromebox 的官方腳本，這份文件只把 KANO 需要的步驟
整理在一起，並附上官方說明的連結（2026-10-13 對照）。遇到問題或兩邊說法不同時，請以官方文件為準。

- 官方支援表（KANO：RW_LEGACY 與 UEFI Full ROM 都支援，寫入保護的解除方式是 `CR50 (SuzyQ)`）：
  <https://docs.mrchromebox.tech/docs/supported-devices.html>
- 解除寫入保護：<https://docs.mrchromebox.tech/docs/firmware/wp/disabling.html>
- 韌體工具腳本：<https://docs.mrchromebox.tech/docs/fwscript.html>

## 你需要

- Acer Chromebook Spin 714，型號 CP714-1WN 或 CP714-2WN（共用代號 KANO 主機板；13 代 CP714-2WN 亦稱 Zydron），量產版。
- 一條 **SuzyQ**（CCD 除錯）線：KANO 的寫入保護在 Cr50 安全晶片上，官方支援表上 KANO 的方法就是 SuzyQ。官方的 CR50 說明
  另外提到拔電池等方法，但支援表的 KANO 列只寫 SuzyQ，這份文件不涵蓋其他方法。
- 機器**不能**被學校或公司註冊／管理：官方文件說這種裝置的 CCD 被鎖住，`gsctool` 與 SuzyQ 的方法都不能用。
- 一支隨身碟：備份原廠韌體用（日後要回到 ChromeOS 就靠它）。
- 充電器全程插著，電量 50% 以上。

## 1. 開發者模式

依 Google 的說明進入開發者模式（`Esc` + `Refresh` + 電源 → `Ctrl+D`）。**這會清除 ChromeOS 上的所有本機資料。**
後面的指令都在登入畫面按 `Ctrl+Alt+F2`（VT2），以 `chronos` 登入後執行。

## 2. 先跑唯讀檢查

在 VT2 執行：

```sh
curl -LO https://gitlab.com/aa22396584/googlebook-anywhere/-/raw/main/install/kano-check.sh && bash kano-check.sh
```

`kano-check.sh` 只讀取資料，不寫任何東西。它會檢查：

- 機型是不是 KANO；
- CPU 是不是量產 stepping；
- HWID 有沒有 `TEST`；
- 目前的韌體、開發者模式、寫入保護、電量與充電器。

結果是 `RESULT: STOP` 就不要繼續；有 WARN 的話，先弄清楚原因再繼續。`RESULT: UNKNOWN` 表示 CPU 的 stepping／型號或韌體
認不出來（例如 13 代 CP714-2WN 的 Raptor Lake CPU `family 6 model 186`），這個檢查不能當作依據，請自己對照 MrChromebox 的官方文件。`RESULT: GO`（只有 12 代 Alder Lake-P stepping 3 或 4 才會出現）只表示
這個檢查**沒有找到已知的阻礙**，不代表保證刷得成功：刷寫仍以 MrChromebox 的官方腳本與文件為準。

最重要的一項是**工程樣機**（CPU stepping 小於 3，或 HWID 含 TEST）。這是這個專案在開發機上觀察到的結果，不是官方說法：
開發這個專案的那台就是工程樣機，公開韌體沒有它的 microcode，刷下去會卡在記憶體初始化；它的韌體是另外手動補上 microcode 的。

## 3. 用 SuzyQ 解除寫入保護

照官方「CR50」一節的步驟（這裡是摘要，指令與順序以官方頁面為準）：

1. **打開 CCD**：在 VT2 執行 `sudo gsctool -a -o`，依提示在最多約五分鐘內多次按電源鍵（實體確認）。成功時會顯示
   `PP Done!`，機器會重開進**一般模式（開發者模式被關掉）**，官方說明這是正常的。
2. **重新進入開發者模式**（同第 1 步，本機資料會再被清除一次），回到 VT2 以 `chronos` 登入。
3. SuzyQ 的 USB-C 端接 KANO 左側（靠上）的 USB-C 孔（只有一個孔是 CCD 孔），USB-A 端接在 KANO 自己的 USB 孔上（官方預設的 loopback 接法）。
   `ls /dev/ttyUSB*` 應該看到 `ttyUSB0`、`ttyUSB1`、`ttyUSB2` 三個。沒有的話先把 USB-C 接頭翻面、換另一個孔；
   還是不行，官方的做法是把 USB-A 端接到另一台電腦，在那台電腦上送下面的指令。
4. 解除硬體寫入保護（含開機時的設定，Cr50 重開後也維持）：
   ```sh
   echo "wp disable" > /dev/ttyUSB0
   echo "wp disable atboot" > /dev/ttyUSB0
   ```
5. **重設 CCD 權限**（官方步驟）：
   ```sh
   echo "ccd reset factory" > /dev/ttyUSB0
   ```
   官方說明這會把 CCD 的權限（包括 `AllowUnverifiedRo`、`OverrideWP`、`FlashAP`、`OpenFromUSB`）設成 Always，**日後才能用
   SuzyQ 救磚**。代價是：之後任何人只要拿到這台機器和一條 SuzyQ 線，不需要實體確認就能解除寫入保護、重刷韌體。
6. 驗證（VT2）：`sudo gsctool -a -w` 應顯示 `Flash WP: forced disabled`，`at boot:` 也是 `forced disabled`；
   `sudo gsctool -a -I` 的 CCD 權限應為 `Y`／`Always`；`crossystem wpsw_cur` 應為 `0`。全部符合後重開機。

## 4. 執行 MrChromebox 腳本

官方要求以一般使用者執行：**不要先 `sudo su`**，直接在 VT2 的 `chronos` 下執行：

```sh
cd; curl -LOf https://mrchromebox.tech/firmware-util.sh && sudo bash firmware-util.sh
```

1. 選 **Install/Update UEFI (Full ROM) Firmware**。
2. 腳本會先問要不要備份原廠韌體：**一定要備份**，存到隨身碟，再複製一份到別台電腦。
3. 完成後關機，再開機會看到 UEFI 韌體的畫面（官方說明：之後直接以 UEFI 模式開機，ChromeOS 不能再開）。

## 5. 之後

- 繼續 README 的第二步（建置映像）與第三步（寫入隨身碟、開機）。
- Googlebook 本身不會更新韌體：映像已移除韌體更新程式，也封鎖了相關驅動（見 SAFETY.md）。
- 量產機要更新 UEFI 韌體時，只用 MrChromebox 的官方腳本，照官方文件做；這個專案沒有測試過韌體更新。
- **工程樣機（CPU stepping 小於 3 或 HWID 含 TEST）不要跑任何韌體更新，也不要跑 MrChromebox 的腳本**：公開韌體沒有它的 microcode。
- 要回到 ChromeOS：用 MrChromebox 腳本的「Restore Stock Firmware」選項，指定第 4 步存的備份。
