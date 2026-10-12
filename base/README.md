# KANO 基礎映像重建管線（`build-base.sh`）

> **中間產物，只給 `builder/build.sh` 用。絕對不要把 `kano-real.img` 寫到隨身碟，也不要在任何實機上開機。**
> 它的 ESP 開機選項還是原廠參數（`spi-intel.writeable=1`，沒有 EC／TPM／SPI 封鎖，選單編輯器開著），vendor 裡
> fwupd、flashrom、futility、ectool、`ap-ec-fw.zip` 都還在（builder 會移除它們並重寫開機選項）。system 的 `gscutil.rc`
> 不一樣：最終映像也照原樣保留它，它要做的 GSC 韌體更新只靠 TPM 驅動被封鎖而失敗（見 `docs/SAFETY.md`「沒有涵蓋的範圍」）。
> （它目前本來就開不起來：ESP initrd 的 vbmeta digest 是舊的。）

這個目錄把 2026-10-11 手工做出的基礎映像 `kano-real.img`
（19069493760 bytes，SHA-256 `1ce7e9589351aa2041d5a1dc795f8c850ae000370761528f3f700ba63e98f73c`）
與獨立的 `work/img/initrd.img`（SHA-256 `a7ffe3f736b1b47d62cc07a5562ef652605086c9e0f12c0be1adcff6b98fb21d`）
寫成一支可重跑的腳本。輸入是 Google 官方的 lapis recovery zip，加上公開 Cuttlefish CI 的兩個 APEX 與 AOSP otatools。

用原本那把 AVB 金鑰時，輸出與 `kano-real.img` 逐位元組相同。換一把金鑰時，只有 `vbmeta_a`、獨立 initrd 的 `androidboot.vbmeta.digest` 那一行，以及整檔雜湊會變。驗證結果在 `RESULT.md`。

證據來源的寫法：`REPORT.md:行號` 指研究報告 `research/REPORT.md`（只在 private 研究 repo 裡；公開版沒有 `research/`）。「產物」指 2026-10-12 對 `work/img/` 與 `downloads/` 既有檔案做的量測，量測程式在 `../research/base-analysis/`。

## 檔案

| 檔案 | 用途 |
|---|---|
| `build-base.sh` | 主程式。參數、釘選的雜湊、步驟順序都在這裡 |
| `gbos_base.py` | 輔助程式（單檔，只用 Python 標準函式庫）：GPT、Android boot v4、LP（super）、EROFS、FAT32、bootconfig、HTTP Range 取 zip 成員 |
| `compare-base.py` | 驗證程式：比對兩個映像的整檔、GPT、各分割區、super 內各邏輯分割區、ESP 檔案、initrd 與 vbmeta，輸出 Markdown 表 |
| `RESULT.md` | 2026-10-12 的驗證表 |

## 用法

```sh
# 用原本的金鑰重建（應得到 1ce7e958…）
base/build-base.sh \
  --zip lapis-user-16452207-recovery.zip \
  --out /path/to/out \
  --key /path/to/original-vbmeta.pem

# 別人從零開始：只給 zip，金鑰自動產生，otatools 與 APEX 自動從 ci.android.com 取
base/build-base.sh --zip recovery.zip --out /path/to/out

# 比對
python3 -I -B base/compare-base.py REF.img out/kano-real.img REF-initrd.img out/initrd.img --md cmp.md
```

全程約 2 分鐘（btrfs 上）。`--out` 尖峰用量約 45 GB，成功後只留 `kano-real.img`、`initrd.img`、`SHA256SUMS`、`build-info.txt`、`BUILD-DONE`。長時間執行請用 `setsid nohup … &` 背景跑。

