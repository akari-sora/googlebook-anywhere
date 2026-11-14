# Googlebook Anywhere v0.3.2

<!-- release-status: unreleased -->

這一段收集 v0.3.1 之後已進入 main 的變更，內容還沒整理完：ROG Ally 診斷 profile、共用維護媒體與相機候選等其他變更，以及各項的實機範圍，要在發版前補齊並核對。v0.3.1 與更早的內容見 [完整版本紀錄](RELEASE-HISTORY.md)。

## KANO 預設開啟 USB-C（typec）

- KANO 建置沒有設定 `GBOS_EC` 時等於 `GBOS_EC=typec`：放行 `cros_ec`、`cros_ec_lpcs`、`cros_ec_dev`、`cros_ec_typec`、`cros_usbpd_notify`，讓 USB-C 做 USB3 與 DisplayPort alt mode。`sensors`、`buttons`、`drain` 仍須明確選用，而且 `drain` 不能和其他功能並用。其他機型維持不放行；對它們設 `typec` 仍會拒絕。
- 關掉：`GBOS_EC=none`，或把 `GBOS_EC` 設成空字串。`thunderbolt`、`cros_typec_switch`、`cros_ec_ucsi`、`cros_usbpd_charger` 維持封鎖。
- 載入 EC transport 之後，休眠失敗的復原仍可能重設 AP 並丟失進行中的寫入。硬體 S0ix 尚未量到。這是維護者接受的安全模型變更，不是新的實機驗收。
- USB-C 外接畫面與底座能用，是 issue #12 的使用者回報（零售 CP714-2W、v0.3.0、stock 韌體加 MrChromebox RW_LEGACY）。維護者尚未在實機確認。已發布的 v0.3.0 與 v0.3.1 映像仍是當時的預設（EC 封鎖）。
- 套用 typec 之後，serial-debug 命令列是 1892 bytes、quiet 是 1800 bytes，都在 builder 的 1909 bytes 限制內。drain 的 serial-debug 仍是 1909 bytes。

## 相機

- KANO 的 `camera.libcamera` 仍是預設關閉的實驗選項。
- v0.3.1 之後：0013 測試可辨識相機，但預覽仍是黑畫面；後續開發中的 0014 測試 USB 已到桌面並核對執行期身分；實機相機串流驗證已通過，但 CPU 影像處理器拒絕兩條輸出串流，預覽仍黑、拍照未通過，睡醒恢復仍待實測；詳見 [KANO 狀態](STATUS-KANO.md)。
