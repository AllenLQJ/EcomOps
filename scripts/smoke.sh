#!/usr/bin/env bash
# 联合冒烟（需已 start_with_pilot.sh）：Pilot 下单→发货→吐槽(聊天候选)→确认→真实 EcomOps 三轨审核挂起→主管批准→出箱回调→退款→校验；再测 KB 与对练。
set -euo pipefail
cd "$(dirname "$0")/.."
P=http://127.0.0.1:8085; O=http://127.0.0.1:8000; TOK=dev-internal-token
j() { python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))" "$1"; }
U=SMK_$(date +%s)
PJ=$(curl -sf -XPOST $P/api/v1/dev/token -H 'content-type: application/json' -d "{\"user_id\":\"$U\"}" | j "d['token']")
PA=(-H "Authorization: Bearer $PJ" -H 'content-type: application/json')
Q=$(curl -sf -XPOST $P/api/v1/quotes "${PA[@]}" -d '{"items":[{"sku_id":"SKU_HEATER_003","qty":1}]}'); QID=$(echo "$Q"|j "d['quote_id']"); PAY=$(echo "$Q"|j "d['total_payable_cents']")
OD=$(curl -sf -XPOST $P/api/v1/orders/create "${PA[@]}" -d "{\"idempotency_key\":\"$U\",\"quote_id\":\"$QID\"}")
OID=$(echo "$OD"|j "d['order_id']"); ITEM=$(echo "$OD"|j "d['items'][0]['order_item_id']")
curl -sf -XPOST $P/api/v1/orders/$OID/pay "${PA[@]}" >/dev/null
curl -sf -XPOST $P/internal/v1/orders/$OID/fulfill -H "X-Internal-Token: $TOK" >/dev/null
CL=$(curl -sf -XPOST $P/api/v1/aftersales/confirm "${PA[@]}" -d "{\"order_id\":\"$OID\",\"order_item_id\":\"$ITEM\",\"buyer_reason_text\":\"热水器打不着火还漏水\",\"evidence_urls\":[\"https://storage.example.invalid/e1.jpg\"]}")
CLM=$(echo "$CL"|j "d['claim_no']"); echo "claim $CLM submitted to real EcomOps"
SJ=$(curl -sf -XPOST $O/api/v1/dev/token -H 'content-type: application/json' -d '{"user_id":"SUP_007","role":"supervisor"}' | j "d['token']")
SA=(-H "Authorization: Bearer $SJ" -H 'content-type: application/json')
ST=$(curl -sf $O/api/v1/claims/$CLM "${SA[@]}" | j "d['claim_status']"); echo "ops status: $ST"; [ "$ST" = PENDING_SUPERVISOR_REVIEW ]
curl -sf $O/api/v1/claims/$CLM "${SA[@]}" | j "[(t['track'],t['passed']) for t in d['tracks']]"
curl -sf -XPOST $O/api/v1/claims/$CLM/confirm "${SA[@]}" -d '{"action":"APPROVE"}' | j "d['claim_status']"
for i in $(seq 1 30); do ST=$(curl -sf $O/api/v1/claims/$CLM "${SA[@]}" | j "d['claim_status']"); [ "$ST" = COMPLETED ] && break; sleep 1; done
echo "final ops status: $ST"; [ "$ST" = COMPLETED ]
RS=$(curl -sf "$P/internal/v1/refunds/status?refund_idempotency_key=REFUND_${CLM}_V1" -H "X-Internal-Token: $TOK"); echo "$RS"
[ "$(echo "$RS"|j "d['refunded_cents']")" = "$PAY" ]
[ "$(curl -sf $P/api/v1/orders/$OID "${PA[@]}" | j "d['order_status']")" = REFUNDED_FULL ]
OJ=$(curl -sf -XPOST $O/api/v1/dev/token -H 'content-type: application/json' -d '{"user_id":"OP1","role":"operator"}' | j "d['token']")
OA=(-H "Authorization: Bearer $OJ" -H 'content-type: application/json')
K=$(curl -sf -XPOST $O/api/v1/kb/ask "${OA[@]}" -d '{"query":"我想了解你们对跨境海淘税费的处理细则"}'); echo "kb: $(echo "$K"|j "(d['query_type'],d['top1_score'],bool(d['enqueued_queue_id']))")"
echo "SMOKE OK"
