#!/usr/bin/env bash
set -euo pipefail

OUT=${1:-artifacts/sequence_benchmark/fine/fine.jsonl}
SUGGEST=${2:-artifacts/sequence_benchmark/reports/fine_suggest.json}
mkdir -p "$(dirname "$OUT")"

if [[ ! -f "$SUGGEST" ]]; then
  echo "missing suggest file: $SUGGEST"
  exit 1
fi

ADDRESS=${LECROY_SCOPE_ADDRESS:-localhost}
PROTOCOL=${LECROY_SCOPE_PROTOCOL:-vicp}
TDIV=${SEQ_TDIV:-1e-9}
SAMPLING=${SEQ_SAMPLING_PERIOD:-1e-10}
WAIT_TIMEOUT=${SEQ_WAIT_TIMEOUT:-4}
OPC_TIMEOUT=${SEQ_OPC_TIMEOUT:-2}
DISPLAY=${SEQ_DISPLAY:-OFF}
POSTPROC=${SEQ_POSTPROC:-minimal}
SYNC_MODE=${SEQ_SYNC_MODE:-wait_then_opc}
CHANNELS=${SEQ_CHANNELS:-"1 2 3 4 5 6 7 8"}

python - <<'PY' "$SUGGEST" > /tmp/seq_fine_pairs.txt
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for seg in data.get("segments", []):
    for np in data.get("np", []):
        print(seg, np)
PY

while read -r seg np; do
  python scripts/benchmark_sequence_latency.py \
    --address "$ADDRESS" \
    --protocol "$PROTOCOL" \
    --segments "$seg" \
    --np "$np" \
    --channels $CHANNELS \
    --tdiv "$TDIV" \
    --sampling-period "$SAMPLING" \
    --wait-timeout "$WAIT_TIMEOUT" \
    --opc-timeout "$OPC_TIMEOUT" \
    --sync-mode "$SYNC_MODE" \
    --sn-mode all \
    --display "$DISPLAY" \
    --postproc-profile "$POSTPROC" \
    --out "$OUT"
done < /tmp/seq_fine_pairs.txt

echo "fine sweep complete: $OUT"
