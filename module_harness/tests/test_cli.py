# module_harness/tests/test_cli.py
"""specmodule CLI 测试：run / status / review / resume 命令路径（hello/fail/resume_hello 测试模块）。"""

from __future__ import annotations

import json

import pytest

from module_harness.cli import main

HELLO_PY = '''\
"""hello 测试模块：单 script 节点 Greet（无 LLM 依赖）。"""
from __future__ import annotations

from module_harness.cli.entry import ModuleEntry
from module_harness.infra.events import EventBus
from module_harness.core.registry import HarnessRegistry


def _registry_for(llm_client, template_name, event_bus):
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus or EventBus.null())

    @reg.script("Greet")
    def greet(view):
        return {"greeting": "hello world"}

    @reg.script("tl")
    def tl(view):
        return {
            "Tasks": {"Greet": {"type": "script", "script": "Greet"}},
            "Flow": "[Greet]",
        }

    return reg


entry = ModuleEntry(
    name="hello",
    description="hello 测试模块",
    templates={
        "hello": {
            "name": "hello",
            "description": "hello 模板",
            "translation": {"type": "script", "script": "tl"},
            "tasklist": {
                "Tasks": {"Greet": {"type": "script", "script": "Greet"}},
                "Flow": "[Greet]",
            },
        },
    },
    build_registry=_registry_for,
    default_spec={"name": "world"},
    default_template="hello",
    review_harness=None,
)
'''

FAIL_PY = '''\
"""fail 测试模块：单 script 节点 Boom 返回 Failure（type=llm，运行继续）。"""
from __future__ import annotations

from tickflow import Failure
from module_harness.cli.entry import ModuleEntry
from module_harness.infra.events import EventBus
from module_harness.core.registry import HarnessRegistry


def _registry_for(llm_client, template_name, event_bus):
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus or EventBus.null())

    @reg.script("Boom")
    def boom(view):
        return Failure("boom failed", type="llm")

    @reg.script("tl")
    def tl(view):
        return {
            "Tasks": {"Boom": {"type": "script", "script": "Boom"}},
            "Flow": "[Boom]",
        }

    return reg


entry = ModuleEntry(
    name="fail",
    description="fail 测试模块",
    templates={
        "fail": {
            "name": "fail",
            "description": "fail 模板",
            "translation": {"type": "script", "script": "tl"},
            "tasklist": {
                "Tasks": {"Boom": {"type": "script", "script": "Boom"}},
                "Flow": "[Boom]",
            },
        },
    },
    build_registry=_registry_for,
    default_template="fail",
    review_harness=None,
)
'''

RESUME_HELLO_PY = '''\
"""resume_hello 测试模块：两节点流水线 [A] --> B（无 LLM 依赖）。"""
from __future__ import annotations

from module_harness.cli.entry import ModuleEntry
from module_harness.infra.events import EventBus
from module_harness.core.registry import HarnessRegistry


def _registry_for(llm_client, template_name, event_bus):
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus or EventBus.null())

    @reg.script("A")
    def a(view):
        return {"value": "from A"}

    @reg.script("B")
    def b(view):
        data = view.field("value")
        return {"greeting": "hello " + data["value"]}

    @reg.script("tl")
    def tl(view):
        return {
            "Tasks": {
                "A": {"type": "script", "script": "A"},
                "B": {"type": "script", "script": "B", "inputs": {"value": "A"}},
            },
            "Flow": "[A] --> B",
        }

    return reg


entry = ModuleEntry(
    name="resume_hello",
    description="resume 测试模块",
    templates={
        "resume_hello": {
            "name": "resume_hello",
            "description": "resume_hello 模板",
            "translation": {"type": "script", "script": "tl"},
            "tasklist": {
                "Tasks": {
                    "A": {"type": "script", "script": "A"},
                    "B": {"type": "script", "script": "B", "inputs": {"value": "A"}},
                },
                "Flow": "[A] --> B",
            },
        },
    },
    build_registry=_registry_for,
    default_spec={"name": "world"},
    default_template="resume_hello",
    review_harness=None,
)
'''


