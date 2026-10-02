"""财务期间关账/解除端到端集成测试
（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_closings.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_close_pg_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    from fastapi.testclient import TestClient
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    def agree(c, work, frm, to, shares):
        r = c.post("/agreements", json={
            "work_code": work, "valid_from": frm, "valid_to": to,
            "shares": [{"holder_code": h, "percent": p} for h, p in shares]})
        assert r.status_code == 201, r.text

    FEB = {"start": "2026-02-01T00:00:00+08:00",
           "end": "2026-03-01T00:00:00+08:00"}
    MAR = {"start": "2026-03-01T00:00:00+08:00",
           "end": "2026-04-01T00:00:00+08:00"}

    def play(c, pid, work, amount, when="2026-02-10T20:00:00+08:00", corr=None):
        r = c.post("/plays/import", json={
            "import_id": pid, "work_code": work, "occurred_at": when,
            "amount": amount, "corrects_id": corr})
        assert r.status_code == 201, r.text

    def close(c, ref, period, code=201):
        r = c.post("/period-closings", json={"close_ref": ref, **period})
        assert r.status_code == code, r.text
        return r

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        c.post("/works", json={"code": "W1", "title": "二月作品"})
        c.post("/works", json={"code": "W2", "title": "无协议作品"})
        c.post("/works", json={"code": "W3", "title": "三月作品"})
        agree(c, "W1", "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00",
              [("A", "62.5"), ("B", "30"), ("C", "7.5")])
        agree(c, "W3", "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00",
              [("A", "100")])

        # P1=100 在 2 月（协议），Q1=100 在 2 月（W2 无协议 => 待处理）
        play(c, "P1", "W1", 100)
        play(c, "Q1", "W2", 100)

        # ---------- 1) 无已出单据拒绝且不留记录 ----------
        r = close(c, "CLS-EMPTY", FEB, code=409)
        check("close no statement 409",
              r.json()["error"] == "period_has_no_statement", r.text)
        check("rejected close absent",
              c.get("/period-closings/CLS-EMPTY").status_code == 404)

        # 出单：100 => 63/30/7，待处理 Q1 一条
        r = c.post("/statements/issue", json=FEB)
        body = r.json()
        initial_id = body["batch_id"]
        check("initial issued", body["kind"] == "initial")
        got = {x["holder_code"]: x["amount"] for x in body["payable"]}
        check("initial 63/30/7", got == {"A": 63, "B": 30, "C": 7}, str(got))
        check("initial pending 1", len(body["pending"]) == 1
              and body["pending"][0]["import_id"] == "Q1")

        # ---------- 2) 关账：快照对应最后一张单据 ----------
        r = close(c, "CLS-1", FEB)
        b = r.json()
        check("close 201 created", r.status_code == 201 and b["status"] == "created")
        check("close latest batch", b["latest_batch_id"] == initial_id)
        check("close pending count", b["pending_count"] == 1, str(b["pending_count"]))
        check("close payables",
              {x["holder_code"]: x["payable_total"] for x in b["payables"]}
              == {"A": 63, "B": 30, "C": 7}, str(b["payables"]))
        check("close status fields",
              b["closing_status"] == "closed" and b["released_at"] is None)

        # ---------- 3) 同号同期间重试：原关账记录 ----------
        r = close(c, "CLS-1", FEB, code=200)
        b2 = r.json()
        check("retry duplicate", b2["status"] == "duplicate"
              and b2["closing_status"] == "closed"
              and b2["latest_batch_id"] == initial_id
              and b2["closed_at"] == b["closed_at"])

        # ---------- 4) 异期间复用同号 409 ----------
        # 先给三月出一张单（避免被"无单据"规则提前拦截）
        play(c, "M1", "W3", 50, when="2026-03-10T20:00:00+08:00")
        c.post("/statements/issue", json=MAR)
        r = close(c, "CLS-1", MAR, code=409)
        check("ref reuse diff period 409",
              r.json()["error"] == "closing_ref_conflict", r.text)

        # ---------- 5) 同期间重复关账（新关账号）409，不留记录 ----------
        r = close(c, "CLS-2", FEB, code=409)
        check("duplicate close other ref 409",
              r.json()["error"] == "period_already_closed", r.text)
        check("second ref absent",
              c.get("/period-closings/CLS-2").status_code == 404)

        # ---------- 6) 未知关账号：查询/解除 404 ----------
        r = c.get("/period-closings/NOPE")
        check("unknown get 404", r.status_code == 404
              and r.json()["error"] == "period_closing_not_found")
        r = c.post("/period-closings/NOPE/release")
        check("unknown release 404", r.status_code == 404
              and r.json()["error"] == "period_closing_not_found", r.text)

        # 字段校验
        check("empty ref 422",
              c.post("/period-closings", json={"close_ref": "", **FEB}).status_code == 422)
        check("bad range 422",
              c.post("/period-closings", json={
                  "close_ref": "X",
                  "start": "2026-03-01T00:00:00+08:00",
                  "end": "2026-02-01T00:00:00+08:00"}).status_code == 422)
        check("naive datetime 422",
              c.post("/period-closings", json={
                  "close_ref": "X", "start": "2026-02-01T00:00:00",
                  "end": "2026-03-01T00:00:00"}).status_code == 422)

        # ---------- 7) 关账后该期间再次出单拒绝；历史单据与归因不变 ----------
        attr_before = c.get(f"/statements/{initial_id}/attributions").json()
        r = c.post("/statements/issue", json=FEB)
        check("issue after close 409", r.status_code == 409
              and r.json()["error"] == "period_closed", r.text)
        check("error detail carries ref",
              r.json()["detail"]["close_ref"] == "CLS-1")
        batches = c.get("/statements", params=FEB).json()
        check("still one batch", len(batches) == 1
              and batches[0]["batch_id"] == initial_id)
        attr_after = c.get(f"/statements/{initial_id}/attributions").json()
        check("evidence unchanged",
              attr_after["total_records"] == attr_before["total_records"]
              and attr_after["holder_totals"] == attr_before["holder_totals"])

        # 普通（未关账）期间出单不受影响
        play(c, "M2", "W3", 50, when="2026-03-11T20:00:00+08:00")
        r = c.post("/statements/issue", json=MAR)
        check("normal period issue works",
              r.status_code == 200 and r.json()["kind"] == "supplement"
              and {x["holder_code"]: x["delta"] for x in r.json()["payable"]
                   if x["delta"] is not None} == {"A": 50}, r.text)

        # ---------- 8) 播放更正/协议登记/争议操作照常 ----------
        # 更正 P1 100 -> 80（50/24/6），并后补 W2 协议、给 Q1 开争议
        play(c, "P1R1", "W1", 80, corr="P1")
        agree(c, "W2", "2026-01-01T00:00:00+08:00", "2027-01-01T00:00+08:00",
              [("A", "100")])
        r = c.post("/play-disputes", json={
            "play_import_id": "Q1", "dispute_ref": "DSP-Q", "reason": "关账期间争议照常"})
        check("dispute while closed", r.status_code == 201, r.text)

        # ---------- 9) 试算照常：实时应付与关账应付差额 ----------
        r = c.get("/statements/preview", params=FEB)
        pv = r.json()
        check("closed preview has closing block", "closing" in pv)
        cl = pv["closing"]
        check("closing block ref", cl["close_ref"] == "CLS-1"
              and cl["latest_batch_id"] == initial_id)
        diffs = {x["holder_code"]:
                 (x["current_payable"], x["closed_payable"], x["difference"])
                 for x in cl["payables"]}
        # P1 链更正为 80 => 50/24/6；Q1 争议中不计应付
        check("realtime vs closed diffs",
              diffs == {"A": (50, 63, -13), "B": (24, 30, -6), "C": (6, 7, -1)},
              str(diffs))
        check("pending counts current/closed",
              cl["pending_count_current"] == 1 and cl["pending_count_closed"] == 1,
              str(cl))
        # 争议解除后 Q1 按后补协议恢复 100：实时应付 A=150，差额 +87
        c.post("/play-disputes/DSP-Q/release")
        cl = c.get("/statements/preview", params=FEB).json()["closing"]
        a_row = next(x for x in cl["payables"] if x["holder_code"] == "A")
        check("Q1 recovered realtime",
              a_row["current_payable"] == 150 and a_row["difference"] == 87
              and cl["pending_count_current"] == 0, str(cl))

        # 普通期间试算无 closing 块
        check("normal preview no closing block",
              "closing" not in c.get("/statements/preview", params=MAR).json())

        # 关账期间仍拒绝出单（即使实时应付与快照已不一致）
        check("issue still rejected while closed",
              c.post("/statements/issue", json=FEB).status_code == 409)

        # 关账查询返回冻结快照（不随后续数据变化）
        b = c.get("/period-closings/CLS-1").json()
        check("get frozen snapshot",
              b["closing_status"] == "closed"
              and {x["holder_code"]: x["payable_total"] for x in b["payables"]}
              == {"A": 63, "B": 30, "C": 7}
              and b["pending_count"] == 1, str(b))

        # ---------- 10) 解除：幂等；解除后按既有规则追补 ----------
        r = c.post("/period-closings/CLS-1/release")
        b = r.json()
        check("release 200 released", r.status_code == 200 and b["status"] == "released"
              and b["closing_status"] == "released"
              and b["released_at"] is not None
              and b["latest_batch_id"] == initial_id)
        r = c.post("/period-closings/CLS-1/release")
        check("release idempotent", r.status_code == 200
              and r.json()["status"] == "duplicate"
              and r.json()["closing_status"] == "released")

        # 解除后试算不再附带 closing 块
        check("preview normal after release",
              "closing" not in c.get("/statements/preview", params=FEB).json())

        # 解除后出追补单：相对关账累计 63/30/7 => 实时 150/24/6
        r = c.post("/statements/issue", json=FEB)
        body = r.json()
        check("supplement after release", body["kind"] == "supplement", r.text)
        got = {x["holder_code"]: (x["amount"], x["delta"]) for x in body["payable"]}
        check("supplement deltas vs closed",
              got == {"A": (150, 87), "B": (24, -6), "C": (6, -1)}, str(got))

        # ---------- 11) 解除后同期间再次关账：拒绝（期间至多一条关账记录）----------
        r = close(c, "CLS-3", FEB, code=409)
        check("reclose after release 409",
              r.json()["error"] == "period_already_closed", r.text)
        check("CLS-3 absent",
              c.get("/period-closings/CLS-3").status_code == 404)
        # 原关账号查询仍为 released
        check("old ref still released",
              c.get("/period-closings/CLS-1").json()["closing_status"] == "released")

        # ---------- 12) 无应付只有待处理的初次单也可关账（pending_count 固化）----------
        c.post("/works", json={"code": "W4", "title": "争议作品"})
        agree(c, "W4", "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00",
              [("A", "70"), ("B", "30")])
        play(c, "D1", "W4", 100, when="2026-04-10T20:00:00+08:00")
        APR = {"start": "2026-04-01T00:00:00+08:00",
               "end": "2026-05-01T00:00:00+08:00"}
        c.post("/play-disputes", json={
            "play_import_id": "D1", "dispute_ref": "DSP-D", "reason": "x"})
        r = c.post("/statements/issue", json=APR)
        check("april initial pending only",
              r.json()["payable"] == [] and len(r.json()["pending"]) == 1)
        r = close(c, "CLS-APR", APR)
        check("close pending-only period",
              r.status_code == 201 and r.json()["payables"] == []
              and r.json()["pending_count"] == 1, r.text)
        c.post("/period-closings/CLS-APR/release")

        # ---------- 13) 并发：关账与出单按期间串行，关账后不漏出单 ----------
        c.post("/works", json={"code": "W5", "title": "并发作品"})
        agree(c, "W5", "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00",
              [("A", "100")])
        play(c, "CC1", "W5", 100, when="2026-05-10T20:00:00+08:00")
        MAY = {"start": "2026-05-01T00:00:00+08:00",
               "end": "2026-06-01T00:00:00+08:00"}
        # 先有一张已出单据，关账才合法
        r = c.post("/statements/issue", json=MAY)
        check("may initial", r.json()["kind"] == "initial", r.text)

        statuses = []
        barrier = threading.Barrier(3)

        def fire(fn):
            barrier.wait()
            try:
                statuses.append(fn().status_code)
            except Exception as e:  # noqa
                statuses.append(repr(e))

        t1 = threading.Thread(target=fire, args=(
            lambda: close(c, "CLS-CC", MAY),))
        t2 = threading.Thread(target=fire, args=(
            lambda: c.post("/statements/issue", json=MAY),))
        t3 = threading.Thread(target=fire, args=(
            lambda: c.post("/statements/issue", json=MAY),))
        [t.start() for t in (t1, t2, t3)]
        [t.join() for t in (t1, t2, t3)]
        check("concurrent close vs issue: one 201",
              statuses.count(201) == 1, str(statuses))
        # 关账必成功；出单先于关账则 200、后于关账则 409，无其他状态
        check("concurrent statuses only 200/409",
              sorted(set(statuses)) <= [200, 201, 409], str(statuses))
        check("concurrent close present",
              c.get("/period-closings/CLS-CC").json()["closing_status"] == "closed")
        # 收敛后：该期间永远拒绝出单
        r = c.post("/statements/issue", json=MAY)
        check("closed stays rejected", r.status_code == 409
              and r.json()["error"] == "period_closed")

        # 13b) 同关账号并发关账：恰一笔 201，其余 200
        c.post("/period-closings/CLS-CC/release")
        play(c, "CC2", "W5", 100, when="2026-06-10T20:00:00+08:00")
        JUN = {"start": "2026-06-01T00:00:00+08:00",
               "end": "2026-07-01T00:00:00+08:00"}
        c.post("/statements/issue", json=JUN)
        codes = []
        barrier2 = threading.Barrier(5)

        def fire_same_ref():
            barrier2.wait()
            codes.append(c.post("/period-closings",
                                json={"close_ref": "CLS-JUN", **JUN}).status_code)

        ts = [threading.Thread(target=fire_same_ref) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("same ref concurrent: one 201 rest 200",
              codes.count(201) == 1 and codes.count(200) == 4, str(codes))

        # 13c) 不同关账号并发关同一期间：恰一笔成功，其余 409，不留多余记录
        play(c, "CC3", "W5", 100, when="2026-07-10T20:00:00+08:00")
        JUL = {"start": "2026-07-01T00:00:00+08:00",
               "end": "2026-08-01T00:00:00+08:00"}
        c.post("/statements/issue", json=JUL)
        res = {}
        barrier3 = threading.Barrier(6)

        def fire_diff_ref(i):
            barrier3.wait()
            r = c.post("/period-closings",
                       json={"close_ref": f"CLS-JUL-{i}", **JUL})
            res[i] = r.status_code

        ts = [threading.Thread(target=fire_diff_ref, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        vals = list(res.values())
        check("diff refs concurrent same period: one 201 rest 409",
              vals.count(201) == 1 and vals.count(409) == 5, str(vals))

        # 13d) 关账与解除并发：最终恰为 released，且解除后可追补
        play(c, "CC4", "W5", 100, when="2026-08-10T20:00:00+08:00")
        AUG = {"start": "2026-08-01T00:00:00+08:00",
               "end": "2026-09-01T00:00:00+08:00"}
        c.post("/statements/issue", json=AUG)
        close(c, "CLS-AUG", AUG)
        results = {}
        barrier4 = threading.Barrier(2)

        def fire_aug(kind):
            barrier4.wait()
            if kind == "release":
                results[kind] = c.post("/period-closings/CLS-AUG/release").status_code
            else:
                results[kind] = c.post("/statements/issue", json=AUG).status_code

        ts = [threading.Thread(target=fire_aug, args=("release",)),
              threading.Thread(target=fire_aug, args=("issue",))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("close/release+issue serial",
              c.get("/period-closings/CLS-AUG").json()["closing_status"]
              == "released", str(results))
        # 解除已生效：出单恢复（无应付变化则返回最后一张单据，200）
        r = c.post("/statements/issue", json=AUG)
        check("issue works after release", r.status_code == 200, r.text)

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL PERIOD CLOSING INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
