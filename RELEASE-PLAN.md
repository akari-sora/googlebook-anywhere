# Googlebook Anywhere v0.3.0 發行計畫

日期：2026-10-29。本文件是後續操作計畫，這次只準備文件與本機測試，沒有執行下列遠端／硬體步驟。現在的發行說明維持 `unreleased`；有阻擋項目就停在發布之前。標為「公開／不可逆」的操作會讓內容離開本機，不能靠刪除 ref 收回已被下載的內容。

## 版號、來源與公開差異

使用 `v0.3.0`。`RELEASE-NOTES.md`、rc2 合併紀錄與 IFMA 主線都已採用這個版號；這是跨 CPU、顯示、更新與 EC 功能的下一個 minor release。`check-updates.py` 以數值 tuple 比較 `vX.Y.Z`，所以 `v0.3.0` 大於 `v0.2.3`；不是用字串排序，也不把 rc 分支名當正式 tag。

私有基準 `dd647e8` 比私有 main 多 100 個 commit。公開 main `03c3359` 的 `PUBLIC-EXPORT` 指向私有 `f47b488`，它已是此基準的祖先；共用檔案在該匯出來源逐一比對相同。公開 `206978b` 的標頭／libdw-dev 修正、`33edf46` 的 USB userdata 擴大修正已由私有 `8f8d66a` 保留，`f613e58` 另修正 keep-data 預檢。唯一公開獨有路徑是產生的 `tools/release/PUBLIC-EXPORT`，下次匯出會重建，不能複製回私有來源。依本機快照不需要再同步公開修正；發布當天若遠端有新增 commit，要重新比較。

## 1. 完成證據與本機候選（可逆、本機）

[v0.3.0 驗收紀錄](docs/VERIFICATION-v0.3.0.md) 不得有待填項目，每一項都要寫明實機已驗證或未驗證（未驗證項目只能以已知限制發布，drain 維持實驗選項），範圍包含內建碟 keep-data 到 16658739、使用者確認 App／帳號／設定保留、`update_engine` 保持停止、螢幕不再自行亮起、真實電源鍵與硬體 S0ix。休眠失敗復原可能重設 AP；遵守 [安全說明](docs/SAFETY.md)，不能只憑安裝中、QEMU 或軟體 gate 宣稱通過。啟用 drain 前仍須核對實際模組原始碼與相依、EC／AML 版本，先在可丟棄資料的 USB 上驗證，不以正在進行的內建碟安裝代替這些先決條件。本次不執行硬體操作。

下面命令從私有 repo 根目錄開始，`../public` 為獨立公開 clone；變數只指向操作者選定的工作目錄，不寫進公開證據。不要把計畫整段直接貼進終端，應逐步讀回結果。

```sh
set -eu
V=v0.3.0
PRIVATE_DIR=$(pwd -P)
PUBLIC_DIR=$(cd ../public && pwd -P)
WORK="$PRIVATE_DIR/out/release-v030"
EXPORT="$WORK/public-tree"
mkdir -p "$WORK"
test "$(git branch --show-current)" = release/next
test -z "$(git status --porcelain)"
git merge-base --is-ancestor dd647e8 HEAD
git diff --stat main...HEAD
```

確認下列不是新建置：依本次工作封包 `EVIDENCE.md`，候選來自產品 `fee74fe`（`fix/apex-zipalign`，基於 `dd647e8`），以既有簽章身分、`GBOS_LAPIS_BUILD=16658739 GBOS_EC=drain` 及僅實驗室使用的 `GBOS_ADB_PUBKEY` 建置；後續 `aa33bd2`／`d7f26c0` 只改 alignment gate，不改映像位元組。交付候選大小 `19069493760` bytes，SHA-256 `f17069da8659d400ae636bfb25e224aaced9a146539157639bf657a02ddac62c`，fingerprint `ASUS/lapis/lapis:17/CL3B.260622.276.R4/16658739:user/release-keys`。可沿用它作驗收候選，前提是實際檔案、建置來源／參數、公開 AVB／APEX 指紋、完整 build／QEMU 紀錄都核對，且後續只改文件。示例只讀檢查：