@pytest.fixture
def modules_dir(tmp_path):
    d = tmp_path / "modules"
    d.mkdir()
    (d / "hello.py").write_text(HELLO_PY, encoding="utf-8")
    (d / "fail.py").write_text(FAIL_PY, encoding="utf-8")
    (d / "resume_hello.py").write_text(RESUME_HELLO_PY, encoding="utf-8")
    return d


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def gc_collect():
    """进程内 CLI run 后 runner 的 SqliteBackend 连接挂在引用环上（由调用方
    管理是库的既定行为），Windows 下未释放前 delete-run 会被文件锁绊倒——
    删除前显式跑一轮循环 GC 使时机确定化（真实消费端跨进程，无此问题）。"""
    import gc

    gc.collect()


def _run(cwd, *argv):
    """run 子命令 + 指向测试模块目录。"""
    return main(["run", *argv, "--modules-dir", str(cwd / "modules")])


def _resume(cwd, *argv):
    """resume 子命令 + 指向测试模块目录。"""
    return main(["resume", *argv, "--modules-dir", str(cwd / "modules")])


def _checkpoints(cwd, *argv):
    return main(["checkpoints", *argv])


def _snapshot(cwd, *argv):
    return main(["snapshot", *argv])


def _rollback(cwd, *argv):
    return main(["rollback", *argv, "--modules-dir", str(cwd / "modules")])


def _checkpoint(cwd, *argv):
    return main(["checkpoint", *argv])


def _visualize(cwd, *argv):
    return main(["visualize", *argv, "--modules-dir", str(cwd / "modules")])


