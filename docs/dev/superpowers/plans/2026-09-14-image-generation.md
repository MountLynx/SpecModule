# 图生成接口适配实施计划(多 provider 路由 + harness 图像模式)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** llm 层新增 OpenAI 兼容 images API 适配与按模型路由的多 provider 客户端;harness 节点以 `mode="image"` 承载图生成,产物落盘返回路径。

**Architecture:** `ProviderConfig` 把 config.json 全部 providers 按名注册(顶层字段保持 = 默认 provider,向后兼容);`RoutingClient` 门面按「调用模型 → models 注册表 provider 名 → 惰性建连」路由,`generate_image` 双客户端对称(OpenAI 兼容实现,Anthropic 显式抛 LLMError);harness body 按 mode 分流,图像模式复用三层 prompt 与事件,落盘返路径,新增 `ImageSaved` 事件。

**Tech Stack:** Python 3.13 / dataclasses / pytest + pytest-asyncio(`@pytest.mark.asyncio`)+ unittest.mock / openai + anthropic SDK(测试用假模块注入,不装真 SDK 也能跑)。

**Spec:** `docs/dev/superpowers/specs/2026-09-14-image-generation-design.md`(已批准)

**测试命令:** `python -m pytest llm/tests/<file> -q`(单文件)、`python -m pytest llm/tests module_harness/tests -q`(全量)。测试不依赖 anthropic/openai 真包——用 `sys.modules` 注入假 SDK(现有 `test_client_wiring.py` 的 `_install_fake_sdk` 风格)。

**前置状态(执行前须知):** 工作区有一组**未提交**改动:`llm/client.py` + `llm/config.py`(`to_client_kwargs` 连接级参数重构)+ 未跟踪的 `llm/tests/test_client_wiring.py`。本计划建立在其之上,Task 1 先验证并提交。行号引用基于当前工作区,执行时以函数名定位。

---

## 文件结构总览

| 文件 | 动作 | 职责 |
|------|------|------|
| `llm/config.py` | 修改 | `ProviderConfig` + `LLMConfig.providers/provider_for/for_provider` |
| `llm/client.py` | 修改 | `ImageResult` + `generate_image`(双客户端)+ `RoutingClient` + 工厂切换 |
| `llm/mock.py` | 修改 | `MockLLMClient.generate_image`(1×1 PNG) |
| `llm/__init__.py` | 修改 | 导出 `ProviderConfig` / `ImageResult` / `RoutingClient` |
| `module_harness/core/config.py` | 修改 | `HarnessConfig` 图像字段 + 构造校验 + `from_task_definition` |
| `module_harness/infra/events.py` | 修改 | `ImageSaved` 事件 |
| `module_harness/core/harness.py` | 修改 | body 图像分支(渲染→生成→落盘→事件→返路径) |
| `module_harness/model/spec.py` | 修改 | `TaskDefinition` 三字段 + `from_dict` |
| `module_harness/orchestrate/graph_builder.py` | 修改 | `_register_harness` 覆盖传播 |
| `llm/tests/test_config_providers.py` | 新建 | 多 provider 表测试 |
| `llm/tests/test_image_gen.py` | 新建 | `generate_image` 测试 |
| `llm/tests/test_routing.py` | 新建 | `RoutingClient` 测试 |
| `llm/tests/test_mock_image.py` | 新建 | Mock 生图测试 |
| `module_harness/tests/test_config.py` | 修改 | 图像字段/互斥校验测试 |
| `module_harness/tests/test_harness.py` | 修改 | 图像 body 全流程测试 |
| `module_harness/tests/test_graph_builder.py` | 修改 | task 级覆盖传播测试 |
| `docs/guides/config-guide.md` | 修改 | `image_gen` 能力位 + 多 provider 路由说明 |
| `docs/references/spec-harness-syntax.md` | 修改 | Task 字段表补 `mode`/`image_size`/`image_dir` |
| `config.example.json` | 修改 | 追加生图 provider/模型示例 |

---

### Task 1: 前置——提交工作区 llm 接线重构

**Files:**
- Modify(提交): `llm/client.py`、`llm/config.py`、`llm/tests/test_client_wiring.py`(均已在工作区,不新改代码)

- [ ] **Step 1: 跑 llm 测试确认工作区改动是绿的**

Run: `python -m pytest llm/tests -q`
Expected: 全部 PASS(含 `test_client_wiring.py`)。若有失败,停下排查,不得带病提交。

- [ ] **Step 2: 提交**

```bash
git add llm/client.py llm/config.py llm/tests/test_client_wiring.py
git commit -m "refactor(llm): 客户端构造统一走 to_client_kwargs —— 连接级参数(timeout/max_retries/base_url)接线"
```

---

### Task 2: ProviderConfig + LLMConfig 多 provider 表

**Files:**
- Modify: `llm/config.py`
- Test: `llm/tests/test_config_providers.py`(新建)

- [ ] **Step 1: 写失败测试**

创建 `llm/tests/test_config_providers.py`:

```python
# llm/tests/test_config_providers.py
"""多 provider 注册表:from_env 全量解析 + provider_for 按模型查表 + for_provider 切片。

models 注册表每模型的 provider 字段自此真正路由;顶层字段(provider/api_key/base_url)
保持 = providers[0](默认 provider),向后兼容。手工构造 LLMConfig(providers 空)
一切行为不变,provider_for 返回 None = 用默认连接。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm.config import LLMConfig, ProviderConfig


def _write(root: Path, data: dict) -> None:
    (root / "config.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def roots(tmp_path):
    project = tmp_path / "project"
    store = tmp_path / "store"
    project.mkdir()
    store.mkdir()
    return project, store


MULTI = {
    "providers": [
        {"name": "deepseek", "sdktype": "openai",
         "base_url": "https://api.deepseek.com", "api_key_env": "DS_KEY"},
        {"name": "zhipu", "sdktype": "openai",
         "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key_env": "ZP_KEY"},
        {"name": "anthropic", "sdktype": "anthropic", "api_key_env": "AN_KEY"},
    ],
    "models": [
        {"name": "deepseek-chat", "provider": "deepseek", "max_tokens": 8192},
        {"name": "cogview-4", "provider": "zhipu", "image_gen": True},
        {"name": "bare-model"},
    ],
}


def _multi_config(roots, monkeypatch) -> LLMConfig:
    project, store = roots
    _write(project, MULTI)
    monkeypatch.setenv("DS_KEY", "kd")
    monkeypatch.setenv("ZP_KEY", "kz")
    monkeypatch.setenv("AN_KEY", "ka")
    return LLMConfig.from_env(project_root=project, store_root=store)


class TestProvidersMap:
    def test_from_env_parses_all_providers(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        assert set(c.providers) == {"deepseek", "zhipu", "anthropic"}
        z = c.providers["zhipu"]
        assert z.sdktype == "openai"
        assert z.api_key == "kz"
        assert z.base_url == "https://open.bigmodel.cn/api/paas/v4"

    def test_top_level_fields_stay_default_provider(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        assert c.provider == "openai"        # providers[0] 的 sdktype
        assert c.api_key == "kd"             # providers[0] 的 key
        assert c.base_url == "https://api.deepseek.com"
        assert c.model == "deepseek-chat"    # models[0] 默认模型,不变

    def test_provider_entry_without_name_falls_back(self, roots, monkeypatch):
        project, _ = roots
        _write(project, {"providers": [{"sdktype": "openai", "api_key_env": "X"}]})
        c = LLMConfig.from_env(project_root=project, store_root=store_root(roots))
        assert list(c.providers) == ["provider_0"]


def store_root(roots):
    return roots[1]


class TestProviderFor:
    def test_lookup_by_model(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        assert c.provider_for("cogview-4").name == "zhipu"
        assert c.provider_for("deepseek-chat").name == "deepseek"

    def test_unregistered_model_returns_none(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        assert c.provider_for("unknown-model") is None

    def test_model_without_provider_field_returns_none(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        assert c.provider_for("bare-model") is None

    def test_handmade_config_returns_none(self):
        c = LLMConfig(api_key="k", model="m")  # providers 空
        assert c.provider_for("m") is None


class TestForProvider:
    def test_slice_switches_connection_shares_request_level(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        sl = c.for_provider(c.providers["zhipu"])
        assert sl.provider == "openai"
        assert sl.api_key == "kz"
        assert sl.base_url == "https://open.bigmodel.cn/api/paas/v4"
        assert sl.model == "deepseek-chat"        # 请求级字段共享
        assert sl.models is c.models
        assert sl.to_client_kwargs()["api_key"] == "kz"  # 建连即取该 provider

    def test_to_client_kwargs_unchanged_on_default(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        assert c.to_client_kwargs() == {
            "api_key": "kd",
            "base_url": "https://api.deepseek.com",
            "timeout": 60.0,
            "max_retries": 3,
        }
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest llm/tests/test_config_providers.py -q`
Expected: FAIL —— `ImportError: cannot import name 'ProviderConfig'`

- [ ] **Step 3: 实现**

`llm/config.py` 三处修改。

(a) 在 `LLMConfig` 类定义**之前**新增 `ProviderConfig`:

```python
@dataclass
class ProviderConfig:
    """单个 provider 的连接配置（来自 config.json providers 条目）。

    多 provider 路由的地基：models 注册表每模型的 provider 字段指向这里的 name。
    """
    name: str = ""
    sdktype: str = "openai"
    api_key: str = ""
    base_url: str | None = None
    timeout: float = 60.0
    max_retries: int = 3

    def to_client_kwargs(self) -> dict[str, Any]:
        """转为 SDK 客户端构造参数（连接级）。"""
        kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return kwargs
```

(b) `LLMConfig` 增加字段(放在 `models` 字段之后):

```python
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    """{provider_name: ProviderConfig}。from_env 解析全部 providers；
    手工构造可为空（provider_for 返回 None = 用默认顶层连接）。"""
```

(c) `LLMConfig` 增加两个方法(放在 `model_info` 之后):

```python
    def provider_for(self, model: str | None) -> ProviderConfig | None:
        """查模型所属 provider 的连接配置。

        models 注册表声明 provider 名且在 providers 表中 → 该 provider；
        任何一步缺失（模型未注册 / 未指明 provider / 表无此名）→ None = 默认连接。
        """
        name = self.models.get(model or "", {}).get("provider")
        if name:
            return self.providers.get(name)
        return None

    def for_provider(self, provider: ProviderConfig) -> "LLMConfig":
        """返回连接字段切到指定 provider 的配置副本（请求级字段共享）。

        RoutingClient 按 provider 建连用：client 构造签名接收 LLMConfig，
        切片把连接字段填进顶层字段，client 内 config.to_client_kwargs()
        即取到该 provider 的连接。
        """
        return LLMConfig(
            provider=provider.sdktype,
            api_key=provider.api_key,
            base_url=provider.base_url,
            timeout=provider.timeout,
            max_retries=provider.max_retries,
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            models=self.models,
            system_rules=self.system_rules,
            providers=self.providers,
        )
```

(d) `from_env` 中,替换「选中 provider」与「解析 API key」两段。现有代码:

```python
        # ── 选中 provider（取第一个）──
        p = providers[0]

        # ── 解析 API key ──
        api_key = overrides.pop("api_key", None)
        if api_key is None:
            key_env = p.get("api_key_env", "")
            api_key = os.environ.get(key_env, "") if key_env else ""
```

替换为:

```python
        # ── 解析全部 providers（name → 连接配置）──
        providers_map: dict[str, ProviderConfig] = {}
        for i, entry in enumerate(providers):
            pname = entry.get("name", "") or f"provider_{i}"
            key_env = entry.get("api_key_env", "")
            providers_map[pname] = ProviderConfig(
                name=pname,
                sdktype=entry.get("sdktype", "openai"),
                api_key=os.environ.get(key_env, "") if key_env else "",
                base_url=entry.get("base_url"),
                timeout=float(entry.get("timeout", 60.0)),
                max_retries=int(entry.get("max_retries", 3)),
            )
        p = providers[0]
        default_pc = providers_map[p.get("name", "") or "provider_0"]

        # ── 解析 API key（overrides 优先，同步回默认 provider 条目）──
        api_key = overrides.pop("api_key", None)
        if api_key is not None:
            default_pc.api_key = api_key
        else:
            api_key = default_pc.api_key
```

并把 `config = cls(...)` 构造改为(连接字段取 `default_pc`,新增 `providers=providers_map`):

```python
        config = cls(
            provider=default_pc.sdktype,
            api_key=api_key,
            base_url=default_pc.base_url,
            timeout=default_pc.timeout,
            max_retries=default_pc.max_retries,
            model=overrides.pop("model", None) or default_model,
            max_tokens=int(overrides.pop("max_tokens", None) or default_max_tokens),
            temperature=float(overrides.pop("temperature", None) or default_temperature),
            models=models_map,
            system_rules=system_rules,
            providers=providers_map,
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest llm/tests/test_config_providers.py llm/tests/test_config_chain.py llm/tests/test_client_wiring.py -q`
Expected: 全部 PASS(新测试 + 既有配置/接线测试无回归)。

- [ ] **Step 5: 提交**

