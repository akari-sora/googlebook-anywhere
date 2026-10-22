# 支援新的機型

目前只有 KANO 完整驗證過。同世代（Intel Alder Lake / Raptor Lake，brya 家族）的其他 Chromebook 大概率能開機；
一般 Intel 筆電與 AMD 機器還需要額外的工作（見最後一節）。每個型號都要逐項實機驗證。

## 前提

- 機器已經換成 **UEFI 韌體**（原廠 ChromeOS 韌體只能開簽章過的 ChromeOS）。換韌體需要拆寫入保護、刷 flash，
  有變磚風險，這個專案不做這一步，也不提供工具。請參考 MrChromebox 官方文件，並先確認你的型號在支援清單上。
- 目前的 lapis 核心只有 Intel 的 xe GPU 驅動；沒有 SATA（只看得到 NVMe、eMMC 與 USB 碟）。

## 機型設定：`profiles/<型號>/`

builder 裡和機型有關的值都在機型設定目錄，換 `GBOS_PROFILE`（或 `install.sh` 前設定 `GBOS_PROFILE=profiles/<型號>`）
就換機型；KANO 是 `profiles/kano/`，也是預設值。

沒有 lapis GPU 驅動的機器（例如 AMD、QEMU；CPU 要 Intel 第 12 代或 AMD Zen 5 以後，見 COMPATIBILITY.md「CPU 最低需求」）用實驗中的 `profiles/generic-x86/`：`display.stack = "gop-swiftshader"`，
畫面走韌體的 GOP framebuffer（`builder/kmod/gbos_efifb.c`，從 base 映像自己的核心標頭、用 GKI 自己的 Android clang r584948c 建置，第一次約下載 1 GB、解開 3.5 GB；輸出以 SHA-256 釘住，每台 Linux 主機建出來都一樣），繪圖用 Cuttlefish 的 SwiftShader
（`builder/fetch-generic-inputs.sh` 下載並核對 SHA-256），沒有 GPU 加速、沒有機型韌體與音效設定。只在 QEMU 開到歡迎畫面過
（`research/amd/PLAN.md` 第 8 節）。

另有針對 **AMD Ryzen AI 9 HX 370 / Radeon 890M** 的 `profiles/amd-hx370/`（需搭配自建的 Track 2 GKI 核心 `GBOS_DEV_KERNEL_DIR`、`tools/mesa/build-radv.sh` 建出的 RADV，以及 `[gralloc]` 指向的 `tools/minigbm` 產物 `GBOS_MINIGBM`，目前只在 QEMU 驗證過 safe 選項）：建置時產生兩個固定開機選單項目，**不做執行期顯示切換**：
- **預設 `"Googlebook AMD HX370 safe"`**（`lapis-quiet.conf`）：`amdgpu` 列入 `module_blacklist` 且不載入、不帶 `amdgpu.*` 參數，走與 `generic-x86` 相同的已知可用路徑（GOP framebuffer + `gbos_efifb` + SwiftShader `pastel`），已在 QEMU 開到設定精靈驗證。
- **實驗 `"Googlebook AMD HX370 native amdgpu (experimental)"`**（`lapis-debug.conf`）：在 `early-boot` 載入一次 `amdgpu`、絕不啟動 `gbos_efifb`，設定 `ro.hardware.vulkan=radeon` 並依 sysfs `/sys/class/drm/card*/device/uevent` 的 `DRIVER=amdgpu` 自動選擇 HWC 的 DRM 裝置。映像依 `[gralloc]` 把 `/vendor/lib64/libminigbm_gralloc.so` 換成 `tools/minigbm` 的建置（`backend_amdgpu`，並保留 `backend_vkms` 給 safe 選項；`GBOS_MINIGBM` 的 SHA-256 必須與設定一致，否則 build 停止）。kano 與 generic-x86 沒有這張表，那顆檔維持原樣。**誠實揭露**：此選項尚未在實機測試。QEMU 沒有 amdgpu，self-test 只開 safe 選項（allocator 走 vkms）。若實機選此項出現黑畫面，請重開機選預設的 `"Googlebook AMD HX370 safe"`。

