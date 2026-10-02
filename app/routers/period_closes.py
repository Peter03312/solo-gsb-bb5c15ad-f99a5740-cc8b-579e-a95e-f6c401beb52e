from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import closes_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import PeriodCloseIn, PeriodCloseOut

router = APIRouter(tags=["period-closes"])


@router.post("/period-closes")
def close_period(body: PeriodCloseIn, conn=Depends(get_conn)):
    """凭唯一关账号封存一个已出单期间（关账）。

    - 请求体：期间起止（半开区间 [start, end)，须带时区）+ 唯一关账号
      close_ref；关账时冻结"最新单据号、各权利人累计应付与待处理数量"；
    - 无已出单据的期间拒绝关账：409 period_not_billed，不留记录；
    - 同号同期间重试幂等返回原关账记录（status=duplicate，HTTP 200）；
      同号异期间复用 409 close_ref_conflict；
      同期间已有未解除关账（含换新关账号）409 period_already_closed；
      上述拒绝均不留任何记录；
    - 关账后该期间再次出单被拒绝（409 period_closed），历史单据与归因
      保持不变；播放更正、协议登记与争议操作照常进行；
    - 关账、解除与出单按期间串行：关账快照对应最后一张已出单据，
      关账后不漏出单。
    """
    result = svc.close_period(conn, body.close_ref, body.start, body.end)
    # 新关账 201；同号同期间重试 200 且返回原记录
    status_code = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status_code,
        content=PeriodCloseOut(**result).model_dump(mode="json"),
    )


@router.post("/period-closes/{close_ref}/release", response_model=PeriodCloseOut)
def release_period_close(close_ref: str, conn=Depends(get_conn)):
    """凭关账号解除封存（解封）。

    - closed -> released：解除后该期间可按既有规则出具追补单；
      历史单据、归因与关账冻结快照保持不变；
    - 重复解除幂等（status=duplicate，HTTP 200，原记录状态不变）；
    - 未知关账号 404 period_close_not_found，不留记录。
    """
    result = svc.release_period_close(conn, close_ref)
    return PeriodCloseOut(**result).model_dump(mode="json")


@router.get("/period-closes/{close_ref}", response_model=PeriodCloseOut)
def get_period_close(close_ref: str, conn=Depends(get_conn)):
    """按关账号查询关账记录：期间、状态、最新单据号、待处理数量
    与冻结的各权利人累计应付。

    未知关账号返回 404 period_close_not_found。
    """
    result = svc.get_period_close(conn, close_ref)
    if result is None:
        raise APIError(
            404,
            "period_close_not_found",
            f"period close {close_ref} not found",
        )
    return result
