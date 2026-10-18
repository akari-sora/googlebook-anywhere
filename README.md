# Googlebook Anywhere

**中文** | [English](README.en.md)

把 Google 官方的 **Googlebook OS**（lapis，Android 17 桌面版）裝到原本不支援它的 x86-64 電腦上。第一個目標機型是
**Acer Chromebook Spin 714（KANO 主機板）**。

- 下載與版本說明：[GitLab Releases](https://gitlab.com/aa22396584/googlebook-anywhere/-/releases)（最新 v0.2.1，見 [docs/RELEASE-NOTES.md](docs/RELEASE-NOTES.md)）
- 原始碼與 issue：[gitlab.com/aa22396584/googlebook-anywhere](https://gitlab.com/aa22396584/googlebook-anywhere)（正本）。GitHub 上的 [akari-sora/googlebook-anywhere](https://github.com/akari-sora/googlebook-anywhere) 是唯讀鏡像，issue 與下載請到 GitLab。

> 這不是 Google 的官方專案。repo 裡沒有任何 Google 的檔案：腳本從 Google 下載官方 recovery 映像，用固定的 SHA-256 核對後，
> 在你自己的電腦上建出可開機的映像。不刷韌體、不寫 SPI／EC／Cr50。

## 一眼看懂

| 項目 | 現況 |
|---|---|
| 實機驗證 | 只有一台 KANO：內顯 `8086:46a6` 的 Alder Lake-P K0 工程樣機 |
| 其他 KANO 型號 | 12 代 CP714-1WN、13 代 CP714-2WN／Zydron（i3／i5／i7）已補齊內顯 ID 與 13 代音效韌體，應可用，**尚未實機測試** |
| CPU 下限 | Intel 第 12 代或 AMD Zen 5 及更新；更舊的 CPU 預期開不了機（[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)「CPU 最低需求」） |
| 其他 x86-64 電腦 | 實驗性 `generic-x86`、`amd-hx370`，只在 QEMU 驗證過 |
| 安裝方式 | 自己建映像（約 10～30 分鐘，另需 7.7 GB 下載、80 GB 磁碟）→ 寫入 USB 隨身碟開機；要裝到內建碟用「安裝碟」 |
| 安全 | 已知的韌體更新程式移除，EC／TPM／SPI 的核心路徑封鎖，見 [docs/SAFETY.md](docs/SAFETY.md) |

<details>
<summary>原始說明（機型與內顯 ID 的完整描述）</summary>

把 Google 官方的 **Googlebook OS**（lapis，Android 17 桌面版）裝到原本不支援它的 x86-64 電腦上。目標機型是共用
**KANO** 主機板的 **Acer Chromebook Spin 714 家族（12 代 CP714-1WN 與 13 代 CP714-2WN／Zydron，i3／i5／i7）**；
**目前唯一在實機上測試過的只有其中一個 SKU（內顯 `8086:46a6` 的 Alder Lake-P K0 工程樣機）**，其餘 KANO 子型號已補齊內顯 PCI ID 與 13 代 SOF 音效韌體（應可用，尚未實機測試，見 [docs/STATUS-KANO.md](docs/STATUS-KANO.md)）。
**v0.2.0 只放行內顯 `46a6`；其他內顯 ID 的 SKU 會黑畫面（未在這台開過的零售與 coreboot 列出的 SKU 為推論）。**

</details>

## 畫面

| Acer Chromebook Spin 714（KANO）實機 | 同一台，設定精靈 | 一般 x86 電腦（`generic-x86`，QEMU） |
|---|---|---|
| ![KANO 實機上的 Googlebook 歡迎畫面](docs/images/kano-welcome.png) | ![KANO 實機上的 Googlebook 設定精靈](docs/images/kano-setup.png) | ![generic-x86 映像在 QEMU 的歡迎畫面](docs/images/generic-x86-qemu.png) |

前兩張是 KANO 實機的螢幕截圖（全新映像開到設定精靈，2026-10-12）。第三張是通用版映像在 QEMU 裡的畫面：
從韌體一路開到設定精靈，畫面由韌體的 framebuffer 加 CPU 繪圖輸出（2026-10-13，新使用者流程 `install.sh` 建出的映像）。

## 現況（KANO 實機）

| 功能 | 狀態 |
|---|---|
| 桌面、Wi-Fi、藍牙、觸控板、鍵盤、喇叭、麥克風、電源鍵 | ✅ 可用 |
| 上蓋休眠／喚醒、電池電量、「充電中」顯示 | ✅ 可用（v0.2 起） |
| 觸控螢幕、觸控筆 | 🟡 偵測得到，還沒做實際點擊測試 |
| 耳機孔、USB-C 資料與外接螢幕 | ⚪ 未驗證 |
| 螢幕關著時用鍵盤叫醒 | ⚠️ 已知問題，請用電源鍵或開合上蓋 |
| 自動旋轉、光感測、鍵盤背光調整 | ❌ 不可用（經過 EC，刻意封鎖或硬體本身不支援） |
| 內建相機 | 🔧 開發中，預設關 |

每一項的驗證方式見 [docs/STATUS-KANO.md](docs/STATUS-KANO.md)。

<details>
<summary>完整說明（待機、喚醒過濾、機型對照）</summary>

在 KANO 上可以日常使用：桌面、Wi-Fi、藍牙、觸控板、鍵盤、喇叭、麥克風、電源鍵、上蓋休眠／喚醒都在實機上驗證過；
觸控螢幕與觸控筆偵測得到，但還沒做實際點擊測試。每一項的驗證方式見 [docs/STATUS-KANO.md](docs/STATUS-KANO.md)。

做不到的：自動旋轉與光感測、鍵盤背光調整（都要經過 EC，刻意封鎖或硬體本身不支援）、內建相機（開發中，預設關）。
「充電中」圖示：v0.2 起映像帶 ACPI 充電器驅動（`ac.ko`，只讀 ACPI，不送 EC 指令），KANO 實機上接充電器時 Android 顯示充電中（2026-10-13）。
待機：v0.1 闔上蓋子或關螢幕後幾十秒內會自己亮起（每次暗中喚醒都帶一個電源鍵的 `KEY_WAKEUP`）；v0.2 起映像帶
`gbos_wakefilter.ko`，電源鍵沒有喚醒系統時就丟掉它。KANO 實機上闔蓋 300 秒期間暗中喚醒 10 次，螢幕都沒亮；螢幕關閉時按電源鍵會亮、開蓋會亮螢幕。
**已知問題**：螢幕關著時按鍵盤可能叫不醒螢幕（喚醒那一下按鍵送到 Android 之前，模組已經把 `KEY_WAKEUP` 丟掉了）。這是已知問題，v0.2.1 沒有修，請用電源鍵或開合上蓋。

**實機驗證僅在一台 KANO（`8086:46a6` Alder Lake-P K0 工程樣機）上完成**：`profiles/kano` 的 12 個內顯 ID 裡，7 個對到零售 KANO SKU，5 個（`46a3`、`46b2`、`a721`、`a7ac`、`a7ad`）是平台候選（ADL-P/RPL-U 巨集成員），KANO 對照未確認，見 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)。零售對照寫在 STATUS-KANO 與 COMPATIBILITY（Acer 零售型號與 coreboot 的 KANO 清單）。涵蓋 12 代 CP714-1WN 的 ADL-P/U 與 13 代 CP714-2WN／Zydron 的 RPL-U，含單通道變體；是 `adl_p_desc` 的子集（不用 `*`）以及 ADL／RPL SOF 韌體，但除 `46a6` 工程樣機外的量產 SKU 均尚未實機測試（各 SKU 狀態與待測項目見 [docs/STATUS-KANO.md](docs/STATUS-KANO.md)、[docs/FIRMWARE.md](docs/FIRMWARE.md)）。v0.2 起另有實驗性的 `profiles/generic-x86`，給 GPU 在 Googlebook 核心裡沒有驅動的
x86-64 UEFI 電腦（例如 AMD；CPU 要 Intel 第 12 代或 AMD Zen 5 及更新，更舊的預期開不了機，見 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)「CPU 最低需求」）：用韌體的畫面輸出（GOP framebuffer）加 CPU 繪圖（SwiftShader），在 QEMU（`-cpu host`）開到設定精靈，沒有
GPU 加速，尚未在任何實體 PC 上測試，見下面「其他 x86-64 電腦（實驗性）」。新機型的適配見 [docs/NEW-DEVICE.md](docs/NEW-DEVICE.md)。
開起來的機器請用「回報你的機器」產生 `summary.md`，貼到 [`.gitlab/issue_templates/new-machine.md`](.gitlab/issue_templates/new-machine.md)
或 [`.gitlab/issue_templates/bug.md`](.gitlab/issue_templates/bug.md)。

