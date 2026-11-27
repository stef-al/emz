#!/bin/bash
# compress each emz stream with paq8px -8 and sum (ablation: structure + strongest CM)
d=$1; tot=0
for f in $d/*.raw; do
  s=$(stat -c %s $f); [ $s -eq 0 ] && continue
  ${PAQ8PX:-paq8px} -8 $f $f.paq8px > /dev/null 2>&1
  c=$(stat -c %s $f.paq8px); tot=$((tot + c)); echo "$(basename $f) $s $c"
done
echo "TOTAL $tot"
