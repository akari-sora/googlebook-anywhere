# 安裝碟（KANO 上直接裝到內建碟）

開發者的隨身碟除了 Googlebook 本身，還有一個維護用 Linux（Arch，`kano-maint` 分割區）與 XBOOTLDR 分割區。
這個目錄把那支隨身碟變成**安裝碟**：開機選單多一個「Install Googlebook to the internal disk」，在 KANO 的螢幕上確認後，
把隨身碟上的 Googlebook 全新安裝到內建 NVMe，不需要另一台電腦。

> 一般使用者照 README 用 `install.sh` + `write-usb.sh` 做出的隨身碟**沒有**維護系統，不能用這個安裝程式。
> 建立維護系統（pacstrap 一個 Arch 到隨身碟的額外分割區）目前沒有自動化腳本，也還沒有寫成文件。

## 安裝程式做什麼（`gbos-install.sh`）

1. **先確認這次開機的防護**：核心 lockdown 必須是 `integrity` 或 `confidentiality`，開機參數必須有 `efi=noruntime`；
   開機參數裡只能有一個 `module_blacklist`（核心只採用最後一個），而且要包含安裝程式選項封鎖的**每一個**模組
   （`module-blacklist.txt`），`initcall_blacklist` 必須包含 `builtin-initcalls.txt` 裡的每一個 initcall；
   這些模組一個都不能已經載入；`/sys/class/tpm` 沒有任何項目，`/dev/tpm0`、`/dev/tpmrm0`、`/dev/nvram`、`/dev/cros_ec`、
   `/dev/mtd*`、`/dev/spidev*` 都不存在。任何一項不符就直接停止，什麼都不寫。
2. **寫入前先核對全部資料**（內建碟還完全沒動）：manifest 格式；映像分割表（`head.img.zst` 的長度、SHA-256、
   protective MBR、GPT 表頭與分割項目的 CRC，且與 manifest 逐項相符）；每個 pristine 檔案（`zstd -t` 加解壓後的 SHA-256）；
   隨身碟上的 super（SHA-256，USB 上要幾分鐘，畫面會提示）；隨身碟與內建碟的邏輯磁區都必須是 512 bytes；
   隨身碟的 XBOOTLDR 與 ESP 掛得起來。任何一項不符就停止，**不會問序號**。
3. 列出內建 NVMe 的型號、容量與序號，要你在鍵盤上輸入**序號最後 4 碼**。輸入其他內容或直接 Enter 就取消，什麼都不寫。
   問序號前與確認後各檢查一次目標碟沒有在使用（掛載、swap、LVM／RAID／dm-crypt 的 holder），而且序號、容量與
   裝置號碼（major:minor）沒變；任何一項查不到（`/sys/block`、`lsblk`、`/proc/mounts`、`/proc/swaps` 讀取失敗）也一律停止，
   不當成「沒在使用」。
4. 先把目標碟原本的分割表（開頭與結尾各 1 MiB）存到隨身碟的 `/var/lib/gbos-install/backup/`。這只是分割表，不是你的資料。
5. 寫入映像自己的分割表；super（12 GB）從隨身碟複製，其餘分割區從 `pristine` 副本寫入。每個分割區寫完都**從碟上讀回**
   （direct I/O）再核對 SHA-256。隨身碟自己的 /data、metadata 等執行期狀態一概不複製。
6. userdata 放到碟尾（保留 manifest 裡的 type 與屬性）、清空開頭，第一次開機時 Android 自己格式化
   （從歡迎畫面開始，需要重新登入 Google 帳號）。
7. 重新產生所有分割區 GUID（隨身碟與內建碟不會重複），ESP 的 initrd 指向內建碟的 boot_a，三個開機選項只拿掉
   `nvme_init` 的封鎖（`module_blacklist`、`efi=noruntime` 不變），預設 quiet、選單編輯器關閉、`auto-firmware no`；
   隨身碟 XBOOTLDR 上的「Googlebook (internal disk)」改指向新的安裝，隨身碟選單的預設改成它。
8. **最後整體核對**才印 `INSTALL OK`：主、備兩份 GPT（CRC、備份在碟尾且與主表相同）、每個分割區的起點／大小／type／
   名稱／屬性與 manifest 相同（userdata 從新起點到最後一個可用磁區）、GUID 都是新的、`sgdisk -v` 回報 `No problems found`、
   /data 開頭為空，以及 ESP 的 initrd、kernel、開機選項、loader.conf 與隨身碟的 kernel、initrd、選單項目逐一讀回比對。

任何一步失敗（包括意外的指令錯誤）都會顯示原因並關機，結束碼非 0；已經開始寫入時會明說「內建碟不完整，請重跑安裝程式」。
畫面上的內容同時記在隨身碟的 `/var/log/gbos-install-<時間>.log`。

