# 共用安裝與救援碟

目標是同一支 USB 能用於 KANO 與 HX370：先啟動共用維護環境，辨識機型，再選擇該機型的獨立核心、設定與安裝資料。規劃中的共用碟一律先進入維護系統，不自動啟動上一台機器留下的內建碟選項。保留資料的更新與全新安裝是不同動作，仍須經過來源驗證、目標碟核對與確認。

目前已製成包含兩個機型資料的共用診斷映像，通過 600 秒隔離 QEMU 開機健康檢查；安裝、更新與復原仍未接入維護啟動流程，兩台實機也尚未驗證共用媒體。辨識原型讀取現有 profile 中的非唯一機型欄位，輸出 JSON；所有結果的 `write_allowed` 一律為 `false`。例如：

```sh
python3 install/stick/detect-model.py
```

原始碼另已加入 `universal-menu.py` 唯讀更新預覽選單，由獨立的 tty1 服務在診斷服務完成後啟動。選單可明確選擇已可見的內建 NVMe，重新檢查來源、系統健康與目標身分，再顯示目前及下一個系統槽位；找不到磁碟或檢查失敗時停止，不載入 NVMe 驅動、不寫入資料，也不產生可留待稍後使用的寫入授權。選單已納入共用診斷候選，在唯讀、沒有 NVMe 的 QEMU 測試中驗證取消及找不到目標時拒絕；兩台實機的開機、TTY 和硬體操作仍未驗證。

選單沿用 `shared-source.py` 的觀察與規劃功能，相關更新及安裝後端也隨媒體打包，供其載入相依模組。這些後端包含特權寫入功能；唯讀選單並非沙箱，來源信任、備份、安裝、更新與復原的實機驗收仍須另行完成。

`matched` 僅表示目前資料符合某個機型家族規則；`unknown`、`incomplete` 與 `ambiguous` 分別表示無符合、資料不足及多個完整符合。空規則會列為 `unconfigured`，不會符合任何電腦。`Google/Kano` 只能辨識 KANO 家族，不能證明是已驗證的特定硬體；HX370 規則使用 Windows SMBIOS 觀察到的 ONEXPLAYER X1Pro 家族與主機板欄位；Linux sysfs 是否提供完全相同的值尚未實測。符合規則也不等於硬體相容或寫入授權。

後續需要完成共用維護系統、各機型資料與來源驗證、安裝器整合及獨立的復原狀態，再用同一支 USB 分別在兩台實機驗證。模擬與唯讀辨識結果不等於實機安裝或支援驗收。

## 共用新裝與更新的確認控制器（尚未接入實機入口）

`install/stick/universal-controller.py` 提供內部 `run()`，處理新裝或保留資料更新的
磁碟選擇、操作說明、明確確認與再次核對。呼叫端提供既有選單 `snapshot()` 的
機型／build／catalog 雜湊觀察、固定的 `shared-source.py` 模組及互動輸入輸出。
控制器沒有命令列寫入入口，也不接受任意命令或從檔案載入的 callback。
目前的診斷選單及 systemd 服務仍只做唯讀預覽，沒有呼叫這個控制器。

控制器與更新來源綁定接受 `kano`、`amd-hx370` 及可選的 `rog-ally`。
Ally 的合成機型規則、清單、來源檔案綁定與唯讀規劃測試已通過；缺少或不匹配的
機型資料、來源內容變更會被拒絕。這尚未提供正式 Ally 機型設定或實機可開機映像。

即使只找到一個磁碟，也必須先選擇目標，再輸入畫面提示的完整操作與裝置路徑，
例如 `INSTALL /dev/nvme0n1` 或 `UPDATE /dev/nvme0n1`。新裝明示清除全部資料，
更新明示保留 userdata 及目前系統槽位的意圖。這是操作與路徑確認，尚不是序號確認。
更新會在確認前後各做一次既有唯讀規劃；新裝不要求目標已有可解析的系統。
機型、來源雜湊、完整目標事實或更新規劃有變動時，停止而不呼叫寫入後端。

