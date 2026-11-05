# 疑難排解（Googlebook Anywhere）

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
  - **v0.2.1 起把 `xe.force_probe` 擴充成 12 個 iGPU PCI ID**（涵蓋 ADL-P/U 與 RPL-U），預期能修好這個問題，但各 SKU 仍標「未實機測試」。當時的 v0.3.0 也未完成這些 SKU 的實測；後續版本的證據見 [v0.3.1 驗收紀錄](VERIFICATION-v0.3.1.md)。
  - 想知道自己的機器屬於哪個 SKU／iGPU，見 `COMPATIBILITY.md` 的 KANO 家族表。

### 第一次開機很慢，或跳出「沒有回應」

- **症狀**：從隨身碟第一次開機要等好幾分鐘，畫面可能跳出
  「沒有回應 / Not responding」對話框。
- **可能原因**：隨身碟讀取速度慢；首次開機要做的初始化比較多。
- **怎麼辦**：
  - 慢速隨身碟首次開機**要幾分鐘是正常的**，請耐心等。
  - 跳出「沒有回應」時**選「等待」（Wait）**，不要選結束。
  - 換一支較快的 USB 3.0 隨身碟會明顯改善。

### 首次設定卡在系統更新，連上網路後又回到同一步

- **症狀**：第一次開機的設定精靈要求連 Wi-Fi。連上之後它找到系統更新、開始下載，然後回到同一步，可以一直重複。v0.2.3 的 KANO 映像有人這樣回報（issue #6，ASUS CX34）。
- **可能原因**：Google 的 OTA 簽章檢查會過（映像沒有更換 `/system/etc/security/otacerts.zip`），但推測（未實機驗證）這個映像完成不了那次更新：增量包會核對實際讀到的來源區塊，而 vendor 一定被改過，啟用 IFMA 修補時 system 與 system_ext 也改過；完整包寫完後要經由被封鎖的 boot-control／crossystem 切換開機 slot，而開機選單不會跟著 Google boot-control 的 slot 切換。伺服器還是提供同一份更新，精靈就再做一次。Google OTA 和這個專案自己的 `install/gbos-update.py` 會爭用 inactive slot。
- **怎麼辦**：
  - **v0.3.0 加入停止服務的設定，已有一次實機停止狀態讀回。** vendor 帶 `etc/init/gbos-no-ota.rc`：在 `update_engine.rc` 啟用 `update_engine` 的同一個觸發條件（`ro.boot.slot_suffix=*`）之後，由 init 執行 `stop update_engine`。QEMU 已驗證 vendor action 有執行、`update_engine` 從未啟動；2026-10-29 KANO 保留資料更新後，在 uptime 65 分鐘時讀到 `stopped`，僅證明當時的服務狀態，見 [v0.3.0 驗收紀錄](VERIFICATION-v0.3.0.md)。
  - v0.3.0 映像已帶 `gbos-no-ota.rc`，但不能保證實機不再卡住。v0.2.3（含）以前：預設映像沒有 adb（只有建置時另外給公鑰才會開），不靠 adb 沒有已知解法。
  - 還沒有在實機上驗證（未實機測試）：`update_engine` 在裝置上完全不會執行、精靈之後會走完、GMS 怎麼反應。QEMU 沒有網路，也看不到 Google 實際提供更新的那一段。

### 開機就失敗，或開機後某些程式閃退（CPU 太舊）

- **症狀**：選單後幾乎立刻失敗、kernel panic，或開機後某些 App 一開就閃退。
- **可能原因**：**CPU 太舊**。lapis 的許多系統程式與函式庫直接編進 AVX2、GFNI、
  AVX-VNNI、SHA-NI 等指令而不先做執行期 CPU 分派；**只要程式實際執行到 CPU 不支援的那條指令**，就會觸發 `#UD`，程式收到 SIGILL（例如第一階段 `/init` 含有 SHA-NI 指令，若在沒有 SHA-NI 的 CPU 上走到該路徑，PID 1 收到 SIGILL 時核心就會 panic）。
