# 升級（保留資料）

裝好以後，新版本有兩種：

- **這個專案的新版本**，Google 的 build 不變，例如加了驅動、改了開機參數。
- **Google 的新 build**（新的 lapis recovery zip）。

兩種都用同一套更新方式：`/data`（App、檔案、Google 帳號）保留，不碰任何韌體；中途失敗時，原本的版本仍能開機。
唯一的例外是 ESP 連新舊兩份開機檔案都放不下、又加了 `--without-fallback`：見下面「ESP 放不下兩個版本」。

## 原理：兩個 slot

映像本來就有 Android 的 A/B 結構：

- super 裡的動態分割區：`system_a`／`system_b`、`vendor_a`／`vendor_b`……；
- 實體分割區：`boot_a`／`boot_b`、`vbmeta_a`／`vbmeta_b`……。

發行的映像只填 slot a。`install/gbos-update.py` 把新版本寫進**預設不會開的那個 slot**，正在用的 slot 一個 byte 都不動：

1. super：新版本的動態分割區寫進空間（舊的那個 slot 的分割區讓出來），再改寫 LP metadata。正在用的 slot 的位置不變；寫入前先檢查，寫入後讀回比對。
2. 實體分割區：`boot`、`init_boot`、`vendor_boot`、`vbmeta`、`pvmfw`、`recovery`、`ota_recovery` 寫進目標 slot，每個都讀回比對。
3. ESP 上的開機檔案：
   - 目標 slot 的 initrd 是 `/gbos/<slot>/initrd.img`：新版本的 ramdisk 用 lz4 壓縮，加上它的 bootconfig。bootconfig 裡的 `androidboot.slot_suffix` 改成這個 slot，`boot_part_uuid` 改成這顆碟的 boot_a。
   - kernel 一樣就共用，不一樣才另放 `/gbos/<slot>/kernel.efi`。
4. 開機選單：
   - `lapis-quiet.conf` 等選項改指向新的 slot；
   - 原本的選項改名為 `prev-*.conf`，標題後面加「(previous version)」；
   - ESP 放得下兩個版本時才保留；Google build 相同時 kernel 共用，放得下。

寫入的順序（每一步都會印出來）：state 檔 →（從安裝碟更新內建碟時：先處理隨身碟選單，見下面）→ 刪掉目標 slot 舊的選項與檔案
→ 動態分割區 → 實體分割區 → LP metadata → 新的開機檔案 →（安裝碟：隨身碟選單改開新版本）→ 切換開機選單 → 刪掉 state 檔。
在切換之前，沒有任何選單項目會開正在寫的那個 slot。

永遠不寫：

- userdata、metadata、misc、frp_persist、desktop_security_persist、desktop_security_storage；
- 分割表；
- 其他所有分割區。

工具內有寫入防護：寫到這些範圍會在寫之前失敗。

開始寫之前，以下情況會拒絕，一個 byte 都不寫：

- 碟上的 AVB 金鑰和新版本不同（會讓 KeyMint 的金鑰、螢幕鎖失效）；
- 機型不同；
- KeyMint／Gatekeeper 的選擇不同；
- Google build 比碟上的舊；
- 分割表不同（需要全新安裝）；
- 新版本多了動態分割區（需要全新安裝）；
- super 放不下，或超過 super 裡這個 slot 的群組上限；
- 映像和它的 `.sha256` 不符；
- ESP（或安裝碟的 XBOOTLDR）的 FAT 標示的大小超過它的分割區（mtools 會寫到下一個分割區）；
- 打開的裝置和確認過的不同：major:minor（`--rdev`），以及 `write-usb.sh` 另外傳的 sysfs 路徑（`--syspath`）與容量（`--bytes`）。

中途停下來時（斷電、拔線、讀回不符），ESP 上的 `/gbos/update-state.txt` 記著正在寫哪個 slot。這時：

- 原本的版本仍是預設（`--without-fallback` 的例外見下面）；
- 再跑一次同一個更新，會從那個 slot 重新寫完；
- `rollback` 會拒絕，要先把更新跑完。

## 隨身碟使用者（Linux／WSL）

用**當初建置的同一個工作目錄**，裡面有你的 AVB 金鑰：

```sh
git pull
install/update.sh --work ~/gbos-work --disk /dev/disk/by-id/usb-<你的隨身碟>
```

`update.sh` 依序做兩件事：

1. `install.sh --update`：
   - 原始碼變了，舊的 base 或映像就移到 `base.prev`／`build.prev`；
   - 用保存在 `~/gbos-work/keys/vbmeta.pem` 的金鑰重建。v0.1.0 的工作目錄第一次執行時，會從 `base/keys/` 複製這把金鑰。
