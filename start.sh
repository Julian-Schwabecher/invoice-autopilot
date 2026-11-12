#!/bin/sh
# Double-click friendly launcher for macOS and Linux: ./start.sh
cd "$(dirname "$0")" || exit 1

if ! command -v uv >/dev/null 2>&1; then
  [ -x "$HOME/.local/bin/uv" ] && PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "Invoice Autopilot needs a small helper called uv. To install it, run:"
  echo
  echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo
  printf "Install it now? [y/N] "
  read -r answer
  case "$answer" in
    y|Y|yes|YES)
      curl -LsSf https://astral.sh/uv/install.sh | sh || { echo "The install didn't work. Check your internet and try again."; exit 1; }
      PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
      ;;
    *) exit 1 ;;
  esac
  if ! command -v uv >/dev/null 2>&1; then
    echo "Installed. Please close this window, open a new one and run ./start.sh again."
    exit 0
  fi
fi

uv run python run.py || { echo; echo "Something went wrong starting the app. The message above says why."; exit 1; }
