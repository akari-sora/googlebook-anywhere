# 開發用遠端實機 harness（KANO）

這些工具是開發這個專案時，讓開發主機（一台 Linux）遠端操作 KANO 用的：EC 電源與按鍵（Cr50 SuzyQ 的 EC console）、
AP 序列埠紀錄、systemd-boot 選單選擇、危險字串看守（EC 斷電）、攝影機截圖、增量推送映像、NVMe 安裝與更新、adb UI 操作。
這是開發者的操作工具，不是給一般使用者的安裝程式：它假設有一台跑著維護 Linux 的 KANO、一支專用的隨身碟，
以及用 `GBOS_ADB_PUBKEY` 建出來（有網路 adb）的映像。

| 工具 | 用途 |
|---|---|
| `kano-env.sh` | 所有設定的名稱與通用預設值，見下節 |
| `kano-env.local.sh` | （選用，不公開）你自己的值：IP、序號、金鑰、目錄 |
| `kano-ser.py` | EC console（allowlist）／AP UART 紀錄與送字 |
| `kano-boot.sh` | EC 斷電重開，依標題在 systemd-boot 選單選項目（可附加核心參數，要映像的選單編輯器打開） |
| `kano-gbos-watch.py`、`kano-gbos-watch-nvme.py` | 看守序列埠，出現危險字串就 EC 斷電（USB 版／NVMe 版） |
| `kano-delta-push.py`、`kano-delta-push-nvme.py` | 只推送變動的 1 MiB 區塊並逐塊回讀驗證 |
| `kano-autooff-image.py` | 讀 GPT／LP／vendor EROFS，拒絕 TEST autooff 映像 |
| `kano-nvme-install.sh`、`kano-nvme-update.sh` | 裝到內建 NVMe（需 `--go`）／更新 |
| `kano-ssh.sh` | 連到維護 Linux 的 ssh（所有腳本的 ssh 都經過它） |
| `kano-ui.sh` | adb + uiautomator：列出畫面元素、依文字點擊、截圖 |
| `kano-tone-find.py`、`kano-maint-*.sh` | 音訊與鍵盤背光的實機驗證 |

## 設定：`kano-env.sh` 與 `kano-env.local.sh`

每個設定都是一行 `: "${NAME:=值}"`，取第一個非空的值：

1. 環境變數；
2. `kano-env.local.sh`（和 `kano-env.sh` 放在同一目錄，選用）；
3. `kano-env.sh` 的預設值。

shell 腳本從自己所在的目錄 source `kano-env.sh`，它會先讀 `kano-env.local.sh`；Python 腳本用同樣的順序讀同樣格式的行
（只展開 `$HOME` 和前面已設定的名稱），所以兩邊的值一定相同。腳本之間互相呼叫一律用自己所在的目錄。

`kano-env.sh` 只有通用預設值：目錄在 `$HOME/kano-harness/` 底下，`adb` 從 PATH 找，pyserial 用系統的。
代表某一台機器、某一支隨身碟、某一顆碟的值**沒有預設**。需要它們的腳本在它們是空的時候，會在任何 ssh、adb、序列埠
或磁碟動作之前就拒絕（exit 2，訊息寫出缺哪個名稱）。空的序號尤其不能放行：它會和「沒有序號的碟」相符。

把自己的值寫進 `kano-env.local.sh`（同樣的行格式），或在執行前 `export`：