**Windows / BitLocker 必做前置步驟（進 BIOS 改 Secure Boot 之前）**：在任何裝有 Windows 的 PC 上，進 BIOS 動 Secure Boot 之前，先在管理員 PowerShell 執行 `manage-bde -status` 檢查所有磁碟區；只要有任何磁碟區啟用了 BitLocker，**必須先把每個加密磁碟區的 48 位數 BitLocker 復原金鑰（Recovery Key）備份並存到另一台裝置**（<https://aka.ms/myrecoverykey> 或對 `manage-bde -status` 列出的每個加密磁碟區執行 `manage-bde -protectors -get <磁碟區>`，例如 `C:`、`D:`）。切換 Secure Boot 可能會改變開機量測與 BitLocker 所綁定的 TPM PCR（例如 PCR 7，或依平台設定綁定的 PCR 0、2、4、11），下次開回 Windows 時**可能會要求輸入 48 位數復原金鑰**。`Suspend-BitLocker -MountPoint "C:" -RebootCount 1` 只是備份好復原金鑰之後的選用便利指令（暫停期間會把明文金鑰留在磁碟上，有保護空窗，且重開機次數用完後仍會檢查 TPM PCR；回到 Windows 後請用 `manage-bde -status`／`Resume-BitLocker` 確認保護已恢復），**絕對不能代替備份 48 位數復原金鑰**。

| 檔案 | 內容 |
|---|---|
| `profile.toml` | 機型名稱與映像檔名、選單標題、開機參數（要拿掉的原廠參數、加上的參數、quiet 專用移除參數 `quiet_drop`、pci-stub ID、first stage 不載入的模組）、`module_blacklist`（含 quiet 專用追加 `quiet_add`）、韌體清單（來源與 SHA-256）、音效卡、Wi-Fi 要載入的模組、`[modules]`（early-boot modprobe；要先在 `profiles/_safety.toml` 的 `[modules]` 允許）、可選的 `[gralloc]`（`libminigbm_gralloc.so` 的 SHA-256；只有列出的機型才換這顆檔）、顯示設定、鍵盤背光 LED 路徑、電源鍵來源 |
| `vendor.py` | 需要寫文字的 vendor 修改（`profile.toml` 的 `hooks.vendor`）：DRAS 音效設定檔、鍵盤背光的 idc／ueventd／SELinux、電源鍵、hwcomposer 設定。新機型從 KANO 的這份複製再改 |
| `audio-src.list` | 音效設定的來源檔（ChromeOS board-overlays，建置時才下載，固定 SHA-256） |

`builder/lib/devprofile.py` 讀取並嚴格檢查設定：不認得的鍵、缺少的鍵、型別錯誤都會拒絕。全專案的安全規則在
`profiles/_safety.toml`，機型只能「加」不能「減」：

- 每個機型的 `module_blacklist` 都必須包含 `_safety.toml` 的 `blacklist.required`：v0.1 的整份硬封鎖清單（57 個模組：
  每種傳輸的 TPM 與 ChromeOS EC、EC 的 chardev 與周邊驅動、SPI flash、GSMI、NVRAM、chromeos_acpi、i2c-dev、AT24、Thunderbolt、
  觸控控制器的 QuickI2C／QuickSPI 等）。機器沒有的模組封鎖了也沒有代價。
- 機型自己的參數不可以用 `module_blacklist=`、`initcall_blacklist=`、`efi=`、`efi_pstore.`、`spi-intel.`、`lockdown=`
  開頭（這些由 builder 統一加上，放在後面會蓋掉前面的）；也不可以拿掉安全參數（`efi=noruntime` 等）。
