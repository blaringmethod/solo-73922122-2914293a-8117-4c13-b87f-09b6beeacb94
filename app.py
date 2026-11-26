"""手摇音乐盒纸带续排网页（Flask + SQLite）。

功能：
- 录入纸带格数、同音孔最小间距、最多 16 组音符（同一拍格的音算一组）；
- 实际打出的孔可随时补记 / 删除误记；
- 改谱或补记后自动重排（也可手动触发）：每组整体前后挪至多一格、
  保持原顺序各占不同拍格、孔位在纸带内、满足同音孔距、用上全部已打孔，
  给出各组挪动格数之和最小的排样并列出还要打的孔；
- 无解时提示原因、收起旧排样（<details> 折叠）、保留打孔记录；
- 曲谱、打孔记录、历次排样结果均存入 SQLite。
"""

import json
import os
import re
import sqlite3

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   url_for)

from solver import Group, solve

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MUSICBOX_DB", os.path.join(BASE_DIR, "musicbox.db"))

MAX_GROUPS = 16
MAX_MIN_SPACING = 8  # 求解状态数随孔距指数增长，演示版限制孔距上限

SCHEMA = """
CREATE TABLE IF NOT EXISTS tunes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    strip_length INTEGER NOT NULL,
    min_spacing INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tune_id INTEGER NOT NULL REFERENCES tunes(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    orig_pos INTEGER NOT NULL,
    notes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS punches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tune_id INTEGER NOT NULL REFERENCES tunes(id) ON DELETE CASCADE,
    pos INTEGER NOT NULL,
    pitch TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tune_id, pos, pitch)
);
CREATE TABLE IF NOT EXISTS solutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tune_id INTEGER NOT NULL REFERENCES tunes(id) ON DELETE CASCADE,
    tune_version INTEGER NOT NULL,
    feasible INTEGER NOT NULL,
    total_shift INTEGER,
    detail TEXT,
    message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "musicbox-dev-secret")


# ---------------------------------------------------------------- 数据库

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(seed: bool = True):
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    if seed and conn.execute("SELECT COUNT(*) FROM tunes").fetchone()[0] == 0:
        seed_demo(conn)
    conn.commit()
    conn.close()


def seed_demo(conn):
    """写入一段《小星星》示例（含两个已打孔与首次排样），便于演示。"""
    cur = conn.execute(
        "INSERT INTO tunes (name, strip_length, min_spacing) VALUES (?,?,?)",
        ("示例：小星星片段", 28, 2))
    tid = cur.lastrowid
    melody = ["C4", "C4", "G4", "G4", "A4", "A4", "G4",
              "F4", "F4", "E4", "E4", "D4", "D4", "C4"]
    groups = []
    for seq, note in enumerate(melody, 1):
        pos = 2 * seq - 1
        conn.execute(
            "INSERT INTO groups (tune_id, seq, orig_pos, notes) VALUES (?,?,?,?)",
            (tid, seq, pos, note))
        groups.append(Group(pos=pos, notes=(note,)))
    punches = {(1, "C4"), (3, "C4")}
    for pos, pitch in sorted(punches):
        conn.execute(
            "INSERT OR IGNORE INTO punches (tune_id, pos, pitch) VALUES (?,?,?)",
            (tid, pos, pitch))
    res = solve(28, 2, groups, punches)
    if res.feasible:
        conn.execute(
            "INSERT INTO solutions"
            " (tune_id, tune_version, feasible, total_shift, detail, message)"
            " VALUES (?,?,?,?,?,?)",
            (tid, 1, 1, res.total_shift, json.dumps({
                "assignments": res.assignments,
                "holes_to_punch": res.holes_to_punch,
                "reused": res.reused,
            }, ensure_ascii=False), res.message))


# ---------------------------------------------------------------- 解析

_ENHARMONIC = {"DB": "C#", "EB": "D#", "GB": "F#", "AB": "G#", "BB": "A#"}
_PITCH_RE = re.compile(r"^([A-G])([#B])?(-?\d{1,2})$")


def normalize_pitch(raw: str) -> str:
    """规范化音名：c4->C4，降号转等音升号（Bb3->A#3）；也接受 MIDI 音高数字。"""
    s = raw.strip().upper()
    m = _PITCH_RE.match(s)
    if m:
        letter, acc, octave = m.groups()
        base = letter + (acc or "")
        return f"{_ENHARMONIC.get(base, base)}{octave}"
    if re.fullmatch(r"\d{1,3}", s):
        return s
    raise ValueError(f"无效音名：{raw!r}（应为 C4、F#3、Bb5 或 0-127 的数字）")