```bash
git add llm/config.py llm/tests/test_config_providers.py
git commit -m "feat(llm): ProviderConfig + LLMConfig.providers 全量注册表 —— provider_for 按模型查表,顶层字段保持默认 provider 向后兼容"
```

---

### Task 3: ImageResult + generate_image(双客户端对称)

**Files:**
- Modify: `llm/client.py`
- Test: `llm/tests/test_image_gen.py`(新建)

- [ ] **Step 1: 写失败测试**

创建 `llm/tests/test_image_gen.py`:

```python
# llm/tests/test_image_gen.py
"""generate_image 适配测试:参数组装 / b64 解码 / 错误包装 / Anthropic 能力缺失。

images/generations 统一请求 response_format="b64_json"(临时 URL 不落库);
api_params 沿用 _apply_api_params(已知字段直入,未知入 extra_body——线上合并进
请求体顶层,对 images 端点同样生效)。
"""

from __future__ import annotations

import base64
import sys
import types
from typing import Any

import pytest

from llm.client import AnthropicClient, LLMError, OpenAIClient
from llm.config import LLMConfig

B64_PNG = base64.b64encode(b"fake-png-bytes").decode()


def _install_fake_sdk(monkeypatch, module_name: str, class_name: str, capture: dict) -> None:
    fake = types.ModuleType(module_name)

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            capture.update(kwargs)

    setattr(fake, class_name, FakeAsyncClient)
    monkeypatch.setitem(sys.modules, module_name, fake)


class _Item:
    def __init__(self, b64: str | None, revised: str | None = None):
        self.b64_json = b64
        self.revised_prompt = revised


class _ImagesResp:
    def __init__(self, items: list, usage: Any = None):
        self.data = items
        self.usage = usage


def _install_fake_openai_images(monkeypatch, capture: dict, response: Any) -> None:
    """注入带 images.generate 的假 openai 模块。"""
    fake = types.ModuleType("openai")

    class FakeImages:
        async def generate(self, **kwargs):
            capture["generate_kwargs"] = kwargs
            if isinstance(response, Exception):
                raise response
            return response

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            capture["constructor"] = kwargs
            self.images = FakeImages()

    fake.AsyncOpenAI = FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)


def _openai_client(monkeypatch, capture: dict, response: Any) -> OpenAIClient:
    _install_fake_openai_images(monkeypatch, capture, response)
    return OpenAIClient(LLMConfig(provider="openai", api_key="k", model="cogview-4"))


class TestOpenAIGenerateImage:
    @pytest.mark.asyncio
    async def test_requests_b64_and_decodes(self, monkeypatch):
        capture: dict = {}
        resp = _ImagesResp([_Item(B64_PNG, revised="a better prompt")],
                           usage=_Usage())
        client = _openai_client(monkeypatch, capture, resp)
        result = await client.generate_image("一只猫", size="1024x1024")
        kw = capture["generate_kwargs"]
        assert kw["model"] == "cogview-4"          # 缺省回落 config.model
        assert kw["prompt"] == "一只猫"
        assert kw["response_format"] == "b64_json"  # 统一取字节
        assert kw["n"] == 1
        assert kw["size"] == "1024x1024"
        assert result.data == b"fake-png-bytes"
        assert result.revised_prompt == "a better prompt"
        assert result.usage == {"input_tokens": 10, "output_tokens": 5}

    @pytest.mark.asyncio
    async def test_model_override_and_size_omitted(self, monkeypatch):
        capture: dict = {}
        client = _openai_client(monkeypatch, capture, _ImagesResp([_Item(B64_PNG)]))
        await client.generate_image("p", model="dall-e-3")
        assert capture["generate_kwargs"]["model"] == "dall-e-3"
        assert "size" not in capture["generate_kwargs"]

    @pytest.mark.asyncio
    async def test_api_params_known_direct_unknown_extra_body(self, monkeypatch):
        capture: dict = {}
        client = _openai_client(monkeypatch, capture, _ImagesResp([_Item(B64_PNG)]))
        await client.generate_image("p", api_params={"n": 2, "quality": "high"})
        kw = capture["generate_kwargs"]
        assert kw["n"] == 2                        # 已知参数直入
        assert kw["extra_body"] == {"quality": "high"}  # 未知入 extra_body

    @pytest.mark.asyncio
    async def test_sdk_error_wrapped_as_llm_error(self, monkeypatch):
        capture: dict = {}
        client = _openai_client(monkeypatch, capture, RuntimeError("boom"))
        with pytest.raises(LLMError, match="images API"):
            await client.generate_image("p")

    @pytest.mark.asyncio
    async def test_missing_b64_raises(self, monkeypatch):
        capture: dict = {}
        client = _openai_client(monkeypatch, capture, _ImagesResp([_Item(None)]))
        with pytest.raises(LLMError, match="b64_json"):
            await client.generate_image("p")


class _Usage:
    input_tokens = 10
    output_tokens = 5


class TestAnthropicGenerateImage:
    @pytest.mark.asyncio
    async def test_capability_missing_raises_llm_error(self, monkeypatch):
        capture: dict = {}
        _install_fake_sdk(monkeypatch, "anthropic", "AsyncAnthropic", capture)
        client = AnthropicClient(LLMConfig(provider="anthropic", api_key="k"))
        with pytest.raises(LLMError, match="Anthropic 无图像生成"):
            await client.generate_image("p")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest llm/tests/test_image_gen.py -q`
Expected: FAIL —— `ImportError: cannot import name 'ImageResult' from 'llm.client'`

- [ ] **Step 3: 实现**

`llm/client.py` 修改:

(a) 顶部 import 加 `base64`(与 `json` 同段)与 `ImageResult` dataclass(放在 `LLMResponse` 之后):

```python
import base64
```

```python
@dataclass
class ImageResult:
    """图像生成结果。generate_image 成功时返回；调用失败抛 LLMError 而非返回此对象。"""
    data: bytes
    revised_prompt: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
```

(b) `AnthropicClient` 增加方法(放在 `complete` 之前):

```python
    async def generate_image(self, prompt: str, **kwargs: Any) -> ImageResult:
        """Anthropic 无图像生成 API——能力缺失显式暴露,框架不猜测不降级。"""
        raise LLMError("Anthropic 无图像生成 API；图像生成请配置 OpenAI 兼容 provider")
```

(c) `OpenAIClient` 增加方法(放在 `_is_reasoning_model` 之后):

