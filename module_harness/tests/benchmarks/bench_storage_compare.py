"""快速模式（NullBackend）vs 持久化模式（SqliteBackend）运行速度对比。

三层对比：
  1. 引擎循环压测（纯 CPU，无 LLM）：持久 / 快速 / 快速+无审计 —— 存储开销显形
  2. 大 output 内存对比（tracemalloc）：快速模式 _records 全量驻留 vs 持久模式窗口
  3. Module 级真实 LLM 端到端：差距应被 LLM 秒级延迟淹没（设计文档准则 4）

用法：python bench_storage_compare.py
"""

from __future__ import annotations

import asyncio
import statistics
import time
import tracemalloc

from tickflow import NullBackend, Registry, Runner, parse
from tickflow.views import Missing

LOOP_LIMIT = 2_000           # 循环迭代数（A/B 各触发 2k 次 ≈ 4 千 tick）
BIG_OUTPUT_N = 500           # 大 output 迭代数
REPS = 2                     # 每模式重复次数（取最小）


def make_loop_graph(limit: int):
    r = Registry()
    r.body("seed_zero", lambda v: 0)

    @r.body("passthru")
    def _p(v):
        for _n, val in v.items():
            if val is not Missing:
                return val
        return None

    @r.body("incr")
    def _incr(v):
        return v.A.value + 1

    r.guard("cont_ltN", lambda v: v.B.value < limit)
    g = parse(
        "[seed]-->A\nseed.body: seed_zero\nA.body: passthru\nA.join: OR\n"
        "A-->B\nB.body: incr\nB--|cont_ltN|-->A",
        registry=r,
    )
    return g, r


def bench_engine(mode: str, graph, reg, reps: int = REPS) -> float:
    """单次引擎全量运行耗时（取 reps 次最小值）。"""
    times: list[float] = []
    for i in range(reps):
        if mode == "persist":
            rn = Runner(graph, reg)                      # 默认临时 SqliteBackend
        elif mode == "fast":
            rn = Runner(graph, reg, backend=NullBackend())
        else:                                            # fast-min
            rn = Runner(graph, reg, backend=NullBackend(), keep_records=False)
        t0 = time.perf_counter()
        rn.run_until_idle(max_ticks=100_000)
        times.append(time.perf_counter() - t0)
        assert rn.is_idle(), f"{mode}: 未跑到 idle"
        print(f"    [{mode} rep {i + 1}/{reps}] {fmt(times[-1])}", flush=True)
    return min(times)


def bench_engine_memory(mode: str) -> tuple[float, int]:
    """大 output 循环的内存峰值（tracemalloc）与耗时。

    body 每次调用生成**新的** 100KB 字符串——真实衡量每节点 output 的驻留，
    而非共享同一模块级常量。
    """
    r = Registry()
    r.body("seed_zero", lambda v: 0)

    @r.body("big")
    def _big(v):
        return {"payload": "x" * 100_000}

    r.guard("always", lambda v: True)
    g = parse(
        "[seed]-->A\nseed.body: seed_zero\nA.body: big\nA.join: OR\n"
        "A--|always|-->A",
        registry=r,
    )
    if mode == "persist":
        rn = Runner(g, r)
    else:
        rn = Runner(g, r, backend=NullBackend())
    tracemalloc.start()
    t0 = time.perf_counter()
    # 手动 tick 循环（不累积 firing 返回值——run_until_idle 的 seen 列表
    # 会保留全部 NodeState，干扰存储层内存测量）
    while rn.tick_count < BIG_OUTPUT_N:
        rn.tick()
    dt = time.perf_counter() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return dt, peak


