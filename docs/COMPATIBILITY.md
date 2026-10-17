# 相容性（Googlebook Anywhere v0.2.1）

本文說明 Googlebook Anywhere v0.2.1 映像可以裝在哪些機器上、每台機型目前
的支援程度，以及**還沒實機驗證**的項目。

- 唯一在實機上驗證過的機器是 Acer Chromebook Spin 714（代號 KANO）的
  **Alder Lake-P 工程樣機**（iGPU `8086:46a6`）。
- 其他 KANO SKU 與其他 x86-64 電腦**尚未在實機上跑過**，狀態一律標成
  「預期可運作，未實機測試」。
- 本文只描述映像層面的相容性；換韌體、寫入隨身碟、安全取捨請看
  `FIRMWARE.md`、`SAFETY.md`、`HOW-IT-WORKS.md`。

> English version: `COMPATIBILITY.en.md`.

## 已驗證的機器

| 項目 | 值 |
|---|---|
| 機型 | Acer Chromebook Spin 714（KANO，Board ID 1，brya / kano） |
| 驗證機 | Alder Lake-P K0 **工程樣機**（exact Acer 零售 MPN `NX.*` 未確認） |
| CPU | 12th Gen Intel Core i7-1260P（Alder Lake-P 4P+8E 28W，CPUID `0x906a2` / `06-9a-02`） |
| iGPU | `8086:46a6`（ADL-P GT2 Iris Xe 96 EU，rev 08） |
| Host bridge | `8086:4621` |
| CBI SKU | `131073`（`0x20001`，FW_CONFIG=`0x13`（實機讀值）） |
| 記憶體 / 儲存 | 16 GB LPDDR4X-4266（DRAM strap ID 0）+ 128 GB `SAMSUNG MZVLQ128HCHQ` NVMe |

> 注意：`131073` 是 ChromeOS 的 CBI SKU ID（`0x20001`），**不是** CPU 型號。
> 這台是工程樣機，**不能**刷公開韌體。
>
> Board ID 與 FW_CONFIG（`0x13`）有實機讀值，紀錄在不公開的研究筆記；舊版表格寫的 `0x100000013` 在 repo 裡找不到來源。host bridge `4621`、LPDDR4X-4266、4P+8E 28W、iGPU rev 08 在這份 repo 裡沒有量測紀錄。

## KANO 家族：Acer Chromebook Spin 714

下表為 KANO（brya / kano）家族各 SKU 的 iGPU 與狀態。iGPU 欄列出
`profiles/kano/profile.toml` 中 `xe.force_probe` 允許的 PCI ID。
`4626`／`4628` 是 `46a6`／`46a8` 的單通道記憶體變體。
`46a3`、`46b2`、`a721`、`a7ac`、`a7ad` 是平台候選（巨集成員），KANO 零售對照尚未確認，不是已核對的替代 ID。

| 機型（零售型號） | CPU | iGPU PCI ID | v0.2.0 行為 | v0.2.1 狀態 |
|---|---|---|---|---|
| **驗證機**（見上表，CBI SKU `131073`） | i7-1260P | `8086:46a6` | 可用 | **較早的映像曾在這台工程樣機上測試；v0.2.1 尚未在實機上測試** |
| CP714-1WN-76B7 / -73F4 等 | i7-1260P | `8086:46a6`（單通道變體 `8086:4626`） | 推論：`46a6` 可用、`4626` 黑畫面 | 預期可運作，未實機測試 |
| CP714-1WN-7988 | i7-1260P | `8086:46a6`（單通道變體 `8086:4626`） | 推論：`46a6` 可用、`4626` 黑畫面 | 預期可運作，未實機測試 |
| CP714-1WN-53M9 / -574L 等 | i5-1235U | `8086:46a8`（單通道變體 `8086:4628`） | 推論：黑畫面 | 預期可運作，未實機測試 |
| CP714-1WN-34BQ / -38M1 等 | i3-1215U | `8086:46b3`（平台候選 `8086:46a3`、`8086:46b2`，KANO 對照未確認） | 推論：黑畫面 | 預期可運作，未實機測試 |
| 其他 ADL-P/U KANO SKU | i5-1240P / i7-1255U / i7-1280P | `8086:46a6` / `8086:46a8` | 推論：`46a6` 可用、`46a8` 黑畫面 | 未實機測試 |
| CP714-2WN-320J / -3184 等 | i3-1315U | `8086:a7a9`（平台候選 `8086:a7ac`、`8086:a7ad`，KANO 對照未確認） | 推論：黑畫面 | 預期可運作，未實機測試；音效未實機測試 |
| CP714-2W-56B2 / -5596 等 | i5-1335U / i5-1345U | `8086:a7a1`（平台候選 `8086:a721`，KANO 對照未確認） | 推論：黑畫面 | 預期可運作，未實機測試；音效未實機測試 |
| CP714-2W-732F / -71M6 等 | i7-1355U / i7-1365U vPro | `8086:a7a1`（平台候選 `8086:a721`，KANO 對照未確認） | 推論：黑畫面 | 預期可運作，未實機測試；音效未實機測試 |

