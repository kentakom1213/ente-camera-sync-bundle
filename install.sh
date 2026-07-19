#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

install -d -m 700 "$HOME/.local/bin"
install -d -m 700 "$HOME/.config/systemd/user"
install -d -m 700 "$HOME/.local/state/ente-camera-sync"

install -m 700 \
  "$script_dir/ente-camera-sync.py" \
  "$HOME/.local/bin/ente-camera-sync"

install -m 600 \
  "$script_dir/ente-camera-sync.service" \
  "$HOME/.config/systemd/user/ente-camera-sync.service"

install -m 600 \
  "$script_dir/ente-camera-sync.timer" \
  "$HOME/.config/systemd/user/ente-camera-sync.timer"

systemctl --user daemon-reload

cat <<'EOF'
インストールしました．

次に，以下を順に実行してください．

  ente account list
  ente account update --email '登録メールアドレス' --dir /data/ente-export

初回ドライラン:
  ~/.local/bin/ente-camera-sync \
    --export-root /data/ente-export \
    --camera-root "$HOME/Pictures/Camera"

既存エクスポートの取り込み:
  ~/.local/bin/ente-camera-sync \
    --apply \
    --export-root /data/ente-export \
    --camera-root "$HOME/Pictures/Camera"

Enteから取得して取り込む手動テスト:
  ~/.local/bin/ente-camera-sync \
    --run-export \
    --apply \
    --export-root /data/ente-export \
    --camera-root "$HOME/Pictures/Camera"

タイマーを有効化:
  systemctl --user enable --now ente-camera-sync.timer

状態確認:
  systemctl --user list-timers ente-camera-sync.timer
  journalctl --user -u ente-camera-sync.service -e
EOF