| 變數 | 用途 |
|---|---|
| `KANO_IP` | 維護 Linux 的 IP；增量推送的第三個參數會覆寫它 |
| `KANO_SSH_KEY` | 登入維護 Linux（root）的私鑰 |
| `KANO_ADB_ADDR`、`KANO_ADB` | Android 的 adb 位址（`host:port`，固定位址，不會掃描網段）與 adb 執行檔（預設 `adb`） |
| `KANO_ANDROID_ID_PROP` | 只有本專案 vendor 映像才有的屬性（`init.svc.kano_bootfix`），用來確認 adb 對面是 KANO |
| `KANO_RUNS`、`KANO_AP_UART_LOG`、`KANO_WORK` | 執行紀錄目錄、AP UART 紀錄檔、`kano-final<N>/` 建置產物目錄 |
| `KANO_NVME_SRC` | `kano-nvme-install.sh` 的來源：隨身碟上那個映像的建置產物目錄（`initrd.img`、`lapis*.conf`），沒有預設 |
| `KANO_PYSERIAL_ROOT` | 內含 `third_party/pyserial` 的目錄；空的就用系統的 pyserial |
| `KANO_CR50_SERIAL`、`KANO_AP_TTY`、`KANO_EC_TTY` | Cr50 序號與它的兩個序列埠（`/dev/serial/by-id/usb-Google_Inc._Cr50_<序號>-if01-port0`／`-if02-port0`） |
| `KANO_STICK_SERIAL` | 隨身碟序號（英數字） |
| `KANO_NVME_SERIAL`、`KANO_NVME_PCI`、`KANO_NVME_BOOT_A_GUID` | 內建 NVMe 的序號、PCI ID（`0x1234:0x5678` 格式）、boot_a 分割的 unique GUID |

例：

```sh
: "${KANO_IP:=192.0.2.10}"
: "${KANO_SSH_KEY:=$HOME/.ssh/id_kano}"
: "${KANO_CR50_SERIAL:=XXXXXXXX-XXXXXXXX}"
: "${KANO_EC_TTY:=/dev/serial/by-id/usb-Google_Inc._Cr50_${KANO_CR50_SERIAL}-if02-port0}"
```

私有 repo 保留開發者的 `kano-env.local.sh`；公開版不含這個檔案。
A clean personal-data scan covers the public export tree only, not the private checkout and not an unscanned installer image.

## 防呆（實際做了什麼，沒做什麼）

**`kano-ser.py`**：這是一般維護工具。`ec` 的 allowlist 是第二層，只限制哪些 EC 指令可以經由它送出。session 走
`kano-session-lib`，範圍更窄（不送 `apshutdown`、`powerbtn` 只能不帶時間、而且要先取得 session 的 hardware lock）。
這支 transport 不檢查 hardware lock。測試用的 `tests/harness/fixtures/kano-session-lib-v117.sh` 是 v11.7 的副本，只把範例網段換成文件專用的
`192.0.2.`（TEST-NET-1），sha256 釘在 `tests/harness/test_ser.py`。

`ec` 子指令只送 allowlist 裡的指令：`apshutdown`、`powerbtn [ms]`、`kbpress <col> <row> [0|1]`、
`version`、`battery`、`powerinfo`、`kblight`（不帶參數的讀取，或 `kblight <p>`，p 為 0..100 的標準十進位：無正負號、無前導零、非十六進位）、
`pwmduty`（讀全部 channel）、`pwmduty 0`（讀 channel 0，鍵盤背光）、`pwmduty 0 <d>`（d 為 `-1`，或同樣的 0..100；channel 1 是風扇，不是字面 `0` 的 channel 拒絕）、
`gpioset EC_KB_BL_EN_L 0|1`（GPIO 名稱大小寫必須與 `EC_KB_BL_EN_L` 完全相同，其他名稱拒絕）、
`sysinfo`、`cbi`（只有不帶參數的讀取；帶參數可能進入 CBI 寫入操作，因此一律拒絕）、
`gpioget [名稱]`、`lidclose`／`lidopen`／`lidstate`
（模擬闔上／打開上蓋：不寫 flash／CBI，只改 EC RAM 裡的上蓋狀態並送出和實體上蓋相同的事件，見 EC 原始碼 `common/lid_switch.c`；`lidopen` 在 AP 關機時會像實體開蓋一樣開機（等同 `powerbtn`）；模擬狀態會一直留到 `lidopen`、實際上蓋動作或 EC 重設。用完一定要 `lidopen`）。