| 參數 | 說明 |
|---|---|
| `--zip` | 官方 zip，SHA-256 必須是 `5bc64936…f247`，不符就停止。這條管線只對 lapis build 16452207 驗證過 |
| `--out` | 輸出目錄，必須是新的、空的，或之前 build-base.sh 的輸出（有 `.gbos-build-base` 標記）。已有 `kano-real.img`、`initrd.img` 或 `BUILD-DONE` 就拒絕；有標記時，未完成的舊 `work/` 與 `.partial` 會先清掉 |
| `--key` | RSA-4096 PEM。沒給就用 `openssl genrsa 4096` 在 `--out/keys/vbmeta.pem` 產生（umask 077，目錄 0700）。程式只印公鑰 SHA-1，不印私鑰 |
| `--otatools` | 本機 otatools 目錄（需 `bin/` 與 `lib64/`）。沒給就用 HTTP Range 從 `otatools.zip` 只取 15 個成員 |
| `--keymint-apex`、`--gatekeeper-apex` | 本機 APEX。沒給就用 HTTP Range 從 target_files zip 取 |
| `--systemd-boot` | 必須是釘選的檔案（SHA-256 `1c09c781…`）。沒給時：主機的那份剛好相同就用它，否則從 Arch 套件封存站下載 `systemd-261.2-1`（套件 SHA-256 `88ed13de…`）取出。不符一律停止 |
| `--cache` | 下載快取，預設 `--out/cache` |
| `--allow-drift` | 中間產物雜湊不符時改成警告，不停止（工具版本不同時用）。下載的輸入、otatools 與 systemd-boot 一律不放寬 |
| `--keep-work` | 成功後保留 `--out/work`（約 25 GB） |

安全邊界：拒絕以 root 執行；不需要 sudo；不 mount；所有寫入都是 `--out` 下的一般檔案，`gbos_base.py` 寫檔前會檢查目標不是區塊裝置。

## 輸入與來源

| 輸入 | 來源 | 大小 | SHA-256 | 證據 |
|---|---|---|---|---|
| 官方 recovery zip | `https://dl.google.com/device/recovery/lapis-user/16452207/recovery.zip`（索引 `https://dl.google.com/dl/edgedl/device/recovery/production_recovery.json`） | 7728873477 | `5bc64936c11d9eb268da03063a443b8fd0bbc0f84d266f9a014231653939f247` | REPORT.md:48；本機重算 |
| zip 內 `android-desktop_signed_recovery_image.bin` | 同上，唯一成員，deflate | 19069493760 | `b10f519ad9b834b316bc3c9d7c33fc45438a8d1c69254c124b7c3cdf72d288c7` | 2026-10-12 對 `downloads/` 的副本重算 |
| `com.android.hardware.keymint.rust_nonsecure.apex` | `https://ci.android.com/builds/submitted/16373615/aosp_cf_x86_64_only_phone-userdebug/latest/raw/aosp_cf_x86_64_only_phone-target_files-16373615.zip` 的成員 `VENDOR/apex/com.android.hardware.keymint.rust_nonsecure.apex` | 7667712 | `01387704ab1ee39d4d70aab09eaf865e83e896ad4f79ed161707ce78da7ef697` | REPORT.md:339、374；2026-10-12 用本腳本實際取回並比對 |
| `com.android.hardware.gatekeeper.nonsecure.apex` | 同一 target_files 的 `VENDOR/apex/com.android.hardware.gatekeeper.nonsecure.apex` | 4911104 | `17ed9be4744ff86e9151af3fc82aff508a5e4bd54e9b5e679d45ecb499b930b3` | 同上 |
| AOSP otatools | `https://ci.android.com/builds/submitted/16373615/aosp_cf_x86_64_only_phone-userdebug/latest/raw/otatools.zip`（531408470 bytes，md5 `b5b9c4aed25fb30bd9f9c6d6083133d3`） | 只取 15 個成員 | 每個成員的 SHA-256 釘在 `build-base.sh` 的 `OTA_FILES` | 先前 grok session 的下載紀錄；2026-10-12 用 HTTP Range 重新取回，15 個檔與 `work/tools/otatools` 逐檔 SHA-256 相同 |
| systemd-boot | Arch Linux `systemd 261.2-1` 的 `systemd-bootx64.efi`（`https://archive.archlinux.org/packages/s/systemd/systemd-261.2-1-x86_64.pkg.tar.zst`，套件 SHA-256 `88ed13de43e53e6b8ba6039e7e58c1bc2a15c47234b05f48e68319c93b661cd7`） | 160256 | `1c09c781f6a492807d1f96e4e064c0638caf553a1319e4de8315a40c2d224d9c` | REPORT.md:185；與 kano-real.img ESP 內 `BOOTX64.EFI` 相同。不是 Google 產物；主機沒有同一份時由腳本下載 |
| AVB 金鑰 | `work/keys/vbmeta.pem`（自產 RSA-4096，公鑰 SHA-1 `79c9857381efd8c1e0b45c9683bf0aecac4245f9`） | — | 不列 | REPORT.md:378 |

