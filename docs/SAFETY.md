# 安全模型

Googlebook（lapis）映像本來是給另一台機器用的，裡面帶著那台機器的韌體與更新程式。KANO 的韌體一旦被寫壞就會變磚，
所以第一原則是：**從 Android 這一側，任何東西都不能寫入 AP SPI flash、EC、Cr50、觸控／PD／retimer 韌體或 EFI 變數。**
啟用任何硬體之前，先證明沒有更新程式碰得到它。下面寫清楚每一層擋住什麼、沒擋住什麼。

## 多層防護

1. **移除 vendor 分割區裡已知的更新程式與燒錄工具**：fwupd（含 LVFS 設定與離線 cab）、flashrom、libflashrom、futility、
   futility-executor、ectool、crossystem、`firmware/ap-ec-fw.zip`。build 時刪除，刪除清單（`removed.txt`）逐項比對。
   範圍只有 vendor：system、system_ext、product 與其他 APEX 保持 Google 原樣，沒有逐一掃描。真正的兜底是下一層的核心封鎖。
2. **核心模組封鎖（`module_blacklist=`）**：TPM（tpm、tpm_tis*、tpm_tis_i2c_cr50）、EC 傳輸與核心（cros_ec、cros_ec_lpcs、
   cros_ec_dev、cros_ec_chardev、cros_ec_spi/i2c/ishtp、cros_ec_sysfs、cros_ec_vbc、cros_ec_debugfs …，共 30 多個）、
   SPI flash（spi_intel、spi_intel_pci、mtd、spi_nor、spidev）、gsmi、chromeos_acpi、nvram、i2c_dev、at24、thunderbolt。
   只放行 `cros_ec_proto`（純函式庫）給 ACPI 鍵盤背光驅動用；沒有任何 EC 裝置會被註冊，所以送不出 EC 指令。
   這是預設映像。開發者選項 `GBOS_EC`（預設關閉）會放行一組經過稽核的 EC 驅動，見下面「EC 功能變體」。
   原映像的 `spi-intel.writeable=1` 也拿掉了。清單在機型設定（`profiles/kano/profile.toml`）；全專案一定要封鎖的模組與參數在
   `profiles/_safety.toml`（`blacklist.required` 就是上面這整份 57 個模組，每個機型都一樣），機型設定只能加、不能減（`builder/lib/devprofile.py` 拒絕），`builder/image.py` 再對每個開機選項逐一檢查，並確認 ESP 上只有 quiet 與 serial-debug 兩個選項
   （base ESP 原本那個沒有封鎖的 `lapis.conf` 會被刪掉）。
3. **`efi=noruntime`**：Linux 不能呼叫 EFI runtime services，所以 OS 寫不了 EFI 變數，也送不出 capsule 更新。
   `efi=noruntime` 不約束核心自己的 EFI stub（stub 在 `ExitBootServices` 之前就結束）。自建核心閘門拒絕會在 stub 裡寫 NV 變數的選項。
4. **裝置節點**：實機上沒有 `/dev/mem`、`/dev/port`、`/dev/mtd*`、`/dev/spidev*`。
5. **USB 映像的 `initcall_blacklist=nvme_init`**：從 USB 開機時看不到內建碟（裝到內建碟的版本才放行 NVMe）。
6. **開機選單編輯器預設關閉（`editor no`）**：在 systemd-boot 選單上無法臨時刪掉上面的封鎖參數。開發用映像可用
   `GBOS_LOADER_EDITOR=yes` 打開。
7. **開發用看守程式**（`tools/harness/kano-gbos-watch*.py`）：序列埠出現 NVMe、SPI、EC、TPM、gsmi、fwupd、rollforward
   等字串就透過 EC 斷電。它只是補充偵測：只在 serial-debug 開機選項下有序列埠輸出可看，看不到紀錄時會回報「無法觀測」，
   不保證在任何情況下都能及時攔下。它拿 KANO 機型設定的整份 `module_blacklist` 核對這次開機的參數，不知道 `GBOS_EC`：
   EC 變體開機時，被放行的 `cros_ec*` 會被當成「少了防護參數」而直接斷電（fail-closed）。用 EC 變體時不要開看守程式。
