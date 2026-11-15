"""入仓加工：别名更正驱动的金额重归属（仅依赖 Python 标准库）。

公开接口
--------
Store()
    初始为空的加工仓。
Store.apply(aliases, rows) -> dict
    整批原子生效；返回批后金额相对批前发生变化的 *规范名 -> 最终金额*，
    归零者映射到 0。
Store.resolve(name) -> str
    返回规范名（无出边者即自身）。
Store.totals() -> dict
    返回所有非零规范名的汇总快照。

设计目标：局部修订（别名更正、同来源明细替换/删除）只访问受影响子树与
受影响明细，不扫描/复制无关别名或明细，也不重建全部汇总。
"""

__all__ = ["Store"]


class Store:
    def __init__(self):
        # 别名图：name -> 目标名（每个节点至多一条出边）
        self._fwd = {}
        # 反向依赖：target -> {source, ...}
        self._rev = {}
        # 明细：id -> (来源名, 金额)
        self._records = {}
        # 来源小计：来源名 -> 该来源名下全部明细金额之和（不保留零值键）
        self._src_sum = {}
        # 规范名汇总（不保留零值键；totals() 直接返回其副本）
        self._totals = {}
        # 调试/实测用访问计数（生产语义无副作用，可忽略）
        self.stats = {
            "node_visits": 0,     # 别名图上被访问的节点次数
            "record_touches": 0,  # 明细记录被读写的次数
        }

    # ------------------------------------------------------------------ #
    # 公开查询
    # ------------------------------------------------------------------ #
    def resolve(self, name):
        if not isinstance(name, str) or not name:
            raise ValueError("名称必须为非空字符串")
        return self._resolve(name)

    def totals(self):
        return dict(self._totals)  # 快照，调用方修改不影响仓内状态

    # ------------------------------------------------------------------ #
    # 状态快照/恢复（供基准测试隔离“建仓成本”与“单批修订成本”）
    # ------------------------------------------------------------------ #
    def _snapshot(self):
        return (
            dict(self._fwd),
            {k: set(v) for k, v in self._rev.items()},
            dict(self._records),
            dict(self._src_sum),
            dict(self._totals),
        )

    def _restore(self, snap):
        fwd, rev, records, src_sum, totals = snap
        self._fwd = dict(fwd)
        self._rev = {k: set(v) for k, v in rev.items()}
        self._records = dict(records)
        self._src_sum = dict(src_sum)
        self._totals = dict(totals)
        self.stats = {"node_visits": 0, "record_touches": 0}

    # ------------------------------------------------------------------ #
    # 批量生效
    # ------------------------------------------------------------------ #
    def apply(self, aliases, rows):
        self._validate_batch(aliases, rows)

        # 1. 过滤出真正改变出边的别名项；None 表示删除出边。
        changed_edges = []  # (src, new_target_or_None)
        for src, tgt in aliases.items():
            old = self._fwd.get(src)
            if (old is not None or tgt is not None) and old != tgt:
                changed_edges.append((src, tgt))

        # 2. 以批后关系做无环校验（任何非法输入在状态修改前即抛出）。
        if changed_edges:
            self._check_acyclic(changed_edges)

        # 批前值惰性快照：只登记本批实际触及的汇总键。
        pre = {}

        def remember(key):
            if key not in pre:
                pre[key] = self._totals.get(key, 0)

        # 3. 明细阶段（先账后图）。在批前图上把本批来源差额落到当时的
        #    规范名；随后别名重算会读到批后来源小计并整体重归属，两段
        #    叠加即等于“以批后关系解释的最终明细”。整批同时生效。
        # 3a. 只在本批触及的 ID 范围内聚合整源差额，不扫描无关明细；
        #     同 ID 在批内以最后一次写入为准（字典天然保留最后一项）。
        delta_src = {}  # 来源 -> 相对批前小计的整源差额
        touched = False
        for rid, entry in rows.items():
            old = self._records.get(rid)
            if entry is None:
                if old is None:
                    continue  # 删除缺项无效果
                old_src, old_amt = old
                delta_src[old_src] = delta_src.get(old_src, 0) - old_amt
            else:
                src, amt = entry
                delta_src[src] = delta_src.get(src, 0) + amt
                if old is not None:
                    old_src, old_amt = old
                    delta_src[old_src] = (
                        delta_src.get(old_src, 0) - old_amt
                    )
            touched = True

        if touched:
            # 3b. 来源小计按整源差额迁移（同来源明细整体转移）。
            for src, delta in delta_src.items():
                if delta:
                    self._bump_src(src, delta)
            # 3c. 在批前图上把整源差额归到当时的规范名。
            root_delta = {}
            for src, delta in delta_src.items():
                if delta == 0:
                    continue
                root = self._resolve(src)
                root_delta[root] = root_delta.get(root, 0) + delta
            for root, delta in root_delta.items():
                remember(root)
                self._set_total(
                    root, self._totals.get(root, 0) + delta
                )
            # 3d. 提交记录变更（同 ID 替换、None 删除）。
            for rid, entry in rows.items():
                if entry is None:
                    if self._records.pop(rid, None) is not None:
                        self.stats["record_touches"] += 1
                else:
                    self._records[rid] = entry
                    self.stats["record_touches"] += 1

        # 4. 别名阶段。先删旧边、再加新边：删边不会产生环，而终图已被
        #    第 2 步证明无环，因此分阶段执行的中间态始终是森林。
        touched_nodes = set()
        if changed_edges:
            for src, tgt in changed_edges:
                old = self._fwd.pop(src, None)
                if old is not None:
                    back = self._rev.get(old)
                    if back is not None:
                        back.discard(src)
                        if not back:
                            del self._rev[old]
                if tgt is not None:
                    self._fwd[src] = tgt
                    self._rev.setdefault(tgt, set()).add(src)
                touched_nodes.add(src)
                if old is not None:
                    touched_nodes.add(old)
                if tgt is not None:
                    touched_nodes.add(tgt)

            # 5. 边迁移后，从每个被触节点沿父链上溯到所在树的根。
            #    脱链子树的旧根（不再是被触节点的祖先）必须靠反向扩展
            #    找回：被触节点本身就可能是旧根。所有受影响树的根都要
            #    重算，未受影响的树完全不访问。
            affected_roots = set()
            for node in touched_nodes:
                affected_roots.add(self._resolve(node))

            # 6. 重算受影响根的整棵子树（仅遍历受影响组件内的节点）。
            #    先登记全部受影响节点的批前值：旧根可能变成非根，其归零
            #    也必须出现在返回值中；非根节点不承载汇总，清除残留旧键。
            #    树和为 0 且批前无值的根不建键，避免零值键污染后续增量。
            for node in touched_nodes | affected_roots:
                remember(node)
            for node in touched_nodes:
                if node in self._fwd:
                    # 现为非根：若它曾承载汇总，清除残留旧键
                    self._totals.pop(node, None)
            for root in affected_roots:
                tree_val = self._tree_sum(root)
                if tree_val != 0 or root in pre:
                    self._set_total(root, tree_val)

        # 6. 仅返回批前→批后金额改变的规范名；归零显式返回 0。
        result = {}
        for key, old_val in pre.items():
            new_val = self._totals.get(key, 0)
            if new_val != old_val:
                result[key] = new_val
        return result

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _resolve(self, name):
        cur = name
        while True:
            self.stats["node_visits"] += 1
            nxt = self._fwd.get(cur)
            if nxt is None:
                return cur
            cur = nxt

    def _tree_sum(self, root):
        """汇总 root 子树全部来源小计；调用方须保证图无环。

        反向森林中每个节点至多有一个父节点，seen 同时承担去重与计数口径：
        同一节点只计一次，避免边迁移残留导致的重复累加。
        """
        total = self._src_sum.get(root, 0)
        seen = {root}
        stack = list(self._rev.get(root, ()))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            self.stats["node_visits"] += 1
            total += self._src_sum.get(node, 0)
            kids = self._rev.get(node)
            if kids:
                stack.extend(kids)
        return total

    def _bump_src(self, src, delta):
        if delta == 0:
            return
        val = self._src_sum.get(src, 0) + delta
        if val:
            self._src_sum[src] = val
        else:
            self._src_sum.pop(src, None)

    def _set_total(self, root, value):
        """写规范名汇总；归零即删键，保证内部无零值残留。"""
        if value:
            self._totals[root] = value
        else:
            self._totals.pop(root, None)

    def _check_acyclic(self, changed_edges):
        """以批后关系逐条校验被改出边：新目标不得（沿批后图）回到源。

        未被本批修改的出边沿用仓内边；被改项使用批后的新目标。
        本仓既有图恒为无环森林，故只需检查被改边。
        """
        overlay = dict(changed_edges)
        for src, tgt in changed_edges:
            if tgt is None:
                continue  # 删边不可能成环
            cur = tgt
            seen = set()
            while cur is not None:
                self.stats["node_visits"] += 1
                if cur == src or cur in seen:
                    raise ValueError("别名成环，整批作废")
                seen.add(cur)
                cur = overlay.get(cur, self._fwd.get(cur))

    @staticmethod
    def _validate_batch(aliases, rows):
        if not isinstance(aliases, dict) or not isinstance(rows, dict):
            raise ValueError("aliases 与 rows 必须为字典")
        for name, tgt in aliases.items():
            if not isinstance(name, str) or not name:
                raise ValueError("别名名称必须为非空字符串")
            if tgt is not None and (not isinstance(tgt, str) or not tgt):
                raise ValueError("别名目标必须为非空字符串或 None")
        for rid, entry in rows.items():
            if not isinstance(rid, str) or not rid:
                raise ValueError("明细 ID 必须为非空字符串")
            if entry is None:
                continue
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("明细必须为 (来源, 金额) 元组或 None")
            src, amt = entry
            if not isinstance(src, str) or not src:
                raise ValueError("明细来源必须为非空字符串")
            if isinstance(amt, bool) or not isinstance(amt, int):
                raise ValueError("金额必须为非布尔整数")

    # ------------------------------------------------------------------ #
    # 测试钩子：全量不变量自检（O(全部状态)，仅核验脚本使用）
    # ------------------------------------------------------------------ #
    def _debug_check_invariants(self):
        # 1. 全图无环（含孤立节点）
        for start in self._fwd:
            seen = set()
            cur = start
            while cur is not None:
                if cur in seen:
                    raise AssertionError("存在环")
                seen.add(cur)
                cur = self._fwd.get(cur)
        # 2. 反向依赖与正向边一致
        for src, tgt in self._fwd.items():
            assert src in self._rev.get(tgt, ()), "反向依赖缺失"
        for tgt, srcs in self._rev.items():
            assert srcs, "反向依赖集合不应为空"
            for src in srcs:
                assert self._fwd.get(src) == tgt, "反向依赖存在幽灵边"
        # 3. 来源小计等于明细逐源重算（零值源不保留键）
        recomputed = {}
        for _rid, (src, amt) in self._records.items():
            recomputed[src] = recomputed.get(src, 0) + amt
        recomputed = {k: v for k, v in recomputed.items() if v != 0}
        assert recomputed == self._src_sum, "来源小计不一致"
        # 4. 规范名汇总等于逐明细 resolve 后重算
        expected = {}
        for _rid, (src, amt) in self._records.items():
            root = self._resolve(src)
            expected[root] = expected.get(root, 0) + amt
        expected = {k: v for k, v in expected.items() if v != 0}
        assert expected == self.totals(), "规范名汇总不一致"
        # 5. 内部汇总不保留零值键
        assert all(v != 0 for v in self._totals.values()), "汇总残留零值"
        return True
