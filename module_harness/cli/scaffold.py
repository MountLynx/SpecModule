# module_harness/scaffold.py
"""specmodule init 脚手架生成逻辑（纯函数，与 CLI 解析分离）。

生成单文件 python 原生模块骨架（modules/<name>.py）+ 项目级文件缺啥补啥
（幂等）。模板文本以模块级常量存放，可直接单测。CLI 只 import 不重实现。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_module_name(name: str) -> bool:
    """是否合法模块名：合法 Python 标识符。

    模块名同时是文件 stem、discover 导入名、--module 选择器、entry.name
    与默认 run_id——四处必须一致，故限制为标识符。
    """
    return bool(_NAME_RE.match(name or ""))


# ── 项目文件模板（常量）──────────────────────────────────────────────

CONFIG_JSON: dict[str, Any] = {
    "providers": [
        {
            "name": "openai",
            "sdktype": "openai",
            "base_url": None,
            "api_key_env": "OPENAI_API_KEY",
            "timeout": 120,
            "max_retries": 3,
        },
    ],
    "models": [],
}

ENV_EXAMPLE = """# 复制为 .env 并填入真实密钥（.env 已被 .gitignore 排除，不进版本库）
# config.json 的 provider.api_key_env 指定了变量名——两者必须对齐。
OPENAI_API_KEY=
"""

GITIGNORE = """__pycache__/
*.pyc
.env
.specmodule/
"""

SPEC_EXAMPLE_JSON: dict[str, Any] = {"message": "你好，世界"}

README_MD = """# {name}

由 `specmodule init {name}` 生成的模块骨架。默认模板回显 spec 的 `message`
字段（单 script 节点，零 LLM 依赖），`--mock` 即可冒烟。

## 立即冒烟（无需 API key）

```bash
python -m module_harness.cli run --module {name} --mock
```

输出结尾应含 `回显: {"message": "你好，世界"}` 一类的节点摘要。

## 真实 LLM 运行

1. 复制 `.env.example` 为 `.env`，填入真实密钥
   （`config.json` 的 `provider.api_key_env` 指定了密钥的环境变量名）。
2. 在 `config.json` 的 `models` 里填入你用的模型名。
3. 运行：

```bash
python -m module_harness.cli run --module {name} --spec '{"message": "..."}'
```

## 配置分工

- `config.json`：非敏感 provider/model 注册表（连接信息 + `api_key_env`
  指向的**变量名**）。
- `.env`：密钥实际值（gitignored，不进版本库）。
- `rules.txt`：框架级输出格式约束（可选）。

## 命令

| 命令 | 作用 |
|------|------|
| `run --module {name} [--spec ...] [--mock]` | 运行模块（三级实时显示 `--verbose 1..3`） |
| `status [--run-id ...]` | 查询运行状态 |
| `review [--tick N] [--node ...] [--failed]` | 审阅历史时间线 |

`init` 只在文件缺失时补齐项目文件（幂等）；已存在的 `config.json`、
`.gitignore` 等保持原样不被覆盖。`--force` 仅覆盖模块文件
`modules/{name}.py`。
"""

# ── 模块骨架模板（__NAME__ / __DESCRIPTION__ 占位符）────────────────

MODULE_TEMPLATE = '''"""{description} — 由 specmodule init 生成的模块骨架。

一个 module 一个 py 文件（modules/{name}.py）：本文件内声明模块级 ``entry``
变量，CLI ``specmodule run --module {name}`` 经 discover_modules() 导入。