新增形狀 `pwmduty`（含 `pwmduty 0` 與 `pwmduty 0 <d>`）、`gpioset EC_KB_BL_EN_L 0|1`、`kblight <p>`、`sysinfo`、`cbi`
只接受已經是正規形的原始字串：指令名稱小寫、token 之間單一 ASCII 空白、沒有前導或尾端空白。舊指令（`apshutdown`、
`powerbtn`、`kbpress`、`gpioget`、`lidclose`／`lidopen`／`lidstate`、`version`、`battery`、`powerinfo`、不帶參數的
`kblight`）仍把指令名稱折成小寫、把連續空白收成一個。

鍵盤背光的寫入（`pwmduty 0 <d>`、`gpioset EC_KB_BL_EN_L`、`kblight <p>`）只改 EC RAM。EC reset 會還原它們。
EC 在 AP suspend／resume 時也會自己重新驅動 `EC_KB_BL_EN_L`。

在開序列埠之前就檢查：任何控制字元（CR、LF、NUL、TAB…）、非 ASCII、不在清單的指令、不合格式的參數都拒絕，exit 2。
舊指令的名稱不分大小寫；新增形狀的名稱必須是小寫。參數維持原本的大小寫。EC 本身接受唯一前綴縮寫，所以前綴一律不放行。
pyserial 只在真的開序列埠時才載入。
`ap-send`、`ap-type`、`ap-keys`、`ap-spam` 寫到 AP UART（韌體、bootloader 選單、維護 shell），**沒有過濾**，送什麼就執行什麼。
`menu` 只把真正的倒數狀態列（`Boot in <n> s.`，單獨占一列）當成選單的結尾，所以標題裡含「Boot in」（例如
「Boot into Linux」）不會截斷選單、讓標題的歧義被漏掉。

**`kano-boot.sh`**：清空 AP UART 紀錄失敗就停止（不開機，避免用舊的選單決定按鍵）。附加核心參數時，先送 `e`，
等到這之後新寫入的 log 裡出現選單編輯器：游標到狀態列第 2 欄、畫出這個項目的選項（含 `名稱=值` 的參數），才送 `End`
和參數。映像預設 `editor no`，這時 `e` 沒有作用，後面的每個字都會被當成選單快捷鍵（`d` 設預設項目、`t`／`T`／`+`／`-`
改倒數、`f` 重開進韌體設定，都是寫在 SPI flash 的 EFI 變數），所以 10 秒內沒看到編輯器就 exit 3，一個字都不再送。

**`kano-delta-push.py`**（隨身碟）：寫入前用一次遠端呼叫取得目標碟的身分與布局，以下任一不符就拒絕，一個區塊都不寫：
維護系統的根目錄在 `/dev/sdX`、`TRAN=usb`、序號等於 `KANO_STICK_SERIAL`、容量不小於映像；隨身碟上的 GPT
（直接讀前 1 MiB、驗 CRC）對映像的每個分割都有相同編號、名稱、起訖，且核心的分割表與它一致；寫入範圍不碰映像以外的分割
（XBOOTLDR、kano-maint…），也不碰已掛載、swap 或被 dm/md 佔用的分割。讀不到 holders 目錄也算失敗。
每一次 `dd` 寫入前，在同一個遠端指令裡重新檢查（`write_guard`）：`TRAN`、序號、根目錄所在碟；GPT（前 1 MiB 的 SHA-256
要和檢查時讀到的相同，推送從不寫這 1 MiB）；這次寫入範圍涵蓋的每個分割在核心分割表的起點與大小；整顆碟和這些分割都
沒有掛載、swap 或被佔用。任何查詢失敗（讀不到 `/proc/self/mounts`、`/proc/swaps`、holders）都和不符一樣拒絕，
`dd` 不會執行。寫完逐 MiB 回讀比對 SHA-256。所有檢查都是明確的 `sys.exit`，不用 `assert`。