</details>

## 安裝步驟

使用前請讀 [docs/SAFETY.md](docs/SAFETY.md)，特別是「資料安全取捨」：`/data` 沒有加密。遇到問題先看 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

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

<details>
<summary><code>install.sh</code> 做了哪些事（點開）</summary>

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

</details>

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
這個檔沒有維護系統，不能拿來裝內建碟。要裝到內建 NVMe，做下面第 4 步。

之後的新版本用 `install/update.sh --work ~/gbos-work --disk /dev/disk/by-id/usb-<你的隨身碟>` 更新，`/data` 保留，舊版本留在開機選單上（「(previous version)」）。**保留 `~/gbos-work/keys/`**：更新必須用同一把 AVB 金鑰。見 [docs/UPGRADE.md](docs/UPGRADE.md)。

### 4. 裝到 KANO 的內建碟（安裝碟）

`install/make-installer-stick.sh` 把你建好的 Googlebook 映像做成**一個**安裝碟檔（旁邊寫 `.sha256`），開機後選「Install Googlebook to the internal disk」就能裝到內建 NVMe。

<details>
<summary>安裝碟檔裡有什麼、要多大的隨身碟（點開）</summary>

`install/make-installer-stick.sh` 把你建好的 Googlebook 映像做成**一個**安裝碟檔，旁邊寫 `.sha256`。它只寫這個檔，不開區塊裝置，也不需要 root。檔案裡已經有：