ci.android.com 的 `raw/` 網址會 307 轉到有時效的 `storage.googleapis.com` 簽名網址。`gbos_base.py fetch-zip` 每次執行時重新解析，只讀 zip 尾端的中央目錄與需要的成員，不下載整包（target_files 2324666307 bytes、otatools 531408470 bytes）。每個成員都核對 CRC32、大小與 SHA-256。

## 主機需求與版本釘選

位元組會隨工具版本改變。腳本開頭檢查版本，不符時警告；中間產物雜湊不符則停止（除非 `--allow-drift`）。

| 工具 | 參考版本 | 影響 |
|---|---|---|
| `mkfs.fat`（dosfstools） | 4.2 | ESP 開機磁區與卷標 |
| mtools（`mmd`、`mcopy`） | 4.0.49 | ESP 目錄項與叢集配置 |
| otatools `avbtool` | 1.4.0 | hashtree、FEC、vbmeta 的 release string |
| otatools `mkfs.erofs` | 1.8.3（erofs-utils） | vendor／system_ext 的 EROFS 位元組 |
| otatools `lpmake`、`fsck.erofs`、`fec` | build 16373615 | super 與 FEC |
| `lz4` | 任何能解 legacy frame 的版本 | 只用來解壓 ramdisk，輸出有雜湊檢查 |
| `python3`、`openssl`、`sha256sum`、`truncate` | 一般 | — |

時間戳：FAT 目錄項的時間來自 `SOURCE_DATE_EPOCH`。腳本把 `TZ` 固定成 `<+08>-8`，因為 mtools 用 `localtime()` 換算，而 mkfs.fat 4.2 用 `gmtime()`，所以 mkfs.fat 另外拿「同一個牆上時間當成 UTC」的 epoch。mtools 的設定檔選項（`MTOOLS_LOWER_CASE` 等）也用環境變數固定成預設值。

## 步驟

每一步的輸出都對照 kano-real 的已知雜湊（`build-base.sh` 的 `EXPECT` 表）。

### 1. 驗證 zip，解出 recovery 映像

- 輸入：官方 zip。
- 做法：`sha256sum` 核對 zip，再用 Python `zipfile` 串流解出 `android-desktop_signed_recovery_image.bin`，邊寫邊算 SHA-256。這個檔直接當成輸出映像的底（`kano-real.img.partial`），之後只覆寫三個 GPT 分割區。
- 原因：kano-real.img 是官方映像的複本，只改了 `EFI-SYSTEM`、`super`、`vbmeta_a`。
- 證據：產物比對。kano-real.img 與官方 .bin 的 30 個 GPT 分割區中，只有這三個 SHA-256 不同；GPT 標頭、分割區 GUID、範圍、分割區之間的空隙與備份 GPT 全部相同（`../research/base-analysis/{orig,real}.json`）。REPORT.md:181（E）、444（M）也寫成複本。

### 2. 讀 GPT 與開機產物

- 輸入：步驟 1 的映像。
- 做法：`gbos_base.py layout` 讀 GPT。`boot-extract` 解析：
  - `boot_a`：Android boot header v4，核心從 4096 bytes 起，長 26723328 → `kernel.efi`（`7816c5d5…`）
  - `vendor_boot_a`：header v4、page 2048，cmdline 871 bytes，ramdisk 表三塊 LZ4（type 1 platform、type 2 recovery、type 3 dlkm），bootconfig 區 122 bytes
  - `init_boot_a`：header v4，只有 ramdisk（LZ4）
  - `vbmeta_a`：前 6720 bytes（256 + 576 + 5888）
  
  LZ4 用 `lz4 -dc` 解開。