**`kano-delta-push-nvme.py`**（內建 NVMe）：只寫 super、vbmeta_a/b。另外要求：`/dev/nvme0n1` 是 `TRAN=nvme` 且序號相符；
目前跑的是維護系統，也就是根目錄在序號等於 `KANO_STICK_SERIAL` 的 USB 碟上（維護系統沒有更可靠的身分標記，這是代用條件），
NVMe 不是根目錄碟；NVMe 上沒有任何分割被掛載、當 swap 或被佔用；三個分割在 NVMe 的 GPT 裡各只有一個、起訖與映像相同，
核心分割表一致。每次寫入同樣附帶 `write_guard`；根目錄所在碟要被正面認出（`lsblk` 失敗或印出空值都拒絕，
不會因為「空字串不等於 nvme0n1」而放行），而且是序號相符的 USB 碟。

**`kano-autooff-image.py`**：給安裝與更新用的唯讀掃描。GPT 名稱用 kernel 的低 7 bits（`utf16_le_to_7bit`；kernel 另把非可列印字元換成 `!`，這個檢查器保留低 7 bits，ASCII 的 `super` 相同）。恰好一個 `super`；多於一個就拒絕。主表的 `my_lba` 必須是正在讀的 LBA，表項必須是 128 bytes。header／分割表 CRC 失敗才讀備援 GPT。LP 欄位檢查對齊 liblp，並套用 `AdjustMetadataForSlot`（`SLOT_SUFFIXED` 在 slot 0／1 加上 `_a`／`_b`；名字放不下，或 slot 2 以上仍帶這個旗標，就拒絕整份映像，不改選備援）。`DISABLED` 的 vendor 略過。`UPDATED` 不拒絕。表項超過 4096 或 tables 超過 1 MiB，而且這份副本 liblp 會接受，就拒絕整份映像，不改選備援。checksum 壞掉的主副本仍改讀備援。每一個 metadata slot 都掃（安裝器會留下 `androidboot.slot_suffix`，含 `_b`）。另一個 slot 留著舊的 TEST vendor 也會被拒。`vendor_b` 沒有 extent（lapis super 的空 slot）是正常的，不讀。EROFS 用 inode mode 走進目錄，並依釘死的 kernel `fs/erofs/erofs_fs.h`（`3938c8d97142`）處理 48-bit 根 nid（`0x80`）與 METABOX（`0x100`）。不認得的 incompat bit 拒絕。解析 `etc/kano-autooff.nonce` 與 `etc/init/kano-autooff.rc`（含目錄符號連結；`..` 走出 vendor 根目錄也拒絕）。解析到一般檔就拒絕。

**`kano-nvme-install.sh`**：每個遠端步驟都是一個 `set -euo pipefail` 的 `bash -s` 腳本，開頭先跑防呆
（NVMe 的 `TRAN`、PCI ID、序號；維護系統根目錄在指定序號的隨身碟；NVMe 沒有東西掛載、swap 或被佔用），
所以防呆和其後的 `blkdiscard`／`dd` 在同一次呼叫裡執行，中間重開機也溜不過去。「沒有掛載／swap／佔用」的查詢失敗
（`grep` exit 2、讀不到 holders 目錄）一律拒絕，不會當成閒置。來源是 `KANO_NVME_SRC`（必須設定）：第一個遠端呼叫之前，
`initrd.img`（bootconfig 的 checksum、boot_part_uuid 與 vbmeta digest 各一行）和三個開機項目（有
`initcall_blacklist=nvme_init,mei_me_driver_init`、`module_blacklist=`、`efi=noruntime`）就先複製到 run 目錄並檢查；
接著以唯讀掛載隨身碟的 ESP，`initrd.img` 和三個項目的 SHA-256 必須和來源相同（來源就是隨身碟上的那個映像），
否則在動 NVMe 之前停止。之後的 NVMe initrd 與項目都由 run 目錄裡的副本產生。每次執行建立新的
`$KANO_RUNS/nvme-install/run-<時間>-<pid>/`，每步的輸出寫在那裡；一步要 ssh exit 0 **且**這次的輸出裡有一整行是該步的標記（腳本最後一個指令，`set -e` 下才印得出來）才算通過，
否則立即停止。不再讀累積的 `install.log`，舊的成功標記不會被當成這次的結果。`blkdiscard` 之前先把 NVMe 的
前 1 MiB 與最後 1 MiB（主、備份 GPT）存到 run 目錄，和 KANO 上算的 sha256 比對，不符就停止。這只是 GPT 的備份，
**不是** NVMe 上資料的備份。寫入前會把檢查器送到維護系統，比對 SHA-256，並要求它印出剛好一行 `vendor clean`；`blkdiscard` 前再掃一次。接著確認 `sda1` 的 PARTLABEL 是 `userdata`，才以 `-o ro` 掛上（f2fs 仍可能補先前的 fsync）。只有 `lstat` 的 ENOENT 算沒有 `kano-autooff.nonce`；已有一般檔、符號連結或目錄，或列出根目錄／查找失敗，就拒絕。複製過來的 userdata 掛上之後清掉那個名字（`rm -f` 不跟隨符號連結；目錄在 python3 裡用 `shutil.rmtree` 刪除，`avoids_symlink_attacks` 必須為真，檔名不交回 shell）。卸載後再唯讀掛上，`lstat` 必須是 ENOENT，然後再卸載。這一步是這條複製路的安全保證：掛載失敗、根目錄列不出來、查找錯誤（errno 不是 ENOENT）、刪不掉、重新掛載、複查或卸載失敗，就停止，不再 `resize.f2fs`、`fsck.f2fs`，也不再寫 ESP。任何 lstat 發生 ENOENT 以外的查找錯誤就停止；刪除後及重新掛載後的確認，必須得到 ENOENT，成功查到任何物件也停止。vendor 掃描是縱深防禦。`dd` 完成之後、清除成功之前的任何停止（行程被殺掉、斷電、ssh 中斷）都可能留下檔案。