- Googlebook，複製到它最後一個分割區為止（映像尾端多出來的磁區不複製）。
- 512 MiB 的 `XBOOTLDR`：`vmlinuz-linux`、`initramfs-linux.img`，以及四個開機選項（Install Googlebook to the internal disk、Update Googlebook on the internal disk (keep data)、Googlebook (internal disk)、Googlebook (internal disk) serial-debug）。沒有普通 Maintenance 殼層。讀序號時會載入 NVMe 驅動，碟看得到；內建碟只會在選了安裝項、並輸入序號之後才被清掉，確認前不寫入、不掛載、也不丟棄。
- 7936 MiB（8321499136 bytes）的 `kano-maint`（ext4，裡面的檔案擁有者記成 uid 0）：`gbos-install` 與它的服務、`gbos-update.py`、安裝程式自己會核對的 pristine 資料、`module-blacklist.txt`、`builtin-initcalls.txt`。

成功時印 `minimum_stick_sectors`。預設做出的檔案就是這個磁區數（512 bytes 一區），可以稀疏存放。Googlebook 複製到 LBA 37244880 的代表映像是 54546417 磁區、27927765504 bytes。報 28.0 GB 的隨身碟是 28000000000 bytes（28×10^9），裝得下這份檔。`write-usb.sh` 拒絕比映像檔還小的碟。別的映像用它自己印出的 `minimum_stick_sectors`。稀疏存放不會少掉這個容量。

</details>

這一步要在 Linux x86_64 上做。`assemble` 用 `unshare -U -r -m -p --fork` 與 chroot 做 initramfs，一般使用者命名空間必須允許。macOS 與 `docker-install.sh` 可以建上面的 Googlebook 映像，做不了維護系統這一步。`fetch` 拒絕還沒有的快取目錄。`assemble` 的 `--out` 還不能存在。`make-pristine.py` 的輸出目錄還不能存在。`pack` 不是這段的輸入。順序與磁區下限的細節見 [install/stick/README.md](install/stick/README.md)。

```sh
mkdir -p "$HOME/gbos-maint/cache"
install/stick/make-maint.sh fetch --cache "$HOME/gbos-maint/cache"
install/stick/make-maint.sh assemble --cache "$HOME/gbos-maint/cache" --out "$HOME/gbos-maint/maint"
install/stick/make-pristine.py ~/gbos-work/build/image/googlebook-kano.img "$HOME/gbos-maint/pristine"
install/make-installer-stick.sh \
  --image ~/gbos-work/build/image/googlebook-kano.img \
  --out "$HOME/gbos-maint/stick.img" \
  --xbootldr "$HOME/gbos-maint/maint/xbootldr" \
  --root "$HOME/gbos-maint/maint/root" \
  --pristine "$HOME/gbos-maint/pristine"
sudo install/write-usb.sh "$HOME/gbos-maint/stick.img" /dev/disk/by-id/usb-<你的隨身碟>
```

`pristine` 必須來自**同一個** Googlebook 映像。寫入仍是 `write-usb.sh`（只接受 USB 整顆碟、要輸入序號、寫完回讀，再把備份 GPT 移到碟尾）。不要加 `--keep-data`：`install/gbos-update.py` 拒絕磁碟上有發行版沒有的分割區，多出來的就是 XBOOTLDR 與 kano-maint。

開機後選 **Install Googlebook to the internal disk**。維護系統已經在 `kano-maint` 上，不必再跑 `setup-stick.sh`。這會清掉內建碟上的一切。還沒有在真的 KANO 上做過這次全新安裝；Linux 測試主機的 QEMU 是直接開這個檔，沒有另注入維護系統，見 [docs/INSTALL-INTERNAL.md](docs/INSTALL-INTERNAL.md)。

使用前請讀 [docs/SAFETY.md](docs/SAFETY.md)，特別是「資料安全取捨」：`/data` 沒有加密。

## 其他 x86-64 電腦（實驗性，v0.2）

給 Googlebook 核心沒有 GPU 驅動的 x86-64 UEFI 電腦（例如 AMD），以及想先試試看的人。CPU 要 Intel 第 12 代或 AMD Zen 5 及更新；更舊的 CPU（Intel 第 11 代及更早、AMD Zen 4 及更早）缺少 Googlebook 直接使用的指令，預期開不了機（見 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)「CPU 最低需求」）。**只在 QEMU（`-cpu host`）開到設定精靈驗證過，
還沒在任何實體 PC 上開過**；畫面靠韌體的 framebuffer、繪圖靠 CPU（沒有 GPU 加速），會慢，解析度由韌體決定。

1. **先看相容性（唯讀）**：在目標電腦上開任何 Linux 隨身碟（例如 Ubuntu 的 Live USB），執行
   `install/gbos-probe.sh --out gbos-probe`。它只讀 `/sys` 與 `/proc`，不載入模組、不碰磁碟、不讀序號，結果在
   `gbos-probe/report.txt`：哪些裝置 Googlebook 有驅動（Wi-Fi、音效、觸控板、儲存裝置…），哪些沒有。
2. **建置**（Linux 或 WSL2）：`GBOS_PROFILE=profiles/generic-x86 install/install.sh --work ~/gbos-generic`。
   會多下載 Cuttlefish 公開建置裡的 SwiftShader（釘選 SHA-256），並從釘選的核心樹建 `gbos_efifb.ko`。
   成品是 `~/gbos-generic/build/image/googlebook-generic-x86.img`。`docker-install.sh` 目前只建 KANO。
