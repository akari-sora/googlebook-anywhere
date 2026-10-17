# 疑難排解（Googlebook Anywhere v0.2.1）

這份文件用「症狀 → 可能原因 → 怎麼辦」的方式，說明幾個已知的開機與使用問題。
只涵蓋有依據的項目；**還沒在實機上驗證過的，會直接標明「未實機測試」**。

換韌體、寫入隨身碟、安全取捨請看 `FIRMWARE.md`、`SAFETY.md`、`HOW-IT-WORKS.md`。
哪些機器能裝請看 `COMPATIBILITY.md`。要回報新機型請看 `NEW-DEVICE.md`。

> English version: `TROUBLESHOOTING.en.md`.

## 開機問題

### 選了開機項目後是黑畫面或凍結（KANO，非 `46a6` 的 SKU）

- **症狀**：在 KANO 上開機，選單出現、也開始載入，但之後只有黑畫面或整個凍結，
  電源燈可能是亮的。
- **可能原因**：你用的映像**只放行了單一 iGPU ID**（`xe.force_probe=46a6`）。
  其他 KANO SKU 的 iGPU（`46a8`、`46b3`、`a7a1`、`a7a9` …）在 `xe_pci_probe()`
  被以 `-ENODEV` 拒絕，而 Googlebook OS（lapis）沒有備援的 DRM/fbdev 驅動，
  所以沒有畫面。音效其實仍在背景初始化，只是你看不到。
- **怎麼辦**：
  - 這就是 **v0.2.0 只在 `46a6` 那一台上能用**的原因。
  - **v0.2.1 把 `xe.force_probe` 擴充成 12 個 iGPU PCI ID**（涵蓋 ADL-P/U 與
    RPL-U），預期能修好這個問題。
  - 但 **v0.2.1 尚未在任何實機上驗證過**（未實機測試）——請改用 v0.2.1 映像後
    再試一次，並把結果回報（見文末）。
  - 想知道自己的機器屬於哪個 SKU／iGPU，見 `COMPATIBILITY.md` 的 KANO 家族表。

### 第一次開機很慢，或跳出「沒有回應」

- **症狀**：從隨身碟第一次開機要等好幾分鐘，畫面可能跳出
  「沒有回應 / Not responding」對話框。
- **可能原因**：隨身碟讀取速度慢；首次開機要做的初始化比較多。
- **怎麼辦**：
  - 慢速隨身碟首次開機**要幾分鐘是正常的**，請耐心等。
  - 跳出「沒有回應」時**選「等待」（Wait）**，不要選結束。
  - 換一支較快的 USB 3.0 隨身碟會明顯改善。

### 開機就失敗，或開機後某些程式閃退（CPU 太舊）

- **症狀**：選單後幾乎立刻失敗、kernel panic，或開機後某些 App 一開就閃退。
- **可能原因**：**CPU 太舊**。lapis 的許多系統程式與函式庫直接編進 AVX2、GFNI、
  AVX-VNNI、SHA-NI 等指令而不先做執行期 CPU 分派；**只要程式實際執行到 CPU 不支援的那條指令**，就會觸發 `#UD`，程式收到 SIGILL（例如第一階段 `/init` 含有 SHA-NI 指令，若在沒有 SHA-NI 的 CPU 上走到該路徑，PID 1 收到 SIGILL 時核心就會 panic）。
- **怎麼辦**：
  - 只在 Intel 第 12 代（KANO）實機驗證過。第 11 代及更早的 Intel 預期會失敗（沒有 AVX-VNNI；第 6～10 代的 Skylake
    系列及更早的 Core 也沒有 SHA-NI，第三方 gbOS86 的紀錄顯示第一次開機時 `/init` 就會執行到）。
  - AMD Zen 4 及更早預期會失敗（沒有 VEX 編碼的 AVX-VNNI）；Zen 5（HX 370）是目標，但還沒在實機上跑過。
  - 在 KANO 或 Zen 5 上只有個別系統程式閃退：也可能是少數 Rust 程式裡的 AVX-IFMA 指令（這兩種 CPU 都沒有），
    請回報 logcat 裡的 SIGILL 和程式名稱。
  - 細節見 `COMPATIBILITY.md`「CPU 最低需求」一節（2026-10-17 對底包的反組譯）。

