# 图生成接口适配设计 — 多 provider 路由 + harness 图像模式

> 日期：2026-09-14
> 状态：设计已确认（brainstorm 四问：范围=OpenAI 兼容 images API / 产物=落盘返路径 /
> 形态=同节点 mode 字段 / 连接=按模型路由 provider），待实施
> 关联：`llm/config.py` / `llm/client.py` / `llm/mock.py`；
> `module_harness/core/config.py` / `core/harness.py` / `infra/events.py`；
> `module_harness/model/spec.py`（TaskDefinition）/ `orchestrate/graph_builder.py`；
> `docs/guides/config-guide.md`（能力位文档同步）

## 0. 缘起与边界

模型接口（`llm/`）目前只有 chat 文本补全一种调用形态，图像生成（文生图）完全缺位。
需求两点：**模型接口增加图生成接口的适配**；**harness 节点可作为图生成节点的载体**。

事实约束（决定了设计的形状）：

1. **Anthropic 官方没有图像生成 API**——图像生成只存在于 OpenAI `images/generations`
   及兼容该格式的各家端点（智谱 CogView、硅基流动、火山方舟等）。适配范围即此一种。
2. **全库单连接不成立**。现状 `LLMConfig.from_env` 只取 `config.json providers[0]`
   建连，models 注册表每模型的 `provider` 字段声明了却没路由。而 chat 用 DeepSeek
   （无生图）、生图用智谱/硅基流动是常见组合——本次一并升级为按模型路由 provider。
3. **图像产物会污染审计链**。节点状态写入 `NodeState.mutable_state`（快照/查询共用），
   大二进制入库让快照变重。产物落盘、状态只记路径与元数据。

非目标（v1 不做）：图像编辑/重绘（edits 端点）、n>1 多图、图生图（参考图输入）、
Module 层自动注入 run 目录、URL 下载兜底。

## 1. llm/config.py — Provider 注册表（多 provider 地基）

### 1.1 ProviderConfig

新增 `@dataclass ProviderConfig`：`name / sdktype / api_key / base_url / timeout /
max_retries`。现 `LLMConfig.to_client_kwargs()` 的建连参数逻辑移至
`ProviderConfig.to_client_kwargs()`——按 provider 分建连后，连接参数按 provider 各自取。

### 1.2 LLMConfig 扩展

- 新增字段 `providers: dict[str, ProviderConfig]`（name → 连接配置）。
- `from_env` 解析**全部** providers；现有顶层字段 `provider / api_key / base_url /
  timeout / max_retries` 保持 = `providers[0]`（默认 provider）。**向后兼容**：
  现有消费方（读这些顶层字段的代码）零改动。
- 新增 `provider_for(model: str) -> ProviderConfig`：查 `models` 注册表该模型的
  `provider` 名 → 对应 `ProviderConfig`；模型未注册或未指明 provider → 默认 provider。
  这是路由的唯一查表点。
- models 注册表能力位新增 `image_gen: bool`（`config.json` models 条目可选键，
  声明该模型可生图；`config-guide.md` 同步）。

## 2. llm/client.py — ImageResult / generate_image / RoutingClient

### 2.1 ImageResult

```python
@dataclass
class ImageResult:
    data: bytes                      # 图像字节（b64_json 解码后）
    revised_prompt: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
```

### 2.2 generate_image（接口对称，能力缺失显式）

- `OpenAIClient.generate_image(prompt, *, model=None, size=None,
  api_params=None) -> ImageResult`：调 `client.images.generate(model, prompt,
  size?, response_format="b64_json", n=1, **api_params)`，取 `data[0].b64_json`
  解码为字节。统一请求 b64——临时 URL（dall-e 系列默认，1 小时时效）不落库。
  `api_params` 沿用 `_apply_api_params`（已知字段直入，未知入 extra_body）。
  SDK 异常 → `LLMError`。
- `AnthropicClient.generate_image(...)`：抛 `LLMError("Anthropic 无图像生成 API")`。
  两客户端接口对称，调用方无需预判 provider；能力缺失是基础设施故障，显式暴露，
  框架不猜测、不降级。

### 2.3 RoutingClient（路由门面）

```python
class RoutingClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._clients: dict[str, Any] = {}   # provider name → client（惰性建连缓存）

    def _client_for(self, model: str | None) -> Any: ...
    async def complete(self, prompt, **overrides) -> LLMResponse: ...
    async def chat(self, messages, tools=None) -> LLMResponse: ...
    async def generate_image(self, prompt, **overrides) -> ImageResult: ...
    async def close(self) -> None: ...
    @property
    def ready(self) -> bool: ...
```

- `_client_for(model)`：`model or config.model` → `config.provider_for(...)` →
  按该 provider 的 `sdktype` 惰性构造 `AnthropicClient` / `OpenAIClient` 并缓存。
  同一 provider 只建连一次；`complete/chat` 按**每次调用的模型**路由（逐调用
  model 覆盖语义保持），`generate_image` 同理。
