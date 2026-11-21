# Ente Desktop 向けの未アップロード写真の検出・準備

`ente-upload-prep.py` は任意の画像・動画ディレクトリを調査し，現在の Ente Photos に
存在しないメディアの候補を一覧化します．必要なものを**手動で** Ente Desktop に取り込む
ためのディレクトリも準備できます．**Ente へのアップロード・削除・変更は一切行いません．**

この機能は既存の `ente-camera-sync-v04.py` および Home Manager の自動バックアップとは独立しています．
Camera バックアップ用 `state-v04.sqlite3` は**読み取り専用**で参照します．

Ubuntu / Python 3.10 以上 / Ente CLI v0.4.5 を想定しています．外部 Python パッケージは不要です．

## 1. 未アップロード候補の一覧

Ente CLI にログイン済みであることを確認し，利用するアカウント名を指定してください．

```zsh
ente account list
export ENTE_ACCOUNT='表示されたアカウント名'

# ~/Pictures 以下の画像・動画を再帰的に調査
python3 ente-upload-prep.py --account "$ENTE_ACCOUNT" local-only ~/Pictures

# 全ファイルの詳細を CSV に保存（既存レポートは上書きしない）
python3 ente-upload-prep.py --account "$ENTE_ACCOUNT" \
  local-only ~/Pictures \
  --format csv --output ~/develop/backup/ente-local-only.csv

# JSON 形式で保存
python3 ente-upload-prep.py --account "$ENTE_ACCOUNT" \
  local-only ~/Pictures --format json --output ~/develop/backup/ente-local-only.json
```

すでに取り込んだ `Camera` のハッシュは既存 SQLite から再利用できます．それ以外のファイルは
初回に SHA-256 と BLAKE2b-512 を計算し，その後はサイズ・更新時刻・ctime・inode が
変わらない限り，新しい専用 SQLite のハッシュを利用します．必要なら `--rehash` で再計算できます．

`--exclude 'Screenshots/**'` を複数回指定して対象を絞り込めます．隠しディレクトリ，
隠しファイル，シンボリックリンク，メディア以外の拡張子は標準では走査しません．

### 判定結果

| `status` | 意味 | ステージング対象 |
| --- | --- | --- |
| `uploaded` | 現在 Ente に同じ内容・同じファイル名がある | いいえ |
| `uploaded_renamed` | 同じ内容があるがファイル名が違う | いいえ |
| `previously_seen` | 過去に Ente に存在した記録があるが現在の一覧にはない | いいえ |
| `local_only` | 現在・過去に同一ハッシュを確認できない | はい |
| `local_duplicate` | ローカルで同じ内容を持つ別の代表ファイルがある | いいえ |
| `needs_review` | Live Photo のペアが曖昧，または部分一致している | いいえ |

判定は **BLAKE2b-512 の内容一致**に基づきます．再エンコードした画像，編集した画像は
別内容と判定されます．また，`local_only` は「このツールが確認できた Ente メタデータに
同じハッシュがない」ことを示し，過去に一度もアップロードされていないことの証明ではありません．

### Ente の削除履歴について

過去に CLI や Camera 同期 DB で確認された Ente メディアについて，ファイル ID・
ハッシュ・名前・最終確認日時を **永続的に** `ente_history` に保存します．
以後 Ente 上から見えなくなっても履歴を消さず，`previously_seen` として再アップロード候補から除外します．

**Ente CLI v0.4.5 ではゴミ箱内の項目を直接一覧化できないため，`previously_seen` は
「ゴミ箱にある」「完全削除された」ことを区別できません．** 初回の記録より前に
削除された項目も検出できません．共有解除やアクセス権の変更でも同様に消える可能性があります．

Ente の最新一覧の取得に失敗した場合，結果を確定しません．リストが以前の半数未満に減少した
場合は既定で処理を中止します．大量削除を意図的に行ったことを確認できた場合に限り
`--allow-large-drop` を指定できます．ライブラリが空になる場合は `--allow-empty-remote`
で明示的に承認する必要があります．

## 2. Desktop 用ディレクトリの準備