**`kano-nvme-update.sh`**：開始時刪掉同一版本上次留下的 `bootconfig.txt`、`initrd-nvme.sha256` 等產物；每一步檢查 exit status，
結果只從這次寫出的檔案讀。dry-run 之前與真正呼叫 `kano-delta-push-nvme.py` 之前各掃一次新映像，必須印出 `vendor clean`。直接呼叫推送程式不會掃描；它只寫 super 與 vbmeta，寫不了 userdata。推送前從 NVMe 讀出 boot_a 的 unique GUID（`sgdisk -i 13`，並確認名稱是 boot_a），
必須等於 `KANO_NVME_BOOT_A_GUID`。adb 只連 `KANO_ADB_ADDR` 這一個位址，而且只有對面有 `KANO_ANDROID_ID_PROP`
屬性時才送 `adb reboot -p`；更新後開機若沒認出這個 Android，就不再對那個位址送任何 `adb shell`。
遠端防呆和安裝一樣，查詢失敗就拒絕。bootconfig 傳輸失敗、遠端 ESP 改寫的任何檢查失敗、看守程式非 0 結束，都會讓整個更新失敗。

**`kano-ui.sh`**：在任何 `uiautomator dump`、`input tap`、`screencap` 之前，先確認 `KANO_ADB_ADDR` 對面有
`KANO_ANDROID_ID_PROP`，不是就 exit 3。

**`kano-gbos-watch.py`／`-nvme.py`**：
- 只看得到 AP UART 印出的東西，所以**只對 serial-debug 開機項有效**；quiet 項沒有序列埠 console，這時它會回報
  「unobservable」（exit 4），不會說「沒有危險」。
- 它是**輔助偵測，不是保證**：每秒輪詢一次 log，在危險字串印出之後才反應，之前或不印字的寫入擋不住。
  真正的保護是核心命令列的防呆參數與映像裡已移除的更新程式，看守程式負責確認這些參數真的在。
- 要在開機後、核心啟動前就啟動它。啟動時 log 裡若已經有核心命令列，那會被當成先前那次開機的，不算這次。
- 啟動後 120 秒內（`--observe-timeout`）必須看到這次開機的核心命令列，而且這段時間內 log 不能停止增長超過
  30 秒（`--stall-timeout`），否則 exit 4。過了這段時間後的安靜只會記一筆提示（Android 開完機本來就會變安靜）。
  整個觀察時間（第二個參數）比這更短、結束時還沒看到命令列，也是 exit 4，不會印「沒有危險」。所有時間參數都必須是
  大於 0 的有限數字（`--camera-every` 可以是 0 = 關閉），否則不啟動（exit 5）。