3. **寫入與開機**：和上面第 3 步一樣寫入隨身碟。目標電腦要能從 USB 開 UEFI 開機、**關閉 Secure Boot**（映像沒有 Secure Boot 簽章）。
   - **Windows / BitLocker 必做前置步驟（進 BIOS 改 Secure Boot 之前）**：如果目標電腦有裝 Windows，進 BIOS 動 Secure Boot 之前，先在管理員 PowerShell 執行 `manage-bde -status` 檢查每個磁碟區。只要有任何磁碟區啟用了 BitLocker，**必須先把每個加密磁碟區的 48 位數 BitLocker 復原金鑰（Recovery Key）備份到另一台裝置**（到 <https://aka.ms/myrecoverykey> 或對 `manage-bde -status` 列出的每個加密磁碟區執行 `manage-bde -protectors -get <磁碟區>`，例如 `C:`、`D:`，並抄寫／拍照到手機）。切換 Secure Boot 可能會改變開機量測與 BitLocker 所綁定的 TPM PCR（例如 PCR 7，或依平台設定綁定的 PCR 0、2、4、11），下次開回 Windows 時**可能會要求輸入 48 位數復原金鑰**。`Suspend-BitLocker -MountPoint "C:" -RebootCount 1` 只是備份好復原金鑰之後的選用便利指令（暫停期間磁碟上的金鑰未受 TPM 保護，且重開機次數用完後仍會檢查 TPM PCR；回到 Windows 後請用 `manage-bde -status`／`Resume-BitLocker` 確認保護已恢復），**絕對不能代替備份 48 位數復原金鑰**。
   - 預設選項是 **Googlebook generic x86 quiet**。

韌體、TPM、EC 的路徑和 KANO 一樣封鎖（同一份 `_safety.toml`），內建 NVMe 在開機時就被藏起來（`initcall_blacklist=nvme_init`）。
**內建碟是 eMMC 的電腦目前沒有藏起來**，Android 可能把它顯示成外接儲存，不要對它做格式化或「用作內部儲存空間」。
Wi-Fi、音效這類需要機型韌體的功能，generic 映像
都沒有帶，多半不能用；能用的範圍以 probe 的報告為準。要做成正式支援的機型，見 [docs/NEW-DEVICE.md](docs/NEW-DEVICE.md)。

另有針對 **AMD Ryzen AI 9 HX 370 / Radeon 890M** 的實驗性 `profiles/amd-hx370`（需搭配自建的 Track 2 GKI 核心與 `tools/mesa/build-radv.sh` 建出的 RADV。設定裡有 `[gralloc]` 時還需要 `GBOS_MINIGBM`，也就是 git repo 裡 `tools/minigbm/build-libminigbm-gralloc.sh` 建出的 `libminigbm_gralloc.so`，sha256 必須與 profile 一致。發佈包只帶 `tools/minigbm/NOTICES.txt`，沒有那支建置腳本。目前只在 QEMU 驗證過，尚未在實機開機）：建置時直接固定兩個開機選單項目，**不做開機期間動態切換**：
- **預設 `"Googlebook AMD HX370 safe"`**（`lapis-quiet.conf`）：**不載入 `amdgpu`**（列入 `module_blacklist` 且不執行 `modprobe amdgpu`），走與 `generic-x86` 相同的已知可用路徑（UEFI GOP framebuffer + `gbos_efifb` + SwiftShader `pastel`），已在 QEMU 開到設定精靈驗證。
- **實驗 `"Googlebook AMD HX370 native amdgpu (experimental)"`**（`lapis-debug.conf`）：在 `early-boot` 載入一次 `amdgpu`、絕不啟動 `gbos_efifb`，設定 `ro.hardware.vulkan=radeon` 並依 sysfs `DRIVER=amdgpu` 指定 HWC 的 DRM 裝置。safe 與 native 兩個選項載入同一顆 `/vendor/lib64/libminigbm_gralloc.so`（`tools/minigbm` 重建，含 `backend_amdgpu` 與 `backend_vkms`）。**誠實揭露**：此選項尚未在實機測試；若選此項出現黑畫面，請重開機選預設的 `"Googlebook AMD HX370 safe"`。

## 回報你的機器

在建置用的 Linux、WSL 或 macOS（bash 3.2 以上，另外要有 python3）上跑。Googlebook 已經開機，`adb devices` 看得到；無線除錯加 `--serial 位址:埠`。
它只對裝置下固定的唯讀指令：`getprop`、`dumpsys`（input、display、audio、power、battery、wifi、bluetooth_manager）、`ls /dev/dri`、`ls /sys/class/bluetooth`、
指定的 `/proc` 與 `/sys` 檔（含 DMI，以及最多四張 DRM 卡的 vendor、device、uevent）、過濾過標籤的 `logcat -d -b crash,main`、`uname -a`。沒有 `adb root`，不安裝、不改設定、不推檔案。
其他指令會被拒絕。

