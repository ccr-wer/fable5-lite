# Fable 5 青春版（fable5-lite）第三方开源组件交叉检查报告

> 生成时间：2026-08-18
> 检查范围：`requirements.txt`、`src/integrations/`、`src/core/`、`src/cli/`、`src/parts/`（vendored）、`skills/`
> 项目自身许可证：**MIT**（见根目录 `LICENSE`，Copyright (c) 2026 fable5-lite contributors）
> 说明：本报告对每一项引用的开源/第三方组件列出许可证类型、使用方式与是否需要声明。**vendored 参考**指源码被直接纳入本仓库 `src/parts/`；**直接依赖**指写入 `requirements.txt`；**间接依赖**指代码中有 `import` 但未在 `requirements.txt` 中声明；**技能引用**指仅由技能文档/SKILL.md 作为运行时指引提及，未随项目分发其代码。

---

## 一、摘要

- **直接依赖（requirements.txt）**：3 项 —— `pyyaml`、`rubric-eval`、`prompt_toolkit`
- **间接/运行时依赖（有 import 但未在 requirements.txt 声明）**：5 项 —— `requests`、`python-dotenv`、`mcp`、`microsandbox`、`agent_knowledge`
- **第三方 API 服务**：1 项 —— `deepseek-v4`（DeepSeek API，专有服务）
- **Vendored 组件（src/parts）**：5 项 —— `fable-5`、`fable-method`、`fable5-orchestrator`、`oh-my-fable`、`opensquilla`（注：`deepcode` 已于 2026-08-18 从项目中移除）
- **评估工具/技能引用**：`evalkit`（npm，已全局安装）、`SkillsBench`（历史引用，已清理）
- **JavaScript 依赖**：`opensquilla-webui`（运行时 9 项 + 开发 7 项）、`oh-my-fable`（零运行时依赖，仅开发依赖 4 项）

### 关键发现（合规风险点）

1. ✅ **`deepcode` 专有组件已移除**（2026-08-18）：原 vendored 的 `src/parts/deepcode/`（"Private. All rights reserved."，非开源）已从项目中删除，相关合规风险点已消除。
2. 🟠 **`fable-5` 无任何 LICENSE 文件**（作者 LearNer），默认"保留所有权利"，vendored 进仓库存在许可不确定性。建议补充许可证声明或取得许可。
3. 🟠 **多个运行时依赖被 import 但未在 `requirements.txt` 中声明**：`requests`、`python-dotenv`、`mcp`、`microsandbox`、`agent_knowledge`。其中 `requests` 在 `llm.py`/`mcp_client.py` 中为首选 HTTP 库，缺失声明会导致环境不完整。**建议将这些包补入 `requirements.txt`（或其可选依赖分组）并在 README 依赖表中增补。**
4. 🟡 **`openai` SDK 并未被本仓库引用**：代码通过 `requests`/`urllib` 直接调用 DeepSeek 的 OpenAI 兼容端点（`https://api.deepseek.com/v1/chat/completions`），未 `import openai`。任务清单中列出的 "openai（SDK）" 经核实**无需声明**。
5. 🟡 **`opensquilla` 自带大量传递依赖**（含少量 copyleft 组件，如 `html2text` GPL-3.0、`python-telegram-bot` LGPL-3.0、`certifi` MPL-2.0、`weasyprint` LGPL-2.1+），其自身为 Apache-2.0 并内置 `THIRD_PARTY_NOTICES.md`。本仓库仅做 vendored 参考（路由层适配），未实际集成其服务端，分发时须一并携带 opensquilla 的许可证与第三方声明。

---

## 二、直接依赖（requirements.txt）

[pyyaml]
• 版本: 未固定（requirements.txt 未锁定版本）
• 许可证: MIT
• 使用方式: 直接依赖
• 是否需要声明: 是
• 备注: `src/integrations/user_data.py` 中 `import yaml`；opensquilla 亦依赖。建议在 requirements.txt 锁定版本（如 `pyyaml>=6.0`）。