### 為什麼 v0.2.0 只有一台能用？

v0.2.0 的 GPU 只放行單一 iGPU ID（`xe.force_probe=46a6`）。其他 KANO SKU
的 iGPU（`46a8`、`46b3`、`a7a1`、`a7a9` …）在 `xe_pci_probe()` 被以
`-ENODEV` 拒絕，而 lapis 沒有備援的 DRM/fbdev 驅動，結果就是**黑畫面或凍結**。
音效（`8086:51c8` → `sof-adl.ri`）在背景仍會初始化，但使用者看不到畫面。

### v0.2.1 改了什麼

v0.2.1 把 `profiles/kano/profile.toml` 的 `xe.force_probe` 從單一 ID 擴充成
**12 個 iGPU PCI ID**（包含 ADL-P/U 與 RPL-U，涵蓋單通道記憶體變體，
`46a6` 排在最前面，**沒有**萬用字元 `*`）。ADL-P 的 ID 定義在核心
`drivers/gpu/drm/xe/xe_pci.c`（`adl_p_desc`）與
`include/drm/intel/pciids.h`。

較早的映像曾在這台工程樣機上測試。v0.2.1 **尚未在任何實機上驗證過**——預期能修好上面那些黑畫面 SKU，但需實機確認。

### 第 13 代（CP714-2WN，代號 Zydron）的音效

13th-gen `CP714-2WN` 共用 `KANO` 主機板與 `Google_Kano.*` 韌體，但音效走
Raptor Lake（RPL）拓撲：需要 `intel/sof/community/sof-rpl.ri` 與
`intel/sof-tplg/sof-rpl-max98373-nau8825.tplg`。**未實機測試**。

## 其他 x86-64 電腦（`generic-x86`，實驗性）

給 Googlebook 核心沒有 GPU 驅動的 x86-64 UEFI 電腦（例如 AMD）。CPU 要符合下面的「CPU 最低需求」（Intel 第 12 代或 AMD Zen 5 及更新）。

- 畫面由韌體的 **GOP framebuffer** 輸出，繪圖由 CPU（**SwiftShader** 軟體算圖）負責。
- **沒有 GPU 加速**，會慢，解析度由韌體決定。
- **只在 QEMU 驗證過，還沒在任何實體 PC 上開過。**
- 需要能從 USB 開 UEFI 開機，且**關閉 Secure Boot**（映像沒有 Secure Boot 簽章）。
- Wi-Fi、音效這類需要機型韌體的功能多半不能用；能用的範圍以
  `install/gbos-probe.sh` 的報告為準。

## CPU 最低需求：預期 Intel 第 12 代及更新、AMD Zen 5 及更新（未實測）

2026-10-17 對 lapis 底包（ramdisk 第一階段 `/init`、`/system` 與 `/vendor` 的 `bin` 及 `lib64`、44 個 APEX，共 2,865 個 ELF）做了反組譯：