```sh
: "${CANDIDATE_IMAGE:?set to the existing candidate image}"
python3 - "$CANDIDATE_IMAGE" <<'PY'
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1])
assert p.stat().st_size == 19069493760
h = hashlib.sha256()
with p.open('rb') as f:
    for b in iter(lambda: f.read(4 * 1024 * 1024), b''):
        h.update(b)
assert h.hexdigest() == 'f17069da8659d400ae636bfb25e224aaced9a146539157639bf657a02ddac62c'
print('candidate size and sha256: PASS')
PY
```

不能只靠雜湊推出來源、fingerprint 或簽章正確，還要核對已有建置紀錄與更新前後公開指紋；不得印出或複製私鑰。若產品程式碼、pins、profile、金鑰身分或建置參數有變，這個候選不可沿用，應在有空間的 Linux 建置環境另建映像並重做驗收。必要時的建置命令如下，這次禁止執行：

```sh
: "${BUILD_WORK:?existing build workspace}"
: "${EXISTING_AVB_KEY:?existing signing identity}"
: "${EXISTING_APEX_KEYS_DIR:?existing signing identity}"
GBOS_LAPIS_BUILD=16658739 GBOS_EC=drain \
  GBOS_APEX_KEYS_DIR="$EXISTING_APEX_KEYS_DIR" \
  install/install.sh --work "$BUILD_WORK" --key "$EXISTING_AVB_KEY" --update
```

16658739 和 drain 都是選用，普通建置仍為 16452207／封鎖 EC。`make-release.sh` 只封裝腳本，完全不建 OS image，因此不需要為了發封存包重建上述 19 GB 映像。公開映像沒有託管位置：專案不散布 Google 磁碟／韌體映像；使用者自行建置。GitLab Generic Package Registry 託管的是 installer tar.gz 與 checksum。`tools/release/published-images.sha256` 只釘選三張公開 PNG。

## 2. 私有 MR 與合併（遠端寫入；main 合併不可逆）

此節要等另一次明確允許遠端操作後才執行。先更新遠端快照、比對兩邊是否新增修正；不要直接把私有 git history 推到公開 repo。

```sh
git fetch origin
git -C "$PUBLIC_DIR" fetch origin
git log --oneline origin/main..HEAD
git -C "$PUBLIC_DIR" log --oneline main..origin/main
test "$(git -C "$PUBLIC_DIR" rev-parse origin/main)" = "$(git -C "$PUBLIC_DIR" rev-parse 03c3359)"
git merge-base --is-ancestor origin/main HEAD
```

若非快轉／main 有新增內容，先整合並重跑 gates，不能跳過。公開 origin/main 與比對基準不同時，上述檢查會停下，先重新逐檔比對並同步公開新增修正，再更新已審核的基準。補齊證據、確認所有 blocker 解決後，才在私有 `release/next` 把 notes 第 3 行改成 `released`，刪掉導言「這一版還沒有發布。」（標題已是精確 `# Googlebook Anywhere v0.3.0`）。不要再搬一次 v0.2.3；它已在 history 頂端且只刪原第 3 行。發布狀態翻轉前，逐項審核新的真實證據是否正面通過；僅刪掉 TODO 不算驗收。依證據同步更新 README、STATUS-KANO、UPGRADE、COMPATIBILITY、TROUBLESHOOTING、驗收紀錄與 notes 裡的目前狀態（含本文件及驗收紀錄的 unreleased 標籤與安裝中／待驗證敘述），仍未證明的硬體限制照實保留，不得一併改成支援。

```sh
python3 - <<'PY'
from pathlib import Path
p = Path('docs/RELEASE-NOTES.md')
s = p.read_text()
assert s.splitlines()[:3] == ['# Googlebook Anywhere v0.3.0', '', '<!-- release-status: unreleased -->']
assert not any('TODO-KANO-EVIDENCE' in p.read_text() for p in Path('docs').glob('VERIFICATION-v0.3.0.md'))
s = s.replace('<!-- release-status: unreleased -->', '<!-- release-status: released -->', 1)
s = s.replace('這一版還沒有發布。', '', 1)
p.write_text(s.rstrip() + '\n')
PY
git diff --check
python3 -I tests/stick/release-notes-gate.py
git diff -- README.md README.en.md docs
git add README.md README.en.md docs RELEASE-PLAN.md
git commit -m "docs(release): finalize v0.3.0 evidence and notes"
git push -u origin release/next
glab mr create --source-branch release/next --target-branch main \
  --title "release: prepare v0.3.0" --squash-before-merge=false \
  --description "$(cat RELEASE-PLAN.md)"
glab ci status --branch release/next
```