def parse_groups(text: str, strip_length: int) -> list[Group]:
    """解析音符组文本：每行「拍格: 音名1 音名2 …」，拍格严格递增。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not 1 <= len(lines) <= MAX_GROUPS:
        raise ValueError(
            f"音符组数量须在 1～{MAX_GROUPS} 之间（当前 {len(lines)} 行）")
    groups: list[Group] = []
    prev = 0
    for ln in lines:
        m = re.match(r"^(\d+)\s*[:：]?\s*(.+)$", ln)
        if not m:
            raise ValueError(f"无法解析行：{ln!r}，格式应为「拍格: 音名1 音名2 …」")
        pos = int(m.group(1))
        tokens = [t for t in re.split(r"[\s,，、]+", m.group(2)) if t]
        if not tokens:
            raise ValueError(f"第 {pos} 格一组至少需要一个音")
        notes: list[str] = []
        for t in tokens:
            p = normalize_pitch(t)
            if p not in notes:
                notes.append(p)
        if pos <= prev:
            raise ValueError("各组拍格必须按严格递增顺序填写")
        if not 1 <= pos <= strip_length:
            raise ValueError(f"拍格 {pos} 超出纸带范围 1～{strip_length}")
        prev = pos
        groups.append(Group(pos=pos, notes=tuple(notes)))
    return groups


def parse_punches(text: str, strip_length: int) -> list[tuple[int, str]]:
    """解析打孔补记文本：每行「拍格 音名」（分隔符可为空格/冒号/逗号）。"""
    out: list[tuple[int, str]] = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"^(\d+)\s*[:：,，\s]\s*(\S+)\s*$", ln)
        if not m:
            raise ValueError(f"无法解析打孔记录行：{ln!r}，格式应为「拍格 音名」")
        pos = int(m.group(1))
        if not 1 <= pos <= strip_length:
            raise ValueError(f"打孔位置 {pos} 超出纸带范围 1～{strip_length}")
        out.append((pos, normalize_pitch(m.group(2))))
    if not out:
        raise ValueError("没有可添加的打孔记录")
    return out


def parse_meta_form(form) -> tuple[str, int, int]:
    name = (form.get("name") or "").strip() or "未命名曲目"
    try:
        strip_length = int(form.get("strip_length", ""))
        min_spacing = int(form.get("min_spacing", ""))
    except ValueError:
        raise ValueError("纸带格数与同音孔距须为整数")
    if not 1 <= strip_length <= 100000:
        raise ValueError("纸带格数须在 1～100000 之间")
    if not 1 <= min_spacing <= MAX_MIN_SPACING:
        raise ValueError(f"同音孔最小间距须在 1～{MAX_MIN_SPACING} 之间")
    return name, strip_length, min_spacing


# ---------------------------------------------------------------- 求解

def load_tune(tid: int):
    row = get_db().execute("SELECT * FROM tunes WHERE id=?", (tid,)).fetchone()
    if row is None:
        abort(404)
    return row


def load_groups(tid: int) -> list[Group]:
    rows = get_db().execute(
        "SELECT orig_pos, notes FROM groups WHERE tune_id=? ORDER BY seq",
        (tid,)).fetchall()
    return [Group(pos=r["orig_pos"], notes=tuple(r["notes"].split()))
            for r in rows]


def bump_version(db, tid: int):
    db.execute("UPDATE tunes SET version = version + 1 WHERE id=?", (tid,))


def run_solve(tid: int):
    """按当前曲谱与打孔记录求解并存档，返回 SolveResult。"""
    db = get_db()
    tune = load_tune(tid)
    groups = load_groups(tid)
    punches = {(r["pos"], r["pitch"]) for r in db.execute(
        "SELECT pos, pitch FROM punches WHERE tune_id=?", (tid,))}
    res = solve(tune["strip_length"], tune["min_spacing"], groups, punches)
    detail = None
    if res.feasible:
        detail = json.dumps({
            "assignments": res.assignments,
            "holes_to_punch": res.holes_to_punch,
            "reused": res.reused,
        }, ensure_ascii=False)
    db.execute(
        "INSERT INTO solutions"
        " (tune_id, tune_version, feasible, total_shift, detail, message)"
        " VALUES (?,?,?,?,?,?)",
        (tid, tune["version"], 1 if res.feasible else 0,
         res.total_shift if res.feasible else None, detail, res.message))
    db.commit()
    return res


def auto_solve(tid: int):
    """改谱/补记后自动重排（已有音符组时才触发）。"""
    n = get_db().execute(
        "SELECT COUNT(*) AS c FROM groups WHERE tune_id=?", (tid,),
    ).fetchone()["c"]
    if n:
        return run_solve(tid)
    return None


# ---------------------------------------------------------------- 页面

@app.route("/")
def index():
    tunes = get_db().execute(
        "SELECT t.*,"
        " (SELECT COUNT(*) FROM groups g WHERE g.tune_id = t.id) AS ngroups,"
        " (SELECT COUNT(*) FROM punches p WHERE p.tune_id = t.id) AS npunches"
        " FROM tunes t ORDER BY t.id DESC").fetchall()
    return render_template("index.html", tunes=tunes)


@app.route("/tunes", methods=["POST"])
def create_tune():
    try:
        name, strip_length, min_spacing = parse_meta_form(request.form)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))
    db = get_db()
    cur = db.execute(
        "INSERT INTO tunes (name, strip_length, min_spacing) VALUES (?,?,?)",
        (name, strip_length, min_spacing))
    db.commit()
    flash("已创建曲目，请录入音符组", "ok")
    return redirect(url_for("tune_page", tid=cur.lastrowid))


@app.route("/tune/<int:tid>")
def tune_page(tid):
    db = get_db()
    tune = load_tune(tid)
    punches = db.execute(
        "SELECT * FROM punches WHERE tune_id=? ORDER BY pos, pitch",
        (tid,)).fetchall()
    groups_text = "\n".join(
        f"{r['orig_pos']}: {r['notes']}" for r in db.execute(
            "SELECT orig_pos, notes FROM groups WHERE tune_id=? ORDER BY seq",
            (tid,)))
    latest = db.execute(
        "SELECT * FROM solutions WHERE tune_id=? ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()
    last_feasible = db.execute(
        "SELECT * FROM solutions WHERE tune_id=? AND feasible=1"
        " ORDER BY id DESC LIMIT 1", (tid,)).fetchone()

    latest_detail = json.loads(latest["detail"]) if latest and latest["detail"] else None
    # 无解时收起旧排样：最近一次可行排样放进折叠的 <details>
    old_detail = None
    if (latest and not latest["feasible"] and last_feasible
            and last_feasible["id"] != latest["id"]):
        old_detail = json.loads(last_feasible["detail"])

    to_punch_grouped = []
    if latest_detail:
        for pos, pitch in latest_detail["holes_to_punch"]:
            if to_punch_grouped and to_punch_grouped[-1][0] == pos:
                to_punch_grouped[-1][1].append(pitch)
            else:
                to_punch_grouped.append([pos, [pitch]])

    return render_template(
        "tune.html", tune=tune, punches=punches, groups_text=groups_text,
        latest=latest, latest_detail=latest_detail, old_detail=old_detail,
        to_punch_grouped=to_punch_grouped,
        max_groups=MAX_GROUPS, max_spacing=MAX_MIN_SPACING)


@app.route("/tune/<int:tid>/meta", methods=["POST"])
def update_meta(tid):
    db = get_db()
    load_tune(tid)
    try:
        name, strip_length, min_spacing = parse_meta_form(request.form)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("tune_page", tid=tid))
    out_of_range = db.execute(
        "SELECT (SELECT COUNT(*) FROM groups WHERE tune_id=? AND orig_pos>?)"
        "     + (SELECT COUNT(*) FROM punches WHERE tune_id=? AND pos>?) AS c",
        (tid, strip_length, tid, strip_length)).fetchone()["c"]
    if out_of_range:
        flash("新的纸带格数小于已有音符组或打孔位置，未保存", "error")
        return redirect(url_for("tune_page", tid=tid))
    db.execute(
        "UPDATE tunes SET name=?, strip_length=?, min_spacing=? WHERE id=?",
        (name, strip_length, min_spacing, tid))
    bump_version(db, tid)
    db.commit()
    auto_solve(tid)
    flash("参数已保存", "ok")
    return redirect(url_for("tune_page", tid=tid))


@app.route("/tune/<int:tid>/groups", methods=["POST"])
def save_groups(tid):
    db = get_db()
    tune = load_tune(tid)
    try:
        groups = parse_groups(request.form.get("groups_text", ""),
                              tune["strip_length"])
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("tune_page", tid=tid))
    db.execute("DELETE FROM groups WHERE tune_id=?", (tid,))
    for seq, grp in enumerate(groups, 1):
        db.execute(
            "INSERT INTO groups (tune_id, seq, orig_pos, notes) VALUES (?,?,?,?)",
            (tid, seq, grp.pos, " ".join(grp.notes)))
    bump_version(db, tid)
    db.commit()
    res = run_solve(tid)
    if res.feasible:
        flash(f"曲谱已保存，重排成功：总挪动 {res.total_shift} 格", "ok")
    else:
        flash(f"曲谱已保存，但重排无解：{res.message}", "error")
    return redirect(url_for("tune_page", tid=tid))


@app.route("/tune/<int:tid>/punches", methods=["POST"])
def add_punches(tid):
    db = get_db()
    tune = load_tune(tid)
    try:
        items = parse_punches(request.form.get("punches_text", ""),
                              tune["strip_length"])
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("tune_page", tid=tid))
    added = 0
    for pos, pitch in items:
        cur = db.execute(
            "INSERT OR IGNORE INTO punches (tune_id, pos, pitch) VALUES (?,?,?)",
            (tid, pos, pitch))
        added += cur.rowcount
    bump_version(db, tid)
    db.commit()
    res = auto_solve(tid)
    msg = f"补记 {added} 个已打孔"
    if len(items) - added:
        msg += f"（{len(items) - added} 个重复已忽略）"
    if res is not None:
        msg += "；重排成功" if res.feasible else f"；重排无解：{res.message}"
    flash(msg, "ok" if res is None or res.feasible else "error")
    return redirect(url_for("tune_page", tid=tid))


@app.route("/tune/<int:tid>/punches/<int:pid>/delete", methods=["POST"])
def delete_punch(tid, pid):
    db = get_db()
    load_tune(tid)
    db.execute("DELETE FROM punches WHERE id=? AND tune_id=?", (pid, tid))
    bump_version(db, tid)
    db.commit()
    auto_solve(tid)
    flash("已删除该打孔记录", "ok")
    return redirect(url_for("tune_page", tid=tid))


@app.route("/tune/<int:tid>/solve", methods=["POST"])
def solve_now(tid):
    load_tune(tid)
    res = run_solve(tid)
    if res.feasible:
        flash(f"重排完成：总挪动 {res.total_shift} 格", "ok")
    else:
        flash(f"无解：{res.message}", "error")
    return redirect(url_for("tune_page", tid=tid))


@app.route("/tune/<int:tid>/delete", methods=["POST"])
def delete_tune(tid):
    db = get_db()
    load_tune(tid)
    db.execute("DELETE FROM tunes WHERE id=?", (tid,))
    db.commit()
    flash("曲目已删除", "ok")
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db(seed=os.environ.get("SEED_DEMO", "1") != "0")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
