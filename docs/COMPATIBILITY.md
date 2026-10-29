# 相容性（Googlebook Anywhere v0.3.0，未發布）

本文說明 Googlebook Anywhere v0.3.0 映像可以裝在哪些機器上、每台機型目前
的支援程度，以及**還沒實機驗證**的項目。

- 唯一在實機上驗證過的機器是 Acer Chromebook Spin 714（代號 KANO）的
  **Alder Lake-P 工程樣機**（iGPU `8086:46a6`）。
- 其他 KANO SKU 與其他 x86-64 電腦**尚未在實機上跑過**，狀態一律標成
  「預期可運作，未實機測試」。
- 本文只描述映像層面的相容性；換韌體、寫入隨身碟、安全取捨請看
  `FIRMWARE.md`、`SAFETY.md`、`HOW-IT-WORKS.md`。

> English version: `COMPATIBILITY.en.md`.

v0.3.0 的 16658739 pins 與 Microdroid IFMA pins 不代表實機驗收；新映像的結果見 [驗收紀錄](VERIFICATION-v0.3.0.md)。以下實機結果屬較早映像。

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
- build 會精確修補 17 個 host ELF：原本 16 個目標，加上 `system_ext_a/bin/trusty_security_vm_launcher`。舊證據把 Microdroid guest 稱為第 17 個 site，因此 Trusty coordinator 在該編號序列中稱 site 18；實際 PATCHED 目標總數仍是 17。

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
- **已知風險與建置期修補（AVX-IFMA，`[cpu_compat] patch_ifma = true`）**：patcher 對 17 個目標核對 SHA-256、可執行 ELF 映射、context、常數表與原始 encoding，再做 12-byte 原位替換；目標分布為 `vendor_a` 5、`system_a` 11、`system_ext_a` 1。APEX signer、payload key/salt、EROFS metadata 與 keyset 持久化仍依 builder 的精確檢核。
  精確檢查包含可執行 `PT_LOAD`／`PF_X` mapping、73-byte context 與 128-byte table。替換為 `c4 e3 69 02 5e 10 05 c4 e2 f1 b4 c3`（`vpblendd` + `vpmadd52luq`）→ `c5 f1 f4 4e 10 c5 f9 d4 c1 0f 1f 00`（`vpmuludq` + `vpaddq` + nop）。目標是 vendor 的 `lib64/libzmij.dylib.so`、`bin/ectool`（刪除前驗證）、USB desktop HAL；system 的 `libzmij`、`libinput`、`libinputflinger`、`prefetch`、`ectool`、`aisealhostservice`；virt APEX 的 `vm`、`virtmgr`、`early_virtmgr`、`crosvm_minimal`、`crosvm`；audio APEX 的 `libzmij`；fingerprint APEX 的 crosfp service；以及 system_ext 的 Trusty launcher。精確路徑與雜湊見 manifest。
  重建 APEX 保留原廠 v3 signer 的 min/max SDK 與核准的 payload `apex.key`／salt。原廠輸入可帶已知的選用 SourceStamp block，但仍獨立驗證 v2/v3 簽章；重簽輸出拒絕舊 stamp block。其他 ZIP entry（含 `stamp-cert-sha256`）皆保留原始壓縮 bytes 與 name／CRC／size／順序；此 metadata 會讓一般 apksigner 驗證出現預期的缺少 SourceStamp 警告，不代表 SourceStamp 真實性已獲驗證。system、system_ext 與 APEX 回讀比較 inode 邏輯 metadata、全部支援 xattr 原始值（含 capabilities）、UUID、build time、pcluster、superblock feature bits 與 volume name；inode 實體配置、壓縮資料位置、inline/shared xattr 編碼可改變。APEX keyset 預設放在 `$(dirname "$GBOS_AVB_KEY")/apex-keys`；installer 預設為 `$WORK/keys/apex-keys`，明確的 `GBOS_APEX_KEYS_DIR` 可覆蓋兩者。請與 AVB key 一起備份：遺失會改變下次 build 的 APEX 身分；Android 17 的 keep-data 行為仍需實機驗證。build-info 記錄 payload 公鑰 SPKI DER 與 container 憑證 DER 的 SHA-256 指紋。
  掃描器不刪除任何 raw VEX candidate。manifest 將其完整分類為 17 個 patched targets、29 個 `known_non_instructions`（`product_a` 17、`system_a` 1、`vendor_a` 2，以及 `boot_a`、`boot_b`、`efi_system_a` 各 3）、9 個 residual 與 1 個 `unsupported_allowlist`；每個 non-instruction/residual 都釘選 partition、逐層 typed container chain、目標 SHA-256（container 層有指定者亦同）、file offset 與 `encoding_hex`／指令 encoding。反組譯或 `.gnu.hash` 證據只決定分類，不能讓 candidate 從報告消失。
  host kernel 的 9 個 pin 對應三個 byte-identical 放置位置（`boot_a`／`boot_b` 的 kernel payload 與 ESP `kernel.efi`）中的三個 site。解開的 vmlinux SHA-256 為 `f8ede4cc42dc65707dea5c281eb6d4f89f3b15a510c51f61fe5f5afbc44cddd5`；從 symbol 起點解碼，`0x5fa481`／`0x5fa617` 位於 `aes_ctr64_crypt_vaes_avx512` 的 EVEX `vpxord` 內，`0x1325ad8` 位於 `uas_queuecommand` 的 `add $0x2,%r12` 內，全部屬 non-instruction。每個放置位置使用獨立的精確 typed pin。
  9 個 residual 是：(1) Microdroid guest `microdroid_manager` 的真 `vpmadd52luq`，`microdroid.img` SHA-256 `df0733894a15bc0cb60480c38958f4d272c196d56b4230f253f2d921faa1e66f`、binary SHA-256 `32efab45a5653acc33c440fc0761744e70a95caf45b807209764185da747bb15`、offset `0x17b96d`；(2) guest kernel 兩個 raw candidate，kernel/vmlinux SHA-256 `47d8cc1b17efff78e77427669a87da77a7905a3e8cca88fe81e5a665d1c4c85d`／`ac5b5281c859544b388080331087633e287e1e6217725614a8f28886fde592e3`、offset `0x4a0d71`／`0x4a0f07`，實際位於 feature／xstate guard 後的 `aes_ctr64_crypt_vaes_avx512` EVEX `vpxord` 內；(3) 六個 recovery ramdisk 命中，目前 repo boot entries 不會載入，其中 `libzmij` SHA-256 `0e8ac83aa1b8c130a11b670250a4829d9750ec854ed3bc8c4ce2e4d2f0f9353e` 在 `0x42dd`，四條 stripped recovery binary 路徑共用 SHA-256 `7e2c7563c54dba1115e83b4f5a624202a190a370b5f02868f16dde389eb033b3`、offset `0xc75f7`。唯一 unsupported pin 是 `etc/fs/microdroid_vbmeta.img` SHA-256 `72a24483f3eba2280623dab019bff5ce291741f18d4554b1adc6ee89ecf94d9a`：65,536-byte AVB0 metadata（libavb 1.0、algorithm 2、auth 576、aux 1600、avbtool 1.4.0），沒有可執行 filesystem payload。
  candidate pin 不存在或改變會停止建置。scanner 以已知 magic 與 image 副檔名的聯集（含解壓 payload 依每層解碼類型先剝除 `!<kind>`／`:<kind>` 與對應 `.gz`／`.gzip`、`.xz`、`.zst`／`.zstd`、`.lz4`，若外層副檔名與實際壓縮 magic 不符則退回剝除單一通用壓縮副檔名後推導出的內層副檔名）辨識 xz、gzip、zstd、lz4、zip、newc、EROFS、ext4、Android sparse、AVB0 與 bzImage；命中任一入口卻無法完整開啟或超過深度的 nested container 會停止。為與 Linux kernel `lib/decompress_inflate.c:__gunzip` 一致，gzip 依 header flags（`FHCRC`／`FEXTRA`／`FNAME`／`FCOMMENT`）與 raw deflate 解碼（含在輸入 EOF 時 `flush()` 取出 `>64 KiB` 無 trailer 串流尾端緩衝輸出）並略過 8-byte trailer（有效 `Z_STREAM_END` 後的 CRC／ISIZE 不符或缺少／截斷 trailer 仍解碼掃描；raw deflate 損毀則為硬錯誤）；legacy LZ4（`0x184C2102`）依 Linux `lib/decompress_unlz4.c:unlz4()` 區塊規則解碼且不以先解出區塊為前提（區塊 size 為 `0x184C2102` 時略過 4 bytes 並繼續解入同一成員與同一輸出 payload；區塊 size 為 `0` 或尾端殘片不足 4 bytes 時不論是否已解出區塊皆於該邊界結束成員，因此空的 `02 21 4c 18` 串流為合法的 `0`-byte `data` 成員，且尾端不足 4 bytes 的殘片與後續位元組一律交由外層 initramfs 迴圈處理——全 NUL 為 padding、含非 NUL 則以不可釘選的 `non-zero initramfs tail` 拒絕；`0 < size <= 8,421,520`（`LZ4_compressBound(8 MiB)`）的區塊直接串流寫入暫存檔而不將全部區塊留在記憶體，以單次 `lz4 -dc` 批次解壓（含總輸出 `> 8 MiB` 的合法多區塊串流亦只解碼一次；因 `lz4` CLI 的 legacy 解碼器 `programs/lz4io.c:LZ4IO_decodeLegacyStream` 本身對每個區塊強制執行 `LEGACY_BLOCKSIZE = 8 MiB` 的單區塊輸出上限，無須事前分流判斷，而 scanner 批次後的 `batch_size <= len(chunks) * 8 MiB`（`LEGACY_LZ4_BLOCK_MAX_OUTPUT`）檢查僅為縱深防禦的合理性上限），批次失敗時以 `O(log N)` 二分法定位第一個失敗區塊；已解出至少一個區塊後緊接的零間隔下一成員：標頭換算的 4-byte little-endian size `> 8,421,520` 的 `cpio-newc`、`xz`、`zstd`、現代 `lz4` frame `0x184D2204` 與 `FLG != 0` 的 `gzip` 會界定並作為下一成員掃描，而 `FLG == 0` 的零間隔 `gzip`（標頭 `1f 8b 08 00` 換算 size 為 `559,903 <= 8,421,520`）在後續位元組 `>= 559,903` B 時依核心規則當作 LZ4 區塊解碼並於失敗時以不可釘選的硬 `ScanError` 拒絕，僅在宣告的 `559,903` B 區塊被截斷時作為相對核心 in-memory 越界讀取殘留的保守 fail-closed 超集改當作 `gzip` 成員掃描；其餘非 magic 且 `> 8,421,520` 的區塊 size 會先掃描目前已解出輸出再以不可釘選的硬 `ScanError` 拒絕，已產生輸出後的截斷或損毀區塊同樣先掃描已解出區塊再以不可釘選的硬 `ScanError` 停止，且產出 `0` bytes 的失敗 legacy LZ4 後面只要跟隨任何非 NUL 位元組亦一律為不可釘選的硬 `ScanError`）；`zstd`、現代 LZ4 frame（`0x184D2204`）與 legacy LZ4 子行程皆以串流管道讀取輸出（以背景執行緒同步排空 `stderr` 並保留固定上限尾端，逾時依輸入大小設為 `30s + 每 4 MiB 1s`，且計時器持續涵蓋 `stdout`、`stderr` 收斂與 `wait()` 直到子行程結束並被回收，同時避免子行程剛好於期限退出時的誤判），當 `zstd` 或現代 LZ4 frame 失敗時由 `_write_terminated_block_prefix` 將完整非末區塊前綴封裝為帶結束標記的合法 frame（一律依帶結束標記前綴的回傳碼 `returncode == 0` 二分搜尋第一個失敗區塊，不依賴 `stderr` 文字；對 `zstd`，`_write_terminated_block_prefix` 會清除 `Single_Segment_flag`、`Frame_Content_Size` 與 content checksum 旗標，且僅在原 frame 為 single-segment 時將視窗設為 `128 MiB` 的 `0x88`（其他 frame 保留原 `Window_Descriptor`），因此 `>128 MiB` 的 single-segment frame 若後段區塊引用超過 `128 MiB` 的歷史時可能少救回部分區塊，屬已知殘留，且該失敗 frame 仍維持不可釘選的硬 `ScanError`）並同時檢查含第一個失敗區塊的原始前綴以保留最長解碼輸出（`prefix_out > destination`），使第一個失敗區塊之前所有完整區塊的輸出皆被掃描並回報其中的禁止指令命中；於直接的 initramfs 成員脈絡（根節點為 `vendor_ramdisk_entry`、`boot_ramdisk`，或最近一層檔案節點之葉節點檔名經 `_is_initramfs_filename` 先將葉名轉成小寫，再剝除壓縮副檔名 `.gz`／`.gzip`／`.xz`／`.zst`／`.zstd`／`.lz4`、`.cpio` 與 `.img` 後符合 `INITRAMFS_FILE_PREFIXES` 的 `ramdisk*`／`initrd*`／`microdroid_initrd*`／`vendor_ramdisk*` 前綴，及其 `initramfs_tail`／`sparse_payload` 子節點；不套用於整個 `boot_*`／`init_boot_*`／`vendor_boot_*`／`standalone_initrd_*` 分割區上的 `kernel`／`Image.gz`，亦不套用於從 `cpio_member` 解出的 `.ko.xz`、`.ko.gz`、韌體 `.bin.zst`／`.bin.lz4` 或 `.txt.gz` 等一般壓縮檔案；不在該檔名啟發式範圍內的已知真實名稱如 `microdroid_crashdump_initrd.img` 與 AOSP `microdroid_gki-*_initrd_*.img` 僅缺少 `Reset` 縱深防禦檢查，其每個位元組仍會被完整解開與掃描）中，任何壓縮成員若解開後未結束於 `_decoded_is_cpio_reset_sequence` 所判定的核心 `init/initramfs.c` `Reset` 狀態——即 `0` bytes（空的 `gzip`、`xz`、`zstd`、現代或 legacy `lz4` 成員）或全 NUL padding（核心僅在前一成員已結束於 `Reset` 時接受，若作為第一個成員核心會報錯停止且不再解出更多內容；scanner 在任何成員位置皆接受，屬掃描超集），或一至多個完整且 4-byte 對齊的 `cpio-newc` 項目（不論是否含 `TRAILER!!!`，項目間的 NUL padding 亦須 4-byte 對齊以符合核心 `do_reset()` 的 `"broken padding"` 檢查；帶非零 body 的 `TRAILER!!!` 項目則由 scanner 拒絕，比核心經 `SkipIt` 跳過並回到 `Reset` 更嚴格）加尾端 NUL padding——在掃描並回報任何禁止指令命中後一律以不可釘選的硬 `ScanError` 拒絕；任何壓縮 frame 或 newc 成員若在解碼失敗前已產生 `> 0` bytes 輸出，必先掃描該部分輸出（若命中未釘選的禁止指令即報出該命中），再以不可釘選的硬 `ScanError` 停止；現代 `zstd`／`lz4` frame（頂層、巢狀或 initramfs 尾端皆同）只要解碼失敗（任何原因，含 content checksum／content size 錯誤）一律為不可釘選的硬 `ScanError`，不論解碼器是否先寫出位元組：已寫出的位元組只為錯誤訊息而掃描（報出 IFMA 命中），此診斷掃描若因任何例外（含 ZIP 成員的 `LZMAError`／`zlib.error`／`EOFError`）中途停止也只寫進錯誤訊息、絕不轉為放行，任何 unsupported pin 都不適用；主機上找不到或無法啟動現代 `zstd`／`lz4` 解碼器時同樣是不可釘選的硬 `ScanError`（只有 legacy LZ4 找不到 `lz4` 時仍記為 `lz4 unavailable` 的 unsupported image，須 exact pin）；ZIP／APEX 中無法解碼的成員記為損毀（已複製出的位元組先掃描），不會中斷其後成員的掃描，最後以不能 pin 的錯誤停止建置；只有產出 `0` decoded bytes 的失敗 `gzip`／`xz` frame 與失敗呼叫未寫出任何位元組的 legacy LZ4 chunk 才能走 `ImageOpenError`（legacy LZ4 失敗呼叫若已寫出位元組，這些位元組會先被掃描，再以不可釘選的硬 `ScanError` 停止） unsupported 路徑。kernel `decompress_method()` 的 2-byte 壓縮 magic（bzip2 `42 5a`、LZMA-alone `5d 00`、LZO `89 4c`、舊版 gzip `1f 9e`）以及 Android boot `ANDROID!` 與 vendor boot `VNDRBOOT` 套用於所有檔案與 ZIP member（扣除前導 NUL 後的前 2 bytes 優先於後續容器檢查），明確列為 unsupported（拒絕與報告 reason 為 `unsupported <kind> at 0x...`），只有完整匹配 exact unsupported pin 才能繼續（pinned base 上為 0；更換底包若一般資料檔前 2 bytes 巧合命中，需加入精確的 unsupported pin）。initramfs 依 Linux kernel 語法逐一解析所有壓縮 frame／newc archive：4-byte 對齊只套用於 raw `cpio-newc`（完整項目序列即使省略 `TRAILER!!!`，只要在 EOF、NUL padding 或下一壓縮成員前結束即視為合法），任意個前導 NUL 之後的支援壓縮 frame 都會開啟走訪並在 `container_chain` 記錄 `initramfs_tail`（`offset:0x...`）；邊界規則為：(1) `position == 0`、`saw_member` 之後，或 `position > 0` 但無效 frame 標頭後跟隨任何非 NUL 位元組時無法界定 frame 長度（`length is None`）的截斷／損毀串流、(2) 已解出任一真實 member（`saw_member`）後的任何損毀 frame、(3) 失敗 frame 之後跟隨非有效後續 initramfs member 的非 NUL bytes（或 `0`-output 失敗 legacy LZ4 後跟隨任何非 NUL byte）、(4) 損毀的 `cpio-newc`（於 `position == 0` 或 4-byte 對齊 NUL 之後）或解開後未結束於 `cpio-newc` `Reset` 狀態的 initramfs 壓縮成員，以及 (5) 已產出 `> 0` decoded bytes 才失敗的 frame，以及任何解碼失敗的現代 `zstd`／`lz4` frame，一律為不可釘選的硬 `ScanError`；唯有在 `not saw_member`、產出 `0` decoded bytes 且失敗 frame 不是現代 `zstd`／`lz4`（即 `gzip`、`xz` 或 legacy LZ4）時，才可進入 `ImageOpenError` unsupported 路徑：(a) `position == 0` 且結構長度可界定（`length is not None`）的 `0`-output 失敗非 legacy frame（或 `position > 0` 前導 NUL 後結構長度可界定且後面緊接有效 initramfs member 的 `0`-output 失敗首 frame）以該 frame 自身的 SHA-256 與 `container_chain`（`position > 0` 時含 `initramfs_tail` `offset:0x...`）做 frame 級別精確釘選，其後的有效 member 仍繼續解碼掃描（若後面是非有效 member 的非 NUL bytes 則仍以不可釘選的 `ScanError` 拒絕）；(b) `position > 0`、`not saw_member`、產出 `0` decoded bytes，且結構長度可界定的失敗 frame 直達 EOF／其後全為 NUL，或無法界定長度（`length is None`）的失敗 frame 在其標頭之後全為 NUL（`_stream_tail_is_all_nul`）時，則與六種 unsupported magic 一併以完整外層物件 SHA-256 與 `container_chain` 精確釘選（reason 記錄類型與真實 offset `0x...`）。尾端 `#BOOTCONFIG\n` 僅在最外層物件上剝除（絕不套用於壓縮成員解開後的內部 payload），且必須通過 size、checksum 與完整 ASCII 文字／尾端 NUL padding 檢查，偽造 footer 不能隱藏 binary archive。沒有已知 container magic 且沒有 image 副檔名的一般檔案視為資料。ELF 的每個 executable `PF_X PT_LOAD` 以 x86_64 kernel 與 bionic 的 4096-byte page 粒度掃描：起點為 `floor(p_offset / 4096) * 4096`，exclusive 終點為 `ceil((p_offset + p_filesz) / 4096) * 4096`，再限制在檔案大小內；頁尾被 EOF 截斷時，為邊界視窗在 EOF 後合成最多 4 個零位元組（不越界讀檔、不延伸到下一頁未映射頁），page-rounded 範圍相接時保留跨界掃描，已在 page 邊界的終點不會多掃下一頁，並拒絕 page-rounded VA 重疊且 file delta 不同的兩個 `PF_X` mapping，以及 `p_filesz == 0` 且 `p_offset` 未對齊 4096 的 `PF_X PT_LOAD`（pinned base 普查三者皆為 0）。對 bionic `_extend_load_segment_vma` 邊界的唯讀超集普查（不篩選 `p_memsz == p_filesz`，將檔案映射 RX 範圍延伸到 `min(max(p_align, 4096), 65536)` 與下一段頁／檔案起點、保留延伸範圍在未對齊 EOF 處被截斷時新末頁的 1..4 bytes 合成零位元組視窗，並掃描延伸後與下一段不同 file delta 的 RX 映射相接的新 VA seam）顯示，底包有 3,478 個 `p_align > 4096` 的 x86 ELF（3,467 個頂層 `x86_elf`、11 個內嵌）與 1,537 個帶 `.note.android.pad_segment` 的 x86 ELF（1,532 個頂層、5 個內嵌；兩者兼具共 1,535 個：1,531 個頂層、4 個內嵌）；延伸檔案範圍（含未對齊 EOF 合成零位元組視窗）與新 VA seam（0 對 seam）在全部 1,535 個（及全部 4,301 個頂層 `x86_elf`）皆為 0 個新 raw IFMA candidate，production scanner 以固定 4096-byte page 模型為邊界。一次性 K4 稽核在 14 個 parent objects 驗證 62 個 x86 ELF（40 個來自 F7 的 `x86_elf` parents、10 個是以 `bsdtar` 從 F7 分類為 data 的單一 `cidata.iso` 列出的實際 ELF、12 個來自 container parents；兩個 `desktop_trusty_*signed.elf` 各含 15 個），另計數並列出 28 個被拒絕的非零 offset header（26 個位於三個 `vendor_a` `apex_payload.img` 內、因 `section_runs_past_eof` 拒絕，其實際檔案已由容器解開掃描；2 個為 `classes.dex` 與 `adservices` payload 內的 bad-ident 巧合）；全部 62 個套用同一 4096-byte page 規則皆為 0 個 IFMA candidate、0 error。此 K4 與全量稽核結果僅適用於 SHA-256 為 `1a12506eade16e6025b83659b717b7d6ee830e9d1b6447248afcb48cc365370c` 的底包（搭配 builder 的 `lapis CL3B.260622.271.R1`／build `16452207` fingerprint 與 vendor／system／system_ext EROFS UUID 閘門）；Trusty TA 與 `cidata.iso` guest userland 僅由一次性稽核涵蓋（每次 build 只掃外層 ELF 的 `PF_X`），nonzero-offset embedded-ELF carving 與 ISO traversal 都不進 production scanner（完整 wrapper materialize 203,559 個 ZIP members、耗時 624.94 秒，約為 F7 321.491 秒的 1.94 倍），更換底包時必須重跑 K4 與唯讀全映像稽核。每次 build 的掃描涵蓋重建的 `vendor_a`／`system_a`／`system_ext_a` 與 APEX；另一次唯讀全映像稽核核對 pinned base 的 55 個候選身分（原 46 個加上 9 個 host-kernel 放置身分），但這不能解讀成每次 build 都掃描完整 GPT。靜態／離線證據不等於 runtime 或實機證明；`vm.supported=1` 仍容許非 protected Microdroid 路徑，guest formatter 若到達保留指令仍可能 SIGILL。keep-data 的 APEX 身分連續性、真實 off-mode 完整映像 A/B，以及跨建置逐位元組可重現性仍未完成實機或雙建置證明。精確分類以目前 manifest 為準。
  可 pin 白名單（v0.3.0）：`unsupported_allowlist` 的精確 pin 只能放行完全沒有解出任何位元組的內容：以 magic 辨識的未知或不支援映像（例如 AVB0 vbmeta）、超過巢狀深度、缺少 erofs／sparse／ext4 工具或被該工具拒絕且沒有寫出任何輸出的映像、前導 NUL 之後、其餘全是 NUL 的 0 輸出 gzip／xz frame、依 0 輸出規則的 legacy LZ4、central directory 無法解析的 ZIP，以及 payload 從未解碼的 bzImage（沒有 HdrS、payload 起點在檔外，或 payload 範圍超出檔尾而檔內剩餘位元組解不出任何內容（解得出就先掃描再以不能 pin 的錯誤停止）、payload magic 認不得；另有主機上沒有對應 zstd／LZ4 frame 解碼器的情況）。永遠不能 pin 的格式（LZO、legacy LZ4）優先於「payload 範圍超出檔尾且解不出任何內容」這條：這兩種格式即使超出檔尾而解不出內容，也停止建置、不能 pin。legacy LZ4 的 bzImage payload 比照 Linux `unlz4()`（中段遇到 magic 繼續同一串流、0-size 區塊或剩不到 4 bytes 正常結束），任何失敗都先掃描已解出的部分再停止建置，永遠不能 pin；LZO kernel（`CONFIG_KERNEL_LZO`）以 `lzop -dc` 解碼後掃描，沒有或無法使用 `lzop` 時停止建置，不能 pin；bzImage 解碼器有逾時（30 秒加每 4 MiB 1 秒）與 512 MiB 輸出上限，超過時先掃描已產生的部分再停止建置。bzImage payload 只要解出過位元組，之後才在 checksum／CRC／尾端資料失敗，也一樣先掃描再以不能 pin 的錯誤停止建置；gzip 比照 kernel 的 `__gunzip`，CRC32／ISIZE 不符不算錯誤，照常解碼並掃描。內容只要曾被解出或部分解出（例如 bzImage 解出後尾端有多餘資料、`.ko.gz`／`.ko.xz`／`.ko.zst` 解出的不是 ELF、ZIP 有讀不出的成員），scanner 會先掃描已解出的位元組（IFMA 命中優先回報），再以不能 pin 的錯誤停止建置；其他非預期的例外一律轉成停止建置的錯誤，不會讓 scanner 崩潰。

