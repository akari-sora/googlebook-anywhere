# 安裝碟（KANO 上直接裝到內建碟）

開發者的隨身碟除了 Googlebook 本身，還有一個維護用 Linux（Arch，`kano-maint` 分割區）與 XBOOTLDR 分割區。
這個目錄把那支隨身碟變成**安裝碟**：開機選單多一個「Install Googlebook to the internal disk」，在 KANO 的螢幕上確認後，
把隨身碟上的 Googlebook 全新安裝到內建 NVMe，不需要另一台電腦。

> 用 `install.sh` 再 `write-usb.sh` 寫出的是 Googlebook 隨身碟，**沒有**維護系統，不能拿來裝內建碟。
> 安裝碟是另一個檔，由 `install/make-installer-stick.sh` 寫成：Googlebook、XBOOTLDR（核心、initramfs 與開機選項），以及已經寫好的 ext4 `kano-maint`。它不需要 root。
> 維護根目錄仍由 `install/stick/make-maint.sh` 檢查釘選清單並組出來。提交的 `maint.pins` 是 Arch Linux Archive 2026/10/01 的 145 個套件。不加 `--cache` 時 `fetch` 只檢查清單。那些檔案不在這個 repo 裡。套件沒有 `boot/initramfs-linux.img` 時，`assemble` 用根目錄自己的 `depmod` 與 `mkinitcpio` 做出來；在 Linux 測試主機上，對得上的快取已留下根目錄（核心 `7.2.7-arch1-1`，工具 23 個）。那次 assemble 印出的 initramfs 是 29790865 位元組；之後每次組裝以它自己印出的 initramfs 行為準。`pack` 在檔案不屬於 uid 0，或缺少映像工具時，什麼都不寫。`pack` 現在寫出的 `kano-maint.img` 是 8321499136 位元組（7936 MiB），`xbootldr.img` 是 536870912 位元組。先前 Linux 測試主機上那份 12348030976 位元組的 `kano-maint.img` 是縮區之前的大小，不是現在的輸入。那兩份映像都不是 `make-installer-stick.sh` 的輸入。

## 安裝程式做什麼（`gbos-install.sh`）

1. **先確認這次開機的防護**：核心 lockdown 必須是 `integrity` 或 `confidentiality`，開機參數必須有 `efi=noruntime`；
   開機參數裡只能有一個 `module_blacklist`（核心只採用最後一個），而且要包含安裝程式選項封鎖的**每一個**模組
   （`module-blacklist.txt`），`initcall_blacklist` 必須包含 `builtin-initcalls.txt` 裡的每一個 initcall；
   這些模組一個都不能已經載入；`/sys/class/tpm` 沒有任何項目，`/dev/tpm0`、`/dev/tpmrm0`、`/dev/nvram`、`/dev/cros_ec`、
   `/dev/mtd*`、`/dev/spidev*` 都不存在。任何一項不符就直接停止，什麼都不寫。
2. **寫入前先核對映像與這支隨身碟**（before the NVMe driver is loaded）：manifest 格式；映像分割表（`head.img.zst` 的長度、SHA-256、
   protective MBR、GPT 表頭與分割項目的 CRC，且與 manifest 逐項相符）；每個 pristine 檔案（`zstd -t` 加解壓後的 SHA-256）；
   安裝模式才在這裡對隨身碟上的 super 做 SHA-256（USB 上要幾分鐘，畫面會提示）。更新模式不在這一步做，見下面。
   隨身碟的邏輯磁區必須是 512 bytes；
   隨身碟的 XBOOTLDR 與 ESP 掛得起來。這一步核對的項目有任何一項不符就停止，**不會問序號**，也不載入 NVMe 驅動（更新模式的隨身碟 super 雜湊在序號相符後、打開目標寫入前才做，若 super 內容損壞會在那時停止）。
