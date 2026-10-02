"""财务期间关账与解除（封存已出单期间）。
财务凭"调用方唯一关账号 close_ref + 期间起止时间"封存一个已出单据期间，
返回关账时最新单号、各权利人累计应付与待处理数量；无已出单据拒绝。

- 关账后该期间再次出单一律拒绝（409 period_closed），历史单据与逐笔归因
  保持冻结；播放更正、协议登记及争议暂缓/解除照常进行；试算（preview）
  照常只读，展示实时应付及其与关账应付的差额。
- 凭关账号解除后可按既有规则出具追补单（追补差额相对关账时的累计应付
  计算）；重复解除幂等（status=duplicate，HTTP 200）。
- 同号同期间重试返回原关账记录（status=duplicate，HTTP 200）；
  异期间复用同号（409 closing_ref_conflict）、同期间重复关账
  （409 period_already_closed，含不同关账号、解除后再关）、无已出单据
  （409 period_has_no_statement）、未知关账号解除/查询
  （404 period_closing_not_found）均拒绝，且不留任何记录。

关账快照：
- latest_batch_id 为关账时该期间最后一张已出单据；
- 各权利人累计应付 = 该期间全部已出单据明细行（初次单与正负追补单）的
  代数和；待处理数量 = 最近一次出具时冻结的待处理清单条数。
  因此快照与"最后一张已出单据"严格对应。

并发协议（关账、解除与出单按期间串行）：
- 关账：关账号事务咨询锁（同号并发请求串行，幂等/冲突判定确定）
  → 期间事务咨询锁（与出单、解除互斥，无死锁，期间锁为末级锁）→ 校验/冻结。
- 解除：关账号事务咨询锁 → 关账行锁 → 期间事务咨询锁 → closed → released。
- 出单（statements_service）：期间事务咨询锁 → 关账状态检查 →
  期间内链根咨询锁 → ……。
  因而"关账先提交则其后出单必被拒、出单先提交则关账必能看到该单"，
  关账快照对应最后一张已出单据，且关账后不漏出单。
"""
from datetime import datetime

from .errors import APIError
from .payments_service import take_ref_advisory_lock
from .statements_service import (
    _get_period_closing,
    _latest_batch,
    _load_pending_snapshot,
    _load_prior_payable,
    canonical_period,
    lock_period,
)

# 关账号事务咨询锁命名空间
_CLOSING_NS = 0x434C5331  # "CLS1"

_CLOSING_COLS = (
    "id, close_ref, period_start, period_end, latest_batch_id, "
    "pending_count, status, closed_at, released_at"
)


def _load_closing_lines(conn, closing_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT holder_code, payable_total
        FROM period_closing_lines
        WHERE closing_id = %s
        ORDER BY holder_code
        """,
        (closing_id,),
    ).fetchall()
    return [
        {"holder_code": r["holder_code"], "payable_total": int(r["payable_total"])}
        for r in rows
    ]


def _payload(conn, row, *, request_status: str) -> dict:
    return {
        "close_ref": row["close_ref"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "latest_batch_id": row["latest_batch_id"],
        "pending_count": row["pending_count"],
        # 请求级状态：created=本次关账；released=本次解除；
        # duplicate=同号同期间重试或重复解除；查询时与关账自身状态一致
        "status": request_status,
        "closing_status": row["status"],
        "closed_at": row["closed_at"],
        "released_at": row["released_at"],
        "payables": _load_closing_lines(conn, row["id"]),
    }


def close_period(conn, close_ref: str, start_in, end_in) -> dict:
    """封存一个已出单据期间。新关账 201，同号同期间重试 200。"""
    start, end, _key = canonical_period(start_in, end_in)
    with conn.transaction():
        # 1) 关账号咨询锁：同号并发请求串行化。
        take_ref_advisory_lock(conn, _CLOSING_NS, close_ref)

        # 2) 期间咨询锁：与同期间的出单、解除完全串行——
        #    锁前已提交的出单对本事务整体可见，本事务提交后到期间锁为止
        #    不可能再有出单（出单在同一锁上检查关账状态）。
        lock_period(conn, start, end)

        # 3) 同号判定：异期间复用 409；同号同期间重试幂等返回原记录。
        existing_ref = conn.execute(
            f"SELECT {_CLOSING_COLS} FROM period_closings WHERE close_ref = %s",
            (close_ref,),
        ).fetchone()
        if existing_ref is not None:
            same_period = (
                existing_ref["period_start"] == start
                and existing_ref["period_end"] == end
            )
            if not same_period:
                raise APIError(
                    409,
                    "closing_ref_conflict",
                    f"close ref {close_ref} already used for a different period",
                    detail={
                        "close_ref": close_ref,
                        "existing_period_start": existing_ref["period_start"].isoformat(),
                        "existing_period_end": existing_ref["period_end"].isoformat(),
                        "request_period_start": start.isoformat(),
                        "request_period_end": end.isoformat(),
                    },
                )
            return _payload(conn, existing_ref, request_status="duplicate")

        # 4) 同期间重复关账（不同关账号；含解除后再关——期间至多一条记录）。
        existing_period = _get_period_closing(conn, start, end)
        if existing_period is not None:
            raise APIError(
                409,
                "period_already_closed",
                f"period [{start.isoformat()}, {end.isoformat()}) already has "
                f"closing {existing_period['close_ref']} "
                f"(status={existing_period['status']}); duplicate close rejected "
                f"and no record written",
                detail={
                    "existing_close_ref": existing_period["close_ref"],
                    "existing_status": existing_period["status"],
                },
            )

        # 5) 无已出单据拒绝（不留任何记录）。
        latest = _latest_batch(conn, start, end)
        if latest is None:
            raise APIError(
                409,
                "period_has_no_statement",
                f"period [{start.isoformat()}, {end.isoformat()}) has no issued "
                f"statement; close at least one statement before closing the period",
            )

        # 6) 冻结快照：累计应付取自全部已出单据明细行（与最后一张单据的
        #    入账状态一致），待处理数量取最近一次出具冻结的待处理清单。
        prior = _load_prior_payable(conn, start, end)
        pending_snapshot = _load_pending_snapshot(conn, start, end)
        payables = [
            {"holder_code": h, "payable_total": int(amt)}
            for h, amt in sorted(prior.items())
        ]

        row = conn.execute(
            """
            INSERT INTO period_closings
                (close_ref, period_start, period_end, latest_batch_id, pending_count)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, closed_at
            """,
            (close_ref, start, end, latest["id"], len(pending_snapshot)),
        ).fetchone()
        if payables:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO period_closing_lines
                        (closing_id, holder_code, payable_total)
                    VALUES (%s, %s, %s)
                    """,
                    [(row["id"], p["holder_code"], p["payable_total"]) for p in payables],
                )

        created = conn.execute(
            f"SELECT {_CLOSING_COLS} FROM period_closings WHERE id = %s",
            (row["id"],),
        ).fetchone()
        return _payload(conn, created, request_status="created")


