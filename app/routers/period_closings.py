from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import closings_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import PeriodCloseIn, PeriodCloseOut

router = APIRouter(tags=["period-closings"])


@router.post("/period-closings")
def close_period(body: PeriodCloseIn, conn=Depends(get_conn)):
    """封存一个已出单据期间（财务关账）。

    - 凭唯一关账号 close_ref 与期间起止时间封存；返回关账时最新单号、
      各权利人累计应付与待处理数量；
    - 无已出单据 409 period_has_no_statement，拒绝且不留记录；
    - 关账后该期间再次出单 409 period_closed（历史单据与归因不变），
      播放更正、协议登记及争议操作照常；试算照常并附实时应付与关账应付差额；
    - 同号同期间重试返回原关账记录（status=duplicate，HTTP 200）；
      同号异期间 409 closing_ref_conflict；同期间重复关账（含不同关账号、
      解除后再关）409 period_already_closed，均不留记录。
    """
    result = svc.close_period(conn, body.close_ref, body.start, body.end)
    # 新关账 201；同号同期间重试 200 且返回原记录
    status_code = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status_code,
        content=PeriodCloseOut(**result).model_dump(mode="json"),
    )


@router.post("/period-closings/{close_ref}/release", response_model=PeriodCloseOut)
def release_closing(close_ref: str, conn=Depends(get_conn)):
    """凭关账号解除封存。

    - closed -> released：解除后可按既有规则出具追补单；
    - 重复解除幂等（status=duplicate，HTTP 200，原关账状态不变）；
    - 未知关账号 404 period_closing_not_found。
    """
    result = svc.release_closing(conn, close_ref)
    return PeriodCloseOut(**result).model_dump(mode="json")


@router.get("/period-closings/{close_ref}", response_model=PeriodCloseOut)
def get_closing(close_ref: str, conn=Depends(get_conn)):
    """查询关账：期间、最新单号、各权利人累计应付、待处理数量与状态。

    未知关账号返回 404 period_closing_not_found。
    """
    result = svc.get_closing(conn, close_ref)
    if result is None:
        raise APIError(
            404,
            "period_closing_not_found",
            f"period closing {close_ref} not found",
        )
    return result