- 很多函式庫含 **AVX2 / FMA / BMI（x86-64-v3）**，例如影音編解碼、TFLite、ML 函式庫，總數約 72 萬條。
- 部分函式庫含 **GFNI**、**AVX-VNNI**、**SHA-NI**、**256-bit VAES / VPCLMULQDQ**、**AVX-512** 與 **AVX-VNNI-INT8**（libcrypto、libyuv、ML、音訊與相機函式庫）。
- 16 個含 Rust 程式碼的 ELF 檔（其中 `libzmij` 3 份、`ectool` 2 份）各含一條 **AVX-IFMA**（例如 `libinputflinger`、USB HAL、`ectool`、crosvm）。

很多地方沒有先檢查 CPU 就直接用：第一階段 `/init` 本身就直接用 x86-64-v3、GFNI、AVX-VNNI 和 SHA-NI；`libcrypto`
一律走 SHA-NI，沒有 AVX-512 時一律走 256-bit VAES／VPCLMULQDQ；zlib 的 adler32 只檢查 SSSE3 就執行 AVX-VNNI。
`libcrypto` 的 AVX-512 有執行期檢查（反組譯確認）；其他 AVX-512（共 41 個檔，例如 ML、音訊、相機函式庫與 `libjxl`）與 AVX-VNNI-INT8 推測有執行期檢查（KANO 兩者都沒有但實機正常）。

實機只驗證過 KANO（Intel 第 12 代 Alder Lake-P）。AMD HX 370（Zen 5）還沒在實機上跑過；同為 Zen 5 的
Ryzen AI 5 PRO 340 實測具備上面這些指令（AVX-IFMA 與 AVX-VNNI-INT8 除外：`avx_vnni=1 avx_ifma=0 avx512ifma=1 sha_ni=1 gfni=1 vaes=1 vpclmulqdq=1 avx_vnni_int8=0`，AVX-IFMA 見最後一點）。

- 先前寫的「只需要 AVX、Sandy Bridge 以上」**不正確**，已撤回。
- **預期可用**：Intel 第 12 代（Alder Lake）及更新、AMD Zen 5 及更新。
- **預期會失敗**（開機時因非法指令 SIGILL）：Intel 第 11 代及更早（沒有 VEX 編碼的 AVX-VNNI；第 6～10 代的 Skylake 系列及更早的
  Core 也沒有 SHA-NI、GFNI），以及 AMD Zen 4 及更早（Zen 4 有 AVX-512 VNNI，但沒有 VEX 編碼的 AVX-VNNI）。這是推論，本專案沒有在
  這些 CPU 上實測。第三方專案 gbOS86 在 Intel 第 8 代（i5-8365U）上的紀錄一致：開機時 `/init` 就執行 SHA-NI、zlib 執行
  AVX-VNNI；它在核心裡攔截非法指令、用軟體模擬才開得了機（較慢）。本專案沒有這個功能。
- **已知風險（AVX-IFMA）**：上面那 16 條 AVX-IFMA 沒有先檢查 CPU，在把非零浮點數轉成文字時執行（例如 JSON 錯誤訊息、
  crosvm 輸出浮點數）。KANO 和 Zen 5 都沒有這組指令（Zen 5 實測執行會 SIGILL），碰到這條路徑時那個程式會閃退。
  正常開機與使用時還沒遇過。

## `amd-hx370`（實驗性，從未在實機上跑過）

- 這是 AMD 機型的**實驗性** profile，**從未在真實硬體上執行過**。
- 提供兩個開機項目：
  - **預設「safe」**：不載入 `amdgpu`。
  - **「native amdgpu (experimental)」**：載入原生 `amdgpu`。stock minigbm 沒有 amdgpu backend，這個項目可能黑畫面；請改選 safe。
- 建置需要自建核心（`GBOS_DEV_KERNEL_DIR`）與 RADV，否則 `build.sh` 拒絕（見 `profiles/amd-hx370/profile.toml`）。
- 若機器啟用 **Secure Boot**：變更 Secure Boot 設定**之前**，務必先備份
  **BitLocker 復原金鑰**，否則可能無法解密。

## 回報新機型

要讓新的機器被納入支援，請在目標電腦上以任何 Linux 隨身碟執行唯讀探測：

```
install/gbos-probe.sh --out gbos-probe
```

它只讀 `/sys` 與 `/proc`，結果在 `gbos-probe/report.txt`。把報告附上
回報單（GitLab issue template）。新機型的完整適配流程見 `NEW-DEVICE.md`。