- **怎麼辦**：
  - 只在 Intel 第 12 代（KANO）實機驗證過。第 11 代及更早的 Intel 預期會失敗（沒有 AVX-VNNI；第 6～10 代的 Skylake
    系列及更早的 Core 也沒有 SHA-NI，第三方 gbOS86 的紀錄顯示第一次開機時 `/init` 就會執行到）。
  - AMD Zen 4 及更早預期會失敗（沒有 VEX 編碼的 AVX-VNNI）；Zen 5（HX 370）是目標，但還沒在實機上跑過。
  - 啟用 `patch_ifma` 的映像會精確修補 17 個 host 目標（原 16 個加上 `system_ext_a/bin/trusty_security_vm_launcher`）。若 KANO 或 Zen 5 上仍有個別程式 SIGILL，請附 logcat、程式名稱與 build-info；靜態掃描不是 runtime／實機證明，非 protected Microdroid 仍可能走到 guest 內保留的 `microdroid_manager` 指令。
  - 若建置因 IFMA scanner 停止，先看報告中的 partition、typed container chain、SHA-256、offset 與 encoding。candidate pin 缺少或改變、額外 raw VEX candidate、已辨識但無法開啟或過深的 nested container，以及串接 initramfs 中無法解釋的非零位元組都會拒絕；反組譯判定為另一條指令的一部分也不會把 candidate 從報告移除。scanner 以已知 magic 與 image 副檔名的聯集（含解壓 payload 依每層解碼類型先剝除尾端壓縮標記與對應 `.gz`／`.gzip`、`.xz`、`.zst`／`.zstd`、`.lz4`，若外層副檔名與實際壓縮 magic 不符則退回剝除單一通用壓縮副檔名後的內層副檔名）辨識 xz、gzip、zstd、lz4、zip、newc、EROFS、ext4、Android sparse、AVB0 與 bzImage，命中任一入口都必須完整開啟；為與 Linux kernel `lib/decompress_inflate.c:__gunzip` 一致，gzip 依 header flags 與 raw deflate 解碼（含在輸入 EOF 時 `flush()` 取出 `>64 KiB` 無 trailer 串流尾端緩衝輸出）並略過 8-byte trailer（有效 `Z_STREAM_END` 後的 CRC／ISIZE 不符或缺少／截斷 trailer 仍解碼掃描；raw deflate 損毀則為硬錯誤）；legacy LZ4（`0x184C2102`）依 Linux `lib/decompress_unlz4.c:unlz4()` 逐區塊解碼（遇到後續 `0x184C2102` magic 繼續解碼同一串流，尾端 `<4` bytes 殘片或 `0`-size 區塊——含僅有 4-byte magic 或 `magic + 00000000` 的空串流，產出 `0` bytes——不論是否已解出區塊皆於該邊界結束成員，將尾端 `<4` bytes 殘片與後續位元組交由外層 initramfs 迴圈處理，故全 NUL 為 padding、含非 NUL 則以不可釘選的 `non-zero initramfs tail` 拒絕；`<=8,421,520` B（`LZ4_compressBound(8 MiB)`）區塊直接串流寫入暫存檔並以單次 `lz4 -dc` 批次解碼——含總輸出 `> 8 MiB` 的合法多區塊串流亦只解碼一次，因 `lz4` CLI 的 legacy 解碼器 `programs/lz4io.c:LZ4IO_decodeLegacyStream` 本身對每個區塊強制執行 `LEGACY_BLOCKSIZE = 8 MiB` 的每區塊解壓上限，無須事前分流判斷，而 scanner 批次後的 `batch_size <= len(chunks) * 8 MiB`（`LEGACY_LZ4_BLOCK_MAX_OUTPUT`）檢查僅為縱深防禦的合理性上限——批次失敗時以 `O(log N)` 二分法定位第一個失敗區塊；已解出至少一個區塊後緊接的零間隔下一成員：標頭換算的 4-byte little-endian size `>8,421,520` B 的 `cpio-newc`、`xz`、`zstd`、現代 `lz4` frame `0x184D2204` 與 `FLG != 0` 的 `gzip` 會結束串流並作為下一成員掃描，而 `FLG == 0` 的零間隔 `gzip`（標頭 `1f 8b 08 00` 換算 size 為 `559,903 <= 8,421,520`）在後續位元組 `>= 559,903` B 時依核心規則當作 LZ4 區塊解碼並於失敗時以不可釘選的硬 `ScanError` 拒絕，僅在宣告的 `559,903` B 區塊被截斷時作為相對核心 in-memory 越界讀取殘留的保守 fail-closed 超集改當作 `gzip` 成員掃描；其餘 `>8,421,520` B 非下一成員標頭或截斷／損毀區塊則先掃描已解出區塊再以不可釘選的硬 `ScanError` 停止；且若 `0`-output 失敗的 legacy LZ4 後跟隨任何非 NUL 位元組亦為不可釘選的硬 `ScanError`）；`zstd`、現代 LZ4 frame（`0x184D2204`）與 legacy LZ4 子行程皆以背景排空 `stderr` 與依輸入大小設為 `30s + 每 4 MiB 1s` 的逾時上限（計時器持續涵蓋 `stdout`、`stderr` 收斂與 `wait()` 直到子行程結束並被回收，且避免子行程剛好於期限退出時的誤判）讀取輸出，並在 `zstd` 或現代 LZ4 frame 失敗時由 `_write_terminated_block_prefix` 將完整非末區塊前綴封裝為帶結束標記的合法 frame（一律依帶結束標記前綴的回傳碼 `returncode == 0` 二分搜尋第一個失敗區塊，不依賴 `stderr` 文字；對 `zstd`，`_write_terminated_block_prefix` 會清除 `Single_Segment_flag`、`Frame_Content_Size` 與 content checksum 旗標，且僅在原 frame 為 single-segment 時將視窗設為 `128 MiB` 的 `0x88`（其他 frame 保留原 `Window_Descriptor`），因此 `>128 MiB` 的 single-segment frame 若後段區塊引用超過 `128 MiB` 的歷史時可能少救回部分區塊，屬已知殘留，且該失敗 frame 仍維持不可釘選的硬 `ScanError`）並同時檢查含第一個失敗區塊的原始前綴以保留最長可解出前綴，且任何壓縮 frame 或 newc 成員若在失敗前已解出 `> 0` bytes 必先掃描該部分輸出再以不可釘選的硬 `ScanError` 停止，現代 `zstd`／`lz4` frame（頂層、巢狀或 initramfs 尾端皆同）只要解碼失敗（任何原因，含 content checksum／content size 錯誤）一律為不可釘選的硬 `ScanError`，不論解碼器是否先寫出位元組：已寫出的位元組只為錯誤訊息而掃描（報出 IFMA 命中），此診斷掃描若因任何例外（含 ZIP 成員的 `LZMAError`／`zlib.error`／`EOFError`）中途停止也只寫進錯誤訊息、絕不轉為放行，任何 unsupported pin 都不適用；主機上找不到或無法啟動現代 `zstd`／`lz4` 解碼器時同樣是不可釘選的硬 `ScanError`（只有 legacy LZ4 找不到 `lz4` 時仍記為 `lz4 unavailable` 的 unsupported image，須 exact pin）；ZIP／APEX 中無法解碼的成員記為損毀（已複製出的位元組先掃描），不會中斷其後成員的掃描，最後以不能 pin 的錯誤停止建置（只有產出 `0` decoded bytes 的失敗 `gzip`／`xz` frame 與失敗呼叫未寫出任何位元組的 legacy LZ4 chunk 才能走 `ImageOpenError`（legacy LZ4 失敗呼叫若已寫出位元組，這些位元組會先被掃描，再以不可釘選的硬 `ScanError` 停止））；此外，在直接的 initramfs 成員脈絡（根節點為 `vendor_ramdisk_entry`、`boot_ramdisk`，或最近一層檔案節點之葉節點檔名經 `_is_initramfs_filename` 先將葉名轉成小寫，再剝除壓縮副檔名 `.gz`／`.gzip`／`.xz`／`.zst`／`.zstd`／`.lz4`、`.cpio` 與 `.img` 後符合 `INITRAMFS_FILE_PREFIXES` 的 `ramdisk*`／`initrd*`／`microdroid_initrd*`／`vendor_ramdisk*` 前綴，及其 `initramfs_tail`／`sparse_payload` 子節點；不套用於整個 `boot_*`／`init_boot_*`／`vendor_boot_*`／`standalone_initrd_*` 分割區上的 `kernel`／`Image.gz`，亦不套用於從 `cpio_member` 解出的 `.ko.xz`、`.ko.gz`、韌體 `.bin.zst`／`.bin.lz4` 或 `.txt.gz` 等一般壓縮檔案；不在該檔名啟發式範圍內的已知真實名稱如 `microdroid_crashdump_initrd.img` 與 AOSP `microdroid_gki-*_initrd_*.img` 僅缺少 `Reset` 縱深防禦檢查，其每個位元組仍會被完整解開與掃描）中，任何壓縮成員解壓後的輸出若未結束於 `_decoded_is_cpio_reset_sequence` 所判定的核心 `init/initramfs.c:unpack_to_rootfs()` `Reset` 狀態——即 `0` bytes（空的 `gzip`、`xz`、`zstd`、現代或 legacy `lz4` 成員）或全 NUL padding（核心僅在前一成員已結束於 `Reset` 時接受，若作為第一個成員核心會報錯停止且不再解出更多內容；scanner 在任何成員位置皆接受，屬掃描超集），或一至多個完整且 4-byte 對齊的 `cpio-newc` 項目（不論是否含 `TRAILER!!!`，項目間的 NUL padding 亦須 4-byte 對齊以符合核心 `do_reset()` 的 `"broken padding"` 檢查；帶非零 body 的 `TRAILER!!!` 項目則由 scanner 拒絕，比核心經 `SkipIt` 跳過並回到 `Reset` 更嚴格）加尾端 NUL padding——亦會在掃描已解出輸出後以不可釘選的硬 `ScanError` 拒絕（尾端 `#BOOTCONFIG\n` 僅在最外層物件上剝除，絕不套用於壓縮成員解開後的內部 payload）。kernel `decompress_method()` 的 2-byte 壓縮 magic（bzip2 `42 5a`、LZMA-alone `5d 00`、LZO `89 4c`、舊版 gzip `1f 9e`）以及 Android boot `ANDROID!` 與 vendor boot `VNDRBOOT` 套用於所有檔案與 ZIP member（扣除前導 NUL 後的前 2 bytes 優先於後續容器檢查），明確列為 unsupported（拒絕與報告 reason 為 `unsupported <kind> at 0x...`），只有完整匹配 exact unsupported pin（目前唯一是 AVB0 `microdroid_vbmeta.img`，pinned base 上 2-byte magic 為 0）才能繼續；若更換底包後一般資料檔前 2 bytes 巧合命中，請加入精確的 unsupported pin。initramfs 依 Linux kernel 語法解析每個 frame／newc archive，包含頂層與解壓成員內在 4-byte 對齊 newc 前的 NUL padding（raw `cpio-newc` 完整項目序列即使省略 `TRAILER!!!`，只要在 EOF、NUL padding 或下一壓縮成員前結束即視為合法），以及任意個前導 NUL 之後的支援壓縮 frame（`container_chain` 記錄 `initramfs_tail` `offset:0x...`）；以下拒絕一律為不可釘選的硬 `ScanError`：(1) `position == 0` 或 `saw_member` 之後無法界定 frame 長度（`length is None`）的截斷／損毀串流（或 `position > 0`、`not saw_member` 但 frame 標頭後並非全為 NUL 的 `length is None` 截斷串流）、(2) 已解出任一真實 member（`saw_member`）後的任何損毀 frame、(3) 失敗 frame 後跟隨非有效後續 initramfs member 的非零尾端 bytes、(4) 損毀的 `cpio-newc`（於 `position == 0` 或 4-byte 對齊 NUL 後），以及 (5) 已解出 `> 0` bytes 才失敗的 frame（或任何解碼失敗的現代 `zstd`／`lz4` frame，或直接 initramfs 成員脈絡中解壓輸出未結束於 `cpio-newc` `Reset` 狀態的壓縮成員）；唯有在 `not saw_member`、產出 `0` decoded bytes 且失敗 frame 不是現代 `zstd`／`lz4`（即 `gzip`、`xz` 或 legacy LZ4）時，才可進入 `ImageOpenError` unsupported 路徑：(a) `position == 0` 且結構長度可界定（`length is not None`）的 `0`-output 失敗 frame（或 `position > 0` 前導 NUL 後結構長度可界定且後面緊接有效 initramfs member 的 `0`-output 失敗首 frame）以該 frame 自身的 SHA-256 與 `container_chain`（`position > 0` 時含 `initramfs_tail` `offset:0x...`）做 frame 級別精確釘選，其後的有效 member 仍繼續解碼掃描（若後面是非有效 member 的非 NUL bytes 則仍以不可釘選的 `ScanError` 拒絕）；(b) `position > 0`、`not saw_member`、產出 `0` decoded bytes 且該失敗 frame 直達 EOF 或其後全為 NUL（對 `length is None` 則為標頭後全為 NUL 直達 EOF）時，則與六種 unsupported magic 一併以完整外層物件 SHA-256 與 `container_chain` 釘選（reason 記錄類型與真實 offset `0x...`）；沒有已知 container magic 且沒有 image 副檔名的一般檔案視為資料。ELF 的每個 executable `PF_X PT_LOAD` 以 4096-byte page 粒度掃描（頁尾被 EOF 截斷時為邊界視窗合成最多 4 個零位元組，並拒絕不同 file delta 的重疊執行頁或 `p_filesz == 0` 且未頁對齊的 `p_offset`）。
  - 可 pin 白名單（v0.3.0）：`unsupported_allowlist` 的精確 pin 只能放行完全沒有解出任何位元組的內容：以 magic 辨識的未知或不支援映像（例如 AVB0 vbmeta）、超過巢狀深度、缺少 erofs／sparse／ext4 工具或被該工具拒絕且沒有寫出任何輸出的映像、前導 NUL 之後、其餘全是 NUL 的 0 輸出 gzip／xz frame、依 0 輸出規則的 legacy LZ4、central directory 無法解析的 ZIP，以及 payload 從未解碼的 bzImage（沒有 HdrS、payload 起點在檔外，或 payload 範圍超出檔尾而檔內剩餘位元組解不出任何內容（解得出就先掃描再以不能 pin 的錯誤停止）、payload magic 認不得；另有主機上沒有對應 zstd／LZ4 frame 解碼器的情況）。永遠不能 pin 的格式（LZO、legacy LZ4）優先於「payload 範圍超出檔尾且解不出任何內容」這條：這兩種格式即使超出檔尾而解不出內容，也停止建置、不能 pin。legacy LZ4 的 bzImage payload 比照 Linux `unlz4()`（中段遇到 magic 繼續同一串流、0-size 區塊或剩不到 4 bytes 正常結束），任何失敗都先掃描已解出的部分再停止建置，永遠不能 pin；LZO kernel（`CONFIG_KERNEL_LZO`）以 `lzop -dc` 解碼後掃描，沒有或無法使用 `lzop` 時停止建置，不能 pin；bzImage 解碼器有逾時（30 秒加每 4 MiB 1 秒）與 512 MiB 輸出上限，超過時先掃描已產生的部分再停止建置。bzImage payload 只要解出過位元組，之後才在 checksum／CRC／尾端資料失敗，也一樣先掃描再以不能 pin 的錯誤停止建置；gzip 比照 kernel 的 `__gunzip`，CRC32／ISIZE 不符不算錯誤，照常解碼並掃描。內容只要曾被解出或部分解出（例如 bzImage 解出後尾端有多餘資料、`.ko.gz`／`.ko.xz`／`.ko.zst` 解出的不是 ELF、ZIP 有讀不出的成員），scanner 會先掃描已解出的位元組（IFMA 命中優先回報），再以不能 pin 的錯誤停止建置；其他非預期的例外一律轉成停止建置的錯誤，不會讓 scanner 崩潰。
  - 若某個資料檔在前導 NUL 之後剛好出現 zstd（`28 b5 2f fd`）或 lz4（`04 22 4d 18`）magic，scanner 會把它當成現代 frame 解碼，失敗即不可釘選地停止建置；先用 `zstd -t`／`lz4 -t` 確認它是否真的是壓縮資料，若只是巧合，pin 無法放行，請附上報告中的 container chain 與 SHA-256 回報，由 scanner 端修正。
  - 細節見 `COMPATIBILITY.md`「CPU 最低需求」一節與 `builder/ifma/manifest.json`。

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
  - 當時的 v0.3.0 對這條路徑**未實機測試**；v0.3.1 也未新增這個 SKU 的實測證據。
  - 12 代的 KANO（ADL-P/U，`8086:51c8` → `sof-adl.ri`）在驗證機上喇叭正常。

