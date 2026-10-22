# 做了哪些修改、為什麼

每一項都在 KANO 實機上驗證過；括號內是 `builder/vendor.sh` 開頭註解裡的版本代號（safeN）。

## 建置流程

1. `base/build-base.sh`：官方 recovery 映像 → 基礎 GPT 映像（中間產物，不能直接開機；ESP 放 systemd-boot、`kernel.efi`、`initrd.img`），
   vendor 換成軟體 KeyMint／Gatekeeper，`vbmeta_a` 用你的金鑰重簽。用原本的金鑰重建時，輸出與 2026-10-11 手工做出的映像逐位元組相同
   （`base/RESULT.md`）。
2. `builder/fetch-inputs.sh`：機型設定（`profiles/kano/profile.toml` 的 `[[firmware]]`）列的韌體（linux-firmware 20260810、
   sof-bin v2025.12.2）與 ChromeOS 音效設定（board-overlays 固定 commit），每個檔案都核對 SHA-256。
3. `builder/vendor.sh`：解開 vendor EROFS，套用下面所有修改（機型相關的部分來自機型設定與它的 `vendor.py`），用 otatools 的 mkfs.erofs 重建（固定時間戳與 UUID，uid／gid／mode／
   SELinux 標籤從原映像逐一抄寫），再比對新舊 vendor 樹：新增、刪除、變更的檔案必須剛好是預期的清單。接著加 hashtree、重簽 vbmeta，
   更新 ESP initrd 的 bootconfig。
4. `builder/image.py`：ESP 的 initrd 與兩個開機選項（quiet、serial-debug；vendor_boot 的原廠參數＋機型設定＋`profiles/_safety.toml`，由
   `builder/lib/devprofile.py` 組合），然後兩個 QEMU 自我測試：
   - 功能測試：把 kernel、initrd 交給 QEMU，但先拿掉 debug 選項裡的 `console=`／`earlycon=`，改成 COM1 的
     `console=ttyS0` 與 I/O `earlycon`。QEMU 沒有 KANO 的 MMIO UART。這是 Android 功能測試，不是原樣 debug 開機，也不能代替實機 UART。
   - 開機鏈測試：不給 kernel，讓韌體從映像自己的 ESP 開 systemd-boot，跑預設選項，選項不做改寫。只在預設是 quiet 時跑。
     quiet 沒有序列埠輸出，所以改看磁碟：QEMU 在時限結束時仍在執行、整顆隨身碟累計寫入 16 MiB 以上，而且最高的寫入位置落在 userdata 內
     （只有 Android 掛上 /data 才會寫到那裡）。
   全部通過才把 `<name>.partial` 改名成正式檔名。

`vendor.sh` 在完成映像前執行 `builder/kmod/check-firmware.py`：離線讀取最後 vendor 樹（含額外／自建模組）、
原廠 vendor_dlkm／system_dlkm 與 ESP initrd 的每個 `.ko` 的 `.modinfo`，逐一核對 `firmware=`（也就是
`modinfo -F firmware`／`MODULE_FIRMWARE`）與映像實際安裝的 `/vendor/firmware`，不查主機或下載快取。
必要模組是 profile 的 `kmods`、`wifi.modprobe`、`[modules].load/unblock`，以及 builder 新增／替換的 vendor 模組
（與原始 vendor 樹比對，含 `builder/kmod` 與 hook 新增的模組；`GBOS_DEV_KERNEL_DIR` 的 `lib/dev-modules` 則依 profile 選取）、啟用 `pci_stub_ids` 時要加入 initrd 的 `pci-stub`。
這些模組缺少必要檔案會停止建置，訊息列出 profile、模組與韌體名稱；萬用字元至少要找到一個非空檔案。
其餘原廠模組只報告，缺韌體或模組 metadata 錯誤都不會擋住建置；不因檔案存在、黑名單或間接依賴就提升為必要。
數字版本不會自動視為相容：若驅動確實會嘗試一段版本，profile 可加 `[[firmware_check.alternatives]]`，指定
`module`（精確名稱，連字號改底線）、`firmware`（宣告名稱或 glob）、`files`（可接受路徑／有限版本 glob 的陣列）
與 `reason`。`[[firmware_check.optional]]` 用同樣的 `module`、`firmware`、`reason` 明列可缺少的檔案；空白理由不接受。
例如 `firmware = "iwlwifi-so-a0-gf-a0-99.ucode"` 可在有驅動版本範圍依據時，配
`files = ["iwlwifi-so-a0-gf-a0-8[3-9].ucode", "iwlwifi-so-a0-gf-a0-9[0-9].ucode"]`。
optional／alternatives 規則只套用必要模組；若規則點名了屬於 report-only 的模組，不會把該模組升為必要，但會在建置 log 印出一行警告並記入報告的 `warnings`。
結果（每個模組的 `scope`、`required_by`、來源樹相對路徑、逐檔狀態，含 optional 理由及替代檔）寫入 `<GBOS_OUT>/firmware-report.json`；optional、未使用規則警告與一行 `firmware report-only:` 原廠模組缺檔摘要也印在建置 log，`firmware check:` 結果行並同時印出必要模組數與必要韌體宣告數（例如 `required: 4 modules, 0 firmware declarations`）。

