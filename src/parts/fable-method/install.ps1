# Manual installer (Windows PowerShell) for users who prefer standalone
# skills over the plugin. Plugin install (recommended): inside Claude Code run
#   /plugin marketplace add Sahir619/fable-method
#   /plugin install fable@fable-method
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$dst = Join-Path $HOME ".claude\skills"

New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item (Join-Path $src "skills\fable-method") $dst -Recurse -Force
Copy-Item (Join-Path $src "skills\fable-loop") $dst -Recurse -Force
Copy-Item (Join-Path $src "skills\fable-judge") $dst -Recurse -Force

Write-Host "Installed: fable-method, fable-loop, fable-judge -> $dst"
Write-Host "Try it: open Claude Code and type /fable-judge after any agent claims work is done."
