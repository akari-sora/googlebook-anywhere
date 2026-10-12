# 開發紀錄（2026-10-10 ～ 2026-10-12）

這份整理開發過程中確認過的事實、踩過的坑與決策，給之後接手的人（或代理）用。程式碼與 git 歷史記得的東西不重複寫；
這裡記的是「為什麼」和「哪些路走不通」。研究報告原文在 `research/`（private repo）。

## 機器與韌體背景

- 機器：Acer Chromebook Spin 714（CP714-1WN-70X5，brya／kano），2021/12 出廠的**工程樣機**，HWID `KANO TEST 2090`，
  CPU 是量產前的 Alder Lake-P **K0 stepping**（CPUID `906a2`）。
- 原本的問題：刷了 2023 年的量產韌體之後卡在記憶體初始化（port80 `dd28`／`dd2b`）。根因是公開的 intel-microcode 只有
  `06-9a-03/04`，沒有 K0 的 microcode。把 K0 microcode（`cpu906A2_plat80_ver00000315`）放進 RO FIT 之後才過得了 MRC。
- 官方簽章韌體在 K0 上跑不起來（RW 的 microcode 沒有 `0x906a2`，在 FSP-S `9a63` machine check），所以現在用的是
  **MrChromebox UEFI + 手動補的 K0 microcode**。**永遠不要在這台跑韌體更新**，包括 MrChromebox 的更新腳本，會把 microcode 蓋掉。
- 這之後先裝了 ChromeOS Flex（能用），使用者最後選擇用 Googlebook 取代 Flex。

## Googlebook 怎麼開起來的（依時間）

| 問題 | 根因 | 解法 |
|---|---|---|
| 原映像不安全 | `gscutil.rc` 開機時跑 GSC 韌體更新；fwupd 開機自動啟動（含 brya Elan 觸控板 cab，`ApprovalRequired=false`）；vendor 帶 ap／ec 韌體；開機參數 `spi-intel.writeable=1` | 移除更新程式、封鎖 EC／TPM／SPI 模組、`efi=noruntime`（見 SAFETY） |
| KeyMint 需要 pKVM protected VM | KANO 沒有 pVM | 換成 Cuttlefish 公開建置的 `keymint.rust_nonsecure`、`gatekeeper.nonsecure` APEX |
| Android 畫面全黑 | xe 6.18.21 把 DPT 放在 stolen memory，掃描 tiled framebuffer 時 plane fault（CVE-2026-80915）；PSR 開著時被面板自我更新遮住 | `xe.enable_dpt=0`（另外試過 client HWC backend，無效，因為 client target 也是 Y-tiled） |
| 首次開機無限重啟 | cppreopts 在慢速隨身碟上複製 odex 540 秒，超過 system_server watchdog | `ro.cp_system_other_odex=0` |
| system_server 被 watchdog 殺 | Kingston DataTraveler 3.0 的 ext4 4K fsync 中位數 2.6 秒、最長 10 秒；f2fs 中位數 4 ms | `/data` 改 f2fs，拿掉 `checkpoint=block`（dm-bow 會在每次斷電後回滾 /data，要 280 秒） |
| 「Checkpoint commit failed」重開 | vold 認為在 checkpoint 中；markBootSuccessful 會呼叫 `crossystem`（韌體 NVRAM，已封鎖） | `persist.vold.dont_commit_checkpoint=1`，由 vendor 的 `kano_bootfix` 每次開機設定（只改 GPT 屬性沒有用） |
| LockSettings 卡住、watchdog | IWeaver／ISecretkeeper 在 VINTF 宣告了，但它們的 HAL 需要 pVM | 刪掉這兩個 VINTF 片段 |
| init 不斷重跑 flags_health_check | timberslide（需要 EC）崩潰循環觸發 `sys.init.updatable_crashing` | vendor action `stop timberslide`、gscd、udsattestation |
| 觸控板、觸控螢幕、音效沒有 | `i2c_hid_acpi` 被封鎖；放行 intel-lpss 又會把序列埠重設 | `pci-stub.ids=8086:51a8,8086:51a9,8086:51aa,8086:51ab` 搶走 LPSS UART 與指紋 GSPI，pci-stub 放在 initrd 第一個載入 |
| 音效 HAL 找不到卡 | DRAS 依 `hal_configs/audio_default.xml` 找 lapis 的 `sof-soundwire` | 新增 `sof-nau8825_kano` 設定（從 ChromeOS kano UCM 人工翻譯），喇叭曲線降 4.75 dB（DSM 保護參數不公開） |
| 電源鍵沒反應 | lapis 的 `excluded-input-devices.xml` 排除 ACPI「Power Button」（它的電源鍵走 cros_ec_buttons，這裡被封鎖） | 不再排除 ACPI Power Button（safe25） |
| 一般選項（plain）面板不亮 | 未解（final14 時觀察到，之後未重測）；Android 有開起來（有寫檔），面板停在暗畫面 | 預設改用 quiet／serial-debug（都帶 pci-stub） |

鍵盤背光：ACPI `GOOG0002` 驅動與 EC console 的 `kblight` 都只改得了 EC 記錄的百分比，攝影機拍到亮度完全不變，
判定這台的 EC 韌體本身不支援調整（要改 EC 韌體，禁止）。