[rubric-eval]
• 版本: 未固定（PyPI 最新为 0.2.0；零必须依赖）
• 许可证: MIT（Kareem Rashed，license = { text = "MIT" }）
• 使用方式: 直接依赖
• 是否需要声明: 是
• 备注: 轻量 LLM Agent 行为评估框架，CLI 为 `rubric run`。在 §39 评估体系中用于 Python 侧评估；非必须依赖（可选 extras：`openai`/`anthropic`/`semantic`/`rouge`）。

[prompt_toolkit]
• 版本: 未固定
• 许可证: BSD-3-Clause
• 使用方式: 直接依赖
• 是否需要声明: 是
• 备注: `src/cli/main.py`（`from prompt_toolkit import prompt` 等历史交互式 CLI）使用。

---

## 三、间接 / 运行时依赖（代码中有 import，但 requirements.txt 未声明）

[requests]
• 版本: 未固定（未在 requirements.txt 中）
• 许可证: Apache-2.0
• 使用方式: 间接依赖（运行时，动态 `import requests`）
• 是否需要声明: 是（且建议补入 requirements.txt）
• 备注: `src/integrations/llm.py`、`src/integrations/mcp_client.py` 中作为首选 HTTP 客户端调用 DeepSeek API / MCP。当前缺失声明属 GAP。

[python-dotenv]（导入名 `dotenv`）
• 版本: 未固定
• 许可证: BSD-3-Clause
• 使用方式: 间接依赖（可选 `from dotenv import load_dotenv`）
• 是否需要声明: 是（建议补入 requirements.txt）
• 备注: `src/integrations/llm.py` 中加载 `.env`，缺失时静默跳过。

[mcp]（Model Context Protocol Python SDK）
• 版本: 未固定
• 许可证: MIT
• 使用方式: 间接依赖（MCP 客户端集成）
• 是否需要声明: 是（建议补入 requirements.txt 或可选分组）
• 备注: `src/integrations/mcp_client.py` 中 `from mcp import ClientSession` / `from mcp.client.streamable_http import streamable_http_client`。opensquilla 将其列为可选 extra `mcp>=1.2.0`。

[microsandbox]
• 版本: 未固定（PyPI 最新 0.6.x）
• 许可证: Apache-2.0（Super Rad Company）
• 使用方式: 间接依赖（可选沙箱后端）
• 是否需要声明: 是（建议补入 requirements.txt 或可选分组）
• 备注: `src/integrations/sandbox.py` 中 `import microsandbox` 作为硬件级隔离优先后端；未安装时回退本地安全执行。基于 microVM（libkrun），需 KVM/WHP 支持。

[agent_knowledge]（PyPI 包名 `compiled-memory`）
• 版本: 未固定（PyPI 最新 compiled-memory 0.3.1）
• 许可证: MIT（yucx-go，"Pure Python, local-first, MIT licensed"）
• 使用方式: 间接依赖（可选记忆后端）
• 是否需要声明: 是
• 备注: `src/integrations/memory.py` 中 `from agent_knowledge import Vault, Compiler, SearchEngine`，优先于本地 JSONL 记忆；未安装时回退。仅依赖 PyYAML + 标准库。

---

## 四、第三方 API 服务

[deepseek-v4]（DeepSeek API）
• 版本: API 端点 `https://api.deepseek.com/v1/chat/completions`；模型 `deepseek-v4-flash` / `deepseek-v4-pro`
• 许可证: 专有服务（无开源软件许可证；适用 DeepSeek 服务条款与隐私政策）
• 使用方式: API 调用（经 `requests`/`urllib`，OpenAI 兼容协议）
• 是否需要声明: 是（作为服务依赖声明，非代码许可证）
• 备注: `src/integrations/llm.py`、`src/config/models.py` 定义。注意：本项目**未引入 openai SDK**，仅复用其 API 形态。

---

## 五、Vendored 组件（src/parts）

