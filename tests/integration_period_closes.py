"""期间关账（封存/解除）端到端集成测试
（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_period_closes.py
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
    import psycopg
    from fastapi.testclient import TestClient
    from app import db
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    FEB = {"start": "2026-02-01T00:00:00+08:00",
           "end": "2026-03-01T00:00:00+08:00"}
    MAR = {"start": "2026-03-01T00:00:00+08:00",
           "end": "2026-04-01T00:00:00+08:00"}
    APR = {"start": "2026-04-01T00:00:00+08:00",
           "end": "2026-05-01T00:00:00+08:00"}
    MAY = {"start": "2026-05-01T00:00:00+08:00",
           "end": "2026-06-01T00:00:00+08:00"}
    JUN = {"start": "2026-06-01T00:00:00+08:00",
           "end": "2026-07-01T00:00:00+08:00"}

    def close_body(ref, period):
        return {"close_ref": ref, **period}

    def payable_map(body):
        return {p["holder_code"]: p["amount"] for p in body["payable"]}

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        c.post("/works", json={"code": "W1", "title": "作品一"})
        c.post("/works", json={"code": "W2", "title": "作品二（无协议）"})
        r = c.post("/agreements", json={
            "work_code": "W1",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [
                {"holder_code": "A", "percent": "62.5"},
                {"holder_code": "B", "percent": "30"},
                {"holder_code": "C", "percent": "7.5"},
            ]})
        assert r.status_code == 201, r.text

        # =================================================================
        # 一、无已出单据拒绝关账，且不留记录
        # =================================================================
        r = c.post("/period-closes", json=close_body("CLOSE-EMPTY", FEB))
        check("close without billed statements rejected",
              r.status_code == 409 and r.json()["error"] == "period_not_billed",
              r.text)
        r = c.get("/period-closes/CLOSE-EMPTY")
        check("rejected close leaves no record",
              r.status_code == 404 and r.json()["error"] == "period_close_not_found")

        # 导入播放：P1(100 分, W1) 与 Q1(100 分, W2 无协议 => 待处理)
        for pid, work in (("P1", "W1"), ("Q1", "W2")):
            r = c.post("/plays/import", json={
                "import_id": pid, "work_code": work,
                "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100})
            assert r.status_code == 201, r.text

        # 出具 2 月初次单：100 => A 63 / B 30 / C 7；Q1 待处理
        r = c.post("/statements/issue", json=FEB)
        assert r.status_code == 200, r.text
        feb_initial = r.json()
        check("feb initial issued",
              feb_initial["kind"] == "initial"
              and payable_map(feb_initial) == {"A": 63, "B": 30, "C": 7}
              and len(feb_initial["pending"]) == 1, str(feb_initial))
        feb_batch1 = feb_initial["batch_id"]

        # =================================================================
        # 二、关账：冻结最新单号、各权利人累计应付与待处理数量
        # =================================================================
        r = c.post("/period-closes", json=close_body("CLOSE-FEB-1", FEB))
        feb_close = r.json()
        check("close created 201",
              r.status_code == 201 and feb_close["status"] == "created"
              and feb_close["close_status"] == "closed", r.text)
        check("close snapshot content",
              feb_close["last_batch_id"] == feb_batch1
              and feb_close["pending_count"] == 1
              and payable_map(feb_close) == {"A": 63, "B": 30, "C": 7}
              and feb_close["released_at"] is None, str(feb_close))

        # 同号同期间重试 => 200 duplicate，返回原关账记录
        r = c.post("/period-closes", json=close_body("CLOSE-FEB-1", FEB))
        dup = r.json()
        check("same ref same period retry duplicate",
              r.status_code == 200 and dup["status"] == "duplicate"
              and dup["closed_at"] == feb_close["closed_at"]
              and dup["last_batch_id"] == feb_batch1
              and payable_map(dup) == {"A": 63, "B": 30, "C": 7}, r.text)

        # 同号异期间复用 => 409 close_ref_conflict，原记录不变
        r = c.post("/period-closes", json=close_body("CLOSE-FEB-1", MAR))
        check("same ref different period conflict",
              r.status_code == 409 and r.json()["error"] == "close_ref_conflict",
              r.text)
        r = c.get("/period-closes/CLOSE-FEB-1")
        check("conflict reuse keeps original record",
              r.status_code == 200
              and r.json()["period_start"].startswith("2026-01-31")
              and r.json()["close_status"] == "closed", r.text)

        # 同期间重复关账（换新关账号）=> 409 period_already_closed，不留记录
        r = c.post("/period-closes", json=close_body("CLOSE-FEB-2", FEB))
        check("duplicate close of closed period rejected",
              r.status_code == 409
              and r.json()["error"] == "period_already_closed"
              and r.json()["detail"]["existing_close_ref"] == "CLOSE-FEB-1",
              r.text)
        r = c.get("/period-closes/CLOSE-FEB-2")
        check("rejected duplicate close leaves no record", r.status_code == 404)

        # =================================================================
        # 三、封存期间：出单拒绝；更正/协议/争议照常；试算展示差额
        # =================================================================
        r = c.post("/statements/issue", json=FEB)
        check("issue into closed period rejected",
              r.status_code == 409 and r.json()["error"] == "period_closed"
              and r.json()["detail"]["close_ref"] == "CLOSE-FEB-1", r.text)

        # 播放更正照常（100 -> 80）
        r = c.post("/plays/import", json={
            "import_id": "P1-R1", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 80,
            "corrects_id": "P1"})
        check("correction allowed while closed", r.status_code == 201, r.text)
        # 协议登记照常（W2 后补协议，Q1 将可核算）
        r = c.post("/agreements", json={
            "work_code": "W2",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "100"}]})
        check("agreement registration allowed while closed",
              r.status_code == 201, r.text)
        # 争议操作照常（对 Q1 链登记暂缓并解除）
        r = c.post("/play-disputes", json={
            "play_import_id": "Q1", "dispute_ref": "DSP-Q1",
            "reason": "封存期间争议照常"})
        check("dispute open allowed while closed",
              r.status_code == 201 and r.json()["dispute_status"] == "open",
              r.text)
        r = c.post("/play-disputes/DSP-Q1/release")
        check("dispute release allowed while closed",
              r.status_code == 200 and r.json()["dispute_status"] == "released",
              r.text)

        # 试算：实时应付（P1R1 80 => 50/24/6；Q1 100% A => 100）
        # 与关账应付（63/30/7）的差额逐人展示
        r = c.get("/statements/preview", params=FEB)
        pv = r.json()
        pv_map = {p["holder_code"]: p for p in pv["payable"]}
        check("preview while closed shows realtime vs closed delta",
              pv["period_closed"] is True
              and pv["period_close"]["close_ref"] == "CLOSE-FEB-1"
              and pv["period_close"]["last_batch_id"] == feb_batch1
              and pv["period_close"]["pending_count"] == 1
              and pv_map["A"]["amount"] == 150
              and pv_map["A"]["closed_amount"] == 63
              and pv_map["A"]["closed_delta"] == 87
              and pv_map["B"]["amount"] == 24
              and pv_map["B"]["closed_amount"] == 30
              and pv_map["B"]["closed_delta"] == -6
              and pv_map["C"]["closed_delta"] == -1
              and pv["pending"] == [], str(pv))

        # 历史单据与归因不变
        r = c.get("/statements", params=FEB)
        batches = r.json()
        check("historical statements unchanged while closed",
              len(batches) == 1
              and {l["holder_code"]: l["amount"] for l in batches[0]["lines"]}
              == {"A": 63, "B": 30, "C": 7}, str(batches))
        r = c.get(f"/statements/{feb_batch1}/attributions")
        att = r.json()
        check("attributions unchanged while closed",
              att["traceable"] is True
              and all(t["reconciles"] for t in att["holder_totals"]), str(att))

        # =================================================================
        # 四、解除：幂等；解除后按既有规则出具追补
        # =================================================================
        r = c.post("/period-closes/NO-SUCH-CLOSE/release")
        check("release unknown ref 404",
              r.status_code == 404
              and r.json()["error"] == "period_close_not_found", r.text)

        r = c.post("/period-closes/CLOSE-FEB-1/release")
        rel = r.json()
        check("release ok",
              r.status_code == 200 and rel["status"] == "released"
              and rel["close_status"] == "released"
              and rel["released_at"] is not None
              and payable_map(rel) == {"A": 63, "B": 30, "C": 7}, r.text)
        r = c.post("/period-closes/CLOSE-FEB-1/release")
        check("repeated release idempotent",
              r.status_code == 200 and r.json()["status"] == "duplicate"
              and r.json()["released_at"] == rel["released_at"], r.text)

        # 解除后试算不再附关账口径
        r = c.get("/statements/preview", params=FEB)
        pv = r.json()
        check("preview after release back to normal",
              pv["period_closed"] is False and pv["period_close"] is None
              and all(p["closed_amount"] is None and p["closed_delta"] is None
                      for p in pv["payable"]), str(pv))

        # 解除后按既有规则出具追补：实时 150/24/6 - 已入账 63/30/7 = +87/-6/-1
        r = c.post("/statements/issue", json=FEB)
        sup = r.json()
        check("supplement issued after release",
              r.status_code == 200 and sup["kind"] == "supplement"
              and sup["supersedes_batch_id"] == feb_batch1
              and {l["holder_code"]: l["delta"] for l in sup["payable"]}
              == {"A": 87, "B": -6, "C": -1}, str(sup))

        # 解除后可用新关账号再次关账（快照锚定追补单）
        r = c.post("/period-closes", json=close_body("CLOSE-FEB-3", FEB))
        reclose = r.json()
        check("re-close with new ref after release",
              r.status_code == 201 and reclose["status"] == "created"
              and reclose["last_batch_id"] == sup["batch_id"]
              and payable_map(reclose) == {"A": 150, "B": 24, "C": 6}
              and reclose["pending_count"] == 0, r.text)
        # 旧关账号同期间重试仍返回原（已解除）记录，不复活
        r = c.post("/period-closes", json=close_body("CLOSE-FEB-1", FEB))
        check("old ref retry returns original released record",
              r.status_code == 200 and r.json()["status"] == "duplicate"
              and r.json()["close_status"] == "released"
              and r.json()["last_batch_id"] == feb_batch1, r.text)
        # 再次出单仍被拒（CLOSE-FEB-3 封存中）
        r = c.post("/statements/issue", json=FEB)
        check("issue rejected again after re-close",
              r.status_code == 409 and r.json()["error"] == "period_closed",
              r.text)
        # 解除 CLOSE-FEB-3，避免影响后续期间无关校验
        r = c.post("/period-closes/CLOSE-FEB-3/release")
        assert r.status_code == 200, r.text

        # =================================================================
        # 五、并发：同号并发关账恰一笔；关账与出单串行（快照对应最后一张单据，
        #     关账后不漏出单）；解除与出单串行
        # =================================================================
        # 3 月：P2(90, W1) => 初次单 A 56/B 27/C 7（余数按小数部分降序补给 C）
        r = c.post("/plays/import", json={
            "import_id": "P2", "work_code": "W1",
            "occurred_at": "2026-03-05T20:00:00+08:00", "amount": 90})
        assert r.status_code == 201, r.text
        r = c.post("/statements/issue", json=MAR)
        mar_batch1 = r.json()["batch_id"]

        # 1) 同号 8 并发关账 => 恰 1 笔 created，其余 duplicate，同一记录
        results = []

        def fire_close():
            rr = c.post("/period-closes", json=close_body("CLOSE-MAR", MAR))
            results.append((rr.status_code, rr.json()))

        ts = [threading.Thread(target=fire_close) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        created = [x for s, x in results if x["status"] == "created"]
        dup = [x for s, x in results if x["status"] == "duplicate"]
        check("concurrent same-ref close serialized",
              len(created) == 1 and len(dup) == 7
              and len({x["closed_at"] for _, x in results}) == 1
              and all(x["last_batch_id"] == mar_batch1 for _, x in results),
              str(results))
        r = c.post("/period-closes/CLOSE-MAR/release")
        assert r.status_code == 200, r.text

        # 2) 关账 vs 出单并发：更正 P2 -> P2-R1(60)，1 路关账 + 4 路出单并发。
        #    出单要么先于关账成功（追补入账），要么被 409 拒绝；
        #    关账快照的 last_batch_id 必为当时最后一张已出单据，
        #    关账提交后不再有出单成功（不漏出单）。
        r = c.post("/plays/import", json={
            "import_id": "P2-R1", "work_code": "W1",
            "occurred_at": "2026-03-05T20:00:00+08:00", "amount": 60,
            "corrects_id": "P2"})
        assert r.status_code == 201, r.text

        outcomes = {"issued": [], "rejected": 0, "close": None, "errors": []}
        barrier = threading.Barrier(5)

        def fire_issue():
            barrier.wait()
            try:
                rr = c.post("/statements/issue", json=MAR)
                if rr.status_code == 200:
                    outcomes["issued"].append(rr.json()["batch_id"])
                elif rr.status_code == 409 \
                        and rr.json()["error"] == "period_closed":
                    outcomes["rejected"] += 1
                else:
                    outcomes["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        def fire_close2():
            barrier.wait()
            try:
                rr = c.post("/period-closes",
                            json=close_body("CLOSE-MAR-2", MAR))
                if rr.status_code == 201:
                    outcomes["close"] = rr.json()
                else:
                    outcomes["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        threads = [threading.Thread(target=fire_issue) for _ in range(4)]
        threads.append(threading.Thread(target=fire_close2))
        [t.start() for t in threads]
        [t.join() for t in threads]

        batches = c.get("/statements", params=MAR).json()
        max_batch = max(b["batch_id"] for b in batches)
        close2 = outcomes["close"]
        check("close vs issue serialized, snapshot anchors last billed",
              not outcomes["errors"] and close2 is not None
              and close2["last_batch_id"] == max_batch
              and all(bid <= close2["last_batch_id"]
                      for bid in outcomes["issued"]),
              f"{outcomes} max={max_batch}")
        # 关账后出单一律拒绝（不漏出单）
        r = c.post("/statements/issue", json=MAR)
        check("no issue slips through after close",
              r.status_code == 409 and r.json()["error"] == "period_closed",
              r.text)

        # 3) 解除 vs 出单并发：解除提交前出单 409，提交后出单成功；
        #    最终解除生效且能出追补单
        outcomes2 = {"issued": 0, "rejected": 0, "released": 0, "errors": []}
        barrier2 = threading.Barrier(5)

        def fire_issue2():
            barrier2.wait()
            try:
                rr = c.post("/statements/issue", json=MAR)
                if rr.status_code == 200:
                    outcomes2["issued"] += 1
                elif rr.status_code == 409 \
                        and rr.json()["error"] == "period_closed":
                    outcomes2["rejected"] += 1
                else:
                    outcomes2["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes2["errors"].append(repr(e))

        def fire_release():
            barrier2.wait()
            try:
                rr = c.post("/period-closes/CLOSE-MAR-2/release")
                if rr.status_code == 200:
                    outcomes2["released"] += 1
                else:
                    outcomes2["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes2["errors"].append(repr(e))

        threads = [threading.Thread(target=fire_issue2) for _ in range(4)]
        threads.append(threading.Thread(target=fire_release))
        [t.start() for t in threads]
        [t.join() for t in threads]
        check("release vs issue serialized",
              not outcomes2["errors"] and outcomes2["released"] == 1,
              str(outcomes2))
        r = c.get("/period-closes/CLOSE-MAR-2")
        check("close released after concurrent window",
              r.json()["close_status"] == "released", r.text)
        r = c.post("/statements/issue", json=MAR)
        check("issue works after release",
              r.status_code == 200, r.text)

        # =================================================================
        # 六、普通期间（无关账）出单/查询按现有约定执行
        # =================================================================
        r = c.post("/plays/import", json={
            "import_id": "P3", "work_code": "W1",
            "occurred_at": "2026-06-15T20:00:00+08:00", "amount": 40})
        assert r.status_code == 201, r.text
        r = c.post("/statements/issue", json=JUN)
        check("normal period issue unaffected",
              r.status_code == 200 and r.json()["kind"] == "initial", r.text)
        r = c.get("/statements/preview", params=JUN)
        pv = r.json()
        check("normal period preview has no close fields",
              pv["period_closed"] is False and pv["period_close"] is None
              and all(p["closed_amount"] is None for p in pv["payable"]),
              str(pv))

        # =================================================================
        # 七、数据库层不可变：关账头/冻结明细拒绝 UPDATE/DELETE；
        #     唯一合法变更 closed -> released 已由 HTTP 路径覆盖
        # =================================================================
        assert db.pool is not None
        with db.pool.connection() as raw:
            blocked = []
            for stmt in (
                "UPDATE period_close_lines SET payable_total = 0 "
                "WHERE holder_code = 'A'",
                "DELETE FROM period_close_lines WHERE holder_code = 'A'",
                "UPDATE period_closes SET pending_count = 0 "
                "WHERE close_ref = 'CLOSE-FEB-1'",
                "UPDATE period_closes SET status = 'closed' "
                "WHERE close_ref = 'CLOSE-FEB-1'",
                "DELETE FROM period_closes WHERE close_ref = 'CLOSE-FEB-1'",
            ):
                try:
                    raw.execute(stmt)
                    blocked.append(False)
                except psycopg.Error:
                    raw.rollback()
                    blocked.append(True)
        check("close header/lines reject update/delete",
              blocked == [True] * 5, str(blocked))

        # 关账记录仍可按关账号查询（冻结值不随后续流水变化）
        r = c.get("/period-closes/CLOSE-FEB-1")
        got = r.json()
        check("get close returns frozen snapshot",
              r.status_code == 200 and got["close_status"] == "released"
              and got["last_batch_id"] == feb_batch1
              and got["pending_count"] == 1
              and payable_map(got) == {"A": 63, "B": 30, "C": 7}, r.text)

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL PERIOD CLOSE INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