- 原因：ESP 的 `kernel.efi`、兩份 initrd 的 cpio 與 bootconfig 基底、loader 的 options 都來自這幾個分割區。recovery 那塊 ramdisk 不用。
- 證據：REPORT.md:72（正常開機用的 initrd 是 platform cpio、dlkm cpio、init_boot cpio 再接 bootconfig）、REPORT.md:76–80（原有三行 bootconfig）、REPORT.md:185–189。產物：解出來的三個 cpio 與 `downloads/boot/{vendor_platform,vendor_dlkm,init_boot}.cpio` 相同；`boot_a` header 的 cmdline 是空的，所以 loader 的 options 就是 vendor cmdline。

### 3. ESP 用的 initrd（里程碑 E 的版本）

- 輸入：三個 cpio（依序 platform、dlkm、init_boot），vendor bootconfig 的 122 bytes。
- 做法：`make-initrd` 把三個 cpio 直接串接，bootconfig 文字後面接上 6 行，再接 Android bootconfig 尾端（`u32 size`、`u32 checksum`＝位元組總和、`#BOOTCONFIG\n`），沒有補齊：
  ```
  androidboot.slot_suffix=_a
  androidboot.force_normal_boot=1
  androidboot.boot_part_uuid=<boot_a 的分割區 GUID：65398d24-ffb3-40d7-bfbc-b472b4b24885>
  androidboot.vbmeta.hash_alg=sha256
  androidboot.vbmeta.size=6720
  androidboot.vbmeta.digest=<官方 vbmeta_a 前 6720 bytes 的 SHA-256：bc270dbe…>
  ```
- 輸出：`initrd.partuuid.img`，26278308 bytes，`623a9855…`，與 `downloads/boot/initrd.partuuid.img` 相同。
- 證據：REPORT.md:191（400 bytes、20 nodes、各行內容）。產物：kano-real.img ESP 裡的 `/initrd.img` 就是這個檔（SHA-256 相同）。

### 4. ESP（FAT32），照原本的三次修改重播

FAT 不會清掉釋放的叢集，所以最後的 ESP 位元組取決於中間每一次寫入。腳本按原順序重播：

1. **里程碑 E（04:47:28）**：`truncate -s 67108864`，`mkfs.fat -F 32 -n ESP -i 461813AC`，`mmd ::/EFI ::/EFI/BOOT ::/loader ::/loader/entries`，依序 `mcopy` `BOOTX64.EFI`、`loader/loader.conf`、`loader/entries/lapis.conf`、`kernel.efi`、`initrd.img`。E 的 `loader.conf` 是 `default lapis.conf`／`timeout 1`／`editor no`，`lapis.conf` 標題 `Lapis`，options 是 vendor cmdline 加上 `console=ttyS0,115200 earlycon=uart8250,io,0x3f8,115200 ignore_loglevel`。
2. **里程碑 G（07:55:02）**：`mcopy -o` 換掉 `loader.conf`（`timeout 10`、`editor yes`）與 `lapis.conf`（標題 `KANO lapis UART`，options 從 vendor cmdline 拿掉 `console=null `，接上 `loglevel=8 ignore_loglevel console=ttyS0,115200n8 console=uart8250,mmio32,0xfe03e000,115200n8 earlycon=uart8250,mmio32,0xfe03e000,115200n8 xe.force_probe=46a6`）。mtools 從 FSInfo 的提示往後配置新叢集（103842–103845），舊叢集 320–322 被釋放，但 E 的內容還留在那裡。
3. **里程碑 M**：`lapis.conf` 在 `console=ttyS0,115200n8` 後面插入 `console=ttyS4,115200n8`（1085 → 1108 bytes）。這次**不是** `mcopy`：FAT、FSInfo 與目錄項時間都沒變，只改了叢集 103843–103845 的內容與目錄項的大小欄位。腳本用 `gbos_base.py esp-rewrite` 做同樣的就地改寫（寫入原叢集鏈，其餘補 0，更新大小）。

