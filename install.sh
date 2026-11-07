#!/bin/bash
# Install MagnetKit for the current user.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v rqbit >/dev/null 2>&1; then
  command -v brew >/dev/null 2>&1 || { echo "需要 Homebrew：https://brew.sh" >&2; exit 1; }
  brew install rqbit
fi
command -v swiftc >/dev/null 2>&1 || { echo "需要 Xcode 命令行工具：xcode-select --install" >&2; exit 1; }

DEST="$HOME/.local/share/magnetkit"
mkdir -p "$DEST" "$HOME/.local/bin"
rm -rf "$DEST/app" "$DEST/ui"
cp -R magnet.py macos_helper.swift app ui "$DEST/"
chmod 755 "$DEST/magnet.py"
ln -sf "$DEST/magnet.py" "$HOME/.local/bin/magnet"

/usr/bin/python3 "$DEST/magnet.py" _setup --python /usr/bin/python3

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "提示：把 ~/.local/bin 加入 PATH 才能直接使用 magnet 命令。" ;;
esac