```python
    async def generate_image(
        self,
        prompt: str,
        *,
        model: str | None = None,
        size: str | None = None,
        api_params: dict[str, Any] | None = None,
    ) -> ImageResult:
        """图像生成(harness 图像模式用)。走 images/generations,统一取 b64。

        - ``model``：生图模型（如 gpt-image-1 / cogview-4），缺省回落 config
        - ``size``：如 "1024x1024"，None = API 默认
        - ``api_params``：透传 SDK 额外参数（已知字段直入，未知入 extra_body）
        """
        self._require_ready()
        kwargs: dict[str, Any] = {
            "model": model or self.config.model,
            "prompt": prompt,
            "response_format": "b64_json",
            "n": 1,
        }
        if size:
            kwargs["size"] = size
        _apply_api_params(kwargs, api_params, _KNOWN_OPENAI_PARAMS)
        try:
            response = await self._client.images.generate(**kwargs)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"OpenAI images API 调用失败: {exc}") from exc
        items = getattr(response, "data", None) or []
        item = items[0] if items else None
        b64 = getattr(item, "b64_json", None) if item is not None else None
        if not b64:
            raise LLMError("images API 未返回 b64_json 图像数据")
        usage: dict[str, int] = {}
        resp_usage = getattr(response, "usage", None)
        if resp_usage is not None:
            for key in ("input_tokens", "output_tokens"):
                val = getattr(resp_usage, key, None)
                if val is not None:
                    usage[key] = val
        return ImageResult(
            data=base64.b64decode(b64),
            revised_prompt=getattr(item, "revised_prompt", None),
            usage=usage,
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest llm/tests/test_image_gen.py -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add llm/client.py llm/tests/test_image_gen.py
git commit -m "feat(llm): ImageResult + generate_image —— OpenAI 兼容 images API 统一取 b64,Anthropic 能力缺失显式抛 LLMError"
```

---

### Task 4: MockLLMClient.generate_image

**Files:**
- Modify: `llm/mock.py`
- Test: `llm/tests/test_mock_image.py`(新建)

- [ ] **Step 1: 写失败测试**

创建 `llm/tests/test_mock_image.py`:

```python
# llm/tests/test_mock_image.py
"""Mock 客户端生图:返回合法 PNG 字节,测试免网络免 key。"""

from __future__ import annotations

from llm.client import ImageResult
from llm.mock import MockLLMClient


async def test_generate_image_returns_png_bytes():
    result = await MockLLMClient().generate_image(prompt="x", model="m")
    assert isinstance(result, ImageResult)
    assert result.data.startswith(b"\x89PNG")  # PNG magic
    assert result.usage == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest llm/tests/test_mock_image.py -q`
Expected: FAIL —— `AttributeError: ... generate_image`

- [ ] **Step 3: 实现**

`llm/mock.py` 顶部 import 加 `base64`,并改 import 行与加方法:

```python
import base64
```

```python
from .client import ImageResult, LLMResponse
```

```python
# 1x1 透明 PNG（最小合法图像,测试断言 PNG magic 用）
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
```

类内追加:

```python
    async def generate_image(self, **kwargs: Any) -> ImageResult:
        """假生图:返回 1x1 PNG。"""
        return ImageResult(data=_PNG_1X1)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest llm/tests/test_mock_image.py -q`
Expected: PASS。顺带验证 PNG 常量合法:`python -c "from llm.mock import _PNG_1X1; assert _PNG_1X1.startswith(b'\x89PNG')"`

- [ ] **Step 5: 提交**

```bash
git add llm/mock.py llm/tests/test_mock_image.py
git commit -m "feat(llm): MockLLMClient.generate_image 返回 1x1 PNG —— 图像链路测试免网络"
```

---

### Task 5: RoutingClient 路由门面 + 工厂切换 + 导出

**Files:**
- Modify: `llm/client.py`、`llm/__init__.py`
- Test: `llm/tests/test_routing.py`(新建)

- [ ] **Step 1: 写失败测试**

创建 `llm/tests/test_routing.py`:

```python
# llm/tests/test_routing.py
"""RoutingClient 路由门面:模型→provider 惰性建连缓存,单 provider 兼容退化。"""

from __future__ import annotations

import sys
import types

import pytest

from llm.client import LLMError, RoutingClient, create_llm_client
from llm.config import LLMConfig, ProviderConfig


def _install_fake_sdk(monkeypatch, module_name: str, class_name: str, capture: dict) -> None:
    fake = types.ModuleType(module_name)

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            capture.setdefault("constructs", []).append(kwargs)

    setattr(fake, class_name, FakeAsyncClient)
    monkeypatch.setitem(sys.modules, module_name, fake)


def _multi_config() -> LLMConfig:
    return LLMConfig(
        provider="openai",
        api_key="kd",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        providers={
            "deepseek": ProviderConfig(name="deepseek", sdktype="openai",
                                       api_key="kd", base_url="https://api.deepseek.com"),
            "zhipu": ProviderConfig(name="zhipu", sdktype="openai",
                                    api_key="kz", base_url="https://open.bigmodel.cn/api/paas/v4"),
        },
        models={
            "deepseek-chat": {"name": "deepseek-chat", "provider": "deepseek"},
            "cogview-4": {"name": "cogview-4", "provider": "zhipu", "image_gen": True},
        },
    )


class TestRouting:
    def test_routes_by_model_provider(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        client = RoutingClient(_multi_config())
        c_ds = client._client_for("deepseek-chat")
        assert cap["constructs"][0] == {
            "api_key": "kd", "base_url": "https://api.deepseek.com",
            "timeout": 60.0, "max_retries": 3,
        }
        c_cg = client._client_for("cogview-4")
        assert cap["constructs"][1]["api_key"] == "kz"   # zhipu 连接
        assert c_ds is not c_cg

    def test_same_provider_cached(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        client = RoutingClient(_multi_config())
        a = client._client_for("cogview-4")
        assert client._client_for("cogview-4") is a      # 同 provider 只建一次
        assert len(cap["constructs"]) == 1

    def test_unregistered_model_uses_default_connection(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        client = RoutingClient(_multi_config())
        c = client._client_for("unknown-model")
        assert c.config.api_key == "kd"                  # 默认连接
        assert c.config.base_url == "https://api.deepseek.com"

    def test_handmade_config_degrades_to_single_client(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        client = RoutingClient(LLMConfig(api_key="k", model="m"))
        assert client._client_for("m") is client._client_for(None)

    def test_generate_image_routes_to_anthropic_client(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        _install_fake_sdk(monkeypatch, "anthropic", "AsyncAnthropic", {})
        config = _multi_config()
        config.models["claude-sonnet-4"] = {"name": "claude-sonnet-4", "provider": "anthropic"}
        config.providers["anthropic"] = ProviderConfig(
            name="anthropic", sdktype="anthropic", api_key="ka")
        client = RoutingClient(config)
        from llm.client import AnthropicClient
        assert isinstance(client._client_for("claude-sonnet-4"), AnthropicClient)

    @pytest.mark.asyncio
    async def test_generate_image_anthropic_model_raises(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        _install_fake_sdk(monkeypatch, "anthropic", "AsyncAnthropic", {})
        config = _multi_config()
        config.models["claude-sonnet-4"] = {"name": "claude-sonnet-4", "provider": "anthropic"}
        config.providers["anthropic"] = ProviderConfig(
            name="anthropic", sdktype="anthropic", api_key="ka")
        client = RoutingClient(config)
        with pytest.raises(LLMError, match="Anthropic 无图像生成"):
            await client.generate_image("p", model="claude-sonnet-4")


class TestFactory:
    def test_create_llm_client_returns_routing_client(self):
        assert isinstance(create_llm_client(LLMConfig(api_key="k")), RoutingClient)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest llm/tests/test_routing.py -q`