## 休眠與喚醒

### 螢幕關著時，按鍵盤叫不醒螢幕（已知問題）

- **症狀**：螢幕已關閉時按鍵盤沒反應，螢幕不亮。
- **可能原因**：喚醒的那一下按鍵送到 Android 之前，`gbos_wakefilter.ko`
  已經把它的 `KEY_WAKEUP` 丟掉了。
- **怎麼辦**：
  - **請用電源鍵或開合上蓋**來喚醒。
  - 當時的 v0.3.0 未修正這個已知問題；v0.3.1 的完整睡醒實測仍未完成，請用電源鍵或開合上蓋。

### 螢幕會自己亮起（v0.1 的暗中喚醒）

- **症狀**：v0.1 映像中，闔上蓋子或關螢幕後幾十秒內螢幕自己亮起。
- **可能原因**：每次暗中喚醒都帶一個電源鍵的 `KEY_WAKEUP`。
- **怎麼辦**：**v0.2 起**映像帶 `gbos_wakefilter.ko`，電源鍵沒有喚醒系統時
  就丟掉它。KANO 實機上闔蓋 300 秒期間暗中喚醒 10 次、螢幕都沒亮。

v0.3.0 可選用 KANO 實驗設定 `GBOS_EC=drain`，期待改善自行亮屏：2026-10-29 在 KANO 上觀察到入睡後約 33 分鐘沒有自行亮屏、鍵盤喚醒正常；S0ix、真實電源鍵、闔蓋與更長待機仍未驗證（見 [v0.3.0 驗收紀錄](VERIFICATION-v0.3.0.md)）；休眠失敗恢復可能重設 AP 並造成資料遺失。設定與限制見 [KANO 硬體狀態](STATUS-KANO.md)。

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
  - 要裝到內建 NVMe，**請用「安裝碟」（installer stick）**：
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
  - **保留整個 `~/gbos-work/keys/`**，尤其是 AVB 的 `vbmeta.pem` 與 APEX payload／container 身分所在的 `apex-keys/`；更新時兩組都要沿用。
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
    「native amdgpu (experimental)」（載入原生 `amdgpu`）。
    兩個項目都會在 early-boot 載入同樣的五個模組：`bluetooth`、`btusb`、
    `snd_hda_codec_alc269`、`iwlmvm`、`snd_hda_intel`。safe 與 native 的差別是
    `amdgpu`，不會略過 HDA、藍牙或 `iwlmvm`。
  - 這個 profile 帶重建的 gralloc，含 `backend_amdgpu`（sha256 釘在 `[gralloc]`），safe 與 native 共用這一顆檔。native 項目仍從未在實機上跑過，可能黑畫面；請改選 safe。
  - 建置需要自建核心（`GBOS_DEV_KERNEL_DIR`）、RADV，以及 `[gralloc]` 指定、sha256 相符的 `GBOS_MINIGBM`（`tools/minigbm/build-libminigbm-gralloc.sh`，只在 git repo），否則 `build.sh` 拒絕（見 `profiles/amd-hx370/profile.toml`）。
  - 建置需要 `GBOS_DEV_KERNEL_DIR/modules/bluetooth.ko`。這個檔案不在時，
    build 會中止。這是建置前提，不是開機後補韌體就能修好的。
  - 模組有載入、ALSA card 出現、Android 音訊 HAL／路由能播放，是不同層。
    這個 profile 的 `[audio]`（`card`、`config_dir`、`sources`）仍是空的。
  - 失敗時用唯讀指令分開看：`dmesg | grep -iE 'firmware|hda|bluetooth|btusb|iwl'`、
    `lsmod`、`/proc/asound/cards`、`dumpsys media.audio_flinger`，以及 SELinux
    AVC denials。模組載入的邊界見 `SAFETY.md` 第 9 點。
  - 見 `SAFETY.md`。

## 顯示大小調到最小還是太大

設定 > 顯示 > 顯示大小的各段是從基準密度 `ro.sf.lcd_density` 按比例算的，依公開資料原廠值是 260（待實機確認），以 AOSP 預設 85% 的最小一段預期約 220。14 吋 1920x1080／1920x1200 的面板可以在機型設定的 `[display]` 加上 `density = 200`（依面板換算建議的起點；最小一段預期約 170）或 `density = 160`，重新建置後寫入。改之前如果動過顯示大小或用過 `adb shell wm density`，先在設定裡改回「預設」或執行 `adb shell wm density reset`，否則看不到新的基準值。詳見 `docs/DISPLAY-DENSITY.md`。

## 硬體功能限制

支援情況與限制依映像及機型而異，請看各項紀錄：

| 功能 | 狀況 |
|---|---|
| 自動旋轉、光感測 | 實測配置中的 Android 未列出可用感測器；不能據此判定硬體不存在 |
| 鍵盤背光調整 | v0.3.1 含背光曲線來源修正，尚未實機驗證；先前 R6 USB 候選已驗證控制端點與亮／滅，但曲線有缺陷。見 [KANO 狀態](STATUS-KANO.md) |
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