核對通過後，控制器只呼叫一次既有 `install_observed_block()` 或
`apply_observed_block()`，並傳入同一份已確認的完整目標事實。取消或確認前拒絕
不呼叫後端；呼叫後若發生例外、中斷或回報內容不符，結果為 `UNKNOWN`，明示目標
可能已修改，不自動重試。`COMPLETED` 只表示後端回報符合介面契約，
`hardware` 仍為 `NOT_VERIFIED`。

打包器會攜帶並讀回核對控制器檔案，不會因此新增自動寫入服務。
確認流程仍須由可信維護環境接線；來源信任、備份／還原、完整的使用中檢查與排他、
開機政策及三台機器的共用流程實測都尚未完成。控制器本身不提供這些保證，也不實作復原。

離線測試分為使用假後端的互動流程，以及以測試 adapter 呼叫真實一般映像後端的整合：

```sh
python3 tests/stick/universal-controller-test.py
python3 tests/stick/universal-controller-integration.py
```

後者只寫入新建的小型一般檔案，驗證兩個機型的新裝／更新結果；測試 adapter 將虛構的
NVMe 顯示名稱對應至該檔案，不能證明實體區塊裝置檢查、實機安裝或 Android 開機。
整合測試需要 Linux、mtools 及 lz4；上述測試亦已接入既有 builder 測試入口。

## 第二階段：離線安裝資料清單驗證

`install/stick/catalog.py` 可檢查 KANO 與 HX370 資料包的清單及映像，
用呼叫者從獨立管道提供的清單 SHA-256 綁定原始清單，再檢查各映像的
大小與完整 SHA-256。工具不信任放在清單旁邊的雜湊檔；清單雜湊本身也
不是簽章或來源證明。它不呼叫機型辨識器或任何寫入工具。

```sh
python3 install/stick/catalog.py --root BUNDLE_DIR --catalog catalog.json --sha256 EXPECTED_SHA256
```

版本 1 的清單使用 `schema: 1` 與 `payloads` 陣列；每項包含 `profile`
（`kano` 或 `amd-hx370`）、`build_id`、`image`（相對 `path`、位元組
`size`、`sha256`）、`boot_entries` 字串陣列及固定為 `unvalidated` 的
`hardware_validation`。啟動選單清單只是預期內容，尚未檢查 ESP；工具
也不驗證啟動政策、AVB 或實機硬體，輸出永遠是 `write_allowed: false`。

工具拒絕不明欄位、重複 JSON 鍵或機型、越界路徑及符號連結，並在讀取
期間檢查檔案變化和路徑替換。程式介面在同一行程中保留已檢查的檔案
描述符直到關閉；這不會阻止檔案稍後被修改，也不能保證路徑永久不變。
這是離線驗證原型，並非可開機的共用媒體或安裝授權。

## 唯讀 release 相容性預覽（實驗性）

`install/stick/shared-source.py` 接受清單根目錄、清單路徑、獨立提供的
清單 SHA-256 及機型 ID；選擇的來源以已驗證的開啟中描述符讀取，並以
現有更新器的 release parser 檢查。此預覽將 `build_id` 解釋為 Android
build 的十進位數字，`boot_entries` 解釋為 ESP `loader/entries` 下
`.conf` 檔名的完整集合。任何不一致均拒絕。可選的
`--synthetic-installed-image` 僅供最多 128 MiB 的合成一般檔案，會先複製到
私有暫存檔再呼叫既有相容性檢查；沒有安裝、更新或復原入口。

此工具需要 Linux 的 `O_PATH`、`/proc/self/fd` 和 mtools。它先以 `O_PATH`
檢查輸入檔 inode 類型，再開啟一般檔案資料；描述符會維持到檢查結束。
最後重查中繼資料、路徑及雜湊，可偵測觀察到的變動，但不保證檔案在
整段期間不可變。輸出永遠不授權寫入，不證明實機、AVB 或啟動政策；
ESP 標題中的 model 字串也不是機型辨識結果。

