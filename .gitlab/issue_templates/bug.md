## 發生了什麼

（一句話）

## 預期

## 實際

## 怎麼重現

1.
2.

## 機器與映像

- 機型、CPU、GPU：（請自己打。報告沒有機型名稱，也不會有 CPU 型號名稱）
- 韌體（請自己打 UEFI 或 coreboot、BIOS 版本、Secure Boot。報告只留廠商列舉，不留版本字串）：
- profile／建置版本：
- 顯示、音效、Wi-Fi、藍牙、觸控、休眠：哪些正常、哪些不正常

## 回報

附上 `summary.md`（`install/gbos-report.sh`，只讀、不改裝置）。報告沒有自由文字，不含機型名稱、BIOS 版本、dumpsys／logcat 原文。上面的機型與韌體版本請自己打。腳本不會附上原始 log；若需要請另外向使用者索取，不要把序號、MAC、帳號、SSID 或 IP 貼進 issue。辨識到硬體不代表功能已驗證。

```sh
install/gbos-report.sh --out gbos-report
```