2. `sudo install/write-usb.sh --keep-data <映像> <隨身碟>`：
   - 和全新寫入一樣的防呆：只接受 USB 整顆碟、不能是系統碟或使用中、要輸入序號；
   - 然後交給 `gbos-update.py`。

> **不要刪 `~/gbos-work/keys/`**。換了金鑰的映像無法更新已安裝的系統，只能全新安裝，資料會清掉。

### ESP 放不下兩個版本

Google 新 build 換了 kernel 時就是這種情況：KANO 的 ESP 是 64 MiB，kernel 約 27 MB，兩個版本的 kernel 加 initrd 放不下。
工具會拒絕並說明。這時加 `--without-fallback` 再執行一次，開機選單只剩新版本，而且順序是先刪舊檔案、再寫新檔案：

- **從刪掉舊檔案到切換選單之前，這顆碟自己的開機選單哪個版本都開不了**。工具停在這段期間時會明說（包括重跑時又停下）；
  /data 不受影響，再跑一次同一個指令就會寫完（隨身碟是在另一台電腦上更新的，所以一定能重跑）。
- 只有新的開機檔案還放得進舊檔案旁邊、只差兩個 previous-version 選項的幾 KiB 時，才會先寫新檔案、切換之後再刪舊檔案；
  實際上幾乎不會遇到。

想退回時，用舊的映像再跑一次 `write-usb.sh --keep-data`。舊映像在 `~/gbos-work/build.prev/`。

## 內建碟使用者（安裝碟）

內建碟上的 Googlebook 要用安裝碟更新，不是 `write-usb.sh --keep-data`（那個指令會拒絕多了 XBOOTLDR 與 kano-maint 的碟）：

1. 在自己的電腦建出新映像。用**同一個**映像跑 `install/stick/make-pristine.py`，再跑 `install/make-installer-stick.sh`（`--root` 與 `--xbootldr` 見 `install/stick/README.md`）。維護系統會寫進新的安裝碟檔，不必在隨身碟上再跑 `setup-stick.sh`。
2. `sudo install/write-usb.sh <安裝碟檔> /dev/disk/by-id/usb-<你的隨身碟>`。不要加 `--keep-data`。
3. 從隨身碟選單選「Update Googlebook on the internal disk (keep data)」。

手上已經有一支維護系統隨身碟、只想換其中的 Googlebook 分割區時，開發者仍可用 git repo 裡的 `tools/harness/kano-delta-push.py`（發行包沒有這個工具），然後重做 pristine 與選單。那不是上面這條一般流程。

這個選項和安裝程式用同一套開機防護：

- lockdown、`efi=noruntime`；
- 完整的 module_blacklist、built-in 驅動的 initcall 封鎖。

執行時：

1. 先顯示內建碟目前的版本與 slot；
2. 要你輸入序號最後 4 碼；
3. 核對隨身碟上的新版本（super 的 SHA-256 等），再寫入另一個 slot。

隨身碟選單也會跟著更新：

- 寫入任何 slot 之前：隨身碟上的「Googlebook (internal disk)」與「Googlebook (internal disk, previous version)」都改開
  正在用的版本，用的是隨身碟上的複本（`/gbos-nvme/<slot>/`，從內建碟本身複製；隨身碟原本的複本可能比較舊，例如內建碟
  曾用另一支安裝碟更新過）。指向目標 slot 的項目與檔案先刪掉；
- 新 slot 寫完、讀回比對之後：「Googlebook (internal disk)」改開新版本，「(previous version)」開原本的版本；
- 中途停下時：隨身碟上的「Googlebook (internal disk)」開的是完整的版本（只差最後一步時是新版本，否則是原本的版本），
  再選一次這個選單項目就會寫完；
- 更新完成後，隨身碟選單的預設設成「Googlebook (internal disk)」。

內建碟的 ESP 放不下兩個版本時（Google 換 kernel 的新 build），內建碟的更新**一律**先刪舊檔案（不需要
`--without-fallback`）：從那時到切換選單之前，內建碟自己的選單開不了任何版本，只能從這支安裝碟的
「Googlebook (internal disk)」開機。

有兩支安裝碟時：用其中一支更新過內建碟之後，另一支上的「Googlebook (internal disk)」與「(previous version)」還是舊的
複本，開了會用舊的 kernel／initrd 開已經換過的 slot。不要直接開那支安裝碟上的這兩個選項；先用它跑一次更新（或
`rollback --stick-boot`，它會拒絕不一致的複本），讓它重新同步。

## 退回上一版

