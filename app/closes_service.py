"""期间关账（财务封存已出单期间）与解除。

财务凭"期间起止时间 + 调用方唯一关账号 close_ref"封存一个已出单期间：
关账时冻结"最新单据号 last_batch_id、各权利人累计应付（period_close_lines）、
待处理数量 pending_count"，此后该期间在解除前拒绝再次出单；
历史单据与逐笔归因保持冻结不变，播放更正、协议登记与争议操作照常进行，
试算（preview）继续按实时数据重算并展示与关账应付的差额。
凭关账号解除后，该期间可按既有规则出具追补单；重复解除幂等。

语义约定：
- 无已出单据的期间拒绝关账（409 period_not_billed），不留任何记录；
- 同号同期间重试：返回原关账记录（status=duplicate，HTTP 200，
  已解除的关账同样按原记录返回，不复活）；
- 同号异期间复用：409 close_ref_conflict，不留记录；
- 同期间已有未解除关账（即使换新关账号）：409 period_already_closed，
  拒绝且不留记录；解除后可用新关账号再次关账；
- 未知关账号解除/查询：404 period_close_not_found；
- 关账头与冻结明细一经写入不可修改/删除（触发器强制），
  唯一允许的变更是 closed -> released。

并发协议（与出单统一加锁次序，无死锁）：
- 关账/解除先取"关账号事务咨询锁"（同号并发请求串行，幂等/冲突判定确定），
  再取"期间事务咨询锁"（与出单 issue_statement 是同一把锁）；
- 关账在期间锁内读取该期间最后一张已出单据并冻结快照：出单与关账互斥，
  快照必然对应封存瞬间的最后一张单据；
- 关账提交后，任何出单请求在同一期间锁后看到 closed 记录即拒绝，
  关账后不漏出单；解除在期间锁内翻状态，解除提交后出单恢复。
"""
from datetime import datetime

from .errors import APIError
from .payments_service import take_ref_advisory_lock
from .statements_service import (
    canonical_period,
    load_period_payable,
    lock_period,
)

# 关账号事务咨询锁命名空间（高 32 位）
_CLOSE_REF_NS = 0x434C5331  # "CLS1"

_HEADER_COLS = (
    "id, close_ref, period_start, period_end, status, last_batch_id, "
    "pending_count, closed_at, created_at, released_at"
)


def _load_by_ref(conn, close_ref: str):
    return conn.execute(
        f"SELECT {_HEADER_COLS} FROM period_closes WHERE close_ref = %s",
        (close_ref,),
    ).fetchone()


