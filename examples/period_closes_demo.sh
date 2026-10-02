#!/usr/bin/env bash
# 期间关账（封存/解除）端到端调用示例。
# 前置：服务已启动（docker compose up -d，或本地 uvicorn）。
# 用法: BASE=http://localhost:8000 bash examples/period_closes_demo.sh
set -u
BASE="${BASE:-http://localhost:8000}"
C="curl -sS"

say() { printf '\n=== %s ===\n' "$1"; }

say "健康检查"
$C "$BASE/health"; echo

say "1) 登记作品、协议（RS-A 62.5% / RS-B 30% / RS-C 7.5%）与两条播放（一条缺协议）"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d '{"code":"W-CLOSE-001","title":"关账演示录音"}'; echo
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d '{"code":"W-CLOSE-002","title":"关账演示录音（暂无协议）"}'; echo
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-CLOSE-001",
  "valid_from":"2026-01-01T00:00:00+08:00",
  "valid_to":"2027-01-01T00:00:00+08:00",
  "shares":[
    {"holder_code":"RS-A","percent":"62.5"},
    {"holder_code":"RS-B","percent":"30"},
    {"holder_code":"RS-C","percent":"7.5"}
  ]
}'; echo
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-C-1","work_code":"W-CLOSE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":100}'; echo
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-C-2","work_code":"W-CLOSE-002",
       "occurred_at":"2026-02-11T20:00:00+08:00","amount":50}'; echo

PERIOD='{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}'

say "2) 出具 2026 年 2 月初次单（100 分 => 63/30/7；PLAY-C-2 缺协议进待处理）"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' -d "$PERIOD" \
  | python3 -m json.tool

say "3) 关账：凭期间起止 + 唯一关账号 CLOSE-202602 封存（201）"
$C -X POST "$BASE/period-closes" -H 'Content-Type: application/json' -d '{
  "close_ref":"CLOSE-202602",
  "start":"2026-02-01T00:00:00+08:00",
  "end":"2026-03-01T00:00:00+08:00"
}' | python3 -m json.tool
# => 冻结：last_batch_id（最新单号）、各权利人累计应付 payable[]、pending_count=1

say "4) 幂等与拒绝：同号同期间重试 200 duplicate；同号异期间 409；同期间重复关账 409"
$C -i -X POST "$BASE/period-closes" -H 'Content-Type: application/json' -d '{
  "close_ref":"CLOSE-202602",
  "start":"2026-02-01T00:00:00+08:00",
  "end":"2026-03-01T00:00:00+08:00"
}' | grep -E '^HTTP'
$C -i -X POST "$BASE/period-closes" -H 'Content-Type: application/json' -d '{
  "close_ref":"CLOSE-202602",
  "start":"2026-03-01T00:00:00+08:00",
  "end":"2026-04-01T00:00:00+08:00"
}' | grep -E '^HTTP'
$C -i -X POST "$BASE/period-closes" -H 'Content-Type: application/json' -d '{
  "close_ref":"CLOSE-202602-AGAIN",
  "start":"2026-02-01T00:00:00+08:00",
  "end":"2026-03-01T00:00:00+08:00"
}' | grep -E '^HTTP'

say "5) 封存期间再次出单 => 409 period_closed；历史单据与归因不变"
$C -i -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' -d "$PERIOD" \
  | grep -E '^HTTP'
$C "$BASE/statements?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00" \
  | python3 -m json.tool

say "6) 封存期间播放更正/协议登记/争议操作照常：更正 100->80、后补 W-CLOSE-002 协议"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-C-1-R1","work_code":"W-CLOSE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":80,
       "corrects_id":"PLAY-C-1"}'; echo
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-CLOSE-002",
  "valid_from":"2026-01-01T00:00:00+08:00",
  "valid_to":"2027-01-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-A","percent":"100"}]
}'; echo

say "7) 封存期间试算：实时应付 + 与关账应付的差额（closed_amount/closed_delta）"
$C "$BASE/statements/preview?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00" \
  | python3 -m json.tool
# => period_closed=true，period_close 回带关账要点；
#    RS-A 实时 50+50=100 vs 关账 63 => closed_delta=+37

say "8) 凭关账号解除（200 released；重复解除幂等 duplicate；未知关账号 404）"
$C -X POST "$BASE/period-closes/CLOSE-202602/release" | python3 -m json.tool
$C -i -X POST "$BASE/period-closes/CLOSE-202602/release" | grep -E '^HTTP'
$C -i -X POST "$BASE/period-closes/NO-SUCH-CLOSE/release" | grep -E '^HTTP'

say "9) 解除后按既有规则出具追补单（差额可正可负）"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' -d "$PERIOD" \
  | python3 -m json.tool

say "10) 按关账号查询关账记录（冻结快照不随后续流水变化）"
$C "$BASE/period-closes/CLOSE-202602" | python3 -m json.tool