[fable-5]
• 版本: 未固定（Claude Code 插件形态；作者 LearNer）
• 许可证: **未声明（无 LICENSE 文件；README 标注 "Built by LearNer"）**
• 使用方式: vendored 参考（方法论 + benchmarks + `skills/fable-5/` 引用）
• 是否需要声明: 是（但当前缺失，属合规风险，须补充许可证或取得授权）
• 备注: 作为 Fable 5 工作循环（Think→Act→Prove）的方法论来源 vendored 进 `src/parts/fable-5/`；含 `benchmarks/` 与 `AGENTS.md`。未随附显式许可证。

[fable-method]
• 版本: 未固定（作者 Sahir619）
• 许可证: MIT
• 使用方式: vendored 参考（方法论设计参考，未实际集成）
• 是否需要声明: 是
• 备注: `src/parts/fable-method/LICENSE`（MIT，Copyright (c) 2026 Sahir619）。README 已将其列为依赖声明项。

[fable5-orchestrator]
• 版本: 未固定（作者 Yusuf Demirkoparan）
• 许可证: MIT
• 使用方式: vendored 参考（多代理编排设计参考；本项目自研 `src/core/orchestrator.py`）
• 是否需要声明: 是
• 备注: `src/parts/fable5-orchestrator/LICENSE`（MIT，Copyright (c) 2026 Yusuf Demirkoparan）。README 已列为依赖声明项。

[oh-my-fable]
• 版本: 0.4.0
• 许可证: MIT
• 使用方式: vendored 参考
• 是否需要声明: 是
• 备注: `src/parts/oh-my-fable/package.json`（`"license": "MIT"`）+ `LICENSE` 文件；声明 "zero dependencies"（运行时零依赖），仅含开发依赖（@types/node、tsup、typescript、vitest）。

[opensquilla]（OpenSquilla）
• 版本: 0.5.2（pyproject.toml）；WebUI `opensquilla-webui` 0.2.1
• 许可证: Apache-2.0
• 使用方式: vendored 参考（路由层适配，见 `src/integrations/routing/opensquilla_adapter.py`；未实际集成其服务端）
• 是否需要声明: 是
• 备注: `src/parts/opensquilla/LICENSE` + `pyproject.toml`（`license = "Apache-2.0"`）。其自身携带 `THIRD_PARTY_NOTICES.md`（构建时排除于 wheel）。**该组件捆绑大量传递依赖，含若干 copyleft 组件**——详见第八节。

---

## 六、评估工具 / 技能引用

[evalkit]（npm 包 `evalkit`）
• 版本: 0.2.0（已全局安装：`npm install -g evalkit`，落点受管 node 环境）
• 许可证: MIT（npm 包页标注；其 GitHub 上游仓库 `evalkit/evalkit` 另声明 Apache-2.0，**二者不一致，建议核实包内 LICENSE**）
• 使用方式: 技能引用 / 工具引用（评估体系）
• 是否需要声明: 是（若随项目分发或文档引用）
• 备注: `src/cli/eval_adapter.py` 为 Fable 5 的 evalkit Agent 适配器；`tests/run_golden.mjs` 直接 `require` 全局 evalkit 库 API（`runSuite` + `loadFile` + `printSuiteResult`）。evalkit 为"零运行时依赖"TypeScript 库。

[SkillsBench]（benchflow-ai/skillsbench）
• 版本: 未固定（历史引用）
• 许可证: 未明确单一许可证（重新引入前须核查上游仓库 LICENSE）
• 使用方式: 技能引用（历史）
• 是否需要声明: 否（当前不在仓库内）
• 备注: §38 曾克隆至项目根**之外**的 `../benchmarks/skillsbench/`，§39.1 已清理删除；README 标注其为"技能树初始技能来源"。当前仓库不含 SkillsBench 代码，无需随分发声明。注意：`skills/` 本地技能树（195+ 技能）已独立于 SkillsBench 保留。

---

## 七、skills/ 目录外部引用说明