增长路径：模块变大（多模板 / submodule / guard）时，把下方实现拆到独立包
``{name}/``，本文件留薄入口——参照 example/modules/academic_writer.py 的形态。
"""

from __future__ import annotations

from typing import Any

from module_harness.core.config import HarnessConfig
from module_harness.cli.entry import ModuleEntry
from module_harness.infra.events import EventBus
from module_harness.core.outputfmt import OutputFormat
from module_harness.core.registry import HarnessRegistry

# ── harness 组件：HarnessConfig 数据类常量 ─────────────────────────────
# prompt_core 中的 {message} 占位符由 tasklist inputs 的 {spec.message} 运行时填充。
HELLO_HARNESS = HarnessConfig(
    name="hello_llm",
    prompt_core="将以下消息改写为正式的英文：{message}",
    output_format=OutputFormat(type="text"),
    temperature=0.3,
)


# ── script 组件：纯 Python 函数，注册后成为图节点 ──────────────────────
# 签名固定为 fn(view)：view.spec.value 读 spec（仅翻译器上下文），
# view.<节点名>.value 读上游节点输出（runner 上下文）。
def echo(view: Any) -> dict[str, Any]:
    """回显上游 Translate 节点的输出（script 消费节点输出）。"""
    return {"message": view.Translate.value}


# ── 模板组件：TasklistTemplate dict（translation + tasklist）──────────
# translation 是注册在 registry 里的 script 节点，收 view 返回 {Tasks, Flow}。
def _tl_hello(view: Any) -> dict[str, Any]:
    return {
        "Tasks": {
            "Translate": {
                "type": "harness",
                "harness": "hello_llm",
                "inputs": {"message": "{spec.message}"},
            },
            "Echo": {
                "type": "script",
                "script": "echo",
                "inputs": {"data": "Translate"},
            },
        },
        "Flow": "Translate --> Echo",
    }


HELLO_TEMPLATE: dict[str, Any] = {
    "name": "hello",
    "description": "harness 读入 spec 的 message → script 回显（--mock 可冒烟）",
    "translation": {"type": "script", "script": "tl_hello"},
    "tasklist": {
        "Tasks": {
            "Translate": {
                "type": "harness",
                "harness": "hello_llm",
                "inputs": {"message": "{spec.message}"},
            },
            "Echo": {
                "type": "script",
                "script": "echo",
                "inputs": {"data": "Translate"},
            },
        },
        "Flow": "Translate --> Echo",
    },
}


# ── registry 构建：注册本模块全部组件（翻译器 + harness + script）──────
def _build_registry(
    llm_client: Any, template_name: str, event_bus: EventBus
) -> HarnessRegistry:
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)
    reg.harness("hello_llm", HELLO_HARNESS)
    reg.script("echo")(echo)
    reg.script("tl_hello")(_tl_hello)
    return reg


# ── 入口声明：discover_modules() 扫描 modules/*.py 找这个变量 ──────────
entry = ModuleEntry(
    name="{name}",
    description=__DESCRIPTION__,
    templates={"hello": HELLO_TEMPLATE},
    build_registry=_build_registry,
    default_template="hello",
    default_spec={"message": "你好，世界"},
    spec_schema={"message": "str"},
    review_harness=None,  # 固定流程骨架模板；需要一致性审核时改回 "spec_tasklist_review"
)
'''


@dataclass
class ScaffoldResult:
    """init 结果：本次创建/跳过的文件路径清单。"""

    created: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)


def _write_if_missing(path: Path, content: str, result: ScaffoldResult) -> None:
    """缺啥补啥：已存在则跳过（不覆盖），否则写入并记入 created。"""
    if path.exists():
        result.skipped.append(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result.created.append(path)


def build_module_source(name: str, description: str = "") -> str:
    """渲染 modules/<name>.py 源码（name 内插 + description 安全嵌入为字面量）。"""
    desc = description or f"{name} — 脚手架生成的示例模块"
    return MODULE_TEMPLATE.replace("{name}", name).replace(
        "__DESCRIPTION__", json.dumps(desc, ensure_ascii=False)
    )


def scaffold(
    name: str,
    *,
    base_dir: str | Path = ".",
    force: bool = False,
    description: str = "",
) -> ScaffoldResult:
    """生成模块骨架 + 项目文件缺啥补啥。

    - 模块名非法 → ValueError（CLI 据此退出码 1，零文件生成）。
    - 模块文件已存在且未 ``force`` → ValueError。
    - ``force`` 仅覆盖模块文件；项目文件永不覆盖（幂等）。
    """
    if not validate_module_name(name):
        raise ValueError(
            f"模块名 '{name}' 不是合法 Python 标识符"
            "（须匹配 ^[A-Za-z_][A-Za-z0-9_]*$）"
        )
    base = Path(base_dir)
    result = ScaffoldResult()

    # 模块文件是本次生成的主角：--force 才覆盖，否则报错。
    module_path = base / "modules" / f"{name}.py"
    if module_path.exists() and not force:
        raise ValueError(f"模块文件已存在: {module_path}（用 --force 覆盖）")
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text(build_module_source(name, description), encoding="utf-8")
    result.created.append(module_path)

    # 项目文件缺啥补啥（幂等，永不覆盖）。
    _write_if_missing(base / "config.json", json.dumps(CONFIG_JSON, ensure_ascii=False, indent=2) + "\n", result)
    _write_if_missing(base / ".env.example", ENV_EXAMPLE, result)
    _write_if_missing(base / ".gitignore", GITIGNORE, result)
    _write_if_missing(
        base / "spec.example.json",
        json.dumps(SPEC_EXAMPLE_JSON, ensure_ascii=False, indent=2) + "\n",
        result,
    )
    _write_if_missing(
        base / "README.md",
        README_MD.replace("{name}", name),
        result,
    )
    return result


# ── 目录形态（init --dir）：与已装模块同构的 pack 目录骨架 ─────────────

DIR_MODULE_JSON: dict[str, Any] = {
    "name": "__NAME__",
    "version": "0.1.0",
    "description": "__DESCRIPTION__",
    "submodule": True,
    "spec_schema": {"input": {"message": "str"}, "output": {"message": "str"}},
    "requires": [],
    "modules": [],
    "tasklist": {
        "Tasks": {
            "Greet": {
                "type": "script",
                "script": "greet",
            },
        },
        "Flow": "[Greet]",
    },
}

DIR_SCRIPT_TEMPLATE = '''\
"""{name} 脚本组件：纯 Python 函数，注册后成为图节点。"""

from __future__ import annotations


def greet(view):
    """回显上游输入（tasklist 固定：无上游依赖，直接输出）。"""
    return {"message": "hello from {name}"}
'''

DIR_HARNESS_EXAMPLE = '''\
"""{name} harness 组件示例（JSON 文件）：LLM 调用节点，三层 prompt。

