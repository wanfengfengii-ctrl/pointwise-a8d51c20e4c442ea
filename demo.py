#!/usr/bin/env python3
"""重归属演示 + 局部修订性能实测。

两部分：
  1. 功能演示：题目示例与同来源整体转移。
  2. 性能实测：十万条同来源明细 + 五万条无关别名（一万条互不相连的
     短链）构成的大林下，一次局部别名修订（只动 1 条边）与一次同来源
     单条明细替换，测量访问量（节点访问/明细触达）、耗时与峰值内存；
     并用独立全量参照（每批全量扫描重建）给出对照基线。

计时方法：大仓只建一次并快照；每轮在计时区外恢复快照，perf_counter
只框住单次 apply，因此测得的是“单批局部修订”的纯净成本。
实测数据同时写入 benchmark_result.json，便于留档。
"""
import json
import os
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from normalizer import Store
from ref_store import RefStore

N_RECORDS = 100_000   # 同来源明细条数
N_CHAINS = 10_000     # 无关别名短链数量
CHAIN_LEN = 5         # 每条短链的边数（总别名数 = N_CHAINS * CHAIN_LEN）
N_NOISE = N_CHAINS * CHAIN_LEN
REPEAT = 20
RESULT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "benchmark_result.json"
)


def functional_demo():
    print("=" * 68)
    print("功能演示")
    print("=" * 68)
    s = Store()
    r1 = s.apply({"a": "b"}, {"r": ("a", 5)})
    print(f"apply({{'a':'b'}}, {{'r':('a',5)}}) -> {r1}")
    r2 = s.apply({"b": "c"}, {})
    print(f"apply({{'b':'c'}}, {{}})             -> {r2}")
    print(f"resolve('a')={s.resolve('a')}  totals()={s.totals()}")

    # 同来源明细整体转移：两条 a 来源明细随 a->d 一次性整体迁走
    s2 = Store()
    s2.apply({}, {"k1": ("a", 100), "k2": ("a", 200), "k3": ("z", 7)})
    moved = s2.apply({"a": "d"}, {})
    print(f"\n同来源整体转移（k1+k2 均来自 a）：apply({{'a':'d'}}, {{}}) -> {moved}")
    print(f"totals()={s2.totals()}（a 归零显式返回 0，d=300，z 不受影响）")
    print()


def noise_aliases():
    """一万条互不相连的短链 g{c}_{i} -> g{c}_{i+1}，与 src 子树无关。"""
    aliases = {}
    for c in range(N_CHAINS):
        for i in range(CHAIN_LEN):
            aliases[f"g{c}_{i}"] = f"g{c}_{i + 1}"
    return aliases


def big_rows():
    return {f"rec{i:06d}": ("src", 1) for i in range(N_RECORDS)}


def build_big_store():
    s = Store()
    s.apply(noise_aliases(), big_rows())
    return s


def measure_local(store, snap, operation, repeat=REPEAT):
    """计时区外恢复快照，计时只框住单次 apply。"""
    best = float("inf")
    last_out = None
    for _ in range(repeat):
        store._restore(snap)          # 不计入耗时
        t0 = time.perf_counter()
        last_out = operation(store)
        dt = time.perf_counter() - t0
        best = min(best, dt)
    return best, last_out, store.stats.copy()