正向檢查的 `scope` 不變；`check-firmware.py` 另建一個載入閉包，對 profile 的每筆 `[[firmware]]` 釘選（`path = …`）執行**反向檢查（reverse check）**。閉包裡的模組是「樹＋相對路徑」的檔案（例如 `vendor_dlkm:lib/modules/xe.ko`），不是 basename：
- 起點只來自 loader 真正讀的檔案，且只在各自的位置，每個起點都記下由誰載入：`vendor_dlkm`、`system_dlkm` 的 `lib/modules/modules.load`（Android init 載入；該樹自己的 `modules.blocklist` 只對這些起點生效）；initrd 的 `lib/modules/modules.load` 依**最終映像**計算，也就是 stage 2 `builder/image.py` 改寫後的清單（`first_stage_drop` 刪掉的名稱不算、`pci_stub_ids` 非空時加上 `pci-stub.ko`；兩邊共用 `devprofile.stage2_modules_load()`），first-stage init 其實會套用 initrd 自己的 `lib/modules/modules.blocklist`（AOSP android-15.0.0_r1 `init/first_stage_init.cpp` `LoadKernelModules()` 建構 `Modprobe m({dir_path}, …)` 時沿用 `libmodprobe/include/modprobe/modprobe.h` 的預設 `use_blocklist = true`），checker 不讀這份檔案，是近似（見下方已知缺口），`vendor_dlkm`／`system_dlkm` 的 blocklist 則不約束 initrd；profile 的必要模組各在自己的樹找（`kmods` 在 `vendor:lib/kano`、`wifi.modprobe` 在 `vendor_dlkm:lib/modules`、`[modules].load/unblock` 依 `profiles/_safety.toml` 該列的 `vendor`（`vendor_dlkm:lib/modules`）或 `dev`（`vendor:lib/dev-modules`）、`pci_stub` 在 `vendor_dlkm` 與 initrd，但只在 `pci_stub_ids` 非空時）、builder 新增／替換的那個 vendor 檔案，以及 builder 自己寫在 `vendor:etc/init/kano-hw.rc` 的 `modprobe --dirname=/vendor/lib/{modules,kano,dev-modules} <模組>`；這些 builder 的 `modprobe` 都沒有 `-b`，不套用 stock blocklist。`lib/dev-modules` 的其他模組不算載入。
- 依賴只沿這些目錄自己的 `modules.dep`（`vendor_dlkm`、`system_dlkm`、initrd 的 `lib/modules/modules.dep`，`vendor:lib/kano/modules.dep`、`vendor:lib/dev-modules/modules.dep`）逐檔展開；其他位置同名的 `modules.load`／`modules.dep` 一律不讀。loader 點名的模組在 `modules.dep` 沒有 head、而同一目錄有兩個同名檔案時視為歧義，檢查失敗（閉包輸入錯誤，見下方「v0.2.3：反向檢查只是 advisory」的退出碼）。這是近似：沒有 head 時退回用檔名找（libmodprobe 不會載入）、softdep 與 ueventd 的 modalias 載入不計入。
- initrd 是串接的 newc，依 Linux v6.18 `init/initramfs.c`（`do_name()`／`maybe_link()`／`free_hash()`）重放：同一路徑以最後一筆為準；`nlink >= 2` 且 device 與 inode 相同的 regular 項目共用一個 inode（每個 archive 的 trailer 清空連結表），之後寫入的資料對每個名稱都生效，零長度的 placeholder 取得後來的資料；被覆寫的模組或清單不算。initrd 項目名稱必須是相對路徑、不含 `..`，否則直接失敗（訊息只列項目序號）。
- 核心拒絕的模組（profile `[blacklist].modules` 與 builder 寫進每個選項 `module_blacklist=` 的名稱）不論誰載入都不在閉包內，`modules.dep` 需要它的模組（一層層往上）也一樣無法載入；只有單一選項封鎖的（例如 `amd-hx370` safe 選項的 `quiet_add = ["amdgpu"]`）不算封鎖。`vendor_dlkm`／`system_dlkm` 自己 `modules.blocklist` 上的模組（或依賴它的模組）只從該樹的 `modules.load` 起點刪除（libmodprobe `IsBlocklisted()`：android-15.0.0_r1 `libmodprobe/libmodprobe.cpp` 對模組本身與 `GetDependencies()` 列出的每個依賴都查 blocklist；round 3 DeepSeek M2「比 `IsBlocklisted()` 嚴格」的讀法經核對不成立），builder 的 `modprobe` 與 initrd 照樣載入。lapis 的 second-stage loader 是否帶 blocklist 旗標不在此 repo，一律當作會套用（只會多擋，不會多放行）。

