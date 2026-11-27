#!/bin/bash
# Ablation ladder on a corpus file: raw -> split -> split+dedup -> emz (all with emcm)
set -e
R=$(cd "$(dirname "$0")/.." && pwd)
C=$1; O=$2; mkdir -p $O
for mode in split dedup; do
  python3 $R/scripts/ablation.py $C $O/$mode $mode
  tot=0
  for f in $O/$mode/*.raw; do $R/emcm c $f $f.emc; tot=$((tot + $(stat -c %s $f.emc))); done
  echo "$mode $tot"
done