```sh
install/gbos-report.sh --out gbos-report
# 已經有 live Linux 上 install/gbos-probe.sh 的目錄時，兩份一起（仍會連 adb）：
install/gbos-report.sh --adb --probe gbos-probe --out gbos-report
# 只看 live Linux 的探測、不要連 Googlebook：
install/gbos-report.sh --probe gbos-probe --out gbos-report
```

`gbos-report-<日期>.txt` 與 `summary.md` 沒有自由文字。值只會是整數、固定格式的十六進位識別碼（PCI 或 USB 的 `xxxx:xxxx`）、列舉（已知驅動，否則 `other` 並保留那個 PCI 或 USB id；已知廠商，否則 `other`；CPU 廠商 GenuineIntel／AuthenticAMD／other；介面只報 `wlan0`–`wlan9`、`eth0`–`eth9`、`hci0`–`hci99`（沒有前導零），DRM 節點只報 `card0`–`card99` 或 `renderD0`–`renderD999`（沒有前導零）；狀態 on／off／connected／disconnected／unknown），以及這個專案自己的版本字串（profile id 與狀態、我們發出的 lapis 建置編號、只有與映像釘選的核心版本字串完全相同才原樣印出。設 `GBOS_DEV_KERNEL_DIR=<核心樹>`，且執行中核心的 release 等於該樹的 `include/config/kernel.release` 時，寫 `dev-kernel`（開發核心不印 release）；該樹的 `.config` 是一般檔時，再依它是否等於釘選 GKI 設定（sha256）寫 `config pass`／`config fail`。其他讀得到的 release 寫 `other`）。機型名稱、BIOS／EC 版本、CPU 型號名稱、音效卡名稱、裝置名稱、SSID、序號、這份清單以外的介面名稱都不進報告。請在 issue 裡自己填機型、BIOS 版本，以及其他你想補充的描述。缺值或檢查不過寫成「未知」。建置編號先看 `ro.build.display.id`，缺值或檢查失敗才看 `ro.build.id`；核心先看 `uname`，沒有 release 才看 `/proc/version`；PCI id 先看 uevent 的 `PCI_ID`，缺值或檢查失敗才看 sysfs 的 vendor 與 device。Wi-Fi／藍牙狀態讀不到時，已讀到的驅動和 PCI 或 USB id 仍寫出來，不因此當成已連線或已開啟。這三處的候選都失敗就是「未知」，不會改拿產品名稱或自由文字。dumpsys 與 logcat 的原文不在報告裡。這個腳本不附原始 log；若真的需要，請直接向使用者索取。辨識到硬體不代表聲音、觸控、Wi-Fi 或休眠已經驗證過。

「已綁定」只在已知的真實 GPU 驅動（i915、xe、amdgpu、nouveau 等）綁上、而且該卡有讀到時才會出現。「沒有原生 GPU 驅動」只在每一張 DRM 卡都讀到、而且沒有一張是那些驅動時才會出現。DRM 清單是空的，或只有 SwiftShader、還沒讀到任何卡，寫「未知」，不寫「沒有原生 GPU 驅動」。清單沒擷取到也是未知，而且不能合併，兩份報告都不從 sysfs 或 uevent 建卡列；成功讀到的空清單，以及沒有 PCI id 的平台裝置，仍可合併。只有成功擷取的清單點名的 card，其 sysfs 與 uevent 才算。缺檔、ENOENT、拒絕讀取，或其他非零結束，都不是成功擷取。沒被點名的卡的檔案略過；清單裡沒有 card（空清單，或只有 renderD）時顯示是未知，不寫驅動結論，仍可合併。缺檔不是空匯流排。`ro.hardware.vulkan=pastel` 是這個專案附的 SwiftShader（`vulkan.pastel.so`），報告寫 SwiftShader，不寫套件檔名。有任何一張沒讀到就寫「未知」。機型是 DMI 廠商列舉，不是產品名稱，也不用 `ro.product.model`。板名是 DMI 板廠列舉；板名是空的也不改拿 `ro.product.board`。probe 的 UEFI／Secure Boot、草稿 profile，以及其他 live Linux 事實，只有在廠商列舉、CPU 的 family、model、stepping 三個數字，以及 PCI 匯流排上讀到的全部裝置 id（含重複，不只 GPU）都一致時才寫進報告。沒有 PCI DRM 驅動的平台裝置（例如 `gbos_efifb`）只要匯流排有讀完，仍可合併。CPU 的型號名稱不參與比對。GPU 或 PCI 有任何一筆沒讀到，probe 沒有 `pci` 鍵、清單是 null、或整份匯流排沒讀到，或 CPU 三個數字不齊，就省略並註明不是同一台。讀過而且是空的 PCI 清單才是空匯流排；缺檔、缺鍵、或只留下成功的 `.rc` 都不是。每次收集都會先清掉舊的擷取檔和 `.rc`。

開 GitLab issue 時選模板：新機器用 [`.gitlab/issue_templates/new-machine.md`](.gitlab/issue_templates/new-machine.md)
（機型、CPU／GPU、韌體、哪些能用、哪些不能。機型名稱和 BIOS 版本要自己打，報告裡沒有），壞掉的地方用 [`.gitlab/issue_templates/bug.md`](.gitlab/issue_templates/bug.md)。
把 `summary.md` 貼進去或當附件。

## 文件