8. **自建核心 `.config`、`bzImage` 內嵌 `IKCFG` 與 `System.map` 安全檢查**（`builder/lib/devprofile.py check-kconfig`）：只在 `GBOS_DEV_KERNEL_DIR`（例如 `profiles/amd-hx370`）時執行。這個閘門要抓的是開發者自己的核心設定裡的失誤（驅動被編成 built-in，或內建 cmdline 蓋掉 bootloader 的參數）。刻意改寫二進位來騙過閘門的人本來就控制這台機器，不在範圍裡。含糊的映像仍然拒絕，因為失誤也藏在同樣的地方：超過一個說得通的 payload 或 IKCFG、HdrS 指名的 payload 不是 `scripts/extract-ikconfig`（kernel/common `3938c8d97142`：先找原始 `IKCFG_ST`+gzip，再依序試 gzip、xz、bzip2、lzma、lzo、lz4 legacy、zstd）會採用的那一份，或 `.symtab` 與執行期 kallsyms 對要查的符號不一致。執行期 kallsyms 優先。解得出的格式是 gzip、LZ4 legacy、xz、lzma-alone、zstd、bzip2 與未壓縮 ELF；`CONFIG_KERNEL_LZO` 與認不得的 magic 一律拒絕。掃描路徑對 xz、lzma 在出錯時把已解出的明文留到尾端（先以 4096 byte 取，沒解完就把尾段改成每次 1 byte，最多少錯誤當下那 1 byte），IKCFG 是否算命中比照 `dump_config`（`IKCFG_ST` 後面是一個 zcat 結束碼不是 1 的 gzip，不要求 `IKCFG_ED`），所以 IKCFG 在 member 明文尾端時和 `unxz`／`unlzma` 一樣算候選；bzip2 仍用 4096／64 byte 分段。誠實 member 解到結尾就用第一輪的完整輸出。`dump_config` 的模擬則和腳本一樣，gzip CRC 錯誤（zcat 結束碼 1）不算命中。切片必須是單一 member，尾端最多 4 byte 的未壓縮長度。解壓後若有兩對 `IKCFG_ST`／`IKCFG_ED` 也拒絕。
   - **已知限度（合成的壓縮尾，不在威脅模型裡）**：上面這段是在抓開發者自己 `.config` 的失誤，不是在證明閘門跟一份刻意拼出來的核心逐 byte 相同。下面幾種合成形狀不拿來當那個主張：gzip 明文尾端沒有 `IKCFG_ED`（`dump_config` 只在 `IKCFG_ST` 後面接到一個 zcat 結束碼不是 1 的 gzip member 時算命中；少掉的若不止那個結束標記，不在此列）；bzip2 的壓縮尾（`recover_tail` 關著，仍是 4096／64 byte 分段，不會像 xz／lzma 把出錯前的明文再逐 byte 補回來）；xz 的 `"none"` 夾具（IKCFG 前面沒有較早的明文，第一輪 4096 byte 排空是空的）；以及「最多少 1 byte」這句話（只描述 xz／lzma 第二輪裡，錯誤發生當下正在讀的那 1 byte；bzip2 不適用，`unxz`／`unlzma` 仍可能把那 1 byte 寫出來）。這些是合成夾具，不是可以開機的核心。
   - 要求 `CONFIG_KALLSYMS=y`（否則 `initcall_blacklist=` 無效）以及 `CONFIG_MODULES=y`、`CONFIG_MODULE_UNLOAD=y`、`CONFIG_MODVERSIONS=y`、`CONFIG_IKCONFIG=y`、`CONFIG_IKCONFIG_PROC=y`；
   - 拒絕 `CONFIG_CMDLINE_OVERRIDE=y`／`CONFIG_CMDLINE_FORCE=y` 以及 `CONFIG_BOOT_CONFIG_EMBED=y`／`CONFIG_BOOT_CONFIG_FORCE=y`（避免丟棄或覆寫 ESP 開機參數與 initrd bootconfig），並檢查 `CONFIG_CMDLINE` 內不得含有 `--`（`kernel/params.c` 會在 `--` 停止解析後續的開機參數）、禁用前綴（同時將參數鍵名的 `-` 與 `_` 正規化比對，攔截 `spi_intel.writeable=1`、`pci_stub.ids=` 等別名）、`stock_drop`／`quiet_drop` 項目或與 safety/profile tail 衝突的鍵；
   - 拒絕 `CONFIG_BLK_DEV_NVME=m` 或 `CONFIG_INTEL_MEI_ME=m`（除非 `nvme`／`mei_me` 在 `module_blacklist`，因為模組不受核心主體 `initcall_blacklist=` 約束），且當 `CONFIG_BLK_DEV_NVME=y` 或 `CONFIG_INTEL_MEI_ME=y` 時**強制要求 `System.map` 存在**、包含文字符號（`t`/`T`）`nvme_init`／`mei_me_driver_init`。有 `.symtab` 時整張表要與 `System.map` 相同，而且若 `System.map` 裡有 `kallsyms_*`，還要核對執行期那張表；兩邊不一致就拒絕，不能靠 `.symtab` 跳過。剝除過的 bzImage（`objcopy -S`，step-1 就是這條）只走 kallsyms：位置由 `System.map` 的 `kallsyms_*` 指出。核對的是那兩個 initcall，加上 `scripts/kallsyms.c` 在沒有 `--all-symbols` 時仍保留的 `_stext`、`_etext`、`_sinittext`、`_einittext`（範圍兩端都包含，`scripts/kallsyms.c:50-52,175-188,196-225`；`scripts/link-vmlinux.sh:141-142` 只在 `CONFIG_KALLSYMS_ALL=y` 時加 `--all-symbols`）。`__init_end` 在 `INIT_DATA_SECTION` 與 `PERCPU_SECTION` 之後（`arch/x86/kernel/vmlinux.lds.S:310,414,429`），超過 `_einittext`，所以 `KALLSYMS_ALL=n`（Kconfig 預設）的執行期表沒有它；閘門只在 `CONFIG_KALLSYMS_ALL=y` 時才另外核對 `__init_begin` 與 `__init_end`。initcall 的名字在 `.init.text` 裡，不論 `ALL` 都會留下，這才是 `initcall_blacklist=` 比對的對象。不是整張 `System.map` 逐項全等。`.symtab` 全表比對讀到的型別字母是測試夾具寫進 `st_other` 的約定，不是一般 ELF 的 visibility 欄位；出貨的 bzImage 已剝除，不靠這條路徑接受未剝除的 vmlinux。`CONFIG_LTO_CLANG`／`CONFIG_LTO_CLANG_THIN`／`CONFIG_LTO_CLANG_FULL` 在 NVMe 或 MEI 為 `=y` 時拒絕（`CONFIG_LTO_NONE` 才是接受的設定）。拒絕 built-in `CONFIG_ATA`／`CONFIG_SATA_AHCI`／`CONFIG_SATA_AHCI_PLATFORM`。現有 profile 的黑名單沒有 `ahci`，所以 `CONFIG_SATA_AHCI=m` 會被拒；`libahci` 是跟 `ahci` 一起建的（`drivers/ata/Makefile:6`），將來若把 `ahci` 放進黑名單，必須同時放入 `libahci`。`linux_banner` 的 release 家族檢查（`Linux version 6.18.<n>-android17-`）在 System.map 能定位 `linux_banner` 時都會做，不只在 NVMe 或 MEI 為 built-in 的時候；這是 release 家族，不是 `3938c8d97142` 的逐 commit 認證。兩者都不是 `=y` 且沒有 System.map 的正常核心仍接受。
   - 拒絕 `CONFIG_EFIVAR_FS`、`CONFIG_EFI_CAPSULE_LOADER`、`CONFIG_DEVMEM`（`/dev/mem`）、`CONFIG_DEVPORT`（`/dev/port`）、`CONFIG_PROC_KCORE`（`/proc/kcore`）、`CONFIG_X86_MSR`（`/dev/cpu/*/msr`）、`CONFIG_ACPI_CUSTOM_METHOD`、`CONFIG_ACPI_EC_DEBUGFS`、`CONFIG_ACPI_DEBUGGER_USER`、`CONFIG_ACPI_CONFIGFS`、`CONFIG_ACPI_CUSTOM_DSDT`（`=y` 或 `=m` 皆拒）以及 `CONFIG_STRICT_DEVMEM=n`／`CONFIG_IO_STRICT_DEVMEM=n`；
   - `blacklist.required` 的 57 個模組裡，55 個對得上樹內符號；`cros_ec_usb` 與 `cros_ec_pwrmon` 是 lapis vendor_dlkm 的樹外模組。`_safety.toml` 沒有 `intel_thc_hid`；樹內 THC 的模組名是 `intel_thc`。`CONFIG_CROS_EC_UART` 與 `CONFIG_CROS_EC_RPMSG` 不在這份 v0.1 清單（改清單會改到釘死的 KANO cmdline），但 `=y` 會被拒，`=m` 則該 profile 的黑名單必須有 `cros_ec_uart`／`cros_ec_rpmsg`；EC 變體另外用 `EC_EXTRA_BLOCK` 帶上這兩個名字。bool `CONFIG_TCG_TIS_SPI_CR50=y` 只在 `CONFIG_TCG_TIS_SPI=m` 且 `tpm_tis_spi` 在黑名單時允許（它折進 `tpm_tis_spi.o`，`drivers/char/tpm/Kconfig:85`、`Makefile:30`）。其餘 built-in 規則（含 TPM、SPI、`CONFIG_SPI_AMD` 同時要求 `spi_amd` 與 `spi_amd_pci`、MTD、ChromeOS EC 與 Wilco、`chromeos_acpi`、`cros_hps_i2c`、`cros_ec_ucsi`、QuickI2C/QuickSPI、`intel_thc`、gsmi、nvram、`i2c-dev`、`at24`、`ee1004`、`thunderbolt`／`USB4`、SDHCI、CCP／TEE／`amdtee`、`CONFIG_AMD_SFH_HID`、`CONFIG_SP5100_TCO`、`CONFIG_UCSI_ACPI`）依 `3938c8d97142` 的 `Kconfig` 與 `Makefile` 拒絕 `=y`；`=m`（或 bool `CONFIG_CRYPTO_DEV_CCP=y`／`CONFIG_CRYPTO_DEV_SP_PSP=y` 搭配 `CONFIG_CRYPTO_DEV_CCP_DD=m`）時模組名必須在 `blacklist.modules`。`modules/*.ko` 若檔名落在永不載入的集合（`msr`、`efivarfs`、`capsule_loader`、`ec_sys`、`acpi_dbg`、`acpi_configfs`）就拒絕；若檔名是上面某個模組，對應的 Kconfig 必須是 `=m` 且名字在黑名單裡。`CONFIG_AMD_PMC=m` 是釘選的 step-1 核心為 s2idle 編成的模組（`drivers/platform/x86/amd/pmc/pmc.c:844` 的 `amd_pmc_driver_init`），不在 `module_blacklist` 裡，閘門不拒絕它。
   - 在 `amd-hx370` 上強制要求 `CONFIG_DRM_AMDGPU=m`（拒絕 `=y`，確保預設的 safe 開機選項能用 `module_blacklist` 擋下 `amdgpu`）且拒絕 `CONFIG_DRM_AMD_ISP`。
   - **EFI／韌體持久寫入（自建核心）**：拒絕 `CONFIG_RESET_ATTACK_MITIGATION`（見第 3 層；`libstub/` 裡受設定控制的 `set_efi_var` 只有 `tpm.c:42` 這一處）。`CONFIG_ACPI_APEI` 會 `select PSTORE` 並把 `erst.o` 編進 `apei.o`（`drivers/acpi/apei/Kconfig:8-11`、`Makefile` 的 `apei-y`）。有 ERST 表時 `erst_init`（`erst.c:1300`）在 `:1266` `pstore_register`，oops／panic 就寫韌體持久區。`erst_disable`（`erst.c:998`）讓 `erst_init` 在 `:1199` 返回，ERST debug 裝置也不註冊（`erst-dbg.c:210`）。閘門只在 quiet 與 serial-debug 兩個實際寫出的開機選項都帶 `erst_disable` 時允許 `CONFIG_ACPI_APEI`（看 `make_entries` 的結果，不只看宣告的 tail；`quiet_drop` 拿掉這個 token 會拒絕）。token 只在 `amd-hx370` 的 tail，不加進釘死的 KANO cmdline。`CONFIG_EFI_VARS_PSTORE` 在每一個實際開機選項都帶安全 tail 的 `efi_pstore.pstore_disable=1` 時允許：`efi-pstore.c:51` 的 `pstore_disable` 在 initcall 前生效，`:261` 就不註冊；原廠 lapis 這個選項是 `=y`（`lapis-kernel.config:2109`）。`load()` 不允許 drop 清單拿掉安全 tail 的 token。`CONFIG_EFI_CAPSULE_LOADER` 與 `CONFIG_EFIVAR_FS` 維持拒絕。`CONFIG_ACPI_PFRUT` 同樣拒絕（韌體更新介面，`efi=noruntime` 擋不到；本輪沒有重讀釘選驅動）。`random.c:142` 刪除 `RandomSeed` 沒有 Kconfig 開關（`x86-stub.c:914` 在 `exit_boot` 前呼叫），原廠 stub 也會做，不在閘門範圍。`CONFIG_EFI_BOOTLOADER_CONTROL` 在 reboot notifier 寫 `LoaderEntryRebootReason`（`efibc.c:22,45`），但 `efibc.c:72` 在沒有 runtime `SetVariable` 時直接 `-ENODEV`，`efi=noruntime` 擋得住。`CONFIG_PSTORE_RAM` 寫 RAM（lapis 有開），不是韌體 flash。`CONFIG_PSTORE_BLK` 不是開機就寫韌體的路徑（lapis 沒開），不另設拒絕。`CONFIG_ACPI_APEI_ERST_DEBUG` 的裝置在 `erst_disable` 時不註冊（`erst-dbg.c:210`）。`CONFIG_ACPI_APEI_EINJ` 是 debugfs、要 root 才寫，和下面的韌體設定驅動同一類，不自動寫。