每個 `path` 都必須以相同目錄層數與逐段 glob（`matches()`）對上閉包內至少一個模組的 `firmware=` 宣告（例如 `kano` 的 `i915/` 三筆由 `vendor_dlkm` 的 `xe` 宣告（fixture 另在 initrd 放 `xe`；真實 initrd 的清單是否含 `xe` 待 build-host 確認）、`iwlwifi-so-a0-gf-a0-*` 由 `iwlmvm` 依賴的 `iwlwifi` 宣告；`amd-hx370` 的 `iwlwifi-ty-a0-gf-a0-*` 由 `iwlwifi` 宣告），這樣當 Google 更新核心把請求檔名改版（例如 `iwlwifi` 從 `-89` 改為 `-90`）時，建置 log 會印出 `ADVISORY firmware pin:` 提醒失效的釘選（v0.2.3 不中止建置，見下方「v0.2.3：反向檢查只是 advisory」）。
只在特定開機選項由腳本載入的模組，用 `loaded_by = "<vendor|vendor_dlkm|system_dlkm|initrd>:<相對 .ko 路徑>"` 指名那個檔案（`amd-hx370` 的 13 筆 `amdgpu/*` 是 `loaded_by = "vendor:lib/dev-modules/amdgpu.ko"`：預設 safe 選項封鎖 `amdgpu`，只有 `amdgpu.modeset=1` 選項由 `/vendor/etc/amd-hx370-graphics.sh` 載入）；檢查要求映像裡正是這個檔案、而且它的 `.modinfo` 有對上釘選的 `firmware=` 宣告，否則記一筆 advisory，所以 `amdgpu` 改請求名時同樣會被報出。`loaded_by` 另外交叉核對：目標已在閉包內（那就該是一般宣告）、被核心封鎖、被閉包內同名的另一個檔案遮蔽，或沒有任何 builder 寫出的腳本／rc（`vendor:etc` 下與原廠不同的 `.rc`／`.sh`）以 `modprobe --dirname=… <模組>` 點名它，都記為 advisory；點名只算真正的 `modprobe` 命令（`#` 註解行、行尾註解與 `echo`／`log` 引號裡的文字不算；以 `shell_words()` 切詞（POSIX 子集，懂單雙引號與反斜線跳脫），命令字前可有 `if`／`then`／`exec_background <context> --`、`NAME=value`、`env`（含其選項）等前綴，由 `command_words()` 略過），它不核對腳本的條件是否真的成立。
驅動確實沒有靜態宣告、在執行期才組出檔名的釘選（`btintel` 從 HCI 讀 `cnvi_top`/`cnvr_top` 組出 `intel/ibt-%04x-%04x.sfi`/`.ddc`；`snd-sof-pci-intel-tgl` 與 `snd-soc-acpi-intel-match` 依 PCI/DMI/ACPI 組出 `intel/sof/community/sof-{adl,rpl}.ri` 與 `sof-tplg/*.tplg`）才加 `requested_by = "runtime: <單行理由>"`；若任何出貨模組其實靜態宣告了這個名稱，記一筆 advisory 並提示改用 `loaded_by`。`requested_by` 與 `loaded_by` 互斥。兩者與 `firmware_check` 的 `reason` 都由 `devprofile.py` 驗證為一行可見文字（拒絕 U+2028、U+0085、DEL、只有零寬字元等）。
`runtime` 釘選除了 SHA-256 外沒有名稱核對：`PASS` 不代表 `kano` 的 6 筆與 `amd-hx370` 的 2 筆 `runtime` 檔名仍被驅動請求。
報告的 `pins` 清單記錄每筆的 `path`、`status`（`declared`／`runtime`／`conditional`／`undeclared`）、`kind`（`static`／`requested_by`／`loaded_by`）、`declared_by` 模組相對標籤、`modules`、命中的宣告字串 `declarations` 與是否靠 glob 命中的 `glob`，以及 `requested_by` 或 `loaded_by`；建置 log 為每筆 `declared`／`runtime`／`conditional` 印一行 `PINNED firmware:`；每個反向檢查的拒絕（`undeclared` 釘選、`requested_by` 誤用、`loaded_by` 交叉核對）印一行 `ADVISORY firmware pin: profile <p>: <pin>: <理由>`，並記入報告最上層的 `advisories` 清單（`pins[].status` 照舊）。`firmware check:` 結果行另外統計 `pins: <總數>, declared: <N>, runtime: <N>, conditional: <N>, advisory: <N>`（`kano` 為 `pins: 11, declared: 5, runtime: 6, conditional: 0, advisory: 0`、`amd-hx370` 為 `pins: 17, declared: 2, runtime: 2, conditional: 13, advisory: 0`、`generic-x86` 為 `pins: 0, declared: 0, runtime: 0, conditional: 0, advisory: 0`）。釘選的 `path`、`declared_by`／`loaded_by`／未載入宣告模組的標籤與命中的宣告字串，和模組宣告的韌體名稱一樣交給 D1 的 `local_names`（沒有任何保留豁免）：不含本機建置根目錄文字的照一般 scrub 輸出；含的則檢查以 `firmware name contains a local build path` 失敗，且只以 repr 印遮罩後的名稱。每一行 `PINNED firmware:` 與 `ADVISORY firmware pin:` 也都經過最後的 output guard，仍含建置根目錄文字（原樣、resolve 後或 repr 形式）的行會被扣下，整次檢查失敗。`firmware-report.json` 也算輸出：每個字串欄位（`firmware`、`matched`、`reason`、`source`、`pins` 的每個欄位，含 `requested_by` 與 `loaded_by`）都過同一個 guard，含建置根目錄文字的欄位整個換成 `diagnostic withheld: it contained a local build path`，檢查失敗；資料欄位本身不改寫。`one_line()` 用 `isprintable()`，所以全形空白 U+3000 與 NBSP 也會被拒。
**v0.2.3：反向檢查只是 advisory。** 釘選的拒絕只報告、從不讓建置失敗。退出碼只取決於：正向檢查、必要模組的 metadata 錯誤、D1、output guard、報告寫入失敗、參數錯誤，以及反向檢查建構載入閉包時的輸入錯誤。後者是：loader 點名的模組在同一目錄有兩個同名檔案（`ambiguous module`）；閉包以 `read()` 讀的那幾個指名的 loader metadata 檔案（`vendor_dlkm`／`system_dlkm` 的 `lib/modules/modules.load`／`modules.dep`／`modules.blocklist`、`vendor:lib/kano/modules.dep`、`vendor:lib/dev-modules/modules.dep`、`vendor:etc/init/kano-hw.rc`）存在但不是 regular file（`loader file is not a regular file`，例如 symlink，一律不跟隨）。一般的 builder `.rc`／`.sh` 腳本（`vendor:etc` 下其他與原廠不同的檔案）若是 symlink 或不是 regular file，`builder_loader_files()` 直接略過、不讀也不失敗，其後果只是 `loaded_by` 目標可能找不到點名而記一筆 advisory；initrd 的 `lib/modules/modules.load`／`modules.dep` 不是 regular file；讀這些檔案時的 I/O 錯誤。這些不是釘選的拒絕，而是映像本身不一致：checker 無法安全地建出閉包（跟隨 symlink 可能讀到映像外的檔案），所以仍讓檢查失敗；有效的映像（包括 KANO 真機建置）不會觸發，和 advisory 不衝突。`ADVISORY` 行本身若被 output guard 扣下，也會讓檢查失敗。`requested_by`／`loaded_by` 的 profile schema 驗證仍是嚴格的（格式錯誤仍是 profile 錯誤）。日後（v0.2.4+）模型補齊後可再升為閘門。review round 1–4 找到、模型尚未涵蓋的已知缺口：
- first-stage init 會套用 initrd 自己的 `modules.blocklist`，checker 不讀這份檔案（多放行，不是 fail-closed）；
- 父模組載入失敗時，已先載入的依賴不會回滾（partial-load rollback），checker 視為整組都沒載；
- newc 裡先當硬連結的名稱、之後變成目錄（hard link turned directory）；
- stage 2 改寫清單與 newc 解開（unpack）的先後順序；
- `modules.dep` 缺少 head 的依賴仍被當作可載入（missing dep head）；
- 閉包被低估時，`loaded_by` 的「已在閉包內」與「被遮蔽」兩項交叉核對跟著變寬鬆（不是 fail-closed）；
- newc 項目的父目錄不存在時，核心跳過該筆紀錄，checker 仍把它算進 initrd；
- 硬連結名稱之後寫入較短的資料時，核心不 truncate 既有內容，checker 直接以後寫的資料為準；
- softdep、ueventd modalias 載入、腳本裡的條件、續行與 here-doc 都不建模；
- loader 點名的 shell 解析只是子集（`loader_modprobes()`）：引號或跳脫的 `;`／`&`／`|` 不分隔命令，`NAME=value` 與 `env` 前綴後的 `modprobe` 照算（round 4），`env` 的選項（`-i`、`-0`、`-v`、`-u NAME`、`--unset=NAME`、`-C DIR`、`-S STRING`、`--`）與其後的 `NAME=value` 也略過，加引號的 `"env"` 仍是 env（round 8）；不建模的有 here-doc 與跨行字串、`case` 分支、單行函式、緊貼命令字的 `(`／`{`、`time`／`busybox`／`command` 前綴、rc 的 `service` 行、重新指定的 `$MODPROBE`、`modprobe` 後不是緊接 `--dirname=<目錄>` 的選項（`-q`、`-d`、`--dirname <目錄>`），而 `--dirname=… -a a b` 會把 `-a` 當成模組名（找不到該模組，目前無害）。