- 每個開機選項最後都有 `initcall_blacklist=nvme_init,mei_me_driver_init efi=noruntime …`（USB 映像看不到內建 NVMe）。
- 當使用自建核心（`GBOS_DEV_KERNEL_DIR`）時，`devprofile.py check-kconfig <profile> <kdir>` 會檢查 `.config`、`bzImage` 內嵌的 `IKCFG` 與 `System.map`，拒絕任何把受封鎖驅動編成 built-in（`=y`）或讓 `nvme_init`／`module_blacklist` 失效的核心設定（見 `docs/SAFETY.md` 第 8 點）。
- `[modules]`（可選）讓這個機型在 `early-boot` 同步 `exec` `modprobe`（命令沒有 `-b`）。先在 `profiles/_safety.toml` 的 `[modules]` 加一列 `"<profile-id> <模組名> <vendor|dev>"`，再在機型 `profile.toml` 的 `[modules]` 用同一個名字。`load` 是一般載入。`unblock` 只能點 stock `vendor_dlkm` `lib/modules/modules.blocklist` 上的模組；那份名單只有一處，`builder/lib/devprofile.py` 的 `STOCK_MODULES_BLOCKLIST`（目前是 `snd_hda_intel`，檔案內容是一行 `blocklist snd_hda_intel`，builder 不重寫 `vendor_dlkm`）。`load` 不能點這些名字。`vendor` 是 `/vendor/lib/modules`，`dev` 是 `/vendor/lib/dev-modules`；`dev` 的 `.ko` 不在 `GBOS_DEV_KERNEL_DIR/modules` 頂層時 build 中止。allowlist 的順序就是載入順序：較早的每一列，不論 `vendor` 或 `dev`，都要先被選到（`btusb` 在 `bluetooth` 後面）。同一張表內照 allowlist 順序；所有 `load` 行都在 `unblock` 行之前輸出（codec 先於 `snd_hda_intel`）。拒絕的名字：`blacklist.required`、profile 自己的黑名單、`_NEVER_SHIP_KO`，以及 kconfig 閘門已經對上的儲存／韌體更新類模組名（`nvme`、sdhci、`mtd`、SPI flash（控制器、SPI-NOR、spidev）、`thunderbolt`、TPM、`ccp` 等；不另設萬用字元）。核心命令列的 `module_blacklist=` 仍然會擋載入。kano 與 generic-x86 沒有這些列。見 `docs/SAFETY.md` 第 9 點。
- 每筆 `[[firmware]]` 釘選（`path`、`from`、`src`、`sha256`）都會在建置時接受 `builder/kmod/check-firmware.py` 的反向檢查（細節見 `docs/HOW-IT-WORKS.md`）：載入閉包以「樹＋相對路徑」辨識模組，起點是 `vendor_dlkm`、`system_dlkm`、initrd 各自 `lib/modules/modules.load` 列的檔案，加上該 profile 的必要模組在它們實際載入的樹（`kmods` → `vendor:lib/kano`，`wifi.modprobe` → `vendor_dlkm`，`[modules]` 依 `_safety.toml` 的 `vendor|dev`，`pci_stub`（只在 `pci_stub_ids` 非空時），vendor 新增／替換的檔案，`kano-hw.rc` 的 `modprobe --dirname=…`），再沿各目錄自己的 `modules.dep` 展開；每個開機選項都封鎖的模組與 stock `modules.blocklist`（未 `unblock`）不算。每個 `path` 都應對上閉包內某個模組的 `.modinfo` `firmware=` 宣告；v0.2.3 的反向檢查只是 advisory：對不上時建置 log 印 `ADVISORY firmware pin: profile <p>: <pin>: <理由>` 並記入報告的 `advisories`，不讓建置失敗（`requested_by`／`loaded_by` 的格式驗證仍嚴格）。只在特定開機選項由腳本條件載入的模組，加 `loaded_by = "<vendor|vendor_dlkm|system_dlkm|initrd>:<相對 .ko 路徑>"`（例如 `amd-hx370` 的 `amdgpu/*` 是 `"vendor:lib/dev-modules/amdgpu.ko"`），該檔案必須在映像裡並宣告這個釘選，且不得已在閉包內、被核心封鎖、被同名模組遮蔽，並要有 builder 寫出的腳本或 rc 以真正的 `modprobe --dirname=… <模組>` 命令點名它（註解與引號內文字不算；不核對腳本條件）。驅動確實不靜態宣告、執行期才組出檔名的（例如 `btintel` 依 HCI 的 `cnvi_top`/`cnvr_top` 組出 `intel/ibt-*.sfi`/`.ddc`，或 SOF 依 PCI/DMI/ACPI 組出 `.ri`/`.tplg`），加 `requested_by = "runtime: <單行理由>"`；引用核心原始碼時寫明樹或 tag（例如 `Linux v6.18` 或 `android17-6.18`）與檔案及函式（行號只在已對該 tag 核實時才寫）。若任何出貨模組靜態宣告了這個名稱，`requested_by` 會記一筆 advisory 並提示改用 `loaded_by`；兩者互斥，都只接受一行可見文字。initrd 依 stage 2 改寫後的清單（`first_stage_drop` 不算）；核心封鎖的模組與需要它的模組都不算；stock `modules.blocklist` 只對 `vendor_dlkm` 的 `modules.load` 起點生效。`firmware-report.json` 的每個字串欄位也過 output guard。`PASS` 行列出 `pins: <總數>, declared: <N>, runtime: <N>, conditional: <N>, advisory: <N>`。反向檢查建構閉包時的輸入錯誤（同目錄同名模組的歧義、loader 檔案不是 regular file）仍讓建置失敗，因為那是映像本身不一致。`PASS` 只證明必要模組的正向檢查與 metadata、閉包輸入、D1 與 output guard 都過（report-only 模組的 metadata 錯誤只進 `warnings`），以及釘選 schema 有效；不證明每筆釘選都被實際載入的模組請求。已知模型缺口（first-stage initrd 自己的 blocklist、partial-load rollback、硬連結變目錄與較短的後寫資料、stage-2 改寫與解開順序、缺少 dep head、父目錄不存在的 newc 紀錄、閉包低估時 `loaded_by` 變寬鬆、loader 的 shell 解析子集等）見 `docs/HOW-IT-WORKS.md`。新機型請讓 `advisory: 0`，非 0 時逐行看 `ADVISORY firmware pin:`。`firmware-report.json` 的 `pins` 記錄 `kind`、`declared_by`、命中的 `declarations` 與 `glob`。