async def bench_module_llm() -> tuple[float, float]:
    """Module 级真实 LLM 端到端：persist vs fast 各 1 次 LLM 调用。"""
    from llm import LLMConfig, create_llm_client
    from module_harness.config import HarnessConfig, OutputFormat
    from module_harness.module import Module
    from module_harness.registry import HarnessRegistry
    from module_harness.spec import TaskDefinition, Tasklist

    client = create_llm_client(LLMConfig.from_env())
    tasklist = Tasklist(tasks={
        "A": TaskDefinition(
            type="harness", harness="translate",
            inputs={"text": "{spec.text}"},
            outputformat={"type": "json_object"})},
        flow="[A]")

    def make_mod(persist: bool):
        reg = HarnessRegistry(llm_client=client)
        reg.harness("translate", HarnessConfig(
            prompt_core="将'{text}'翻译为中文，只输出 JSON: {\"translation\": \"...\"}",
            output_format=OutputFormat(type="json_object"), temperature=0.1))
        return Module(
            spec={"text": "Hello world"},
            tasklist=tasklist, llm_client=client, registry=reg,
            review_harness=None, module_id=f"bench_{persist}", persist=persist)

    times = {"persist": [], "fast": []}
    for _ in range(2):                                  # 各 2 次取最小
        for persist in (True, False):
            t0 = time.perf_counter()
            firings = await make_mod(persist).run(max_ticks=10)
            times["persist" if persist else "fast"].append(time.perf_counter() - t0)
            assert firings and firings[-1].status == "ok"
    return min(times["persist"]), min(times["fast"])


def fmt(seconds: float) -> str:
    return f"{seconds:8.3f}s"


def main() -> None:
    import sys
    memory_only = "--memory-only" in sys.argv
    if not memory_only:
        total_ticks = 2 * LOOP_LIMIT + 1
        print("=" * 72)
        print(f"1) 引擎循环压测（纯 CPU，无 LLM）— 循环 {LOOP_LIMIT} 次 ≈ {total_ticks} tick")
        print("=" * 72)
        graph, reg = make_loop_graph(LOOP_LIMIT)
        t_persist = bench_engine("persist", graph, reg)
        t_fast = bench_engine("fast", graph, reg)
        t_fastmin = bench_engine("fast-min", graph, reg)
        print(f"  persist (默认 SqliteBackend)  : {fmt(t_persist)}")
        print(f"  fast    (NullBackend)         : {fmt(t_fast)}")
        print(f"  fast-min(快+无审计)           : {fmt(t_fastmin)}")
        print(f"  持久化开销（persist - fast-min）= {t_persist - t_fastmin:7.3f}s "
              f"({(t_persist / t_fastmin - 1) * 100:6.1f}% 慢于基线)")
        print(f"  ≈ 每 tick 持久化成本 {(t_persist - t_fastmin) / total_ticks * 1e3:7.1f} µs")
        print(f"  fast 模式残余开销（fast - fast-min）= {t_fast - t_fastmin:6.3f}s "
              f"({(t_fast / t_fastmin - 1) * 100:5.1f}% 慢于基线)")
        print()

    print("=" * 72)
    print(f"2) 大 output 内存对比（{BIG_OUTPUT_N} 次 × 每次新建 100KB，tracemalloc 峰值）")
    print("=" * 72)
    dt_p, peak_p = bench_engine_memory("persist")
    dt_f, peak_f = bench_engine_memory("fast")
    print(f"  persist: 耗时 {fmt(dt_p)}  内存峰值 {peak_p / 1e6:8.1f} MB")
    print(f"  fast:    耗时 {fmt(dt_f)}  内存峰值 {peak_f / 1e6:8.1f} MB "
          f"({peak_f / peak_p:5.1f}x)")
    print(f"  （fast 的 keep_records 全量驻留 _records；persist 只留窗口 2 条，"
          f"其余落盘）")
    print()

    if memory_only:
        return
    print("=" * 72)
    print("3) Module 级真实 LLM 端到端（各 2 次取最小，单节点翻译）")
    print("=" * 72)
    t_llm_p, t_llm_f = asyncio.run(bench_module_llm())
    print(f"  persist: {fmt(t_llm_p)}")
    print(f"  fast:    {fmt(t_llm_f)}")
    print(f"  差异 {abs(t_llm_p - t_llm_f):5.3f}s "
          f"—— LLM 秒级延迟下可忽略（设计准则 4）")


if __name__ == "__main__":
    main()