3. 核對通過後才載入 NVMe 驅動，序號才讀得到。The disk is visible from here。列出型號、容量與序號，要你在鍵盤上輸入**序號最後 4 碼**。
   Do not write, mount, or discard：最後 4 碼相符之前，不寫分割區、不掛檔案系統、不做 discard。輸入其他內容或直接 Enter 就取消，leaves the disk untouched。
   內建碟的邏輯磁區也必須是 512 bytes，這是載入驅動之後才讀的。
   問序號前與確認後各檢查一次目標碟沒有在使用（掛載、swap、LVM／RAID／dm-crypt 的 holder），而且序號、容量與
   裝置號碼（major:minor）沒變；任何一項查不到（`/sys/block`、`lsblk`、`/proc/mounts`、`/proc/swaps` 讀取失敗）也一律停止，
   不當成「沒在使用」。
4. 先把目標碟原本的分割表（開頭與結尾各 1 MiB）存到隨身碟的 `/var/lib/gbos-install/backup/`。這只是分割表，不是你的資料。
5. 寫入映像自己的分割表；super（12 GB）從隨身碟複製，其餘分割區從 `pristine` 副本寫入。每個分割區寫完都**從碟上讀回**
   （direct I/O）再核對 SHA-256。隨身碟自己的 /data、metadata 等執行期狀態一概不複製。
6. userdata 放到碟尾（保留 manifest 裡的 type 與屬性）、清空開頭，第一次開機時 Android 自己格式化
   （從歡迎畫面開始，需要重新登入 Google 帳號）。
7. 重新產生所有分割區 GUID（隨身碟與內建碟不會重複），ESP 的 initrd 指向內建碟的 boot_a，每個開機選項只拿掉
   `nvme_init` 的封鎖（`module_blacklist`、`efi=noruntime` 不變），預設 quiet、選單編輯器關閉、`auto-firmware no`；
   隨身碟 XBOOTLDR 上的「Googlebook (internal disk)」改指向新的安裝，隨身碟選單的預設改成它。
8. **最後整體核對**才印 `INSTALL OK`：主、備兩份 GPT（CRC、備份在碟尾且與主表相同）、每個分割區的起點／大小／type／
   名稱／屬性與 manifest 相同（userdata 從新起點到最後一個可用磁區）、GUID 都是新的、`sgdisk -v` 回報 `No problems found`、
   /data 開頭為空，以及 ESP 的 initrd、kernel、開機選項、loader.conf 與隨身碟的 kernel、initrd、選單項目逐一讀回比對。

任何一步失敗（包括意外的指令錯誤）都會顯示原因並關機，結束碼非 0；已經開始寫入時會明說「內建碟不完整，請重跑安裝程式」。
畫面上的內容同時記在隨身碟的 `/var/log/gbos-install-<時間>.log`。

## 安裝程式開機選項的防護

安裝程式只在開機參數有 `gbos.install=1` 時啟動（只有那個選單項目有）。選單項目由 `gbos-install.entry.template` 產生：

- `module_blacklist`：Android 開機選項封鎖的所有模組（`profiles/kano/profile.toml` 的 `blacklist.modules`，另含 `cros_ec_proto`），再加上
  `mei_me`、`8250_dw`、`intel_lpss_pci`、`intel_lpss`、`intel_lpss_acpi`、`i915`、`tpm_crb`。`nvme` 不封鎖。
- `initcall_blacklist`：由 `builtin-initcalls.txt` 產生（見下面）。
- `efi=noruntime`：不呼叫 EFI runtime services（不寫 EFI 變數）。
- `lockdown=integrity`：即使是 root 也不能用 `/dev/mem`、`/dev/port`、`iopl`／`ioperm` 或寫 MSR，
  用來擋住 flashrom internal 與 ectool 經 LPC 的存取路徑（這點沒有在實機驗證）。
- 第一次開機不改命令列。`assemble` 寫入 `/etc/machine-id`（固定 32 個十六進位字 `67626f73000000000000000000000001`，前四個位元組是 ASCII `gbos`）。`systemd-firstboot.service` 的 `ConditionFirstBoot=yes` 因此不成立，不會詢問 locale、鍵盤、時區或 root 密碼。根目錄有一般檔 `usr/share/zoneinfo/UTC` 時，另外把 `/etc/localtime` 連到 `../usr/share/zoneinfo/UTC`。選項仍以 `gbos.install=1` 結尾。`setup-stick.sh` 只在這個結尾時，才在後面補上 `gbos.update=1`。