## 安裝程式開機選項的防護

安裝程式只在開機參數有 `gbos.install=1` 時啟動（只有那個選單項目有）。選單項目由 `gbos-install.entry.template` 產生：

- `module_blacklist`：Android 開機選項封鎖的所有模組（`builder/image.py` 的 `BL`，另含 `cros_ec_proto`），再加上
  `mei_me`、`8250_dw`、`intel_lpss_pci`、`intel_lpss`、`intel_lpss_acpi`、`i915`、`tpm_crb`。`nvme` 不封鎖。
- `initcall_blacklist`：由 `builtin-initcalls.txt` 產生（見下面）。
- `efi=noruntime`：不呼叫 EFI runtime services（不寫 EFI 變數）。
- `lockdown=integrity`：即使是 root 也不能用 `/dev/mem`、`/dev/port`、`iopl`／`ioperm` 或寫 MSR，
  用來擋住 flashrom internal 與 ectool 經 LPC 的存取路徑（這點沒有在實機驗證）。

這**不是**和 Android 選項相同的保護：維護系統跑的是 Arch 的核心，`module_blacklist` 只擋得住可載入模組，
擋不住編進核心（built-in）的驅動，而 Android 核心的實測結論也不能直接套用到 Arch 核心。
目前的維護核心（Arch 7.2.3-arch1-3）把 `tpm`、`tpm_tis`、`tpm_tis_core`、`tpm_crb`、`nvram` 編進核心。這幾個改用
`initcall_blacklist` 擋：`builtin-initcalls.txt` 列出每個 built-in 模組對應的 initcall（`tpm_init`、`init_tis`、
`crb_acpi_driver_init`、`nvram_module_init`，另外照 Android 選項保留 `mei_me_driver_init`）。`tpm_tis_core` 是函式庫，
沒有自己的 initcall，只有在 `tpm_init`、`init_tis`、`crb_acpi_driver_init` 都被擋住時才接受。所以：

- `setup-stick.sh` 讀 XBOOTLDR 上那個核心的 `/usr/lib/modules/<版本>/modules.builtin`：封鎖名單上的模組若是 built-in，
  而 `builtin-initcalls.txt` 沒有涵蓋，就拒絕；選單項目的 `initcall_blacklist` 由這個檔案產生，檔案本身也裝到
  `/usr/local/share/gbos-install/` 給安裝程式用。它也會核對映像本身三個選項封鎖的模組都在名單裡
  （`builder/image.py` 以後加了模組，這裡會擋下來），再把選項的整份 `module_blacklist` 寫成同一個目錄的
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

## 建立安裝碟（開發者）

```sh
# 1. 隨身碟的 Googlebook 分割區要先是某個映像（例如用 tools/harness/kano-delta-push.py 推上去）
# 2. 從同一個映像做 pristine 資料（在主機上）
install/stick/make-pristine.py googlebook-kano.img pristine/
# 3. 把 install/stick/ 與 pristine/ 複製到維護系統，在維護系統上以 root 執行
install/stick/setup-stick.sh pristine/
```

`setup-stick.sh`（要和 `gbos-install.entry.template`、`builtin-initcalls.txt` 放在同一個目錄）用 `gbos-install` 自己的核對程式檢查 pristine 資料，確認隨身碟上的 super 來自同一個映像，做上一節的
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
  Linux + KVM，需要 mtools 與 e2fsprogs，不需要 root。測試碟用的是**這個 repo 目前的**安裝程式：以 `debugfs -w`
  把 `gbos-install.sh`、服務、`builtin-initcalls.txt`、`module-blacklist.txt`（和 `setup-stick.sh` 一樣由 template 產生）
  與 pristine 資料寫進維護根目錄的副本並讀回核對，選單項目由同一個 template 產生。
  靜態檢查封鎖名單、`builtin-initcalls.txt` 與維護核心的 built-in 模組，接著跑七個情境：沒有 lockdown（拒絕）、
  `initcall_blacklist` 缺 TPM／nvram 的 initcall（拒絕）、`module_blacklist` 少了中間的 `spi_intel_pci`（拒絕）、
  super 被改一個 byte（序號前就拒絕）、取消、裝到有 12 個 Flex 分割區與資料的 NVMe、裝到空白 NVMe；取消與安裝時要看到
  安裝程式的開機參數與 built-in 檢查都通過。拒絕與取消時以 QMP 的寫入計數確認 NVMe 零寫入；
  兩次安裝都逐項比對分割表、內容與開機檔，最後只用 NVMe 開機，確認 Android 寫入 userdata。
  執行結果記在 `tests/stick/RESULT.md`。
