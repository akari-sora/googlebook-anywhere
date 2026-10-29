# 升級（保留資料）

裝好以後，新版本有兩種：

- **這個專案的新版本**，Google 的 build 不變，例如加了驅動、改了開機參數。
- **Google 的新 build**（新的 lapis recovery zip）。

兩種都用同一套更新方式：預期保留 `/data`（App、檔案、Google 帳號），不碰任何韌體；實際保留結果需自行確認。中途失敗時，原本的版本通常仍能開機。
唯一的例外是 ESP 連新舊兩份開機檔案都放不下、又加了 `--without-fallback`：見下面「ESP 放不下兩個版本」。

## 怎麼知道有新版本

Google 的 OTA 簽章仍可能通過，但推測無法完成更新，且會與本專案更新工具爭用 slot。v0.3.0 映像在啟動時停止 `update_engine`；2026-10-29 KANO keep-data 更新至 16658739 後 uptime 65 分鐘時讀回 stopped，未證明全程不曾啟動；全新安裝的設定精靈／GMS 反應仍未驗證。Play 商店的 App 更新不受此限制。查詢新版請在建置用的電腦上執行：

```sh
install/check-updates.py [--json]   # --hint 只給 install.sh / update.sh 用；--json 與 --hint 不能並用
```

它只讀兩份公開的小 JSON，不下載映像、不帶 cookie 或 token，User-Agent 只有 `googlebook-anywhere-check-updates`，逾時 10 秒：

- **Google**：`https://dl.google.com/dl/edgedl/device/recovery/production_recovery.json`（`base/builds/16452207.sh` 註明的 recovery 索引），找 `target` 為 `lapis-user` 的項目，`buildidentifier` 開頭的 build 編號比釘選的（`GBOS_LAPIS_BUILD`，預設 16452207）新時，印出 `newer Google build available: <新> (pinned <舊>)`。新 build 要等這個專案加上 `base/builds/<build>.sh` 才能建置（見「支援 Google 的新 build」）。
- **這個專案**：GitLab Releases API（`https://gitlab.com/api/v4/projects/aa22396584%2Fgooglebook-anywhere/releases`，公開、不需 token），取 `upcoming_release` 不是 true 的最大 `vX.Y.Z` `tag_name`，和這份 checkout 的 `docs/RELEASE-NOTES.md` 第 1 行比較。有新版時印出 `newer release available: <新> (this checkout <舊>)`，先 `git pull` 再更新。

兩份格式都在 2026-10-23 對照實際回應確認過。網路不通或格式不對時印出 `could not check`，結束碼仍是 0。

`update.sh` 開始建置前、`install.sh` 的最後摘要都會呼叫 `check-updates.py --hint`：只在有新版本時印一行 `NOTE (...)`，其他情況不印，也不會讓安裝或更新失敗。`--no-update-check` 或 `GBOS_NO_UPDATE_CHECK=1` 跳過（離線建置；這兩個只對直接執行的 `install.sh`／`update.sh` 有效，`docker-install.sh` 沒有轉發這個選項與環境變數）；設了 `CI` 時 `--hint` 不印任何東西。

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
   - 用保存在 `~/gbos-work/keys/vbmeta.pem` 的 AVB 金鑰與 `~/gbos-work/keys/apex-keys/` 的 APEX payload／container keyset 重建。AVB 金鑰旁的 `apex-keys/` 目錄是簽章身分的一部分；請和 AVB 金鑰放在一起、一起備份，每次更新都用同一對建置。若 `GBOS_AVB_KEY` 指到暫存目錄裡的複本，預設 keyset 目錄會跟著變、每次建置都會產生全新的 APEX keyset；請把 `GBOS_APEX_KEYS_DIR` 指到固定的持久目錄。v0.1.0 的工作目錄第一次執行時，會從 `base/keys/` 複製 AVB 金鑰；APEX keyset 則由首次啟用 IFMA 修補的建置建立並持續沿用。
2. `sudo install/write-usb.sh --keep-data <映像> <隨身碟>`：
   - 和全新寫入一樣的防呆：只接受 USB 整顆碟、不能是系統碟或使用中、要輸入序號；
   - 然後交給 `gbos-update.py`。

> **不要刪 `~/gbos-work/keys/`，也不要只備份 `vbmeta.pem` 而漏掉 `apex-keys/`**。遺失其中任何一組都會改變映像身分。`out/build-info.txt` 記下兩個 APEX 指紋（payload 公鑰 SPKI 與 container 憑證的 SHA-256）；更新前後比對，必須維持相同指紋。第一次從 Google 簽章的 APEX 更新到自行重簽的 APEX 尚未在 KANO 實機驗證；更新前應保存資料並記錄 build-info 中的兩個 APEX 指紋，後續建置必須維持相同指紋。若身分不連續，請停止 keep-data 更新並改做全新安裝。

