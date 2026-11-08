# WebApp Builder Agent

根据结构化需求与参考图片生成 React + Express + SQLite 网站的 Agent，基于 [Requirement Compilation Structure](https://github.com/code-philia/requirement-compilation-structure)。使用 ARC-Bench 评测，BookStack 是其中一个任务。

当前输入为包含 `requirements.yaml` 和引用图片的目录；自然语言需求入口尚未实现。

本分支为 **B7**。默认 compact 流程直接从完整原始需求独立生成验收用例和整站实现。程序负责 Playwright 文件、覆盖 ID 与检查，不再单独请求模型写计划。无图片且首次通过时用 2 次文本调用，有图片时每 4 张唯一图片增加 1 次视觉调用。默认总逻辑调用上限为图片批数加 4、最多 12 次，可通过 `ARC_B6_MAX_MODEL_CALLS` 调整。额外的 2 次调用在修正、补读与故障恢复之间共用。官方通过率与实际账单需平台运行取得。

每次模型调用的已报告输入、缓存输入、输出、推理 Token 及耗时见 `.arc/compact-metrics.json`，缺失用量标为 null。构建、测试收集、类型检查和浏览器验收的耗时见 `.arc/compilation.json` 的 `checks`。这些检查耗时包含于所在阶段。整站生成默认使用 low 推理，沿用显式的 MODEL_REASONING 设置。

验收作者在同一次调用中为每个需求 ID 指定页面或控件、匹配条件和预期值。编译器生成并等待 Playwright 断言。收集阶段核对声明的 ID，执行阶段逐用例核对实际通过的断言，记录位于 `.arc/compact-last-tests.json` 的 `cases[].asserted_ids_by_run`。旧版仅含覆盖标签的测试需要重新生成才能通过这项检查。

## 环境

- Python 3.11+、[uv](https://docs.astral.sh/uv/)
- Node.js 22.12+、npm、Git、Bash
- 支持代码中模型及图像输入的 OpenAI Chat Completions 兼容 API

在仓库根目录安装依赖：

```sh
uv sync --frozen
uv run playwright install chromium
```

将 `.env.example` 复制为本地 `.env`，配置 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 和 `MODEL`。默认模型为 `deepseek-flash`；视觉模型可通过 `VISUAL_MODEL` 单独指定。不要提交 `.env` 或真实密钥。

## 运行

仓库的 `input/` 提供示例需求。完整评测任务需单独获取，并替换输入路径。

```sh
uv run python main.py input --output-dir outputs/demo
```

启动生成的网站：

```sh
cd outputs/demo
npm run db:init
npm run build
npm start
```

默认访问地址为 `http://localhost:3000`。通过 `main.py --help` 查看参数；`--model-log` 开启详细模型日志，`--port` 设置应用端口。

编译报告保存在生成目录的 `.arc/compilation.json`。B6 对无法构建或启动的失败返回非零退出码；内部验收失败但构建和启动通过时，保留失败状态与 `evaluation_ready` 证据，允许正式评测部分成果。classic 路线在部分阶段失败时仍可能返回退出码 0，不能仅看退出码判断验收是否通过。

## 代码结构

| 路径 | 内容 |
|---|---|
| `main.py`、`00_*.py`–`12_*.py` | 完整编译入口及 13 个阶段入口 |
| `infrastructure/compiler.py` | 编译流程调度 |
| `infrastructure/compact/` | B6 少轮完整应用生成、通用运行库与独立验收 |
| `infrastructure/stages/` | 需求、数据库、后端、前端处理 |
| `infrastructure/shared/` | 模型、检索、编辑循环、项目模板 |
| `tests/` | 编译器及工具测试 |
| `team-tools/package-baseline.py` | 当前 B7 干净打包工具；旧版本使用对应分支 |
| `team-tools/check-stage.py` | 不调用模型的本地单阶段检查 |
| `team-tools/check-compact.py` | 不调用模型的 B6 构建与浏览器验收复查 |

## 提交与协作

- [ARC-Bench 打包与提交](docs/submission.md)
- [贡献指南](CONTRIBUTING.md)
- [B3 成组流程实现与成本控制](docs/b3-flow-batches.md)
- [B4 批量生成与单套独立验收](docs/b4-batched-generation.md)
- [本地单步检查](docs/local-validation.md)
- [B4.1 本地回放与 UI 设计恢复](docs/b41-local-replay.md)
- [B4.2 通用接口接线生成](docs/b42-contract-codegen.md)
- [B4.3 环境恢复与全局停损](docs/b43-environment-guards.md)
- [B5 场景覆盖与连续用户流程](docs/b5-scenario-coverage.md)
- [B5.1 任务级推理与完整请求预算](docs/b51-model-budget.md)
- [B6 通用运行库与少轮完整应用生成](docs/b6-reusable-runtime.md)
- [B6.1 三次尝试与工具错误停损](docs/b61-three-attempts.md)
- [内部验收条件的来源和边界](docs/acceptance-oracle.md)

参考实现固定版本：`d3da23e9897db84cfd37ce04a4ab5b38424ef399`。B6.1 继承环境保护、文本请求预算和通用运行库，并收紧重试；已发布旧版 ZIP 保持不变。实际正确率、综合分与费用收益需平台评测验证。