所以 `PASS` 證明的是：必要模組的必要韌體都在、必要模組的 metadata 檢查通過、閉包輸入一致、D1 與 output guard 都過（沒有本機路徑外洩）。report-only 原廠模組的 metadata 錯誤不擋建置，仍可能存在，只記入 `warnings` 與摘要。釘選方面，`PASS` 只證明 profile 的 `[[firmware]]` schema（含 `requested_by`／`loaded_by`）有效；SHA-256 由 `fetch-inputs.sh` 與 `vendor.sh` 另外核對。它**不**證明每筆釘選都被某個實際會載入的模組請求；`advisory: 0` 只表示在上述近似模型下每筆釘選都找到宣告者，不證明它在真機上一定被載入。`advisory: N` 非 0 時請逐行看 `ADVISORY firmware pin:`。

這只檢查模組靜態宣告的韌體名稱，不保證硬體可用；既有 profile 的 `[[firmware]]` 韌體 SHA-256 釘選仍由 `fetch-inputs.sh` 與 `vendor.sh` 核對。

## 開機與顯示

- **自帶 systemd-boot 的 USB 映像**：base 階段把官方 recovery 映像轉成 GPT 映像，ESP 放 `kernel.efi`、`initrd.img` 與 loader
  entries。這個中間產物**還不能開機**（開機選項是原廠參數、initrd 的 vbmeta digest 是舊的），只給 builder 用；builder 重寫 ESP 的
  initrd 與開機選項之後，才是給 UEFI 韌體開機的映像。