Expected: FAIL —— `ImportError: cannot import name 'RoutingClient'`

- [ ] **Step 3: 实现**

`llm/client.py`:把文件末尾「客户端工厂」段的 `create_llm_client` 整体替换为 `RoutingClient` + 新工厂:

```python
# ---------------------------------------------------------------------------
# 路由门面客户端
# ---------------------------------------------------------------------------


class RoutingClient:
    """按模型路由的客户端门面。

    每次调用按「模型 → models 注册表 provider 名 → provider 连接」选择客户端,
    同一 provider 惰性建连一次。模型未注册 / 未指明 provider / 手工配置
    (providers 表为空)→ 默认连接(config 顶层字段)——单 provider 配置行为
    与直连客户端一致。
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._clients: dict[str | None, Any] = {}

    def _client_for(self, model: str | None):
        pc = self.config.provider_for(model)
        key = pc.name if pc is not None else None
        if key not in self._clients:
            cfg = self.config.for_provider(pc) if pc is not None else self.config
            if cfg.provider == "anthropic":
                self._clients[key] = AnthropicClient(cfg)
            else:
                self._clients[key] = OpenAIClient(cfg)
        return self._clients[key]

    @property
    def ready(self) -> bool:
        """默认 provider 客户端就绪(惰性触发建连)。"""
        return self._client_for(None).ready

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        think: bool | dict | None = None,
        output_format: dict[str, Any] | None = None,
        notdo: list[str] | None = None,
        on_token: Callable[[str], None] | None = None,
        api_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """单轮调用(harness body 入口),按调用模型路由。"""
        return await self._client_for(model).complete(
            prompt,
            system=system,
            model=model,
            temperature=temperature,
            think=think,
            output_format=output_format,
            notdo=notdo,
            on_token=on_token,
            api_params=api_params,
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """多轮聊天(底层接口),按 config.model 路由。"""
        return await self._client_for(None).chat(messages, tools)

    async def generate_image(
        self,
        prompt: str,
        *,
        model: str | None = None,
        size: str | None = None,
        api_params: dict[str, Any] | None = None,
    ) -> ImageResult:
        """图像生成,按调用模型路由。"""
        return await self._client_for(model).generate_image(
            prompt, model=model, size=size, api_params=api_params,
        )

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()


def create_llm_client(config: LLMConfig) -> RoutingClient:
    """创建按模型路由的 LLM 客户端门面(原工厂签名不变,消费方透明切换)。"""
    return RoutingClient(config)
```

`llm/__init__.py` 整体替换为:

```python
"""LLM 模块"""

from .config import LLMConfig, ProviderConfig
from .client import (
    LLMError,
    AnthropicClient,
    OpenAIClient,
    RoutingClient,
    Message,
    LLMResponse,
    ImageResult,
    create_llm_client,
)
from .mock import MockLLMClient

__all__ = [
    "LLMConfig",
    "ProviderConfig",
    "LLMError",
    "AnthropicClient",
    "OpenAIClient",
    "RoutingClient",
    "Message",
    "LLMResponse",
    "ImageResult",
    "create_llm_client",
    "MockLLMClient",
]
```

- [ ] **Step 4: 跑 llm 全量测试确认通过**

Run: `python -m pytest llm/tests -q`
Expected: 全部 PASS(含既有 wiring/config_chain 测试——工厂签名未变,消费方透明)。

- [ ] **Step 5: 提交**

```bash
git add llm/client.py llm/__init__.py llm/tests/test_routing.py
git commit -m "feat(llm): RoutingClient 按模型路由门面 —— provider 惰性建连缓存,create_llm_client 透明切换"
```

---

### Task 6: HarnessConfig 图像字段 + 构造校验

**Files:**
- Modify: `module_harness/core/config.py`
- Test: `module_harness/tests/test_config.py`(追加)

- [ ] **Step 1: 写失败测试**

`module_harness/tests/test_config.py` 末尾追加(若该文件无以下 import 则补:`import pytest`、`from llm.client import ImageResult` 不需要——本任务只测 config;确保有 `import pytest` 与 `from module_harness.core.config import HarnessConfig`、`from module_harness.core.outputfmt import OutputFormat`):

```python
class TestImageModeConfig:
    """mode="image" 配置:字段默认、非法值与 output_format 互斥。"""

    def test_defaults(self):
        cfg = HarnessConfig(prompt_core="x")
        assert cfg.mode == "text"
        assert cfg.image_size is None
        assert cfg.image_dir == "images"

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError, match="mode"):
            HarnessConfig(prompt_core="x", mode="video")

    def test_image_mode_rejects_output_format(self):
        with pytest.raises(ValueError, match="互斥"):
            HarnessConfig(
                prompt_core="x",
                mode="image",
                output_format=OutputFormat(type="json_object"),
            )

    def test_image_mode_without_output_format_ok(self):
        cfg = HarnessConfig(prompt_core="画:{title}", mode="image")
        assert cfg.mode == "image"

    def test_from_task_definition_reads_image_fields(self):
        cfg = HarnessConfig.from_task_definition({
            "prompt_core": "画:{title}",
            "mode": "image",
            "image_size": "1024x1024",
            "image_dir": "out/imgs",
        })
        assert cfg.mode == "image"
        assert cfg.image_size == "1024x1024"
        assert cfg.image_dir == "out/imgs"

    def test_from_task_definition_image_rejects_outputformat(self):
        with pytest.raises(ValueError, match="互斥"):
            HarnessConfig.from_task_definition({
                "prompt_core": "画:{title}",
                "mode": "image",
                "outputformat": {"type": "json_object"},
            })

    def test_to_dict_from_dict_roundtrip(self):
        cfg = HarnessConfig(prompt_core="画:{title}", mode="image",
                            image_size="1024x1024", image_dir="d")
        data = cfg.to_dict()
        assert data["mode"] == "image"
        restored = HarnessConfig.from_dict(data)
        assert restored == cfg
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_config.py -q`
Expected: FAIL —— `TypeError: __init__() got an unexpected keyword argument 'mode'`

