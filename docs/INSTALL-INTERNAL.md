# 裝到內建 NVMe（取代原本的系統）

有兩種做法：

- **安裝碟**（`install/stick/`）：在 KANO 上從隨身碟選單選「Install Googlebook to the internal disk」，確認序號後全新安裝。
  需要帶維護系統的隨身碟，見 `install/stick/README.md`。
- **harness 遠端安裝**（下面這份紀錄）：開發機透過 ssh 操作維護系統，連隨身碟上的 /data 一起搬過去。

> 這份是**開發者在 KANO 上做過一次的操作紀錄**，不是一般使用者可以照抄的安裝程式。它需要：
> 隨身碟上另外裝的「維護 Linux」（不在這個 repo 裡）、harness 用的序號與 IP（`tools/harness/kano-env.sh`），以及用開發者選項
> 建出來的映像（`GBOS_ADB_PUBKEY`、`GBOS_LOADER_EDITOR=yes`、serial-debug 預設；`kano-nvme-update.sh` 與 `kano-ui.sh` 都靠 adb）。

**這會清掉內建碟上的一切**（例如 ChromeOS Flex 與它的使用者資料）。先確定 USB 版本在你的機器上一切正常，並自行備份資料。

流程在 `tools/harness/kano-nvme-install.sh`（需要 `--go` 才會寫入）：

0. 來源是必須設定的 `KANO_NVME_SRC`（隨身碟上那個映像的建置產物目錄）。第一次連到 KANO 之前就核對全部本機輸入
   （initrd、bootconfig、三個開機選項），清碟之前再確認來源和隨身碟 ESP 上的是同一個映像；不符就什麼都不寫。
1. 從隨身碟開一個「放行 NVMe、其他封鎖不變」的維護 Linux（`initcall_blacklist` 與 `module_blacklist` 拿掉 nvme）。
2. 備份目標碟原本的 GPT（開頭與結尾各 1 MiB）到本機，以 SHA-256 確認備份完整，失敗就停止。這只保住分割表，不是使用者資料備份。
3. 防呆與寫入在同一次遠端執行裡完成：目標必須是 `TRAN=nvme`、PCI ID 與序號相符的內建碟，不是維護系統的根目錄碟、沒有掛載、
   沒有 holder；任何一項不符就在寫入前停止。每一步只看這一次執行的結果，不看舊的紀錄。
4. `blkdiscard` 後把 USB 碟上 Googlebook 的範圍（到最後一個 Googlebook 分割為止）`dd` 到 NVMe，`cmp` 全段比對。
5. `sgdisk -e`（備份表頭移到碟尾）、刪掉維護系統的分割、userdata 移到碟尾並 `resize.f2fs`、`sgdisk -G` 重新產生所有 GUID
   （避免與隨身碟重複；Android 用 `androidboot.boot_part_uuid` 找自己的碟）。
6. NVMe ESP 的 initrd 改成 NVMe 的 boot_a GUID，開機選項拿掉 `nvme_init` 封鎖；隨身碟的 XBOOTLDR 另放一份
   「internal NVMe」選項。
7. 實機開機驗證：by-name 連結指向 `nvme0n1`、`/data` 在 nvme0n1p1。

之後的更新用 `tools/harness/kano-delta-push-nvme.py`：只寫 super、vbmeta_a/b 的變動區塊，ESP initrd 只換 bootconfig
（`tools/harness/kano-nvme-update.sh`，會先從目標碟讀 boot_a GUID 核對）。
