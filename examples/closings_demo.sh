#!/usr/bin/env bash
# 财务期间关账/解除端到端调用示例。
# 前置：服务已启动（docker compose up -d，或本地 uvicorn）。
# 用法: BASE=http://localhost:8000 bash examples/closings_demo.sh
set -u
BASE="${BASE:-http://localhost:8000}"
C="curl -sS"

say() { printf '\n=== %s ===\n' "$1"; }

say "健康检查"
$C "$BASE/health"; echo

PERIOD_START="2026-02-01T00:00:00+08:00"
PERIOD_END="2026-03-01T00:00:00+08:00"
PERIOD="{\"start\":\"$PERIOD_START\",\"end\":\"$PERIOD_END\"}"
PV="$BASE/statements/preview?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00"

say "1) 登记作品、协议（RS-A 70% / RS-B 30%）与播放 PLAY-C-1（100 分）"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d '{"code":"W-CLOSE-001","title":"关账演示录音"}'; echo
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-CLOSE-001",
  "valid_from":"2026-01-01T00:00:00+08:00",
  "valid_to":"2027-01-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-A","percent":"70"},{"holder_code":"RS-B","percent":"30"}]
}'; echo
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-C-1","work_code":"W-CLOSE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":100}'; echo

say "2) 无已出单据关账 => 409 period_has_no_statement（不留记录）"
$C -i -X POST "$BASE/period-closings" -H 'Content-Type: application/json' \
  -d "{\"close_ref\":\"CLOSE-202602-EMPTY\",\"start\":\"$PERIOD_START\",\"end\":\"$PERIOD_END\"}" \
  | grep -E '^HTTP|period_has_no_statement'

say "3) 出具 2026 年 2 月单据（100 分 => 70/30）"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' -d "$PERIOD" \
  | python3 -m json.tool

say "4) 凭唯一关账号关账（201）：返回最新单号、各权利人累计应付、待处理数量"
$C -X POST "$BASE/period-closings" -H 'Content-Type: application/json' \
  -d "{\"close_ref\":\"CLOSE-202602\",\"start\":\"$PERIOD_START\",\"end\":\"$PERIOD_END\"}" \
  | python3 -m json.tool

say "5) 同号同期间重试 => 200 status=duplicate，返回原关账记录"
$C -i -X POST "$BASE/period-closings" -H 'Content-Type: application/json' \
  -d "{\"close_ref\":\"CLOSE-202602\",\"start\":\"$PERIOD_START\",\"end\":\"$PERIOD_END\"}" \
  | grep -E '^HTTP'
$C -X POST "$BASE/period-closings" -H 'Content-Type: application/json' \
  -d "{\"close_ref\":\"CLOSE-202602\",\"start\":\"$PERIOD_START\",\"end\":\"$PERIOD_END\"}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["closing_status"], d["latest_batch_id"])'

say "6) 拒绝：同号异期间 409 closing_ref_conflict；同期间换号重复关账 409 period_already_closed"
$C -i -X POST "$BASE/period-closings" -H 'Content-Type: application/json' \
  -d '{"close_ref":"CLOSE-202602","start":"2026-03-01T00:00:00+08:00","end":"2026-04-01T00:00:00+08:00"}' \
  | grep -E '^HTTP|closing_ref_conflict'
$C -i -X POST "$BASE/period-closings" -H 'Content-Type: application/json' \
  -d "{\"close_ref\":\"CLOSE-202602-OTHER\",\"start\":\"$PERIOD_START\",\"end\":\"$PERIOD_END\"}" \
  | grep -E '^HTTP|period_already_closed'
$C -i "$BASE/period-closings/UNKNOWN-REF" | grep -E '^HTTP|period_closing_not_found'

say "7) 关账后该期间再次出单 => 409 period_closed（历史单据与归因不变）"
$C -i -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' -d "$PERIOD" \
  | grep -E '^HTTP|period_closed'

say "8) 播放更正/协议登记/争议操作照常：更正 PLAY-C-1 为 80 分（链尾 PLAY-C-1-R1 => 56/24）"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-C-1-R1","work_code":"W-CLOSE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":80,
       "corrects_id":"PLAY-C-1"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'

say "9) 试算照常：payable 为实时应付 56/24，closing 块给关账应付 70/30 与差额 -14/-6"
$C "$PV" | python3 -m json.tool

say "10) 凭关账号解除（200 released；重复解除幂等 200 duplicate；未知关账号 404）"
$C -X POST "$BASE/period-closings/CLOSE-202602/release" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["closing_status"])'
$C -X POST "$BASE/period-closings/CLOSE-202602/release" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
$C -i -X POST "$BASE/period-closings/UNKNOWN-REF/release" | grep -E '^HTTP'

say "11) 解除后试算不再有 closing 块；再出单按既有规则出具追补（差额 -14/-6）"
$C "$PV" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("closing block:", "closing" in d)'
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' -d "$PERIOD" \
  | python3 -m json.tool

say "完成"
