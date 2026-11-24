#!/bin/bash
# Double-click in Finder to install Claude Pocket for this Mac user (see README.md).
# The first time, macOS may say it cannot check this file: right-click it, choose Open, then Open again.
cd "$(dirname "$0")" || exit 1
bash ./install.sh "$@"
echo
read -r -n 1 -s -p "Press any key to close this window." </dev/tty
echo
