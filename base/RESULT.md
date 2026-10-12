# 驗證結果（2026-10-12，開發機）

參考映像是 2026-10-11 手工做出的 `kano-real.img` 與 `initrd.img`（開發機上的 `work/img/`）。比對程式是 `compare-base.py`，完整輸出在 `../research/base-analysis/compare-run3.md`（原金鑰）與 `../research/base-analysis/compare-run2.md`（新金鑰）。重建出的 19 GB 映像比對完已經刪除，只留下 `out/run*/build-info.txt` 與 `SHA256SUMS`。

## 結論

| 執行 | 條件 | 整檔 SHA-256 | 結果 |
|---|---|---|---|
| run1 | 原金鑰 `work/keys/vbmeta.pem`；otatools 與 APEX 用 HTTP Range 從 ci.android.com 取回 | `1ce7e9589351aa2041d5a1dc795f8c850ae000370761528f3f700ba63e98f73c` | 與 kano-real.img 逐位元組相同 |
| run3 | 原金鑰；`--otatools`、`--keymint-apex`、`--gatekeeper-apex` 指向 googlebook-os 的本機檔（最終版程式） | `1ce7e9589351aa2041d5a1dc795f8c850ae000370761528f3f700ba63e98f73c` | 逐位元組相同；比對表 59 列全部相同 |
| run2 | 不給 `--key`，腳本用 `openssl genrsa 4096` 產生新金鑰；空快取，全部重新下載 | `749f3829652b9c6b91c4d5df5274008e1f40946e21106e8e6a49703ba8a5e33b` | 只有預期的差異，見下 |

兩份獨立 `initrd.img`（run1、run3）都是 `a7ffe3f736b1b47d62cc07a5562ef652605086c9e0f12c0be1adcff6b98fb21d`，與 `work/img/initrd.img` 相同。每次約 2 分鐘。同一個 `--out` 再跑一次，腳本會回 `already contains a finished image` 並以 1 結束。

## run3（原金鑰）逐項比對

| 項目 | 參考 | 重建 | 相同 |
|---|---|---|---|
| 檔案大小 | 19069493760 | 19069493760 | 是 |
| 主／備份 GPT 原始位元組、30 個分割區的名稱、type、GUID、範圍、屬性 | — | — | 是 |
| GPT 分割區之間的 3 段空隙 | — | — | 是 |
| `EFI-SYSTEM`（重建） | `59df36eb623255fd…` | `59df36eb623255fd…` | 是 |
| `super`（重建） | `a64bb6065332489a…` | `a64bb6065332489a…` | 是 |
| `vbmeta_a`（重簽） | `569fdb8b71ef131d…` | `569fdb8b71ef131d…` | 是 |
| 其餘 27 個 GPT 分割區（官方原樣） | 各自雜湊 | 各自雜湊 | 全部是 |

super 內的邏輯分割區（LP 中繼資料解析結果與開頭 1 MiB 中繼資料區的位元組都相同）：

| 邏輯分割區 | 大小 | SHA-256（前 16 碼） | 相同 |
|---|---|---|---|
| system_a | 1136197632 | `67034fd421037723` | 是 |
| system_b | 4403888128 | `2b99ca21e4a9d64a` | 是 |
| vendor_a（重建） | 452984832 | `d496bbfb7c931aae` | 是 |
| product_a | 1970524160 | `428ace4c7044f861` | 是 |
| system_ext_a（重建） | 209715200 | `2b748b1359714d61` | 是 |
| system_dlkm_a | 9232384 | `177d0d4782db9981` | 是 |
| vendor_dlkm_a | 18096128 | `5e0e5f7b515f1574` | 是 |
| 5 個 `_b` 空分割區 | 0 | — | 是 |

ESP 檔案（大小、FAT mtime、叢集範圍、內容都相同）：