- `skills/` 下为大量 `SKILL.md` 技能文档（command/、diagnose/、edit/、fs/、base64-codec 等），其中**指令性引用**了许多外部工具/库，以 `pip install` / `npm install` / `apt-get install` 形式作为运行时指引（例如 `ffmpeg`、`scipy`、`casadi`、`pymatgen`、`lightkurve`、`pycbc`、`qutip`、`rapidfuzz`、`pandas`、`selenium`、`markitdown`、`pptxgenjs`、`playwright`、`LibreOffice`、`imagemagick`、`trivy`、`uv` 等）。
- **这些引用是技能使用指引，并非 vendored 代码，也不构成 fable5-lite 的项目依赖**——仓库不随附这些第三方源代码，因此 fable5-lite 本身无需为它们做许可证声明。
- 经检索，`skills/` 内未发现捆绑的第三方源代码（仅 2 个 SKILL.md 含 "Copyright/license" 字样，均为对外部库的说明性文字，如 `fuzzing-python` 提及 Atheris、`citation-management` 列举引用库）。
- 结论：**skills/ 不影响 fable5-lite 的许可证义务**，但作为随项目分发的文档，其引用的外部工具应在合规审查中知悉（尤其涉及 GPL/copyleft 工具如 `trivy` 等为 Apache-2.0，安全可用）。

---

## 八、JavaScript 依赖（vendored WebUI / 包）

### 8.1 opensquilla-webui（vendored 于 src/parts/opensquilla/opensquilla-webui，v0.2.1）

运行时依赖（均需声明，随 WebUI 分发）：

| 包 | 许可证 |
|---|---|
| dompurify | MIT |
| highlight.js | BSD-3-Clause |
| html-to-image | MIT |
| katex | MIT |
| marked | MIT |
| pinia | MIT |
| vue | MIT |
| vue-i18n | MIT |
| vue-router | MIT |
| @types/dompurify（类型，非运行时） | MIT |

开发依赖：@playwright/test（Apache-2.0）、@vitejs/plugin-vue（MIT）、happy-dom（MIT）、typescript（Apache-2.0）、vite（MIT）、vitest（MIT）、vue-tsc（MIT）。

### 8.2 oh-my-fable（v0.4.0，"zero dependencies"）

运行时依赖：无。开发依赖：@types/node（MIT）、tsup（MIT）、typescript（Apache-2.0）、vitest（MIT）。

### 8.3 opensquilla 自带 Python 传递依赖（vendored Apache-2.0 项目内置）

opensquilla 自身为 Apache-2.0 并内置 `THIRD_PARTY_NOTICES.md`。其 `pyproject.toml` 声明的依赖（节选，许可证以各上游为准）：

- 宽松（MIT/BSD/Apache-2.0）为主：starlette(BSD-3)、python-multipart(Apache-2.0)、uvicorn(BSD-3)、pydantic(MIT)、pydantic-settings(MIT)、sqlmodel(MIT)、sqlalchemy(MIT)、anyio(MIT)、httpx(BSD-3)、brotli(MIT)、jinja2(BSD-3)、structlog(MIT/Apache-2.0)、typer(MIT)、rich(MIT)、websockets(BSD-3)、aiosqlite(MIT)、apscheduler(MIT)、pyyaml(MIT)、readability-lxml(Apache-2.0)、beautifulsoup4(MIT)、cachetools(Apache-2.0)、pdfplumber(MIT)、pillow(HPND)、croniter(MIT)、tomli-w(MIT)、yoyo-migrations(Apache-2.0)、questionary(MIT)、python-docx(MIT)、python-pptx(MIT)、openpyxl(MIT)、pypdf(BSD-3)、reportlab(BSD-3)、lark-oapi(MIT)、dingtalk-stream(MIT)、qq-botpy(MIT)、cryptography(Apache-2.0/BSD-3)、duckduckgo-search(MIT)。
- ⚠️ **需注意的 copyleft / 弱 copyleft 组件**（若启用相关功能并随分发，须遵守其条款）：
  - `html2text` — **GPL-3.0**（核心转换功能，强 copyleft）
  - `python-telegram-bot` — **LGPL-3.0**
  - `certifi` — **MPL-2.0**（文件级弱 copyleft）
  - `weasyprint`（document-extras extra）— **LGPL-2.1+**
  - `matrix-nio`（matrix extra）— Apache-2.0 / ISC
