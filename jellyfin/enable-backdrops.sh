#!/bin/sh
set -u

BUNDLE="/jellyfin/jellyfin-web/main.jellyfin.bundle.js"
TARGET='enableBackdrops:function(){return _}'

if grep -q "$TARGET" "$BUNDLE"; then
  echo "Backdrops patch already applied"
else
  sed -Ei 's/enableBackdrops:function\(\)\{return [A-Za-z_$][A-Za-z0-9_$]*\}/enableBackdrops:function(){return _}/' "$BUNDLE"
  if grep -q "$TARGET" "$BUNDLE"; then
    echo "Backdrops patch applied"
  else
    echo "Backdrops patch pattern not found; starting Jellyfin without patch"
  fi
fi

exec /jellyfin/jellyfin