| 路徑 | 大小 | FAT mtime | 叢集 | SHA-256（前 16 碼） |
|---|---|---|---|---|
| /EFI/BOOT/BOOTX64.EFI | 160256 | 2026-10-11 04:47:28 | 7–319 | `1c09c781f6a49280` |
| /kernel.efi | 26723328 | 2026-10-11 04:47:28 | 323–52516 | `7816c5d5b3500093` |
| /initrd.img | 26278308 | 2026-10-11 04:47:28 | 52517–103841 | `623a985526d4bc45` |
| /loader/loader.conf | 41 | 2026-10-11 07:55:02 | 103842 | `62ab8a1ebbea77bf` |
| /loader/entries/lapis.conf | 1108 | 2026-10-11 07:55:02 | 103843–103845 | `61d3c16d0ec914ed` |
| 卷標 `ESP`、4 個目錄 | — | 2026-10-11 04:47:28 | 3–6 | — |

initrd（cpio 封存與 bootconfig 分開比）：

| 檔案 | 大小 | 封存 SHA-256 | bootconfig | vbmeta.digest | 相同 |
|---|---|---|---|---|---|
| ESP `/initrd.img` | 26278308 | `676edbab6595c87a` | 400 bytes | `bc270dbe…`（官方 vbmeta） | 是 |
| 獨立 `initrd.img` | 26278551 | `676edbab6595c87a` | 643 bytes | `8ad0c62e…`（自簽 vbmeta） | 是，bootconfig 文字完全相同 |

vbmeta_a：6720 bytes，SHA256_RSA4096（2），rollback index 1339，release `avbtool 1.4.0`，公鑰 SHA-1 `79c9857381efd8c1e0b45c9683bf0aecac4245f9`，摘要 `8ad0c62e4da7879d55c2a1f5ede8b119e61220872510ea6550ccbe30fb9accd8`，兩邊相同。

## run2（新金鑰）的差異與原因

| 項目 | 參考 | run2 | 原因 |
|---|---|---|---|
| `vbmeta_a` 分割區 | `569fdb8b71ef131d…` | `e32dc7b629869197…` | 公鑰換成 SHA-1 `270709dbbb0b7f8ac1c0cb8dcdf1ada5c2ffae31`，簽章跟著變；結構、描述子、rollback 1339 不變 |
| vbmeta 摘要 | `8ad0c62e…` | `d0b4ddee3cc35a1f057ce5119dac0c28977aa80e04daa3879773fd4bc01baf5d` | 同上 |
| 獨立 initrd 的 bootconfig | `androidboot.vbmeta.digest=8ad0c62e…` | `androidboot.vbmeta.digest=d0b4ddee…` | 只有這一行不同；cpio 封存相同 |
| 整檔 SHA-256 | `1ce7e958…` | `749f3829…` | 由上面兩項造成 |

其他 29 個 GPT 分割區、GPT 本身、super 與其中每個邏輯分割區、ESP 全部相同。

`avbtool verify_image` 對 run1 與 run2 都通過：vbmeta 的 SHA256_RSA4096 簽章用內嵌公鑰驗過；boot、init_boot、pvmfw、vendor_boot 的 hash，以及 product、system、system_dlkm、system_ext、vendor、vendor_dlkm 的 hashtree，都與映像裡的分割區內容相符（`../research/base-analysis/avbverify.sh`）。

## 沒有解掉的差異

沒有。以原金鑰重建時，`kano-real.img` 與 `initrd.img` 都逐位元組相同。

## 要注意的事（不是重建差異）

- kano-real.img 本身的 ESP `/initrd.img` 帶的是官方 vbmeta 摘要 `bc270dbe…`，與自簽的 `vbmeta_a`（`8ad0c62e…`）不符。重建照原樣保留。從 ESP 直接開機會在 first stage 因摘要不符而停；要用獨立 `initrd.img`，或照 `kano-safe2` 的做法換掉 ESP initrd 的 bootconfig。
- 換一份主機的 systemd-boot（不是 Arch `systemd 261.2-1`），ESP 就會不同；mkfs.fat 4.2、mtools 4.0.49、otatools build 16373615 以外的版本也可能改變位元組。腳本會警告；中間產物不符時停止，加 `--allow-drift` 才會繼續。