最後 `splice` 寫進 `EFI-SYSTEM`（位元組 142606336 起，67108864 bytes）。

- 輸出：ESP 分割區 SHA-256 `59df36eb623255fde8ae277f00a91bc8bb1676acd0c3a54c8104183edc63d0a7`。
- 證據：REPORT.md:183–189（E 的檔案、標籤 `ESP`、序號 `4618-13AC`、`/usr/lib/systemd/boot/efi/systemd-bootx64.efi`）、REPORT.md:283–285（G 的 loader 設定與 options）、REPORT.md:444（M 加 `console=ttyS4`）。產物：`minfo` 顯示 `mkfs.fat` banner、FAT32、1 sector/cluster、32 保留磁區；目錄項時間 E 是 04:47:28、G 是 07:55:02；`downloads/lapis-uefi-boot.img`（E）、`downloads/lapis-kano-uart.img`（G）、kano-real.img（M）三者 ESP 逐磁區比對：G 動了 FSInfo、兩份 FAT、兩個目錄叢集與 4 個資料磁區；M 只動目錄磁區 2054 的 1 個 byte（大小欄位）與資料磁區 105892–105893。實測 `mcopy -o` 會把 M 配到叢集 103846，與 kano-real 不符，所以 M 必須就地改寫。三階段重播分別與三個映像的 ESP 逐位元組相同。

### 5. 取出 super 內的邏輯分割區

- 做法：`lp-extract` 解析 super（位元組 746586112 起）的 LP 中繼資料：geometry（metadata 65536、3 slots、block 4096）、header v10.2、flags `VIRTUAL_AB_DEVICE`、group `google_dynamic_partitions_a/_b`（各 12880707584）、12 個分割區（全部 readonly）與 extents，再依 extents 把 7 個非空分割區寫成檔（btrfs 上用 `copy_file_range`，近乎零成本）。
- 輸出：`system_a`、`system_b`、`vendor_a`、`product_a`、`system_ext_a`、`system_dlkm_a`、`vendor_dlkm_a`，與 `work/img/lp/*.img` 雜湊相同。
- 證據：產物（`../research/base-analysis/lp.orig.json`、`lp.real.json`）。kano-real 的 super 裡，除了 vendor_a、system_ext_a，其他 5 個邏輯分割區與官方 super 逐位元組相同。

### 6. vendor：軟體 KeyMint／Gatekeeper（里程碑 L）

- 做法：
  1. 依 AVB footer 的 original size（373395456）切出原 EROFS，`gbos_base.py erofs-meta` 讀出每個 inode 的 mode、uid、gid、`security.selinux`（自寫的 EROFS 解析器，與先前 `rebuild.py`＋`dump.erofs` 的結果逐項相同，1659 個 inode，沒有 capability、沒有 hard link、沒有 extended inode）。
  2. `fsck.erofs --extract --preserve-perms` 解出樹。
  3. `edit-vendor` 改樹（每個改過的檔都核對 kano-real 內的 SHA-256）：
     - 刪 `apex/com.android.hardware.keymint.strongbox.desktop.apex`（不再宣告 `IKeyMintDevice/strongbox`）
     - 加 `apex/com.android.hardware.keymint.rust_nonsecure.apex`、`apex/com.android.hardware.gatekeeper.nonsecure.apex`，標記與 uid/gid/mode 抄 `com.android.hardware.cas.apex`（`vendor_apex_file`、0:0、0644）
     - `etc/init/android.hardware.gatekeeper-service.trusty.rc` 改寫成 `disabled`（原為 `class hal`），刪 `etc/vintf/manifest/android.hardware.gatekeeper-service.trusty.xml`
     - `build.prop`：`ro.boot.hypervisor.protected_vm.supported=0`、`ro.hardware.keystore=default`、`ro.hardware.gatekeeper=default`
     - 加 `bin/maint-kmsg.sh`（0755、0:2000、`vendor_shell_exec`）與 `etc/init/maint-kmsg.rc`（0644、0:0、`vendor_configs_file`）。這是 L8 用來把 `maintprobe: Service android.security.maintenance: found` 寫進 kmsg 的探針。REPORT.md 只在 L 的開機紀錄裡提到它，沒有寫它被打包進 vendor；是比對 kano-real 的 vendor 樹才找到的
  4. 產生 fs_config 與 file_contexts：每個 inode 一條精確路徑規則，依（長度、路徑）排序，標記與權限抄原 inode（與 `work/src/rebuild.py` 的 `write_configs` 同一套規則）。
  5. `mkfs.erofs -z lz4hc,level=9 -b 4096 -T 1230768000 -U 7aead136-f6d4-508e-9bf2-2517134fe885 --mount-point /vendor --fs-config-file … --file-contexts …`
  6. `avbtool add_hashtree_footer --partition_name vendor --partition_size 452984832 --hash_algorithm sha256 --salt b14f17c0… --block_size 4096 --fec_num_roots 2`，三個 `--prop` 從原 footer 抄（os_version、fingerprint、security_patch）。
