## 機型

報告沒有自由文字。機型名稱、BIOS 版本和其他描述都不會出現在 `summary.md` 裡。下面幾列請自己打。

- 品牌與型號：
- CPU：
- GPU（PCI ID，例如 `8086:46a6`）：
- 韌體（請自己打 UEFI 或 coreboot、BIOS 版本、Secure Boot 開或關）：

## 這台機器上什麼能用

對照 `summary.md`。能用的寫「能」，不能的寫現象（黑畫面、沒有聲音、掃不到網路）。

- 顯示／GPU 驅動有沒有綁上：
- 音效卡：
- Wi-Fi：
- 藍牙：
- 觸控板、觸控螢幕：
- 休眠／喚醒（成功與失敗次數）：
- 其他：

## 回報檔

把 `install/gbos-report.sh` 產生的 `summary.md` 貼在下面，或加為附件。不要另貼序號、MAC、帳號、SSID 或 IP。報告沒有機型名稱，也沒有 BIOS 版本；那些請填在上面。腳本不會附上原始 log；若需要請另外向使用者索取。辨識到硬體不代表功能已驗證。

在建置用的 Linux、WSL 或 macOS（bash 3.2 以上與 python3）上跑。Googlebook 已經開機，`adb devices` 看得到。無線除錯用 `--serial 位址:埠`：

```sh
install/gbos-report.sh --out gbos-report
# 已經有 live Linux 上 install/gbos-probe.sh 的結果時，兩份一起：
install/gbos-report.sh --adb --probe gbos-probe --out gbos-report
# 只有 live Linux、不連 Googlebook：
install/gbos-report.sh --probe gbos-probe --out gbos-report
```

## 映像

- profile 與建置版本（`summary.md` 的那兩列）：
- 從 USB 開機，還是內建碟：
