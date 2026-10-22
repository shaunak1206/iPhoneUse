#!/bin/zsh
# Waits for the phone to be put down: presses Mirroring's own "Connect"
# button every ~45 s while it says "iPhone in Use", prints one line per state
# change, and exits once mirroring is live.
cd "$(dirname "$0")/.."
prev=""; n=0
while true; do
  out=$(./bin/mirrorctl state 2>/dev/null)
  cur=$(print -r -- "$out" | /usr/bin/python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('state-error'); sys.exit()
conn = bool(d.get('phone_window')) and d.get('view_menu',{}).get('Home Screen',False)
msg = ' / '.join(t for a in d.get('ax',[]) for t in a.get('texts',[])[:1])
print(f\"connected={conn} msg={msg[:60]}\")")
  [[ "$cur" != "$prev" ]] && { echo "$cur"; prev="$cur"; }
  [[ "$cur" == connected=True* ]] && { echo "READY"; exit 0; }
  if [[ "$cur" == *"in Use"* || "$cur" == *"Connect"* ]]; then
    n=$((n+1)); (( n % 15 == 0 )) && ./bin/mirrorctl button Connect >/dev/null 2>&1
  fi
  sleep 3
done
