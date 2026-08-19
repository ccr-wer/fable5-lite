#!/usr/bin/env node
/**
 * run_golden.mjs — 通过 evalkit 运行 Fable 5 黄金用例套件。
 *
 * evalkit 是「库」而非带 CLI 的可执行（v0.2.0 无 bin/run 子命令），因此这里直接调用
 * evalkit 的库 API（runSuite + loadFile + printSuiteResult）来充当 `evalkit run` 的等价入口：
 *   - loadFile("tests/golden-set.yaml") 读取 evalkit 原生 SuiteConfig；
 *   - agent 函数把每个用例的 query 喂给 src/cli/eval_adapter.py（Fable 5 的 evalkit 适配器），
 *     捕获其 stdout 的 JSON 作为 responseText；
 *   - runSuite 按 checks.regexPatterns（regexMode: any = 命中任一即通过）逐用例判定 PASS/FAIL。
 *
 * 等价于任务要求的：
 *   evalkit run tests/golden-set.yaml --agent "python src/cli/eval_adapter.py"
 *
 * 退出码：全部通过为 0，存在失败为非 0，便于 CI 判定。
 */

import { spawnSync } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import os from "node:os";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

// ── 路径解析 ──
const ROOT = process.cwd(); // 在 fable5-lite 项目根目录下运行
const ADAPTER = path.join(ROOT, "src", "cli", "eval_adapter.py");
const YAML = path.join(ROOT, "tests", "golden-set.yaml");

// 受管运行时（与项目其他脚本保持一致）
const PY = "C:/Users/imf/.workbuddy/binaries/python/envs/default/Scripts/python.exe";
const EVALKIT_CJS =
  "C:/Users/imf/.workbuddy/binaries/node/versions/22.22.2/node_modules/evalkit/dist/index.cjs";

// 加载 evalkit（优先按包名，回退到受管绝对路径）
let evalkit;
try {
  evalkit = require("evalkit");
} catch {
  evalkit = require(EVALKIT_CJS);
}
const { runSuite, printSuiteResult, loadFile } = evalkit;

// ── Agent：把 query 交给 Fable 5 适配器，返回 { responseText, actualTools } ──
// §44：沙箱已迁移到用户数据目录（Win %APPDATA%/fable5/sandbox，
// Linux/macOS ~/.local/share/fable5/sandbox），用例间清理也指向该路径。
function userDataSandbox() {
  if (process.platform === "win32") {
    return path.join(process.env.APPDATA, "fable5", "sandbox");
  }
  return path.join(os.homedir(), ".local", "share", "fable5", "sandbox");
}
const agent = async (query) => {
  // §43/§44：每个用例前清空用户数据沙箱，避免跨用例状态污染（如上一用例遗留的
  // sandbox/sandbox/... 嵌套文件导致本用例「读取已存在」而跳过写入）。
  try {
    fs.rmSync(userDataSandbox(), { recursive: true, force: true });
  } catch {
    /* 目录不存在时忽略 */
  }
  const res = spawnSync(PY, [ADAPTER], {
    input: JSON.stringify({ input: query }),
    encoding: "utf-8",
    timeout: 180_000, // 单用例超时 3 分钟（真实模型 + 流式字段流）
    maxBuffer: 16 * 1024 * 1024,
    cwd: ROOT,
  });
  if (res.error) throw res.error;
  const out = (res.stdout || "").trim();
  // 适配器只输出一行 JSON；取最后一行非空作为 responseText
  const lines = out.split(/\r?\n/).filter(Boolean);
  const jsonLine = lines[lines.length - 1] || "{}";
  let parsed = {};
  try {
    parsed = JSON.parse(jsonLine);
  } catch {
    /* 解析失败也继续，responseText 原样交予 evalkit 判定 */
  }
  return {
    responseText: jsonLine,
    actualTools: parsed.tools_used || [],
  };
};

async function main() {
  const config = loadFile(YAML);
  const result = await runSuite({
    cases: config,
    agent,
    name: config.name || "Fable 5 golden-set",
    print: true,
  });
  printSuiteResult(result);
  process.exit(result.failed === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error("运行套件失败：", e);
  process.exit(2);
});