預覽限制 ESP 為 512 MiB、每個 vbmeta slot 為 16 MiB、每份 LP metadata 為
1 MiB、至多四個 slot 和 16384 個分割區引用的 extent。既有底包研究
記錄 ESP 64 MiB、vbmeta_a 4 MiB；合成 fixture 使用 64 KiB、三個 slot。
預覽的 ramdisk 解壓上限為 256 MiB，LZ4 解碼子程序限時 60 秒並由呼叫者
回收。這些界限不改變舊更新器路徑，也不是適用所有映像的格式規格；
實際映像的 ramdisk 大小仍需核對。不應將此預覽視為不可信輸入的沙箱。

## 合成檔案的中斷續跑一致性（開發中）

未提供下述 `SharedBlockOperation` 的共用更新路徑，只允許不超過 128 MiB 的合成一般檔案，以仍開啟且驗證過的
catalog 映像建立 release；catalog context 負責檢驗外部提供的清單雜湊與選取，
更新器自行檢查持有的來源檔描述符並建立 release；stick 與 NVMe 模式都只限此合成範圍，且強制保留回退、不得傳入 stick boot。沒有提供寫入 CLI 或實機支援。它把來源、作業、
slot 與目標 GPT 身分的版本化摘要存進更新狀態；狀態同步讀回後才開始更新。
帶標記狀態須使用相符的共用來源續跑，舊呼叫者會拒絕；共用呼叫者也拒絕
未標記的中斷狀態。若已進入無法確保舊選單回退的晚期階段，會保留狀態並
拒絕自動續跑。

這只檢查一致性，不是防竄改機制：若外部刪除狀態或所有共用標記，不能與
原有未標記狀態區分。描述符也不能凍結來源檔案內容；合成路徑不建立實體
裝置身分或獨占持有。可信 profile 與 release 內容的獨立綁定、實機授權仍待整合。

## 共用來源的逐塊讀取核對

共用更新器在核對完整映像 SHA-256 的同一次掃描中建立 1 MiB 區塊摘要；
完整雜湊及檔案狀態均符合後才啟用。後續讀取先核對涵蓋範圍的完整區塊，
再從同一份已核對的記憶體內容回傳資料；不會核對後重新讀取未驗證的位元組。
這綁定實際讀出的內容，不凍結原始映像，也不認證機型。共用更新使用的 ESP
另有下述封印快照；一般唯讀 preview 的暫存檔不具同樣保證。

若來源在更新寫入階段變動，流程回報更新失敗並保留原始原因與中斷狀態；
此時部分目標資料可能已寫入，不能宣稱「完全未寫入」。合成測試核對拒絕後
舊槽、使用者資料、舊版開機檔案及更新狀態仍保留；這不是實機驗收。

## 共用來源的開機資料與實際更新入口

`SharedBinding` 從逐塊驗證的 reader 建立 Linux memfd ESP 快照，填妥後封印
寫入、增長、縮小和追加封印。mtools 繼承該 fd，以有上限的 stdout 讀取
kernel、initrd、選單和 bootconfig；不經可變的來源暫存路徑。封印功能不可用
即拒絕。binding 結束時關閉快照，catalog 仍負責原始來源描述符的生命週期。
帶共用來源型別的 release 即使目標尚無更新狀態，也必須傳入相符且仍開啟的
binding，不能省略它而走舊更新器路徑。

既有目標 FAT 的讀寫改由 mcopy stdout／stdin 傳遞 bytes 並讀回核對；共用
更新的 ramdisk 轉換也使用持有的 bytes 與匿名檔描述符，避免重新開啟可變的
`fat-read`、`fat-write` 或 `ramdisk.cpio`。這不防禦其他特權程序直接修改目標。

內部 `apply_observed_image` 依完整且唯一的 DMI 規則選來源，整次持有 catalog、
binding、私有暫存目錄與目標 `Disk`，真正呼叫既有 updater。它只更新明確指定的
不超過 128 MiB 一般測試映像，沒有寫入 CLI、block override 或實機授權。
唯讀觀察後開啟可寫 fd，比對身分與容量、排除來源別名；耗時規劃完成後、
首次寫入前再查一次。檢查是離散觀察，不能原子地排除任意並發 rename；
同一持有 fd 才能確保路徑之後被換掉也不會寫進替代檔案。若寫入後名稱
改指別檔，回報失敗並明示原開啟映像可能已修改。寫入階段的一般錯誤保留
cause 並回報 `Failed`；取消亦釋放描述符，已寫的 durable state 留供診斷。
可安全續行的階段與晚期選單拒絕仍依既有 updater 規則，並非任意中斷都可恢復。

