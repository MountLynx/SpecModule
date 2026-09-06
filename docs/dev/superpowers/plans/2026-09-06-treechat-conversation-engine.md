# TreeChat 对话引擎实现计划（V1）

> ⚠️ **实施修订注记（2026-09-06，执行后回写）**：执行中发现并修正了 9 处计划缺陷，实现以修正后为准
> （详见仓库 TreeChat 提交历史 f4d1945..7aa25d8）：① config.py 加 `__post_init__`（str→Path）；②
> CardCreate 事件加 `created_at` 字段（重放同一性）；③ test_reopen 断言改 `pointer==a`（set_pointer
> 不落事件，重放指针=文件序末 assistant）；④ 窗口测试预算 200 / history 末条 msg7（原值永不触发
> 丢弃 / off-by-one）；⑤ branch_segment 断言含 user fork 自身（[2,4,5]/[2,6,7]）+ 补 assistant-fork
> 排除测试 + test_list_sessions monkeypatch create_client + session.create/open 的 window=None 归一
> 默认策略；⑥ SessionStore.append 自动 mkdir 父目录；⑦ /branch 非 int 转 TreeChatError；⑧
> test_repl_card_show 改两段式 REPL（卡片 id 运行时才产生）；⑨ CLI 测试 create/open 均需
> monkeypatch create_client（环境无 config.json）。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在独立新仓库实现 TreeChat V1——事件溯源消息树（分支/叶子一等公民）+ 卡片式上下文产出 + 薄 REPL CLI，LLM 层复用 SpecModule（`llm` 客户端 + `call_harness`）。

**Architecture:** 会话 = JSONL 追加事件日志（7 种事件，分支/叶子语义在 `parent` 指针里）；内存态 = 重放派生视图（消息 DAG + 指针 + 卡片注册表，主干 = 最长路径视图规则）。`core/` 零 SpecModule import；`llm_bridge.py` 是唯一依赖面（chat 轮次 + 卡片提炼）；`session.py` 门面组合两者；CLI 为 stdlib REPL。设计依据：`docs/dev/superpowers/specs/2026-09-06-treechat-conversation-engine-design.md`（含 nanobot 对照）。

**Tech Stack:** Python ≥3.10（本机 3.13）、pytest、specmodule≥0.2.0（提供 `llm` 与 `module_harness.call_harness`）。

**约定（全部任务适用）：**
- 新仓库根：`C:\Users\xingy\Desktop\开发\TreeChat`（若用户指定了其他路径，全局替换即可）
- 所有命令在新仓库根执行；测试命令 `python -m pytest tests/ -q`；异步代码测试用 `asyncio.run(...)` 包装（**不引入 pytest-asyncio**）
- 每个文件 `from __future__ import annotations`；数据模型用 `@dataclass`；公共签名有类型注解；`__init__.py` 显式 `__all__`；中文 docstring
- 红线：`treechat/core/` 内**禁止 import `llm` / `module_harness`**（唯一例外点 `treechat/llm_bridge.py`）

---

## 文件结构总览

```
TreeChat/
  pyproject.toml                 # deps: specmodule；console_script: treechat
  README.md
  .gitignore
  treechat/
    __init__.py                  # __all__ 公共 API
    config.py                    # TreeChatConfig（data_dir/budget_tokens/model）
    core/
      __init__.py
      errors.py                  # TreeChatError / EventFormatError
      events.py                  # 7 种事件 dataclass + to_dict/from_dict 校验
      store.py                   # SessionStore（append+fsync / load 撕裂尾分级）+ read_session_meta
      cards.py                   # Card + CardRegistry（默认 pinned）
      conversation.py            # Conversation（追加/重放/指针/path_to/trunk/fork_point）
      context.py                 # assemble + TokenWindowStrategy + WindowStrategy 协议
    llm_bridge.py                # 唯一 import specmodule 处（chat_turn / extract_card / create_client）
    session.py                   # TreeChatSession 门面 + list_sessions
    cli/
      __init__.py
      treeview.py                # /tree ASCII 渲染（纯展示）
      commands.py                # 斜杠命令路由（async）
      repl.py                    # main() 非交互 + run_repl() 主循环
  tests/
    conftest.py                  # FakeChatClient / FakeCardClient / session 工厂
    test_smoke.py
    test_events.py
    test_store.py
    test_cards.py
    test_conversation.py
    test_context.py
    test_llm_bridge.py
    test_session.py
    test_cli.py
```

---

### Task 1: 新仓库脚手架

**Files:**
- Create: `pyproject.toml`、`.gitignore`、`README.md`、`treechat/__init__.py`、`treechat/core/__init__.py`、`treechat/cli/__init__.py`、`treechat/config.py`、`tests/test_smoke.py`

- [ ] **Step 1: 创建目录与 git**

```bash
mkdir -p "C:/Users/xingy/Desktop/开发/TreeChat" && cd "C:/Users/xingy/Desktop/开发/TreeChat"
git init
mkdir -p treechat/core treechat/cli tests
```

- [ ] **Step 2: 写 `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "treechat"
version = "0.1.0"
description = "对话树一等公民 + 卡片式上下文产出的可嵌入对话引擎"
readme = "README.md"
requires-python = ">=3.10"
license = { text = "MIT" }
dependencies = [
    # LLM 层复用：llm 客户端（chat 多轮）+ module_harness.call_harness（卡片提炼）
    "specmodule>=0.2.0,<0.3",
]

[project.optional-dependencies]
dev = ["pytest"]

[project.scripts]
treechat = "treechat.cli.repl:main"

[tool.setuptools.packages.find]
include = ["treechat*"]
```

- [ ] **Step 3: 写 `.gitignore`、`README.md`、包骨架**

`.gitignore`：

```
__pycache__/
*.egg-info/
.pytest_cache/
dist/
build/
.venv/
```

`README.md`：

```markdown
# TreeChat

对话树一等公民 + 卡片式上下文产出的可嵌入对话引擎（V1 开发中）。

- 会话 = 追加式事件日志（JSONL），分支/叶子是 `parent` 指针的原生语义
- 卡片 = 结构化 LLM 提炼的上下文产出，pinned 后注入后续轮次
- LLM 层复用 [SpecModule](https://pypi.org/project/specmodule/)（配置回退链 / 输出校验）

## 安装（开发态）

    pip install -e ".[dev]"
    python -m pytest tests/ -q

配置复用 SpecModule 的回退链：项目根 `config.json`/`.env` → `~/.specmodule`。
```

`treechat/__init__.py`（占位，Task 8 填充 `__all__`）：

```python
"""TreeChat —— 对话树一等公民的可嵌入对话引擎。"""
```

`treechat/core/__init__.py` 与 `treechat/cli/__init__.py`：空 docstring 文件（`"""core 层：零 SpecModule 依赖。"""` / `"""CLI。"""`）。

`treechat/config.py`：

```python
"""TreeChatConfig —— 运行配置。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TreeChatConfig:
    """会话存储位置与默认参数。

    data_dir 默认 `~/.treechat`，测试/嵌入方按需覆盖。
    """

    data_dir: Path = field(default_factory=lambda: Path.home() / ".treechat")
    budget_tokens: int = 100_000
    model: str | None = None
    """默认模型覆盖；None = 走 SpecModule 配置链的默认模型。"""

    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"
```

`tests/test_smoke.py`：

```python
"""脚手架冒烟：包可导入、配置可构造。"""
from treechat.config import TreeChatConfig


def test_import_and_config():
    cfg = TreeChatConfig(data_dir=".")
    assert cfg.sessions_dir().name == "sessions"
```

- [ ] **Step 4: 安装并验证**

```bash
python -m pip install -e ".[dev]"
python -m pytest tests/ -q
```

Expected: `1 passed`（specmodule≥0.2.0 已安装；若缺，先 `pip install "specmodule>=0.2.0"`）

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore: 仓库脚手架 — pyproject/包骨架/config/冒烟测试"
```

---

### Task 2: core/errors + core/events —— 事件模型与序列化

**Files:**
- Create: `treechat/core/errors.py`、`treechat/core/events.py`
- Test: `tests/test_events.py`

- [ ] **Step 1: 写失败测试 `tests/test_events.py`**

```python
"""事件模型：roundtrip + 严格校验（无隐式行为）。"""
import pytest

from treechat.core.errors import EventFormatError
from treechat.core.events import (
    AssistantMsg, CardCreate, Pin, SessionMeta, SystemUpdate, Unpin, UserMsg,
    event_from_dict, event_to_dict,
)


def test_roundtrip_all_types():
    cases = [
        SessionMeta(name="n1", created_at="2026-09-06T00:00:00+00:00", system="s"),
        UserMsg(parent=None, text="hi"),
        UserMsg(parent=2, text="branch"),
        AssistantMsg(parent=2, text="ok", model="m1", usage={"input_tokens": 1}),
        SystemUpdate(text="new rules"),
        CardCreate(card_id="card_ab12", title="t", body="b", from_path=[1, 2], instruction="i"),
        Pin(card_id="card_ab12"),
        Unpin(card_id="card_ab12"),
    ]
    for ev in cases:
        d = event_to_dict(seq=7, event=ev)
        assert d["seq"] == 7
        seq, back = event_from_dict(d)
        assert seq == 7
        assert back == ev


def test_unknown_type_rejected():
    with pytest.raises(EventFormatError, match="未知事件类型"):
        event_from_dict({"seq": 1, "type": "magic"})


def test_missing_field_rejected():
    with pytest.raises(EventFormatError, match="缺字段"):
        event_from_dict({"seq": 1, "type": "user_msg", "parent": None})


def test_extra_field_rejected():
    with pytest.raises(EventFormatError, match="多余字段"):
        event_from_dict({"seq": 1, "type": "pin", "card_id": "c", "junk": 1})


def test_missing_seq_or_type_rejected():
    with pytest.raises(EventFormatError):
        event_from_dict({"type": "pin", "card_id": "c"})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_events.py -q`
Expected: FAIL（`ModuleNotFoundError: treechat.core.errors`）

- [ ] **Step 3: 实现 `treechat/core/errors.py` 与 `treechat/core/events.py`**

`errors.py`：

```python
"""TreeChat 异常层级。"""
from __future__ import annotations


class TreeChatError(RuntimeError):
    """TreeChat 基础异常（显式失败，不静默兜底）。"""


class EventFormatError(TreeChatError):
    """事件格式非法（未知类型 / 缺字段 / 多余字段 / 损坏行 / seq 断裂）。"""
