"""store 共享层：家目录、搜索路径、模块统一枚举（module-user-store 主线）。

生态契约：TUI/MCP/Web 可视化消费 ``store_home`` / ``search_paths`` /
``list_modules`` 作为统一枚举；CLI 管理面（install/uninstall/setup/publish/
update）与 run 解析共用本层。零第三方依赖（stdlib 仅 ``importlib.metadata``）。

目录约定（``SPECMODULE_HOME`` 覆盖，默认 ``~/.specmodule``）：:

    ~/.specmodule/
    ├─ modules/<name>/            pack 格式模块目录（唯一逻辑真相）
    ├─ manifests/<name>.json      {source, version, files:{rel→sha256}, installed_at}
    ├─ .env / config.json / rules.txt   用户级配置（回退层）
    └─ cache/                     临时 clone/下载缓存，可清

模块搜索路径 = [cwd/modules] + $SPECMODULE_PATH（os.pathsep 分隔）+ store/modules
            + pip dist entry points（``specmodule.modules`` 组，附加来源）。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# entry points 组名：pip 分发的 module 声明 ``specmodule.modules``，
# 值 = 模块内函数（调用返回 pack 目录路径列表）
ENTRY_POINT_GROUP = "specmodule.modules"


def store_home() -> Path:
    """store 家目录：``SPECMODULE_HOME`` 非空则用之，否则 ``~/.specmodule``。

    目录惰性创建（调用即建，幂等）。
    """
    env = os.environ.get("SPECMODULE_HOME", "").strip()
    home = Path(env) if env else Path.home() / ".specmodule"
    home.mkdir(parents=True, exist_ok=True)
    return home


def store_config_dir() -> Path:
    """store 级配置所在目录（= 家目录本身，与 modules/ 等并列）。"""
    return store_home()


def modules_dir() -> Path:
    """store 内已安装模块目录（``<home>/modules``）。"""
    d = store_home() / "modules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def manifests_dir() -> Path:
    """store 内安装期元数据目录（``<home>/manifests``）。"""
    d = store_home() / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    """store 内缓存目录（``<home>/cache``，可清）。"""
    d = store_home() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def search_paths() -> list[Path]:
    """模块搜索路径：[cwd/modules] + $SPECMODULE_PATH + store/modules。

    ``$SPECMODULE_PATH`` 用 ``os.pathsep``（Windows ``;`` / POSIX ``:``）分隔，
    与 PATH 语义一致；条目可以是任意目录（手动摆放入口）。
    只返回存在的目录（不存在的路径不参与发现，避免误报）。
    """
    out: list[Path] = []
    cwd_modules = Path.cwd() / "modules"
    if cwd_modules.is_dir():
        out.append(cwd_modules)
    env = os.environ.get("SPECMODULE_PATH", "").strip()
    if env:
        for raw in env.split(os.pathsep):
            p = Path(raw.strip())
            if p.is_dir():
                out.append(p)
    store_mods = store_home() / "modules"
    if store_mods.is_dir():
        out.append(store_mods)
    return out


def pip_entry_point_dirs() -> list[Path]:
    """pip 分发声明的 pack 目录（``specmodule.modules`` entry points）。

    值 = 模块内函数（无参），返回 pack 目录路径或路径列表。加载/调用失败
    记录 warning 并跳过（不阻断整体枚举）。
    """
    out: list[Path] = []
    try:
        eps = importlib.metadata.entry_points()
        selected = (
            eps.select(group=ENTRY_POINT_GROUP)
            if hasattr(eps, "select") else eps.get(ENTRY_POINT_GROUP, [])
        )
    except Exception:
        log.exception("读取 entry points 失败")
        return out
    for ep in selected:
        try:
            fn = ep.load()
            result = fn()
            paths = result if isinstance(result, (list, tuple)) else [result]
            for p in paths:
                p = Path(p)
                if p.is_dir():
                    out.append(p)
        except Exception:
            log.exception("entry point 加载失败（跳过）: %s", ep.name)
    return out


# ── 统一模块引用 ────────────────────────────────────────────────────

@dataclass
class ModuleSource:
    """统一模块引用：三类来源（entry 单文件 / packed 目录 / pip dist）。"""

    name: str
    kind: str                 # "entry" | "packed" | "pip"
    path: Path                # entry 文件路径 | pack 目录 | pip pack 目录
    description: str = ""
    version: str = ""
    priority: int = 0         # 搜索路径序（0 最高）；pip 附加来源排最后
    pip_dist: str | None = None   # pip 分发名（kind="pip" 时）

    @property
    def is_packed(self) -> bool:
        """是否 pack 目录形态（需经 ModuleLoader 加载）。"""
        return self.kind in ("packed", "pip")


def _read_module_json(path: Path) -> dict[str, Any]:
    """读取 pack 目录 module.json（缺失/损坏 → {}）。"""
    try:
        return json.loads((path / "module.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _entry_description(entry: Any) -> str:
    return getattr(entry, "description", "")


def list_modules(
    search: list[Path] | None = None,
    include_pip: bool = True,
) -> dict[str, list[ModuleSource]]:
    """枚举全部可用模块（同名多来源全量展示）。

    合并三类来源：
    - ``entry``：搜索路径下 ``<dir>/<name>.py``（模块级 ``entry`` 变量声明，
      经 ``discover_modules`` 解析，导入失败跳过并 warning）
    - ``packed``：搜索路径下 ``<dir>/<name>/module.json`` 存在的子目录
      （不校验内容——加载时由 ModuleLoader 负责；此处只做轻量元数据读取）
    - ``pip``：``specmodule.modules`` entry points 指向的 pack 目录

    返回 ``{name: [ModuleSource...]}``，列表内按 priority 升序（先命中者优先）。
    同名冲突不静默改名：解析按列表首项，管理面/列表命令全量展示。
    """
    search = search if search is not None else search_paths()
    out: dict[str, list[ModuleSource]] = {}

    def add(src: ModuleSource) -> None:
        out.setdefault(src.name, []).append(src)

    for priority, d in enumerate(search):
        # entry 单文件
        if d.is_dir():
            from .entry import discover_modules

            entries = discover_modules(d)
            for name, entry in entries.items():
                add(ModuleSource(
                    name=name,
                    kind="entry",
                    path=d / f"{name}.py",
                    description=_entry_description(entry),
                    priority=priority,
                ))
            # packed 目录：<dir>/<name>/module.json
            for sub in sorted(p for p in d.iterdir() if p.is_dir()):
                manifest = _read_module_json(sub)
                if manifest:
                    add(ModuleSource(
                        name=manifest.get("name", sub.name),
                        kind="packed",
                        path=sub,
                        description=manifest.get("description", ""),
                        version=manifest.get("version", ""),
                        priority=priority,
                    ))
    if include_pip:
        try:
            pip_dirs = pip_entry_point_dirs()
        except Exception:
            log.exception("pip entry points 枚举失败（跳过 pip 来源）")
            pip_dirs = []
        for d in pip_dirs:
            manifest = _read_module_json(d)
            if manifest:
                add(ModuleSource(
                    name=manifest.get("name", d.name),
                    kind="pip",
                    path=d,
                    description=manifest.get("description", ""),
                    version=manifest.get("version", ""),
                    priority=len(search),
                    pip_dist=None,  # entry point 名不保证等于 dist 名
                ))
    return out


def resolve_module(
    name: str,
    search: list[Path] | None = None,
) -> ModuleSource | None:
    """按名解析：搜索路径序第一个命中（D3：PATH 惯例，不静默改名）。"""
    sources = list_modules(search=search).get(name, [])
    return sources[0] if sources else None


# ── 安装管理（install/uninstall/update/publish 共用）──────────────────

def file_sha256(path: Path) -> str:
    """文件 sha256（安装期元数据；不做物理去重，仅脏检测）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_dotenv(path: Path) -> dict[str, str]:
    """解析 .env 文件为 {key: value}（忽略注释/空行；不做 env 写入）。"""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    return out