- **vbmeta 用你自己的 AVB 金鑰重簽**：vendor 改了，頂層 vbmeta 必須重簽；bootconfig 的 `androidboot.vbmeta.digest/size`
  同步更新，`androidboot.boot_part_uuid` 指向這顆碟的 boot_a。
- **軟體 KeyMint／Gatekeeper**：KANO 沒有 pKVM protected VM，改用 `rust_nonsecure` / `gatekeeper.nonsecure` APEX。
- **`xe.enable_dpt=0 xe.enable_psr=0 xe.enable_fbc=0`**：這版 xe（6.18.21）透過 stolen memory 裡的 DPT 掃描 tiled
  framebuffer 會出錯，畫面全黑並出現 plane fault（CVE-2026-80915，上游修正 `fc648757908304aedbad74f74bf58192aec383db`
  「drm/xe: Fix DPT allocation paths」，6.18.y 的 backport 是 `491c499295fb`），關掉 DPT 就正常。
- **pci-stub 先於 intel-lpss 載入**（initrd 第一個模組；`pci-stub.ids=8086:51a8,8086:51a9,8086:51aa,8086:51ab`）：
  LPSS UART（序列埠）與指紋 GSPI 交給 pci-stub，intel-lpss 只接 I²C，觸控板、觸控螢幕、音效 codec 才能用，序列埠也不會被重設。
