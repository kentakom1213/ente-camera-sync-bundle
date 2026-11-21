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
| `uploaded` | 現在 Ente に同じ内容がある | いいえ |
| `previously_seen` | 過去に Ente に存在した記録があるが現在の一覧にはない | いいえ |
| `local_only` | 現在・過去に同一ハッシュを確認できない | はい（重複以外） |
| `needs_review` | Live Photo のペアが曖昧，または部分一致している | いいえ |

ファイル名だけが違う `uploaded` は `name_mismatch=true` として報告します．
ローカルの同一内容の重複は `local_only` のまま `duplicate_of` に代表ファイルのパスを保持します．
ステージングするのは `duplicate_of` が空の候補だけです．

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
# ステージングは ~/Pictures の外の新しいディレクトリに作成
mkdir -p ~/share/ente-upload

# 元の階層を維持して，対象ファイルすべての計画を確認
python3 ente-upload-prep.py --account "$ENTE_ACCOUNT" \
  prepare-upload ~/Pictures \
  --output ~/share/ente-upload/batch-001 --dry-run

# 必要なら階層をなくしてフラットに配置
python3 ente-upload-prep.py --account "$ENTE_ACCOUNT" \
  prepare-upload ~/Pictures --flatten \
  --output ~/share/ente-upload/batch-001 --dry-run

# 計画を確認したら，実際に作成
python3 ente-upload-prep.py --account "$ENTE_ACCOUNT" \
  prepare-upload ~/Pictures --flatten \
  --output ~/share/ente-upload/batch-001
```

`prepare-upload` は通常，ステージングを作成します．`--dry-run` を指定した場合だけ事前確認になります．
`--dry-run` は対象件数・総容量・出力先の一部を表示し，ステージングと永続 SQLite を変更しません．
既存 SQLite があればメモリ上に複製して判定します．
Ente CLI 自身のメタデータキャッシュは更新される場合があります．
`--dry-run` を外すとステージングが実行されます．旧 `--apply` は使用できません．

**すべての未アップロード候補が対象**です．件数・容量による途中選択は行いません．
ステージング対象が大量になる場合は，コピーに必要な容量を確認し，
必要なら入力ディレクトリを分けて実行してください．

```text
~/share/ente-upload/batch-001/
├── manifest.json
└── media/
    ├── IMG_1234__a3f8c2d1.jpg
    ├── IMG_1234__b9a1ef03.jpg
    └── IMG_5678.heic
```

標準では `media/` 以下に入力元の相対ディレクトリ構造を保持します．
`--flatten` では `media/` 直下にすべて配置します．同名（大文字小文字・Unicode NFC の差を含む）が
重複するときは，**衝突する全ファイル**に内容由来の短い SHA-256 接尾辞を追加します．
接尾辞がさらに衝突すれば桁数を増やします．Live Photo の静止画と動画には同じ接尾辞を付けます．
元のパスと変更後の名前は `manifest.json` に保存し，`verify-upload` は実際に使用した名前で検証します．

- Linux `FICLONE` による Reflink を試し，利用できなければ通常 Copy に切り替えます．Hardlink は使用しません．
- ステージングした内容を元データの SHA-256 と BLAKE2b-512 に照合し，変更があれば中止します．
- Live Photo のペアが曖昧，または片方だけが Ente にある場合は自動ステージングしません．
- ステージングは新しいディレクトリに限り，既存のものを上書き・削除しません．
- ステージング先が入力ディレクトリの内側，または入力ディレクトリを包含する場合は拒否します．
- 途中で失敗した場合，作成途中のディレクトリは自動削除しません．内容と `manifest.json` の有無を手動確認してください．

複数回のステージングで同じ内容を選択することも可能です．
SQLite で作成済みバッチを追跡・自動除外する処理はありません．
既存 DB に古い `batches` / `batch_files` テーブルがあっても利用せず，データを勝手に削除しません．

## 3. Ente Desktop からアップロード

Ente Desktop で `batch-001/media` を選択して手動アップロードします．
`batch-001` 全体を選択すると `manifest.json` まで含まれる可能性があるため，
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
└── upload-preparation.sqlite3  # スキャンキャッシュ・Ente 履歴
```

写真・動画の実体は専用 SQLite に保存しません．パス・ハッシュ・Ente ファイル ID・
最終確認日時だけを保存します．アカウントを混同しないよう DB 内に固定します．

本ツールが触る Ente 側 API は **ファイル一覧取得のみ**です．
`ente-camera-sync-v04.py` の自動実行，ダウンロード先，既存 SQLite スキーマを変更しません．

## テスト

```zsh
python3 -m unittest discover -s tests -v
```

オフラインの模擬 Ente 一覧を使い，削除履歴，重複，ステージング，Reflink 失敗時のコピー，
Live Photo，誤操作の抑止をテストします．**Ente Desktop への実アップロードや実際の
Live Photo のペア認識は，まだ検証していません．**
