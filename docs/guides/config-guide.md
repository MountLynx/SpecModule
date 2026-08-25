# 配置指南：.env、config.json 与回退链

本文档系统化说明 SpecModule 的配置全貌。命令级配置（`setup` 向导）见 [`references/cli-usage.md`](../references/cli-usage.md) 第 16 节；store 使用路径见 [`guides/store-walkthrough.md`](store-walkthrough.md)。

## 配置回退链（一条规则）

```
os.environ（最高，不覆盖已有键）
  → 项目根 .env / config.json / rules.txt
  → store 家目录（~/.specmodule/）同名文件
```

`LLMConfig.from_env(project_root, store_root)` 按此顺序加载：**先 `.env` → `os.environ`**（API key 等密钥），**再 `config.json`**（provider/model 注册表），**最后 `rules.txt`**（框架级输出格式约束，注入每次 LLM 调用的 system prompt 最前面）。

配置项优先级（覆盖关系）：

```
HarnessConfig.api_params > LLMConfig 默认值 > config.json > 硬编码默认值
```

> `os.environ` **不覆盖已有键**——shell 里已 export 的变量优先于 `.env` 文件。

## 三个配置文件

### `.env` — 密钥

API key 注入环境变量，变量名由 provider 的 `api_key_env` 指定：

```bash
DEEPSEEK_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

`.env` 从候选根依次加载（项目根优先，store 家目录兜底），**不覆盖已存在的环境变量**。`.env` 已被仓库 `.gitignore` 排除。

### `config.json` — provider 与 model 注册表

```json
{
  "providers": [
    {"name": "deepseek", "sdktype": "openai", "base_url": "https://api.deepseek.com",
     "api_key_env": "DEEPSEEK_API_KEY", "timeout": 120, "max_retries": 3}
  ],
  "models": [
    {"name": "deepseek-v4-flash", "provider": "deepseek",
     "think": true, "multimodal": false, "max_tokens": 1000000}
  ]
}
```

完整参考：仓库根 [`config.example.json`](../../config.example.json)。

| 字段 | 位置 | 说明 |
|------|------|------|
| `sdktype` | providers | 后端类型：`openai` / `openai-compatible`（OpenAI 及兼容接口，如 DeepSeek）/ `anthropic`（Claude） |
| `base_url` | providers | API 端点；OpenAI 兼容服务必须指定 |
| `api_key_env` | providers | 读 key 的环境变量名（key 本身放 `.env`） |
| `timeout` / `max_retries` | providers | 连接参数 |
| `models[]` | models | 模型能力注册表：`{name, provider, think, multimodal, max_tokens}`——`think`/`multimodal` 供客户端能力判断 |

`from_env` 取 `providers[0]` 为当前 provider，`models[0]` 为默认 model。**providers 为空 → `ValueError`**（"请参照 config.example.json 配置"）——框架不猜。

### `rules.txt` — 框架规则

框架级输出格式约束（文本），注入每次 LLM 调用的 system prompt 最前面。取候选根第一个存在的。

## LLMConfig 字段速查

`llm.config.LLMConfig`（`from_env()` 构造）：

| 字段 | 来源 | 说明 |
|------|------|------|
| `provider` | config.json `providers[0].sdktype` | 后端类型 |
| `api_key` | `api_key_env` 指定环境变量 | 密钥（无 key 时 `bool(config.api_key)` 为 False） |
| `base_url` | providers | API 端点 |
| `timeout` / `max_retries` | providers | 连接参数 |
| `model` | models[0] 或 override | 默认模型名 |
| `max_tokens` / `temperature` | models[0] 或默认值 | 默认生成参数 |
| `models` | config.json `models` | 模型能力注册表（`model_info(name)` 查询） |
| `system_rules` | rules.txt | 框架规则文本 |

## 各消费位置

| 位置 | 配置来源 | 说明 |
|------|---------|------|
| **CLI**（`run` 等） | 自动：项目根 → store 家目录 | 无需手动构造；`setup` 写 store 级 `.env` + `config.json` |
| **编程 API** | `LLMConfig.from_env()` / 显式传参 | 嵌入宿主自持配置（见 `guides/embedding.md`） |
| **harness 覆盖** | `HarnessConfig(model/temperature/think/api_params)` | 单节点覆盖 LLM 默认参数；`api_params` 按 SDK 官方格式透传，优先级最高 |

## 常见问题

| 症状 | 原因 | 处理 |
|------|------|------|
| `config.json 中 providers 为空或缺失` | 无 config.json 且 setup 未跑 | `specmodule setup`，或按 config.example.json 建项目级 config.json |
| key 未生效 | `.env` 位置不对 / 环境变量已占用 | 检查回退链：项目根 `.env` 优先；shell 已 export 的键不被 `.env` 覆盖 |
| 模型能力不对（think/multimodal） | models 注册表缺失该模型 | 在 config.json `models` 补 `{name, provider, think, multimodal}` |

运行时故障（退出码/错误速查）见 [`references/cli-usage.md`](../references/cli-usage.md) 第 6 节。