def release_closing(conn, close_ref: str) -> dict:
    """凭关账号解除封存（幂等：重复解除返回原记录，状态不变）。"""
    with conn.transaction():
        # 1) 关账号咨询锁：同号并发解除串行。
        take_ref_advisory_lock(conn, _CLOSING_NS, close_ref)

        # 2) 锁关账行：与一切状态判定互斥。
        row = conn.execute(
            f"SELECT {_CLOSING_COLS} FROM period_closings "
            "WHERE close_ref = %s FOR UPDATE",
            (close_ref,),
        ).fetchone()
        if row is None:
            raise APIError(
                404,
                "period_closing_not_found",
                f"period closing {close_ref} not found",
                detail={"close_ref": close_ref},
            )

        if row["status"] == "released":
            return _payload(conn, row, request_status="duplicate")

        # 3) 期间咨询锁：与出单串行——解除提交前出单被拒（period_closed），
        #    解除提交后出单可按追补规则出具，不混合前后状态。
        lock_period(conn, row["period_start"], row["period_end"])

        updated = conn.execute(
            """
            UPDATE period_closings
            SET status = 'released', released_at = now()
            WHERE id = %s AND status = 'closed'
            RETURNING id
            """,
            (row["id"],),
        ).fetchone()
        if updated is None:  # 行锁下理论不可达，防御性兜底
            refreshed = conn.execute(
                f"SELECT {_CLOSING_COLS} FROM period_closings WHERE id = %s",
                (row["id"],),
            ).fetchone()
            if refreshed is not None and refreshed["status"] == "released":
                return _payload(conn, refreshed, request_status="duplicate")
            raise APIError(
                409, "period_closing_not_closed",
                f"period closing {close_ref} is not closed; release rejected",
            )

        released = conn.execute(
            f"SELECT {_CLOSING_COLS} FROM period_closings WHERE id = %s",
            (row["id"],),
        ).fetchone()
        return _payload(conn, released, request_status="released")


def get_closing(conn, close_ref: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_CLOSING_COLS} FROM period_closings WHERE close_ref = %s",
        (close_ref,),
    ).fetchone()
    if row is None:
        return None
    return _payload(conn, row, request_status=row["status"])


def build_preview_closing(
    conn, start: datetime, end: datetime, current_payable: dict[str, int],
    current_pending: list[dict],
) -> dict | None:
    """试算侧调用：期间处于"已关账"时，返回实时应付与关账应付的对照。

    未关账或已解除时返回 None（普通期间试算保持原约定）。
    current_payable/current_pending 为本次试算重算结果（调用瞬间已提交状态）。
    """
    row = _get_period_closing(conn, start, end)
    if row is None or row["status"] != "closed":
        return None
    closed_lines = _load_closing_lines(conn, row["id"])
    closed_map = {l["holder_code"]: l["payable_total"] for l in closed_lines}
    holders = sorted(set(closed_map) | set(current_payable))
    return {
        "close_ref": row["close_ref"],
        "closing_status": row["status"],
        "closed_at": row["closed_at"],
        "latest_batch_id": row["latest_batch_id"],
        # 关账时冻结的待处理数量
        "pending_count_closed": row["pending_count"],
        # 试算瞬间的实时待处理数量
        "pending_count_current": len(current_pending),
        # 各权利人实时应付、关账应付与差额（实时 − 关账；可正可负可为 0）。
        # 关账期间该差额无法出具追补，须先解除关账。
        "payables": [
            {
                "holder_code": h,
                "current_payable": int(current_payable.get(h, 0)),
                "closed_payable": int(closed_map.get(h, 0)),
                "difference": int(current_payable.get(h, 0) - closed_map.get(h, 0)),
            }
            for h in holders
        ],
    }