- **開機選單**：選「Googlebook KANO quiet (previous version)」，就是更新前的那個 slot，用的是同一份 /data。
- **改成預設**：`gbos-update.py rollback <碟> [--rdev MAJ:MIN] [--stick-boot <安裝碟的 XBOOTLDR>] [--allow-build-change]`
  （需要 root，碟上的系統不能在執行中）。它把「目前」與「previous version」兩組選項互換，什麼分割區都不寫。之後的更新會寫進換下來的那個 slot。
  - block device 一定要給 `--rdev`：先用 `lsblk -dno MAJ:MIN <碟>` 確認是哪顆碟；
  - `--stick-boot`：安裝碟上的「Googlebook (internal disk)」與「(previous version)」也跟著互換；隨身碟的複本不是這顆碟的開機檔案時（例如之後用別支安裝碟更新過）會拒絕；
  - 更新做到一半時拒絕（要先跑完）。
- **Google build 不同時**：新版本開過一次以後，Android 已經把 /data 升級了。舊版本不一定能用這份 /data 開機（Android 不支援降級）。只在新版本完全開不起來（還沒碰過 /data）時，退回才可靠；否則只能全新安裝。更新時畫面會提醒這一點，`rollback` 也會拒絕，除非加 `--allow-build-change`。

## 支援 Google 的新 build（維護者）

每個 Google build 的釘選值在 `base/builds/<build>.sh`：

- recovery zip 的網址與 SHA-256；
- 裡面映像的 SHA-256 與大小；
- 每個中間產物的 SHA-256；
- vendor／system_ext 分割區大小。

`base/build-base.sh` 與 `install/install.sh` 讀 `GBOS_LAPIS_BUILD`（預設 16452207）選擇其中一個。

新增一個 build 的步驟：

1. 從 Google 的 recovery 索引（`production_recovery.json`）取得新 zip 的網址與 SHA-256，複製一份 `base/builds/16452207.sh` 改名。
2. 用 `build-base.sh --allow-drift` 跑一次。
   - 看每個 WARNING：哪個中間產物變了、為什麼變（kernel、ramdisk、分割區大小）。
   - 確認沒問題後，把新的 SHA-256 填進新檔。
   - 分割表若和舊 build 不同，現有安裝無法 A/B 更新，`gbos-update.py` 會拒絕並要求全新安裝。這件事要寫進發行說明。
   - `builder/image.py` 用 SHA-256 釘住 build 16452207 的 vendor_boot 開機參數，`tests/builder/profile-test.py` 的 `VCMD` 也是
     這份參數。新 build 的參數不同時，建置會在這裡停下（fail closed）：逐項確認差異（特別是 console、SPI、看門狗相關的參數，
     以及 `_safety.toml` 的 `stock_drop` 是否還涵蓋），再更新這兩處。
3. 跑完整測試：
   - `tests/builder/e2e.sh`（新使用者流程與 QEMU 自我測試）；
   - `tests/update/qemu-update.py <空目錄> <上一版映像> <新映像>`：舊版開機 → 更新 → 新 slot 開機、/data 同一個檔案系統 → previous version 開機；
   - 實機：先在隨身碟驗證，再更新內建碟。
4. 確認 KeyMint／Gatekeeper 的選擇（bootconfig 的 `androidboot.vendor.apex.*`）沒變。變了的話，更新會被拒絕：換 KeyMint 會讓已安裝系統的金鑰與螢幕鎖失效，要另外設計遷移。

## 測試

- `tests/update/selftest.py <空目錄>`：小型合成碟，不需要 root 或 QEMU。涵蓋：
  - 更新、第二次更新（換 kernel）、退回（含跨 Google build、安裝碟選單一起互換）；
  - 每一種拒絕（碟不變）；
  - 每一步中斷後重跑；內建碟的第二次更新在每一步中斷時，兩邊的選單都沒有項目開正在寫的 slot；
  - 內建碟模式與安裝碟選單（含兩支安裝碟輪流更新）；
  - ESP 放不下時的兩種順序與中間的視窗；
  - 寫入防護。
- `tests/update/mutate.py`：把 `gbos-update.py` 列出的 25 項防護逐一在暫存複本裡拿掉，每一項都必須讓 selftest 至少一項檢查失敗（改壞後無法編譯、或 selftest 沒有任何一項失敗就崩潰，都算這個清單的錯誤）。不在清單上的：只在寫入程式本身出錯時才會觸發的事後檢查，以及要真的 block device 才能測的 sysfs 路徑檢查（`tests/install/run.sh` 用 loop 裝置測）。
- `tests/update/qemu-update.py <空目錄> <N> <N+1>`：真的映像，Linux＋KVM。依序：
  1. N 第一次開機（/data 格式化）；
  2. 更新；
  3. 韌體 → ESP → systemd-boot 開預設（slot b）；
  4. slot b 的 serial-debug 開機：掛載的是 `system_b` 等、/data 沒有重新格式化、f2fs UUID 不變；
  5. previous version（slot a）開機：同上；
  6. 在 systemd-boot 選單用按鍵選「(previous version)」。