### 在一般 PC 上打不開（`generic-x86`）

- **症狀**：映像寫好、也插上 PC，但開不了機。
- **可能原因**：映像沒有 Secure Boot 簽章；機器無法從 USB 開 UEFI；或 CPU 早於 Intel 第 12 代／AMD Zen 5（缺少必要指令集）。
- **怎麼辦**：
  - 目標電腦要能**從 USB 開 UEFI 開機**，並**關閉 Secure Boot**。
  - 若 CPU 是 **Intel 第 11 代及更早**或 **AMD Zen 4 及更早**，請看上一節「開機就失敗，或開機後某些程式閃退（CPU 太舊）」。
  - 預設開機項目是 **Googlebook generic x86 quiet**。
  - 這條路徑**只在 QEMU 驗證過，還沒在任何實體 PC 上開過**（未實機測試）。

### 一般 PC 上 Android 看到「內建 eMMC」被當成外接儲存

- **症狀**：`generic-x86` 開機後，Android 把內建 eMMC 顯示成外接儲存裝置。
- **可能原因**：`generic-x86` 映像會在開機時藏起內建 **NVMe**
  （`initcall_blacklist=nvme_init`），但**內建碟是 eMMC 的電腦目前沒有藏起來**。
- **怎麼辦**：**不要**對它做格式化，也**不要**選「用作內部儲存空間」，
  以免覆蓋掉原本的內容。

## 音效問題

### 13 代 CP714-2WN（Zydron）沒有聲音

- **症狀**：畫面正常，但喇叭沒聲音。
- **可能原因**：13 代 `CP714-2WN` 雖然共用 `KANO` 主機板與 `Google_Kano.*`
  韌體，但音效走 Raptor Lake（RPL）拓撲，需要不同的 SOF 檔。
- **怎麼辦**：
  - 對應的音效需要 `intel/sof/community/sof-rpl.ri` 與
    `intel/sof-tplg/sof-rpl-max98373-nau8825.tplg`。
  - 這條路徑**未實機測試**；請用 v0.2.1 映像實測後回報。
  - 12 代的 KANO（ADL-P/U，`8086:51c8` → `sof-adl.ri`）在驗證機上喇叭正常。

## 休眠與喚醒

### 螢幕關著時，按鍵盤叫不醒螢幕（已知問題）

- **症狀**：螢幕已關閉時按鍵盤沒反應，螢幕不亮。
- **可能原因**：喚醒的那一下按鍵送到 Android 之前，`gbos_wakefilter.ko`
  已經把它的 `KEY_WAKEUP` 丟掉了。
- **怎麼辦**：
  - **請用電源鍵或開合上蓋**來喚醒。
  - 這是已知問題，v0.2.1 沒有修，請用電源鍵或開合上蓋。

### 螢幕會自己亮起（v0.1 的暗中喚醒）

- **症狀**：v0.1 映像中，闔上蓋子或關螢幕後幾十秒內螢幕自己亮起。
- **可能原因**：每次暗中喚醒都帶一個電源鍵的 `KEY_WAKEUP`。
- **怎麼辦**：**v0.2 起**映像帶 `gbos_wakefilter.ko`，電源鍵沒有喚醒系統時
  就丟掉它。KANO 實機上闔蓋 300 秒期間暗中喚醒 10 次、螢幕都沒亮。

### 沒有顯示「充電中」

- **症狀**：接上充電器，但 Android 沒有顯示充電中。
- **可能原因**：需要 ACPI 充電器驅動。
- **怎麼辦**：**v0.2 起**映像帶 `ac.ko`（只讀 ACPI，**不送 EC 指令**）；
  KANO 實機上接充電器時 Android 會顯示充電中。舊版（v0.1）沒有這個驅動。