def validate_pack_dir(path: Path) -> dict[str, Any]:
    """校验 pack 目录（manifest 解析 + 结构检查），返回 manifest。

    校验失败抛 ValueError（带原因）；**不**实例化 LLM client（D6）。
    校验点：module.json 存在且可解析、name 合法、tasklist 存在、
    scripts/harnesses/commands 目录引用完整（经 ModuleLoader 语义）。
    """
    manifest_path = path / "module.json"
    if not manifest_path.is_file():
        raise ValueError(f"不是有效 pack 目录（缺少 module.json）: {path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"module.json 解析失败: {e}") from e
    if not isinstance(manifest, dict):
        raise ValueError("module.json 顶层必须是对象")
    name = manifest.get("name")
    if not name:
        raise ValueError("module.json 缺少 'name'")
    tasklist = manifest.get("tasklist")
    if tasklist is None:
        raise ValueError("module.json 缺少 'tasklist'")
    # 引用完整性经 ModuleLoader 校验（requires/provides/子模块目录）
    try:
        from .loader import ModuleLoader

        ModuleLoader().load(path, lazy_client=True)
    except Exception as e:
        raise ValueError(f"pack 校验失败: {e}") from e
    return manifest


def install_pack(
    src: Path,
    *,
    source: str,
    name: str | None = None,
) -> Path:
    """把校验通过的 pack 目录复制进 store/modules，写 manifest。

    ``source``：来源描述（本地路径 / git URL / pip 包名），写入 manifest。
    校验（validate_pack_dir）先于任何写入——校验失败零落盘。
    同名已存在 → 抛 ValueError（提示 uninstall，不覆盖）。
    """
    manifest = validate_pack_dir(src)
    target_name = name or manifest["name"]
    dest = modules_dir() / target_name
    if dest.exists():
        raise ValueError(
            f"模块 '{target_name}' 已存在于 store（{dest}）——"
            "先 specmodule uninstall 再重装"
        )
    import shutil

    shutil.copytree(src, dest)
    try:
        files = {
            str(p.relative_to(dest)): file_sha256(p)
            for p in dest.rglob("*")
            if p.is_file()
        }
        manifest_path = manifests_dir() / f"{target_name}.json"
        manifest_path.write_text(json.dumps({
            "name": target_name,
            "source": source,
            "version": manifest.get("version", "0.1.0"),
            "files": files,
            "installed_at": _now_iso(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # 写入失败回滚（manifest 失败 → 删除已复制目录，保持零残留）
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def load_manifest(name: str) -> dict[str, Any] | None:
    """读取安装 manifest；缺失/损坏 → None。"""
    p = manifests_dir() / f"{name}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def uninstall_pack(name: str) -> bool:
    """移除模块目录 + manifest。不存在 → False（调用方报错）。"""
    dest = modules_dir() / name
    removed = False
    if dest.is_dir():
        import shutil

        shutil.rmtree(dest)
        removed = True
    mp = manifests_dir() / f"{name}.json"
    if mp.is_file():
        mp.unlink()
        removed = True
    return removed


# ── update 脏检测 ────────────────────────────────────────────────────

def check_updates(name: str, src: Path) -> dict[str, Any]:
    """按 manifest 比对来源目录与已装模块：返回差异清单。

    返回::

        {
            "name": ...,
            "changed": ["rel/path", ...],   # 来源内容变化的文件（相对 manifest）
            "added": [...],                 # 来源新增文件
            "removed": [...],               # 来源删除的文件（manifest 有、来源无）
            "untracked": [...],             # 已装文件不在 manifest（本地新增）
            "local_modified": [...],        # 已装文件内容 ≠ manifest（本地改动）
        }

    manifest 缺失（非 install 安装的模块）→ ValueError（提示用 install）。
    """
    manifest = load_manifest(name)
    if manifest is None:
        raise ValueError(
            f"模块 '{name}' 无安装 manifest（未用 install/publish 安装）——"
            "无法做脏检测，请用 install 重装"
        )
    recorded = manifest.get("files", {})
    installed = modules_dir() / name

    changed: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    untracked: list[str] = []
    local_modified: list[str] = []

    # 来源侧
    src_files = {
        str(p.relative_to(src)): p
        for p in src.rglob("*")
        if p.is_file()
    }
    for rel, sp in src_files.items():
        if rel in recorded:
            if file_sha256(sp) != recorded[rel]:
                changed.append(rel)
        else:
            added.append(rel)
    # 已装侧
    installed_files = {
        str(p.relative_to(installed)): p
        for p in installed.rglob("*")
        if p.is_file()
    }
    for rel in recorded:
        if rel not in src_files:
            removed.append(rel)
    for rel, ip in installed_files.items():
        if rel not in recorded:
            untracked.append(rel)
        elif file_sha256(ip) != recorded[rel]:
            local_modified.append(rel)

    return {
        "name": name,
        "changed": sorted(changed),
        "added": sorted(added),
        "removed": sorted(removed),
        "untracked": sorted(untracked),
        "local_modified": sorted(local_modified),
    }


def apply_update(name: str, src: Path) -> None:
    """无差异直接替换（先备份再覆盖）；有差异列清单交调用方决策。

    本函数只做"确认后"的执行：覆盖已装目录内容为来源目录内容，
    重写 manifest（来源/版本/哈希/installed_at 刷新）。未确认不调用。
    """
    import shutil

    dest = modules_dir() / name
    backup = dest.with_name(f".{name}.bak")
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    if dest.exists():
        shutil.move(str(dest), str(backup))
    try:
        shutil.copytree(src, dest)
        shutil.rmtree(backup, ignore_errors=True)
        old = load_manifest(name) or {}
        manifest = validate_pack_dir(src)
        files = {
            str(p.relative_to(dest)): file_sha256(p)
            for p in dest.rglob("*")
            if p.is_file()
        }
        mp = manifests_dir() / f"{name}.json"
        mp.write_text(json.dumps({
            "name": name,
            "source": old.get("source") or str(src),
            "version": manifest.get("version", "0.1.0"),
            "files": files,
            "installed_at": _now_iso(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # 失败回滚备份
        shutil.rmtree(dest, ignore_errors=True)
        if backup.exists():
            shutil.move(str(backup), str(dest))
        raise
