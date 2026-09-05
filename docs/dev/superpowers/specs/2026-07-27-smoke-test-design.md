# 真实 LLM 冒烟测试设计

> ⚠️ **tickflow 0.2.0 bind 迁移注记（2026-09-05）**：本文档编写于旧视图机制时期——`input_aliases` / producer 名访问（`view["X"].value`、`view.A.value`）/ DictView 构造均已被具名 bind 机制取代：body/guard 经 `view.field()`、`view.output`、`v.named` 消费，字段名即 `task.inputs` 键。文中代码示例为当时形态，勿照抄；当前契约见 `docs/references/spec-harness-syntax.md` 与 `docs/references/tickflow-integration.md`。


> 日期：2026-07-27 | 状态：设计中

## 背景

SpecModule 所有 125 个现有测试使用 `MagicMock`/`AsyncMock` 替代 LLM 客户端，
从未在真实 API 下运行过。本次设计冒烟测试覆盖核心流水线，暴露 mock 无法发现的问题。

## 目标

- 验证 Harness → LLM(DeepSeek) → OutputValidator → EventBus → AsyncRunner 全链路
- 验证 `api_params` 透传（think 模式开关可用）
- 验证 Module 编排器 + script 翻译器
- 验证内置 `translate.json` 模板（LLM 翻译 tasklist）
- 发现问题当场修复，不积累技术债

## 配置依赖

- `config.json` — Provider + Model 注册表
- `.env` — `DEEPSEEK_API_KEY`
- `LLMConfig.from_env()` 解析上述两个文件

## 测试结构

```
module_harness/tests/smoke/
├── __init__.py
├── conftest.py         # llm_config / llm_client / event_bus fixtures
├── test_minimal.py     # 基础链路：1 harness + 1 script
├── test_think.py       # think=False vs think={"type":"enabled"} 对比
├── test_module.py      # Module 编排器全链路（script 翻译器）
└── test_builtin.py     # 内置 translate.json 模板（LLM 翻译）
```

所有测试标记 `@pytest.mark.smoke`。

### conftest.py

三个 module-scoped fixtures：
- `llm_config` — 从 `config.json` + `.env` 加载
- `llm_client` — `create_llm_client(config)`
- `event_bus()` — function-scoped，带全事件录制回调

### test_minimal.py — 基础链路

**Graph**: `[A] --> B`

- A: harness `translate`，prompt="将'{text}'翻译为中文，输出JSON: {\"translation\": \"...\"}"，`json_object`，`think=False`
- B: script `echo`，透传 `view.A.value`

**断言**:
- 两个 firing `status == "ok"`，无 `Failure`
- `output["translation"]` 非空字符串
- EventBus 收集到 `PromptRendered`, `LlmCallStarted`, `LlmToken`, `LlmCallCompleted`, `OutputValidated`

### test_think.py — Think 开关对比

**Graph**: `[A] --> B`

- A: harness `analyze`，prompt="分析以下代码的时间复杂度并输出JSON: {code}"
- B: script `echo`

**两个 sub-test**:
1. `think=False` — 普通模式
2. `think={"type": "enabled"}` — 思考模式

**断言**:
- 两次都成功（无 Failure）
- 思考模式 `usage["output_tokens"]` 显著多于普通模式
- 思考模式响应可能更详细

### test_module.py — Module 全链路

- 注册 harness `translate` + script `format_output`
- 注册 script 翻译器：返回固定 2 节点 tasklist
- 注册模板引用该翻译器
- `Module(spec={...}, template_name="smoke_module")` → `await mod.run()`

**断言**:
- Runner 正常完成（`is_idle()`）
- firings 数量 >= 2
- B 的 output 非空

### test_builtin.py — 内置模板

- `TemplateLoader.load_builtins()` 加载 `translate.json`
- 注册 `spec_to_tasklist` harness（LLM 翻译） + `translate` harness + `format_output` script
- `Module(spec={"source_text": "Hello world", "style": "formal"}, template_name="translate")` → `await mod.run()`

**断言**:
- Runner 正常完成
- 若 LLM 翻译 tasklist 失败（JSON 解析错误），记录问题并标记 `xfail`

## 运行

```bash
python -m pytest module_harness/tests/smoke/ -v -s        # 冒烟测试
python -m pytest module_harness/tests/ -q --ignore=smoke/  # 常规测试
```

## 不在范围

- 不修改 tickflow
- 不修改内置模板 translate.json
- 不修改 Harness / PromptRenderer / OutputValidator（除非发现阻断性 bug）
- 不加入 CI（真实 LLM 测试不适合 CI）

## 失败处理策略

发现问题当场修复，修复后重跑直到通过。修复范围限定在：
- `llm/` 客户端层（api_params 透传、JSON 解析）
- `module_harness/` 翻译层（Translator、TasklistValidator）
- 测试代码本身

不修改 tickflow 和已稳定的 Harness/PromptRenderer/OutputValidator。
