// tests/run_eval.mjs
// 轻量级评估运行器：充当你任务里提到的 `evalkit run tests/basic-tasks.yaml`。
//
// 说明：evalkit (v0.2.0) 是一个 Node 库（提供 runSuite），本身没有 CLI 子命令，
// 因此这里用一段薄 runner 把 `tests/basic-tasks.yaml` 翻译成 evalkit 的
// test_cases（把 `expected: VERIFIED/REFUTED` 映射为 checks.mustContain），
// 并通过 agent 回调真正调用 Fable 5（src/cli/main.py）来获取其最终裁决。
//
// 运行：
//   NODE_PATH="<global node_modules>" node tests/run_eval.mjs
// 或在本仓库（evalkit 已全局安装）直接：
//   node tests/run_eval.mjs

import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const require = createRequire(import.meta.url);
const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, '..');

// evalkit 为全局安装（npm install -g），无 CLI；这里直接用绝对路径加载其 CJS 入口。
// 若要迁移到其它机器，把 EVALKIT_CJS 指向全局 node_modules/evalkit/dist/index.cjs 即可。
const EVALKIT_CJS =
  'C:/Users/imf/.workbuddy/binaries/node/versions/22.22.2/node_modules/evalkit/dist/index.cjs';
const { runSuite } = require(EVALKIT_CJS);

const PY = 'C:/Users/imf/.workbuddy/binaries/python/envs/default/Scripts/python.exe';

// ── 解析 basic-tasks.yaml（用户指定的高层格式：tasks[].id/name/input/expected）──
function parseTasks(text) {
  const tasks = [];
  let cur = null;
  for (const raw of text.split('\n')) {
    const line = raw.replace(/\r$/, '');
    if (!line.trim() || line.trim().startsWith('#')) continue;
    const item = line.match(/^\s*-\s*id:\s*(.+?)\s*$/);
    const field = line.match(/^\s+(\w+):\s*(.*)$/);
    if (item) {
      if (cur) tasks.push(cur);
      cur = { id: item[1].trim() };
    } else if (field && cur) {
      const k = field[1];
      let v = field[2].trim();
      if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
        v = v.slice(1, -1);
      }
      cur[k] = v;
    }
  }
  if (cur) tasks.push(cur);
  return tasks;
}

// ── Fable 5 agent 适配器：把 query 作为用户任务喂给 src/cli/main.py，取回最终裁决 ──
function makeFableAgent() {
  return async (query) => {
    const start = Date.now();
    const input = 'n\n' + query + '\n'; // 先答 'n' 跳过 checkpoint 恢复提示，再提交任务
    const res = spawnSync(PY, ['src/cli/main.py'], {
      input,
      cwd: PROJECT_ROOT,
      encoding: 'utf-8',
      timeout: 120000,
      env: { ...process.env },
      windowsHide: true,
    });
    const out = (res.stdout || '') + (res.stderr || '');
    const m = out.match(/VERIFIED|REFUTED|UNVERIFIABLE/);
    const verdict = m ? m[0] : '(无明确裁决)';
    if (res.error) {
      return { responseText: out + '\n[adapter-error] ' + res.error.message, latencyMs: Date.now() - start };
    }
    return { responseText: out, latencyMs: Date.now() - start };
  };
}

async function main() {
  const yamlPath = resolve(__dirname, 'basic-tasks.yaml');
  const tasks = parseTasks(readFileSync(yamlPath, 'utf-8'));
  if (!tasks.length) {
    console.error('未解析到任何任务，请检查 tests/basic-tasks.yaml');
    process.exit(2);
  }

  const test_cases = tasks.map((t) => ({
    id: t.id,
    query: t.input,
    checks: { mustContain: [t.expected], thresholdMs: 120000 },
    metadata: { name: t.name || '', expected: t.expected },
  }));

  console.log(`加载 ${tasks.length} 个评测任务，开始运行（agent=Fable 5）...\n`);
  const result = await runSuite({
    cases: { test_cases },
    agent: makeFableAgent(),
    name: 'Fable 5 基础能力',
    concurrency: 1,
    print: true,
  });

  console.log(`\n汇总：${result.passed}/${result.total} 通过，${result.failed} 失败`);
  process.exit(result.failed > 0 ? 1 : 0);
}

main().catch((e) => {
  console.error('运行器异常：', e);
  process.exit(3);
});