- **兩個開機選項**：quiet（預設，不輸出序列埠）與 serial-debug（開發用）。serial-debug 留在 UART 上的是
  `earlycon=uart8250,mmio32,0xfe03e000,115200n8` 加上 `keep_bootcon`（printk 的 bootconsole，不是可登入的 tty）；
  `console=ttyS0`／`ttyS4`／`uart8250` 沒有放回去。quiet 本來就沒有這三個 `console=`。兩邊還差 log level。
  `image.py` 會檢查命令列長度（lapis 的 x86 上限再扣 64 bytes，給 harness 附加除錯參數）。base ESP 原本的 `lapis.conf`
  （Google 的參數，沒有 KANO 的封鎖）會被刪掉，`image.py` 也確認 ESP 上只剩這兩個選項。早期版本另有一個沒有 `pci-stub.ids`
  的 plain 選項，在 KANO 上面板一直是暗的，已不再產生。

## 儲存與首次開機

- **/data 用 f2fs、拿掉 FBE 與 `checkpoint=block`**：慢速 USB 碟上 ext4 的 fsync 會拖過 watchdog（safe7、safe10）。
- **`ro.cp_system_other_odex=0`**：首次開機複製 odex 太久（safe8）。
- **`persist.vold.dont_commit_checkpoint=1`**（`kano_bootfix` 服務）：vold 的 checkpoint commit 會呼叫 boot HAL 的
  markBootSuccessful → crossystem（韌體 NVRAM，已封鎖）而失敗重開（safe12/13）。
- **移除 IWeaver、ISecretkeeper 的 VINTF 宣告**：它們需要 protected VM，LockSettings 會一直等（safe11）。
- **停掉 timberslide、gscd、udsattestation**：需要 EC／Cr50，只會崩潰循環（safe21/23）。
- **system_server watchdog 5 分鐘**（`Settings.Global.system_server_watchdog_timeout_ms`）。

## 硬體

- **Wi-Fi／藍牙／音效韌體**：AX211（`iwlwifi-so-a0-gf-a0-89`、pnvm、`ibt-0040-0041`）、SOF `sof-adl.ri`／`sof-rpl.ri` 與
  `sof-adl-max98373-nau8825.tplg`／`sof-rpl-max98373-nau8825.tplg`，加上 ADL-P／RPL-P 共用的 GPU 韌體（DMC、GuC、HuC），全部只載入 RAM、不燒錄；載入 `iwlmvm`。
  內容固定在 linux-firmware 20260810 與 sof-bin v2025.12.2，別的版本會被拒絕（`GBOS_ALLOW_FIRMWARE_DRIFT=1` 才放行，未經實機測試）。
- **音效設定（DRAS）**：ChromeOS 官方 kano UCM（board-overlays 的 `ucm-config/sof-nau8825.kano/HiFi.conf`）是**人工翻譯**的參考來源：
  `audio_config.xml` 的 PCM 編號與 `mixer_paths.xml` 的控制項寫在 `profiles/kano/vendor.py` 裡，build 時不會讀 HiFi.conf。
  只有音量曲線從 `sof-nau8825.card_settings` 讀取並轉換。`hal_configs/audio_default.xml` 指向 `sof-nau8825`。
  官方 DSM 喇叭保護參數沒有公開，喇叭音量曲線整體降 4.75 dB（100% = -10 dB）。
