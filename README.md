# Ente Camera Sync

## 未アップロード画像の検出・Ente Desktop 向け準備

`ente-upload-prep.py` は，任意のディレクトリ（例：`~/Pictures` 全体）の画像・動画を走査し，Ente に存在しない内容を一覧化します．過去の Ente メタデータも保存して再アップロード候補から除外します．

```zsh
python3 ente-upload-prep.py --account "$ENTE_ACCOUNT" local-only ~/Pictures
python3 ente-upload-prep.py --account "$ENTE_ACCOUNT" \
  prepare-upload ~/Pictures --output ~/share/ente-upload/batch-001 --apply
python3 ente-upload-prep.py --account "$ENTE_ACCOUNT" \
  verify-upload ~/share/ente-upload/batch-001/manifest.json
```

ステージングは元のフォルダ構造を維持し，Reflink を優先，未対応なら Copy します．**Ente へのアップロード操作は実装しておらず，`media/` を Ente Desktop から手動で取り込みます．** 既存の Camera 同期 DB は読み取り専用です．

詳細と注意事項は [LOCAL-UPLOAD-PREPARATION.md](LOCAL-UPLOAD-PREPARATION.md) を参照してください．

## Ente CLI v0.4.5 に対応する差分ダウンロード（プレビュー）

従来のエクスポート方式とは別に，新しいSQLiteとEnte CLIの個別ダウンロードを使用する
追加専用のプレビュー実装を用意しています．**まだ自動実行には使用しません．**
初期移行と手動テストの手順は [V04-INCREMENTAL-SYNC.md](V04-INCREMENTAL-SYNC.md) を参照してください．

以下は従来のエクスポート方式の説明です．

Ente CLIで写真・動画をエクスポートし，`~/Pictures/Camera/YYYY/MM`へ
追加専用で整理するためのスクリプトです．

## 安全方針

- `--apply`なしではCamera側へ書き込みません．
- `--run-export`なしでは`ente export`を実行しません．
- 既存ファイルは上書きしません．
- SHA-256が同じファイルは追加しません．
- 同名で内容が異なる場合は`__ente_<file-id>`を付けます．
- コピーは同じディレクトリ内の一時ファイルへ行い，SHA-256検証後に確定します．
- EnteのJSONに日時がなければ`Camera/unknown-date`へ入れます．
- Ente上の削除はCamera側へ伝播しません．

注意: `ente export`自体は，Ente上で削除された項目をエクスポート用ミラーから
削除することがあります．正式な保存先であるCamera側はこのスクリプトが削除しません．

## ファイル種類

画像:

- jpg / jpeg / png / webp / heic / heif / avif
- gif / dng / tif / tiff / bmp

動画:

- mp4 / mov / m4v / 3gp / webm / mkv
- avi / mts / m2ts

未知の拡張子は，Ubuntuの`file`コマンドが`image/*`または`video/*`と
判定した場合だけ対象にします．JSON，サムネイル，ログなどは追加しません．

## インストール

```bash
unzip ente-camera-sync-bundle.zip
cd ente-camera-sync-bundle
./install.sh
```

インストール先:

```text
~/.local/bin/ente-camera-sync
~/.config/systemd/user/ente-camera-sync.service
~/.config/systemd/user/ente-camera-sync.timer
~/.local/state/ente-camera-sync/
```

## Ente CLIの準備

最初のログインは手動で行います．スクリプトから`pass`は読みません．

```bash
ente account list
ente account update \
  --email 'Enteに登録したメールアドレス' \
  --dir /data/ente-export
```

複数アカウントがある場合は，各アカウントに対して同じエクスポート先を設定します．

## 初回確認

まず，すでにエクスポート済みの内容だけをドライランします．

```bash
~/.local/bin/ente-camera-sync \
  --export-root /data/ente-export \
  --camera-root "$HOME/Pictures/Camera"
```

出力を確認後，コピーを実行します．

```bash
~/.local/bin/ente-camera-sync \
  --apply \
  --export-root /data/ente-export \
  --camera-root "$HOME/Pictures/Camera"
```

Enteからの取得も含めて手動テストします．

```bash
~/.local/bin/ente-camera-sync \
  --run-export \
  --apply \
  --export-root /data/ente-export \
  --camera-root "$HOME/Pictures/Camera"
```

## 定期実行

毎日3時から最大15分のランダム遅延後に実行します．PCが停止していた場合は，
次にユーザーのsystemdマネージャーが起動したときに補完されます．

```bash
systemctl --user enable --now ente-camera-sync.timer
systemctl --user list-timers ente-camera-sync.timer
```

直ちに1回動かす場合:

```bash
systemctl --user start ente-camera-sync.service
```

ログ:

```bash
journalctl --user -u ente-camera-sync.service -e
journalctl --user -u ente-camera-sync.service --since today
```

ログインしていない状態でも起動時からユーザータイマーを動かす場合だけ，
lingerを有効にします．

```bash
sudo loginctl enable-linger "$USER"
```

無効化:

```bash
sudo loginctl disable-linger "$USER"
```

## パスの変更

`~/.config/systemd/user/ente-camera-sync.service`の`ExecStart=`を編集し，
その後に再読込します．

```bash
systemctl --user daemon-reload
systemctl --user restart ente-camera-sync.timer
```

## 状態データ

`~/.local/state/ente-camera-sync/state.sqlite3`には，既存Cameraファイルの
パス・サイズ・更新時刻・SHA-256を保存します．パスワードやEnteの認証情報は
保存しません．

実行ごとの要約は次に保存されます．

```text
~/.local/state/ente-camera-sync/runs/YYYY-MM-DD_HHMMSS.json
```

キャッシュを無視してCamera全体のSHA-256を再計算する場合:

```bash
~/.local/bin/ente-camera-sync --reindex
```