內部 `rollback_observed_image` 可讓同樣範圍的一般測試映像，在更新完成後切回上一個槽位。
首次寫入前重驗來源 binding 與目標，保留未完成更新、無前版與跨 Google build 的拒絕；
兩個 profile 的新裝、更新與來回切槽已在 Linux 小型映像通過整合測試，資料與舊槽保持不變。
這個入口沒有持久回退 journal，尚未接入實體區塊裝置或選單，也未證明斷電後可復原。

上述一般檔案入口是共用更新生命週期的合成整合；區塊裝置後端與限制見下節。

## 受觀察區塊裝置的保留資料更新後端（開發中）

內部 `apply_observed_block` 已接到既有 updater，但未接入共用維護系統的實際
准入流程。呼叫者須提供獨立可信的 catalog／profile 對應，以及完整的
`expected_target`：`rdev`、`syspath`、`nbytes`、`logical_sector_size`、
`identity_kind` 和 `device_identity`，全部須與重新觀察的事實相同。
`observe_block_target` 只提供唯讀事實，不代表已確認寫入。

後端只接受 512-byte 邏輯磁區的完整、已知 NVMe namespace 或可核對 kernel
backing 的 loop，拒絕一般 USB block 裝置、分割區、重複 NVMe namespace 身分、
來源／暫存與目標重疊，以及觀察到的掛載、swap、holder 或未知拓撲。
穩定身分摘要綁定 NVMe 的 WWID、serial、model 與 namespace ID，或 loop
backing 的檔案系統裝置編號及 inode；不是只靠裝置名稱或 GPT GUID。
自建 loop 的擁有權由測試控制，不能把 backing 核對當成 caller 授權。

整段操作持有 catalog、來源 binding、私有暫存與目標 `O_EXCL` 可寫描述符。
規劃使用有上限的目標 GPT／LP／vbmeta 擷取及封印 memfd ESP 快照；首次寫入
前重查目標身分、容量、拓撲、catalog，以及擷取的中繼資料與完整 ESP。
通過後才用同一目標 fd 寫入，並強制保留舊槽、使用者資料及前版開機回退。
更新狀態另綁定穩定裝置摘要：複製到另一個 backing inode 的 loop 不能沿用
中斷狀態；原裝置也只允許安全階段續跑，晚期選單狀態仍拒絕自動重試。

這些離散檢查不是原子快照或對所有特權寫入者的全域獨占；掛載只檢查目前與
PID 1 的 namespace。寫入後失敗可能已有部分資料變更。可信 caller、獨立的
機型到產物映射與實際維護准入尚未整合；沒有寫入 CLI 或任意 block override，
本節更新入口不執行全新安裝；獨立的新裝核心見下節，完整備份還原仍未接入。既有一般映像更新入口仍限制為 128 MiB。

`tests/stick/shared-block-operation-integration.py` 的 7 項 Linux 自建 loop 測試
已分別通過，涵蓋兩個合成 profile、寫前拒絕、ESP／拓撲變動、中斷續跑、
複製目標拒絕、晚期失敗及唯讀掛載。完整執行有 6 項通過，另 1 項在寫入前
因身分／拓撲觀察不一致而停止；同一來源及測試的單項重跑通過，並非單次
7/7。不一致的確切欄位未記錄、原因未重現，保守拒絕仍保留。
R5 映像維持原樣，未包含這個後端；實體 USB／NVMe、KANO／HX370 均未新增驗收。

## 內部全新安裝核心（開發中）