這**不是**和 Android 選項相同的保護：維護系統跑的是 Arch 的核心，`module_blacklist` 只擋得住可載入模組，
擋不住編進核心（built-in）的驅動，而 Android 核心的實測結論也不能直接套用到 Arch 核心。
提交的 `maint.pins` 組出來的維護核心是 `7.2.7-arch1-1`。2026-10-13 實機量到把 `tpm`、`tpm_tis`、`tpm_tis_core`、`tpm_crb`、`nvram` 編進核心的，是當時的 Arch 7.2.3-arch1-3。`builtin-initcalls.txt` 仍用那一份對應。這幾個改用
`initcall_blacklist` 擋：`builtin-initcalls.txt` 列出每個 built-in 模組對應的 initcall（`tpm_init`、`init_tis`、
`crb_acpi_driver_init`、`nvram_module_init`，另外照 Android 選項保留 `mei_me_driver_init`）。`tpm_tis_core` 是函式庫，
沒有自己的 initcall，只有在 `tpm_init`、`init_tis`、`crb_acpi_driver_init` 都被擋住時才接受。所以：

- `setup-stick.sh` 讀 XBOOTLDR 上那個核心的 `/usr/lib/modules/<版本>/modules.builtin`：封鎖名單上的模組若是 built-in，
  而 `builtin-initcalls.txt` 沒有涵蓋，就拒絕；選單項目的 `initcall_blacklist` 由這個檔案產生，檔案本身也裝到
  `/usr/local/share/gbos-install/` 給安裝程式用。它也會核對映像本身每個選項封鎖的模組都在名單裡
  （機型設定以後加了模組，這裡會擋下來），再把選項的整份 `module_blacklist` 寫成同一個目錄的
  `module-blacklist.txt`。
- `gbos-install.sh` 執行時再檢查一次（第 1 步）：lockdown、`efi=noruntime`、整份 `module_blacklist`、每一個 initcall、
  這些模組都沒有載入，以及 TPM、nvram、cros_ec、MTD、spidev 的裝置都不存在；任何一項不符就拒絕執行。
  所以從別的選項（例如一般的維護系統）或少了任何一個模組的選項開機，安裝程式都不會動手。
- 名單以外的 built-in 驅動、以及 Arch 核心本身的行為，沒有另外驗證。

### 實機驗證（KANO，2026-10-13）

在真的 KANO 上、用開發者的安裝碟（Googlebook 分割區是開發者用這個 repo 建的映像）做了「開進安裝程式再取消」：

- 從開機選單選「Install Googlebook to the internal disk」開機。這次開機的實際狀態：`/proc/cmdline` 有
  `lockdown=integrity efi=noruntime initcall_blacklist=mei_me_driver_init,tpm_init,init_tis,crb_acpi_driver_init,nvram_module_init gbos.install=1`；
  `/sys/kernel/security/lockdown` 是 `none [integrity] confidentiality`；`/sys/class/tpm` 沒有項目；`/dev/tpm0`、
  `/dev/tpmrm0`、`/dev/nvram` 都不存在；dmesg 有 `initcall tpm_init blacklisted`、`initcall nvram_module_init blacklisted`、
  `initcall init_tis blacklisted`、`initcall crb_acpi_driver_init blacklisted`；以 root 讀 `/dev/mem` 得到
  `Operation not permitted`。
- 安裝程式印出 `ok   kernel lockdown: integrity; EFI runtime services off`、built-in 驅動封鎖的檢查通過，
  接著核對安裝資料（28 個分割檔）與隨身碟上的 super，都通過，才列出內建 NVMe 並問序號。
- 輸入錯的答案（`1`）加 Enter：`Cancelled. Nothing was written.`；依畫面按 Enter 關機；再以預設選項開機，
  開進內建碟上原本的 Googlebook（同一個 boot_a）。

