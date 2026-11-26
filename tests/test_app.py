"""Flask 路由级自测：建曲、录谱、补记、重排、无解收起旧排样、恢复。"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_fd, DB = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["MUSICBOX_DB"] = DB

import app as m  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture()
def client():
    if os.path.exists(DB):
        os.remove(DB)
    m.init_db(seed=False)
    m.app.config.update(TESTING=True)
    with m.app.test_client() as c:
        yield c


def create_tune(client, **kw):
    data = {"name": "测试曲", "strip_length": "12", "min_spacing": "2"}
    data.update(kw)
    resp = client.post("/tunes", data=data)
    assert resp.status_code == 302
    return int(resp.headers["Location"].rstrip("/").split("/")[-1])


def save_groups(client, tid, text):
    return client.post(f"/tune/{tid}/groups", data={"groups_text": text},
                       follow_redirects=True)


def test_create_save_solve_flow(client):
    tid = create_tune(client)
    resp = save_groups(client, tid, "2: C4\n5: G4 E4")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "重排成功" in page
    assert "总挪动格数" in page
    # 还要打的孔：2:C4、5:G4、5:E4
    assert "还要打的孔（3 个）" in page
    assert "第 2 格：C4" in page
    assert "第 5 格：E4、G4" in page or "第 5 格：G4、E4" in page


def test_punch_record_and_reuse(client):
    tid = create_tune(client)
    save_groups(client, tid, "2: C4\n5: G4")
    resp = client.post(f"/tune/{tid}/punches",
                       data={"punches_text": "2 C4"}, follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert "沿用的已打孔（1 个）" in page
    assert "还要打的孔（1 个）" in page
    # 打孔记录已入库
    conn = sqlite3.connect(DB)
    n = conn.execute("SELECT COUNT(*) FROM punches WHERE tune_id=?", (tid,)).fetchone()[0]
    conn.close()
    assert n == 1


def test_duplicate_punch_ignored(client):
    tid = create_tune(client)
    save_groups(client, tid, "2: C4")
    client.post(f"/tune/{tid}/punches", data={"punches_text": "2 C4"})
    client.post(f"/tune/{tid}/punches", data={"punches_text": "2 C4"})
    conn = sqlite3.connect(DB)
    n = conn.execute("SELECT COUNT(*) FROM punches WHERE tune_id=?", (tid,)).fetchone()[0]
    conn.close()
    assert n == 1


def test_infeasible_collapses_old_solution_keeps_punches(client):
    tid = create_tune(client)
    save_groups(client, tid, "2: C4")
    # 先得到一个可行排样
    page = client.get(f"/tune/{tid}").get_data(as_text=True)
    assert "总挪动格数" in page
    # 补记一个任何组都够不着的孔 → 无解
    resp = client.post(f"/tune/{tid}/punches",
                       data={"punches_text": "11 C4"}, follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert "无解" in page
    assert "旧排样" in page and "<details" in page  # 旧排样被收起
    assert "勿照此打孔" in page
    # 打孔记录保留
    assert "11" in page and "C4" in page
    conn = sqlite3.connect(DB)
    n = conn.execute("SELECT COUNT(*) FROM punches WHERE tune_id=?", (tid,)).fetchone()[0]
    conn.close()
    assert n == 1


def test_delete_bad_punch_recovers_solution(client):
    tid = create_tune(client)
    save_groups(client, tid, "2: C4")
    client.post(f"/tune/{tid}/punches", data={"punches_text": "11 C4"})
    page = client.get(f"/tune/{tid}").get_data(as_text=True)
    assert "无解" in page
    conn = sqlite3.connect(DB)
    pid = conn.execute(
        "SELECT id FROM punches WHERE tune_id=? AND pos=11", (tid,)).fetchone()[0]
    conn.close()
    resp = client.post(f"/tune/{tid}/punches/{pid}/delete", follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert "总挪动格数" in page  # 删除误记孔后自动重排成功


def test_group_limit_16(client):
    tid = create_tune(client, strip_length="60")
    text = "\n".join(f"{3 * i + 1}: C4" for i in range(17))
    resp = save_groups(client, tid, text)
    assert "1～16" in resp.get_data(as_text=True)


def test_groups_must_be_increasing(client):
    tid = create_tune(client)
    resp = save_groups(client, tid, "5: C4\n3: G4")
    assert "严格递增" in resp.get_data(as_text=True)


def test_group_pos_out_of_strip(client):
    tid = create_tune(client)
    resp = save_groups(client, tid, "2: C4\n99: G4")
    assert "超出纸带范围" in resp.get_data(as_text=True)


def test_punch_out_of_strip_rejected(client):
    tid = create_tune(client)
    save_groups(client, tid, "2: C4")
    resp = client.post(f"/tune/{tid}/punches",
                       data={"punches_text": "99 C4"}, follow_redirects=True)
    assert "超出纸带范围" in resp.get_data(as_text=True)


def test_pitch_normalization(client):
    tid = create_tune(client)
    resp = save_groups(client, tid, "2: c4 Bb3\n5: 60")
    page = resp.get_data(as_text=True)
    assert "C4" in page and "A#3" in page and "60" in page


def test_meta_update_triggers_resolve(client):
    tid = create_tune(client)
    save_groups(client, tid, "2: C4\n3: C4\n4: C4")  # 孔距 2 下可挪成 (1,3,5)
    page = client.get(f"/tune/{tid}").get_data(as_text=True)
    assert "总挪动格数" in page
    # 把孔距改成 3 → 三组同音 ±1 挪不出两两 ≥3 → 自动重排得无解
    resp = client.post(f"/tune/{tid}/meta",
                       data={"name": "测试曲", "strip_length": "12",
                             "min_spacing": "3"},
                       follow_redirects=True)
    assert "无解" in resp.get_data(as_text=True)


def test_manual_solve_button(client):
    tid = create_tune(client)
    save_groups(client, tid, "2: C4")
    resp = client.post(f"/tune/{tid}/solve", follow_redirects=True)
    assert "重排完成" in resp.get_data(as_text=True)


def test_index_lists_tune(client):
    tid = create_tune(client, name="我的曲子")
    save_groups(client, tid, "2: C4")
    page = client.get("/").get_data(as_text=True)
    assert "我的曲子" in page