讀回 MR 的變更範圍、review、最新 SHA 與五個 test jobs（lint、builder、stick、install-tools、harness），全部通過才合併。`GBOS_REQUIRE_PRIVATE_SCAN` 必須在私有專案 CI 設為非 protected 的有效值；公開專案不設。若只在 Mac 通過不能代替 Linux CI。

```sh
: "${MR_IID:?reviewed private MR number}"
glab mr view "$MR_IID"
REVIEWED_SHA=$(git rev-parse HEAD)
glab mr merge "$MR_IID" --sha "$REVIEWED_SHA" --squash=false --auto-merge=false
git fetch origin
git show --stat origin/main
SOURCE=$(git rev-parse origin/main)
git merge-base --is-ancestor dd647e8 "$SOURCE"
```

私有 main 合併後還要讀回合併 SHA 的 pipeline；不可把分支較舊 pipeline 當成合併後驗證。需要保留 100 個來源 commit 時不要選 squash；先確認專案允許 merge commit 或 fast-forward 且沒有強制 squash，不能只靠 CLI 假設合併策略。此時不在私有 repo 打 release tag，因為同一份 CI 的 tag jobs 會發布產物。

## 3. 公開匯出與本機審查（可逆、本機）

```sh
test ! -e "$EXPORT"
bash tools/release/make-public.sh "$SOURCE" "$EXPORT"
test "$(cat "$EXPORT/tools/release/PUBLIC-EXPORT")" = "$SOURCE"
test ! -e "$EXPORT/research"
test ! -e "$EXPORT/tools/harness/kano-env.local.sh"
test ! -e "$EXPORT/tools/release/private-patterns.txt"
```

`make-public.sh` 從 commit 匯出，檢查 private-patterns、檔名、普通檔案與 PNG pins；乾淨結果只適用匯出樹，不證明任何磁碟映像沒有個資。執行前確認既有公開 clone 沒有 staged／untracked／ignored 個人檔；以下 `rsync --delete` 只能用在這個已核對的公開 clone，保留 `.git`。

```sh
test -z "$(git -C "$PUBLIC_DIR" status --porcelain --ignored)"
git -C "$PUBLIC_DIR" switch main
git -C "$PUBLIC_DIR" merge --ff-only origin/main
rsync -ani --delete --exclude=/.git/ "$EXPORT/" "$PUBLIC_DIR/"
```

先審核 dry-run 的所有刪除／覆寫，確認沒有尚未同步的公開修正與個人檔，再套用：

```sh
rsync -ai --delete --exclude=/.git/ "$EXPORT/" "$PUBLIC_DIR/"
git -C "$PUBLIC_DIR" add -A
git -C "$PUBLIC_DIR" diff --cached --check
git -C "$PUBLIC_DIR" diff --cached --stat
git -C "$PUBLIC_DIR" diff --cached
git -C "$PUBLIC_DIR" var GIT_AUTHOR_IDENT
git -C "$PUBLIC_DIR" var GIT_COMMITTER_IDENT
```

先核對 staged diff 與已核准的公開身分，再提交；不要把私人信箱放進公開 commit：

```sh
git -C "$PUBLIC_DIR" commit -m "release: Googlebook Anywhere v0.3.0"
PUBLIC_SHA=$(git -C "$PUBLIC_DIR" rev-parse HEAD)
```

逐項看新增／刪除檔案與 marker，確認保留兩個公開安裝修正，沒有 private-patterns、research、個人設定、私鑰或系統映像。作者／committer 也須使用已核准的公開身分，不能把私人信箱帶進新公開 commit。

在最終私有 commit 與公開 commit 各跑 CI 的全部本機指令（以下從相應樹根目錄執行；輸出放 gitignored／session scratch，合成碟只用空目錄）：