2026-10-13 再用目前這版重做一次：`setup-stick.sh` 在實機的維護系統上重新設定隨身碟（裝上 `module-blacklist.txt`），
安裝程式通過整份 `module_blacklist` 核對與其他執行期檢查，到序號提示；輸入錯的答案後同樣 `Cancelled. Nothing was written.`，
關機後開回內建碟。**真的寫入內建 NVMe 的完整安裝只在 QEMU 驗證過**（真隨身碟的 dump、
模擬的 NVMe），沒有在 KANO 的內建碟上實際跑過。

上面記下來的 `/proc/cmdline` 沒有 `systemd.mask=systemd-firstboot.service`。目前的安裝選項以 `gbos.install=1` 結尾，更新選項以 `gbos.install=1 gbos.update=1` 結尾，兩者都不加那個 token。This tree has no separate v0.2.0 blob for fcb0e6a, so the menu check cannot prove byte equality with that commit. 現行檢查比的是這份 template、render 與 FAT 回讀，不是那個 commit 的原文。

## 更新內建碟（保留資料）

`setup-stick.sh` 也會加上選單項目「Update Googlebook on the internal disk (keep data)」。它就是安裝程式的選項，多一個 `gbos.update=1`，所以開機防護與第 1 步的檢查完全相同。

接著 `gbos-install.sh` 進入更新模式，依序：

Update mode hashes the stick super after the serial matches, before the target is opened for write.
安裝模式在載入 NVMe 之前就雜湊隨身碟 super。更新模式不是那樣：序號最後 4 碼相符、目標再核對一次之後，`gbos-update.py` 的 `pristine_release` 才雜湊這份 super，而且是在打開目標寫入之前。兩種模式並沒有都在載入驅動之前做完 super 雜湊。

1. 核對安裝資料（manifest 與 pristine；super 的 SHA-256 在第 4 步、寫入之前）；
2. 顯示內建碟目前的版本與 slot（`gbos-update.py inspect`）；
3. 要你輸入序號最後 4 碼；
4. 執行 `gbos-update.py apply --mode nvme`：
   - 新版本寫進另一個 slot，/data 保留；
   - 寫入任何 slot 之前，隨身碟選單的「Googlebook (internal disk)」與「(previous version)」都先改開正在用的版本（從內建碟複製到隨身碟的副本），沒有項目開正在寫的 slot；寫完、讀回之後「Googlebook (internal disk)」才改開新版本；
   - 中途停下時，隨身碟的「Googlebook (internal disk)」仍開一個完整的版本；再選一次這個項目就會寫完；
   - 完成後，隨身碟選單的預設設成「Googlebook (internal disk)」；
   - 內建碟的 ESP 放不下兩個版本時（Google 換 kernel 的新 build），更新期間內建碟自己的選單開不了任何版本，只能從這支
     安裝碟開機；
   - 有兩支安裝碟時，另一支上的內建碟選項在這次更新後就過時了：先用它跑一次更新再開那兩個選項（見 docs/UPGRADE.md）。

原理、退回與限制見 [docs/UPGRADE.md](../../docs/UPGRADE.md)。這個模式還沒有在 KANO 實機上跑過，只在合成碟上的 `tests/update/selftest.py` 與 QEMU（`tests/stick/qemu-install.py` 的更新情境，用真隨身碟的維護系統）驗證過。

## 建立安裝碟

下面這一段可以照做。主機是 Linux x86_64。`assemble` 用 `unshare -U -r -m -p --fork` 與 chroot 做 initramfs，一般使用者命名空間必須允許。macOS 與 `docker-install.sh` 可以建 Googlebook 映像，做不了這一步。

`fetch` 拒絕還沒有的快取目錄，所以先建立它。`assemble` 的 `--out` 還不能存在，上一層要已經存在。`make-pristine.py` 自己建立輸出目錄，那個目錄還不能存在。`fetch` 與 `assemble` 只用已經提交的 `maint.pins`。

Googlebook 複製到 LBA 37244880 的代表映像，加上維護區與 33 個備份磁區，是 54546417 磁區、27927765504 bytes。報 28.0 GB 的隨身碟是 28000000000 bytes（28×10^9）。`write-usb.sh` 拒絕比映像檔還小的碟。別的映像用它自己印出的 `minimum_stick_sectors`。稀疏存放不會少掉這個容量。`--disk-sectors` 只檢查容量，不會自己把分割區縮小。