`install/gbos-fresh.py` 由 `install/stick/shared-source.py` 的內部
`install_observed_image`／`install_observed_block` 呼叫。前者只接受不超過
128 MiB 的明確合成一般檔案；後者沿用受觀察 block 的來源、目標身分、
拓撲及獨占描述符檢查。兩者持有來源 binding 與目標，寫前重查，寫後讀回。
caller 必須另行確認破壞性操作及目標，並從獨立可信管道確認來源是所選
profile 的原始產物；catalog 雜湊及來源格式檢查不能證明它不含使用者資料。
沒有新裝 CLI、公開媒體 writer 或共用維護確認／備份流程接到這些介面。

新裝重新產生磁碟及所有分割區 GUID，複製來源的非 userdata 分割區，
只將 userdata 開頭 16 MiB 清零並讀回，將 GPT userdata 範圍延伸至目標
可用末端。這不表示檔案系統已增長或格式化；後續由 Android 首次開機
格式化，仍需實機驗證。目標 ESP 的 bootconfig 綁定新 boot-a GUID，
使用內建 NVMe 的 slot-a 開機設定；共用來源及其維護預設不改寫。

來源的其他分割區或 ESP，以及目標舊資料空隙或 userdata 剩餘範圍，
仍可能含資料或憑證。這不是安全抹除，也不能保證來源位元組沒有憑證。
新裝沒有 durable 復原狀態、自動回退或原子安裝保證；寫入開始後的錯誤
可能留下部分修改及不可開機的目標。保留資料更新的續跑狀態不適用於新裝，
完整備份還原仍是另一條尚未整合的流程。

Linux 已通過 19 項新裝純邏輯、6 項實際小型一般檔案新裝整合，另有
9 項既有共用更新、21 項來源、9 項 block 規劃及 12 項 engine 測試，
合計 76 項通過、無跳過。新增的 5 項自建 loop 新裝測試尚未執行；
本輪特權測試啟動器的來源別名鎖定未通過審查，已停用，未執行特權測試。
以上不構成實體 USB／NVMe、KANO／HX370 的安裝、容量、開機或回復驗收；
可信機型到產物映射與維護准入仍待整合。

## 共用維護系統的虛擬 NVMe 操作測試

`tests/stick/shared-qemu-operation.py` 在非 root Linux 主機使用 TCG，啟動既有
共用診斷映像的測試副本。它只擷取維護分區以加入測試服務，再將變更區塊
寫入 qcow2 差異檔；不複製整張共用映像。原映像以唯讀描述符持有，原本的
EFI、核心、initramfs、開機項目及雙機型正式資料包不更動。這是有測試注入的
開機流程，不能稱為未修改交付映像的安裝驗收。

另附的唯讀測試資料碟攜帶兩組小型合成 v1/v2 來源與目前後端程式。
宿主透過獨立 serial 控制通道提供 catalog 雜湊及機型到 fixture 的映射。
客體讀取自己的實際 sysfs DMI，分別執行 KANO、HX370 的
`install_observed_block` 與 `apply_observed_block`。維護系統原本禁止 NVMe
自動載入；測試控制器先核對唯讀掛載、控制資料及唯一的虛擬 PCI 控制器，
才明確載入 NVMe 驅動，之後仍核對 namespace、型號及合成序號。
它不改動正式的自動載入保護，也不產生實體寫入授權。
測試服務等待正常維護前置服務，並在觀察目標之前停止客體的互動式序列登入。
實測發現序列登入服務會延後建立憑證掛載，觸發完整 mountinfo 比對而在寫前拒絕；
測試環境已排除這個啟動競爭，後端的掛載及目標身分檢查仍保持原樣。

所有可寫磁碟都是新建的一般檔案。宿主持續持有目標建立時的描述符，
停止客體後才取 QMP 計數，並獨立讀回雙 GPT、GUID、userdata、開機項目、
保留分割區，以及 super 的兩份 geometry、六份 metadata 和兩槽邏輯內容。
錯誤 catalog 與未知 DMI 必須在指定的後端階段拒絕，且沒有 NVMe 寫入。
最後比對測試媒體的邏輯內容與原映像的完整雜湊。缺少結果或計數即失敗。

