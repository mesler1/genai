#!/usr/bin/env bash
# Sandbox hook: make software from environment modules (Lmod, Environment Modules, EasyBuild) work
# inside the sandbox. Prints requests only: it asks for the package directories behind your search
# paths (ro), asks for the search paths themselves (path), and passes variables that point into
# those directories, such as EBROOTGCC (env). HOPLON_MODULE_ROOTS is a colon list of whole trees to
# mount, such as /apps.
#
# Install: ln -s "$PWD/sandbox/hooks/modules.sh" ~/.local/share/hoplon-sandbox/hooks/
# Then load your modules and run sandbox/run. See HOOKS.md.
set -u

vars=(PATH LD_LIBRARY_PATH LIBRARY_PATH CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH PKG_CONFIG_PATH CMAKE_PREFIX_PATH MANPATH)
# Leaf directories that are not the package itself, longest first.
leaves=(lib64/pkgconfig lib/pkgconfig libdata/pkgconfig share/pkgconfig share/man share/info
        sbin bin lib64 lib32 libexec lib include man)

inside() { [[ "$1" == "$2" || "$1" == "${2%/}"/* ]]; }

home_real=$(realpath -m "${HOME:-/nonexistent}")
declare -A asked=() exported=()
while IFS= read -r name; do
  if [[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then exported[$name]=1; fi
done < <(compgen -e)
prefixes=()

ask_ro() {  # $1 = directory; print it once and remember it
  [[ -z "${asked[$1]:-}" ]] || return 0
  asked[$1]=1
  prefixes+=("$1")
  printf 'ro %s\n' "$1"
}

covered() {  # is $1 at or under something already asked for?
  local p
  for p in "${prefixes[@]}"; do
    if inside "$1" "$p"; then return 0; fi
  done
  return 1
}

package_dir() {  # strip a well-known leaf from $1; otherwise the entry itself
  local p=$1 leaf
  for leaf in "${leaves[@]}"; do
    if [[ "$p" == */"$leaf" ]]; then
      p=${p%/"$leaf"}
      break
    fi
  done
  # "/" and a single top-level directory are too broad to ask for unless named in HOPLON_MODULE_ROOTS
  if [[ -z "$p" || "$p" =~ ^/[^/]*$ ]]; then p=$1; fi
  printf '%s\n' "$p"
}

# Whole trees named by the user.
IFS=: read -r -a roots <<<"${HOPLON_MODULE_ROOTS:-}"
for r in "${roots[@]}"; do
  [[ "$r" == /* && -d "$r" ]] || continue
  r=$(realpath "$r")
  [[ "$r" != / ]] || continue
  ask_ro "$r"
done

for v in "${vars[@]}"; do
  [[ -n "${exported[$v]:-}" ]] || continue
  value=${!v:-}
  rest="$value:"
  while [[ -n "$rest" ]]; do
    e=${rest%%:*}
    rest=${rest#*:}
    [[ "$e" == /* && -e "$e" ]] || continue
    rp=$(realpath -e "$e" 2>/dev/null) || continue
    if inside "$rp" /usr || inside "$rp" /etc || inside "$rp" "$home_real"; then continue; fi
    if covered "$rp"; then continue; fi
    p=$(package_dir "$rp")
    [[ -n "$p" && ! "$p" =~ ^/[^/]*$ ]] || continue
    ask_ro "$p"
  done
done

for v in "${vars[@]}"; do
  if [[ -n "${exported[$v]:-}" && -n "${!v:-}" ]]; then printf 'path %s\n' "$v"; fi
done

# Other exported variables whose whole value is one path inside what was asked for.
while IFS= read -r name; do
  [[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
  case "$name" in
    HOME|XDG_CONFIG_HOME|HOPLON_SANDBOX|PATH|PYTHONPATH|LD_*|PWD|OLDPWD|_|HOPLON_MODULE_ROOTS) continue ;;
  esac
  skip=0
  for v in "${vars[@]}"; do
    if [[ "$v" == "$name" ]]; then skip=1; fi
  done
  [[ $skip == 0 ]] || continue
  value=${!name:-}
  [[ "$value" == /* && "$value" != *:* && -e "$value" ]] || continue
  rp=$(realpath -e "$value" 2>/dev/null) || continue
  if covered "$rp"; then printf 'env %s\n' "$name"; fi
done < <(compgen -e | LC_ALL=C sort)