- [ ] **Step 3: 实现**

`module_harness/core/config.py` 修改:

(a) `HarnessConfig` 在 `api_params` 字段后追加:

```python
    # ── 调用形态 ──
    mode: str = "text"
    """调用形态："text" = chat 补全（默认）；"image" = 图像生成（文生图）。"""

    image_size: str | None = None
    """图像尺寸，如 "1024x1024"；None = API 默认。仅 mode="image" 生效。"""

    image_dir: str = "images"
    """图像产物落盘目录（cwd 相对）。仅 mode="image" 生效。"""
```

(b) 类内追加构造校验(dataclass 的 `__post_init__`):

```python
    def __post_init__(self) -> None:
        if self.mode not in ("text", "image"):
            raise ValueError(f"mode 须为 'text' | 'image'，得到 {self.mode!r}")
        if self.mode == "image" and self.output_format is not None:
            raise ValueError(
                "mode='image' 与 output_format 互斥（图像无文本输出格式可校验）"
            )
```

(c) `from_task_definition` 的 `return cls(...)` 追加三行:

```python
            mode=task.get("mode", "text"),
            image_size=task.get("image_size"),
            image_dir=task.get("image_dir", "images"),
```

(docstring 的预期键列表同步补 `mode / image_size / image_dir`。)

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_config.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add module_harness/core/config.py module_harness/tests/test_config.py
git commit -m "feat(harness): HarnessConfig 图像模式字段 —— mode/image_size/image_dir,image×output_format 互斥校验"
```

---

### Task 7: ImageSaved 事件 + Harness body 图像分支

**Files:**
- Modify: `module_harness/infra/events.py`、`module_harness/core/harness.py`
- Test: `module_harness/tests/test_harness.py`(追加)

- [ ] **Step 1: 写失败测试**

`module_harness/tests/test_harness.py` 修改与追加:

(a) 顶部 import 区补:

```python
from pathlib import Path

from llm.client import ImageResult, LLMError, LLMResponse
from module_harness.infra.events import ImageSaved
```

(文件现有 `from llm.client import LLMResponse` 在函数体内局部 import——顶部统一后,保留原局部 import 不动也无害,不强制删。)

(b) 追加 fixture 与用例(放进现有 `_make_view` 可见的模块级区域):

```python
@pytest.fixture
def mock_image_llm():
    client = MagicMock()
    client.generate_image = AsyncMock(
        return_value=ImageResult(data=b"\x89PNG\r\n\x1a\nfake-bytes",
                                 usage={"input_tokens": 3, "output_tokens": 100})
    )
    return client


class TestHarnessImageMode:
    """mode="image":渲染→生成→落盘→事件→返回路径。"""

    @pytest.mark.asyncio
    async def test_returns_path_and_writes_file(self, mock_image_llm, tmp_path):
        cfg = HarnessConfig(prompt_core="画:{title}", mode="image",
                            image_dir=str(tmp_path / "imgs"))
        h = Harness(cfg, mock_image_llm, EventBus())
        result = await h.build_body()(_make_view(title="封面"))
        p = Path(result)
        assert p.parent == tmp_path / "imgs"
        assert p.name.startswith("test_node-") and p.suffix == ".png"
        assert p.read_bytes() == b"\x89PNG\r\n\x1a\nfake-bytes"

    @pytest.mark.asyncio
    async def test_prompt_and_params_reach_client(self, mock_image_llm, tmp_path):
        cfg = HarnessConfig(prompt_core="画:{title}", mode="image",
                            image_size="1024x1024", image_dir=str(tmp_path))
        h = Harness(cfg, mock_image_llm, EventBus())
        await h.build_body()(_make_view(title="封面"))
        kw = mock_image_llm.generate_image.call_args.kwargs
        assert kw["prompt"] == "画:封面"
        assert kw["model"] is None          # HarnessConfig 未指定 model
        assert kw["size"] == "1024x1024"
        assert kw["api_params"] is None

    @pytest.mark.asyncio
    async def test_no_token_callback_and_no_output_validation(self, mock_image_llm, tmp_path):
        cfg = HarnessConfig(prompt_core="x", mode="image", image_dir=str(tmp_path))
        bus = EventBus()
        seen: list = []
        for evt in (LlmToken, OutputValidated):
            bus.subscribe(evt, lambda e: seen.append(e))
        h = Harness(cfg, mock_image_llm, bus)
        await h.build_body()(_make_view())
        assert seen == []                    # 图像模式不发 LlmToken / OutputValidated

    @pytest.mark.asyncio
    async def test_events_and_state(self, mock_image_llm, tmp_path):
        cfg = HarnessConfig(prompt_core="x", mode="image", image_dir=str(tmp_path))
        bus = EventBus()
        seen: list = []
        for evt in (PromptRendered, LlmCallStarted, LlmCallCompleted, ImageSaved):
            bus.subscribe(evt, lambda e: seen.append(e))
        h = Harness(cfg, mock_image_llm, bus)
        state: dict = {}
        view = NodeView(node="img_node", fields=(), values=(), state=state)
        result = await h.build_body()(view)
        types_seen = [type(e) for e in seen]
        assert types_seen == [PromptRendered, LlmCallStarted, LlmCallCompleted, ImageSaved]
        saved = seen[-1]
        assert saved.path == result
        assert saved.bytes_len == len(b"\x89PNG\r\n\x1a\nfake-bytes")
        assert state["_image_path"] == result
        assert state["_usage"] == {"input_tokens": 3, "output_tokens": 100}

    @pytest.mark.asyncio
    async def test_llm_error_maps_to_infrastructure_failure(self, mock_image_llm, tmp_path):
        mock_image_llm.generate_image.side_effect = LLMError("provider 挂了")
        cfg = HarnessConfig(prompt_core="x", mode="image", image_dir=str(tmp_path))
        bus = EventBus()
        seen: list = []
        bus.subscribe(HarnessFailed, lambda e: seen.append(e))
        h = Harness(cfg, mock_image_llm, bus)
        result = await h.build_body()(_make_view())
        assert isinstance(result, Failure)
        assert result.type == "infrastructure"
        assert seen and seen[0].failure_type == "infrastructure"

    @pytest.mark.asyncio
    async def test_disk_error_maps_to_infrastructure_failure(self, mock_image_llm):
        # image_dir 指向一个文件 → mkdir/write 必败
        blocker = tmp_path_file()
        cfg = HarnessConfig(prompt_core="x", mode="image", image_dir=str(blocker))
        h = Harness(cfg, mock_image_llm, EventBus())
        result = await h.build_body()(_make_view())
        assert isinstance(result, Failure)
        assert result.type == "infrastructure"