9. **profile `[modules]` early-boot modprobe**（`profiles/_safety.toml` 的 `[modules]`，檢查在 `builder/lib/devprofile.py`）：機型可以列出要在 `early-boot` 同步 `exec` 的 `modprobe`。命令是 `/vendor/bin/modprobe --dirname=<vendor 或 dev 目錄> <名字>`，沒有 `-b`。kano 與 generic-x86 沒有 allowlist 列，它們的 `kano-hw.rc` 開頭不因這層而改變。這層的限度：
   - 名字必須在該 profile id 的 allowlist 上。`blacklist.required`、該 profile 的 `blacklist.modules`／`debug_add`／`quiet_add` 都不能進。`_NEVER_SHIP_KO`（`msr`、`efivarfs`、`capsule_loader`、`ec_sys`、`acpi_dbg`、`acpi_configfs`）也不能進。儲存／韌體更新類用的是 kconfig 閘門已經對上的模組名，不是新的萬用字元：`KCONFIG_INITCALL_BLACKLIST` 的 `nvme` 與 `mei_me`，SDHCI／MTD／SPI flash（控制器、SPI-NOR、spidev）／Thunderbolt／TPM／CCP 在 `KCONFIG_BUILTIN_MODULE` 裡的名字，以及 SATA 的 `libata`、`ahci`、`ahci_platform`、`libahci`。
   - `unblock` 只能點 stock `modules.blocklist` 上的模組。名單只有 `STOCK_MODULES_BLOCKLIST` 一處（目前 `snd_hda_intel`；出貨的 `vendor_dlkm` `lib/modules/modules.blocklist` 是一行 `blocklist snd_hda_intel`，這個 builder 不重寫該分割區）。`load` 不能點這些名字。省略 `-b` 只跳過那份 userspace 檔；**核心命令列的 `module_blacklist=` 仍然生效**，依賴模組也算。
   - 依賴順序不看列是 `vendor` 還是 `dev`：allowlist 裡較早的每一列都必須先被選到，所以 `bluetooth` 在 `btusb` 前面。同一張表內照 allowlist 順序；所有 `load` 行都在 `unblock` 行之前輸出（codec 先於 `snd_hda_intel`）。`exec` 會等這次 `modprobe` 結束。模組 probe 之後的 codec／韌體路徑是非同步的，這不表示裝置已經綁定。
   - 這層只把模組載入排進 early-boot。喇叭、Wi-Fi、藍牙是否可用，以實機紀錄為準。

