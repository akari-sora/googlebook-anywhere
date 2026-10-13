# Googlebook Anywhere

把 Google 官方的 **Googlebook OS**（lapis，Android 17 桌面版）裝到原本不支援它的 x86-64 電腦上。第一台、也是目前
唯一在實機上驗證過的是 **Acer Chromebook Spin 714（CP714-1WN，代號 KANO）**。

> English summary: scripts that turn Google's official Googlebook "lapis" recovery image (downloaded from Google by the
> script itself) into a bootable USB image for x86-64 UEFI machines — so far the Acer Chromebook Spin 714 (KANO) —
> with the known firmware updaters removed and the EC/TPM/SPI kernel paths blocked. No Google binaries, firmware files
> or third-party configuration files are stored in this repository (only short functional excerpts that the build
> scripts edit); every input is fetched from its original source and checked against a pinned SHA-256.

## 畫面

| Acer Chromebook Spin 714（KANO）實機 | 同一台，設定精靈 | 一般 x86 電腦（`generic-x86`，QEMU） |
|---|---|---|
| ![KANO 實機上的 Googlebook 歡迎畫面](docs/images/kano-welcome.png) | ![KANO 實機上的 Googlebook 設定精靈](docs/images/kano-setup.png) | ![generic-x86 映像在 QEMU 的歡迎畫面](docs/images/generic-x86-qemu.png) |

前兩張是 KANO 實機的螢幕截圖（全新映像開到設定精靈，2026-10-12）。第三張是通用版映像在 QEMU 裡的畫面：
從韌體一路開到設定精靈，畫面由韌體的 framebuffer 加 CPU 繪圖輸出（2026-10-13，新使用者流程 `install.sh` 建出的映像）。

## 現況

在 KANO 上可以日常使用：桌面、Wi-Fi、藍牙、觸控板、鍵盤、喇叭、麥克風、電源鍵、上蓋休眠／喚醒都在實機上驗證過；
觸控螢幕與觸控筆偵測得到，但還沒做實際點擊測試。每一項的驗證方式見 [docs/STATUS-KANO.md](docs/STATUS-KANO.md)。

做不到的：自動旋轉與光感測、鍵盤背光調整（都要經過 EC，刻意封鎖或硬體本身不支援）、內建相機（開發中，預設關）。
「充電中」圖示：v0.2 起映像帶 ACPI 充電器驅動（`ac.ko`，只讀 ACPI，不送 EC 指令），KANO 實機上接充電器時 Android 顯示充電中（2026-10-13）。
待機：v0.1 闔上蓋子或關螢幕後幾十秒內會自己亮起（每次暗中喚醒都帶一個電源鍵的 `KEY_WAKEUP`）；v0.2 起映像帶
`gbos_wakefilter.ko`，電源鍵沒有喚醒系統時就丟掉它。KANO 實機上闔蓋 300 秒期間暗中喚醒 10 次，螢幕都沒亮；螢幕關閉時按電源鍵會亮、開蓋會亮螢幕。
**已知問題**：螢幕關著時按鍵盤可能叫不醒螢幕（喚醒那一下按鍵送到 Android 之前，模組已經把 `KEY_WAKEUP` 丟掉了），請用電源鍵或開合上蓋；下一版修正。

**實機驗證集中在 KANO**（驗證機是一台 Alder Lake-P 工程樣機；量產型號的支援範圍與未測項目見 docs/STATUS-KANO.md、docs/FIRMWARE.md）。v0.2 起另有實驗性的 `profiles/generic-x86`，給 GPU 在 Googlebook 核心裡沒有驅動的
x86-64 UEFI 電腦（AMD、較舊的 Intel）：用韌體的畫面輸出（GOP framebuffer）加 CPU 繪圖（SwiftShader），能開機但沒有
GPU 加速，只在 QEMU 驗證過，見下面「其他 x86-64 電腦（實驗性）」。新機型的適配見 [docs/NEW-DEVICE.md](docs/NEW-DEVICE.md)。

## 三步驟

### 1. 換成 UEFI 韌體（一次）

照 [docs/FIRMWARE.md](docs/FIRMWARE.md)：進入開發者模式後，先跑唯讀的 `install/kano-check.sh` 確認沒有已知的阻礙
（`GO` 不代表保證刷得成功），再用 MrChromebox 的官方腳本換成 UEFI Full ROM（需要 SuzyQ 線解除寫入保護）。
這一步有變磚風險，這個專案不代為刷寫，一切以官方文件為準。

### 2. 建置映像（在你的電腦上，約 10～30 分鐘，不含 7.7 GB 下載）

