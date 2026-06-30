# Command 节点设计文档

> 日期：2026-06-30 | 状态：已确认，待实现

## 概述

在 module_harness 中新增第三种 node type：`"command"`。一行 shell 命令作为一个节点执行（subprocess），零样板代码。与 harness/script 平级，复用 tickflow 引擎和 EventBus。

## 设计原则

- **字符串即节点**：`CommandConfig` 定义一个命令，框架自动生成 body 闭包
- 复杂管道/多步操作用已有的 `@reg.script()` 写 Python
- 跟随 harness 的注册模式：`reg.command(name, config)`

---

## 数据模型

### CommandConfig

```python
@dataclass
class CommandConfig:
    command: str                        # shell 命令字符串
    timeout: float = 60.0               # 超时秒数（subprocess TimeoutExpired → Failure）
    cwd: str | None = None              # 工作目录，None = 进程当前 cwd
    env: dict[str, str] | None = None   # 额外环境变量（合并到当前进程 env）
    capture_output: bool = True         # True = 捕获 stdout/stderr
    shell: bool = True                  # True = 通过 shell 执行
```

### TaskDefinition 扩展

`type` 枚举加 `"command"`，Harness/script 原有字段不变，新增：

```python
command: str | None = None    # type="command" 时使用，引用 reg.command 注册名
timeout: float | None = None  # 覆盖 config 中 timeout
cwd: str | None = None        # 覆盖 config 中 cwd
```

### 事件类型

新增（`events.py`）：

| 事件 | 父类 | 字段 |
|------|------|------|
| `CommandStarted` | `HarnessEvent` | — |
| `CommandCompleted` | `HarnessEvent` | `stdout: str`, `stderr: str`, `returncode: int` |
| `CommandFailed` | `HarnessEvent` | `error: str` |

### Command 类（类似 Harness）

```python
class Command:
    """持有 CommandConfig + EventBus，生成 body 闭包。"""

    def __init__(self, config: CommandConfig, event_bus: EventBus): ...

    def build_body(self, *, timeout=None, cwd=None):
        """返回 sync body: (DictView) -> dict | Failure"""
        # 闭包执行:
        #   1. emit CommandStarted
        #   2. subprocess.run(command, shell=True, timeout=..., cwd=..., capture_output=True)
        #   3. 成功 → emit CommandCompleted → return {"stdout": ..., "stderr": ..., "returncode": 0}
        #   4. TimeoutExpired → emit CommandFailed → return Failure("超时", type="llm")
        #   5. 其他异常 → emit CommandFailed → return Failure(str(e), type="llm")
```

### 注册 API

`HarnessRegistry` 新增：

```python
def command(self, name: str, config: CommandConfig) -> "HarnessRegistry": ...
```

### Tasklist 示例

```json
{
  "Tasks": {
    "A": {
      "type": "command",
      "command": "git_status",
      "cwd": "/path/to/repo",
      "timeout": 30
    }
  },
  "Flow": "A"
}
```

---

## 文件变更

| 操作 | 文件 | 内容 |
|------|------|------|
| 创建 | `module_harness/command.py` | `CommandConfig` + `Command` 类 |
| 修改 | `module_harness/events.py` | 新增 `CommandStarted`, `CommandCompleted`, `CommandFailed` |
| 修改 | `module_harness/spec.py` | `TaskDefinition.type` + `"command"`，加字段 |
| 修改 | `module_harness/registry.py` | 新增 `reg.command(name, config)` |
| 修改 | `module_harness/graph_builder.py` | `_register_body` 加 command 分支 |
| 修改 | `module_harness/translator.py` | `TasklistValidator` 加 command 引用检查 |
| 修改 | `module_harness/__init__.py` | 导出新符号 |
| 创建 | `module_harness/tests/test_command.py` | Command 单元测试 |

## 执行流程

```
tick N, Node A (command body):
  CommandStarted
    → subprocess.run("git status --porcelain", shell=True, timeout=30)
    → CommandCompleted(stdout="M README.md", stderr="", returncode=0)
    → return {"stdout": "M README.md", "stderr": "", "returncode": 0}

若超时:
  CommandStarted
    → subprocess.run(... 300s ...) → TimeoutExpired
    → CommandFailed(error="命令超时 (30s)")
    → return Failure("命令超时 (30s)", type="llm")
```

## 错误处理

| 场景 | 行为 |
|------|------|
| 命令超时 | `Failure(type="llm")`，下游 AND-join 跳过 |
| 命令返回非 0 | 仍返回 dict（stdout/stderr/returncode），下游 guard 判断是否重试 |
| 命令本身抛异常 | `Failure(type="llm")` |

## 全局约束

- tickflow 零修改
- llm 模块零修改
- 已有 module_harness 文件仅追加修改（不重写现有逻辑）
- body 为 sync def（subprocess 同步阻塞），AsyncRunner 兼容