`python3 builder/lib/devprofile.py check profiles/<型號>` 只做檢查。`tests/builder/profile-test.py` 對過 KANO 開機參數相對 v0.2.0 的差異：`xe.force_probe`、拿掉的三個 `console=`，以及兩個 RPL SOF 檔。`tests/builder/repro.sh` 對過的逐檔相同是 final26 那個較早的 builder，不是現在這張家族映像（命令列、兩個韌體檔，以及 vbmeta／initrd 的 hash 鏈都不同）。

## 第一步：在目標機器上跑 probe

在目標機器上開任何 live Linux（USB），從這個 repo 執行：

```sh
install/gbos-probe.sh --out gbos-probe     # 不需要 root；只讀 /sys 與 /proc，不執行其他程式，只寫 --out 目錄
```

| 輸出 | 內容 |
|---|---|
| `report.txt` | 機型、韌體（UEFI、coreboot、Secure Boot）、CPU、GPU、內建碟、Wi-Fi、音效、輸入裝置，以及警告 |
| `coverage.txt` | 每個 PCI／USB／ACPI 裝置在 lapis 上有沒有驅動：lapis 的模組（`install/probe/lapis-modules.tsv`）或編進核心的驅動 |
| `profile.draft.toml` | `profiles/<型號>/profile.toml` 的起點（`status = "probe-only"`）。PCI 清單完整時可以通過 `devprofile.py` 檢查，但每個值都要確認。PCI 沒讀完時 `display.stack = "unknown"`，這份草稿故意不能通過，直到完整 probe，或人手改成 `native` 或 `gop-swiftshader` |
| `probe.json` | 原始資料 |

它不讀序號、MAC 位址或碟上的資料。`lapis-modules.tsv` 只有 lapis（build 16452207）vendor_dlkm 與 system_dlkm 的模組
名稱與它們宣告的裝置比對字串（核心的 `MODULE_DEVICE_TABLE`），由 `install/probe/gen-module-list.py` 從映像產生。
Googlebook 開機之後的回報是 `install/gbos-report.sh`（可以帶這個目錄），issue 模板見文末「把結果寄回來」。

常見的結論：