Google 的映像不能轉散布，所以每個人都要從官方 zip 自己建一次。腳本會自動下載、核對、建置。需要約 80 GB 磁碟空間。

| 你的電腦 | 指令 |
|---|---|
| **Linux**（x86-64） | `install/install.sh --work ~/gbos-work` |
| **Windows**（WSL2，建議） | 在 WSL 的 Ubuntu 裡照 Linux 做（缺少的套件與安裝指令會印在畫面上）。沒有 KVM 時加 `--no-selftest`（映像不會經過 QEMU 自我測試）。要用 Docker Desktop 的話，也是在 WSL 裡執行 `install/docker-install.sh`（打開 Docker Desktop 的 WSL 整合）；這個腳本需要 bash，不要在 PowerShell 或 cmd 裡跑 |
| **macOS**（Docker Desktop） | `install/docker-install.sh --work ~/gbos-work`（Docker 的磁碟上限要 80 GB 以上；Apple 晶片用 x86 模擬，比較慢） |

`docker-install.sh` 的路徑（repo、`--work`、`--zip` 等）不能含 `:`；Linux 主機有 KVM 時，容器會帶上 `/dev/kvm` 的群組，照樣做 QEMU 自我測試。

`install.sh` 不需要 root，依序：

1. 從 Google 下載官方 recovery zip，核對 SHA-256。只接受已驗證的 lapis build `16452207`（`CL3B.260622.271.R1`）。
2. `base/build-base.sh` 重建基礎映像（中間產物，不能直接開機），換上軟體 KeyMint／Gatekeeper，並用**你自己新產生的 AVB 金鑰**
   （`~/gbos-work/base/keys/vbmeta.pem`）重簽。otatools 與兩個 APEX 從 ci.android.com 的公開建置取得。
3. `builder/fetch-inputs.sh` 從 linux-firmware、sof-bin 與 ChromiumOS board-overlays 取得 KANO 的韌體與音效設定。
   KANO 的 ACPI 充電器驅動 `ac.ko` 由 `builder/kmod/build-ac.sh` 從固定版本的核心原始碼與工具鏈建置（另外下載約 2.5 GB，建完會刪掉約 9 GB 的暫存），
   同一棵核心樹接著建 `gbos_wakefilter.ko`（`builder/kmod/build-gbos_wakefilter.sh`）。
4. `builder/build.sh` 產生 KANO 映像，接著在 QEMU 裡開機自我測試（Android 開機檢查、從映像自己的 ESP 開機的整條開機鏈）。
   `docker-install.sh` 在沒有 KVM 的主機上（例如 macOS）、或加了 `--no-selftest` 時會略過這一步，並在映像旁留下 `.not-self-tested`
   標記。**這樣的映像沒有經過 QEMU 開機驗證**，只做了建置時的檢查。

在一台 12 執行緒、NVMe 的 Linux 主機上約 10 分鐘（base 2.5 分鐘、映像與兩個 QEMU 測試 7.5 分鐘）。

### 3. 寫入隨身碟並開機

成品是 `~/gbos-work/build/image/googlebook-kano.img`，旁邊的 `.sha256` 是它的核對碼。**寫入會清掉整支隨身碟。**

- **Linux**：`sudo install/write-usb.sh ~/gbos-work/build/image/googlebook-kano.img /dev/disk/by-id/usb-<你的隨身碟>`
  （只接受 USB 整顆碟，拒絕系統碟與使用中的碟，要你輸入碟的序號確認，寫完會整段回讀比對，再把備份 GPT 移到隨身碟尾端並核對）。
  需要先裝 `sgdisk`（`gdisk` 套件），沒有的話什麼都不寫就停止。映像旁有 `.not-self-tested` 時，還要多輸入 `untested` 才會寫。
