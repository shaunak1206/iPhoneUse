#!/bin/zsh
# Starts WebDriverAgent on the iPhone and keeps it running for as long as this script runs (phone_connect starts
# it in the background). Builds first if needed, and re-signs when the free 7-day signature has under 2 days left.
# Needs the phone reachable: plugged in, or on the same Wi-Fi once Xcode has paired it ("Connect via network").
#   scripts/wda_start.sh            build if needed, then run (blocks)
#   scripts/wda_start.sh --build    build / re-sign only
set -uo pipefail
ROOT=${0:A:h:h}
DD=$ROOT/build/wda
BUNDLE=${IPHONE_WDA_BUNDLE:-com.shaunakkapur.WebDriverAgentRunner}
PROFILES=~/Library/Developer/Xcode/UserData/Provisioning\ Profiles
log() { print -r -- "$(date '+%F %T') $*" }
mkdir -p $ROOT/build

team() {
  # Personal Team id from the "Apple Development" certificate Xcode makes after you sign in.
  security find-certificate -a -c "Apple Development" -p 2>/dev/null | openssl x509 -noout -subject 2>/dev/null \
    | grep -oE 'OU *= *[A-Z0-9]{10}' | head -1 | grep -oE '[A-Z0-9]{10}'
}

hours_left() {
  local expires=0 plist e t
  for p in $PROFILES/*.mobileprovision(N); do
    plist=$(security cms -D -i "$p" 2>/dev/null) || continue
    [[ $plist == *".$BUNDLE.xctrunner<"* ]] || continue
    e=$(print -r -- "$plist" | plutil -extract ExpirationDate raw -o - - 2>/dev/null) || continue
    t=$(date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$e" +%s 2>/dev/null) || continue
    (( t > expires )) && expires=$t
  done
  print $(( (expires - $(date +%s)) / 3600 ))
}

build() {
  local t=$(team)
  [[ -n $t ]] || { log "no Apple Development certificate: sign into Xcode (Settings > Accounts)"; return 1 }
  log "building WebDriverAgent (team $t, $BUNDLE)"
  if xcodebuild build-for-testing -project $ROOT/vendor/WebDriverAgent/WebDriverAgent.xcodeproj \
      -scheme WebDriverAgentRunner -destination 'generic/platform=iOS' -derivedDataPath $DD \
      -allowProvisioningUpdates DEVELOPMENT_TEAM=$t CODE_SIGN_STYLE=Automatic PRODUCT_BUNDLE_IDENTIFIER=$BUNDLE \
      > $ROOT/build/wda-build.log 2>&1; then
    log "built"
  else
    log "build failed (build/wda-build.log):"; grep -E "error:" $ROOT/build/wda-build.log | sort -u | head -5
    return 1
  fi
}

left=$(hours_left)
xctestrun=($DD/Build/Products/*.xctestrun(N))
if (( left < 48 )); then
  log "signature has ${left}h left; re-signing"
  # A new 7-day profile is only issued once the old one is gone.
  for p in $PROFILES/*.mobileprovision(N); do
    security cms -D -i "$p" 2>/dev/null | grep -q ".$BUNDLE.xctrunner<" && rm -f "$p"
  done
  build || exit 1
elif (( ! $#xctestrun )); then
  build || exit 1
fi
[[ ${1:-} == --build ]] && exit 0
xctestrun=($DD/Build/Products/*.xctestrun(N))

# devicectl's table shows a UDID or a CoreDevice id depending on the Xcode version, so read its JSON instead.
LIST=$(mktemp -t devices)
xcrun devicectl list devices --json-output $LIST >/dev/null 2>&1 || true
UDID=${IPHONE_UDID:-$($ROOT/.venv/bin/python -c '
import json, sys
devs = (json.load(open(sys.argv[1])).get("result") or {}).get("devices") or []
phones = [d for d in devs if (d.get("hardwareProperties") or {}).get("reality") == "physical"
          and (d.get("hardwareProperties") or {}).get("platform") == "iOS"]
phones = [d for d in phones if (d.get("connectionProperties") or {}).get("tunnelState") != "unavailable"]
phones.sort(key=lambda d: (d.get("connectionProperties") or {}).get("tunnelState") != "connected")
print(phones[0]["hardwareProperties"]["udid"] if phones else "")
' $LIST 2>/dev/null)}
rm -f $LIST
[[ -n $UDID ]] || { log "iPhone not reachable: plug it in (or join the same Wi-Fi after pairing it with Xcode once)"; exit 3 }

log "starting WebDriverAgent on $UDID (stays up while this runs)"
# keep the test result bundle here (replaced each run) instead of piling up in Xcode's DerivedData
rm -rf $ROOT/build/wda-run.xcresult

# Idle stop: the MCP server touches build/wda.last on every call. After IPHONE_WDA_IDLE seconds (default 120)
# without one, end the test cleanly so the phone's "Automation Running" banner goes away by itself, even if
# the session that started it is gone. $$ becomes xcodebuild's pid after the exec below.
IDLE=${IPHONE_WDA_IDLE:-120}
touch $ROOT/build/wda.last
if (( IDLE > 0 )); then
  (
    while sleep 10; do
      kill -0 $$ 2>/dev/null || exit 0
      (( $(date +%s) - $(stat -f %m $ROOT/build/wda.last 2>/dev/null || print 0) < IDLE )) && continue
      log "idle ${IDLE}s: stopping WebDriverAgent"
      curl -s -m 5 "$(cat $ROOT/build/wda.url 2>/dev/null)/wda/shutdown" >/dev/null
      $ROOT/.venv/bin/python -m iphone_mcp.journal flush session >/dev/null 2>&1  # this session's actions -> GitHub
      sleep 10
      kill -0 $$ 2>/dev/null && kill -INT $$
      rm -f $ROOT/build/wda.url
      exit 0
    done
  ) &!
fi
exec xcodebuild test-without-building -xctestrun $xctestrun[1] -destination "id=$UDID" -destination-timeout 20 \
  -resultBundlePath $ROOT/build/wda-run.xcresult