- **鍵盤背光路徑**：KANO 的 GOOG0002 在 EC 節點底下（`platform/GOOG0004:00/GOOG0002:00/leds`），idc／ueventd／genfscon
  一起改（這台 EC 本身不調亮度，見 STATUS）。
- **電源鍵**：lapis 排除 ACPI「Power Button」（它的電源鍵來自 cros_ec_buttons）；KANO 的 cros_ec_keyb 被封鎖，所以要放行
  ACPI 電源鍵（safe25）。

## 開發用元件

除了 `kano_adb` 之外，這些元件目前在每個 build 都會包含，沒有開關（安全上的影響見 SAFETY.md「資料安全取捨」）：

- `kano_diag`：每 120 秒把負載、I/O 寫到 `/data/kano-diag`（fsync）；開機約 4 分鐘時存一次 dmesg 與 logcat。
- `kano_logcat`：只在 serial-debug 選項下把 logcat 鏡像到序列埠，其他選項不會啟動。
- `kano_audiotest`：只有 `/data/kano-audiotest.enable` 存在時才播測試音。
- `kano_wifi`：只有 `/data/kano-wifi.conf` 存在時才連線（開發用維護系統寫入）。
- vendor build.prop 的 `ro.debuggable=1`：這一行實際上沒有生效，KANO 實機讀到的是 `ro.debuggable=0`（見 SAFETY.md）。
- `kano_adb`：只有 build 時提供 `GBOS_ADB_PUBKEY` 才會包含（金鑰驗證的 TCP 5555 adb）。

`kano_autooff` 不在上面這份清單。只有開發者的 TEST 映像（`GBOS_TEST_AUTOOFF`＝60..7200 秒）才會放進這個服務。它要斷電，`/vendor` 裡的 nonce 和 `/data/kano-autooff.nonce` 必須都在，而且是同一份 33 bytes。`/data` 那一份只由這次硬體 session 寫到已核對身份的 USB 隨身碟 userdata，用完就刪。沒有這份檔，碟上即使有 TEST vendor 也只會 staying on（QEMU 自我測試就是這個情況：`image.py` 的 `autooff_log_checks` 要求出現 `nonce gate closed, staying on`，而且同一份 log 不能有 `powering off`）。

斷電只能靠 `/data` 上的 nonce。會把隨身碟的 userdata 複製到內建碟的，只有 `tools/harness/kano-nvme-install.sh`。這條路的安全保證是：複製並掛上之後清掉 `kano-autooff.nonce`（一般檔、符號連結或目錄）。一般檔與符號連結用 `rm -f`，不跟隨符號連結。目錄在同一個遠端 python3 裡刪除：`os.lstat` 必須是真目錄而不是符號連結，`shutil.rmtree.avoids_symlink_attacks` 必須為真，`rmtree` 的錯誤會讓 helper 非 0；檔名不經過文字紀錄。卸載後再以唯讀重新掛上，同一個名字的 `lstat` 必須是 ENOENT，然後再卸載。掛載失敗、根目錄列不出來、查找錯誤（errno 不是 ENOENT）、刪不掉、重新掛載失敗、複查失敗或任一次卸載失敗，就以非 0 停止，不再 `resize.f2fs`、`fsck.f2fs`，也不再寫 ESP。只有 ENOENT 算這個名字不存在。任何 lstat 發生 ENOENT 以外的查找錯誤就停止；刪除後及重新掛載後的確認，必須得到 ENOENT，成功查到任何物件也停止。`dd` 完成之後、這次清除成功之前的任何停止（行程被殺掉、斷電、ssh 中斷）都可能留下檔案；清除失敗則不會繼續。之後只更新 super 的路不會清掉已經留下的 nonce。vendor 掃描是縱深防禦：寫入前拒絕解析得到 `etc/kano-autooff.nonce` 或 `etc/init/kano-autooff.rc` 的映像，讓 TEST 服務不要留在日常碟上。`blkdiscard` 前先確認 `/dev/sda1` 的 PARTLABEL 是 `userdata`，再以 `-o ro` 掛上；f2fs 即使唯讀掛載仍可能把先前的 fsync 補完。已有一般檔、符號連結或目錄 `kano-autooff.nonce`，或列出根目錄／`lstat` 失敗（不是 ENOENT），就拒絕。這個 repo 沒有寫 session nonce 的程式。