## EC 功能變體（`GBOS_EC`，預設關閉）

`GBOS_EC=sensors,typec,buttons`（可以只選其中幾項；只稽核過 KANO，其他機型的設定會被拒絕）是給想要自動旋轉、USB-C 的 USB3／DisplayPort、側邊音量鍵的人用的。
它會改寫上面第 2 層的保證：從「沒有任何 EC 裝置被註冊，送不出 EC 指令」，變成
「**只有下面這組經過稽核的核心驅動會送 EC 指令，其中沒有任何指令會寫 EC 或周邊的 flash、CBI、I²C，或讓 EC 重開**」。
這組包括放行的模組，以及每個選項本來就放行、在變體下也會走 EC 的 `cros_kbd_led_backlight`（見稽核表）。
它們送出的寫入類指令只改 EC 的 RAM 狀態（EC 重開就恢復）。

**放行的模組**（其餘全部維持封鎖）：

| 功能 | 模組 | 作用 |
|---|---|---|
| 共同 | `cros_ec`、`cros_ec_lpcs`、`cros_ec_dev` | EC 核心、LPC/eSPI 傳輸（ACPI GOOG0004）、EC 裝置 |
| `sensors` | `cros_ec_sensorhub`、`cros_ec_sensors_core`、`cros_ec_sensors`、`cros_ec_lid_angle` | 上蓋與底座的加速度計、陀螺儀、開合角度（IIO） |
| `typec` | `cros_ec_typec`、`cros_usbpd_notify` | USB-C 的模式切換（USB3、DP alt mode），mux 由已載入的 `intel_pmc_mux` 設定 |
| `buttons` | `cros_ec_keyb` | `cros_ec_buttons`：音量鍵、上蓋與平板模式開關 |

