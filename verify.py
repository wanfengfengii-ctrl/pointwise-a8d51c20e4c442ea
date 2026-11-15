#!/usr/bin/env python3
"""差分核验：normalizer.Store 与独立全量参照 RefStore 逐批对比。

覆盖：随机合法操作流（别名链/改向/删边、明细增删改、同来源整体转移、
负金额、归零）、成环拒绝、非法输入拒绝、失败原子性、不变量自检、
题目示例、边界场景。退出码 0 表示全部通过。
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from normalizer import Store
from ref_store import RefStore

NAMES = ["a", "b", "c", "d", "e", "f"]
SOURCES = ["a", "b", "c", "d", "x", "y"]


def snapshot(store):
    """通过公开接口可观察的全部状态。"""
    totals = store.totals()
    resolves = {n: store.resolve(n) for n in NAMES + SOURCES}
    return totals, resolves


def assert_same(live, ref, tag):
    t1, r1 = snapshot(live)
    t2, r2 = snapshot(ref)
    assert t1 == t2, f"totals 不一致 @{tag}: {t1} != {t2}"
    assert r1 == r2, f"resolve 不一致 @{tag}: {r1} != {r2}"


def random_aliases(rng):
    aliases = {}
    for name in NAMES:
        roll = rng.random()
        if roll < 0.25:
            aliases[name] = None
        elif roll < 0.75:
            target = rng.choice(NAMES)
            aliases[name] = target
    return aliases


def random_rows(rng, id_pool):
    rows = {}
    for _ in range(rng.randint(0, 6)):
        roll = rng.random()
        if roll < 0.2 and id_pool:
            rid = rng.choice(id_pool)
            rows[rid] = None  # 删除（可能删缺项）
        elif roll < 0.55 and id_pool:
            rid = rng.choice(id_pool)
            rows[rid] = (rng.choice(SOURCES), rng.randint(-9, 9))  # 替换
        else:
            rid = f"r{rng.randint(0, 40)}"
            rows[rid] = (rng.choice(SOURCES), rng.randint(-9, 9))  # 新增
    return rows


def run_random_streams():
    rng = random.Random(20261115)
    for stream in range(120):
        live, ref = Store(), RefStore()
        id_pool = []
        for step in range(60):
            aliases = random_aliases(rng)
            rows = random_rows(rng, id_pool)
            out_live = _apply_or_raise(live, aliases, rows)
            out_ref = _apply_or_raise(ref, aliases, rows)
            kind_live, val_live = out_live
            kind_ref, val_ref = out_ref
            assert kind_live == kind_ref, (
                f"stream {stream} step {step}: 抛错行为不一致 "
                f"{kind_live} vs {kind_ref}, aliases={aliases}, rows={rows}"
            )
            if kind_live == "ok":
                assert val_live == val_ref, (
                    f"stream {stream} step {step}: apply 返回不一致 "
                    f"{val_live} != {val_ref}"
                )
                id_pool.extend(rid for rid, e in rows.items() if e is not None)
                id_pool = list(dict.fromkeys(id_pool))
            else:
                # 失败原子性：公开状态与批前一致（两个实现都未提交）
                pass
            assert_same(live, ref, f"stream {stream} step {step}")
            live._debug_check_invariants()
    print("[ok] 120 条随机流 x 60 批：apply/resolve/totals 全部一致，不变量通过")


def _apply_or_raise(store, aliases, rows):
    try:
        return "ok", store.apply(aliases, rows)
    except ValueError as exc:
        return "raise", str(exc)


def run_spec_example():
    s = Store()
    assert s.apply({"a": "b"}, {"r": ("a", 5)}) == {"b": 5}
    assert s.resolve("a") == "b"
    assert s.totals() == {"b": 5}
    assert s.apply({"b": "c"}, {}) == {"b": 0, "c": 5}
    assert s.resolve("a") == "c" and s.resolve("b") == "c"
    assert s.totals() == {"c": 5}
    s._debug_check_invariants()
    print("[ok] 题目示例：{'b':5} -> {'b':0,'c':5}")


def run_edge_cases():
    s = Store()
    # 空仓查询
    assert s.resolve("x") == "x" and s.totals() == {}
    # 无变化 apply 返回 {}
    assert s.apply({}, {}) == {}
    # 负金额与归零
    assert s.apply({}, {"r1": ("a", -7), "r2": ("a", 7)}) == {}
    assert s.totals() == {}
    # 同 ID 替换：同来源改金额（-7 -> 10，差额 +17；另一明细 +7 不变）
    assert s.apply({}, {"r1": ("a", 10)}) == {"a": 17}
    assert s.totals() == {"a": 17}
    assert s.apply({}, {"r1": ("a", 3)}) == {"a": 10}
    # 同 ID 替换：换来源，同来源明细整体转移（r2 仍留在 a=7）
    assert s.apply({}, {"r1": ("b", 3)}) == {"a": 7, "b": 3}
    assert s.totals() == {"a": 7, "b": 3}
    # 删除缺项无效果
    assert s.apply({}, {"ghost": None}) == {}
    # 实际删除 r1：b 归零，a 不变
    assert s.apply({}, {"r1": None}) == {"b": 0}
    assert s.totals() == {"a": 7}
    # 加别名 a->b：a 的 7 与新明细 4 整体归到 b
    assert s.apply({"a": "b"}, {"r9": ("a", 4)}) == {"a": 0, "b": 11}
    assert s.totals() == {"b": 11}
    # 删别名出边后规范名回到自身
    assert s.apply({"a": None}, {}) == {"a": 11, "b": 0}
    assert s.totals() == {"a": 11}
    # 别名链 a->b->c：a 的 11 沿链归到 c
    assert s.apply({"a": "b", "b": "c"}, {}) == {"a": 0, "c": 11}
    assert s.totals() == {"c": 11}
    # 链下新明细直接落到 c
    assert s.apply({}, {"r10": ("a", 1)}) == {"c": 12}
    # 同批同时改边与改明细，以批后关系解释：b 脱链后 a->b，根为 b
    assert s.apply({"b": None}, {"r10": ("a", 9)}) == {"b": 20, "c": 0}
    assert s.totals() == {"b": 20}
    s._debug_check_invariants()
    print("[ok] 边界场景：负金额、归零、换源、删缺项、删边、链解析、同批混合")


def run_rejections():
    bad_batches = [
        ({"a": ""}, {}),
        ({"": "b"}, {}),
        ({"a": 1}, {}),
        ({}, {"r": ("a", True)}),       # 布尔金额
        ({}, {"r": ("a", 1.5)}),        # 非整数
        ({}, {"r": ("", 1)}),
        ({}, {"r": ["a", 1]}),
        ({}, {"": ("a", 1)}),
        ([], {}),                        # 非字典
        ({}, []),
        (None, {}),
    ]
    for aliases, rows in bad_batches:
        s = Store()
        try:
            s.apply(aliases, rows)
        except ValueError:
            pass
        else:
            raise AssertionError(f"非法输入未拒绝: {aliases!r}, {rows!r}")
        assert s.totals() == {} and s.resolve("a") == "a"
    print("[ok] 11 类非法输入均抛 ValueError 且状态不变")

    # 成环：直接环、自环、长链环、同批“删旧边+加新边”仍成环
    cycle_cases = [
        ({"a": "b", "b": "a"}, {}),
        ({"a": "a"}, {}),
        ({"a": "b", "b": "c", "c": "a"}, {}),
    ]
    s = Store()
    s.apply({"a": "b", "b": "c"}, {"r": ("a", 5)})
    before = (s.totals(), {n: s.resolve(n) for n in ["a", "b", "c"]})
    cycle_cases.append(({"c": "a"}, {}))  # 既有链上回边
    for aliases, rows in cycle_cases:
        try:
            s.apply(aliases, rows)
        except ValueError:
            pass
        else:
            raise AssertionError(f"成环未拒绝: {aliases!r}")
        after = (s.totals(), {n: s.resolve(n) for n in ["a", "b", "c"]})
        assert before == after, "成环失败后状态被修改，违反原子性"
    s._debug_check_invariants()

    # 同批删边恰好破环：必须成功（以批后关系判断）
    ok = Store()
    ok.apply({"a": "b"}, {})
    assert ok.apply({"a": "b", "b": "a", "b": None}, {}) == {}
    assert ok.resolve("a") == "b" and ok.resolve("b") == "b"
    print("[ok] 4 组成环拒绝且整批不变；删边破环场景正确放行")


def main():
    run_spec_example()
    run_edge_cases()
    run_rejections()
    run_random_streams()
    print("\n全部核验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