### ESP 放不下兩個版本

Google 新 build 換了 kernel 時就是這種情況：KANO 的 ESP 是 64 MiB，kernel 約 27 MB，兩個版本的 kernel 加 initrd 放不下。
工具會拒絕並說明。這時加 `--without-fallback` 再執行一次，開機選單只剩新版本，而且順序是先刪舊檔案、再寫新檔案：

- **從刪掉舊檔案到切換選單之前，這顆碟自己的開機選單哪個版本都開不了**。工具停在這段期間時會明說（包括重跑時又停下）；
  /data 不受影響，再跑一次同一個指令就會寫完（隨身碟是在另一台電腦上更新的，所以一定能重跑）。
- 只有新的開機檔案還放得進舊檔案旁邊、只差兩個 previous-version 選項的幾 KiB 時，才會先寫新檔案、切換之後再刪舊檔案；
  實際上幾乎不會遇到。

想退回時，用舊的映像再跑一次 `write-usb.sh --keep-data`。舊映像在 `~/gbos-work/build.prev/`。

## 內建碟使用者（安裝碟）

內建碟上的 Googlebook 要用安裝碟更新，不是 `write-usb.sh --keep-data`（那個指令會拒絕多了 XBOOTLDR 與 kano-maint 的碟）。2026-10-29 已在 KANO 由 16452207 slot a keep-data 更新至 16658739 slot b；套件清單、帳號數、設定、鎖定狀態及主要使用者檔名比對相同，個人檔案內容、逐項 App 資料／登入有效性未驗證，見 [驗收紀錄](VERIFICATION-v0.3.0.md)；先備份並保留原有 AVB 與 APEX 簽章身分，確認後再執行：

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

2026-10-29 的失敗候選曾兩次退回舊版並保留資料；成功更新至 16658739 後再退回仍未驗證。新版可能已升級 `/data`，保留 previous-version 選項不保證舊版可開機。

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

本樹也提供 16658739（CL3B.260622.276.R4）的獨立 pins。選它時，整個 base 與裝置建置都要使用相同的
`GBOS_LAPIS_BUILD=16658739`，例如：

```sh
GBOS_LAPIS_BUILD=16658739 GBOS_APEX_KEYS_DIR=/path/to/existing/keys/apex-keys \
  install/install.sh --work /path/to/work --key /path/to/existing/keys/vbmeta.pem --update
```

AVB 與 APEX 金鑰必須沿用既有安裝的身分。切換 Google build 會使 base 快取失效，即使 base 原始碼沒有改變；
`--update` 會保存上一份 base，再依選定的 pins 重建。16658739 保留相同 kernel／開機參數；AVB rollback index
從 1339 更新到 stock 的 1369，分割區 UUID、fingerprint、security patch 與 APEX salts 從該 build 的原始映像核對。
KANO 的這次 keep-data 更新已有上述有限實機證據；其他機器仍須按上述跨 Google build 的驗證步驟進行，不能以軟體建置或 QEMU 取代。

實驗性螢幕亮起修正另須明確設定 `GBOS_EC=drain`；只選 16658739 不會啟用 drain。它不能和其他 EC 功能並用，休眠失敗恢復可能重設 AP 並丟失資料。先決條件見 [安全說明](SAFETY.md)，實機結果見 [v0.3.0 驗收紀錄](VERIFICATION-v0.3.0.md)。

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
- `tests/update/check-updates-test.py`：`check-updates.py` 與 `update.sh` 提示的離線測試（`tests/update/fixtures/updcheck/` 是兩份公開 JSON 的精簡複本，加上較新／較舊的項目；網路錯誤與逾時用本機 socket）。`GBOS_UPDCHECK_LIVE=1` 時多一項讀真正的兩個網址，預設跳過。`install.sh` 摘要裡的提示在 `tests/install/install-sh-test.sh` 測。
- `tests/update/mutate.py`：把 `gbos-update.py` 列出的 25 項防護逐一在暫存複本裡拿掉，每一項都必須讓 selftest 至少一項檢查失敗（改壞後無法編譯、或 selftest 沒有任何一項失敗就崩潰，都算這個清單的錯誤）。不在清單上的：只在寫入程式本身出錯時才會觸發的事後檢查，以及要真的 block device 才能測的 sysfs 路徑檢查（`tests/install/run.sh` 用 loop 裝置測）。
- `tests/update/qemu-update.py <空目錄> <N> <N+1>`：真的映像，Linux＋KVM。依序：
  1. N 第一次開機（/data 格式化）；
  2. 更新；
  3. 韌體 → ESP → systemd-boot 開預設（slot b）；
  4. slot b 的 serial-debug 開機：掛載的是 `system_b` 等、/data 沒有重新格式化、f2fs UUID 不變；
  5. previous version（slot a）開機：同上；
  6. 在 systemd-boot 選單用按鍵選「(previous version)」。