準備可讀的一般檔案來源、獨立核對的來源 SHA-256、OVMF code/vars，及尚未
存在的私人工作目錄。需要 QEMU、mtools、e2fsprogs 與 lz4；不需要宿主 root、
KVM、loop、網路或目錄分享。完整 QEMU 測試不在一般 CI 自動執行：

```sh
python3 tests/stick/shared-qemu-operation.py \
  --source SHARED_DIAGNOSTIC_IMAGE --source-sha256 EXPECTED_IMAGE_SHA256 \
  --work NEW_PRIVATE_WORK_DIRECTORY \
  --ovmf-code OVMF_CODE_FILE --ovmf-vars OVMF_VARS_FILE
```

預設執行原有六個直接呼叫後端的案例。若要測試尚未接入正式選單的
操作控制器，在上述命令最後加上 `--controller`：以兩組模擬 DMI 各執行
合成 v1 新裝與 v2 保留資料更新，另測一次新裝取消。客體經控制器呼叫
實際 observed-block 後端；目標仍是宿主新建的一般檔案與 TCG 虛擬 NVMe。
測試核對 QMP 計數、來源完整雜湊及目標 GPT、userdata 與 LP 內容；取消
不得寫入目標。這是另一組測試，不取代預設六案例。7 項新增及 26 項既有
相關測試已在 Linux 通過；控制器模式的五個完整 TCG 案例、測試媒體邏輯比對
及原始來源完整雜湊再核對也已通過。這些結果不代表實體裝置驗收。

輸出包含各案例的 serial/QEMU log、fixture 映射及成功時的 `result.json`。
每個已完成客體結果也會先保存停止後的 QMP 計數，再檢查通過條件；
serial 解析須等到完整換行紀錄，避免將分段傳輸中的內層 JSON 結尾當成整筆結果。
這只驗證虛擬 NVMe 上的合成來源操作，沒有安裝原共用媒體內的正式雙資料包，
也沒有驗證 Android 首次開機、實體容量、完整備份還原或失敗回復。
正式維護映像的安裝、更新與復原寫入仍未接入。

本輪重用已準備的測試差異檔及唯讀 fixture 媒體，執行相同的六個案例與
宿主讀回函式；CLI 準備階段的證據來自先前執行。KANO 與 HX370 模擬 DMI
各自新裝、保留資料更新均通過，QMP 分別記錄 173、47 次 NVMe 寫入；
錯誤 catalog 與未知 DMI 均由指定後端階段拒絕，NVMe 寫入為 0。
六個案例的測試 USB 寫入及 unmap 皆為 0，差異檔的最終邏輯內容比對及
原映像完整 SHA-256 再核對也通過。
相關 26 項純測試在 Mac 與 Linux 通過；此結果不涵蓋真實機型辨識與硬體操作。

## 更新器目標 ESP 描述符綁定

既有更新器對已開啟目標磁碟的 ESP 操作，透過 Linux `/proc/self/fd`
與明確傳給 mtools 子程序的描述符存取同一個已開啟 inode；路徑後續被
替換時不會轉向替代檔案。描述符只在各次 FAT 子程序中傳遞，必須在整個
操作期間保持開啟。這項改動不驗證內容不被其他程序修改，也不建立硬體
或裝置拓撲授權；共用來源寫入限制維持不變。目標磁碟開啟後會比對
開啟前後的類型、檔案身分及裝置編號；
初始化檢查失敗會關閉描述符。這可拒絕開啟交接時觀察到的目標替換，
但不凍結內容，也不擴大共用來源的寫入範圍。

`tests/update/fat-block-loop-test.py` 已在 Linux 的自建 8 MiB loop 裝置實測：
持有 `Disk` 的 `O_EXCL` 描述符時，mtools 經繼承 fd 寫入、讀回、列目錄、
查容量及刪除成功；磁碟同步、第二次獨占開啟遭拒、關閉後重新獨占開啟也通過。
同次測試確認未提供相符、仍開啟的 `SharedBlockOperation` 時，共用 `apply`
拒絕 block 目標且拒絕前後內容相同。
這只證明該 Linux loop 與 mtools 組合，不代表實體 USB／NVMe 或雙機更新驗收。