```zsh
# ステージングを ~/Pictures の外に用意する
mkdir -p ~/share/ente-upload

# 事前確認：ファイル数・容量の上限を指定
python3 ente-upload-prep.py --account "$ENTE_ACCOUNT" \
  prepare-upload ~/Pictures \
  --max-files 300 --max-bytes 5GiB \
  --output ~/share/ente-upload/batch-001

# 確認後に明示的に作成
python3 ente-upload-prep.py --account "$ENTE_ACCOUNT" \
  prepare-upload ~/Pictures \
  --max-files 300 --max-bytes 5GiB \
  --output ~/share/ente-upload/batch-001 --apply
```

ステージング先は**新しいディレクトリでなければなりません**．上書きも自動削除もしません．
現在の対象とステージング済みのバッチに含まれる内容を照合し，対象候補を決定します．
すでに準備したものを再度準備したい場合に限り `--include-staged` を明示します．

出力例：

```text
~/share/ente-upload/batch-001/
├── manifest.json
├── manifest.csv
└── media/
    ├── Camera/2020/01/IMG_1234.jpg
    └── OldPhotos/2015/IMG_5678.heic
```

`media/` 以下は**指定元からの相対ディレクトリ構造とファイル名を維持**します．

- Linux `FICLONE` による Reflink を試し，使用できなければ通常 Copy に切り替えます．
  **Hardlink は使用しません**．
- ステージングした内容を元データの SHA-256 と BLAKE2b-512 に照合し，変更があれば中止します．
- Live Photo と推定した `.HEIC` / `.JPG` + `.MOV` のペアは分割せず，同じ場所に配置します．
  ペアが曖昧・部分一致の場合は自動ステージングしません．
- ステージング先が入力ディレクトリの内側の場合は拒否します．
- 書き込みエラーなどで途中停止した場合，作成途中のディレクトリは**自動削除しません**．
  `manifest.json` の存在を確認し，必要なら手動で状態を確認してください．

## 3. Ente Desktop からアップロード

Ente Desktop で `batch-001/media` を選択して手動アップロードします．
`batch-001` 全体を選択すると `manifest.json` と `manifest.csv` まで含まれる可能性があるため，
**必ず `media` だけを選択**してください．アルバム構成は Desktop 側で確認してください．

**Watch Folder には登録しないでください．** ステージングを整理した際に Ente 側の
アルバム所属に影響するおそれがあります．

## 4. アップロード後の検証

```zsh
python3 ente-upload-prep.py --account "$ENTE_ACCOUNT" \
  verify-upload ~/share/ente-upload/batch-001/manifest.json
```

`verified` は，同じ内容が現在 Ente の一覧にあり，ファイル名が一致することを示します．
Live Photo は静止画と動画の両ハッシュを持つ1件の Live Photo 項目との一致で確認します．
`content_present_other_name` や `needs_review` は個別に確認してください．
この検証は **Ente に内容が現在存在すること**を示すもので，今回の操作でアップロードされた
ことや自分の所有するアルバムにあることの証明ではありません（共有ファイルも一覧に含まれ得ます）．

ステージングは検証後も削除しません．不要になればユーザーが手動で削除してください．

## 5. 保存データと安全性

```text
~/.local/state/ente-camera-sync/
├── state-v04.sqlite3           # 既存 Camera バックアップ用（読み取り専用参照）
└── upload-preparation.sqlite3  # 今回のスキャンキャッシュ・Ente 履歴・バッチ
```

写真・動画の実体は専用 SQLite に保存しません．パス・ハッシュ・Ente ファイル ID と
ステージング履歴だけを保存します．アカウントを混同しないよう DB 内に固定します．

本ツールが触る Ente 側 API は **ファイル一覧取得のみ**です．
`ente-camera-sync-v04.py` の自動実行，ダウンロード先，既存 SQLite スキーマを変更しません．

## テスト

```zsh
python3 -m unittest discover -s tests -v
```

オフラインの模擬 Ente 一覧を使い，削除履歴，重複，ステージング，Reflink 失敗時のコピー，
Live Photo，誤操作の抑止をテストします．**Ente Desktop への実アップロードや実際の
Live Photo のペア認識は，まだ検証していません．**