- **macOS、Windows**：用 [balenaEtcher](https://etcher.balena.io/)（Mac／Windows／Linux）或 [Rufus](https://rufus.ie/)
  （Windows，選「DD 映像」模式）。WSL 建出來的檔案在 Windows 的 `\\wsl$\Ubuntu\home\<你>\gbos-work\build\image\`。
  寫入前先核對：Windows `certutil -hashfile googlebook-kano.img SHA256`，macOS `shasum -a 256 googlebook-kano.img`，
  結果要和 `.sha256` 檔的內容相同。

插到 KANO 開機，預設選項是 **Googlebook KANO quiet**。慢速隨身碟首次開機要幾分鐘，也可能跳出「沒有回應」的對話框（選「等待」）。
**這一版一般使用者只能從隨身碟執行。** 裝到內建 NVMe 目前只有開發者流程：安裝程式（`install/stick/`）需要隨身碟上另外裝的
維護系統，還沒有腳本自動建立；見 [docs/INSTALL-INTERNAL.md](docs/INSTALL-INTERNAL.md)。自動做出安裝碟排在下一版。

之後的新版本用 `install/update.sh --work ~/gbos-work --disk /dev/disk/by-id/usb-<你的隨身碟>` 更新，`/data` 保留，舊版本留在開機選單上（「(previous version)」）。**保留 `~/gbos-work/keys/`**：更新必須用同一把 AVB 金鑰。見 [docs/UPGRADE.md](docs/UPGRADE.md)。

使用前請讀 [docs/SAFETY.md](docs/SAFETY.md)，特別是「資料安全取捨」：`/data` 沒有加密。

## 其他 x86-64 電腦（實驗性，v0.2）

給 Googlebook 核心沒有 GPU 驅動的 x86-64 UEFI 電腦（AMD、較舊的 Intel），以及想先試試看的人。**只在 QEMU 驗證過，
還沒在任何實體 PC 上開過**；畫面靠韌體的 framebuffer、繪圖靠 CPU，會慢，解析度由韌體決定。

1. **先看相容性（唯讀）**：在目標電腦上開任何 Linux 隨身碟（例如 Ubuntu 的 Live USB），執行
   `install/gbos-probe.sh --out gbos-probe`。它只讀 `/sys` 與 `/proc`，不載入模組、不碰磁碟、不讀序號，結果在
   `gbos-probe/report.txt`：哪些裝置 Googlebook 有驅動（Wi-Fi、音效、觸控板、儲存裝置…），哪些沒有。
2. **建置**（Linux 或 WSL2）：`GBOS_PROFILE=profiles/generic-x86 install/install.sh --work ~/gbos-generic`。
   會多下載 Cuttlefish 公開建置裡的 SwiftShader（釘選 SHA-256），並從釘選的核心樹建 `gbos_efifb.ko`。
   成品是 `~/gbos-generic/build/image/googlebook-generic-x86.img`。`docker-install.sh` 目前只建 KANO。
3. **寫入與開機**：和上面第 3 步一樣寫入隨身碟。目標電腦要能從 USB 開 UEFI 開機、**關閉 Secure Boot**（映像沒有 Secure Boot 簽章）。
   預設選項是 **Googlebook generic x86 quiet**。

韌體、TPM、EC 的路徑和 KANO 一樣封鎖（同一份 `_safety.toml`），內建 NVMe 在開機時就被藏起來（`initcall_blacklist=nvme_init`）。
**內建碟是 eMMC 的電腦目前沒有藏起來**，Android 可能把它顯示成外接儲存，不要對它做格式化或「用作內部儲存空間」。
Wi-Fi、音效這類需要機型韌體的功能，generic 映像
都沒有帶，多半不能用；能用的範圍以 probe 的報告為準。要做成正式支援的機型，見 [docs/NEW-DEVICE.md](docs/NEW-DEVICE.md)。

## 結構

| 路徑 | 內容 |
|---|---|
| `install/` | `install.sh`（一鍵流程）、`update.sh` 與 `gbos-update.py`（保留資料的更新，見 docs/UPGRADE.md）、`docker-install.sh`（Docker 版）、`write-usb.sh`（有防呆的寫入；`--keep-data` 更新）、`kano-check.sh`（換韌體前的唯讀檢查）、`gbos-probe.sh`（新機型的唯讀硬體探測）、`stick/`（安裝碟：在 KANO 上裝到內建碟） |
| `base/` | 官方 recovery → 基礎映像（中間產物：軟體 KeyMint、你的 AVB 金鑰；只給 builder 用），附驗證紀錄 |
| `builder/` | `build.sh`：依機型設定（預設 KANO）建映像（vendor 重建、vbmeta 重簽、ESP、QEMU 自我測試）；`fetch-inputs.sh`；`lib/devprofile.py` 讀取並檢查機型設定 |
| `profiles/` | 機型設定：`kano/`（`profile.toml` 的參數、韌體與封鎖清單，`vendor.py` 的 vendor 修改，音效來源清單）、`generic-x86/`（沒有 GPU 驅動的機器：GOP framebuffer + SwiftShader，實驗性）與全專案的安全規則 `_safety.toml`；新機型見 [docs/NEW-DEVICE.md](docs/NEW-DEVICE.md) |
| `tests/` | 快速防呆測試、完整流程、可重現性、安裝碟 QEMU 測試、harness 測試（見下面「測試」） |
| `tools/harness/` | 開發用的遠端實機測試工具（EC 電源控制、序列埠、增量推送、NVMe 安裝）；個人設定放在 `kano-env.local.sh` |
| `tools/release/` | 安裝包與公開 repo 的匯出腳本 |
| `docs/` | 韌體、狀態、安全模型、技術說明、內建碟安裝、新機型適配、開發紀錄 |

發版的安裝包（`googlebook-anywhere-<版本>.tar.gz`）只有 `README.md`、`LICENSE`、`base/`、`builder/`、`install/`、`profiles/`、`docs/`
與 `tests/` 的 `builder`、`install`、`probe`、`stick`、`update`，另外附 `tests/builder` 會用到的 `tools/release/png-ok.py` 與
`tools/release/published-images.sha256`；其餘的 `tools/` 與 `tests/harness/` 只在 git repo 裡。

## 開發者選項

`builder/build.sh` 也可以直接用環境變數呼叫，變數說明在檔案開頭。和預設值不同的開發用設定：

- `GBOS_ADB_PUBKEY=~/.android/adbkey.pub`（或 `install.sh --adb-pubkey`）：只接受這把金鑰的 TCP 5555 adb。KANO 實機上確認過
  沒登記的金鑰會被拒（`unauthorized`），見 SAFETY.md「資料安全取捨」。
- `GBOS_DEFAULT_ENTRY=lapis-debug.conf`：預設改成 serial-debug（序列埠 console + logcat 鏡像）。
- `GBOS_LOADER_EDITOR=yes`：開機選單可以編輯核心參數（harness 的 `kano-boot.sh` 附加參數時需要）。
- `GBOS_EC=sensors,typec,buttons`（可只選幾項）：放行一組經過稽核的 EC 驅動，換來自動旋轉、USB-C 的 USB3／DP、側邊音量鍵。安全保證會跟著改變，而且要先確認休眠能進入 S0ix，見 [docs/SAFETY.md](docs/SAFETY.md)「EC 功能變體」。尚未在實機驗證。

`install.sh` 會記下這些設定與 `base/`、`builder/`、機型設定的內容摘要（包括還沒 commit 的檔案）；設定或原始碼一變，就會停下來而不是沿用舊的 base 或映像。舊版 `install.sh` 建的工作目錄要先刪掉一次 `base/` 與 `build/`。

## 測試

每次 push 由 GitLab CI 跑 shellcheck／py_compile 與前九項；後面四項需要大磁碟、KVM 或實機的輸入，在開發機上跑。
CI 的容器沒有 loop 裝置，所以 `tests/install/run.sh` 裡用 loop 假碟的案例在 CI 會 SKIP，只跑寫入程式的拒絕測試
與部分防呆；完整案例要在 Linux 開發機上跑。

```sh
tests/builder/run.sh              # 幾秒：語法、輸入防呆、截斷映像、adb 公鑰格式
tests/install/run.sh              # write-usb.sh 防呆（--check-only 加上假的 lsblk，不寫入任何碟；Linux）
tests/install/install-sh-test.sh  # install.sh 的參數與重用防呆
tests/install/kano-check-test.sh  # 換韌體前的檢查，對假的機器資料
tests/stick/selftest.py <空目錄>   # 安裝碟安裝程式的核對邏輯（不需要 QEMU、root 或磁碟）
tests/update/selftest.py <空目錄>  # 保留資料的更新（gbos-update.py），小型合成碟（不需要 QEMU、root 或磁碟）
tests/update/mutate.py            # gbos-update.py 列出的 25 項防護逐一拿掉，selftest 都必須有檢查失敗
tests/probe/probe-test.py        # install/gbos-probe.sh（新機型的唯讀硬體探測）對假的 /sys、/proc
tests/harness/run.sh              # harness 防呆（假的 ssh／序列埠，不碰實機）與 mutation 檢查
tests/builder/e2e.sh <空目錄> [--zip 已下載的 zip]   # 新使用者完整流程 + QEMU 自我測試
tests/update/qemu-update.py <空目錄> <舊映像> <新映像>  # 更新的完整流程：舊版開機、更新、新 slot 與 previous version 開機（QEMU）
tests/builder/repro.sh <參考映像> <空目錄> [允許的 ESP 差異] [之後刪掉的 ESP 檔案]   # 用開發設定重建，與實機驗證過的映像逐檔比對
tests/stick/qemu-install.py ...   # 安裝碟（需要真隨身碟的維護分割區 dump，參數見檔案開頭）
```

## 授權

本 repo 的腳本與文件以 GPL-3.0-or-later 授權（Copyright 2026 ImL1s，全文見 `LICENSE`）。Googlebook 映像、裡面的 HAL／APEX、
韌體與 ChromeOS 音效設定屬於各自的權利人，本 repo 不包含、也不散布；建置時由腳本從原始來源取得，適用原始授權。