def _load_lines(conn, close_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT holder_code, payable_total
        FROM period_close_lines
        WHERE close_id = %s
        ORDER BY holder_code
        """,
        (close_id,),
    ).fetchall()
    return [
        {"holder_code": r["holder_code"], "amount": int(r["payable_total"])}
        for r in rows
    ]


def _payload(conn, row: dict, *, status: str) -> dict:
    return {
        "close_ref": row["close_ref"],
        # 请求级状态：created=本次关账；released=本次解除；
        # duplicate=同号同期间重试或重复解除，返回原记录
        "status": status,
        # 关账记录自身状态：closed=封存中；released=已解除
        "close_status": row["status"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        # 关账时该期间最新一张已出单据号（快照锚点）
        "last_batch_id": row["last_batch_id"],
        # 关账时该期间待处理播放数量
        "pending_count": row["pending_count"],
        # 各权利人累计应付（关账时该期间已出具单据的正负累计，整数分）
        "payable": _load_lines(conn, row["id"]),
        "closed_at": row["closed_at"],
        "released_at": row["released_at"],
    }


def close_period(conn, close_ref: str, start_in: datetime, end_in: datetime) -> dict:
    """凭唯一关账号封存一个已出单期间。

    返回 (payload)：新关账 status=created（HTTP 201）；
    同号同期间重试 status=duplicate（HTTP 200，返回原记录）。
    各类拒绝均抛 APIError 且不留任何记录。
    """
    start, end, key = canonical_period(start_in, end_in)
    with conn.transaction():
        # 1) 关账号咨询锁：同号并发请求串行化，恰一笔创建，其余幂等/冲突。
        take_ref_advisory_lock(conn, _CLOSE_REF_NS, close_ref)

        existing = _load_by_ref(conn, close_ref)
        if existing is not None:
            # 同号异期间复用：拒绝，不留记录（关账号全局绑定首次期间）
            if (
                existing["period_start"] != start
                or existing["period_end"] != end
            ):
                raise APIError(
                    409,
                    "close_ref_conflict",
                    f"close ref {close_ref} already used for a different period; "
                    "request rejected",
                    detail={
                        "close_ref": close_ref,
                        "existing_period_start": existing["period_start"].isoformat(),
                        "existing_period_end": existing["period_end"].isoformat(),
                        "request_period_start": start.isoformat(),
                        "request_period_end": end.isoformat(),
                    },
                )
            # 同号同期间重试：无论封存中还是已解除，都返回原关账记录
            return _payload(conn, existing, status="duplicate")

        # 2) 期间咨询锁：与出单、其他关账/解除按期间串行——
        #    持锁后读到的"最新单据"即封存瞬间的最后一张已出单据。
        lock_period(conn, key)

        active = conn.execute(
            """
            SELECT close_ref FROM period_closes
            WHERE period_start = %s AND period_end = %s AND status = 'closed'
            """,
            (start, end),
        ).fetchone()
        if active is not None:
            raise APIError(
                409,
                "period_already_closed",
                f"period [{start.isoformat()}, {end.isoformat()}) is already "
                f"sealed by period close {active['close_ref']}; "
                "duplicate close rejected",
                detail={
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "existing_close_ref": active["close_ref"],
                },
            )

        latest = conn.execute(
            """
            SELECT id FROM statement_batches
            WHERE period_start = %s AND period_end = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (start, end),
        ).fetchone()
        if latest is None:
            raise APIError(
                409,
                "period_not_billed",
                f"period [{start.isoformat()}, {end.isoformat()}) has no issued "
                "statement; close rejected",
                detail={
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                },
            )

        # 冻结口径：各权利人累计应付 = 该期间全部已出具单据明细的正负累计；
        # 待处理数量 = 最后一次出单刷新的待处理快照行数。
        payable = load_period_payable(conn, start, end)
        pending_count = conn.execute(
            """
            SELECT COUNT(*) AS n FROM statement_pending
            WHERE period_start = %s AND period_end = %s
            """,
            (start, end),
        ).fetchone()["n"]

        header = conn.execute(
            """
            INSERT INTO period_closes
                (close_ref, period_start, period_end, last_batch_id, pending_count)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, closed_at, created_at
            """,
            (close_ref, start, end, latest["id"], pending_count),
        ).fetchone()
        if payable:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO period_close_lines
                        (close_id, holder_code, payable_total)
                    VALUES (%s, %s, %s)
                    """,
                    [
                        (header["id"], holder, payable[holder])
                        for holder in sorted(payable)
                    ],
                )

        row = {
            "id": header["id"],
            "close_ref": close_ref,
            "period_start": start,
            "period_end": end,
            "status": "closed",
            "last_batch_id": latest["id"],
            "pending_count": pending_count,
            "closed_at": header["closed_at"],
            "created_at": header["created_at"],
            "released_at": None,
        }
        return _payload(conn, row, status="created")


def release_period_close(conn, close_ref: str) -> dict:
    """凭关账号解除封存（幂等：重复解除返回原记录，状态不变）。

    解除后该期间可按既有规则出具追补单；历史单据、归因与关账快照不变。
    未知关账号 404 period_close_not_found，不留记录。
    """
    with conn.transaction():
        # 1) 关账号咨询锁：同号并发解除/关账串行。
        take_ref_advisory_lock(conn, _CLOSE_REF_NS, close_ref)

        row = _load_by_ref(conn, close_ref)
        if row is None:
            raise APIError(
                404,
                "period_close_not_found",
                f"period close {close_ref} not found",
                detail={"close_ref": close_ref},
            )
        if row["status"] == "released":
            # 重复解除幂等：返回原记录，不改动任何状态
            return _payload(conn, row, status="duplicate")

        # 2) 期间咨询锁：与出单串行——解除提交前出单看到 closed（继续拒绝），
        #    解除提交后出单看到 released（按追补规则恢复），不混合前后状态。
        _start, _end, key = canonical_period(row["period_start"], row["period_end"])
        lock_period(conn, key)

        updated = conn.execute(
            """
            UPDATE period_closes
            SET status = 'released', released_at = now()
            WHERE id = %s AND status = 'closed'
            RETURNING id
            """,
            (row["id"],),
        ).fetchone()
        if updated is None:  # 关账号锁下理论不可达，防御性兜底
            refreshed = _load_by_ref(conn, close_ref)
            if refreshed is not None and refreshed["status"] == "released":
                return _payload(conn, refreshed, status="duplicate")
            raise APIError(
                409,
                "period_close_not_closed",
                f"period close {close_ref} is not in closed status; "
                "release rejected",
            )

        released = _load_by_ref(conn, close_ref)
        return _payload(conn, released, status="released")


def get_period_close(conn, close_ref: str) -> dict | None:
    """按关账号查询关账记录（含冻结的各权利人累计应付）；未知返回 None。"""
    row = _load_by_ref(conn, close_ref)
    if row is None:
        return None
    return _payload(conn, row, status=row["status"])