| 文件 | 內容 |
|---|---|
| [docs/RELEASE-NOTES.md](docs/RELEASE-NOTES.md) | 每一版的變更與已知問題 |
| [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)（[English](docs/COMPATIBILITY.en.md)） | 哪些機器可以裝、CPU 最低需求 |
| [docs/STATUS-KANO.md](docs/STATUS-KANO.md) | KANO 各子型號與每項功能的驗證狀態 |
| [docs/FIRMWARE.md](docs/FIRMWARE.md) | 換成 UEFI 韌體的步驟與風險 |
| [docs/INSTALL-INTERNAL.md](docs/INSTALL-INTERNAL.md) | 裝到內建碟（安裝碟）與備份 |
| [docs/UPGRADE.md](docs/UPGRADE.md) | 保留資料的更新 |
| [docs/SAFETY.md](docs/SAFETY.md) | 安全模型、資料安全取捨、EC 功能變體 |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)（[English](docs/TROUBLESHOOTING.en.md)） | 疑難排解 |
| [docs/NEW-DEVICE.md](docs/NEW-DEVICE.md) | 新機型的適配流程 |
| [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md) | 技術說明 |
| [docs/JOURNAL.md](docs/JOURNAL.md) | 開發紀錄 |

## Repo 結構

| 路徑 | 內容 |
|---|---|
| `install/` | `install.sh`（一鍵流程）、`update.sh` 與 `gbos-update.py`（保留資料的更新，見 docs/UPGRADE.md）、`docker-install.sh`（Docker 版）、`write-usb.sh`（有防呆的寫入；`--keep-data` 更新）、`kano-check.sh`（換韌體前的唯讀檢查）、`gbos-probe.sh`（新機型的唯讀硬體探測）、`gbos-report.sh`（實機唯讀回報，見「回報你的機器」）、`stick/`（安裝碟：在 KANO 上裝到內建碟） |
| `base/` | 官方 recovery → 基礎映像（中間產物：軟體 KeyMint、你的 AVB 金鑰；只給 builder 用），附驗證紀錄 |
| `builder/` | `build.sh`：依機型設定（預設 KANO）建映像（vendor 重建、vbmeta 重簽、ESP、QEMU 自我測試）；`fetch-inputs.sh`；`lib/devprofile.py` 讀取並檢查機型設定與自建核心 `.config` 安全規則 |
| `profiles/` | 機型設定：`kano/`（`profile.toml` 的參數、韌體與封鎖清單，`vendor.py` 的 vendor 修改，音效來源清單）、`generic-x86/`（沒有 GPU 驅動的機器：GOP framebuffer + SwiftShader，實驗性）、`amd-hx370/`（AMD Ryzen AI 9 HX 370：預設 safe GOP + SwiftShader，另附 experimental native amdgpu 選項）與全專案的安全規則 `_safety.toml`；新機型見 [docs/NEW-DEVICE.md](docs/NEW-DEVICE.md) |
| `tests/` | 快速防呆測試、完整流程、可重現性、安裝碟 QEMU 測試、harness 測試（見下面「測試」） |
| `tools/mesa/` | `amd-hx370` 用的 Android x86-64 Mesa RADV（`vulkan.radeon.so`）與 loader probe 建置腳本、ELF 檢查器及第三方授權聲明（`NOTICES.txt`） |
| `tools/minigbm/` | amd-hx370 的 `/vendor/lib64/libminigbm_gralloc.so` 重建腳本（safe 與 native 共用這一顆檔）。`NOTICES.txt` 進發佈壓縮檔，builder 會把它裝進映像。建置腳本、`amdgpu_linear.c` 與 `upstream-delta.patch` 只在 git repo |
| `tools/harness/` | 開發用的遠端實機測試工具（EC 電源控制、序列埠、增量推送、NVMe 安裝）；個人設定放在 `kano-env.local.sh` |
| `tools/release/` | 安裝包與公開 repo 的匯出腳本 |
| `docs/` | 韌體、狀態、安全模型、技術說明、內建碟安裝、新機型適配、開發紀錄 |

發版的安裝包（`googlebook-anywhere-<版本>.tar.gz`）只有 `README.md`、`LICENSE`、`base/`、`builder/`、`install/`、`profiles/`、`docs/`、`tools/mesa/`
與 `tests/` 的 `builder`、`install`、`probe`、`report`、`stick`、`update`，另外附 `tools/minigbm/NOTICES.txt`（映像安裝 notice 時要讀）、
`tests/builder` 會用到的 `tools/release/png-ok.py` 與 `tools/release/published-images.sha256`。
`tools/minigbm` 的建置腳本、`amdgpu_linear.c` 與 `upstream-delta.patch`，以及其他 `tools/` 與 `tests/harness/`，只在 git repo 裡。

## 開發者

<details>
<summary>開發者選項（點開）</summary>

`builder/build.sh` 也可以直接用環境變數呼叫，變數說明在檔案開頭。和預設值不同的開發用設定：

- `GBOS_ADB_PUBKEY=~/.android/adbkey.pub`（或 `install.sh --adb-pubkey`）：只接受這把金鑰的 TCP 5555 adb。KANO 實機上確認過
  沒登記的金鑰會被拒（`unauthorized`），見 SAFETY.md「資料安全取捨」。