```sh
shellcheck -S warning $(git ls-files '*.sh' ':!research/')
git ls-files -z '*.py' ':!research/' | xargs -0 python3 -m py_compile
bash tests/builder/run.sh
python3 tests/probe/probe-test.py
bash tests/report/report-test.sh
python3 tests/stick/selftest.py "$(mktemp -d)/w"
python3 tests/update/selftest.py "$(mktemp -d)/w"
python3 tests/update/mutate.py -j 2
python3 tests/update/check-updates-test.py
python3 -I tests/stick/run_make_maint_test.py
python3 -I tests/stick/release-notes-gate.py
python3 tests/stick/docs-story-test.py
python3 tests/stick/ci-maint-test.py
bash tests/install/run.sh
bash tests/install/install-sh-test.sh
bash tests/install/kano-check-test.sh
bash tests/harness/run.sh
```

公開匯出檢查要分開執行：

```sh
(cd "$PRIVATE_DIR" && GBOS_REQUIRE_PRIVATE_SCAN=1 python3 -I tests/stick/public-export-test.py)
(cd "$PUBLIC_DIR" && env -u GBOS_REQUIRE_PRIVATE_SCAN python3 -I tests/stick/public-export-test.py)
```

公開樹讀回它因有效 marker 而 SKIP，這個 SKIP 不是重新掃描。私有來源要強制 scan。Python bytecode 請設 `PYTHONPYCACHEPREFIX` 到 scratch；Linux CI 需使用普通使用者，root 與無 loop 裝置的 skip 要分別記錄。缺少 mtools 或其他工具、Linux-only fail、任何 unexpected skip 都不能標 PASS。另核對修改文件的相對連結存在、`git diff --check` 與 private-pattern scan；本輪不進行外網連結查詢。

正式 notes 的真實 package gate 也要在公開樹重播（這不是會改 notes 的暫存模擬）：

```sh
python3 - "$V" <<'PY'
import os, subprocess, sys
from pathlib import Path
lines = Path('.gitlab-ci.yml').read_text().splitlines()
start = lines.index('package:')
command = next(line[6:] for line in lines[start:] if line.startswith('    - if test ! -f docs/RELEASE-NOTES.md;'))
raise SystemExit(subprocess.run(['bash', '-c', command], env=dict(os.environ, CI_COMMIT_TAG=sys.argv[1])).returncode)
PY
```

## 4. 公開主線 push（公開／不可逆）

GitLab 是 canonical，GitHub 是鏡像。不要 push 私有 branch／refs 或 force push。先讀回 remote URL 指向既定公開專案，接著精確推單一 SHA 到 main：

```sh
git -C "$PUBLIC_DIR" remote -v
git -C "$PUBLIC_DIR" push https://gitlab.com/aa22396584/googlebook-anywhere.git "$PUBLIC_SHA:refs/heads/main"
git -C "$PUBLIC_DIR" ls-remote https://gitlab.com/aa22396584/googlebook-anywhere.git refs/heads/main
git -C "$PUBLIC_DIR" push https://github.com/akari-sora/googlebook-anywhere.git "$PUBLIC_SHA:refs/heads/main"
git -C "$PUBLIC_DIR" ls-remote https://github.com/akari-sora/googlebook-anywhere.git refs/heads/main
```

兩邊 main 必須讀回同一個 `PUBLIC_SHA`。執行 `(cd "$PUBLIC_DIR" && glab ci status --branch main)`，再檢視各 job；五項測試全部 success，才往下打 tag。若鏡像 push 或公開測試失敗，保留失敗證據，先修正，不能提早宣布 release。

## 5. 公開 tag、封裝與 release（公開／不可逆）

```sh
test "$(git -C "$PUBLIC_DIR" rev-parse HEAD)" = "$PUBLIC_SHA"
git -C "$PUBLIC_DIR" tag -a "$V" "$PUBLIC_SHA" -m "Googlebook Anywhere $V"
git -C "$PUBLIC_DIR" rev-parse "$V^{}"
git -C "$PUBLIC_DIR" push https://gitlab.com/aa22396584/googlebook-anywhere.git "refs/tags/$V"
git -C "$PUBLIC_DIR" ls-remote https://gitlab.com/aa22396584/googlebook-anywhere.git "refs/tags/$V" "refs/tags/$V^{}"
```

tag 必須解參照到 `PUBLIC_SHA`，不是私有 `SOURCE`。GitLab tag pipeline 的五項測試先通過，package job 再驗 notes，執行 `bash tools/release/make-release.sh "$CI_COMMIT_TAG" "$CI_COMMIT_SHA"`。它從 committed source 製作 `googlebook-anywhere-v0.3.0.tar.gz` 與 `.tar.gz.sha256`，拒絕同版本覆寫，沒有完整映像建置。package job 自動上傳到 Generic Package Registry 的 `installer/v0.3.0/`；release job 依賴 package，發布 notes 全文再加同 tag 的 history 絕對連結。