- GPU 是 Intel Gen12 但 xe 要 `xe.force_probe=<ID>`：probe 會放進草稿的 `cmdline.add`（KANO 的驗證機是 `46a6`；`profiles/kano` 收的是 SKU 表裡的 12 個 ADL-P/U 與 RPL-U ID，不是 `adl_p_desc` 全部 28 個）。
- GPU 沒有 lapis 驅動（例如 AMD、QEMU；CPU 要 Intel 第 12 代或 AMD Zen 5 及更新，見 COMPATIBILITY.md「CPU 最低需求」）：用 `profiles/generic-x86`（UEFI GOP framebuffer + SwiftShader，沒有 GPU 加速；
  只在 QEMU 驗證過，`display.stack = "gop-swiftshader"`）。
- 內建碟只有 SATA：lapis 看不到，只能從 USB 跑。
- 有 eMMC：USB 版映像要像 `nvme_init` 一樣把它藏起來，目前的安全規則還沒有這一項。
- `ACPI0003`（AC 變壓器）：lapis 沒有這個驅動。KANO 用 `kmods = ["ac"]` 帶上釘選建置的 `ac.ko`（`builder/kmod/build-ac.sh`），
  顯示「充電中」；其他機型 probe 看到 ACPI0003 時可以同樣加上。
- 電源鍵走 ACPI（`input.power_button = "acpi"`）的機型：GKI 的 ACPI 電源鍵驅動每次恢復都送 `KEY_WAKEUP`，暗中喚醒
  （RTC 鬧鐘、EC 事件）也會亮螢幕。加上 `kmods` 的 `gbos_wakefilter`（`builder/kmod/build-gbos_wakefilter.sh`）就只在電源鍵
  真的喚醒系統時才放行；`devprofile.py` 要求它和 `power_button = "acpi"` 一起用。

## 每個型號要確認的值

| 項目 | 怎麼查 | KANO 的值（`profiles/kano/profile.toml`） |
|---|---|---|
| LPSS UART／GSPI 的 PCI ID（給 pci-stub） | probe 的 `coverage.txt`，或 `lspci -nn` 找 `00:1e.x` | `cmdline.pci_stub_ids`：`8086:51a8 51a9 51aa 51ab` |
| 序列埠（serial-debug 選項，僅開發用） | coreboot log／`/proc/iomem` | `cmdline.add`：`earlycon=uart8250,mmio32,0xfe03e000,…` |
| GPU 參數 | probe 的 `report.txt`（`profile.draft.toml` 的 `display.stack`：xe 接得上是 `native`，沒有 lapis GPU 驅動是 `gop-swiftshader`，PCI 沒讀完是 `unknown`） | `display.stack = "native"`；`cmdline.add`：`xe.force_probe=46a6,46a3,…`（7 個對到零售 KANO SKU；5 個 46a3、46b2、a721、a7ac、a7ad 是平台候選（ADL-P/RPL-U 巨集成員），KANO 對照未確認，見 docs/COMPATIBILITY.md）；`cmdline.tail`：`xe.enable_dpt=0` 等 |
| 音效卡名稱與 UCM | `cat /proc/asound/cards`；UCM 取自 chromiumos board-overlays 的 `files/<型號>/audio/ucm-config/` | `audio.card = "sof-nau8825"`，DRAS 檔在 `vendor.py` 的 `audio()` |
| SOF 韌體與 topology | `dmesg | grep sof` 會印出要的檔名 | `[[firmware]]`：`sof-adl.ri`、`sof-rpl.ri`、`sof-{adl,rpl}-max98373-nau8825.tplg`（IPC3） |
| Wi-Fi／藍牙韌體與模組 | `dmesg | grep -E "iwlwifi|Bluetooth"` | `[[firmware]]` 的 AX211 檔、`wifi.modprobe = ["iwlmvm"]` |
| 觸控裝置 | probe 的 `report.txt`（`/proc/bus/input/devices`） | `SYNA0000` 觸控板走 `vendor_dlkm` 的 `i2c_hid_acpi.ko`；二供 `ELAN0000` 觸控板走 `elan_i2c.ko`；ELAN 觸控螢幕走 `i2c_hid_acpi.ko` |
| 鍵盤背光 LED 路徑 | probe 的 `profile.draft.toml` 註解（`/sys/class/leds/`） | `input.kbd_backlight_leds` |
| 電源鍵來源 | 有沒有 `cros_ec_buttons`；沒有就放行 ACPI Power Button | `input.power_button = "acpi"` |