def tmp_path_file() -> Path:
    """cwd 下造一个文件占住 image_dir 位置(测试收尾由 pytest tmp 清理)。"""
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "blocker"
    f.write_text("i am a file")
    return f
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_harness.py -q`
Expected: FAIL —— `ImportError: cannot import name 'ImageSaved'`

- [ ] **Step 3: 实现**

(a) `module_harness/infra/events.py` 在 `OutputValidated` 之后追加:

```python
@dataclass
class ImageSaved(HarnessEvent):
    """图像产物已落盘（mode="image" harness）。"""
    path: str
    bytes_len: int
```

(b) `module_harness/core/harness.py`:

- 顶部 import:`from pathlib import Path`;events import 列表加 `ImageSaved`。
- `build_body` 内,body 函数的「2. 调用 LLM」段改造——在 `bus.emit(LlmCallStarted(...))` 之后、`def on_token` 之前插入图像分支(文本流程保持原样不动):

```python
            if config.mode == "image":
                return await _run_image(view, rendered, state)

```

- 然后在 `async def body(...)` 定义之前(build_body 内、validator 赋值之后)定义图像分支闭包:

```python
        async def _run_image(view: NodeView, rendered: str,
                             state: dict | None) -> Any:
            """图像模式:渲染好的 prompt → generate_image → 落盘 → 事件 → 返回路径。

            无 token 流(LlmToken 不发)、无文本校验(OutputValidated 不发);
            LLMError 与落盘 OSError 同归 infrastructure Failure。
            ``state`` 由 body 传入(body 内局部变量 ``state = view.state``,
            闭包不可见,必须显式传参)。
            """
            node = view.node
            try:
                from llm.client import LLMError

                result = await llm.generate_image(
                    prompt=rendered,
                    model=config.model,
                    size=config.image_size,
                    api_params=config.api_params if config.api_params else None,
                )
            except LLMError as e:
                if state is not None:
                    state["_llm_error"] = str(e)
                bus.emit(HarnessFailed(
                    timestamp=time.monotonic(), node=node, tick=0,
                    reason=str(e), failure_type="infrastructure",
                ))
                return Failure(str(e), type="infrastructure")

            out_dir = Path(config.image_dir)
            path = out_dir / f"{node}-{time.monotonic_ns()}.png"
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(result.data)
            except OSError as e:
                bus.emit(HarnessFailed(
                    timestamp=time.monotonic(), node=node, tick=0,
                    reason=f"图像落盘失败: {e}", failure_type="infrastructure",
                ))
                return Failure(f"图像落盘失败: {e}", type="infrastructure")

            if state is not None:
                state["_image_path"] = str(path)
                state["_usage"] = dict(result.usage)

            bus.emit(LlmCallCompleted(
                timestamp=time.monotonic(), node=node, tick=0,
                content_chars=len(result.data),
                usage=result.usage,
                finish_reason=None,
            ))
            bus.emit(ImageSaved(
                timestamp=time.monotonic(), node=node, tick=0,
                path=str(path), bytes_len=len(result.data),
            ))
            return str(path)
```

`_run_image` 与 body 同处 `build_body` 闭包,`config`/`llm`/`bus` 直接引用外层局部变量;`rendered` 与 `state` 是 body 内的局部值,经参数显式传入(见上面两处调用/签名)。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_harness.py -q`
Expected: 全部 PASS(既有文本模式用例无回归)。

- [ ] **Step 5: 跑 call_harness 回归确认零改动自动获得图像能力**

Run: `python -m pytest module_harness/tests/test_call.py -q`
Expected: PASS(无改动,纯回归)。

- [ ] **Step 6: 提交**

```bash
git add module_harness/infra/events.py module_harness/core/harness.py module_harness/tests/test_harness.py
git commit -m "feat(harness): body 图像模式分支 —— generate_image→落盘→ImageSaved 事件→返回路径,LLMError/落盘失败归 infrastructure"
```

---

### Task 8: TaskDefinition 三字段 + graph_builder 覆盖传播

**Files:**
- Modify: `module_harness/model/spec.py`、`module_harness/orchestrate/graph_builder.py`
- Test: `module_harness/tests/test_graph_builder.py`(追加)

- [ ] **Step 1: 写失败测试**

`module_harness/tests/test_graph_builder.py` 末尾追加:

```python
class TestImageModePropagation:
    """Task 级 mode/image_size/image_dir 覆盖传播到隔离注册的 HarnessConfig。"""

    def test_task_overrides_image_fields(self, mock_llm, reg):
        reg.harness("draw", HarnessConfig(prompt_core="画:{title}"))
        tl = Tasklist(
            tasks={
                "D": TaskDefinition(
                    type="harness", harness="draw",
                    mode="image", image_size="1024x1024", image_dir="out",
                ),
            },
            flow="[D]",
        )
        builder = TasklistTranslator(reg, module_id="m1")
        graph, out_reg = builder.build(tl)
        cfg = out_reg.harness_config("m1:D")
        assert cfg.mode == "image"
        assert cfg.image_size == "1024x1024"
        assert cfg.image_dir == "out"

    def test_task_without_image_fields_keeps_base(self, mock_llm, reg):
        reg.harness("draw", HarnessConfig(prompt_core="画:{title}",
                                          image_size="512x512"))
        tl = Tasklist(tasks={"D": TaskDefinition(type="harness", harness="draw")},
                      flow="[D]")
        builder = TasklistTranslator(reg, module_id="m1")
        _, out_reg = builder.build(tl)
        cfg = out_reg.harness_config("m1:D")
        assert cfg.mode == "text"
        assert cfg.image_size == "512x512"      # 未覆盖,保留基配置

    def test_image_mode_conflict_raises_at_build(self, mock_llm, reg):
        reg.harness("draw", HarnessConfig(prompt_core="画:{title}"))
        tl = Tasklist(
            tasks={"D": TaskDefinition(
                type="harness", harness="draw", mode="image",
                outputformat={"type": "json_object"},
            )},
            flow="[D]",
        )
        with pytest.raises(ValueError, match="互斥"):
            TasklistTranslator(reg, module_id="m1").build(tl)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_graph_builder.py::TestImageModePropagation -q`
Expected: FAIL —— `TypeError: __init__() got an unexpected keyword argument 'mode'`

- [ ] **Step 3: 实现**

(a) `module_harness/model/spec.py` `TaskDefinition` 在 `api_params` 字段后追加:

```python
    mode: str | None = None             # "image" = 图像生成节点
    image_size: str | None = None       # 图像尺寸覆盖
    image_dir: str | None = None        # 图像落盘目录覆盖
```

`from_dict` 的 `return cls(...)` 追加:

```python
            mode=d.get("mode"),
            image_size=d.get("image_size"),
            image_dir=d.get("image_dir"),
```

(b) `module_harness/orchestrate/graph_builder.py` `_register_harness` 的 `cfg = HarnessConfig(...)` 构造追加三行(与 model/temperature 同款覆盖语义):

```python
            mode=task.mode if task.mode is not None else existing.mode,
            image_size=task.image_size if task.image_size is not None else existing.image_size,
            image_dir=task.image_dir if task.image_dir is not None else existing.image_dir,
```

(注:base harness 为 text 而本 task mode="image" 且基配置带 output_format 时,构造 HarnessConfig 会因互斥校验抛 ValueError——这正是 `test_image_mode_conflict_raises_at_build` 的预期,build 期即失败。)

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_graph_builder.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add module_harness/model/spec.py module_harness/orchestrate/graph_builder.py module_harness/tests/test_graph_builder.py
git commit -m "feat(harness): tasklist Task 增 mode/image_size/image_dir —— graph_builder 覆盖传播,互斥冲突 build 期暴露"
```

---

### Task 9: 文档同步

**Files:**
- Modify: `docs/guides/config-guide.md`、`docs/references/spec-harness-syntax.md`、`config.example.json`

- [ ] **Step 1: config-guide.md**

(a) models 字段表那一行(`| models[] | models | 模型能力注册表:...`)改为:

```markdown
| `models[]` | models | 模型能力注册表：`{name, provider, think, multimodal, image_gen, max_tokens}`——`think`/`multimodal`/`image_gen` 供客户端能力判断 |
```

(b) 「from_env 取 providers[0] 为当前 provider」一段改为:

```markdown
`from_env` 解析**全部** providers 按名注册；顶层连接字段取 `providers[0]`（默认
provider），`models[0]` 为默认 model。**按模型路由**：每次 LLM 调用查 models
注册表该模型的 `provider` 名 → 对应 provider 连接（未注册/未指明 → 默认连接）。
chat 用 A 家、生图用 B 家由此可配。**providers 为空 → `ValueError`**（"请参照
config.example.json 配置"）——框架不猜。
```

(c) 示例 JSON(40 行附近代码块)追加第二条 provider 与生图模型:

```json
  "providers": [
    {"name": "deepseek", "sdktype": "openai", "base_url": "https://api.deepseek.com",
     "api_key_env": "DEEPSEEK_API_KEY", "timeout": 120, "max_retries": 3},
    {"name": "zhipu", "sdktype": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "api_key_env": "ZHIPU_API_KEY", "timeout": 120, "max_retries": 3}
  ],
  "models": [
    {"name": "deepseek-v4-flash", "provider": "deepseek",
     "think": true, "multimodal": false, "max_tokens": 1000000},
    {"name": "cogview-4", "provider": "zhipu",
     "image_gen": true, "max_tokens": 4096}
  ]
```

- [ ] **Step 2: spec-harness-syntax.md**

(a) Task 字段表(约 94-99 行)追加三行:

```markdown
| `mode` | `str \| None` | 调用形态覆盖：`"image"` = 图像生成节点（缺省 text）。与 `outputformat` 互斥，同设 → 构建期 `ValueError` |
| `image_size` | `str \| None` | 图像尺寸覆盖，如 `"1024x1024"`；仅 `mode="image"` 生效 |
| `image_dir` | `str \| None` | 图像落盘目录覆盖（cwd 相对）；图像节点输出为**文件路径字符串** |
```

(b) 在 api_params 示例附近追加一个图像 task 示例:

```python
# 图像生成节点：mode="image"，输出为落盘路径（下游节点消费路径字符串）
"draw_cover": {
    "harness": "draw",
    "mode": "image",
    "image_size": "1024x1024",
    "inputs": {"title": "outline"},
}
```

- [ ] **Step 3: config.example.json**

providers 数组**末尾**追加(不動 providers[0],默认行为不变)、models 数组**末尾**追加:

```json
    {
      "name": "zhipu",
      "sdktype": "openai",
      "base_url": "https://open.bigmodel.cn/api/paas/v4",
      "api_key_env": "ZHIPU_API_KEY",
      "timeout": 120,
      "max_retries": 3
    }
```

```json
    {
      "name": "cogview-4",
      "provider": "zhipu",
      "image_gen": true,
      "max_tokens": 4096
    }
```

- [ ] **Step 4: 校验 example JSON 合法**

Run: `python -c "import json; json.load(open('config.example.json', encoding='utf-8')); print('ok')"`
Expected: `ok`

- [ ] **Step 5: 提交**

```bash
git add docs/guides/config-guide.md docs/references/spec-harness-syntax.md config.example.json
git commit -m "docs: 图生成配置文档 —— image_gen 能力位/多 provider 路由/tasklist 图像字段,example 追加生图 provider"
```

---

### Task 10: 全量回归

**Files:** 无新改动

- [ ] **Step 1: 全量测试**

Run: `python -m pytest llm/tests module_harness/tests -q`
Expected: 全部 PASS,零失败零 error。(smoke/benchmarks 默认排除,无需处理。)

- [ ] **Step 2: 冒烟验证 mock 链路端到端(免网络)**

Run:

```bash
python - <<'EOF'
import asyncio, tempfile
from unittest.mock import AsyncMock, MagicMock
from llm.client import ImageResult
from module_harness.core.config import HarnessConfig
from module_harness.core.call import call_harness
from module_harness.infra.events import EventBus

async def main():
    llm = MagicMock()
    llm.generate_image = AsyncMock(return_value=ImageResult(data=b"\x89PNGfake"))
    d = tempfile.mkdtemp()
    cfg = HarnessConfig(prompt_core="画:{title}", mode="image", image_dir=d)
    r = await call_harness(cfg, {"title": "封面"}, llm_client=llm)
    assert r.value.startswith(d) and r.value.endswith(".png")
    print("call_harness 图像模式 OK →", r.value)

asyncio.run(main())
EOF
```

Expected: 打印 `call_harness 图像模式 OK → <路径>`(task 级地板零改动自动获得图像能力的实证)。

- [ ] **Step 3: 收尾提交(如有尚未提交的边角)**

```bash
git status --short
```

Expected: 干净(除既有的计划/spec 文档)。若计划文档本身未提交:

```bash
git add docs/dev/superpowers/plans/2026-09-14-image-generation.md
git commit -m "docs: 图生成接口适配实施计划"
```