## 安裝與寫入

### v0.2.0 只能從 USB 開，裝不進內建碟

- **症狀**：想裝到內建 NVMe，但找不到安裝選項。
- **可能原因**：直接用 `install.sh` 建出的映像**沒有維護系統**，不能拿來裝內建碟。
- **怎麼辦**：
  - 要裝到內建 NVMe，**v0.2.1 請用「安裝碟」（installer stick）**：
    用 `install/make-installer-stick.sh` 把建好的映像做成安裝碟檔，
    開機後選 **Install Googlebook to the internal disk**。
  - **這會清掉內建碟上的一切。**
  - 維護系統在 `kano-maint` 上，不必再跑 `setup-stick.sh`。
  - 注意：**這次全新安裝還沒在真實的 KANO 上做過**（未實機測試）。
  - 完整步驟見 `INSTALL-INTERNAL.md`。

### 寫入新版本時該保留什麼

- **症狀**：更新後開不起來，或想換回上一版。
- **可能原因**：更新必須用**同一把 AVB 金鑰**；舊版會留在開機選單上。
- **怎麼辦**：
  - **保留 `~/gbos-work/keys/`**，更新時要用同一把金鑰。
  - 舊版本會以「(previous version)」留在開機選單上。
  - 見 `UPGRADE.md`。

### Secure Boot 與 BitLocker（AMD 機型）

- **症狀**：要在 AMD 機器上試 `amd-hx370`，但系統開了 Secure Boot。
- **可能原因**：`amd-hx370` 是**實驗性** profile，**從未在真實硬體上執行過**
  （未實機測試）。
- **怎麼辦**：
  - 變更 Secure Boot 設定**之前**，**務必先備份 BitLocker 復原金鑰**，
    否則變更後可能無法解密磁碟。
  - 它提供兩個開機項目：預設「safe」（不載入 `amdgpu`）與
    「native amdgpu (experimental)」（載入原生 `amdgpu`）。stock minigbm 沒有 amdgpu backend，native 項目可能黑畫面；請改選 safe。
  - 建置需要自建核心（`GBOS_DEV_KERNEL_DIR`）與 RADV，否則 `build.sh` 拒絕（見 `profiles/amd-hx370/profile.toml`）。
  - 見 `SAFETY.md`。

## 硬體功能限制

以下是**目前做不到或刻意封鎖**的功能，多數與 EC（嵌入式控制器）或機型韌體有關：

| 功能 | 狀況 |
|---|---|
| 自動旋轉、光感測 | 需要經過 EC，刻意封鎖或硬體不支援 |
| 鍵盤背光調整 | 需要經過 EC，刻意封鎖或硬體不支援 |
| 內建相機 | 開發中，**預設關閉** |
| 觸控螢幕、觸控筆 | 偵測得到，但**還沒做實際點擊測試**（未實機測試） |
| Wi-Fi、音效（`generic-x86`） | 需要機型韌體，generic 映像多數沒帶，多半不能用 |

`generic-x86` 的畫面靠韌體的 GOP framebuffer、繪圖靠 CPU（SwiftShader），
**沒有 GPU 加速，會慢**，解析度由韌體決定。能用的範圍以探測報告為準
（見下一節）。

## 回報問題或新機型

在目標電腦上用**任何 Linux 隨身碟**（例如 Ubuntu Live USB）執行唯讀探測：

```
install/gbos-probe.sh --out gbos-probe
```

- 它**只讀 `/sys` 與 `/proc`**：不載入模組、不碰磁碟、**不讀序號**。
- 結果在 `gbos-probe/report.txt`：哪些裝置 Googlebook 有驅動（Wi-Fi、音效、
  觸控板、儲存裝置…），哪些沒有。
- 把報告附到 **GitLab 的 issue template** 回報單上，讓新機器能被納入支援。
- 完整的適配流程見 `NEW-DEVICE.md`。

> 提醒：為保護他人隱私，回報前請自行確認報告中不含個人資訊。
