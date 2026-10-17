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

另有針對 **AMD Ryzen AI 9 HX 370 / Radeon 890M** 的 `profiles/amd-hx370/`（需搭配自建的 Track 2 GKI 核心 `GBOS_DEV_KERNEL_DIR` 與 `tools/mesa/build-radv.sh` 建出的 RADV，目前只在 QEMU 驗證過）：建置時產生兩個固定開機選單項目，**不做執行期顯示切換**：
- **預設 `"Googlebook AMD HX370 safe"`**（`lapis-quiet.conf`）：`amdgpu` 列入 `module_blacklist` 且不載入、不帶 `amdgpu.*` 參數，走與 `generic-x86` 相同的已知可用路徑（GOP framebuffer + `gbos_efifb` + SwiftShader `pastel`），已在 QEMU 開到設定精靈驗證。
- **實驗 `"Googlebook AMD HX370 native amdgpu (experimental)"`**（`lapis-debug.conf`）：在 `early-boot` 載入一次 `amdgpu`、絕不啟動 `gbos_efifb`，設定 `ro.hardware.vulkan=radeon` 並依 sysfs `/sys/class/drm/card*/device/uevent` 的 `DRIVER=amdgpu` 自動選擇 HWC 的 DRM 裝置。**誠實揭露**：此選項尚未在實機測試，且目前 lapis 映像的 `minigbm` gralloc 尚未包含 `amdgpu` 後端；若選此項出現黑畫面，請重開機選預設的 `"Googlebook AMD HX370 safe"`。

**Windows / BitLocker 必做前置步驟（進 BIOS 改 Secure Boot 之前）**：在任何裝有 Windows 的 PC 上，進 BIOS 動 Secure Boot 之前，先在管理員 PowerShell 執行 `manage-bde -status` 檢查所有磁碟區；只要有任何磁碟區啟用了 BitLocker，**必須先把每個加密磁碟區的 48 位數 BitLocker 復原金鑰（Recovery Key）備份並存到另一台裝置**（<https://aka.ms/myrecoverykey> 或對 `manage-bde -status` 列出的每個加密磁碟區執行 `manage-bde -protectors -get <磁碟區>`，例如 `C:`、`D:`）。切換 Secure Boot 可能會改變開機量測與 BitLocker 所綁定的 TPM PCR（例如 PCR 7，或依平台設定綁定的 PCR 0、2、4、11），下次開回 Windows 時**可能會要求輸入 48 位數復原金鑰**。`Suspend-BitLocker -MountPoint "C:" -RebootCount 1` 只是備份好復原金鑰之後的選用便利指令（暫停期間會把明文金鑰留在磁碟上，有保護空窗，且重開機次數用完後仍會檢查 TPM PCR；回到 Windows 後請用 `manage-bde -status`／`Resume-BitLocker` 確認保護已恢復），**絕對不能代替備份 48 位數復原金鑰**。

| 檔案 | 內容 |
|---|---|
| `profile.toml` | 機型名稱與映像檔名、選單標題、開機參數（要拿掉的原廠參數、加上的參數、quiet 專用移除參數 `quiet_drop`、pci-stub ID、first stage 不載入的模組）、`module_blacklist`（含 quiet 專用追加 `quiet_add`）、韌體清單（來源與 SHA-256）、音效卡、Wi-Fi 要載入的模組、顯示設定、鍵盤背光 LED 路徑、電源鍵來源 |
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

## 還沒做的

- `base/` 仍是 KANO 那份基礎映像的逐位元組重建（`kano-real.img`），它 ESP 裡的開機選項（含 `xe.force_probe=46a6`）
  builder 已經不再使用：開機參數一律由 vendor_boot 的原廠參數、機型設定與安全規則產生。
- vendor 裡的服務與腳本名稱仍是 `kano-*`（功能通用；改名會讓 KANO 映像與已驗證的版本不同）。
- 一般 PC 的通用顯示 fallback 已是 `profiles/generic-x86`（只在 QEMU 驗證過）；AMD HX 370 的 `profiles/amd-hx370` 預設走相同的 safe GOP + SwiftShader 路徑（已在 QEMU 開到設定精靈驗證），其 experimental native `amdgpu` 選項與還缺的 `minigbm` `amdgpu` gralloc 後端見 `research/amd/PLAN.md`。