讀回 tag pipeline 七項 jobs 全數 success 後，再推同一個本機 tag 到鏡像，不另建不同 tag：

```sh
git -C "$PUBLIC_DIR" push https://github.com/akari-sora/googlebook-anywhere.git "refs/tags/$V"
git -C "$PUBLIC_DIR" ls-remote https://github.com/akari-sora/googlebook-anywhere.git "refs/tags/$V" "refs/tags/$V^{}"
```

GitHub 只有 ref 鏡像，不另外建立 GitHub release。失敗時不要刪 tag／重寫 tag 或覆寫已發布同版本檔案；先保存 job 與檔案證據再決定修正版。

## 6. 發布後讀回（唯讀、外網）

```sh
V=v0.3.0
API=https://gitlab.com/api/v4
PROJECT=aa22396584%2Fgooglebook-anywhere
mkdir -p "$WORK/download"
curl --fail --silent --show-error "$API/projects/$PROJECT/releases/$V" -o "$WORK/release.json"
curl --fail --location "$API/projects/$PROJECT/packages/generic/installer/$V/googlebook-anywhere-$V.tar.gz" -o "$WORK/download/googlebook-anywhere-$V.tar.gz"
curl --fail --location "$API/projects/$PROJECT/packages/generic/installer/$V/googlebook-anywhere-$V.tar.gz.sha256" -o "$WORK/download/googlebook-anywhere-$V.tar.gz.sha256"
(cd "$WORK/download" && shasum -a 256 -c "googlebook-anywhere-$V.tar.gz.sha256")
gzip -t "$WORK/download/googlebook-anywhere-$V.tar.gz"
tar -tzf "$WORK/download/googlebook-anywhere-$V.tar.gz" > "$WORK/archive-entries.txt"
git -C "$PUBLIC_DIR" show "$V:docs/RELEASE-NOTES.md" > "$WORK/expected-description.md"
printf '\n[Full release history](https://gitlab.com/aa22396584/googlebook-anywhere/-/blob/%s/docs/RELEASE-HISTORY.md)\n' "$V" >> "$WORK/expected-description.md"
python3 - "$WORK/release.json" "$WORK/expected-description.md" <<'PY'
import json, pathlib, sys
r = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert r['tag_name'] == 'v0.3.0'
assert r['description'] == pathlib.Path(sys.argv[2]).read_text()
assert len(r['assets']['links']) == 2
print('release tag, description and asset count: PASS')
PY
(cd "$PUBLIC_DIR" && GBOS_RELEASE_OUT="$WORK/local-package" bash tools/release/make-release.sh "$V" "$PUBLIC_SHA")
gzip -dc "$WORK/download/googlebook-anywhere-$V.tar.gz" > "$WORK/published.tar"
gzip -dc "$WORK/local-package/googlebook-anywhere-$V.tar.gz" > "$WORK/local.tar"
cmp "$WORK/published.tar" "$WORK/local.tar"
```

同時讀回 release asset 的兩個 URL，確認指向上述 canonical registry，並逐項審核 tar 成員只含普通檔案／目錄、已釘選 PNG，沒有磁碟映像、韌體、金鑰或私有筆記；核對封存包 notes 與 tag 完全一致。macOS 與 CI gzip bytes 可能不同，published checksum 要驗 published bytes，重現比較用解壓後 tar bytes 與 metadata，不能混用兩份 checksum。這是額外重現檢查；若 Git／tar 版本或環境造成 tar 不同，保留差異，逐項比較成員內容與 metadata，不能單憑跨平台 tar byte 不同就斷言來源不同。最後記錄私有來源 SHA、公開 SHA、tag 物件／目標、七項 job 結果、下載 checksum、description 與鏡像 refs 的讀回結果；清理已驗證的暫存副本，保留小型證據。

任何 KANO 待填項目、Linux gate 未跑完／失敗、個資命中、簽章身分不一致、候選來源不可追溯、公開分支有遺漏修正，都必須在公開 push／tag 之前解決。本次停在本機準備、提交與報告。