- 可选 extras 含：`tiktoken`(MIT/Apache-2.0)、`jieba`(MIT)、`numpy`(BSD-3)、`lightgbm`(MIT)、`scikit-learn`(BSD-3)、`onnxruntime`(MIT)、`tokenizers`(Apache-2.0)、`extract-msg`(MIT)、`swebench`(MIT)、`datasets`(Apache-2.0)、`markdown`(BSD-3)。

> 本仓库对 opensquilla 仅做**路由层适配**（vendored 参考），未实际集成其服务端；如将来启用 opensquilla 完整功能并分发，须一并携带其 `THIRD_PARTY_NOTICES.md` 与全部适用许可证文本。

---

## 九、组件清单快速索引

| 组件 | 类型 | 许可证 | 是否需要声明 |
|---|---|---|---|
| pyyaml | 直接依赖 | MIT | 是 |
| rubric-eval | 直接依赖 | MIT | 是 |
| prompt_toolkit | 直接依赖 | BSD-3-Clause | 是 |
| requests | 间接依赖（缺失声明） | Apache-2.0 | 是 |
| python-dotenv | 间接依赖（缺失声明） | BSD-3-Clause | 是 |
| mcp | 间接依赖（缺失声明） | MIT | 是 |
| microsandbox | 间接依赖（缺失声明） | Apache-2.0 | 是 |
| agent_knowledge (compiled-memory) | 间接依赖（缺失声明） | MIT | 是 |
| deepseek-v4 (API) | API 服务 | 专有（服务条款） | 是 |
| fable-5 | vendored 参考 | 未声明（风险） | 是（须补） |
| fable-method | vendored 参考 | MIT | 是 |
| fable5-orchestrator | vendored 参考 | MIT | 是 |
| oh-my-fable | vendored 参考 | MIT | 是 |
| opensquilla | vendored 参考 | Apache-2.0（+ 传递依赖） | 是 |
| evalkit | 技能/工具引用 | MIT（上游或标 Apache-2.0，待核） | 是 |
| SkillsBench | 历史引用（已清理） | 未明确 | 否 |
| opensquilla-webui JS 依赖 | vendored（前端） | 多为 MIT/BSD；详见第八节 | 是 |
| openai SDK | **未引用** | — | 否 |

---

## 十、交叉检查建议（行动项）

1. **补依赖声明**：将 `requests`、`python-dotenv`、`mcp`、`microsandbox`、`agent_knowledge` 加入 `requirements.txt`（建议分组为可选/运行时依赖），并在 README「依赖声明」章节增补，消除与 §70.2 既有声明的缺口（现有 README 表也未列 `pyyaml`）。
2. **deepcode 专有代码已移除**（2026-08-18）：`src/parts/deepcode/` 已从发行版删除，原合规风险点已消除。
3. **澄清 fable-5 许可**：向其作者/上游确认许可证，补入 `src/parts/fable-5/` 或取得使用许可。
4. **核实 evalkit 许可证**：确认全局安装的 `evalkit@0.2.0` 包内 LICENSE（npm 页 MIT vs 上游 Apache-2.0）。
5. **分发 opensquilla 时**：携带其 `THIRD_PARTY_NOTICES.md` 与 copyleft 组件（html2text GPL-3.0、python-telegram-bot LGPL-3.0、certifi MPL-2.0、weasyprint LGPL-2.1+）的许可证文本与合规说明。
6. **移除误导性清单项**：确认 `openai` SDK 不在本仓库任何 `import` 中（已核实），无需为其声明；如 README 或文档提及，建议改为"DeepSeek OpenAI 兼容 API（经 requests）"。

---
*本报告由开源组件交叉检查任务自动生成，许可证信息以各上游仓库 LICENSE 文件为准。*