- 檢查的防呆參數來自 builder 用的機型設定 `profiles/kano/profile.toml`（加上全專案的 `profiles/_safety.toml`，用
  `builder/lib/devprofile.py` 讀取並核對，不執行 builder）：`blacklist.modules` 的每一個模組
  都要在這次開機最後一個 `module_blacklist=` 裡（核心只用最後一個）；`initcall_blacklist` 的每一個 initcall
  （NVMe 版除了 `nvme_init`，而且 `nvme_init` 不可以被封鎖）；`efi=noruntime`；不可以有 `spi-intel.*`。
  分段印出的長命令列（每段結尾 `\`）會先接起來。讀不到機型設定（harness 不在 repo 的 `tools/harness/` 裡，
  或設定不合法）就不啟動（exit 5）。
- 攝影機截圖在背景跑，超過 `--camera-timeout` 就殺掉，不會卡住偵測；EC 呼叫有 timeout，斷電後用 `powerinfo`
  確認 AP 已在 G3/S5。exit status：0 正常、2 危險字串（已斷電）、3 命令列缺防呆參數（已斷電）、4 unobservable、
  5 沒有啟動（參數不合法、沒設 `KANO_EC_TTY` 所以無法斷電，或讀不到機型設定）、6 偵測到危險但無法確認斷電
  （請手動斷電）。

**ssh 主機金鑰沒有固定**：`kano-ssh.sh` 仍是 `StrictHostKeyChecking=no` 加 `UserKnownHostsFile=/dev/null`，因為維護系統
每次重刷都會換主機金鑰。這代表 harness 不驗證 `KANO_IP` 上的主機身分；能防止寫錯碟的是上面那些在遠端檢查序號的防呆，
不是 ssh。若要固定金鑰，改 `kano-ssh.sh` 一處即可（所有 ssh 都經過它）。

## 已知限制

- 這些修改只在假的 KANO（見下節）上測過，還沒有在實機上重跑。
- `kano-nvme-update.sh` 仍讀 `kano-final<N>/kano-final<N>.img` 這種舊的建置產物布局，和 `builder/build.sh` 的
  新輸出路徑不同。
- 維護系統的身分只用「根目錄在指定序號的 Kingston 隨身碟上」判斷；Android 的身分只用 `KANO_ANDROID_ID_PROP` 判斷。

## 測試

```
tests/harness/run.sh                 # 全部：語法檢查、單元與整合測試、mutation check
tests/harness/run.sh --no-mutation   # 不跑 mutation check
```

測試不碰網路、序列埠、adb 或任何磁碟，也不含任何真實的序號、IP 或路徑（全部用假的測試值，經環境變數傳入）：`tests/harness/fakes/ssh` 把遠端指令放在暫存目錄裡的假 KANO 上執行
（假的 `lsblk`、`dd`、`blkdiscard`、`sgdisk`、`/sys`、`/proc`，磁碟是帶真 GPT 的映像檔）。`HOME` 是暫存目錄，
`KANO_IP` 與 `KANO_ADB_ADDR` 是 TEST-NET 位址。macOS 的 `/bin/bash` 3.2 與 Linux 都能跑；不能用 root 執行。
`tests/harness/mutate.py` 會在暫存複本裡把每個防呆逐一拿掉，確認對應的測試會失敗；加 `--classify` 會再區分
「真的會多寫／多送／多斷電」（effect）和「只有拒絕訊息不同、另一層防呆仍擋住」（message）。

個人資料的檢查（`test_no_personal_data.py`）不在 repo 裡存任何值或 hash：它讀 `tools/release/private-patterns.txt`
（`make-public.sh` 用的同一份清單，不公開），用同樣的 `grep -E` 掃 `tools/harness` 與 `tests/harness`；公開版沒有這份清單，
這個測試與對應的兩個 mutation 會跳過（mutation 植入的值也是執行時從私有檔讀的）。