一般執行只跑 4 項清理回歸測試，跳過真實 loop；CI 也使用這條路徑。
真實 loop 需 root、`GBOS_TEST_BLOCK_LOOP=1`、指向既存 root 私有目錄的
`GBOS_TEST_LOOP_ROOT`，以及獨立核對的 `GBOS_TEST_UPDATE_SHA256`。
測試不接受裝置參數，只建立自己的 backing；以核心回報的 backing 檔案系統
裝置編號和 inode 核對後才解除 loop。配置中斷也會重新查找自己的配置；
無法確認或解除失敗時保留 backing 與原始錯誤，禁止直接刪除仍連接的映像。
執行者仍須核對測試程式本身，並在隔離的 mount namespace 與受限執行時間下執行。

## 觀察一般映像的唯讀更新規劃（開發中）

`plan_observed_image` 程式介面根據本機 DMI 規則選取唯一完整機型，從同一
已驗證清單 context 取得來源，並對明確指定的一般映像檔執行 NVMe 模式的
相容性與 ESP 空間規劃。它不接受 block 裝置，也不提供寫入授權；大型目標
不需整碟複製。規劃會在私人暫存空間擷取有上限的 GPT、LP 與 ESP，執行
時間及 mtools 輸出受限。這些分段擷取不是原子快照，檔案身分檢查也無法
排除所有並發變動；是否使用中、硬體相容性與啟動政策均未驗證。實體碟
規劃仍需來源與目標拓撲、掛載狀態與裝置身分檢查。

## 共用維護系統的 QEMU 診斷映像（開發中）

`install/stick/universal-stick.py` 將既有 `make-maint.sh assemble` 的維護根目錄與
核心、initramfs，搭配各自獨立的 KANO 和 HX370 資料映像，放進新的單一 GPT
檔案。它建立一個 ESP、維護根目錄與資料分割區，外層選單只載入診斷維護系統；
既有的單機型安裝流程不受影響。系統開機後顯示機型辨識及清單與資料的雜湊
核對結果。**安裝、更新與復原寫入尚未接入；這還不是可用的共用安裝碟。**

此原型只能在隔離的 QEMU 測試；共同維護核心、initramfs 和開機前保護尚未在
KANO 與 HX370 驗證，請勿把原型拿到實機開機。現階段規則僅辨識 KANO 和 HX370 的機型家族；HX370 的 Linux sysfs
欄位尚待實測。清單雜湊綁定資料內容，但不是簽章，也不代表
AVB、開機政策或硬體驗收通過。維護根目錄和資料預期以唯讀方式掛載；診斷
程式會檢查兩個掛載點是否唯讀。作業系統的其餘行為與硬體限制仍需獨立驗證。

在 Linux 的隔離建置環境中，準備由受信任輸入產生的維護 root / xbootldr、
經獨立核對的 x86-64 systemd-boot EFI 執行檔，以及通過 `catalog.py` 驗證、
同時包含兩個 profile 的資料目錄。這個原型需要 root 打包檔案擁有權，以及
`mke2fs`、`mkfs.vfat`、`mcopy`、`mmd`、`mtype` 和 `debugfs`；它不接觸任何
區塊裝置或呼叫 USB 寫入程式。以下雜湊應從已核對的獨立來源取得，不要把
隨資料附上的雜湊檔當成認證：

```sh
sudo python3 install/stick/universal-stick.py \
  --root MAINT_ROOT --xbootldr XBOOTLDR_DIR \
  --efi SYSTEMD_BOOT_EFI --efi-sha256 EXPECTED_EFI_SHA256 \
  --kernel-sha256 EXPECTED_KERNEL_SHA256 --initrd-sha256 EXPECTED_INITRD_SHA256 \
  --bundle BUNDLE_DIR --catalog-sha256 EXPECTED_CATALOG_SHA256 \
  --out NEW_DIAGNOSTIC_IMAGE
```