**永遠封鎖**（`builder/image.py` 的 `EC_NEVER`，是 `profiles/_safety.toml` `blacklist.required` 的一部分；每個機型、每個變體的每個開機選項都逐一檢查）：`cros_ec_chardev`（`/dev/cros_ec`）、`cros_ec_debugfs`、
`cros_ec_sysfs`（`reboot`、`kb_wake_angle`）、`cros_ec_vbc`、`cros_ec_lightbar`、`i2c_cros_ec_tunnel`（經 EC 的 I²C）、
`cros_charge_control`、`leds_cros_ec`、`cros_usbpd_charger`（可寫輸入電流上限）、`cros_usbpd_logger`、`cros_peripheral_charger`、
`cros_typec_switch`、`cros_ec_ucsi`、`cros_ec_spi`、`cros_ec_i2c`、`cros_ec_ishtp`、`cros_ec_usb`，以及 TPM、SPI flash（`spi_intel`、`spi_intel_pci`、`mtd`、`spi_nor`、`spidev`）、`thunderbolt`
（USB4 retimer 的 NVM 更新路徑）、gsmi、nvram、`chromeos_acpi`。變體另外封鎖這份 lapis 沒有、但將來的 vendor_dlkm 可能加進來的
`gpio_cros_ec`、`cros_ec_hwmon`、`cros_ec_wdt`、`rtc_cros_ec`、`cros_ec_uart`、`cros_ec_rpmsg`。
`cros_ec_dev` 仍然會建立 chardev、sysfs、debugfs 等子裝置，但對應的驅動被封鎖，所以 **`/dev/cros_ec`、`/dev/cros_fp`、
`/dev/cros_ish` 永遠不存在**。vendor 裡會寫 CBI、改防寫、做 I²C passthru、切電池的 `ec-executor` 等程式只走這些 chardev，
所以仍然一個指令都送不出去。

**稽核：放行的驅動會送的 EC 指令**（來源：kernel/common `3938c8d97142`，即 lapis 核心的 commit；檔名:行號）

| 模組 | 指令 | 性質 |
|---|---|---|
| `cros_ec` | `HOST_SLEEP_EVENT`（`cros_ec.c:183`，休眠／喚醒時）、`RWSIG_ACTION(CONTINUE)`（`cros_ec_proto.c:306`，註冊時一次） | 休眠通知，見下面「休眠」；RWSIG 在 brya 的 EFS2 EC 上回 `INVALID_COMMAND` |
| `cros_ec_proto`（已放行的函式庫） | `HELLO`、`GET_PROTOCOL_INFO`、`GET_CMD_VERSIONS`、`GET_FEATURES`、`GET_NEXT_EVENT`、`HOST_EVENT_GET_WAKE_MASK`、`MKBP_STATE`、`READ_MEMMAP`、`GET_COMMS_STATUS`、`MOTION_SENSE_CMD(DUMP)` | 只讀 |
| `cros_ec_lpcs` | 只做傳輸；收到 EC panic 通知時關機（`cros_ec_lpc.c:458`） | — |
| `cros_ec_dev` | `PCHG_COUNT`（`cros_ec_dev.c:317`） | 只讀 |
| `cros_ec_sensorhub` | `MOTIONSENSE`：`INFO`、`FIFO_INFO`、`FIFO_READ`、`FIFO_INT_ENABLE`（`cros_ec_sensorhub_ring.c:126`） | 讀，或改 EC RAM 的 FIFO 中斷設定 |
| `cros_ec_sensors_core`、`cros_ec_sensors`、`cros_ec_lid_angle` | `MOTIONSENSE`：`DATA`、`EC_RATE`、`SENSOR_ODR`、`SENSOR_RANGE`、`SENSOR_OFFSET`、`SENSOR_SCALE`、`PERFORM_CALIB`、`FIFO_FLUSH`、`LID_ANGLE` | 改取樣率、量程、校正值，存在 EC RAM，不碰 flash |
| `cros_ec_typec` | `TYPEC_STATUS`、`TYPEC_DISCOVERY`、`USB_PD_PORTS`、`USB_PD_MUX_INFO`、`USB_PD_MUX_ACK`、`TYPEC_CONTROL`（`ENTER_MODE`、`EXIT_MODES`、`CLEAR_EVENTS`、`SEND_VDM_REQ`）、`USB_PD_CONTROL`（使用者寫 typec 的 `data_role`／`power_role` 時做角色交換）、`TYPEC_VDM_RESPONSE`（`cros_typec_vdm.c:32,70`，讀對接裝置回的 VDM） | PD 的執行期動作：進出 DP 模式、角色交換、送 DP 的 VDM 給對接的裝置；沒有韌體更新 |
| `cros_usbpd_notify` | `PD_HOST_EVENT_STATUS`（`cros_usbpd_notify.c:77`） | 只讀 |
| `cros_ec_keyb` | `MKBP_INFO`（`cros_ec_keyb.c:368`） | 只讀 |
| `cros_kbd_led_backlight`（每個選項都放行，給鍵盤背光用） | `PWM_GET_KEYBOARD_BACKLIGHT`、`PWM_SET_KEYBOARD_BACKLIGHT`（`cros_kbd_led_backlight.c:146,164`）。只在變體下：EC 回報 `EC_FEATURE_PWM_KEYB` 時，`cros_ec_dev` 會建立 `cros-keyboard-leds` 子裝置（`cros_ec_dev.c:106,150-152`），這個驅動也綁上它 | 讀、改 EC RAM 裡的背光 PWM，不碰 flash。這個子裝置沒辦法單獨封鎖；預設映像沒有 EC 裝置，背光只走 ACPI GOOG0002。同一個 LED 名稱會註冊兩次（第二個自動改名），idc 用的是 GOOG0002 那條路徑；變體下沒有在實機驗證過 |