复制为 ``harnesses/<名>.json`` 并在 module.json 的 tasklist 中引用：
    {
        "type": "harness",
        "harness": "<名>",
        "inputs": {"text": "{spec.message}"},
        "outputformat": {"type": "text"}
    }
"""

'''

DIR_README = """# {name}

由 `specmodule init --dir {name}` 生成的目录形态模块骨架。

与已装模块同构（pack 格式）：`module.json` 声明 spec_schema/tasklist，
`scripts/` 放 Python 函数，`harnesses/` 放 LLM 调用配置（JSON），
`commands/` 放 shell 命令配置。

运行：

```bash
specmodule run --module {name} --spec '{"message": "hi"}' --mock
specmodule publish {name} --from .
```
"""


def scaffold_dir(
    name: str,
    *,
    base_dir: str | Path = ".",
    force: bool = False,
    description: str = "",
) -> ScaffoldResult:
    """生成目录形态模块骨架（--dir）：pack 同构目录 + 项目文件缺啥补啥。

    与 ``scaffold``（单文件）同语义：模块名非法 → ValueError；模块目录
    已存在且未 force → ValueError；force 仅覆盖模块目录，项目文件永不覆盖。
    """
    if not validate_module_name(name):
        raise ValueError(
            f"模块名 '{name}' 不是合法 Python 标识符"
            "（须匹配 ^[A-Za-z_][A-Za-z0-9_]*$）"
        )
    base = Path(base_dir)
    result = ScaffoldResult()
    mod_dir = base / "modules" / name
    if mod_dir.exists() and not force:
        raise ValueError(f"模块目录已存在: {mod_dir}（用 --force 覆盖）")
    mod_dir.mkdir(parents=True, exist_ok=True)
    result.created.append(mod_dir)

    manifest = dict(DIR_MODULE_JSON)
    manifest["name"] = name
    manifest["description"] = description or f"{name} — 脚手架生成的示例模块"
    _write_if_missing(
        mod_dir / "module.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        result,
    )
    for sub in ("scripts", "harnesses", "commands", "guards", "submodules"):
        d = mod_dir / sub
        d.mkdir(exist_ok=True)
        result.created.append(d)
    _write_if_missing(
        mod_dir / "scripts" / "greet.py",
        DIR_SCRIPT_TEMPLATE.replace("{name}", name),
        result,
    )
    _write_if_missing(
        mod_dir / "harnesses" / "README.txt",
        DIR_HARNESS_EXAMPLE.replace("{name}", name),
        result,
    )

    # 项目文件缺啥补啥（幂等，永不覆盖）——与单文件形态共用。
    _write_if_missing(base / "config.json", json.dumps(CONFIG_JSON, ensure_ascii=False, indent=2) + "\n", result)
    _write_if_missing(base / ".env.example", ENV_EXAMPLE, result)
    _write_if_missing(base / ".gitignore", GITIGNORE, result)
    _write_if_missing(
        base / "spec.example.json",
        json.dumps(SPEC_EXAMPLE_JSON, ensure_ascii=False, indent=2) + "\n",
        result,
    )
    _write_if_missing(
        base / "README.md",
        DIR_README.replace("{name}", name),
        result,
    )
    return result