- `GBOS_DEFAULT_ENTRY=lapis-debug.conf`：預設改成 serial-debug（序列埠 console + logcat 鏡像）。
- `GBOS_LOADER_EDITOR=yes`：開機選單可以編輯核心參數（harness 的 `kano-boot.sh` 附加參數時需要）。
- `GBOS_EC=sensors,typec,buttons`（可只選幾項）：放行一組經過稽核的 EC 驅動，換來自動旋轉、USB-C 的 USB3／DP、側邊音量鍵。安全保證會跟著改變，而且要先確認休眠能進入 S0ix，見 [docs/SAFETY.md](docs/SAFETY.md)「EC 功能變體」。尚未在實機驗證。

`install.sh` 會記下這些設定與 `base/`、`builder/`、機型設定的內容摘要（包括還沒 commit 的檔案）；設定或原始碼一變，就會停下來而不是沿用舊的 base 或映像。舊版 `install.sh` 建的工作目錄要先刪掉一次 `base/` 與 `build/`。

</details>

<details>
<summary>測試（點開）</summary>

每次 push 由 GitLab CI 跑 shellcheck／py_compile 與下面到 `tests/harness/run.sh` 為止的項目（`tests/report/report-test.sh` 也在 `tests/builder/run.sh` 裡，builder job 會再跑一次）。從 `tests/builder/e2e.sh` 起需要大磁碟、KVM 或實機的輸入，在開發機上跑。
CI 的容器沒有 loop 裝置，所以 `tests/install/run.sh` 裡用 loop 假碟的案例在 CI 會 SKIP，只跑寫入程式的拒絕測試
與部分防呆；完整案例要在 Linux 開發機上跑。

```sh
tests/builder/run.sh              # 幾秒：語法、輸入防呆、截斷映像、adb 公鑰格式，以及下面的 report 測試
tests/install/run.sh              # write-usb.sh 防呆（--check-only 加上假的 lsblk，不寫入任何碟；Linux）
tests/install/install-sh-test.sh  # install.sh 的參數與重用防呆
tests/install/kano-check-test.sh  # 換韌體前的檢查，對假的機器資料
tests/stick/selftest.py <空目錄>   # 安裝碟安裝程式的核對邏輯（不需要 QEMU、root 或磁碟）
tests/update/selftest.py <空目錄>  # 保留資料的更新（gbos-update.py），小型合成碟（不需要 QEMU、root 或磁碟）
tests/update/mutate.py            # gbos-update.py 列出的 25 項防護逐一拿掉，selftest 都必須有檢查失敗
tests/probe/probe-test.py        # install/gbos-probe.sh（新機型的唯讀硬體探測）對假的 /sys、/proc
tests/report/report-test.sh       # install/gbos-report.sh：清除、允許清單的拒絕、假的擷取（不連實機）
tests/harness/run.sh              # harness 防呆（假的 ssh／序列埠，不碰實機）與 mutation 檢查
tests/builder/e2e.sh <空目錄> [--zip 已下載的 zip]   # 新使用者完整流程 + QEMU 自我測試
tests/update/qemu-update.py <空目錄> <舊映像> <新映像>  # 更新的完整流程：舊版開機、更新、新 slot 與 previous version 開機（QEMU）
tests/builder/repro.sh <參考映像> <空目錄> [允許的 ESP 差異] [之後刪掉的 ESP 檔案]   # 用開發設定重建，與實機驗證過的映像逐檔比對
tests/stick/qemu-install.py ...   # 安裝碟的舊參數：用隨身碟分割區 dump，測試會在自己的副本上注入維護系統
tests/stick/qemu-install.py --produced <工作目錄> <Googlebook 映像> <安裝碟檔> <pristine 目錄>
                              # 直接開 make-installer-stick.sh 的檔，不注入、不改那個檔。
                              # 序列埠案例會改開機副本的 loader default，來源檔的 sha256 不變。
tests/stick/qemu-install.py --asis <工作目錄> <Googlebook 映像> <安裝碟檔> <pristine 目錄>
                              # 開機副本與產出檔逐位元組相同，寫入進 snapshot，不改選單。
                              # 普通畫面的虛擬鍵盤跑 Install／Update。結果與序列埠案例分開印。
```

</details>

<details>
<summary>維護者：發版規則（點開）</summary>

打 tag 的那個 commit 要讓 `docs/RELEASE-NOTES.md` 同時滿足下面幾件事。狀態只看固定行，整個檔案每行與每個 Markdown 段落（空行分隔的段落、清單項目、表格列或標題，包含 `>` 引用區塊內）都會掃描，不靠猜版本段落或程式碼區塊的邊界：