沒有任何放行的驅動會送 `FLASH_*`、`REBOOT_EC`、`I2C_PASSTHRU`、`SET_CROS_BOARD_INFO`、`VBNV_CONTEXT`、`BATTERY_CUT_OFF`、
`CHARGE_CONTROL`、`USB_PD_FW_UPDATE`、`EXTERNAL_POWER_LIMIT` 或 `PWM_SET_FAN_DUTY`。vendor_dlkm 的這些 `.ko` 是另一棵樹編的
（scmversion `ga3bbc4527b7b`，不公開），稽核依據的是同一個核心版本的公開原始碼；這些模組的字串裡沒有原始碼以外的 EC 功能
（`cros-ec-proto.ko` 裡的整張指令名稱表是 tracepoint 用的）。換 vendor_dlkm 或 lapis 版本時要重新稽核。

**休眠（最重要的使用風險）**：載入 `cros_ec_lpcs` 後，核心每次休眠都會告訴 EC「進入 S0ix」。EC 等 10 秒；如果 SoC 沒有真的進入
S0ix（SLP_S0# 沒拉起），EC 會把 AP 叫醒；AP 醒來後 10 秒內沒有回報「已喚醒」，EC 就會強制重設 AP（`power/host_sleep.c`）。
AP 正常醒來時只會多一次喚醒，核心記一行 `EC detected sleep transition timeout`；只有 AP 卡住時才會被重設。
所以 **這個變體要在「目前的映像量到 S0ix」之後才使用**，並且先從隨身碟測試，不要直接裝到內建碟。核心沒有參數可以關掉這個偵測
（`suspend_timeout_ms` 只在被封鎖的 `cros_ec_debugfs` 裡）。

**沒有在 build 時驗證的部分**：`sensors` 為了自動旋轉，就地改了 vendor 的 framework RRO（`config_supportAutoRotation`）。
PackageManager 是否仍啟用這個 overlay，build 與 QEMU 都沒有檢查；在實機上用 `cmd overlay list` 確認。如果它被停用，同一個
overlay 裡的自動亮度、power_profile 等設定也會一起消失。

**其他行為改變**：EC panic 時 AP 會正常關機（ChromeOS 的行為）；USB-C 的角色交換只在使用者或 USB HAL 寫 typec sysfs 時發生；
電源鍵維持 ACPI Power Button，`cros_ec_buttons` 的電源鍵沒有對應（`usr/keylayout/cros_ec_buttons.kl`），所以不會按一下觸發兩次。
`thunderbolt` 仍封鎖，所以 TBT／USB4 的 PCIe 通道與 TBT 底座不能用。

## 沒有涵蓋的範圍

- **韌體與開機程式自己的 EFI 變數寫入**：`efi=noruntime` 只約束 Linux。UEFI 韌體本身會更新 `Boot####`、`BootOrder` 等變數；
  systemd-boot 選單上的按鍵 `d`／`D`（預設項目）、`+`／`-`／`t`／`T`（等待時間）會寫入 NV 變數，`f`、`F2`、`F10`、`Del`、`Esc`
  會重開進韌體設定（寫入 `OsIndications`）。loader.conf 設了 `auto-firmware no`，選單裡不會出現進韌體的項目，但按鍵本身仍有效。在 MrChromebox UEFI 上，這些變數存在
  AP SPI flash 的 SMMSTORE 區。不要在選單上按這些鍵。ESP 裡沒有 `loader/random-seed`，所以 systemd-boot 不會寫
  `LoaderSystemToken`。
- **韌體設定寫入類驅動（已知限制）**：`CONFIG_HP_BIOSCFG`、`CONFIG_THINK_LMI`、`CONFIG_DELL_WMI_SYSMAN`、`CONFIG_ASUS_WMI` 設成 `=y` 時，閘門目前不拒絕。它們可以經 WMI／SMM 寫 BIOS 設定，和已經拒絕的 `DEVPORT`／`X86_MSR` 是同一類、需要 root 的能力。原廠 lapis 與釘選的 step-1 都不編這些驅動。這四個驅動的 `.ko` 若出現在 `modules/`，basename 不在閘門的對照表裡，同樣不拒絕。這是文件化的限制，不是安全尾已經擋住的項目。
- **具備 `CAP_SYS_RAWIO` 的特權 root 程序與 x86 I/O port 指令**：原廠 lapis 與 Track 2 自建核心皆關閉 `CONFIG_DEVMEM`、`CONFIG_DEVPORT`、`CONFIG_PROC_KCORE`、`CONFIG_X86_MSR`、`CONFIG_ACPI_CUSTOM_METHOD`，因此 `/dev/mem`、`/dev/port`、`/proc/kcore`、`/dev/cpu/*/msr` 等裝置與 procfs 節點完全不存在；然而 x86_64 PCI／傳統驅動需要 `CONFIG_HAS_IOPORT=y`，且核心維持 `CONFIG_X86_IOPL_IOPERM=y`，因此具備 `CAP_SYS_RAWIO` 的 root 程序理論上仍可呼叫 `ioperm(2)`／`iopl(2)` 直接執行 CPU I/O port 指令。這個安全模型旨在移除了燒錄工具與裝置驅動後防止系統服務誤寫韌體，並非針對已取得 `CAP_SYS_RAWIO` 的惡意 root 程式碼提供核心層隔離。
- **非 PCI／ACPI 的其他 SD/MMC 主控制器**：`amd-hx370` 等 profile 封鎖了 `sdhci_pci` 與 `sdhci_acpi`（且 `check-kconfig` 拒絕 built-in `CONFIG_MMC_SDHCI_PCI`／`CONFIG_MMC_SDHCI_ACPI`），可遮蔽透過 PCI 或 ACPI 掛載的內建 SD/eMMC（HX 370 使用內建 NVMe）；在 `generic-x86` 或使用其他非 PCI/ACPI MMC 控制器驅動的機器上，內建 eMMC 仍可能對 Android 可見。
- **i2c-hid 裝置的韌體**：觸控板、觸控螢幕的韌體理論上可以由 root 透過 hidraw 更新。vendor 裡唯一的更新程式（fwupd 與
  Elan 的 cab）已移除，但核心層沒有封鎖 hidraw。
