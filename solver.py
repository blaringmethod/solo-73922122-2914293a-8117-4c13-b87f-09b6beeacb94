"""手摇音乐盒纸带续排求解器。

模型
----
- 纸带共 strip_length 格，编号 1..N。
- 曲谱为 k 个音符组（1 <= k <= 16），第 i 组原始拍格 pos_i，组内为一组音名
  （同一拍格的音符算一组，组内音名去重）。
- 重排时第 i 组整体平移 delta_i ∈ {-1, 0, +1}，新位置 q_i = pos_i + delta_i。
- 约束：
  1. q_1 < q_2 < ... < q_k（各组保持原顺序、各占不同拍格）；
  2. 1 <= q_i <= N（孔位须在纸带内）；
  3. 同音孔距：任意两个相同音名的孔，位置差 >= min_spacing；
  4. 已打孔集合 punches 必须全部被用上：每个已打孔 (x, p) 都要求
     某组落在 x 且 p 属于该组（已打孔是最终孔位的子集）。
- 目标：min Σ|delta_i|（各组挪动格数之和最小）。

算法
----
按组顺序做动态规划。由于位置严格递增，下标差 >= min_spacing 的两组，
位置差必然 >= min_spacing，同音孔距只需回看最近 min_spacing-1 组的落位；
再考虑到定序需要上一组位置，状态取最近 max(1, min_spacing-1) 个位置。
每组候选位置至多 3 个，状态数至多 3^(min_spacing-1)，k <= 16 时很快。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class Group:
    """一个音符组：原始拍格 + 组内音名（已规范化、去重、保持输入顺序）。"""

    pos: int
    notes: tuple[str, ...]


@dataclass
class SolveResult:
    feasible: bool
    message: str = ""
    assignments: list[dict] = field(default_factory=list)
    total_shift: int = 0
    holes_to_punch: list[tuple[int, str]] = field(default_factory=list)
    reused: list[tuple[int, str]] = field(default_factory=list)


def solve(
    strip_length: int,
    min_spacing: int,
    groups: list[Group],
    punches: Iterable[tuple[int, str]],
) -> SolveResult:
    """求总挪动格数最小的排样；无解时返回 feasible=False 及原因。"""
    punches = set(punches)
    k = len(groups)

    punched_by_pos: dict[int, set[str]] = {}
    for pos, pitch in punches:
        punched_by_pos.setdefault(pos, set()).add(pitch)
    punched_positions = sorted(punched_by_pos)

    # 预检：每个已打孔位置至少要有一组能整体挪到（±1 格）
    for x in punched_positions:
        if not any(g.pos - 1 <= x <= g.pos + 1 for g in groups):
            return SolveResult(
                False, f"纸带第 {x} 格已打孔，但没有任何音符组能整体挪到该格")

    if k == 0:
        if punches:
            return SolveResult(False, "没有音符组，无法用上已打的孔")
        return SolveResult(True, "空曲谱，无需打孔")

    # 每组候选位置：±1 格、在纸带内、且落在该格时能用上该格全部已打孔
    cand: list[list[int]] = []
    for idx, g in enumerate(groups):
        notes = set(g.notes)
        cs = []
        for q in (g.pos - 1, g.pos, g.pos + 1):
            if not 1 <= q <= strip_length:
                continue
            need = punched_by_pos.get(q)
            if need and not need <= notes:
                continue
            cs.append(q)
        if not cs:
            return SolveResult(
                False, f"第 {idx + 1} 组（原第 {g.pos} 格）没有可落位的拍格")
        cand.append(cs)

    lookback = max(1, min_spacing - 1)

    def punched_between(a: int, b: int) -> bool:
        """开区间 (a, b) 内是否存在已打孔位置（存在则该分支无法用上它）。"""
        i = bisect.bisect_right(punched_positions, a)
        return i < len(punched_positions) and punched_positions[i] < b

    # dp: 状态（最近 <=lookback 个落位的元组） -> 最小总挪动格数
    dp: dict[tuple[int, ...], int] = {(): 0}
    parents: list[dict[tuple[int, ...], tuple[tuple[int, ...], int]]] = []

    for i in range(k):
        notes_i = set(groups[i].notes)
        new_dp: dict[tuple[int, ...], int] = {}
        new_par: dict[tuple[int, ...], tuple[tuple[int, ...], int]] = {}
        for state, cost in dp.items():
            prev_pos = state[-1] if state else 0
            for q in cand[i]:
                if q <= prev_pos:
                    continue  # 保序且各占不同拍格
                if punched_between(prev_pos, q):
                    continue  # 中间夹着用不上的已打孔
                ok = True
                slen = len(state)
                for j in range(slen):
                    pq = state[j]
                    if q - pq >= min_spacing:
                        continue
                    gidx = i - (slen - j)
                    if notes_i & set(groups[gidx].notes):
                        ok = False  # 同音孔距不足
                        break
                if not ok:
                    continue
                ns = (state + (q,))[-lookback:]
                nc = cost + abs(q - groups[i].pos)
                if ns not in new_dp or nc < new_dp[ns]:
                    new_dp[ns] = nc
                    new_par[ns] = (state, q)
        if not new_dp:
            return SolveResult(
                False, "在保序、同音孔距与已打孔约束下无法排下全部音符组")
        dp = new_dp
        parents.append(new_par)

    # 末尾之后不能再有未覆盖的已打孔位置
    best_state: tuple[int, ...] | None = None
    best_cost: int | None = None
    for state, cost in dp.items():
        last_q = state[-1]
        if bisect.bisect_right(punched_positions, last_q) < len(punched_positions):
            continue
        if best_cost is None or cost < best_cost:
            best_state, best_cost = state, cost
    if best_state is None:
        return SolveResult(False, "末尾的已打孔位置超出所有组可达范围")

    # 回溯出每组落位
    qs = [0] * k
    st = best_state
    for i in range(k - 1, -1, -1):
        prev_st, q = parents[i][st]
        qs[i] = q
        st = prev_st

    assignments = []
    holes_to_punch: list[tuple[int, str]] = []
    reused: list[tuple[int, str]] = []
    for i, (g, q) in enumerate(zip(groups, qs)):
        assignments.append({
            "seq": i + 1,
            "notes": list(g.notes),
            "orig": g.pos,
            "new": q,
            "shift": q - g.pos,
        })
        for pitch in g.notes:
            if (q, pitch) in punches:
                reused.append((q, pitch))
            else:
                holes_to_punch.append((q, pitch))
    holes_to_punch.sort()
    reused.sort()

    return SolveResult(
        True,
        "OK",
        assignments=assignments,
        total_shift=best_cost if best_cost is not None else 0,
        holes_to_punch=holes_to_punch,
        reused=reused,
    )