```sh
mkdir -p "$HOME/gbos-maint/cache"
install/stick/make-maint.sh fetch --cache "$HOME/gbos-maint/cache"
install/stick/make-maint.sh assemble --cache "$HOME/gbos-maint/cache" --out "$HOME/gbos-maint/maint"
install/stick/make-pristine.py ~/gbos-work/build/image/googlebook-kano.img "$HOME/gbos-maint/pristine"
install/make-installer-stick.sh \
  --image ~/gbos-work/build/image/googlebook-kano.img \
  --out "$HOME/gbos-maint/stick.img" \
  --xbootldr "$HOME/gbos-maint/maint/xbootldr" \
  --root "$HOME/gbos-maint/maint/root" \
  --pristine "$HOME/gbos-maint/pristine"
sudo install/write-usb.sh "$HOME/gbos-maint/stick.img" /dev/disk/by-id/usb-<你的隨身碟>
```

映像旁會有 `.not-self-tested`，因為 `make-installer-stick.sh` 沒有把這支碟開機。`write-usb.sh` 印出那個檔的第一行，下一個提示要輸入 `untested` 才寫入。這只是多一次確認。

不要加 `--keep-data`。`assemble` 留下 `root/` 與 `xbootldr/`（裡面是 `vmlinuz-linux` 與 `initramfs-linux.img`）。`pack` 不是這段的輸入。它只在 uid 0 或 fakeroot，而且有 `mke2fs`、`mkfs.vfat`、`mcopy` 時，另外寫出 `kano-maint.img` 與 `xbootldr.img`。那兩份映像不是 `make-installer-stick.sh` 的輸入。

開機選單是 Install Googlebook to the internal disk、Update Googlebook on the internal disk (keep data)、Googlebook (internal disk)、Googlebook (internal disk) serial-debug。沒有 `maintenance.conf`，也沒有普通 Maintenance 殼層。開機後選 **Install Googlebook to the internal disk**。維護系統已經在 `kano-maint` 上，不必再跑 `setup-stick.sh`。

### 其他子命令

`install/stick/make-maint.sh` 不寫磁碟。子命令：

```sh
install/stick/make-maint.sh layout --image googlebook-kano.img
install/stick/make-maint.sh check-pins
install/stick/make-maint.sh verify-cache --cache DIR
install/stick/make-maint.sh fetch [--cache DIR]
install/stick/make-maint.sh assemble --cache DIR --out DIR
install/stick/make-maint.sh pack --out DIR
install/stick/make-maint.sh build --cache DIR --out DIR
```