- **指紋 MCU（FPMCU）的燒錄路徑**：vendor 的 `com.android.hardware.fingerprint.crosfp` APEX 沒有移除（指紋功能要用它），
  裡面的指紋服務有 FPMCU 韌體的更新路徑。這條路徑碰不到，**只**因為 `/dev/cros_fp` 永遠不會存在：建立它需要 FPMCU 的 SPI
  傳輸 `cros_ec_spi` 與 `cros_ec_chardev`，兩者在每個開機選項（包括每個 EC 變體）都封鎖（`EC_NEVER`，也在
  `blacklist.required` 裡）。這是已知的殘留風險：開發用映像打開選單編輯器（`GBOS_LOADER_EDITOR=yes`）後，可以在選單上臨時
  拿掉封鎖；將來要放行 FPMCU 的傳輸，必須先處理這個 APEX 的更新程式。
- **system 分割區的 `gscutil.rc`**：原映像開機時會跑 `perform-os-startup`（GSC／Cr50 韌體更新）。system 分割區維持原樣，
  這條路徑只靠 TPM 模組（`tpm`、`tpm_tis*`、`tpm_tis_i2c_cr50`）被封鎖而失敗；放行 TPM 之前必須先處理它。
- **system 分割區的 OS 更新**（update_engine）維持原樣。它更新的是 Android 分割區，不是韌體；套用 Google 的 OTA 會蓋掉這裡的修改，結果未測試。

## 資料安全取捨（日常使用前要知道）

為了在 KANO 上開得起來，這個映像放寬了幾項 Android 的資料保護，每一項都在每次 build 中固定包含：

- **`/data` 沒有加密**：拿掉了 FBE 與 metadata encryption（沒有 pKVM／Trusty，金鑰沒有安全的存放處）。Google 帳號的權杖與
  使用者資料以明文存在碟上，**筆電遺失就等於資料外洩**。
- **`ro.debuggable`**：builder 會在 vendor 的 build.prop 加上 `ro.debuggable=1`（原本給序列埠 shell 用），但這一行**沒有生效**：
  2026-10-13 在 KANO 實機（開發者自己的 build）讀到 `ro.debuggable=0`、`ro.force.debuggable=0`、`ro.build.type=user`。
  原因推測是 vendor 的 build.prop 不能設定 system 擁有的屬性（`/vendor/build.prop` 從 adb shell 讀不到，沒有直接確認）。
  所以 app 不能被任意除錯，adbd 也是一般 user build 的權限，`adb root` 不能用。哪天這一行生效了，所有 app 都會變成可除錯，要重新評估。
- **以 root 執行的 permissive 服務**：`kano_bootfix`（設定 `persist.vold.dont_commit_checkpoint=1`）、`kano_kmsg_logcat`
  （只在 serial-debug 選項把 logcat 鏡像到序列埠）、`kano_diag`（定期把負載與 dmesg／logcat 片段寫到 `/data/kano-diag`）、
  `kano_wifi`（只有 `/data/kano-wifi.conf` 存在時才動作，該檔只由開發用維護系統建立，內容是明文 Wi-Fi 密碼）。
- **網路 adb 預設不包含**：只有 build 時給 `GBOS_ADB_PUBKEY` 才會開 TCP 5555，而且只接受那一把公鑰（`ro.adb.secure` 維持 1）。
  builder 會拒絕不像 adb 公鑰的檔案，例如誤傳的私鑰。2026-10-13 在 KANO 實機上的開發者 build（開了 adb）確認過：
  `ro.adb.secure=1`、`service.adb.listen_addrs=tcp:5555`、`/adb_keys` 指向 `/product/etc/security/adb_keys`；
  一把新產生、沒登記的金鑰經 TCP 5555 連線被拒（`unauthorized`，shell 不能用，螢幕上也沒有跳出授權對話框），
  登記的那把金鑰拿到的是 `uid=2000(shell)`。

