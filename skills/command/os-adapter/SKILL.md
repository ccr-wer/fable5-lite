---
name: os-adapter
description: 根据当前操作系统适配命令和路径
---

# 操作系统适配

本技能提供当前操作系统的命令映射，用于在 Act 阶段执行命令时选择正确的命令。

## 命令映射

根据当前操作系统类型，返回对应的命令映射表：

- Windows (cmd/powershell):
  - list: dir
  - move: move
  - remove: del
  - mkdir: mkdir
  - copy: copy

- Linux / macOS (bash/zsh):
  - list: ls -la
  - move: mv
  - remove: rm
  - mkdir: mkdir -p
  - copy: cp

## 使用方式

在 Act 阶段，在生成命令前调用本技能，获取当前系统的命令映射表，并据此生成正确的命令。
