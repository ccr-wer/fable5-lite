#!/usr/bin/env bash
# Manual installer (macOS / Linux / Git Bash) for users who prefer standalone
# skills over the plugin. Plugin install (recommended): inside Claude Code run
#   /plugin marketplace add Sahir619/fable-method
#   /plugin install fable@fable-method
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)"
dst="$HOME/.claude/skills"

mkdir -p "$dst"
cp -r "$src/skills/fable-method" "$dst/"
cp -r "$src/skills/fable-loop" "$dst/"
cp -r "$src/skills/fable-judge" "$dst/"

echo "Installed: fable-method, fable-loop, fable-judge -> $dst"
echo "Try it: open Claude Code and type /fable-judge after any agent claims work is done."