## 驗證順序（照 KANO 走過的順序最省時間）

0. 若目標機器裝有 Windows，進 BIOS 關閉 Secure Boot **之前**，**必須先將每個加密磁碟區的 48 位數 BitLocker 復原金鑰備份到另一台裝置**（見上方說明；`Suspend-BitLocker` 不能代替備份復原金鑰）。
1. `GBOS_PROFILE=profiles/<型號> builder/build.sh`：QEMU 自我測試會自動跑（設定檢查在最前面）。
2. 寫到 USB，用 serial-debug 選項開機（需要序列埠；沒有的話用 quiet，靠 `/data/kano-diag` 事後讀取）。
3. 顯示（AMD 機型切到 experimental native `amdgpu` 開機選項時，先跑 `dmesg | grep -E 'amdgpu|Direct firmware load'` 確認韌體與 IP block 初始化無 `-ENOENT`）→ OOBE → Wi-Fi → 觸控 → 音效 → 電源鍵／上蓋／休眠，對照 `docs/STATUS-KANO.md` 的方法逐項驗證。
4. 任何需要放寬安全封鎖的功能，先讀 `docs/SAFETY.md`。驗證通過後才把 `model.status` 改成 `verified`。
5. 用 `install/gbos-report.sh` 產生 `summary.md`，開 issue 套用 `.gitlab/issue_templates/new-machine.md`（壞掉的地方用 `bug.md`）。見下面。

## 把結果寄回來

在建置用的 Linux、WSL 或 macOS（bash 3.2 以上與 python3）上跑。live Linux 上的探測仍是上一節的 `gbos-probe.sh`。Googlebook 已開機、`adb devices` 看得到時：

```sh
install/gbos-report.sh --out gbos-report
install/gbos-report.sh --adb --probe gbos-probe --out gbos-report   # 連同上面的 probe 目錄
install/gbos-report.sh --probe gbos-probe --out gbos-report         # 只有 live Linux，不連 adb
```

無線除錯加 `--serial 位址:埠`。腳本只下唯讀指令，沒有 `adb root`，不安裝、不改設定、不推檔案。`summary.md` 沒有自由文字：廠商列舉、CPU 的 family／model／stepping、GPU PCI、已知驅動或 `other`（未知驅動另附 PCI 或 USB id；狀態讀不到時 id 仍留著）、建置編號、只有與映像釘選完全相同的核心版本才原樣印出。設 `GBOS_DEV_KERNEL_DIR=<核心樹>`，且執行中核心的 release 等於該樹的 `include/config/kernel.release` 時，寫 `dev-kernel`（開發核心不印 release）；該樹的 `.config` 是一般檔時，再依它是否等於釘選 GKI 設定（sha256）寫 `config pass`／`config fail`。其他讀得到的 release 寫 `other`、`wlan0`–`wlan9`／`eth0`–`eth9`／`hci0`–`hci99`、`card0`–`card99`／`renderD0`–`renderD999`（`card`、`hci`、`renderD` 都沒有前導零）、Wi-Fi／藍牙狀態、觸控的類型／匯流排／四碼 id、休眠次數。機型名稱、BIOS／EC 版本、CPU 型號名稱、音效卡名稱、這份清單以外的介面名稱都不寫。這些請在 issue 裡自己打。同一台機器才合併 probe：廠商列舉、CPU 三個數字，以及 PCI 匯流排上的全部裝置 id 都要一致；CPU 型號名稱不算。GPU 或 PCI 沒讀完、probe 沒有 `pci` 鍵或清單是 null、擷取檔不在（就算旁邊留著成功的 `.rc`）、或 CPU 數字不齊，就省略。DRM 清單沒擷取到是未知，不能合併，也不從 sysfs 或 uevent 建卡列。成功讀到的空清單，以及沒有 PCI id 的平台 shim，仍可合併。空的 DRM 清單是「未知」，不是「沒有原生 GPU 驅動」。只有成功擷取的清單點名的 card 才算；缺檔、ENOENT、拒絕讀取，或其他非零結束都不是成功擷取。沒被點名的卡的檔案略過，不能把顯示列改成已綁定或沒有原生 GPU 驅動。`ro.hardware.vulkan=pastel` 是這個專案的 SwiftShader（`vulkan.pastel.so`），報告寫 SwiftShader。不含 dumpsys／logcat 原文，也不提供附上原文的開關。缺值或檢查不過是「未知」。辨識到硬體不代表該功能已經驗證過。若需要原始 log，請直接向使用者索取。