- `layout` 還可以加 `--disk-sectors N`。它讀 Googlebook 映像的 GPT，印出 XBOOTLDR 與 kano-maint 的位置。XBOOTLDR 是最後一個 Googlebook 分割區之後的 512 MiB，kano-maint 是接下來的 7936 MiB（8321499136 bytes）。映像裡若已經有名為 `XBOOTLDR` 或 `kano-maint` 的分割區，或分割區編號到 32，就拒絕。不要把做好的安裝碟檔再餵給 `layout`。Googlebook 複製到 LBA 37244880 的代表映像是 54546417 磁區、27927765504 bytes。報 28.0 GB 的隨身碟是 28000000000 bytes（28×10^9）。比映像檔還小的碟，`write-usb.sh` 會拒絕。別的映像用它自己印出的 `minimum_stick_sectors`。
- `check-pins` 檢查釘選清單。每一行是 `sha256 位元組數 快取檔名 https 網址`。`install/stick/maint.pins` 是 2026/10/01 的 145 個套件。`check-pins --file` 只檢查另一份清單。
- `fetch` 與 `build` 只讀腳本旁邊那份清單，不接受 `--file`。不加 `--cache` 時 `fetch` 只印檢查結果，不下載。加上 `--cache DIR` 才把清單裡的檔案抓進那個目錄；大小或 sha256 不符就拒絕，不留下那份檔。清單目前是 2026/10/01 的 145 個 Arch 套件。快取裡沒有這些檔時，`build` 拒絕，結束碼 2。
- `install/stick/maint-pin-arch.py` 只給維護者重產 `maint.pins`，不在 fetch、assemble、build 或 `make-installer-stick.sh` 裡。它先讀 `maint-db.pins` 裡事先釘好的 `core.db` 與 `extra.db`（sha256 與位元組數）。下載的位元組對不上就不解析資料庫，也不寫新的套件清單。沒有那一天的摘要時，它在下載前就拒絕。一般建置只使用已經提交的 `maint.pins`。
- `verify-cache --file` 與 `assemble --file` 是離線檢查。它們不是拿另一份清單來建置的方法。`assemble` 在清單有檔、快取核對通過，而且根目錄裡有核心和工具時，才把目錄放到 `--out`。套件沒有 `boot/initramfs-linux.img` 時，它用這份根目錄的 `mkinitcpio` 做出該檔（linux 套件沒有 `modules.dep`，所以先跑 `depmod -a`）。做不出檔，或契約沒過，就不留下目錄。空清單拒絕。它不寫映像。
- `pack` 在檔案會屬於執行者自己的 uid 時，寫任何東西之前就拒絕。root，或 fakeroot，而且有 `mke2fs -d`、`mkfs.vfat`、`mcopy` 時，才寫出 `kano-maint.img`（ext4，7936 MiB，8321499136 bytes）與 `xbootldr.img`（FAT32，512 MiB）。它把根目錄記成 uid 0，不跟隨符號連結。套件裡的 setuid 會留著；沒有擁有者讀取位的一般檔會先補上那個位，mke2fs 才讀得到，uid 0 之後這個位不會多給別人權限。FAT 標籤是 `XBOOTLDR`，裡面是 `vmlinuz-linux` 與 `initramfs-linux.img`。那兩份映像不是 `make-installer-stick.sh` 的輸入。先前 Linux 測試主機上 12348030976 位元組的 `kano-maint.img` 是縮區之前的檔。

`install/make-installer-stick.sh` 只寫一個普通檔案，不開區塊裝置，也不需要 root。上面的順序就是它的輸入。`--image` 是 Googlebook 映像。`--xbootldr` 必須是目錄，裡面要有 `vmlinuz-linux` 與 `initramfs-linux.img`。`--root` 是 `assemble` 留下的維護根目錄。`--pristine` 必須由**同一個**映像經 `make-pristine.py` 做出。也可以加 `--disk-sectors N`；碟比配置所需還小就拒絕。成功時印 `stick ok`、`kano-maint ext4`、`owners uid 0`、`sha256`，以及 `minimum_stick_sectors`。預設檔案就是這個磁區數（512 bytes 一區），可以稀疏存放，旁邊有 `.sha256`。檔案內容：

1. Googlebook 複製到它最後一個分割區為止。映像檔尾端多出來的磁區不複製。輸出檔上的 ESP `loader.conf` 預設是 `gbos-install.conf`（`timeout 5`、`editor no`、`auto-firmware no`）。來源映像本身不改。
2. 接著是 512 MiB 的 FAT32，分割區名稱 `XBOOTLDR`。裡面有上面那兩個核心檔，以及 `gbos-install.conf`、`gbos-update.conf`、`gbos-nvme.conf`、`gbos-nvme-debug.conf`。沒有 `maintenance.conf`，也沒有普通 Maintenance 殼層。
3. 再接著是 7936 MiB（8321499136 bytes）的 ext4 `kano-maint`。擁有者記成 uid 0。裡面有 `gbos-install`、它的服務、`gbos-update.py`、pristine 資料、`module-blacklist.txt`、`builtin-initcalls.txt`。Googlebook 複製到 LBA 37244880 時，整個檔是 54546417 磁區、27927765504 bytes，不大於 28000000000 bytes。`write-usb.sh` 拒絕比這個檔還小的碟。