- 輸出：`vendor.erofs` 428445696 bytes（`da4ce412…`），`vendor.img` 452984832 bytes（`d496bbfb…`，root digest `e5f13ecb…`）。
- 證據：REPORT.md:372（otatools mkfs.erofs 1.8.3、`--fs-config-file`、`--file-contexts`、`-T 1230768000`、UUID）、REPORT.md:374–378（APEX、四個重疊怎麼收、分割區大小）、REPORT.md:396–399（maintprobe）。產物：`work/src/apply_build.py`（同樣的樹修改與 mkfs 參數）、`work/img/vendor.erofs`／`vendor.img` 的雜湊、`avbtool info_image`（FEC num roots 2、props）、kano-real vendor 樹與原 vendor 樹的 `diff`。分割區大小 452984832 是當時手選的，不是公式算的，腳本直接用常數。

### 7. system_ext：framework manifest（里程碑 L）

- 做法：同步驟 6。`edit-system-ext` 從 `etc/vintf/manifest.xml` 刪掉四段 `<hal>`：keymint v5 `IKeyMintDevice/default`、keymint v3 `IRemotelyProvisionedComponent/default`、`ISecureClock/default`、`ISharedSecret/security_vm`。UUID 沿用原超級區塊（`5600e670-83c3-5b38-9984-89debd2d7f0b`），footer 分割區大小 209715200。
- 輸出：`system_ext.erofs` 187203584 bytes（`05f2b873…`），`system_ext.img`（`2b748b13…`，root digest `300c2873…`）。
- 證據：REPORT.md:376、378；產物 `work/src/apply_build.py`、`work/img/system_ext.{erofs,img}`。

### 8. 重簽 vbmeta_a

- 做法：兩次 `avbtool update_partition_descriptor --algorithm SHA256_RSA4096 --key KEY --rollback_index 1339`：先用 `vendor.img` 換 vendor 的 hashtree 描述子（`vbmeta.step1.img`），再用 `system_ext.img` 換 system_ext 的。描述子順序與其他描述子、props 都不動。RSA PKCS#1 v1.5 簽章是確定性的，同一把金鑰得到同樣位元組。
- 輸出：6720 bytes，摘要 `8ad0c62e…`（原金鑰）。寫入 `vbmeta_a` 開頭，分割區其餘部分補 0（官方分割區在 6720 bytes 之後本來就全 0）。
- 證據：REPORT.md:378（SHA256_RSA4096、rollback 1339、6720 bytes、`8ad0c62e…`、公鑰 SHA-1 `79c98573…`）。產物：`work/img/vbmeta.step1.img`（只有 vendor 描述子變）、`vbmeta.new.img`（再加 system_ext），兩步都重現出相同雜湊。`build-safe2.sh` 也用同一個子命令。

### 9. 重建 super