```

`events.py`：

```python
"""事件模型 —— 7 种事件 + 严格序列化。

type 字符串（snake_case）与 dataclass 一一对应；from_dict 严格校验
字段集（缺字段/多余字段/未知类型一律 EventFormatError），不做隐式补全。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields as dc_fields
from typing import Any

from .errors import EventFormatError


@dataclass
class SessionMeta:
    name: str
    created_at: str
    system: str = ""


@dataclass
class UserMsg:
    parent: int | None
    text: str


@dataclass
class AssistantMsg:
    parent: int
    text: str
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class SystemUpdate:
    text: str


@dataclass
class CardCreate:
    card_id: str
    title: str
    body: str
    from_path: list[int]
    instruction: str = ""


@dataclass
class Pin:
    card_id: str


@dataclass
class Unpin:
    card_id: str


_EVENT_TYPES: dict[str, type] = {
    "session_meta": SessionMeta,
    "user_msg": UserMsg,
    "assistant_msg": AssistantMsg,
    "system_update": SystemUpdate,
    "card_create": CardCreate,
    "pin": Pin,
    "unpin": Unpin,
}


def event_to_dict(seq: int, event: object) -> dict[str, Any]:
    """事件 → {"seq", "type", **字段}。未知事件对象抛 EventFormatError。"""
    for type_name, cls in _EVENT_TYPES.items():
        if isinstance(event, cls):
            d: dict[str, Any] = {"seq": seq, "type": type_name}
            d.update(asdict(event))
            return d
    raise EventFormatError(f"未知事件对象: {event!r}")


def event_from_dict(d: dict[str, Any]) -> tuple[int, object]:
    """dict → (seq, event)。严格校验字段集。"""
    if not isinstance(d, dict) or "seq" not in d or "type" not in d:
        raise EventFormatError(f"事件缺 seq/type 字段: {d!r}")
    type_name = d["type"]
    cls = _EVENT_TYPES.get(type_name)
    if cls is None:
        raise EventFormatError(f"未知事件类型: {type_name!r} (seq={d['seq']})")
    names = {f.name for f in dc_fields(cls)}
    kwargs = {k: v for k, v in d.items() if k not in ("seq", "type")}
    missing = names - kwargs.keys()
    if missing:
        raise EventFormatError(f"事件 {type_name} 缺字段 {sorted(missing)} (seq={d['seq']})")
    extra = kwargs.keys() - names
    if extra:
        raise EventFormatError(f"事件 {type_name} 多余字段 {sorted(extra)} (seq={d['seq']})")
    return d["seq"], cls(**kwargs)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_events.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add treechat/core/errors.py treechat/core/events.py tests/test_events.py
git commit -m "feat(core): 7 种事件模型 + 严格序列化校验"
```

---

### Task 3: core/store —— JSONL 追加日志与加载

**Files:**
- Create: `treechat/core/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: 写失败测试 `tests/test_store.py`**

```python
"""SessionStore：追加/加载/撕裂尾分级/seq 连续性/首行元数据。"""
import json

import pytest

from treechat.core.errors import EventFormatError
from treechat.core.events import Pin, SessionMeta, UserMsg
from treechat.core.store import SessionStore, read_session_meta


def _meta(name="s1"):
    return SessionMeta(name=name, created_at="2026-09-06T00:00:00+00:00", system="")


def test_append_assigns_sequential_seq_and_load_roundtrip(tmp_path):
    store = SessionStore(tmp_path / "s.jsonl")
    s1 = store.append(_meta())
    s2 = store.append(UserMsg(parent=None, text="hi"))
    assert (s1, s2) == (1, 2)
    events = SessionStore(tmp_path / "s.jsonl").load()
    assert [seq for seq, _ in events] == [1, 2]
    assert events[0][1] == _meta()
    assert events[1][1] == UserMsg(parent=None, text="hi")


def test_append_continues_after_reopen(tmp_path):
    p = tmp_path / "s.jsonl"
    SessionStore(p).append(_meta())
    store = SessionStore(p)
    assert store.append(UserMsg(parent=None, text="x")) == 2


def test_torn_tail_last_line_ignored_with_warning(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"seq": 1, "type": "session_meta", "name": "s",
                             "created_at": "t", "system": ""}) + "\n"
                 + '{"seq":2,"type":"user_msg","par', encoding="utf-8")
    with pytest.warns(UserWarning, match="末行不完整"):
        events = SessionStore(p).load()
    assert len(events) == 1


def test_corrupt_middle_line_hard_error_with_lineno(tmp_path):
    p = tmp_path / "s.jsonl"
    lines = [
        json.dumps({"seq": 1, "type": "session_meta", "name": "s", "created_at": "t", "system": ""}),
        "{not json",
        json.dumps({"seq": 3, "type": "pin", "card_id": "c"}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(EventFormatError, match="第 2 行"):
        SessionStore(p).load()


def test_wellformed_but_invalid_last_line_still_hard_error(tmp_path):
    p = tmp_path / "s.jsonl"
    lines = [
        json.dumps({"seq": 1, "type": "session_meta", "name": "s", "created_at": "t", "system": ""}),
        json.dumps({"seq": 2, "type": "magic"}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(EventFormatError, match="未知事件类型"):
        SessionStore(p).load()


def test_seq_gap_hard_error(tmp_path):
    p = tmp_path / "s.jsonl"
    lines = [
        json.dumps({"seq": 1, "type": "session_meta", "name": "s", "created_at": "t", "system": ""}),
        json.dumps({"seq": 3, "type": "pin", "card_id": "c"}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(EventFormatError, match="seq 不连续"):
        SessionStore(p).load()


def test_read_session_meta(tmp_path):
    p = tmp_path / "s.jsonl"
    SessionStore(p).append(_meta(name="会话甲"))
    assert read_session_meta(p).name == "会话甲"


def test_read_session_meta_rejects_non_meta_first_line(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"seq": 1, "type": "pin", "card_id": "c"}) + "\n", encoding="utf-8")
    with pytest.raises(EventFormatError, match="首行不是 session_meta"):
        read_session_meta(p)


def test_load_empty_and_missing_file(tmp_path):
    assert SessionStore(tmp_path / "nope.jsonl").load() == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_store.py -q`
Expected: FAIL（`ModuleNotFoundError: treechat.core.store`）

- [ ] **Step 3: 实现 `treechat/core/store.py`**

```python
"""会话存储 —— JSONL 追加日志（append + fsync）+ 分级加载。