它不呼叫 `write-usb.sh`。`write-usb.sh` 的檢查都還在：只接受 USB 整顆碟、要輸入序號、寫完回讀，備份 GPT 移到碟尾。要把這個檔放到隨身碟，仍用 `write-usb.sh`。不要加 `--keep-data`：`install/gbos-update.py` 拒絕磁碟上有發行版沒有的分割區，多出來的就是 XBOOTLDR 與 kano-maint。

維護系統已經在這個檔的 `kano-maint` 上。開機後選 **Install Googlebook to the internal disk**，不必再跑 `setup-stick.sh`。

### 已經有維護系統時

上面的 `make-installer-stick.sh` 已經把安裝程式放進新檔的 `kano-maint`。下面這段是手上已經有一支維護系統隨身碟、只想換 Googlebook 時的做法，不是做新安裝碟的步驟。

```sh
# 1. 隨身碟的 Googlebook 分割區要先是某個映像：用 git repo 裡的 tools/harness/kano-delta-push.py 推上去（發行包沒有這個工具；
#    write-usb.sh --keep-data 會拒絕安裝碟，因為它的分割區多了維護系統與 XBOOTLDR）
# 2. 從同一個映像做 pristine 資料（在主機上）
install/stick/make-pristine.py googlebook-kano.img pristine/
# 3. 把 install/stick/、install/gbos-update.py 與 pristine/ 複製到維護系統（保持 install/ 的目錄結構），在維護系統上以 root 執行
install/stick/setup-stick.sh pristine/
```

`setup-stick.sh`（要和 `gbos-install.entry.template`、`builtin-initcalls.txt` 放在同一個目錄；`gbos-update.py` 放在同一個目錄或上一層，即 repo 的 `install/`）用 `gbos-install` 自己的核對程式檢查 pristine 資料，確認隨身碟上的 super 來自同一個映像，做上一節的
核心檢查，再安裝 `gbos-install`、systemd 服務與 `module-blacklist.txt`（複製後的 pristine 資料再核對一次）並加上選單項目。
隨身碟上「Googlebook (internal disk)」的選項，只有在隨身碟裡那份內建碟的 kernel 與 initrd（`/gbos-nvme/`，安裝程式寫的）
就是這個映像的（initrd 只差內建碟自己的 boot_a GUID）時，才用這個映像的選項重寫；否則保持原樣並印出說明，
不會讓舊的 kernel 配上新映像的開機參數。
隨身碟選單的預設**只有在**「Googlebook (internal disk)」已經存在（裝過一次之後）才改成它；之後隨身碟一直插著也照常開內建碟。
推了新映像到隨身碟後，要重做第 2、3 步，否則安裝程式會在核對 super 時停止（內建碟不會被動到）。

## 測試

- `tests/stick/selftest.py <空目錄>`：不需要 QEMU、root 或磁碟，任何有 python3、bash 與 zstd 的機器都能跑。用小的合成映像跑
  `make-pristine.py`，再對 `gbos-install.sh` 內嵌的核對程式餵正確與各種損壞的資料（分割檔、head、manifest、GPT CRC、
  備份 GPT、userdata 位置與屬性、GUID、bootconfig、開機選項）；開機參數的核對（`setup-stick.sh` 的 `blacklist()` 從
  template 產生的名單、少了中間一個模組、第二個 `module_blacklist`、少 initcall、沒有 `efi=noruntime`、模組已載入）；
  以及安裝程式的「目標碟是否在使用」檢查（假的 `/proc`、`/sys` 與 `lsblk`：掛載、swap、holder，以及讀不到時必須停止）。
- `tests/stick/qemu-install.py <工作目錄> <最終映像> <隨身碟前 1 MiB> <隨身碟最後 1 MiB> <XBOOTLDR dump> <維護根目錄 dump> <pristine 目錄>`：
  Linux + KVM，需要 mtools 與 e2fsprogs，不需要 root。這組參數仍會在**自己的測試碟副本**上，以 `debugfs -w`
  把 `gbos-install.sh`、服務、`builtin-initcalls.txt`、`module-blacklist.txt`（和 `setup-stick.sh` 一樣由 template 產生）
  與 pristine 資料寫進維護根目錄的副本並讀回核對，選單項目由同一個 template 產生。