`kano_autooff` 不在每次 build 裡。`GBOS_TEST_AUTOOFF` 是開發者開關。斷電還要內建碟的 `/data/kano-autooff.nonce`，而那份檔只會被寫到已核對身份的 USB userdata。會把那份 userdata 複製進內建碟的路只有 harness 的 `kano-nvme-install.sh`。斷電只能靠 `/data` nonce。安全保證是卸載前清掉複製過來的 `kano-autooff.nonce`（一般檔、符號連結或目錄；目錄由 python3 的 `shutil.rmtree` 刪除，檔名不經過文字紀錄），再唯讀重新掛上複查必須是 ENOENT，然後再卸載。掛載失敗、根目錄列不出來、查找錯誤（errno 不是 ENOENT）、刪不掉、重新掛載、複查或卸載失敗，就非 0 停止，不再 `resize.f2fs`、`fsck.f2fs`，也不再寫 ESP。只有 ENOENT 算這個名字不存在。任何 lstat 發生 ENOENT 以外的查找錯誤就停止；刪除後及重新掛載後的確認，必須得到 ENOENT，成功查到任何物件也停止。`dd` 完成之後、清除成功之前的任何停止（行程被殺掉、斷電、ssh 中斷）都可能留下檔案。vendor 掃描是縱深防禦：解析得到 `etc/kano-autooff.nonce` 或 `etc/init/kano-autooff.rc` 就一個區塊都不寫。`blkdiscard` 前先確認 `sda1` 的 PARTLABEL 是 `userdata`，再以 `-o ro` 掛上（f2fs 仍可能把先前的 fsync 補完）；已有這個 nonce，或列出根目錄／`lstat` 失敗（不是 ENOENT），也拒絕。`kano-nvme-update.sh` 也在推送前拒絕 TEST，但它只寫 super／vbmeta，不清 userdata。直接呼叫的 `kano-delta-push-nvme.py` 不寫 userdata。安裝碟的更新（`gbos-update.py apply`）用 `guard_ranges` 把 userdata 排除，`rollback` 只改 ESP 選單；全新安裝的 userdata 是新建的空檔案系統。這些路上的 TEST vendor 沒有 `/data` nonce 就保持開機。`install.sh` 與 `docker-install.sh` 不寫內建碟；它們在建置前拒絕這個開關。`install/update.sh` 呼叫 `install.sh`，帶 `--disk` 時寫的是 USB（`write-usb.sh --keep-data`）。日常系統的 vendor 沒有這個服務。

TEST 映像只在 `/vendor/etc/kano-autooff.nonce` 與 `/data/kano-autooff.nonce` 逐 byte 相同時才斷電。nonce 只寫到已核對身份的 USB 隨身碟 userdata（`/dev/disk/by-id/usb-...`，`install/write-usb.sh` 的目標），必須是一般檔，內容是該次 build 的 `<out>/image/autooff.nonce`。用完必須刪掉。不要寫到內建 NVMe 的 `/data`。留下檔案再把 TEST 映像裝進內建碟，之後每次開機都會斷電，而 super／vbmeta 更新不會清掉那個檔。合約見 HOW-IT-WORKS.md。安裝碟 `install/stick/gbos-install.sh` 不複製隨身碟的 userdata：全新安裝的內建碟 `/data` 是新建的，開頭 16 MiB 為零；更新模式把 super 交給 `gbos-update.py`，那支程式不寫 userdata。這兩條都不檢查 vendor，也建不出 nonce，所以單靠它們不會把日常系統武裝成斷電。QEMU 沒有 `/data` nonce，自我測試保持開機。

## 實際攔下過的寫入嘗試

- 設定精靈的 `ActivationDateWorker` 會讀寫 RW VPD（AP flash 上的啟用日期），經由 `vpd-executor` 執行 flashrom。
  因為 flashrom 已被移除、也沒有 `/dev/mem`，紀錄是 `Failed to execute flashrom`，什麼都沒寫。
- `adbd-killer` 用 `futility gbb --get --flash` 讀 GBB 旗標；futility 已移除。

## 不能做的事

- 不要從 Googlebook 或任何非官方工具更新 KANO 的韌體。量產機要更新 UEFI 韌體時，只用 MrChromebox 的官方腳本、照官方文件做
  （見 FIRMWARE.md；這個專案沒有測試過韌體更新）。**工程樣機**（CPU stepping 小於 3 或 HWID 含 TEST）連 MrChromebox 的腳本
  都不要跑：公開韌體沒有它的 microcode，開發這個專案的那台的韌體帶有手動補的 K0 microcode。
- 不要放行 TPM、EC chardev、SPI 驅動，即使某個功能看起來需要。`GBOS_EC` 只放行上面稽核過的驅動，要多放行任何 EC 模組都必須先重新稽核。
- 不要在 systemd-boot 選單按 `d`、`D`、`+`、`-`、`t`、`T`、`f`、`F2`、`F10`、`Del`、`Esc`；用方向鍵和 Enter 選項目就好。
- **不要在沒備份 48 位數 BitLocker 復原金鑰前就進 BIOS 改 Secure Boot**：在裝有 Windows 的 PC（`generic-x86`、`amd-hx370` 等）上，切換 Secure Boot 可能會改變開機量測與 BitLocker 所綁定的 TPM PCR（例如 PCR 7，或依平台設定綁定的 PCR 0、2、4、11），下次開回 Windows 時**可能會跳出 BitLocker 復原畫面要求輸入 48 位數復原金鑰**。進 BIOS 之前，必須先在管理員 PowerShell 執行 `manage-bde -status` 檢查所有磁碟區，並將**每個加密磁碟區的 48 位數 BitLocker 復原金鑰（Recovery Key）備份並存到另一台裝置**（<https://aka.ms/myrecoverykey> 或對每個加密磁碟區執行 `manage-bde -protectors -get <磁碟區>`，例如 `C:`、`D:`）。`Suspend-BitLocker -MountPoint "C:" -RebootCount 1` 只是備份好復原金鑰之後的選用便利指令（暫停期間會把明文金鑰留在磁碟上，有保護空窗，且重開機次數用完後仍會檢查 TPM PCR；回到 Windows 後請用 `manage-bde -status`／`Resume-BitLocker` 確認保護已恢復），**絕對不能代替備份 48 位數復原金鑰**。