新機型開 GitLab issue，選 [`.gitlab/issue_templates/new-machine.md`](.gitlab/issue_templates/new-machine.md)（自己打機型、BIOS 版本、CPU／GPU、哪些能用、哪些不能，附上 `summary.md`）。某一項壞掉則用 [`.gitlab/issue_templates/bug.md`](.gitlab/issue_templates/bug.md)。報告不會替你填那些描述。

## amd-hx370 native 選項：實機關卡

QEMU 沒有 amdgpu，self-test 只開 safe。native 選項在實機上通過下面每一項之前，不算驗收。上面『驗證順序』第 3 步的韌體與畫面檢查仍要做。

1. 選 experimental native 開機。`dmesg` 裡 `amdgpu` 與 `Direct firmware load` 沒有 `-ENOENT`。畫面能起來；黑畫面就重開，改選 safe。
2. 配置、map、export、import、釋放都走完，safe 與 native 載入的是同一顆 `/vendor/lib64/libminigbm_gralloc.so`。
3. **YV12 / GFX10+（硬體關卡）**。`amdgpu_linear.c` 為了 Android 的 chroma stride，把 luma 對齊到兩倍的 256-byte alignment。640×480 TEXTURE 的三個平面 pitch 是 1024 / 512 / 512，不是上游在「stride 盡量小」時會得到的 768。Radeon 890M 是 GFX11.5，落在上游 `family >= AMDGPU_FAMILY_NV` 的範圍。實機要配置 YV12 640×480、usage TEXTURE，用 `VkImageDrmFormatModifierExplicitCreateInfoEXT`（modifier LINEAR，pitch 1024/512/512）匯入 RADV 並取樣。RADV 拒絕這個 pitch 的話，改走上游分支：在 `BO_USE_GPU_HW` 且 `stride/256` 為奇數時不加倍，改用 `DRM_FORMAT_YVU420` 並記錄。這項不是 QEMU 檢查。
4. `SurfaceFlinger: trackPendingFrame: Invalid present fence` 不反覆出現。QEMU 上原廠庫也會出現同一對 log，所以那一行本身不是這顆重建庫的回歸；實機仍要確認它沒有一直刷。

## 還沒做的

- `base/` 仍是 KANO 那份基礎映像的逐位元組重建（`kano-real.img`），它 ESP 裡的開機選項（含 `xe.force_probe=46a6`）
  builder 已經不再使用：開機參數一律由 vendor_boot 的原廠參數、機型設定與安全規則產生。
- vendor 裡的服務與腳本名稱仍是 `kano-*`（功能通用；改名會讓 KANO 映像與已驗證的版本不同）。
- 一般 PC 的通用顯示 fallback 已是 `profiles/generic-x86`（只在 QEMU 驗證過）；AMD HX 370 的 `profiles/amd-hx370` 預設走相同的 safe GOP + SwiftShader 路徑（已在 QEMU 開到設定精靈驗證）。safe 與 native 兩個選項載入同一顆 `/vendor/lib64/libminigbm_gralloc.so`（`[gralloc]`、`tools/minigbm`：`backend_amdgpu` 與 `backend_vkms` 都在這顆檔裡，差別是開機選項選哪個 backend）。QEMU 沒有 amdgpu，所以 native 選項不在 self-test 裡。實機仍未驗證，見 `research/amd/PLAN.md`。
