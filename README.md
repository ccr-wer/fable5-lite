# fable5-lite

> **🌏 Language Note / 语言说明**  
> This project's terminal interface and documentation are primarily in **Chinese (Simplified)**.  
> 本项目的终端界面和文档主要以**简体中文**呈现。  
> English README and internationalization support are planned for future releases.

Fable 5 青春版零件包 —— **Think → Act → Prove** 核心循环的最小可运行实现。
基于 DeepSeek V4 的本地命令行智能体：模型决策、本地沙箱执行、规则验证层裁决。

## 特性

- **Think → Act → Prove 循环**：模型先规划（think），再调用工具执行（act），最后由规则验证层 + 模型共同裁决（prove）。
- **任务拆解与并行执行**：可拆解任务自动拆为多个子任务，由 orchestrator 启动多个「子终端」并行执行，完成后合并统一验证。
- **沙箱隔离**：所有工具操作限制在用户数据目录 `sandbox/` 内，越界路径与危险命令（`del /F`、`rm -rf`、`curl` 等）被拦截。
- **技能树**：`./skills/<分类>/<子技能>/SKILL.md` 按分类索引，任务自动匹配并注入相关技能（含 os-adapter 操作系统命令适配）。
- **规则验证层**：语义匹配（文件/命令/代码）+ 子任务一致性 + 依赖关系检查；识别失败标记与过程性失败。
- **记忆层**：agent-knowledge 跨会话召回（回退本地 JSONL）。
- **Token 用量监测**：任务完成后显示输入/输出 token，按缓存命中拆分（`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`）并记录命中率。
- **项目报告**：Prove 通过后自动生成 Markdown 报告到 `reports/`。
- **输入体验**：prompt_toolkit 历史记录 / 自动补全 / 多行输入。

## 快速安装与启动

```bash
# 1. 安装依赖（Python 3.11+）
pip install -r requirements.txt

# 2. 启动（首次启动会弹出 API Key 配置向导）
cd fable5-lite
python src/cli/main.py

# 3. 输入任务开始一轮 Think->Act->Prove；exit/quit 退出；Ctrl+C 保存退出
#    输入「创建一个 test.txt 文件」试试看
```

## 配置说明

- **API Key**：首次启动的「配置向导」会提示输入 DeepSeek API Key，写入用户数据目录
  `config/config.yaml`（Windows: `%APPDATA%/fable5/`，Linux/macOS: `~/.local/share/fable5/`）。
  也可通过环境变量 `V4_API_KEY` 提供（优先级高于配置文件）。
- **工作空间**：`/workspace` 查看当前工作空间；`/workspace <路径或别名>` 切换
  （别名：桌面 / 下载 / 文档 / 项目；切换时自动清空记忆层）。
- **技能管理**：`/skill search <关键词> | /skill info <id> | /skill install <id> | /skill list`。

## 目录结构

```
src/
  cli/main.py            交互式终端入口
  core/                  核心循环（cycle/）、orchestrator（并行调度）、subagent、result_merger、
                         report_generator、validator/judge.py（规则验证层）
  integrations/          llm.py（DeepSeek 调用）、tools.py（工具执行与沙箱）、workspace.py、
                         memory.py（记忆层）、skill_manager.py（技能树）
  prompts/               系统提示词（system_prompt_merged.md）
skills/                  技能树（fs / edit / command / search / diagnose / understand / other）
src/parts/               vendored 参考组件（fable-method / fable-5 / fable5-orchestrator / oh-my-fable，未实际集成）
requirements.txt         运行时依赖
```

## 语言说明

- 界面与提示默认使用简体中文；模型回答跟随用户语言（中文问中文答、英文问英文答）。
- 代码标识符 / 命令 / 路径使用英文（行业惯例）；代码注释与项目文档默认中文。
- 报告（`reports/`）、日志（`logs/`）与记忆层使用中文为主。

## 依赖声明

本项目构建于以下开源项目 / 服务之上（按用途分组，许可证以各项目仓库 LICENSE 为准）：

| 项目 | 用途 | 许可证 |
| --- | --- | --- |
| [DeepSeek V4 API](https://platform.deepseek.com/) | 底层 LLM 推理服务（deepseek-v4-flash / deepseek-v4-pro） | 服务条款（非开源软件） |
| fable-method | Think → Act → Prove 方法论（vendored 于 `src/parts/fable-method`，未实际集成；核心循环设计参考） | MIT |
| fable-5 | 核心循环插件（8 步循环 / 验证目录 / 分解模式参考；vendored 于 `src/parts/fable-5`，未实际集成） | 原仓库未声明（vendored 参考） |
| oh-my-fable | 状态管理与 checkpoint 机制（会话恢复、检查点保存参考） | 以原仓库为准 |
| fable5-orchestrator | 多代理编排设计（vendored 于 `src/parts/fable5-orchestrator`，未实际集成；本项目自研 `src/core/orchestrator.py`） | MIT |
| agent-knowledge | 记忆层（跨会话召回，`compiled-memory` YAML vault + SQLite） | 以原仓库为准 |
| rubric-eval | 评估工具（TraceQuality / ToolCallAccuracy 观测层参考） | 以原仓库为准 |
| [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) | 终端输入增强（历史记录 / 自动补全 / 多行输入） | BSD-3-Clause |
| [SkillsBench](https://github.com/SkillsBench/SkillsBench) | 技能树初始技能来源（按分类整理到 `./skills/`） | 以原仓库为准 |

> 完整运行时依赖见 `requirements.txt`（按字母序）：`pyyaml`、`prompt_toolkit`、`requests`、
> `python-dotenv` 为必选；`mcp`、`microsandbox`、`agent_knowledge`（`compiled-memory`）
> 等为可选依赖（缺失时自动回退本地实现）；`rubric-eval` 为评估可选依赖。
> 路由层为本项目自研（`src/integrations/routing/router.py`，LightweightRouter）。

## 贡献指南

- **添加技能**：在 `./skills/<分类>/<子技能>/SKILL.md` 创建技能（frontmatter 含
  `name` / `description`）。`description` 用于任务自动匹配，建议包含任务常见关键词。
  注意：分类目录下自动生成的 `SKILL.md` 索引在新增子技能后需删除重建（运行一次
  `build_index` 即可自动重新生成）。
- **报告问题**：请附上复现任务文本、操作系统类型、`logs/tools.log`（工具调用日志）与
  `runs/session.json`（检查点）。
- **提交规范**：改动同步记录到 `DEVELOPMENT_LOG.md`（按 §编号追加）；提交前确认
  `.env` / `config.yaml` 未被 git 跟踪。

## 开源协议

MIT License。详见 [LICENSE](LICENSE)。
