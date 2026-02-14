#!/usr/bin/env bash
set -euo pipefail

OUT=${1:-artifacts/sequence_benchmark/coarse/coarse.jsonl}
mkdir -p "$(dirname "$OUT")"

ADDRESS=${LECROY_SCOPE_ADDRESS:-localhost}
PROTOCOL=${LECROY_SCOPE_PROTOCOL:-vicp}
TDIV=${SEQ_TDIV:-1e-9}
SAMPLING=${SEQ_SAMPLING_PERIOD:-1e-10}
WAIT_TIMEOUT=${SEQ_WAIT_TIMEOUT:-4}
OPC_TIMEOUT=${SEQ_OPC_TIMEOUT:-2}

SEGMENTS_LIST=${SEQ_SEGMENTS_LIST:-"10 30 100 300 1000"}
NP_LIST=${SEQ_NP_LIST:-"1000 10000 100000"}
CHANNELS_LIST=${SEQ_CHANNELS_LIST:-"1|1 2"}
POSTPROC_LIST=${SEQ_POSTPROC_LIST:-"minimal default"}
DISPLAY_LIST=${SEQ_DISPLAY_LIST:-"ON OFF"}
SYNC_LIST=${SEQ_SYNC_LIST:-"wait_only wait_then_opc"}

for seg in $SEGMENTS_LIST; do
  for np in $NP_LIST; do
    IFS='|' read -r -a ch_sets <<< "$CHANNELS_LIST"
    for chs in "${ch_sets[@]}"; do
      for postproc in $POSTPROC_LIST; do
        for display in $DISPLAY_LIST; do
          for sync_mode in $SYNC_LIST; do
            python scripts/benchmark_sequence_latency.py \
              --address "$ADDRESS" \
              --protocol "$PROTOCOL" \
              --segments "$seg" \
              --np "$np" \
              --channels $chs \
              --tdiv "$TDIV" \
              --sampling-period "$SAMPLING" \
              --wait-timeout "$WAIT_TIMEOUT" \
              --opc-timeout "$OPC_TIMEOUT" \
              --sync-mode "$sync_mode" \
              --sn-mode all \
              --display "$display" \
              --postproc-profile "$postproc" \
              --out "$OUT"
          done
        done
      done
    done
  done
done

echo "coarse sweep complete: $OUT"