- 第 1 行必須正好是 `# Googlebook Anywhere <tag>`（例如 `# Googlebook Anywhere v0.2.1`）。拿掉標題上的 ` (未發布)`。
- 第 2 行是空行。
- 第 3 行必須正好是 `<!-- release-status: released -->`（開發期間是 `unreleased`）。寫在別行的 released 不算。
- 版本字串 `Googlebook Anywhere v…` 只能出現在頂格的 `# Googlebook Anywhere v…` 版本標題行（不縮排、不放進清單或引用、不用 setext、不用 `##`）；內文提到舊版本時只寫 `v0.2.1` 這種版本號。
- 整個檔案只接受純 Markdown（除了第 3 行的狀態註解外，行內 code span 以外的任何原始 HTML 標籤、註解或 `<https://...>` 自動連結一律拒絕；任何數字字元參照 `&#`——不論是否帶分號——與具名字元參照 `&name;` 一律拒絕；含空白、引號、括號、title 或跨行的連結目標 `](...)` 一律拒絕；跳脫方括號 `\[`、`\]` 與跳脫括號 `\(`、`\)` 一律拒絕；連結目標裡的反引號、參考式連結 `[文字][標籤]` 與連結定義 `[標籤]: 網址` 一律拒絕，請用 `[文字](網址)`；第 3 行以外的 HTML 註解 `<!--` 一律拒絕），且從頭到尾不能出現任何未發布字樣（包含三個反引號或 `~~~` 圍起來的程式碼區塊、縮排區塊，以及舊版本段落，沒有任何豁免）：除了逐行檢查列出的字樣外，也會以每個 Markdown 段落（空行或僅含 `>` 的行、setext 底線／分隔線、清單項目、表格列或標題，包含引用區塊內）為單位，在移除連結目標（`](...)`）與單行參考連結標籤（`][...]`）後，並把圖片的 `![`、GitLab 行內差異的 `{+ +}`／`[+ +]`／`{- -}` 與行內數學式的 `` $` ``／`` `$ `` 當成標記移除後，以兩種英文檢視（一種刪除標記符號並將空白與換行折成單一空白，另一種將標記符號與 `-_/` 換成空白再將空白與換行折成單一空白）及一種中文檢視（僅刪除標記符號、空白與換行）比對，因此跨行換行、清單或引用續行、連字號／底線／斜線、連結或強調標記拆開的列出字樣一樣會被擋下。限制：同一段落內相鄰的表格儲存格或句子接起來若剛好湊出禁語仍可能被擋下（請換個寫法）；刻意混淆（例如長得像的字母、不可見字元）不在這個檢查的範圍內——打 tag 的 commit 會經過審查。這一段要拿掉標題的 `(未發布)` 和導言的「這一版還沒有發布。」。會被擋下的字樣還有：未發布、還沒有發布、還沒發布、待發布、即將發布、沒正式發布、沒有正式發布、未正式發布、未發佈、還沒有發佈、還沒發佈、未发布、unreleased、unpublished、not yet released（英文不分大小寫）。

私有 GitLab 專案的 CI/CD 變數 `GBOS_REQUIRE_PRIVATE_SCAN` 寫在專案設定，不寫進 repo。沒有設、去頭尾空白後是空字串、`0` 或 `false`（不分大小寫）時，真正的公開匯出仍會 SKIP。其他任何非空值（包含 `1` 和 `true`）都會強制掃描，缺少 `tools/release/private-patterns.txt` 就失敗。不要把這個變數勾成 Protect variable，否則未受保護的分支和 merge request pipeline 拿不到它，會在沒有掃描的情況下跑。公開 CI 不要設這個變數。

打 tag 的 pipeline 會核對發行說明的第一個標題與 tag 一致，只發布該版本的段落（保留三個反引號或 `~~~` 圍起來的程式碼區塊內的標題），並在結尾補上指向同一個 tag 的完整 `docs/RELEASE-NOTES.md` 歷史絕對連結，讓發版附加檔案保持好找。

</details>

## 授權

本 repo 的腳本與文件以 GPL-3.0-or-later 授權（Copyright 2026 ImL1s，全文見 `LICENSE`）。例外：`tools/minigbm/amdgpu_linear.c` 與 `tools/minigbm/upstream-delta.patch`（BSD-3-Clause；`amdgpu_linear.c` 保留被複製段落的 Chromium OS Authors 著作權並加上 Copyright 2026 ImL1s），以及 `tools/minigbm/stock-dynsyms.txt` 與 `tools/minigbm/stock-undefsyms.txt`（原廠函式庫的符號清單）。libdrm 標頭為 MIT，全文在 `tools/minigbm/NOTICES.txt`。`tools/minigbm/build-libminigbm-gralloc.sh`、`tools/minigbm/README.md` 與 `tools/minigbm/SOURCES.txt` 仍是 GPL-3.0-or-later。另一個例外：`tests/builder/fixtures/extract-ikconfig` 是核心 `scripts/extract-ikconfig` 的逐位元組複本，只給測試當獨立行程呼叫，沒有和本 repo 的程式連結。檔頭只寫 “Licensed under the terms of the GNU General Public License.”，沒有 SPDX、也沒有版本；核心對沒有 SPDX 標記的檔案適用核心自己的 COPYING（GPL-2.0-only）。同目錄的 `kernel-LICENSES-preferred-GPL-2.0` 與 `kernel-COPYING` 是 kernel/common `3938c8d97142` 的 `LICENSES/preferred/GPL-2.0` 與 `COPYING` 逐位元組複本。說明在同目錄的 `extract-ikconfig.SOURCE`。Googlebook 映像、裡面的 HAL／APEX、
韌體與 ChromeOS 音效設定屬於各自的權利人，本 repo 不包含、也不散布；建置時由腳本從原始來源取得，適用原始授權。