## `amd-hx370`（實驗性，從未在實機上跑過）

- 這是 AMD 機型的**實驗性** profile，**從未在真實硬體上執行過**。
- 提供兩個開機項目：
  - **預設「safe」**：不載入 `amdgpu`。
  - **「native amdgpu (experimental)」**：載入原生 `amdgpu`。這個 profile 帶重建的 gralloc，含 `backend_amdgpu`（sha256 釘在 `[gralloc]`），仍從未在實機上跑過。這個項目可能黑畫面；請改選 safe。
- 建置需要自建核心（`GBOS_DEV_KERNEL_DIR`）、RADV，以及 `[gralloc]` 指定、sha256 相符的 `GBOS_MINIGBM`（`tools/minigbm/build-libminigbm-gralloc.sh`，只在 git repo），否則 `build.sh` 拒絕（見 `profiles/amd-hx370/profile.toml`）。
- 若機器啟用 **Secure Boot**：變更 Secure Boot 設定**之前**，務必先備份
  **BitLocker 復原金鑰**，否則可能無法解密。
- 已加入 AX210 韌體與 HD-audio／藍牙模組載入；從未在實機上測試。

## 回報新機型

要讓新的機器被納入支援，請在目標電腦上以任何 Linux 隨身碟執行唯讀探測：

```
install/gbos-probe.sh --out gbos-probe
```

它只讀 `/sys` 與 `/proc`，結果在 `gbos-probe/report.txt`。把報告附上
回報單（GitLab issue template）。新機型的完整適配流程見 `NEW-DEVICE.md`。