- 做法：`lp-build` 依步驟 5 的中繼資料組出 `lpmake --metadata-size 65536 --super-name super --metadata-slots 3 --device-size 12884901888 --virtual-ab --group …a:12880707584 --group …b:12880707584`，12 個 `--partition name:readonly:size:group`（順序同官方），7 個 `--image`；vendor_a、system_ext_a 換成新檔，大小跟著變。`lpmake` 會對 raw 映像印 `Invalid sparse file format at header magic`，這是正常訊息。
- 輸出：12884901888 bytes raw super（`a64bb606…`），寫進 GPT `super`。
- 證據：REPORT.md:378（lpmake 訊息、raw、712 MiB 處）。產物：`work/img/super.new.img` 與 kano-real 的 super 相同；kano-real 與官方的 LP 中繼資料只差 vendor_a／system_ext_a 的大小與其後各分割區的位置。

### 10. 獨立 initrd.img

- 做法：同步驟 3 的三個 cpio，bootconfig 用同樣 6 行，但 `vbmeta.size`／`vbmeta.digest` 換成步驟 8 的新 vbmeta，最後再加 3 行：
  ```
  androidboot.hypervisor.protected_vm.supported=0
  androidboot.vendor.apex.com.android.hardware.keymint=com.android.hardware.keymint.rust_nonsecure
  androidboot.vendor.apex.com.android.hardware.gatekeeper=com.android.hardware.gatekeeper.nonsecure
  ```
- 輸出：`initrd.img`，26278551 bytes，bootconfig 643 bytes（原金鑰：`a7ffe3f7…`）。
- 證據：REPORT.md:374、378；產物 `work/img/initrd.img`、`work/img/vbmeta.digest.txt`。

### 11. 收尾

整檔 SHA-256，原金鑰時與 `1ce7e958…` 比對。寫 `SHA256SUMS`、`build-info.txt`（輸入、工具版本、公鑰 SHA-1、vbmeta 摘要、警告），`.partial` 改名，建 `BUILD-DONE`，刪 `work/`。

## 已知問題：kano-real.img 的 ESP initrd 摘要過期

kano-real.img ESP 裡的 `/initrd.img` 是里程碑 E 的檔，bootconfig 仍是官方 vbmeta 的摘要 `bc270dbe…`，但 `vbmeta_a` 已經換成自簽的 `8ad0c62e…`。從 ESP 直接開機時，first stage init 會因摘要不符而停（REPORT.md:94–102 的 `Failed to verify vbmeta digest`；REPORT.md:430 也寫 ESP 那份舊 initrd 沒換）。要開這份映像，需要用獨立的 `initrd.img`（QEMU `-initrd`），或像 `kano-safe2` 那樣換掉 ESP initrd 的 bootconfig（REPORT.md:454）。

腳本的目標是重現 kano-real.img，所以照原樣保留這個不一致，並寫進 `build-info.txt`。

## 換金鑰時會變什麼

用新金鑰（或不給 `--key` 讓腳本產生）時：

- `vbmeta_a`：公鑰、簽章，因此摘要也變
- 獨立 `initrd.img`：只有 `androidboot.vbmeta.digest=` 那一行
- 整檔雜湊

其他都不變：ESP（裡面那份 initrd 本來就帶官方摘要）、super 與其中每個邏輯分割區、其餘 27 個 GPT 分割區。2026-10-12 用新產生的金鑰實跑過一次，`compare-base.py` 與 `avbtool verify_image` 的結果在 `RESULT.md`。

## 沒有涵蓋的部分

- 只驗證 lapis build 16452207。moonstone、ruby 或其他 build 的 zip 會在第一步被拒絕。vendor／system_ext 的修改是針對這個 build 的內容寫死的，每處修改都要求原文剛好出現一次
- `kano-safe*`、`kano-final*`、pastel／QEMU 顯示用的 vendor（`work/img/vendor.qemu.*`、`initrd.disp.img`）不在範圍內
- systemd-boot 不是 Google 產物，固定為 Arch systemd 261.2-1 的那一份（主機沒有就下載）。mkfs.fat 或 mtools 版本不同時，ESP 的 FAT 位元組會不同（內容相同），其他分割區不受影響