- `tests/stick/qemu-install.py --produced <工作目錄> <最終映像> <make-installer-stick.sh 的檔> <pristine 目錄>`：
  直接開那個檔的稀疏副本，不對來源檔做 `debugfs -w`，也不注入 ext4。開機前先讀取產出的 `gbos-install.conf`、`gbos-update.conf` 與 ESP `loader.conf`，必須與組裝寫入的位元組相同，且沒有 `maintenance.conf`。序列埠與缺少 lockdown、initcall 或模組的情境使用額外檔名，不改寫那三個交付檔。來源檔的 sha256 在測試前後不變。副本的 ESP default 只有在核對過 `default gbos-install.conf` 之後，才改指額外的序列埠檔名，timeout 也改成 1。來源檔的 sha256 相同，不代表這份開機副本沒被改。先前若把這次開機寫成「沒有先改寫 loader default」，那一句是錯的。
- `tests/stick/qemu-install.py --asis` 用同一組參數。QEMU 前對**開機副本自己**算 sha256，必須與產出的安裝碟檔相同。副本的寫入進 QEMU snapshot。不改 entry、cmdline、loader default 或 timeout。預設就是交付的 Install 項目。虛擬鍵盤（QMP send-key）打在普通畫面，不走序列埠項目。(a) 不按鍵，提示出現後再等一段時間，NVMe 零寫入；(b) 錯的四碼，零寫入；(c) 對的後四碼，畫面必須出現 `INSTALL OK` 與 `Press Enter to reboot`，而且虛擬 NVMe 的分割與映像讀回通過，少一邊不算過；(d) 用方向鍵選 Update（按幾下由可見項目的反向檔名排序決定：預設列是 Install，排在它下面或 Update 上面的項目不算），畫面必須出現 `UPDATE OK` 與 `Press Enter to reboot`，已有的使用者資料還在。`(c)` 讀回通過後、`(d)` 取基準雜湊前，先在 userdata 前 16 MiB（安裝程式清空的那段）與起點後 64 MiB 各植入非零資料，`(d)` 同時雜湊這兩段，因此更新若誤清 userdata 開頭也會失敗。QMP 沒有帶回 `unmap_operations` 時，這項是未覆蓋，不算沒有 discard。這四項印成 `AS-IS ACCEPTANCE`，與上面的序列埠／負面案例分開。韌體若一直沒印出 `BdsDxe: loading` 且 QEMU 還在，會重開一次，第二次才算這項失敗。離線檢查是 `tests/stick/asis-gate.py`。
  靜態檢查封鎖名單、`builtin-initcalls.txt` 與維護核心的 built-in 模組，接著跑七個情境：沒有 lockdown（拒絕）、
  `initcall_blacklist` 缺 TPM／nvram 的 initcall（拒絕）、`module_blacklist` 少了中間的 `spi_intel_pci`（拒絕）、
  super 被改一個 byte（序號前就拒絕）、取消、裝到有 12 個 Flex 分割區與資料的 NVMe、裝到空白 NVMe；取消與安裝時要看到
  安裝程式的開機參數與 built-in 檢查都通過。拒絕與取消時以 QMP 的寫入計數確認 NVMe 零寫入；
  兩次安裝都逐項比對分割表、內容與開機檔，最後只用 NVMe 開機，確認 Android 寫入 userdata。
  執行結果記在 `tests/stick/RESULT.md`。2026-10-14 在 Linux 測試主機上，用分割區 dump 組出的測試碟跑完，98 項通過、0 項失敗；那一份不是 `make-installer-stick.sh` 的檔。2026-10-15 對 `make-installer-stick.sh` 做出的檔分開跑 `--asis`（開機副本 sha256 與產出檔相同、`snapshot=on`、不改選單或 loader default，印出 `AS-IS ACCEPTANCE OK (40/40)`）與 `--produced`（核對交付選單後把開機副本的 loader default 改指額外序列埠項目，印出 `STICK INSTALLER TEST OK (98/98)`），來源檔的 sha256 都沒有變。這幾次都不是真的 KANO。