def benchmark():
    print("=" * 68)
    print(f"性能实测：{N_RECORDS:,} 条同来源明细 + {N_NOISE:,} 条无关别名"
          f"（{N_CHAINS:,} 条深度 {CHAIN_LEN} 的独立短链）")
    print("=" * 68)

    t_build0 = time.perf_counter()
    base = build_big_store()
    build_seconds = time.perf_counter() - t_build0
    base._debug_check_invariants()
    # 单独再建一仓，仅用于干净的 Python 对象峰值口径（不污染上面的构建耗时）
    tracemalloc.start()
    mem_store = build_big_store()
    cur_py, peak_py = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del mem_store
    snap = base._snapshot()
    print(f"建仓耗时 {build_seconds * 1000:.1f} ms；"
          f"明细 {len(base._records):,} 条，别名边 {len(base._fwd):,} 条，"
          f"汇总根 {len(base.totals())} 个")
    print(f"单仓 Python 对象峰值（tracemalloc）= {peak_py / 1024 / 1024:.1f} MB，"
          f"当前占用 {cur_py / 1024 / 1024:.1f} MB")

    result = {
        "scale": {
            "records": N_RECORDS,
            "noise_aliases": N_NOISE,
            "noise_chains": N_CHAINS,
            "chain_depth": CHAIN_LEN,
        },
        "build_seconds": round(build_seconds, 6),
    }

    # ---- 1. 局部别名修订：只改 1 条边（src 挂到新规范名 root2）----
    op_alias = lambda st: st.apply({"src": "root2"}, {})  # noqa: E731
    dt_alias, out_alias, stats_alias = measure_local(base, snap, op_alias)
    base._restore(snap)
    base.apply({"src": "root2"}, {})
    base._debug_check_invariants()
    base._restore(snap)
    alias_metric = {
        "changed_edges": 1,
        "node_visits": stats_alias["node_visits"],
        "record_touches": stats_alias["record_touches"],
        f"min_seconds_over_{REPEAT}": round(dt_alias, 9),
        "returned": out_alias,
    }
    result["local_alias_revision"] = alias_metric
    print("\n[局部别名修订] apply({'src':'root2'}, {})  —— 只动 1 条边")
    print(f"  节点访问量    : {alias_metric['node_visits']} "
          f"（与 {N_NOISE:,} 无关别名、{N_RECORDS:,} 明细的规模无关）")
    print(f"  明细触达量    : {alias_metric['record_touches']}（不触碰任何明细）")
    print(f"  最短耗时      : {dt_alias * 1e6:.1f} µs（{REPEAT} 次取最小）")
    print(f"  返回          : {out_alias}")

    # ---- 2. 同来源单条明细替换（金额 1 -> 9）----
    op_row = lambda st: st.apply({}, {"rec000042": ("src", 9)})  # noqa: E731
    dt_row, out_row, stats_row = measure_local(base, snap, op_row)
    row_metric = {
        "changed_rows": 1,
        "node_visits": stats_row["node_visits"],
        "record_touches": stats_row["record_touches"],
        f"min_seconds_over_{REPEAT}": round(dt_row, 9),
        "returned": out_row,
    }
    result["local_row_replacement"] = row_metric
    print("\n[同来源单条明细替换] apply({}, {'rec000042': ('src', 9)})")
    print(f"  节点访问量    : {stats_row['node_visits']}（仅一次规范名解析）")
    print(f"  明细触达量    : {stats_row['record_touches']}（仅该 ID，无关明细零扫描）")
    print(f"  最短耗时      : {dt_row * 1e6:.1f} µs（{REPEAT} 次取最小）")
    print(f"  返回          : {out_row}")

    # ---- 3. 全量重建基线：独立参照实现，同一次修订全量扫描 ----
    ref = RefStore()
    ref.apply(noise_aliases(), big_rows())
    t0 = time.perf_counter()
    ref_out = ref.apply({"src": "root2"}, {})
    dt_ref = time.perf_counter() - t0
    result["full_rebuild_baseline_alias"] = {
        "seconds": round(dt_ref, 6),
        "returned": ref_out,
        "speedup_x": round(dt_ref / dt_alias, 1),
    }
    print("\n[全量重建基线] 参照实现同一次别名修订（全量扫描全部明细 + 全图环检）")
    print(f"  耗时          : {dt_ref * 1000:.3f} ms")
    print(f"  提速比        : {dt_ref / dt_alias:,.0f}x")
    assert ref_out == out_alias, "局部修订与全量重建结果必须一致"

    # 明细替换的全量基线
    ref2 = RefStore()
    ref2.apply(noise_aliases(), big_rows())
    t0 = time.perf_counter()
    ref_row_out = ref2.apply({}, {"rec000042": ("src", 9)})
    dt_ref_row = time.perf_counter() - t0
    result["full_rebuild_baseline_row"] = {
        "seconds": round(dt_ref_row, 6),
        "returned": ref_row_out,
        "speedup_x": round(dt_ref_row / dt_row, 1),
    }
    print(f"[全量重建基线] 单条明细替换参照耗时 {dt_ref_row * 1000:.3f} ms，"
          f"提速 {dt_ref_row / dt_row:,.0f}x")
    assert ref_row_out == out_row

    # ---- 4. 峰值内存与常驻规模 ----
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    mem = {
        "single_store_tracemalloc_peak_mb": round(peak_py / 1024 / 1024, 2),
        "single_store_tracemalloc_current_mb": round(cur_py / 1024 / 1024, 2),
        "process_peak_rss_mb": round(rss_mb, 2),
        "records": len(base._records),
        "alias_edges": len(base._fwd),
        "reverse_edge_sets": len(base._rev),
        "source_subtotals": len(base._src_sum),
        "canonical_totals": len(base._totals),
    }
    result["memory"] = mem
    print(f"\n[峰值内存] 单仓 tracemalloc 峰值 = {mem['single_store_tracemalloc_peak_mb']} MB；"
          f"进程 ru_maxrss = {rss_mb:.1f} MB（含参照仓与快照）")
    print(f"  常驻结构：明细 {mem['records']:,} / 别名边 {mem['alias_edges']:,} "
          f"/ 来源小计 {mem['source_subtotals']} / 规范汇总 {mem['canonical_totals']}")

    # 访问量上界断言：局部修订成本不得随无关别名/明细数量增长
    assert stats_alias["node_visits"] <= 10
    assert stats_alias["record_touches"] == 0
    assert stats_row["record_touches"] == 1
    assert stats_row["node_visits"] <= 2

    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n实测结果已写入 {os.path.basename(RESULT_PATH)}")
    return result


def main():
    functional_demo()
    benchmark()
    print("\n演示与实测完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
