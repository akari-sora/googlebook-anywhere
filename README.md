# Googlebook Anywhere

把 Google 官方的 **Googlebook OS**（lapis，Android 17 桌面版）裝到原本不支援它的 x86-64 電腦上。第一台、也是目前
唯一在實機上驗證過的是 **Acer Chromebook Spin 714（CP714-1WN，代號 KANO）**。

> English summary: scripts that turn Google's official Googlebook "lapis" recovery image (downloaded from Google by the
> script itself) into a bootable USB image for x86-64 UEFI machines — so far the Acer Chromebook Spin 714 (KANO) —
> with the known firmware updaters removed and the EC/TPM/SPI kernel paths blocked. No Google binaries, firmware files
> or third-party configuration files are stored in this repository (only short functional excerpts that the build
> scripts edit); every input is fetched from its original source and checked against a pinned SHA-256.

## 現況

在 KANO 上可以日常使用：桌面、Wi-Fi、藍牙、觸控板、鍵盤、喇叭、麥克風、電源鍵、上蓋休眠／喚醒都在實機上驗證過；
觸控螢幕與觸控筆偵測得到，但還沒做實際點擊測試。每一項的驗證方式見 [docs/STATUS-KANO.md](docs/STATUS-KANO.md)。

做不到的：自動旋轉與光感測、「充電中」圖示、鍵盤背光調整（都要經過 EC，刻意封鎖或硬體本身不支援）、內建相機（需要 IPU6 HAL）。

**目前只支援 KANO（量產版）。** 其他 x86-64 UEFI 機器（換過韌體的 Chromebook、一般 Intel 筆電）走同一條路，但每個型號
要另外適配韌體、音效與開機參數，見 [docs/NEW-DEVICE.md](docs/NEW-DEVICE.md)。

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
要裝到內建 NVMe 的話，先看 [docs/INSTALL-INTERNAL.md](docs/INSTALL-INTERNAL.md)。

使用前請讀 [docs/SAFETY.md](docs/SAFETY.md)，特別是「資料安全取捨」：`/data` 沒有加密。

## 結構

| 路徑 | 內容 |
|---|---|
| `install/` | `install.sh`（一鍵流程）、`docker-install.sh`（Docker 版）、`write-usb.sh`（有防呆的寫入）、`kano-check.sh`（換韌體前的唯讀檢查）、`stick/`（安裝碟：在 KANO 上裝到內建碟） |
| `base/` | 官方 recovery → 基礎映像（中間產物：軟體 KeyMint、你的 AVB 金鑰；只給 builder 用），附驗證紀錄 |
| `builder/` | `build.sh`：KANO 映像（vendor 重建、vbmeta 重簽、ESP、QEMU 自我測試）；`fetch-inputs.sh` |
| `profiles/kano/` | KANO 機型設定（音效來源清單） |
| `tests/` | 快速防呆測試、完整流程、可重現性、安裝碟 QEMU 測試、harness 測試（見下面「測試」） |
| `tools/harness/` | 開發用的遠端實機測試工具（EC 電源控制、序列埠、增量推送、NVMe 安裝）；個人設定放在 `kano-env.local.sh` |
| `tools/release/` | 安裝包與公開 repo 的匯出腳本 |
| `docs/` | 韌體、狀態、安全模型、技術說明、內建碟安裝、新機型適配、開發紀錄 |

發版的安裝包（`googlebook-anywhere-<版本>.tar.gz`）只有 `README.md`、`LICENSE`、`base/`、`builder/`、`install/`、`profiles/`、`docs/`
與 `tests/` 的 `builder`、`install`、`stick`；`tools/` 與 `tests/harness/` 只在 git repo 裡。

## 開發者選項

`builder/build.sh` 也可以直接用環境變數呼叫，變數說明在檔案開頭。和預設值不同的開發用設定：

- `GBOS_ADB_PUBKEY=~/.android/adbkey.pub`（或 `install.sh --adb-pubkey`）：只接受這把金鑰的 TCP 5555 adb。KANO 實機上確認過
  沒登記的金鑰會被拒（`unauthorized`），見 SAFETY.md「資料安全取捨」。
- `GBOS_DEFAULT_ENTRY=lapis-debug.conf`：預設改成 serial-debug（序列埠 console + logcat 鏡像）。
- `GBOS_LOADER_EDITOR=yes`：開機選單可以編輯核心參數（harness 的 `kano-boot.sh` 附加參數時需要）。

`install.sh` 會記下這些設定與 `base/`、`builder/`、機型設定的內容摘要（包括還沒 commit 的檔案）；設定或原始碼一變，就會停下來而不是沿用舊的 base 或映像。舊版 `install.sh` 建的工作目錄要先刪掉一次 `base/` 與 `build/`。

## 測試

每次 push 由 GitLab CI 跑 shellcheck／py_compile 與前六項；後面三項需要大磁碟、KVM 或實機的輸入，在開發機上跑。
CI 的容器沒有 loop 裝置，所以 `tests/install/run.sh` 裡用 loop 假碟的案例在 CI 會 SKIP，只跑寫入程式的拒絕測試
與部分防呆；完整案例要在 Linux 開發機上跑。

```sh
tests/builder/run.sh              # 幾秒：語法、輸入防呆、截斷映像、adb 公鑰格式
tests/install/run.sh              # write-usb.sh 防呆（--check-only 加上假的 lsblk，不寫入任何碟；Linux）
tests/install/install-sh-test.sh  # install.sh 的參數與重用防呆
tests/install/kano-check-test.sh  # 換韌體前的檢查，對假的機器資料
tests/stick/selftest.py <空目錄>   # 安裝碟安裝程式的核對邏輯（不需要 QEMU、root 或磁碟）
tests/harness/run.sh              # harness 防呆（假的 ssh／序列埠，不碰實機）與 mutation 檢查
tests/builder/e2e.sh <空目錄> [--zip 已下載的 zip]   # 新使用者完整流程 + QEMU 自我測試
tests/builder/repro.sh <參考映像> <空目錄> [允許的 ESP 差異]   # 用開發設定重建，與實機驗證過的映像逐檔比對
tests/stick/qemu-install.py ...   # 安裝碟（需要真隨身碟的維護分割區 dump，參數見檔案開頭）
```

## 授權

本 repo 的腳本與文件以 GPL-3.0-or-later 授權（Copyright 2026 ImL1s，全文見 `LICENSE`）。Googlebook 映像、裡面的 HAL／APEX、
韌體與 ChromeOS 音效設定屬於各自的權利人，本 repo 不包含、也不散布；建置時由腳本從原始來源取得，適用原始授權。