只寫 super 或開機分割、因此建不出 `/data` nonce 的路，不會因為裝上 TEST vendor 就斷電：`kano-nvme-update.sh` 在推送前會拒絕 TEST，但它不清 userdata；直接呼叫的 `kano-delta-push-nvme.py` 只寫 super 與 vbmeta_a/b（userdata 有變動就拒絕），不掃描 vendor。安裝碟的更新走 `gbos-update.py apply`：寫入的是目標 slot、super 裡的動態分割、LP metadata，以及 ESP 選單。`guard_ranges` 把 userdata 列進不可寫範圍，`pwrite` 碰到就拒絕。`rollback` 只改 ESP 的 loader 項目，不寫 userdata。`NEVER` 那一行是說明，擋住寫入的是上述範圍。安裝碟的全新安裝把內建碟的 userdata 建成新的，開頭 16 MiB 寫零，不複製隨身碟的 userdata。`install.sh` 與 `docker-install.sh` 不寫內建碟，它們在建置 USB 映像之前拒絕這個開關。`install/update.sh` 先呼叫 `install.sh`；若帶了 `--disk`，接著 `exec sudo install/write-usb.sh --keep-data`，目標是 USB 隨身碟，不是內建 NVMe。

Build 產生一組新的 128-bit nonce：32 個小寫十六進位再加一個 LF，共 33 bytes。同一份放進映像的 `/vendor/etc/kano-autooff.nonce`（mode 0444）以及 `<out>/image/autooff.nonce`（給這次 session 讀）。沒有這個開關時，這兩個檔案和服務都不存在。

開機腳本把兩個檔案當原始位元組讀。只有兩邊都是一般檔（不是符號連結）、都是那 33 bytes、而且逐 byte 相同，才開始等。等滿 N 秒（`sleep` 必須成功，而且 `/proc/uptime` 至少前進 N 秒）之後、斷電前再讀一次、再比一次，通過才執行一次 `reboot -p`。`/proc/uptime` 是 `CLOCK_BOOTTIME`（含 s2idle／S0ix 的時間）；`sleep` 用的 `CLOCK_MONOTONIC` 在休眠時暫停。正常睡滿 N 秒時，boottime 的增量不會小於 monotonic，閘門仍會過。缺檔、讀取失敗、長度不對、NUL、CR、多出來的位元組、大寫、兩邊不同，都在 kmsg 記 staying on 並以 exit 0 結束。符號連結即使指到正確內容也保持開機：`/data` 上的連結可以指回 vendor 檔或指到別顆碟。硬連結跨不過唯讀的 vendor erofs 與 userdata 的 f2fs，所以不能用連結把 vendor nonce 掛到 `/data`。

`/data/kano-autooff.nonce` 不在映像裡。和 `/data/kano-wifi.conf` 一樣，由這次 TEST 開機前的 session（維護系統）寫到**已核對身份的 USB 隨身碟** userdata（`install/write-usb.sh` 的目標是 `/dev/disk/by-id/usb-...`），而且必須是一般檔：內容是該次 build 的 `<out>/image/autooff.nonce` 原始 33 bytes。用完立刻刪掉。不要寫到內建 NVMe 的 userdata。不要把 TEST 映像裝成內建系統。若這些檢查不在，留下檔案再跑 harness 安裝會讓內建碟同時得到服務與 nonce，之後每次開機都會斷電；`kano-nvme-update.sh` 只推 super／vbmeta，不會清掉那個檔。目前 `kano-nvme-install.sh` 用 vendor 掃描當縱深防禦，在 `blkdiscard` 前確認 `sda1` 是 userdata 再以 `-o ro` 掛上並拒絕已有的 nonce，並在卸載複製過來的 userdata 之前清掉 `kano-autooff.nonce`（一般檔、符號連結或目錄；目錄由 python3 的 `shutil.rmtree` 刪除，不把檔名交回 shell）。卸載後再唯讀掛上複查，名字必須是 ENOENT，然後再卸載。那次清除或複查失敗（含查找錯誤（errno 不是 ENOENT），或確認時成功查到任何物件）就停止，不再 `resize.f2fs`、`fsck.f2fs`，也不再寫 ESP。`dd` 完成之後、清除成功之前的任何停止（行程被殺掉、斷電、ssh 中斷）都可能留下檔案。這一步才是這條複製路的安全保證。QEMU 沒有這份 `/data` 檔，自我測試會 staying on。
