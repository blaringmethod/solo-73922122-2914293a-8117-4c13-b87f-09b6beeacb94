"""求解器单元测试：约束与最优性。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solver import Group, solve


def G(pos, *notes):
    return Group(pos=pos, notes=tuple(notes))


def test_no_punches_prefers_zero_shift():
    r = solve(10, 2, [G(2, "C4"), G(5, "G4")], [])
    assert r.feasible
    assert r.total_shift == 0
    assert [a["new"] for a in r.assignments] == [2, 5]
    assert sorted(r.holes_to_punch) == [(2, "C4"), (5, "G4")]
    assert r.reused == []


def test_punch_forces_shift_and_is_reused():
    # 第 3 格已打 C4，第 1 组（原第 4 格）须挪到 3 才能用上它
    r = solve(10, 2, [G(4, "C4"), G(7, "D4")], [(3, "C4")])
    assert r.feasible
    assert r.assignments[0]["new"] == 3
    assert r.assignments[0]["shift"] == -1
    assert r.total_shift == 1
    assert r.reused == [(3, "C4")]
    assert r.holes_to_punch == [(7, "D4")]


def test_unreachable_punch_infeasible():
    r = solve(10, 2, [G(4, "C4")], [(9, "C4")])
    assert not r.feasible


def test_punched_pitch_not_in_group_infeasible():
    # 第 4 格打了 D4，但落在第 4 格的组只有 C4，用不上这个孔
    r = solve(10, 2, [G(4, "C4")], [(4, "D4")])
    assert not r.feasible


def test_punch_after_last_group_infeasible():
    r = solve(10, 2, [G(4, "C4")], [(4, "C4"), (8, "C4")])
    assert not r.feasible


def test_same_pos_two_pitches_single_note_groups_infeasible():
    # 同一格打了两个音，但没有哪一组同时含这两个音
    r = solve(10, 2, [G(5, "C4"), G(6, "E4")], [(5, "C4"), (5, "E4")])
    assert not r.feasible


def test_same_pos_two_pitches_chord_group_feasible():
    # 同一格打了两个音，和弦组可以同时用上
    r = solve(10, 2, [G(5, "C4", "E4")], [(5, "C4"), (5, "E4")])
    assert r.feasible
    assert r.reused == [(5, "C4"), (5, "E4")]
    assert r.holes_to_punch == []


def test_spacing_forces_groups_apart():
    # 两个 C4 相邻，孔距 3，只能 (3,6)，总挪动 2
    r = solve(10, 3, [G(4, "C4"), G(5, "C4")], [])
    assert r.feasible
    pos = [a["new"] for a in r.assignments]
    assert pos[1] - pos[0] >= 3
    assert r.total_shift == 2


def test_min_total_shift_among_ties():
    # (3,6) 与 (4,7) 都可满足孔距 3，总挪动都应为 1 而非 2
    r = solve(10, 3, [G(4, "C4"), G(6, "C4")], [])
    assert r.feasible
    assert r.total_shift == 1


def test_punched_holes_own_spacing_conflict_infeasible():
    # 已打两个同音孔相距 1 < 2，且都必须用上 → 无解
    r = solve(10, 2, [G(3, "C4"), G(4, "C4")], [(3, "C4"), (4, "C4")])
    assert not r.feasible


def test_order_cannot_be_swapped():
    # 第 1 组要到 6、第 2 组要到 5 才能用上孔，但会颠倒顺序 → 无解
    r = solve(10, 1, [G(5, "C4"), G(6, "E4")], [(6, "C4"), (5, "E4")])
    assert not r.feasible


def test_strip_bounds_respected():
    r = solve(5, 2, [G(1, "C4")], [])
    assert r.feasible and r.assignments[0]["new"] in (1, 2)
    r2 = solve(5, 2, [G(5, "C4")], [(5, "C4")])
    assert r2.feasible and r2.assignments[0]["new"] == 5


def test_chord_group_partial_reuse():
    r = solve(10, 2, [G(4, "C4", "E4", "G4")], [(4, "E4")])
    assert r.feasible
    assert r.reused == [(4, "E4")]
    assert sorted(r.holes_to_punch) == [(4, "C4"), (4, "G4")]


def test_sixteen_groups_chain_zero_shift():
    groups = [G(2 * i + 1, "C4") for i in range(16)]  # 1,3,…,31，间距 2
    r = solve(32, 2, groups, [])
    assert r.feasible
    assert r.total_shift == 0


def test_shift_at_most_one_enforced():
    # 孔距 4 时两组 C4 至少相隔 4，但 ±1 挪不开 → 无解
    r = solve(10, 4, [G(4, "C4"), G(5, "C4")], [])
    assert not r.feasible