完整性策略（spec §2.3）：
- 末行 JSON 不完整（崩溃撕裂尾）→ UserWarning 并忽略
- 中间行 JSON 损坏 / 行内容非法（未知类型、缺字段）/ seq 不连续 → EventFormatError 硬报错带行号
"""
from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

from .errors import EventFormatError
from .events import SessionMeta, event_from_dict, event_to_dict


class SessionStore:
    """一个会话的 JSONL 事件文件。进程内顺序追加（V1 单进程，无文件锁）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._last_seq = 0
        self._loaded = False

    def load(self) -> list[tuple[int, object]]:
        """加载并返回全部 (seq, event)。空/缺失文件返回 []。"""
        events: list[tuple[int, object]] = []
        if not self.path.exists():
            self._loaded = True
            return events
        with open(self.path, encoding="utf-8") as f:
            lines = f.readlines()
        total = len(lines)
        for lineno, raw in enumerate(lines, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                d = json.loads(stripped)
            except json.JSONDecodeError as exc:
                if lineno == total:
                    warnings.warn(
                        f"会话文件末行不完整（可能为崩溃残留），已忽略: {self.path}",
                        stacklevel=2,
                    )
                    break
                raise EventFormatError(f"第 {lineno} 行 JSON 损坏: {exc}") from exc
            try:
                seq, event = event_from_dict(d)
            except EventFormatError as exc:
                raise EventFormatError(f"第 {lineno} 行事件非法: {exc}") from exc
            expected = events[-1][0] + 1 if events else 1
            if seq != expected:
                raise EventFormatError(f"第 {lineno} 行 seq 不连续：得到 {seq}，期望 {expected}")
            events.append((seq, event))
        self._last_seq = events[-1][0] if events else 0
        self._loaded = True
        return events

    def append(self, event: object) -> int:
        """追加事件（fsync 持久化），返回分配的 seq。"""
        if not self._loaded:
            self.load()
        seq = self._last_seq + 1
        line = json.dumps(event_to_dict(seq, event), ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._last_seq = seq
        return seq


def read_session_meta(path: Path) -> SessionMeta:
    """读首行 session_meta（会话枚举用；不做全文件校验，打开时才全量 load）。"""
    with open(path, encoding="utf-8") as f:
        first = f.readline().strip()
    if not first:
        raise EventFormatError(f"空会话文件: {path}")
    seq, event = event_from_dict(json.loads(first))
    if not isinstance(event, SessionMeta):
        raise EventFormatError(f"首行不是 session_meta: {path}")
    return event
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_store.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5: Commit**

```bash
git add treechat/core/store.py tests/test_store.py
git commit -m "feat(core): SessionStore JSONL 追加日志 — fsync/撕裂尾分级/seq 连续校验"
```

---

### Task 4: core/cards —— 卡片与注册表

**Files:**
- Create: `treechat/core/cards.py`
- Test: `tests/test_cards.py`

- [ ] **Step 1: 写失败测试 `tests/test_cards.py`**

```python
"""CardRegistry：默认 pinned、pin/unpin、未知 id 显式报错。"""
import pytest

from treechat.core.cards import Card, CardRegistry
from treechat.core.errors import TreeChatError


def _card(cid="card_0001", title="t", body="b"):
    return Card(id=cid, title=title, body=body, from_path=[1], instruction="")


def test_add_defaults_pinned():
    reg = CardRegistry()
    reg.add(_card())
    assert [c.id for c in reg.pinned_cards()] == ["card_0001"]


def test_add_opt_out_pin_and_repin():
    reg = CardRegistry()
    reg.add(_card(), pinned=False)
    assert reg.pinned_cards() == []
    reg.pin("card_0001")
    assert len(reg.pinned_cards()) == 1
    reg.unpin("card_0001")
    assert reg.pinned_cards() == []


def test_unknown_id_raises():
    reg = CardRegistry()
    with pytest.raises(TreeChatError, match="未知卡片"):
        reg.pin("card_nope")
    with pytest.raises(TreeChatError, match="未知卡片"):
        reg.get("card_nope")


def test_duplicate_id_raises():
    reg = CardRegistry()
    reg.add(_card())
    with pytest.raises(TreeChatError, match="重复"):
        reg.add(_card())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_cards.py -q`
Expected: FAIL（`ModuleNotFoundError: treechat.core.cards`）

- [ ] **Step 3: 实现 `treechat/core/cards.py`**

```python
"""卡片模型与注册表。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import TreeChatError


@dataclass
class Card:
    """上下文产出卡片。body 必须自包含（提炼 prompt 的硬约束）。"""

    id: str
    title: str
    body: str
    from_path: list[int] = field(default_factory=list)
    instruction: str = ""
    created_at: str = ""


class CardRegistry:
    """id → Card + pinned 状态。card_create 事件默认 pinned（spec §3.3）。"""

    def __init__(self) -> None:
        self._cards: dict[str, Card] = {}
        self._pinned: set[str] = set()

    def add(self, card: Card, *, pinned: bool = True) -> None:
        if card.id in self._cards:
            raise TreeChatError(f"卡片 id 重复: {card.id}")
        self._cards[card.id] = card
        if pinned:
            self._pinned.add(card.id)

    def pin(self, card_id: str) -> None:
        self._require(card_id)
        self._pinned.add(card_id)

    def unpin(self, card_id: str) -> None:
        self._require(card_id)
        self._pinned.discard(card_id)

    def get(self, card_id: str) -> Card:
        self._require(card_id)
        return self._cards[card_id]

    def pinned_cards(self) -> list[Card]:
        return [c for cid, c in self._cards.items() if cid in self._pinned]

    def all_cards(self) -> list[Card]:
        return list(self._cards.values())

    def ids(self) -> set[str]:
        return set(self._cards)

    def _require(self, card_id: str) -> None:
        if card_id not in self._cards:
            raise TreeChatError(f"未知卡片: {card_id}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_cards.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add treechat/core/cards.py tests/test_cards.py
git commit -m "feat(core): Card + CardRegistry — card_create 默认 pinned"
```

---

### Task 5: core/conversation —— 重放派生视图（消息 DAG / 指针 / 主干）

**Files:**
- Create: `treechat/core/conversation.py`
- Test: `tests/test_conversation.py`

- [ ] **Step 1: 写失败测试 `tests/test_conversation.py`**

```python
"""Conversation：三种 parent 语义 / 指针 / path_to / trunk / fork_point / 卡片事件 / 重放。"""
import pytest

from treechat.core.cards import Card
from treechat.core.conversation import Conversation
from treechat.core.errors import TreeChatError


def _conv(tmp_path, name="t"):
    return Conversation.create(tmp_path / "s.jsonl", name=name, system="sys0")


def test_create_and_first_user_is_root(tmp_path):
    conv = _conv(tmp_path)
    assert (conv.name, conv.system, conv.pointer) == ("t", "sys0", None)
    seq = conv.append_user("第一句")
    assert conv.nodes[seq].parent is None
    assert conv.pointer is None  # 指针只在 assistant 后推进


def test_turn_appends_and_pointer_advances(tmp_path):
    conv = _conv(tmp_path)
    u = conv.append_user("q")
    a = conv.append_assistant(u, "a", model="m")
    assert conv.pointer == a
    assert conv.nodes[a].parent == u
    u2 = conv.append_user("q2")
    assert conv.nodes[u2].parent == a


def test_branch_semantics_via_set_pointer(tmp_path):
    conv = _conv(tmp_path)
    u = conv.append_user("q")
    a = conv.append_assistant(u, "a")
    conv.set_pointer(u)  # 回到 user 节点开分支
    b = conv.append_user("追问")
    assert conv.nodes[b].parent == u


def test_leaf_creates_second_root(tmp_path):
    conv = _conv(tmp_path)
    conv.append_user("q")
    leaf = conv.append_user("概念提问", leaf=True)
    assert conv.nodes[leaf].parent is None


def test_append_assistant_requires_user_target(tmp_path):
    conv = _conv(tmp_path)
    u = conv.append_user("q")
    a = conv.append_assistant(u, "a")
    with pytest.raises(TreeChatError, match="user 节点"):
        conv.append_assistant(a, "x")


def test_path_to_and_trunk_longest_wins(tmp_path):
    conv = _conv(tmp_path)
    u1 = conv.append_user("q1")
    a1 = conv.append_assistant(u1, "a1")
    u2 = conv.append_user("q2")
    a2 = conv.append_assistant(u2, "a2")          # 主干：1-2-3-4
    conv.set_pointer(u1)
    b1 = conv.append_user("分支问")               # 分支：1-5
    conv.append_assistant(b1, "分支答")           # 1-5-6
    assert [n.seq for n in conv.path_to(a2)] == [u1, a1, u2, a2]
    assert conv.trunk_end() == a2                 # 4 节点 > 3 节点
    assert conv.nodes[conv.trunk_end()].role == "assistant"


def test_trunk_tie_prefers_newer_end(tmp_path):
    conv = _conv(tmp_path)
    u1 = conv.append_user("q1")
    a1 = conv.append_assistant(u1, "a1")          # 路径1：2 节点
    conv.set_pointer(None)
    u2 = conv.append_user("leaf", leaf=True)
    conv.append_assistant(u2, "a2")               # 路径2：2 节点，末端更新
    assert conv.trunk_end() == conv.pointer


def test_fork_point(tmp_path):
    conv = _conv(tmp_path)
    u1 = conv.append_user("q")
    a1 = conv.append_assistant(u1, "a")
    u2 = conv.append_user("q2")
    conv.set_pointer(a1)
    b1 = conv.append_user("分支问")
    assert conv.fork_point(u2) == a1              # a1 有两个子节点
    assert conv.fork_point(b1) == a1
    assert conv.fork_point(a1) is None            # 不含自身


def test_cards_default_pinned_and_pin_events(tmp_path):
    conv = _conv(tmp_path)
    u = conv.append_user("q")
    conv.append_assistant(u, "a")
    cid = conv.add_card("标题", "正文", from_path=[u, conv.pointer], instruction="总结")
    assert [c.id for c in conv.cards.pinned_cards()] == [cid]
    conv.unpin(cid)
    assert conv.cards.pinned_cards() == []
    conv.pin(cid)
    assert len(conv.cards.pinned_cards()) == 1


def test_system_update_event(tmp_path):
    conv = _conv(tmp_path)
    conv.update_system("新指令")
    assert conv.system == "新指令"


def test_reopen_replays_identical_view(tmp_path):
    p = tmp_path / "s.jsonl"
    conv = Conversation.create(p, name="t", system="s")
    u = conv.append_user("q")
    a = conv.append_assistant(u, "a", model="m")
    cid = conv.add_card("t", "b", from_path=[u, a])
    conv.unpin(cid)
    conv.update_system("s2")
    conv.set_pointer(u)
    conv2 = Conversation.open(p)
    assert conv2.name == "t" and conv2.system == "s2"
    assert conv2.pointer == u
    assert set(conv2.nodes) == set(conv.nodes)
    assert conv2.cards.get(cid) == conv.cards.get(cid)
    assert conv2.cards.pinned_cards() == []


def test_unanswered_user(tmp_path):
    conv = _conv(tmp_path)
    u = conv.append_user("q")                      # 悬而未答
    assert conv.unanswered_user() == u
    a = conv.append_assistant(u, "a")
    assert conv.unanswered_user() is None
    leaf = conv.append_user("q2", leaf=True)       # 新悬而未答叶子
    assert conv.unanswered_user() == leaf
    conv.append_assistant(leaf, "a2")
    assert conv.unanswered_user() is None


def test_replay_rejects_dangling_parent(tmp_path):
    p = tmp_path / "s.jsonl"
    Conversation.create(p, name="t")
    store_lines = p.read_text(encoding="utf-8")
    p.write_text(store_lines + '{"seq":2,"type":"assistant_msg","parent":9,"text":"x","model":"","usage":{}}\n',
                 encoding="utf-8")
    with pytest.raises(TreeChatError, match="parent"):
        Conversation.open(p)


def test_set_pointer_unknown_node_raises(tmp_path):
    conv = _conv(tmp_path)
    with pytest.raises(TreeChatError, match="指针目标"):
        conv.set_pointer(99)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_conversation.py -q`
Expected: FAIL（`ModuleNotFoundError: treechat.core.conversation`）

- [ ] **Step 3: 实现 `treechat/core/conversation.py`**

```python
"""Conversation —— 会话：SessionStore + 重放派生视图。

指针不变量：指向最新完成轮次的 assistant 节点（或 None）；它是下一条
user_msg 的默认 parent。分支 = set_pointer(历史节点) 后继续输入；叶子 =
append_user(leaf=True) 强制 parent=None。指针推进只发生在 assistant 落盘时。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .cards import Card, CardRegistry
from .errors import TreeChatError
from .events import (
    AssistantMsg, CardCreate, Pin, SessionMeta, SystemUpdate, Unpin, UserMsg,
)
from .store import SessionStore


@dataclass
class MsgNode:
    """消息节点。id = seq（稳定可引用）。"""

    seq: int
    parent: int | None
    role: str  # "user" | "assistant"
    text: str
    model: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_card_id(existing: set[str]) -> str:
    while True:
        cid = "card_" + uuid4().hex[:4]
        if cid not in existing:
            return cid


class Conversation:
    """一个会话：持久层 + 重放派生视图（节点表/子索引/指针/卡片注册表）。"""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.name = ""
        self.system = ""
        self.pointer: int | None = None
        self.nodes: dict[int, MsgNode] = {}
        self.children: dict[int | None, list[int]] = {}
        self.cards = CardRegistry()

    # ── 构造 ──

    @classmethod
    def create(cls, path: Path, name: str, system: str = "") -> "Conversation":
        conv = cls(SessionStore(path))
        conv.store.append(SessionMeta(name=name, created_at=_now(), system=system))
        conv._replay(conv.store.load())
        return conv

    @classmethod
    def open(cls, path: Path) -> "Conversation":
        conv = cls(SessionStore(path))
        events = conv.store.load()
        if not events:
            raise TreeChatError(f"会话文件为空: {path}")
        conv._replay(events)
        return conv

    def _replay(self, events: list[tuple[int, object]]) -> None:
        for seq, ev in events:
            self._apply(seq, ev)

    # ── 事件应用（追加与重放共用一份语义）──

    def _apply(self, seq: int, ev: object) -> None:
        match ev:
            case SessionMeta():
                self.name, self.system = ev.name, ev.system
            case SystemUpdate():
                self.system = ev.text
            case UserMsg():
                self._add_node(seq, ev.parent, "user", ev.text)
            case AssistantMsg():
                self._add_node(seq, ev.parent, "assistant", ev.text, model=ev.model)
                self.pointer = seq
            case CardCreate():
                self.cards.add(Card(
                    id=ev.card_id, title=ev.title, body=ev.body,
                    from_path=list(ev.from_path), instruction=ev.instruction,
                    created_at=_now(),
                ))
            case Pin():
                self.cards.pin(ev.card_id)
            case Unpin():
                self.cards.unpin(ev.card_id)
            case _:
                raise TreeChatError(f"不可重放的事件: {ev!r}")

    def _add_node(self, seq: int, parent: int | None, role: str, text: str,
                  model: str = "") -> None:
        if parent is not None and parent not in self.nodes:
            raise TreeChatError(f"parent 指向不存在的节点: seq={seq} parent={parent}")
        self.nodes[seq] = MsgNode(seq=seq, parent=parent, role=role, text=text, model=model)
        self.children.setdefault(parent, []).append(seq)

    # ── 追加（持久化即真相）──

    def append_user(self, text: str, *, leaf: bool = False) -> int:
        """追加 user_msg：默认 parent=指针；leaf=True 强制 parent=None。"""
        target = None if leaf else self.pointer
        ev = UserMsg(parent=target, text=text)
        seq = self.store.append(ev)
        self._apply(seq, ev)
        return seq

    def append_assistant(self, user_seq: int, text: str, *,
                         model: str = "", usage: dict[str, int] | None = None) -> int:
        """在 user 节点下追加 assistant 回复并推进指针。"""
        node = self.nodes.get(user_seq)
        if node is None or node.role != "user":
            raise TreeChatError(f"append_assistant 目标必须是 user 节点: {user_seq}")
        ev = AssistantMsg(parent=user_seq, text=text, model=model, usage=dict(usage or {}))
        seq = self.store.append(ev)
        self._apply(seq, ev)
        return seq

    def add_card(self, title: str, body: str, from_path: list[int],
                 instruction: str = "") -> str:
        """追加 card_create（默认 pinned），返回 card_id。"""
        ev = CardCreate(
            card_id=_new_card_id(self.cards.ids()), title=title, body=body,
            from_path=list(from_path), instruction=instruction,
        )
        seq = self.store.append(ev)
        self._apply(seq, ev)
        return ev.card_id

    def pin(self, card_id: str) -> None:
        self._append_apply(Pin(card_id=card_id))

    def unpin(self, card_id: str) -> None:
        self._append_apply(Unpin(card_id=card_id))

    def update_system(self, text: str) -> None:
        self._append_apply(SystemUpdate(text=text))

    def _append_apply(self, ev: object) -> None:
        seq = self.store.append(ev)
        self._apply(seq, ev)

    def set_pointer(self, seq: int | None) -> None:
        """把指针挪到任意历史节点（/branch、/trunk 用；纯内存操作，不落事件）。"""
        if seq is not None and seq not in self.nodes:
            raise TreeChatError(f"指针目标不存在: {seq}")
        self.pointer = seq

    # ── 视图 ──

    def path_to(self, seq: int) -> list[MsgNode]:
        """根到该节点的消息路径（即该分支的完整上下文）。"""
        if seq not in self.nodes:
            raise TreeChatError(f"节点不存在: {seq}")
        path = []
        cur: int | None = seq
        while cur is not None:
            path.append(self.nodes[cur])
            cur = self.nodes[cur].parent
        return list(reversed(path))

    def trunk(self) -> list[MsgNode]:
        """最长根→叶路径（按节点数；平局取末端 seq 最大者）。纯视图规则。"""
        best: list[MsgNode] = []
        for root in self.children.get(None, []):
            stack = [root]
            while stack:
                seq = stack.pop()
                kids = self.children.get(seq, [])
                if kids:
                    stack.extend(kids)
                else:
                    p = self.path_to(seq)
                    if len(p) > len(best) or (
                        len(p) == len(best) and p[-1].seq > best[-1].seq
                    ):
                        best = p
        return best

    def trunk_end(self) -> int | None:
        t = self.trunk()
        return t[-1].seq if t else None

    def unanswered_user(self) -> int | None:
        """最新的悬而未答 user 节点（无子节点）；/retry 的目标。无则 None。"""
        best: int | None = None
        for s, n in self.nodes.items():
            if n.role == "user" and not self.children.get(s):
                if best is None or s > best:
                    best = s
        return best

    def fork_point(self, seq: int) -> int | None:
        """路径上最后一个拥有 ≥2 子节点的祖先（不含自身）；无则 None。

        卡片提炼默认范围的起点依据（spec §3.2）。
        """
        for node in reversed(self.path_to(seq)[:-1]):
            if len(self.children.get(node.seq, [])) >= 2:
                return node.seq
        return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_conversation.py -q`
Expected: PASS（13 passed）

- [ ] **Step 5: Commit**

```bash
git add treechat/core/conversation.py tests/test_conversation.py
git commit -m "feat(core): Conversation 重放派生视图 — 指针/DAG/主干视图规则/fork_point"
```

---

### Task 6: core/context —— 上下文组装与 V1 窗口策略

**Files:**
- Create: `treechat/core/context.py`
- Test: `tests/test_context.py`

- [ ] **Step 1: 写失败测试 `tests/test_context.py`**

```python
"""上下文组装：history/current 分离、连续同角色合并、卡片块、V1 窗口策略。"""
import pytest

from treechat.core.cards import Card
from treechat.core.context import TokenWindowStrategy, assemble
from treechat.core.conversation import MsgNode
from treechat.core.errors import TreeChatError


def _node(seq, role, text, parent=None):
    return MsgNode(seq=seq, parent=parent, role=role, text=text)


def _path(*texts_roles):
    nodes = []
    for i, (role, text) in enumerate(texts_roles, start=1):
        nodes.append(_node(i, role, text))
    return nodes


def test_assemble_splits_history_and_current():
    path = _path(("user", "q1"), ("assistant", "a1"), ("user", "q2"))
    ctx = assemble(path, system="S", cards=[])
    assert ctx.current == "q2"
    assert ctx.history == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    assert ctx.system == "S"


def test_leaf_path_history_empty():
    ctx = assemble(_path(("user", "概念提问")), system="", cards=[])
    assert ctx.history == []
    assert ctx.current == "概念提问"


def test_assemble_requires_user_tail():
    with pytest.raises(TreeChatError, match="user 节点"):
        assemble(_path(("user", "q"), ("assistant", "a")), system="", cards=[])


def test_merge_consecutive_same_role():
    path = _path(("user", "q1"), ("user", "q2(未答)"), ("user", "q3"))
    ctx = assemble(path, system="", cards=[])
    assert ctx.history == [{"role": "user", "content": "q1\n\nq2(未答)"}]
    assert ctx.current == "q3"


def test_pinned_cards_block_in_system():
    cards = [Card(id="card_1", title="标题", body="正文", from_path=[1])]
    ctx = assemble(_path(("user", "q")), system="S", cards=cards)
    assert ctx.system == "S\n\n[参考卡片 card_1: 标题]\n正文"


def test_window_drops_oldest_with_warning_but_keeps_cards():
    cards = [Card(id="card_1", title="T", body="B" * 200, from_path=[1])]
    path = _path(*[("user" if i % 2 == 0 else "assistant", f"msg{i}长" * 30) for i in range(1, 9)])
    strat = TokenWindowStrategy(budget_tokens=400)
    ctx = assemble(path, system="S", cards=cards, strategy=strat)
    assert "[参考卡片 card_1: T]" in ctx.system          # 卡片整块保留
    assert "因窗口预算未纳入" in ctx.system               # 显式警示
    assert len(ctx.history) < 8                          # 丢了最旧
    assert ctx.history[-1]["content"].startswith("msg8") # 保留最新


def test_window_within_budget_no_warning():
    path = _path(("user", "q1"), ("assistant", "a1"), ("user", "q2"))
    ctx = assemble(path, system="S", cards=[], strategy=TokenWindowStrategy(budget_tokens=10_000))
    assert "因窗口预算未纳入" not in ctx.system
    assert len(ctx.history) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_context.py -q`
Expected: FAIL（`ModuleNotFoundError: treechat.core.context`）

- [ ] **Step 3: 实现 `treechat/core/context.py`**

```python
"""上下文组装 —— system(会话指令+pinned 卡片) + history/current 分离 + V1 窗口策略。

V1 窗口：超预算丢最旧 history 并在 system 末尾注入显式警示（不静默）；
system 与卡片整块保留。V2 将升级为"摘要卡片 + compact 事件"（spec §4.1）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .cards import Card
from .conversation import MsgNode
from .errors import TreeChatError

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """V1 估算：chars/4（后续可换 provider usage 精确计费，见 spec §10）。"""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def card_block(card: Card) -> str:
    return f"[参考卡片 {card.id}: {card.title}]\n{card.body}"


def build_system(system: str, cards: list[Card]) -> str:
    parts = [p for p in [system, *[card_block(c) for c in cards]] if p]
    return "\n\n".join(parts)


def _merge_consecutive(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """合并连续同角色消息（失败重试的兄弟 user 节点 / user 下挂 user 的分支），
    保证 LLM API 的角色交替要求。"""
    merged: list[dict[str, str]] = []
    for msg in history:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n\n" + msg["content"]
        else:
            merged.append(dict(msg))
    return merged


class WindowStrategy(Protocol):
    """窗口策略协议：返回 (最终 system, 装填后 history, 警示或 None)。"""

    def fit(self, system: str, history: list[dict[str, str]]) -> tuple[
        str, list[dict[str, str]], str | None,
    ]: ...


@dataclass
class TokenWindowStrategy:
    """V1 默认策略：system+卡片整块保留，history 从最新往回装填，丢最旧。"""

    budget_tokens: int = 100_000
    estimator: Callable[[str], int] = estimate_tokens

    def fit(self, system: str, history: list[dict[str, str]]):
        used = self.estimator(system)
        kept: list[dict[str, str]] = []
        for msg in reversed(history):
            cost = self.estimator(msg["content"])
            if kept and used + cost > self.budget_tokens:
                break
            kept.append(msg)
            used += cost
        kept.reverse()
        dropped = len(history) - len(kept)
        warning = None
        if dropped:
            warning = f"（更早 {dropped} 条消息因窗口预算未纳入上下文）"
            system = system + ("\n\n" if system else "") + warning
        return system, kept, warning


@dataclass
class AssembledContext:
    system: str
    history: list[dict[str, str]]  # [{"role": ..., "content": ...}]，llm_bridge 转 Message
    current: str


def assemble(path: list[MsgNode], system: str, cards: list[Card],
             strategy: WindowStrategy | None = None) -> AssembledContext:
    """path = path_to(本条 user 节点)；末条即 current（叶子分支 path 长度 1 → history 空）。"""
    if not path or path[-1].role != "user":
        raise TreeChatError("assemble 需要以 user 节点结尾的路径")
    history = [{"role": n.role, "content": n.text} for n in path[:-1]]
    history = _merge_consecutive(history)
    sys_text = build_system(system, cards)
    if strategy is not None:
        sys_text, history, _ = strategy.fit(sys_text, history)
    return AssembledContext(system=sys_text, history=history, current=path[-1].text)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_context.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: Commit**

```bash
git add treechat/core/context.py tests/test_context.py
git commit -m "feat(core): 上下文组装 — history/current 分离+连续同角色合并+TokenWindow 显式警示"
```

---

### Task 7: llm_bridge —— 唯一 SpecModule 依赖面

**Files:**
- Create: `treechat/llm_bridge.py`
- Test: `tests/test_llm_bridge.py`

- [ ] **Step 1: 写失败测试 `tests/test_llm_bridge.py`**

```python
"""llm_bridge：chat_turn 消息组装 / extract_card 结构校验 / create_client 配置链。"""
import asyncio
import json

import pytest
from llm import LLMError, LLMResponse

from treechat import llm_bridge
from treechat.core.errors import TreeChatError


class CardOkClient:
    """带 complete() 的假客户端：返回合法卡片 JSON。"""

    async def complete(self, **kwargs):
        return LLMResponse(content=json.dumps({"title": "卡片标题", "body": "卡片正文"}))


class CardBadClient:
    async def complete(self, **kwargs):
        return LLMResponse(content=json.dumps({"foo": 1}))


class BoomClient:
    async def complete(self, **kwargs):
        raise LLMError("鉴权失败")


class ChatRecorder:
    def __init__(self, reply="回复"):
        self.reply = reply
        self.calls = []

    async def chat(self, messages):
        self.calls.append(list(messages))
        return LLMResponse(content=self.reply, usage={"input_tokens": 3, "output_tokens": 5})


def test_chat_turn_builds_messages_and_returns_reply():
    client = ChatRecorder(reply="答")
    reply, usage = asyncio.run(llm_bridge.chat_turn(
        client, system="SYS",
        history=[{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}],
        current="q2",
    ))
    assert reply == "答" and usage == {"input_tokens": 3, "output_tokens": 5}
    roles = [(m.role, m.content) for m in client.calls[0]]
    assert roles == [
        ("system", "SYS"),
        ("user", "q1"), ("assistant", "a1"), ("user", "q2"),
    ]


def test_extract_card_ok():
    out = asyncio.run(llm_bridge.extract_card("转录", "总结", llm_client=CardOkClient()))
    assert out == {"title": "卡片标题", "body": "卡片正文"}


def test_extract_card_rejects_missing_keys():
    with pytest.raises(TreeChatError, match="title/body"):
        asyncio.run(llm_bridge.extract_card("t", "i", llm_client=CardBadClient()))


def test_extract_card_propagates_harness_call_error():
    from module_harness import HarnessCallError
    with pytest.raises((HarnessCallError, LLMError, TreeChatError)):
        asyncio.run(llm_bridge.extract_card("t", "i", llm_client=BoomClient()))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_llm_bridge.py -q`
Expected: FAIL（`ImportError: cannot import name 'llm_bridge'`）

- [ ] **Step 3: 实现 `treechat/llm_bridge.py`**

```python
"""SpecModule 依赖面 —— treechat 唯一 import llm/module_harness 的地方。

职责：① 对话轮次（client.chat 多轮，V1 非流式）；② 卡片提炼（call_harness
结构化调用 + {title, body} 显式校验）。测试替换点：带 chat/complete 的假客户端。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from llm import LLMConfig, Message, create_llm_client
from module_harness import call_harness
from module_harness.core.config import HarnessConfig
from module_harness.core.outputfmt import OutputFormat

from .core.errors import TreeChatError

CARD_PROMPT_CORE = """\
你是上下文压缩器。把下面这段对话提炼成一张"卡片"：脱离原对话也能独立读懂的浓缩产出。

## 对话转录
{transcript}

## 提炼指令
{instruction}

要求：
- body 自包含：不出现"上面/刚才/对方/这段对话"等指代；保留事实、决策、结论与关键理由
- 去对话语气，写成结构化 markdown（可用小标题/列表）
- title 为一句话概括
"""

CARD_HARNESS_CONFIG = HarnessConfig(
    prompt_core=CARD_PROMPT_CORE,
    prompt_modes={"default": "按核心模板执行。"},
    output_format=OutputFormat(
        type="json_object",
        instruction='输出 JSON 对象：{"title": "一句话标题", "body": "markdown 正文"}',
    ),
    notdo=["不要在 JSON 之外输出任何文字"],
)


def create_client(model: str | None = None,
                  project_root: Path | None = None) -> Any:
    """env 驱动创建客户端（复用 SpecModule 配置回退链：项目根 → ~/.specmodule）。"""
    overrides: dict[str, Any] = {"model": model} if model else {}
    config = LLMConfig.from_env(
        project_root=project_root or Path.cwd(),
        store_root=Path.home() / ".specmodule",
        **overrides,
    )
    return create_llm_client(config)


async def chat_turn(client: Any, system: str, history: list[dict[str, str]],
                    current: str) -> tuple[str, dict[str, int]]:
    """一轮对话：组装 messages（system 打头）→ client.chat → (回复, usage)。

    失败 LLMError 原样上抛（调用方保证此时 user_msg 已落盘，悬而未答节点保留）。
    """
    messages = [Message(role="system", content=system)]
    messages += [Message(role=m["role"], content=m["content"]) for m in history]
    messages.append(Message(role="user", content=current))
    resp = await client.chat(messages)
    return resp.content, dict(resp.usage or {})


async def extract_card(transcript: str, instruction: str, *,
                       llm_client: Any) -> dict[str, str]:
    """卡片提炼：call_harness 校验 json_object，再显式校验 {title, body} 键。"""
    result = await call_harness(
        CARD_HARNESS_CONFIG,
        {"transcript": transcript, "instruction": instruction},
        llm_client=llm_client,
        promptmode="default",
    )
    value = result.value
    if not isinstance(value, dict) or "title" not in value or "body" not in value:
        raise TreeChatError(f"卡片提炼输出缺 title/body: {value!r}")
    return {"title": str(value["title"]), "body": str(value["body"])}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_llm_bridge.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add treechat/llm_bridge.py tests/test_llm_bridge.py
git commit -m "feat(bridge): llm_bridge — chat 轮次组装 + call_harness 卡片提炼（唯一依赖面）"
```

---

### Task 8: session 门面 + config + 包导出

**Files:**
- Create: `treechat/session.py`
- Modify: `treechat/__init__.py`（填充 `__all__`）
- Modify: `treechat/config.py`（无需改，Task 1 已建）
- Test: `tests/test_session.py`、`tests/conftest.py`

- [ ] **Step 1: 写失败测试 `tests/conftest.py` 与 `tests/test_session.py`**

`tests/conftest.py`：

```python
"""共享假客户端与 session 工厂。"""
import json

import pytest
from llm import LLMError, LLMResponse


class FakeChatClient:
    """带 chat() 的假客户端：固定回复，记录调用（轮次测试用）。"""

    def __init__(self, reply: str = "mock reply") -> None:
        self.reply = reply
        self.calls: list[list] = []
        self.fail = False

    async def chat(self, messages):
        self.calls.append(list(messages))
        if self.fail:
            raise LLMError("模拟基础设施故障")
        return LLMResponse(content=self.reply, usage={"input_tokens": 1, "output_tokens": 2})


class FakeCardClient:
    """带 complete() 的假客户端：返回合法卡片 JSON（走 call_harness 校验路径）。"""

    async def complete(self, **kwargs):
        return LLMResponse(content=json.dumps({"title": "卡片标题", "body": "卡片正文"}))


@pytest.fixture
def fake_chat():
    return FakeChatClient()


@pytest.fixture
def fake_card_client():
    return FakeCardClient()
```

`tests/test_session.py`：

```python
"""TreeChatSession：轮次持久时序 / 失败悬而未答 / 卡片默认范围 / 导出。"""
import asyncio

import pytest

from treechat.config import TreeChatConfig
from treechat.core.events import UserMsg
from treechat.session import TreeChatSession, list_sessions


def _session(tmp_path, fake_chat, fake_card_client, name="t"):
    path = tmp_path / "s.jsonl"
    conv_session = TreeChatSession.__new__(TreeChatSession)
    from treechat.core.conversation import Conversation
    conv = Conversation.create(path, name=name, system="sys")
    conv_session.conversation = conv
    conv_session.client = fake_chat
    conv_session.card_llm = fake_card_client
    from treechat.core.context import TokenWindowStrategy
    conv_session.window = TokenWindowStrategy(budget_tokens=10_000)
    return conv_session


def test_turn_persists_user_first_then_assistant(tmp_path, fake_chat, fake_card_client):
    s = _session(tmp_path, fake_chat, fake_card_client)
    # 让 chat 失败：user 节点已落盘，无 assistant
    fake_chat.fail = True
    with pytest.raises(RuntimeError):
        asyncio.run(s.turn("问题"))
    assert s.conversation.nodes[2].role == "user"          # seq1=meta, seq2=user
    assert s.conversation.pointer is None
    # 恢复后 retry：在原节点下补 assistant，不重复问题
    fake_chat.fail = False
    a = asyncio.run(s.turn_retry(2))
    assert s.conversation.pointer == a
    assert s.conversation.nodes[a].parent == 2
    assert [n.seq for n in s.conversation.nodes.values()].count(2) == 1


def test_turn_happy_path(tmp_path, fake_chat, fake_card_client):
    s = _session(tmp_path, fake_chat, fake_card_client)
    a = asyncio.run(s.turn("你好"))
    assert s.conversation.nodes[a].text == "mock reply"
    assert s.conversation.path_to(a)[-1].role == "assistant"


def test_turn_leaf_no_context(tmp_path, fake_chat, fake_card_client):
    s = _session(tmp_path, fake_chat, fake_card_client)
    asyncio.run(s.turn("主对话"))
    asyncio.run(s.turn("叶子提问", leaf=True))
    msgs = fake_chat.calls[-1]
    # system + 仅当前 user（无 history）
    assert [m.role for m in msgs] == ["system", "user"]


def test_branch_segment_default_and_user_fork(tmp_path, fake_chat, fake_card_client):
    s = _session(tmp_path, fake_chat, fake_card_client)
    a1 = asyncio.run(s.turn("主干问"))            # 1u? meta=1 → user=2, assistant=3
    conv = s.conversation
    conv.set_pointer(2)                            # 回 user 节点
    asyncio.run(s.turn("分支问"))                  # user=4, assistant=5
    assert conv.pointer == 5
    assert s.branch_segment() == [4, 5]            # fork 在 #2(assistant) → 不含
    # fork 在 user 节点的情形：#2 下再开一支 → #2 有两个子
    conv.set_pointer(2)
    asyncio.run(s.turn("另一支"))                  # user=6, assistant=7
    assert s.branch_segment() == [6, 7]
    # 从 user 节点分叉：#4 变成有两个子的 fork（4 的孩子是 5）——构造 user fork：
    conv.set_pointer(4)                            # 指回 user#4
    b = conv.append_user("user fork 下的问题")     # user=8, parent=4
    a = conv.append_assistant(b, "答")             # assistant=9
    assert s.branch_segment(9) == [4, 8, 9]        # fork #4 是 user → 含其自身


def test_make_card_defaults_pinned(tmp_path, fake_chat, fake_card_client):
    s = _session(tmp_path, fake_chat, fake_card_client)
    asyncio.run(s.turn("问"))
    cid = asyncio.run(s.make_card("总结为卡片"))
    card = s.conversation.cards.get(cid)
    assert (card.title, card.body) == ("卡片标题", "卡片正文")
    assert [c.id for c in s.conversation.cards.pinned_cards()] == [cid]


def test_export_card(tmp_path, fake_chat, fake_card_client):
    s = _session(tmp_path, fake_chat, fake_card_client)
    asyncio.run(s.turn("问"))
    cid = asyncio.run(s.make_card("总结"))
    out = s.export_card(cid, tmp_path / "card.md")
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# 卡片标题") and "卡片正文" in text


def test_list_sessions(tmp_path, fake_chat, fake_card_client):
    config = TreeChatConfig(data_dir=tmp_path)
    config.sessions_dir().mkdir(parents=True)
    path = config.sessions_dir() / "甲.jsonl"
    TreeChatSession.create(path, "甲", system="")
    listed = list_sessions(config)
    assert [m.name for _, m in listed] == ["甲"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_session.py -q`
Expected: FAIL（`ImportError: cannot import name 'TreeChatSession'`）

- [ ] **Step 3: 实现 `treechat/session.py`**

```python
"""TreeChatSession —— 编程 API 门面：轮次 + 卡片（组合 Conversation 与 llm_bridge）。

轮次持久时序（spec §2.2）：send 先落 user_msg；complete 调 LLM 后落 assistant_msg。
LLM 失败 → 悬而未答节点保留，turn_retry 在原节点下补 assistant（问题不丢、不重复）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm import LLMError

from . import llm_bridge
from .config import TreeChatConfig
from .core.context import TokenWindowStrategy, WindowStrategy, assemble
from .core.conversation import Conversation
from .core.errors import TreeChatError
from .core.events import SessionMeta
from .core.store import read_session_meta


@dataclass
class TreeChatSession:
    """一个打开的会话 + LLM 客户端 + 窗口策略。"""

    conversation: Conversation
    client: Any
    window: WindowStrategy | None = field(default_factory=TokenWindowStrategy)
    card_llm: Any = None
    """卡片提炼客户端（需支持 complete）；None = 复用 client（真客户端两者都有）。"""

    # ── 构造 ──

    @classmethod
    def create(cls, path: Path, name: str, system: str = "", *,
               model: str | None = None,
               window: WindowStrategy | None = None) -> "TreeChatSession":
        conv = Conversation.create(path, name, system)
        return cls(conversation=conv, client=llm_bridge.create_client(model), window=window)

    @classmethod
    def open(cls, path: Path, *, model: str | None = None,
             window: WindowStrategy | None = None) -> "TreeChatSession":
        conv = Conversation.open(path)
        return cls(conversation=conv, client=llm_bridge.create_client(model), window=window)

    # ── 轮次 ──

    def send(self, text: str, *, leaf: bool = False) -> int:
        """落 user_msg（先持久化，防丢），返回其 seq。"""
        return self.conversation.append_user(text, leaf=leaf)

    async def complete(self, user_seq: int) -> int:
        """组装 → chat → 落 assistant_msg → 指针推进。LLMError 上抛。"""
        conv = self.conversation
        if user_seq not in conv.nodes or conv.nodes[user_seq].role != "user":
            raise TreeChatError(f"complete 目标必须是 user 节点: {user_seq}")
        ctx = assemble(conv.path_to(user_seq), conv.system,
                       conv.cards.pinned_cards(), strategy=self.window)
        reply, usage = await llm_bridge.chat_turn(
            self.client, ctx.system, ctx.history, ctx.current)
        return conv.append_assistant(user_seq, reply, usage=usage)

    async def turn(self, text: str, *, leaf: bool = False) -> int:
        """send + complete 一步走。失败 user 节点已落盘，turn_retry 重试。"""
        seq = self.send(text, leaf=leaf)
        try:
            return await self.complete(seq)
        except LLMError:
            raise

    async def turn_retry(self, user_seq: int) -> int:
        """对悬而未答的 user 节点重新调 LLM。"""
        return await self.complete(user_seq)

    # ── 卡片 ──

    def branch_segment(self, seq: int | None = None) -> list[int]:
        """默认提炼范围（spec §3.2）：fork_point 起到 seq（默认指针）；
        fork_point 为 user 节点时含其自身（提问属于这段讨论），assistant 则不含。"""
        target = seq if seq is not None else self.conversation.pointer
        if target is None:
            raise TreeChatError("空会话没有可提炼范围")
        path = self.conversation.path_to(target)
        fp = self.conversation.fork_point(target)
        if fp is None:
            return [n.seq for n in path]
        idx = next(i for i, n in enumerate(path) if n.seq == fp)
        start = idx if path[idx].role == "user" else idx + 1
        segment = [n.seq for n in path[start:]]
        return segment or [target]

    async def make_card(self, instruction: str, *, seq: int | None = None,
                        from_seqs: list[int] | None = None) -> str:
        """提炼卡片：默认当前分支段；from_seqs 显式区间（/card all / <a>-<b>）。"""
        seqs = from_seqs if from_seqs is not None else self.branch_segment(seq)
        if not seqs:
            raise TreeChatError("提炼范围为空")
        lines = []
        for s in seqs:
            n = self.conversation.nodes[s]
            lines.append(f"[{n.role}] {n.text}")
        transcript = "\n\n".join(lines)
        client = self.card_llm if self.card_llm is not None else self.client
        out = await llm_bridge.extract_card(transcript, instruction, llm_client=client)
        return self.conversation.add_card(out["title"], out["body"], seqs, instruction)

    def export_card(self, card_id: str, file_path: Path) -> Path:
        card = self.conversation.cards.get(card_id)
        out = Path(file_path)
        out.write_text(f"# {card.title}\n\n{card.body}\n", encoding="utf-8")
        return out


def list_sessions(config: TreeChatConfig) -> list[tuple[Path, SessionMeta]]:
    """枚举 data_dir/sessions 下的会话（坏文件跳过——打开时才硬报错）。"""
    d = config.sessions_dir()
    if not d.exists():
        return []
    out: list[tuple[Path, SessionMeta]] = []
    for p in sorted(d.glob("*.jsonl")):
        try:
            out.append((p, read_session_meta(p)))
        except (TreeChatError, json.JSONDecodeError, OSError):
            continue
    return out
```

再把 `treechat/__init__.py` 替换为：

```python
"""TreeChat —— 对话树一等公民的可嵌入对话引擎。"""
from __future__ import annotations

from .config import TreeChatConfig
from .core.cards import Card, CardRegistry
from .core.conversation import Conversation, MsgNode
from .core.errors import EventFormatError, TreeChatError
from .session import TreeChatSession, list_sessions

__all__ = [
    "Card",
    "CardRegistry",
    "Conversation",
    "EventFormatError",
    "MsgNode",
    "TreeChatConfig",
    "TreeChatError",
    "TreeChatSession",
    "list_sessions",
]
```

- [ ] **Step 4: 跑全部测试确认通过**

Run: `python -m pytest tests/ -q`
Expected: PASS（此前累计 + 8 passed）

- [ ] **Step 5: Commit**

```bash
git add treechat/session.py treechat/__init__.py tests/conftest.py tests/test_session.py
git commit -m "feat: TreeChatSession 门面 — 轮次持久时序/悬而未答重试/卡片默认范围/导出"
```

---

### Task 9: CLI 骨架 —— 非交互命令 + REPL 主循环

**Files:**
- Create: `treechat/cli/repl.py`、`treechat/cli/commands.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: 写失败测试 `tests/test_cli.py`（第一批）**

```python
"""CLI：非交互命令 + REPL 对话/where/help/quit/retry。"""
import asyncio

from llm import LLMError
from treechat.cli.repl import main, run_repl
from treechat.config import TreeChatConfig


def _make_input(lines):
    it = iter(lines)

    async def input_fn(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    return input_fn


def _make_say():
    out = []

    def say(text=""):
        out.append(str(text))

    return say, out


def _open_session(tmp_path, fake_chat, fake_card_client):
    from treechat.core.conversation import Conversation
    from treechat.core.context import TokenWindowStrategy
    from treechat.session import TreeChatSession
    conv = Conversation.create(tmp_path / "t.jsonl", name="t", system="sys")
    s = TreeChatSession(conversation=conv, client=fake_chat,
                        window=TokenWindowStrategy(budget_tokens=10_000),
                        card_llm=fake_card_client)
    return s


def test_main_new_creates_session(tmp_path, capsys):
    config = TreeChatConfig(data_dir=tmp_path)
    # new 会进 REPL；喂 /quit 立即退出
    code = main(["new", "甲", "--system", "s"], config=config,
                input_fn=_make_input(["/quit"]))
    assert code == 0
    assert (config.sessions_dir() / "甲.jsonl").exists()


def test_main_list(tmp_path, capsys):
    config = TreeChatConfig(data_dir=tmp_path)
    config.sessions_dir().mkdir(parents=True)
    from treechat.core.conversation import Conversation
    Conversation.create(config.sessions_dir() / "乙.jsonl", name="乙")
    code = main(["list"], config=config)
    assert code == 0
    assert "乙" in capsys.readouterr().out


def test_repl_bare_input_and_where(tmp_path, fake_chat, fake_card_client):
    s = _open_session(tmp_path, fake_chat, fake_card_client)
    say, out = _make_say()
    code = run_repl(s, TreeChatConfig(data_dir=tmp_path),
                    input_fn=_make_input(["你好", "/where", "/quit"]), say=say)
    assert code == 0
    assert any("mock reply" in line for line in out)
    assert any("/where" in line or "指针" in line for line in out)


def test_repl_llm_failure_surface_and_retry(tmp_path, fake_chat, fake_card_client):
    s = _open_session(tmp_path, fake_chat, fake_card_client)
    say, out = _make_say()
    fake_chat.fail = True
    code = run_repl(s, TreeChatConfig(data_dir=tmp_path),
                    input_fn=_make_input(["问题", "/retry", "/quit"]), say=say)
    fake_chat.fail = False
    assert code == 0
    assert any("LLM 调用失败" in line for line in out)
    assert any("悬而未答" in line for line in out)


def test_repl_help_and_unknown_command(tmp_path, fake_chat, fake_card_client):
    s = _open_session(tmp_path, fake_chat, fake_card_client)
    say, out = _make_say()
    run_repl(s, TreeChatConfig(data_dir=tmp_path),
             input_fn=_make_input(["/help", "/nope", "/quit"]), say=say)
    assert any("命令" in line for line in out)
    assert any("未知命令" in line for line in out)
```

注意：`main`/`run_repl` 需支持注入 `input_fn`（测试）；`main` 的 `new`/`open` 走 REPL，`list` 走打印。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_cli.py -q`
Expected: FAIL（`ImportError: cannot import name 'main'`）

- [ ] **Step 3: 实现 `treechat/cli/commands.py` 与 `treechat/cli/repl.py`**

`commands.py`（本任务先实现 help/where/quit；tree/branch/trunk/leaf/retry/card 族在 Task 10/11 补）：

```python
"""斜杠命令路由。返回 True 表示退出 REPL。

state（REPL 会话态，repl 层持有）：{"leaf_next": bool}；/retry 目标由 `conv.unanswered_user()` 视图给出（跨 REPL 打开仍然有效）
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from llm import LLMError

from ..config import TreeChatConfig
from ..core.errors import TreeChatError
from ..session import TreeChatSession

Say = Callable[[str], None]

_HELP = """\
命令：
  /tree                 树视图（* 指针 ◆ 主干末端 [卡片]）
  /branch <seq>         指针挪到历史节点 → 下一条输入长新枝
  /trunk                指针跳回主干末端
  /leaf                 下一条输入 = 无上下文叶子提问
  /retry                对最后一个悬而未答节点重新调 LLM
  /card [all|<a>-<b>|指令]   提炼卡片（默认当前分支段）
  /cards；/card show <id>；/pin /unpin <id>；/card export <id> <file>
  /where                当前位置
  /system [新指令]       查看/修改会话级 system
  /model [名]           查看/切换模型
  /help；/quit
"""

_UNIMPLEMENTED = {"tree", "branch", "trunk", "leaf", "card", "cards",
                  "pin", "unpin", "system", "model"}


async def handle_command(session: TreeChatSession, config: TreeChatConfig,
                         line: str, *, state: dict[str, Any], say: Say) -> bool:
    """处理斜杠命令；返回 True = 退出 REPL。业务错误显式显示，不中断会话。"""
    parts = line[1:].strip().split(maxsplit=1)
    cmd = parts[0] if parts else "help"
    rest = parts[1].strip() if len(parts) > 1 else ""
    conv = session.conversation
    try:
        if cmd in ("quit", "q"):
            return True
        if cmd == "help":
            say(_HELP)
        elif cmd == "where":
            depth = len(conv.path_to(conv.pointer)) if conv.pointer else 0
            say(f"指针: #{conv.pointer} · 路径深度 {depth} · 会话 {conv.name}")
        elif cmd in _UNIMPLEMENTED:
            say(f"/{cmd} 尚未实现（Task 10/11）")
        else:
            say(f"未知命令: /{cmd}（/help 查看命令）")
    except LLMError as exc:
        dangling = conv.unanswered_user()
        say(f"LLM 调用失败：{exc}"
            + (f"\n节点 #{dangling} 悬而未答；/retry 重试" if dangling else ""))
    except TreeChatError as exc:
        say(f"错误：{exc}")
    return False
```

`repl.py`：

```python
"""REPL 主循环（stdlib）+ 非交互入口。

asyncio 单循环贯穿 REPL 生命周期（客户端 httpx 绑定 loop，不能每轮
asyncio.run）；input 用 to_thread 避免阻塞 loop。
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from llm import LLMError

from .commands import handle_command
from ..config import TreeChatConfig
from ..core.errors import TreeChatError
from ..session import TreeChatSession

Say = Callable[[str], None]
InputFn = Callable[[str], Awaitable[str]]


def _reply_line(seq: int | None, text: str) -> str:
    return f"[#{seq}] {text}" if seq is not None else text


async def _repl_async(session: TreeChatSession, config: TreeChatConfig, *,
                      input_fn: InputFn, say: Say) -> int:
    conv = session.conversation
    say(f"treechat · {conv.name} · /help 查看命令")
    state: dict = {"leaf_next": False}
    while True:
        try:
            line = (await input_fn("› ")).strip()
        except (EOFError, KeyboardInterrupt):
            say("")
            return 0
        if not line:
            continue
        try:
            if line.startswith("/"):
                if await handle_command(session, config, line, state=state, say=say):
                    return 0
            else:
                seq = session.send(line, leaf=state["leaf_next"])
                state["leaf_next"] = False
                say("…")
                await session.complete(seq)
                node = conv.nodes[conv.pointer]
                say(_reply_line(node.seq, node.text))
        except LLMError as exc:
            say(f"LLM 调用失败：{exc}"
                + (f"\n节点 #{conv.unanswered_user()} 悬而未答；/retry 重试"
                   if conv.unanswered_user() else ""))
        except TreeChatError as exc:
            say(f"错误：{exc}")


async def _stdin_line(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


def run_repl(session: TreeChatSession, config: TreeChatConfig, *,
             input_fn: InputFn | None = None, say: Say | None = None) -> int:
    """REPL 主循环。input_fn/say 可注入（测试/嵌入方）。"""
    return asyncio.run(_repl_async(
        session, config,
        input_fn=input_fn or _stdin_line,
        say=say or print,
    ))


def main(argv: list[str] | None = None, config: TreeChatConfig | None = None,
         input_fn: InputFn | None = None) -> int:
    parser = argparse.ArgumentParser(prog="treechat", description="对话树对话引擎")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new", help="新建会话并进入 REPL")
    p_new.add_argument("name")
    p_new.add_argument("--system", default="")
    p_open = sub.add_parser("open", help="打开会话进入 REPL")
    p_open.add_argument("name")
    sub.add_parser("list", help="列出会话")
    args = parser.parse_args(argv)
    config = config or TreeChatConfig()

    if args.cmd == "list":
        from ..session import list_sessions
        for path, meta in list_sessions(config):
            print(f"{meta.name}\t{path.name}\t{meta.created_at}")
        return 0

    path = config.sessions_dir() / f"{args.name}.jsonl"
    if args.cmd == "new":
        if path.exists():
            print(f"会话已存在: {args.name}")
            return 1
        session = TreeChatSession.create(path, args.name, args.system)
    else:
        session = TreeChatSession.open(path)
    return run_repl(session, config, input_fn=input_fn)
```

- [ ] **Step 4: 跑全部测试确认通过**

Run: `python -m pytest tests/ -q`
Expected: PASS（累计 + 5 passed）

- [ ] **Step 5: Commit**

```bash
git add treechat/cli tests/test_cli.py
git commit -m "feat(cli): REPL 骨架 — 非交互 new/list + 轮次循环 + 失败显式呈现"
```

---

### Task 10: CLI 树与分支命令（/tree /branch /trunk /leaf）

**Files:**
- Create: `treechat/cli/treeview.py`
- Modify: `treechat/cli/commands.py`
- Test: `tests/test_cli.py`（追加）与 `tests/test_treeview.py`

- [ ] **Step 1: 写失败测试**

`tests/test_treeview.py`：

```python
"""/tree ASCII 渲染。"""
from treechat.cli.treeview import render_tree
from treechat.core.conversation import Conversation


def _build(tmp_path):
    conv = Conversation.create(tmp_path / "s.jsonl", name="t")
    u1 = conv.append_user("主干问")                 # 2
    a1 = conv.append_assistant(u1, "主干答")        # 3
    conv.set_pointer(u1)
    u2 = conv.append_user("分支问")                 # 4
    a2 = conv.append_assistant(u2, "分支答")        # 5
    cid = conv.add_card("标题", "正文", from_path=[u2, a2])
    leaf = conv.append_user("叶子提问", leaf=True)  # 7
    conv.set_pointer(a2)
    return conv, cid, leaf, a2


def test_render_marks_pointer_trunk_and_cards(tmp_path):
    conv, cid, leaf, a2 = _build(tmp_path)
    text = render_tree(conv)
    assert "◆" in text                       # 主干末端 #3
    assert "*" in text                       # 指针 #5
    assert f"[{cid}]" in text                # 卡片来源标注
    assert "#7 user" in text                 # 叶子根可见
    lines = text.splitlines()
    assert lines[0].startswith("#2")         # 第一根在顶部


def test_render_empty(tmp_path):
    conv = Conversation.create(tmp_path / "e.jsonl", name="e")
    assert render_tree(conv) == "（空会话）"
```

`tests/test_cli.py` 追加：

```python
def test_repl_branch_trunk_leaf_flow(tmp_path, fake_chat, fake_card_client):
    s = _open_session(tmp_path, fake_chat, fake_card_client)
    say, out = _make_say()
    run_repl(
        s, TreeChatConfig(data_dir=tmp_path),
        input_fn=_make_input([
            "主干一问",            # 2u 3a
            "/branch 2",           # 指针 → #2
            "分支一问",            # 4u 5a（此链 3 节点 > 主干 2 节点，分支即成主干）
            "/trunk",              # 谁最长谁是主干 → 指针 #5
            "/leaf",               # 下一条 = 叶子
            "叶子一问",            # 6u 7a
            "/quit",
        ]),
        say=say,
    )
    conv = s.conversation
    assert conv.nodes[4].parent == 2          # 分支挂在 #2
    assert conv.nodes[6].parent is None       # 叶子根
    assert conv.pointer == 7                  # /trunk 后又轮转到叶子链末端
    assert any("新枝" in line for line in out)
    assert any("叶子" in line for line in out)


def test_repl_tree_command(tmp_path, fake_chat, fake_card_client):
    s = _open_session(tmp_path, fake_chat, fake_card_client)
    say, out = _make_say()
    run_repl(s, TreeChatConfig(data_dir=tmp_path),
             input_fn=_make_input(["问一句", "/tree", "/quit"]), say=say)
    assert any("◆" in line for line in out)
    assert any("#" in line for line in out)


def test_repl_branch_unknown_seq_errors(tmp_path, fake_chat, fake_card_client):
    s = _open_session(tmp_path, fake_chat, fake_card_client)
    say, out = _make_say()
    run_repl(s, TreeChatConfig(data_dir=tmp_path),
             input_fn=_make_input(["/branch 99", "/quit"]), say=say)
    assert any("错误" in line for line in out)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_treeview.py tests/test_cli.py -q`
Expected: FAIL（`ModuleNotFoundError: treechat.cli.treeview`；`/branch 尚未实现`）

- [ ] **Step 3: 实现 `treechat/cli/treeview.py` 并扩展 `commands.py`**

`treeview.py`：

```python
"""/tree ASCII 渲染（纯展示层；第二消费方出现再上收查询层）。"""
from __future__ import annotations

from ..core.conversation import Conversation


def render_tree(conv: Conversation) -> str:
    """根列表逐棵渲染；标注 ◆ 主干末端、* 指针、[card_x] 卡片来源。"""
    if not conv.nodes:
        return "（空会话）"
    lines: list[str] = []
    trunk_end = conv.trunk_end()
    card_by_seq: dict[int, list[str]] = {}
    for c in conv.cards.all_cards():
        for s in c.from_path:
            card_by_seq.setdefault(s, []).append(c.id)

    def emit(seq: int, prefix: str = "", branch: str = "") -> None:
        n = conv.nodes[seq]
        marks: list[str] = []
        if seq == trunk_end:
            marks.append("◆")
        if seq == conv.pointer:
            marks.append("*")
        if seq in card_by_seq:
            marks.append("[" + ",".join(card_by_seq[seq]) + "]")
        text = n.text.replace("\n", " ")[:32]
        suffix = (" " + " ".join(marks)) if marks else ""
        lines.append(f"{prefix}{branch}#{seq} {n.role}{suffix} {text}")

    def walk(seq: int, prefix: str) -> None:
        kids = conv.children.get(seq, [])
        for i, k in enumerate(kids):
            last = i == len(kids) - 1
            emit(k, prefix, "└─ " if last else "├─ ")
            walk(k, prefix + ("   " if last else "│  "))

    for root in conv.children.get(None, []):
        emit(root)
        walk(root, "")
    return "\n".join(lines)
```

`commands.py`：把 `"tree", "branch", "trunk", "leaf"` 从 `_UNIMPLEMENTED` 移除，替换对应分支（放在 `elif cmd == "where":` 之后）：

```python
        elif cmd == "tree":
            from .treeview import render_tree
            say(render_tree(conv))
        elif cmd == "branch":
            if not rest:
                say("用法: /branch <seq>")
            else:
                conv.set_pointer(int(rest))
                say(f"指针 → #{conv.pointer}；下一条输入长新枝")
        elif cmd == "trunk":
            end = conv.trunk_end()
            if end is None:
                say("（空会话）")
            else:
                conv.set_pointer(end)
                say(f"指针 → 主干末端 #{end}")
        elif cmd == "leaf":
            state["leaf_next"] = True
            say("下一条输入 = 无上下文叶子提问")
```

`_UNIMPLEMENTED` 更新为 `{"card", "cards", "pin", "unpin", "system", "model"}`。

- [ ] **Step 4: 跑全部测试确认通过**

Run: `python -m pytest tests/ -q`
Expected: PASS（累计 + 5 passed）

- [ ] **Step 5: Commit**

```bash
git add treechat/cli tests/test_treeview.py tests/test_cli.py
git commit -m "feat(cli): /tree /branch /trunk /leaf — 树视图与分支/叶子操作"
```

---

### Task 11: CLI 卡片与系统命令（/card /cards /pin /unpin /system /model /retry）

**Files:**
- Modify: `treechat/cli/commands.py`
- Test: `tests/test_cli.py`（追加）

- [ ] **Step 1: 追加失败测试 `tests/test_cli.py`**

```python
def test_repl_card_flow(tmp_path, fake_chat, fake_card_client):
    s = _open_session(tmp_path, fake_chat, fake_card_client)
    say, out = _make_say()
    run_repl(
        s, TreeChatConfig(data_dir=tmp_path),
        input_fn=_make_input([
            "讨论一下",            # 2u 3a
            "/card 总结成卡片",     # 默认当前分支段 [2,3]
            "/cards",
            "/quit",
        ]),
        say=say,
    )
    conv = s.conversation
    assert len(conv.cards.all_cards()) == 1
    card = conv.cards.all_cards()[0]
    assert card.instruction == "总结成卡片"
    assert any("卡片已创建" in line for line in out)
    assert any("📌" in line or card.id in line for line in out)


def test_repl_card_show_and_unknown_pin(tmp_path, fake_chat, fake_card_client):
    s = _open_session(tmp_path, fake_chat, fake_card_client)
    say, out = _make_say()
    run_repl(
        s, TreeChatConfig(data_dir=tmp_path),
        input_fn=_make_input([
            "问", "/card 总结",
            "/card show " + s.conversation.cards.all_cards()[0].id,
            "/unpin card_nope",
            "/quit",
        ]),
        say=say,
    )
    assert any("卡片标题" in line for line in out)
    assert any("未知卡片" in line for line in out)


def test_repl_system_and_model_show(tmp_path, fake_chat, fake_card_client):
    s = _open_session(tmp_path, fake_chat, fake_card_client)
    say, out = _make_say()
    run_repl(
        s, TreeChatConfig(data_dir=tmp_path),
        input_fn=_make_input(["/system", "/system 新指令", "/system", "/model", "/quit"]),
        say=say,
    )
    assert s.conversation.system == "新指令"
    assert any("新指令" in line for line in out)
    assert any("当前模型" in line for line in out)


def test_repl_retry_command(tmp_path, fake_chat, fake_card_client):
    s = _open_session(tmp_path, fake_chat, fake_card_client)
    say, out = _make_say()
    fake_chat.fail = True
    run_repl(s, TreeChatConfig(data_dir=tmp_path),
             input_fn=_make_input(["问题", "/quit"]), say=say)
    fake_chat.fail = False
    say2, out2 = _make_say()
    run_repl(s, TreeChatConfig(data_dir=tmp_path),
             input_fn=_make_input(["/retry", "/quit"]), say=say2)
    assert any("mock reply" in line for line in out2)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_cli.py -q`
Expected: FAIL（`/card 尚未实现`）

- [ ] **Step 3: 扩展 `commands.py`**

文件顶部补 import：

```python
import re

from .. import llm_bridge
```

模块级：

```python
_RANGE_RE = re.compile(r"^(\d+)-(\d+)$")
```

`_UNIMPLEMENTED` 删除（全部命令已实现），把 `elif cmd in _UNIMPLEMENTED:` 分支整体移除，替换为：

```python
        elif cmd == "retry":
            seq = conv.unanswered_user()
            if seq is None:
                say("没有待重试的节点")
            else:
                await session.complete(seq)
                node = conv.nodes[conv.pointer]
                say(_reply_line_of(node.seq, node.text))
        elif cmd == "card":
            await _card_command(session, rest, say)
        elif cmd == "cards":
            cards = conv.cards.all_cards()
            if not cards:
                say("（无卡片）")
            for c in cards:
                pin_mark = "📌" if c in conv.cards.pinned_cards() else ""
                say(f"[{c.id}] {c.title} {pin_mark}  来源 {c.from_path}")
        elif cmd == "pin":
            conv.pin(rest)
            say(f"已 pin {rest}")
        elif cmd == "unpin":
            conv.unpin(rest)
            say(f"已 unpin {rest}")
        elif cmd == "system":
            if rest:
                conv.update_system(rest)
                say(f"system 已更新")
            else:
                say(f"system: {conv.system or '（空）'}")
        elif cmd == "model":
            if rest:
                session.client = llm_bridge.create_client(rest)
                say(f"模型已切换（当前会话内有效）: {rest}")
            else:
                cfg = getattr(session.client, "config", None)
                say(f"当前模型: {getattr(cfg, 'model', '（未知）')}")
        else:
            say(f"未知命令: /{cmd}（/help 查看命令）")
```

模块级再补两个辅助：

```python
def _reply_line_of(seq: int, text: str) -> str:
    return f"[#{seq}] {text}"


async def _card_command(session: TreeChatSession, rest: str, say: Say) -> None:
    """/card 子命令：show / export / all / <a>-<b> / 默认当前分支段。"""
    conv = session.conversation
    tokens = rest.split(maxsplit=1)
    head = tokens[0] if tokens else ""
    tail = tokens[1].strip() if len(tokens) > 1 else ""
    if head == "show" and tail:
        card = conv.cards.get(tail)
        say(f"[{card.id}] {card.title}\n{card.body}")
        return
    if head == "export" and tail:
        sub = tail.split(maxsplit=1)
        if len(sub) < 2:
            say("用法: /card export <id> <file>")
            return
        path = session.export_card(sub[0], Path(sub[1]))
        say(f"已导出 → {path}")
        return
    instruction = tail or "总结为卡片"
    if head == "all":
        if conv.pointer is None:
            say("（空会话）")
            return
        seqs = [n.seq for n in conv.path_to(conv.pointer)]
        cid = await session.make_card(instruction, from_seqs=seqs)
    elif (m := _RANGE_RE.match(head)) :
        seqs = list(range(int(m.group(1)), int(m.group(2)) + 1))
        cid = await session.make_card(instruction, from_seqs=seqs)
    else:
        cid = await session.make_card(rest or "总结为卡片")
    say(f"卡片已创建: [{cid}] {conv.cards.get(cid).title}（默认 pinned，/unpin 可移除）")
```

顶部 import 补 `from pathlib import Path`。

- [ ] **Step 4: 跑全部测试确认通过**

Run: `python -m pytest tests/ -q`
Expected: PASS（累计 + 4 passed）

- [ ] **Step 5: Commit**

```bash
git add treechat/cli/commands.py tests/test_cli.py
git commit -m "feat(cli): 卡片/系统/模型/重试命令 — /card 族 + /system /model /retry"
```

---

### Task 12: 收尾 —— 全量验证 + README + 冒烟说明

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 全量测试**

Run: `python -m pytest tests/ -q`
Expected: 全部通过，0 failed（累计约 50 项）

- [ ] **Step 2: Mock 冒烟（免 key 验证 CLI 面板）**

```bash
python - <<'PY'
import asyncio, json, tempfile
from pathlib import Path
from llm import LLMResponse
from treechat.core.conversation import Conversation
from treechat.core.context import TokenWindowStrategy
from treechat.session import TreeChatSession


class FakeChatClient:  # 内联假客户端（与 tests/conftest.py 同款）
    async def chat(self, messages):
        return LLMResponse(content="mock reply", usage={"input_tokens": 1, "output_tokens": 2})


class FakeCardClient:
    async def complete(self, **kwargs):
        return LLMResponse(content=json.dumps({"title": "卡片标题", "body": "卡片正文"}))


tmp = Path(tempfile.mkdtemp())
conv = Conversation.create(tmp / "demo.jsonl", name="demo", system="你是助手")
s = TreeChatSession(conversation=conv, client=FakeChatClient(),
                    window=TokenWindowStrategy(budget_tokens=10000),
                    card_llm=FakeCardClient())
asyncio.run(s.turn("演示问题"))
cid = asyncio.run(s.make_card("总结"))
from treechat.cli.treeview import render_tree
print(render_tree(conv))
print("card:", cid, "pinned:", [c.id for c in conv.cards.pinned_cards()])
PY
```

Expected: 打印树视图（`◆`/`*` 标注）与卡片 id（`pinned` 列表非空）。

- [ ] **Step 3: 补全 `README.md`**

在 Task 1 的 README 基础上追加使用说明：

```markdown
## 使用

    treechat new 日常 [--system "简洁回答"]   # 新会话进 REPL
    treechat list
    treechat open 日常

REPL 内：裸输入对话；/branch <seq> 在任意历史节点开分支；/leaf 无上下文提问；
/card [指令] 把当前分支段提炼为卡片（默认 pinned，注入后续轮次）；/tree 看树；
/trunk 回主干末端；/retry 重试失败轮次。

## 编程 API

    from treechat import TreeChatSession
    s = TreeChatSession.create(path, "会话名", system="...")
    a = await s.turn("问题")          # 失败留下悬而未答节点，s.turn_retry(节点号) 重试
    cid = await s.make_card("总结为卡片")   # 默认当前分支段，产出即 pinned

## 配置

复用 SpecModule 配置回退链：项目根 `config.json`（providers/models）+ `.env`（API key）
→ `~/.specmodule` 用户级。`/model 名` 会话内切换（重建客户端）。
```

- [ ] **Step 4: Commit**

```bash
git add README.md && git commit -m "docs: README 使用说明 + mock 冒烟验证通过"
```

---

## Self-Review 记录（已执行）

1. **Spec 覆盖**：§2 事件模型/持久时序/完整性 → Task 2/3/5；§3 卡片 → Task 4/7/8/11；§4 组装与窗口 → Task 6（V1 非流式已在 Task 7 落实）；§5 CLI → Task 9/10/11（含 spec 修订后新增的 /retry）；§6 依赖与分层 → Task 1/7（core 零依赖由红线约束）；§7 错误处理 → Task 2/3/5/6/9 各显式报错路径；§8 测试 → 各任务 TDD；§10 V1 范围内无遗漏；V2（compact 压缩/卡片 import）不在本计划。
2. **占位符扫描**：无 TBD/TODO；Task 9 的 `_UNIMPLEMENTED` 是显式的任务间交接状态（Task 10/11 消除），非计划缺口。
3. **类型一致性**：`Card.from_path`、`Conversation.set_pointer/path_to/trunk/trunk_end/fork_point`、`assemble(path, system, cards, strategy)`、`chat_turn(client, system, history, current)`、`extract_card(transcript, instruction, llm_client=)`、`make_card(instruction, seq=, from_seqs=)`、`handle_command(session, config, line, state=, say=)` 在定义处与使用处签名一致；测试中 seq 编号已按 `session_meta=1` 起算核对。