輸出只能是原本不存在的普通檔案，旁邊會留下 `.not-self-tested`。工具從實際
資料大小計算容量，`--max-bytes` 可指定上限；兩份各約 19 GB 的資料，加上
維護系統與檔案系統後已超過 32 GB。建置期間另需放置資料副本、分割區映像
與最終映像的暫存空間。完成後會讀回 GPT、各分割區和所需資料的雜湊；
這些離線核對不等於 QEMU 開機或實機驗收。


### 唯讀根目錄的開機修正候選

候選以 256 MiB、`nodev,nosuid` 的 tmpfs 掛載 `/var`，根目錄與資料
維持唯讀。客體內的 sysusers、tmpfiles（只準備 `/etc`）和 ldconfig
在複製出的暫存根目錄中先執行，tmpfiles 使用 `--boot` 處理僅開機時
適用的 `/etc` 規則；全部成功後才標記 `/etc` 已更新。映像會讀回核對
`locale.conf` 與 `vconsole.conf`。開機參數以 `systemd.gpt_auto=0` 停用
systemd GPT 自動分割區產生器。
只移除客體 `root.conf` 中已核對的 `z / 555 - - -` 規則，保留其餘行。
診斷必須檢查掛載、logind 與相關服務結果；不健康時以非零狀態結束。

離線準備在獨立的 mount、PID、network namespace 中執行。可信主機 helper
先進入暫存根目錄、降低 capabilities 並設定 no-new-privs，才執行客體工具。
客體使用新 PID namespace 的唯讀 proc，不掛入主機的 proc、sys 或 dev。
單一建置程序以 subreaper 回收全部自有後代後，才允許暫存目錄清理。
Linux 上已驗證合成程序的隔離、逾時／中斷回收及固定客體工具的離線準備；
600 秒隔離 QEMU 已驗證開機健康、抽樣 NVMe 寫入計數為零及測試前後目標雜湊一致；映像輸入雜湊也保持一致。此結果只涵蓋該次模擬，不能證明來源永遠不可變，硬體支援與安裝能力仍須分別驗證。


### 唯讀的清單選擇預覽

診斷只在機型規則恰有一個完整符合、且沒有資料不完整或未設定的競爭規則時，
從同一次已驗證的 catalog 取出該機型的 profile、build 與預期開機項目。未知、
無法讀取、不完整、模糊或有未設定競爭規則時不選擇；選中機型缺少資料時拒絕。
此結果只是清單中繼資料，不檢查實機支援、目標磁碟或實際開機項目。安裝、
更新與復原各自保持停用；未知 QEMU 機型本身不代表系統健康檢查失敗。

## 實驗性唯讀區塊裝置規劃介面

`plan_observed_block` 是內部 Python 介面，接受明確的完整區塊裝置並以
唯讀描述符執行規劃，不提供 CLI 或寫入權限。它只接受唯一完整的機型規則；
拒絕分割區、來源或暫存空間與目標重疊、未知拓撲、觀察到的掛載、swap
及 holder。規劃前後核對裝置路徑、sysfs、容量與相關拓撲；loop 裝置也須
能解析其 backing file。暫存根目錄須能在建立暫存檔之前解析來源。

這些檢查只涵蓋目前及 PID 1 的 mount namespace，並非全系統獨占；分段
擷取也不是原子快照。結果不表示來源不可變、硬體或開機政策已驗證，更不
授權安裝、更新或復原。loop 合成測試需要明確設定 `GBOS_TEST_BLOCK_LOOP=1`
並使用 root；它只建立與移除自己的暫存 loop，仍不構成實機驗收。

### Optional build records

The diagnostic builder accepts `--kano-build-record`, `--kano-build-record-sha256`,
`--hx370-build-record`, and `--hx370-build-record-sha256` together. Independently
review and pin each record digest. The records must match the validated catalog images,
build IDs, and packaged profiles, and report passed QEMU self-tests. Checked bytes and
pins are included in the diagnostic root. With no record options, legacy diagnostic
builds remain available. This is an unsigned builder self-report with incomplete input
coverage; pristine state is NOT_PROVEN. It is not authentication or permission to write.
Existing images cannot acquire historical build proof through this record. Hardware
remains NOT_VERIFIED and all installation writes remain disabled.