class TestRun:
    def test_hello_success(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        out = capsys.readouterr().out
        assert "运行完成" in out
        assert "hello world" in out
        assert (cwd / ".specmodule" / "runs" / "hello" / "run.sqlite").exists()

    def test_default_spec(self, cwd, modules_dir, capsys):
        # 无 --spec/--spec-file，走 entry.default_spec
        assert _run(cwd, "--module", "hello", "--mock") == 0

    def test_module_not_found(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "nope", "--mock") == 1
        assert "未找到" in capsys.readouterr().err

    def test_missing_spec(self, cwd, modules_dir, capsys):
        # fail 无 default_spec 也不传 spec → 报错
        assert _run(cwd, "--module", "fail", "--mock") == 1
        assert "缺少 spec" in capsys.readouterr().err

    def test_tasklist_template_mutually_exclusive(self, cwd, modules_dir, capsys):
        assert _run(
            cwd, "--module", "hello", "--mock",
            "--template", "hello", "--tasklist", "x.json",
        ) == 1
        assert "互斥" in capsys.readouterr().err

    def test_verbose3(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock", "--verbose", "3") == 0
        out = capsys.readouterr().out
        assert "═══" in out
        assert "[ok]" in out

    def test_tasklist_file(self, cwd, modules_dir, capsys):
        tl = cwd / "tl.json"
        tl.write_text(
            json.dumps({
                "Tasks": {"Greet": {"type": "script", "script": "Greet"}},
                "Flow": "[Greet]",
            }),
            encoding="utf-8",
        )
        assert _run(cwd, "--module", "hello", "--mock", "--tasklist", str(tl)) == 0
        assert "运行完成" in capsys.readouterr().out


class TestStatus:
    def test_status_text(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert main(["status", "--run-id", "hello"]) == 0
        assert "phase=done" in capsys.readouterr().out

    def test_status_json(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert main(["status", "--run-id", "hello", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["module_id"] == "hello"
        assert data["phase"] == "done"

    def test_status_no_run(self, cwd, capsys):
        assert main(["status", "--run-id", "ghost"]) == 1
        assert "无运行记录" in capsys.readouterr().err


class TestReview:
    def test_review_timeline(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert main(["review", "--run-id", "hello"]) == 0
        out = capsys.readouterr().out
        assert "Greet ✓" in out
        assert "最新 tick" in out

    def test_review_json(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert main(["review", "--run-id", "hello", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["module_id"] == "hello"
        assert data["entries"][0]["node"] == "Greet"

    def test_review_failed(self, cwd, modules_dir, capsys):
        # fail 无 default_spec——显式传 spec 让运行进入（Boom 失败路径）
        assert _run(cwd, "--module", "fail", "--mock", "--spec", "{}") == 0
        capsys.readouterr()
        assert main(["review", "--run-id", "fail", "--failed"]) == 0
        out = capsys.readouterr().out
        assert "Boom ✗" in out
        assert "boom failed" in out

    def test_review_no_run(self, cwd, capsys):
        assert main(["review", "--run-id", "ghost"]) == 1
        assert "无运行记录" in capsys.readouterr().err


class TestResume:
    def test_resume_default(self, cwd, modules_dir, capsys):
        # 截断后缺省 resume：从最新 tick 快照续跑，B 补齐、流程完成
        assert _run(cwd, "--module", "resume_hello", "--mock", "--max-ticks", "1") == 0
        capsys.readouterr()
        assert _resume(cwd, "--module", "resume_hello", "--mock") == 0
        out = capsys.readouterr().out
        assert "续跑完成" in out
        assert "hello from A" in out  # B 消费 A 的输出

    def test_resume_falls_back_to_archived_tasklist(self, cwd, modules_dir, capsys):
        """tasklist 通道启动的 run（模块无 default_template）恢复时沿用归档
        tasklist——此前此类 run 只能显式 --tasklist 续跑（Web 恢复通道实测缺陷）。"""
        (modules_dir / "tasklist_only.py").write_text(
            RESUME_HELLO_PY.replace(
                '    default_template="resume_hello",\n', ""
            ).replace('name="resume_hello"', 'name="tasklist_only"'),
            encoding="utf-8",
        )
        tasklist_file = cwd / "tl.json"
        tasklist_file.write_text(
            '{"Tasks": {"A": {"type": "script", "script": "A"},'
            ' "B": {"type": "script", "script": "B", "inputs": {"value": "A"}}},'
            ' "Flow": "[A] --> B"}',
            encoding="utf-8",
        )
        assert (
            _run(cwd, "--module", "tasklist_only", "--tasklist", str(tasklist_file),
                 "--mock", "--max-ticks", "1")
            == 0
        )
        capsys.readouterr()
        # 不带 --tasklist/--template：归档兜底
        assert _resume(cwd, "--module", "tasklist_only", "--mock") == 0
        captured = capsys.readouterr()
        assert "沿用 module_inputs 归档 tasklist" in captured.err
        assert "续跑完成" in captured.out

    def test_resume_explicit_tick(self, cwd, modules_dir, capsys):
        # 截断后可用快照 tick 1（快照编号 N 在 tick N-1 结束后落盘——A 完成态）
        assert _run(cwd, "--module", "resume_hello", "--mock", "--max-ticks", "1") == 0
        capsys.readouterr()
        assert _resume(cwd, "1", "--module", "resume_hello", "--mock") == 0
        out = capsys.readouterr().out
        assert "续跑完成" in out
        assert "hello from A" in out

    def test_resume_invalid_target(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "resume_hello", "--mock", "--max-ticks", "1") == 0
        capsys.readouterr()
        assert _resume(cwd, "99", "--module", "resume_hello", "--mock") == 1
        err = capsys.readouterr().err
        assert "回退目标" in err
        assert "可用 tick" in err

    def test_resume_no_run_record(self, cwd, modules_dir, capsys):
        # hello 模块存在但从未运行 → 无 run.sqlite
        assert _resume(cwd, "--module", "hello", "--mock") == 1
        err = capsys.readouterr().err
        assert "无运行记录" in err
        assert "先执行 specmodule run" in err

    def test_resume_compat_rejected(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "resume_hello", "--mock", "--max-ticks", "1") == 0
        capsys.readouterr()
        db = cwd / ".specmodule" / "runs" / "resume_hello" / "run.sqlite"

        def snap_ticks():
            # 拒绝前后对比用：读快照 tick 列表（内容度量，平台无关——
            # Windows 上 SQLite WAL 自动 checkpoint 会刷 mtime，不能断言文件时间戳）
            from tickflow.persistence import SqliteBackend
            b = SqliteBackend(db)
            try:
                return b.list_snapshots("resume_hello")
            finally:
                b.close()

        before = snap_ticks()
        tl = cwd / "bad_tl.json"
        tl.write_text(
            json.dumps({
                "Tasks": {
                    "A": {"type": "script", "script": "A", "inputs": {"value": "Ghost"}}
                },
                "Flow": "[A]",
            }),
            encoding="utf-8",
        )
        assert _resume(
            cwd, "--module", "resume_hello", "--mock", "--tasklist", str(tl)
        ) == 1
        err = capsys.readouterr().err
        assert "不在新图中" in err  # check_resume_compat 硬错误 1
        assert snap_ticks() == before  # 兼容性拒绝未产生新快照（原 run 未被触碰）

    def test_resume_invalid_target_hints_checkpoints(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "resume_hello", "--mock", "--max-ticks", "1") == 0
        capsys.readouterr()
        assert _resume(cwd, "99", "--module", "resume_hello", "--mock") == 1
        err = capsys.readouterr().err
        assert "checkpoints" in err  # 无效目标时引导查看可用回退点


class TestCheckpoints:
    def test_list_after_run(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert _checkpoints(cwd, "--run-id", "hello") == 0
        out = capsys.readouterr().out
        assert "可用回退点" in out
        assert "tick 1" in out        # 快照编号 N 在 tick N-1 结束后落盘
        assert "Greet" in out         # fired 节点轨迹

    def test_json(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert _checkpoints(cwd, "--run-id", "hello", "--json") == 0
        data = json.loads(capsys.readouterr().out)
        assert data["module_id"] == "hello"
        assert data["checkpoints"][0]["kind"] == "tick"
        assert data["checkpoints"][0]["target"] == "1"
        assert data["checkpoints"][0]["fired"] == ["Greet"]

    def test_no_run(self, cwd, capsys):
        assert _checkpoints(cwd, "--run-id", "ghost") == 1
        assert "无运行记录" in capsys.readouterr().err


class TestSnapshot:
    def test_summary(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert _snapshot(cwd, "--run-id", "hello") == 0
        out = capsys.readouterr().out
        assert "tick: " in out
        assert "各节点最新输出" in out
        assert "hello world" in out

    def test_json_full(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert _snapshot(cwd, "--run-id", "hello", "--json") == 0
        snap = json.loads(capsys.readouterr().out)
        assert snap["tick"] >= 1             # 快照编号 N 在 tick N-1 结束后落盘
        assert "marking" in snap and "run_state" in snap  # 完整 runner 快照

    def test_export_out(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        out_path = cwd / "snap.json"
        assert _snapshot(cwd, "--run-id", "hello", "--out", str(out_path)) == 0
        out = capsys.readouterr().out
        assert "已导出" in out
        snap = json.loads(out_path.read_text(encoding="utf-8"))
        assert snap["tick"] >= 1 and "marking" in snap  # 文件即完整快照 JSON

    def test_bad_tick(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert _snapshot(cwd, "99", "--run-id", "hello") == 1
        assert "不存在" in capsys.readouterr().err

    def test_no_run(self, cwd, capsys):
        assert _snapshot(cwd, "--run-id", "ghost") == 1
        assert "无运行记录" in capsys.readouterr().err


class TestRollback:
    def test_rollback_explicit(self, cwd, modules_dir, capsys):
        # 截断后显式 rollback 到 tick 1：A 已执行保留，B 补跑完成
        assert _run(cwd, "--module", "resume_hello", "--mock", "--max-ticks", "1") == 0
        capsys.readouterr()
        assert _rollback(cwd, "1", "--module", "resume_hello", "--mock") == 0
        out = capsys.readouterr().out
        assert "续跑完成" in out
        assert "hello from A" in out  # B 消费 A 的输出

    def test_rollback_missing_target(self, cwd, modules_dir, capsys):
        # 目标必填：argparse 拒绝（rollback 与 resume 的语义差异）
        assert _run(cwd, "--module", "resume_hello", "--mock", "--max-ticks", "1") == 0
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            _rollback(cwd, "--module", "resume_hello", "--mock")
        assert exc.value.code == 2  # argparse 必填参数错误
        assert "rollback" in capsys.readouterr().err


class TestCheckpoint:
    def _entries(self, cwd, capsys):
        main(["checkpoints", "--run-id", "hello", "--json"])
        out = capsys.readouterr().out
        return json.loads(out)["checkpoints"]

    def test_create_and_list(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert _checkpoint(cwd, "baseline", "--run-id", "hello") == 0
        out = capsys.readouterr().out
        assert "已创建检查点 manual:baseline" in out  # 自动补 manual: 前缀
        entries = self._entries(cwd, capsys)
        manual = [e for e in entries if e["kind"] == "manual"]
        assert len(manual) == 1
        assert manual[0]["target"] == "manual:baseline"
        assert manual[0]["tick"] >= 1

    def test_specific_tick(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert _checkpoint(cwd, "early", "1", "--run-id", "hello") == 0
        capsys.readouterr()  # 消费 checkpoint 输出，避免污染 json 读取
        entries = self._entries(cwd, capsys)
        manual = [e for e in entries if e["kind"] == "manual"][0]
        assert manual == {
            "target": "manual:early", "tick": 1,
            "kind": "manual", "fired": [], "label": "manual:early",
        }

    def test_rollback_manual_end_to_end(self, cwd, modules_dir, capsys):
        # run 截断 → 命名最新 tick → rollback manual:<label> 续跑完成
        assert _run(cwd, "--module", "resume_hello", "--mock", "--max-ticks", "1") == 0
        capsys.readouterr()
        assert _checkpoint(cwd, "after-a", "--run-id", "resume_hello") == 0
        capsys.readouterr()
        assert main([
            "rollback", "manual:after-a", "--module", "resume_hello", "--mock",
            "--modules-dir", str(cwd / "modules"),
        ]) == 0
        out = capsys.readouterr().out
        assert "续跑完成" in out
        assert "hello from A" in out  # B 消费 A 的输出

    def test_no_run(self, cwd, capsys):
        assert _checkpoint(cwd, "x", "--run-id", "ghost") == 1
        assert "无运行记录" in capsys.readouterr().err

    def test_bad_tick(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert _checkpoint(cwd, "x", "99", "--run-id", "hello") == 1
        assert "不存在" in capsys.readouterr().err


class TestVisualize:
    def test_from_run_archive(self, cwd, modules_dir, capsys):
        # 存档模式：渲染最近一次 run 的 tasklist（A start + A-->B 流水线）
        assert _run(cwd, "--module", "resume_hello", "--mock") == 0
        capsys.readouterr()
        assert _visualize(cwd, "--module", "resume_hello") == 0
        out = capsys.readouterr().out
        assert "graph TD" in out
        assert 'A(["A"])' in out      # start 节点 stadium 形状
        assert "A --> B" in out

    def test_from_tasklist_file(self, cwd, modules_dir, capsys):
        # 不依赖运行记录：--tasklist 直接渲染
        tl = cwd / "tl.json"
        tl.write_text(
            json.dumps({
                "Tasks": {"A": {"type": "script", "script": "A"}},
                "Flow": "[A]",
            }),
            encoding="utf-8",
        )
        assert _visualize(cwd, "--module", "resume_hello", "--tasklist", str(tl)) == 0
        out = capsys.readouterr().out
        assert "graph TD" in out
        assert "A" in out and "B" not in out

    def test_out_file(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "resume_hello", "--mock") == 0
        capsys.readouterr()
        out_path = cwd / "graph.md"
        assert _visualize(
            cwd, "--module", "resume_hello", "--out", str(out_path)
        ) == 0
        assert "已导出" in capsys.readouterr().out
        assert "graph TD" in out_path.read_text(encoding="utf-8")

    def test_module_not_found(self, cwd, modules_dir, capsys):
        assert _visualize(cwd, "--module", "ghost") == 1
        assert "未找到" in capsys.readouterr().err

    def test_run_id_defaults_to_module_dir_not_latest(self, cwd, modules_dir, capsys):
        # 坑1：缺省 run_id = 模块同名运行目录——全局更新的干扰目录被忽略
        from module_harness.infra.checkpoint import ModuleInputStore

        assert _run(cwd, "--module", "resume_hello", "--mock") == 0
        capsys.readouterr()
        # 造 mtime 更新的干扰运行目录（存档引用未注册 harness；旧逻辑
        # _latest_run_id 会捡到它 → harness not found）
        store = ModuleInputStore("zzz_ghost")
        store.save_module_inputs(
            {"spec": {}},
            {
                "Tasks": {"Ghost": {"type": "harness", "harness": "ghost_harness"}},
                "Flow": "[Ghost]",
            },
        )
        store.close()
        assert _visualize(cwd, "--module", "resume_hello") == 0
        out = capsys.readouterr().out
        assert "graph TD" in out and "A --> B" in out
        assert "Ghost" not in out

    def test_tasklist_registry_mismatch_hint(self, cwd, modules_dir, capsys):
        # 坑2：tasklist 引用 registry 未注册元件 → 报错并列出可用模板
        tl = cwd / "tl_ghost.json"
        tl.write_text(
            json.dumps({
                "Tasks": {"Ghost": {"type": "harness", "harness": "ghost_harness"}},
                "Flow": "[Ghost]",
            }),
            encoding="utf-8",
        )
        assert _visualize(cwd, "--module", "resume_hello", "--tasklist", str(tl)) == 1
        err = capsys.readouterr().err
        assert "ghost_harness" in err
        assert "可用模板: resume_hello" in err

    def test_no_run_no_tasklist(self, cwd, modules_dir, capsys):
        # 无运行记录且无 --tasklist → 缺省最近运行失败
        assert _visualize(cwd, "--module", "resume_hello") == 1
        assert "无运行记录" in capsys.readouterr().err

class TestFeed:
    """stdlib 可视化开关：零依赖 http.server 运行 feed（roadmap 独立线）。"""

    def _serve(self, cwd):
        """起服务于临时端口，返回 (server, url)。"""
        from module_harness.orchestrate.feed import RunFeedServer

        server = RunFeedServer(("127.0.0.1", 0), base_dir=cwd)
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        return server, f"http://{host}:{port}"

    def _get(self, url, path):
        import urllib.request

        with urllib.request.urlopen(url + path, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_feed_json_after_run(self, cwd, modules_dir):
        assert _run(cwd, "--module", "resume_hello", "--mock") == 0
        server, url = self._serve(cwd)
        try:
            code, body = self._get(url, "/feed.json?run_id=resume_hello")
            assert code == 200
            data = json.loads(body)
            assert data["run_id"] == "resume_hello"
            assert data["status"]["phase"] == "done"
            assert data["timeline"]["entries"], "时间线应含 firing 条目"
            assert data["timeline"]["entries"][0]["node"] == "A"
            assert data["checkpoints"]["checkpoints"], "应列出 tick 快照检查点"
        finally:
            server.shutdown()

    def test_feed_json_latest_run(self, cwd, modules_dir):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        assert _run(cwd, "--module", "resume_hello", "--mock") == 0
        server, url = self._serve(cwd)
        try:
            code, body = self._get(url, "/feed.json")  # 缺省 = 最近运行
            assert code == 200
            data = json.loads(body)
            assert data["run_id"] == "resume_hello"
        finally:
            server.shutdown()

    def test_feed_json_no_run(self, cwd):
        server, url = self._serve(cwd)
        try:
            import urllib.error

            with pytest.raises(urllib.error.HTTPError) as ei:
                self._get(url, "/feed.json?run_id=ghost")
            assert ei.value.code == 404
            assert "无运行记录" in json.loads(ei.value.read().decode("utf-8"))["error"]
        finally:
            server.shutdown()

    def test_feed_html_page(self, cwd, modules_dir):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        server, url = self._serve(cwd)
        try:
            code, body = self._get(url, "/?run_id=hello")
            assert code == 200
            assert "SpecModule 运行 feed" in body
            assert "feed.json" in body  # 页面轮询端点
        finally:
            server.shutdown()

    def test_feed_404(self, cwd):
        server, url = self._serve(cwd)
        try:
            import urllib.error

            with pytest.raises(urllib.error.HTTPError) as ei:
                self._get(url, "/nope")
            assert ei.value.code == 404
        finally:
            server.shutdown()


class TestFeedCommand:
    """CLI feed 子命令接线：启动服务、打印 URL、Ctrl+C 退出。"""

    def test_feed_command_serves(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        import threading
        import urllib.request

        from module_harness.cli import main

        port_holder = {}

        def serve():
            # 用固定端口启动（0 端口拿不到实际端口——直接读 stdout 不可行，
            # 因此用线程内 main() + 轮询端口列表）
            import socket

            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            port_holder["port"] = port
            main(["feed", "--host", "127.0.0.1", "--port", str(port)])

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        import time

        for _ in range(50):
            if "port" in port_holder:
                break
            time.sleep(0.05)
        port = port_holder["port"]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/feed.json?run_id=hello", timeout=10
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data["run_id"] == "hello"
        # 停止服务线程（serve_forever 需 shutdown——通过 server 实例不可达，
        # 改用直接 socket 连接触发 KeyboardInterrupt 不现实；daemon 线程随
        # 进程退出，测试结束时自动清理）


class TestRunsCommand:
    """runs 子命令：list_runs 共享层列表展示（人类可读 + --json）。"""

    def test_lists_runs_after_run(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()   # 丢弃 run 输出，只断言 runs 命令自身输出
        assert main(["runs"]) == 0
        out = capsys.readouterr().out
        assert "hello" in out       # run_id 列
        assert "done" in out        # phase 列

    def test_json_payload(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()   # 丢弃 run 输出
        assert main(["runs", "--json"]) == 0
        runs = json.loads(capsys.readouterr().out)
        assert runs[0]["run_id"] == "hello"
        # status.json module 字段（build_module 自动带 entry 名）
        assert runs[0]["module"] == "hello"
        assert runs[0]["phase"] == "done"
        assert runs[0]["has_sqlite"] is True
        assert runs[0]["error"] is None

    def test_empty_history_ok(self, cwd, capsys):
        assert main(["runs"]) == 0
        assert "无运行记录" in capsys.readouterr().out


class TestDeleteRunCommand:
    """delete-run 子命令：delete_run 共享层（删除打印移除目录，不存在非零）。"""

    def test_delete_after_run(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()   # 丢弃 run 输出
        run_dir = cwd / ".specmodule" / "runs" / "hello"
        assert run_dir.exists()
        gc_collect()   # 释放进程内 CLI run 残留的 backend 引用环（Windows 文件锁）
        assert main(["delete-run", "hello"]) == 0
        out = capsys.readouterr().out
        assert "已删除运行" in out and "hello" in out
        assert not run_dir.exists()

    def test_delete_twice_second_fails(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        gc_collect()   # 同上：删除前释放进程内 backend 引用环
        assert main(["delete-run", "hello"]) == 0
        assert main(["delete-run", "hello"]) == 1
        assert "无运行记录" in capsys.readouterr().err

    def test_delete_ghost_fails(self, cwd, capsys):
        assert main(["delete-run", "ghost"]) == 1
        assert "无运行记录" in capsys.readouterr().err

    def test_delete_traversal_rejected_nonzero(self, cwd, modules_dir, capsys):
        # 路径穿越形态：delete_run 返回 False → 非零退出（不删 runs 根外内容）
        assert main(["delete-run", "../evil"]) == 1