- `ready` = 默认 provider client 构造成功（惰性触发建连）。
- `create_llm_client(config)` 改为返回 `RoutingClient`（函数签名不变）。
  直接构造 `AnthropicClient` / `OpenAIClient` 的既有用法不受影响——只是不经路由。
- 单 provider 配置行为不变：所有模型都落到默认 provider。

### 2.4 MockLLMClient

新增 `generate_image(**kwargs) -> ImageResult`：返回 1×1 PNG 字节（硬编码最小
PNG 头），测试免网络免 key。

## 3. module_harness/core — harness 节点承载图生成

### 3.1 HarnessConfig 新字段

```python
mode: str = "text"            # 仅 "text" | "image"，非法值构造时 ValueError
image_size: str | None = None # 如 "1024x1024"；None = API 默认
image_dir: str = "images"     # cwd 相对；调用方可指向任意目录
```

- 校验：`mode="image"` 与 `output_format` 互斥——同时出现 `ValueError`
  （图像无文本输出格式可校验，组合非法即报错，框架不猜测）。
- `to_dict / from_dict` 自然覆盖新字段（dataclasses.asdict）。
- `from_task_definition` 读取 `mode / image_size / image_dir`（可选键）。
- `image_dir` 默认 cwd 相对 `"images"`；模块作者/嵌入者可显式指向
  `.specmodule/runs/<module_id>/images` 等任意位置。Module 层自动注入为非目标。

### 3.2 Harness.build_body 图像分支

`config.mode == "image"` 时 body 流程：

1. 三层 prompt 渲染——与文本模式完全同配方；`PromptRendered` /
   `LlmCallStarted`（model, prompt_chars）事件照发。
2. `llm.generate_image(prompt=rendered, model=config.model, size=config.image_size,
   api_params=...)`——模型经 RoutingClient 按调用模型路由到所属 provider。
   `LLMError` → `HarnessFailed`（`failure_type="infrastructure"`）
   → `Failure(type="infrastructure")`，与文本模式同一映射。`LlmToken` **不发**
   （images API 无 token 流）。
3. 落盘 `image_dir/<node>-<monotonic_ns>.png`（`Path.mkdir(parents=True,
   exist_ok=True)`；文件名含节点名与 `time.monotonic_ns()` 防撞；扩展名固定
   png——gpt-image-1 与 dall-e 系列输出均为 png）。落盘 `OSError` 与 `LLMError`
   同归 `Failure(type="infrastructure")`。
4. 新事件 `ImageSaved(HarnessEvent)`：`path: str, bytes_len: int`；state 审计链写
   `_image_path` / `_usage`（字节不入 state）。`OutputValidated` **不发**（无文本
   校验语义）。
5. **返回路径字符串**——下游节点按字段消费路径。

`call_harness` 与 `HarnessRegistry` **零改动**：body 是同一份执行配方，图像能力
自动获得（`HarnessCallResult.value` = 路径）。

### 3.3 事件

`infra/events.py` 新增：

```python
@dataclass
class ImageSaved(HarnessEvent):
    path: str
    bytes_len: int
```

## 4. tasklist 侧

- `TaskDefinition` 新增可选字段 `mode: str | None`、`image_size: str | None`、
  `image_dir: str | None`；`from_dict` 对应读取。
- `graph_builder._register_harness` 按既有 `model / temperature` 同款方式做
  task 级覆盖传播（task 值非 None 则覆盖基 harness 配置值）。
- tasklist 声明示例：

```json
"draw_cover": {
  "type": "harness", "harness": "draw",
  "mode": "image", "image_size": "1024x1024",
  "inputs": {"title": "outline"}
}
```

## 5. 错误契约与测试

### 5.1 错误契约（完全沿用两级）

| 故障 | 映射 |
|------|------|
| 鉴权/网络/端点不支持/Anthropic 无此能力 | `LLMError` → `Failure(type="infrastructure")` → ABORTED |
| 落盘 OSError | 同上（基础设施故障，重试同调用不可解） |
| 图像模式无 `type="llm"` 失败路径 | 没有输出格式可校验，OutputValidated 不发 |

### 5.2 测试（TDD，先测后码）

llm/tests：
- config：多 provider 解析（providers 全量入表、顶层字段 = 默认 provider）、
  `provider_for` 查表与回落。
- RoutingClient：模型→provider 路由（按 sdktype 建对应 client）、惰性缓存
  （同 provider 只建一次）、单 provider 兼容（未注册模型落默认）。
- `generate_image`：参数组装（response_format=b64_json、size、api_params）与
  b64 解码（mock SDK client）；MockLLMClient.generate_image 返回合法 PNG。

module_harness/tests：
- HarnessConfig：mode 非法值 ValueError；image × output_format 互斥 ValueError。
- 图像 body 全流程：mock generate_image → tmp_path 落盘 → 返回路径、
  `ImageSaved` / `PromptRendered` / `LlmCallStarted` 事件断言、state 审计链断言、
  LLMError → infrastructure Failure 断言。
- `from_task_definition` 读图像字段；graph_builder task 级覆盖传播。