## 內建 NVMe（2026-10-12）

- 使用者明確選擇「用 Googlebook 取代 Flex」並確認清碟。Flex 的 GPT 與第一個 MiB 先備份在開發機。
- 從隨身碟複製 Googlebook 區段（含 /data），`cmp` 驗證；userdata 移到碟尾、`resize.f2fs`（先 rw 掛載一次重播 log）、`sgdisk -G`。
- 結果：`boot_completed` 60 秒（隨身碟 100～130 秒），ANR 0，藍牙 3 秒開啟、0 次崩潰。
- 韌體 Boot Menu 還留著一個過期的「ChromeOS Flex」項目（不影響；刪它要寫 EFI 變數，不做）。

## 遠端實機 harness 的教訓

- **不要讓使用者插拔**：隨身碟一直插在 KANO，選單選擇、斷電、重開都透過 Cr50 的 EC console（`kbpress`、`apshutdown`、`powerbtn`）。
- edk2 設定畫面只畫在面板上（序列埠看不到），用攝影機讀；序列埠的方向鍵與 Enter 有效，單獨的 ESC 無效，要用 EC `kbpress 1 1`。
- `kano-gbos-watch.py` 檢查的是 Android 開機參數，**不能在維護 Linux 上跑**（曾經因此把正在測試的維護系統斷電）。
- 不要改寫正在執行的 bash 腳本（bash 逐段讀檔，會讀到錯位的內容）；改複本。
- f2fs 在隨身碟上 umount／checkpoint 時可能讓新的 ssh 連線卡十幾分鐘：先在維護系統上打包、核對 SHA-256，確認後才刪。
- 經 ssh 傳大檔到維護系統可能被悄悄截斷：在 KANO 端產生檔案，或傳完一定核對 SHA-256。
- Wi-Fi 在 `kano_adb` 結束後約 80 秒才連上，adb 探測要重試幾分鐘；要斷電前先 `adb reboot -p` 正常關機，否則最後寫的診斷檔會是全 0。
- `svc power stayon` 沒有用（沒有 AC 偵測）。測試時改過的裝置設定（螢幕逾時等）要改回使用者原本的值。
- `adb root` 在這個 user build 被編譯拿掉；網路 adb 必須覆寫 system 的 `service.adb.listen_addrs`（原本固定 localhost）。

## 建置的可重現性

- `base/build-base.sh` 用原金鑰重建時與手工映像逐位元組相同（子代理做完後，我另外重跑並用 `cmp` 確認）。
- `builder/` 從 `build-safe25.sh` 與 `make-kano-final26.py` 參數化而來。同樣輸入時與 final26 逐檔相同，只有 ESP 的 FAT 時間戳不同。
- `rebuild.py` 原本靠 `dump.erofs` 列目錄，現在改成純 Python 讀 EROFS；在 base vendor、system_ext、final26 vendor 上與舊版逐項相同。
- 韌體從 linux-firmware `20260810` 與 sof-bin `v2025.12.2` 取得，與 Arch 套件裡的檔案逐位元組相同。

## 發版前的驗證（2026-10-12 晚上）

- 兩輪雙代理審查（GPT-6.1-Sol、Opus 5.5）找出的問題：write-usb 在確認與寫入之間沒有重新核對碟的身分、安裝程式在清碟後
  才核對來源、install.sh 會沿用不同設定的舊映像、base 會刪不是自己建的 `work/`、systemd-boot 沒有固定版本等，都已修正並補測試。
- 新使用者的預設映像在 KANO 實機從零開到歡迎畫面（見 STATUS）。
- 背景工作要用 `setsid nohup` 和 PID 檔脫離 ssh：一次使用者中斷把 ssh 底下的準備腳本和 AP 序列埠記錄程式一起帶走了。
- 不要用 scp 覆寫正在執行的腳本（同一個 inode，bash 會從舊位移讀新內容）；這次差一點在準備結束後按到 KANO 的電源鍵。
- 隨身碟寫入非常慢（壓縮檔寫回隨身碟每分鐘只有幾十 MB），備份一律串流到開發機。

## 決策與原則

- 公開 repo 之後另開、乾淨歷史；只放腳本、補丁與文件，不放 Google 映像或任何二進位；使用者自己從 Google 下載。
- 不寫 AP SPI flash、EC、GSC／Cr50、觸控／PD 韌體、EFI 變數；不 `ccd reset factory`；TPM、EC chardev、SPI 維持封鎖。
- 每次推送前都要有 harness 測試，加上兩個獨立代理（GPT-6.1-Sol、Opus 5.5）審查。

## 未完成

- 觸控螢幕點擊、耳機孔、USB-C 資料與 DisplayPort：需要有人在實機前操作。
- 「充電中」顯示、自動旋轉、光感測：都在 EC 後面，維持封鎖。
- 內建相機：Googlebook 的相機 HAL 是給 IPU7 的，要移植 IPU6 HAL。
- plain 開機選項的暗畫面。
- 只支援 KANO；其他型號見 NEW-DEVICE.md。
