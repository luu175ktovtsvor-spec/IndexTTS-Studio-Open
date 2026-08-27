#!/usr/bin/env bash

_native_no_proxy_contains() {
  local list="$1"
  local wanted="$2"
  local remaining="$list"
  local item

  while true; do
    if [[ "$remaining" == *,* ]]; then
      item="${remaining%%,*}"
      remaining="${remaining#*,}"
    else
      item="$remaining"
      remaining=""
    fi
    item=$(printf '%s' "$item" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    if [[ "$item" == "$wanted" ]]; then
      return 0
    fi
    [[ -n "$remaining" ]] || break
  done
  return 1
}

merge_no_proxy_rules() {
  local merged=""
  local source_list
  local remaining
  local item

  for source_list in "$@"; do
    remaining="$source_list"
    while true; do
      if [[ "$remaining" == *,* ]]; then
        item="${remaining%%,*}"
        remaining="${remaining#*,}"
      else
        item="$remaining"
        remaining=""
      fi
      item=$(printf '%s' "$item" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
      if [[ -n "$item" ]] && ! _native_no_proxy_contains "$merged" "$item"; then
        merged="${merged:+${merged},}${item}"
      fi
      [[ -n "$remaining" ]] || break
    done
  done
  printf '%s\n' "$merged"
}
