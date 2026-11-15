"""独立全量参照实现与差分核验。

ref_store.py 用最直白的“整批重建 + 全量扫描”方式重写一份语义参照，
不导入 normalizer 的任何内部结构；verify.py 随机生成操作流，逐批比较
两个实现的 apply 返回值、resolve、totals，并检查不变量与失败原子性。
"""


class RefStore:
    """朴素参照：每批都复制/重建全部状态，逻辑与优化版完全独立。"""

    def __init__(self):
        self._fwd = {}
        self._records = {}

    def resolve(self, name):
        if not isinstance(name, str) or not name:
            raise ValueError("名称必须为非空字符串")
        cur = name
        seen = set()
        while cur in self._fwd:
            if cur in seen:
                raise ValueError("别名成环")
            seen.add(cur)
            cur = self._fwd[cur]
        return cur

    def totals(self):
        out = {}
        for _rid, (src, amt) in self._records.items():
            root = self.resolve(src)
            out[root] = out.get(root, 0) + amt
        return {k: v for k, v in out.items() if v != 0}

    @staticmethod
    def _validate(aliases, rows):
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

    def apply(self, aliases, rows):
        # 先在副本上构造批后状态，任何异常都不触碰原状态。
        self._validate(aliases, rows)
        new_fwd = dict(self._fwd)
        for src, tgt in aliases.items():
            if tgt is None:
                new_fwd.pop(src, None)
            else:
                new_fwd[src] = tgt

        # 全量无环检查
        for start in new_fwd:
            seen = set()
            cur = start
            while cur in new_fwd:
                if cur in seen:
                    raise ValueError("别名成环，整批作废")
                seen.add(cur)
                cur = new_fwd[cur]

        new_records = dict(self._records)
        for rid, entry in rows.items():
            if entry is None:
                new_records.pop(rid, None)
            else:
                new_records[rid] = entry

        before = self.totals()
        # 提交
        self._fwd = new_fwd
        self._records = new_records
        after = self.totals()
        changed = {}
        for key in set(before) | set(after):
            b, a = before.get(key, 0), after.get(key, 0)
            if b != a:
                changed[key] = a
        return changed
