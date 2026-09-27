#!/usr/bin/env bash
# Read-only checks to run on an AmpliPi before installing the Sendspin client.
#   usage: bash sendspin_preflight.sh [--deep]
# --deep additionally downloads uv + a managed python into a temp dir (removed afterwards) and
# dry-run resolves sendspin to show exactly which packages would have to be compiled on this box.

SENDSPIN_VERSION=7.5.0
DEEP=false
[[ "$1" == "--deep" ]] && DEEP=true

pass() { printf '  \e[32mPASS\e[0m %s\n' "$*"; }
warn() { printf '  \e[33mWARN\e[0m %s\n' "$*"; }
fail() { printf '  \e[31mFAIL\e[0m %s\n' "$*"; }
info() { printf '       %s\n' "$*"; }
ver_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" == "$2" ]]; }

echo "== Platform"
. /etc/os-release 2>/dev/null
info "OS: ${PRETTY_NAME:-unknown} (${VERSION_CODENAME:-?})"
machine=$(uname -m)
userland=$(getconf LONG_BIT)
dpkg_arch=$(dpkg --print-architecture 2>/dev/null)
info "kernel arch: $machine, userland: ${userland}-bit, dpkg arch: $dpkg_arch"
glibc=$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')
info "glibc: $glibc"
if [[ "$dpkg_arch" == "arm64" ]]; then
  pass "64-bit userland: PyPI has aarch64 wheels for all sendspin dependencies"
elif [[ "$dpkg_arch" == "armhf" ]]; then
  warn "32-bit armhf userland: numpy, pillow, cffi and sendspin have no armv7 cp312 wheels on PyPI"
fi
# PyPI armv7 wheels used by sendspin (av, aiohttp, zeroconf) need manylinux_2_31
GLIBC_231=true
if ver_ge "$glibc" 2.31; then pass "glibc >= 2.31 (needed by the av/aiohttp armv7 wheels)"; else fail "glibc < 2.31, av/aiohttp armv7 wheels will not install"; GLIBC_231=false; fi
# piwheels cp313 armv7 wheels are built on Raspberry Pi OS trixie (glibc 2.41)
PIWHEELS_OK=false
if [[ "$dpkg_arch" != "armhf" ]]; then
  :
elif ver_ge "$glibc" 2.41; then
  pass "glibc >= 2.41: piwheels cp313 wheels (numpy/pillow/cffi) should load, no big compiles needed with python 3.13"
  PIWHEELS_OK=true
else
  warn "glibc < 2.41: piwheels cp313 wheels won't load, numpy/pillow/cffi would be compiled from source"
fi

echo "== Resources"
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
swap_mb=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
info "RAM: ${mem_mb}MB, swap: ${swap_mb}MB"
if (( mem_mb + swap_mb < 1500 )); then warn "RAM+swap < 1.5GB, compiling numpy may run out of memory"; else pass "RAM+swap ok for compiling"; fi
home_free=$(df -Pm "$HOME" | awk 'NR==2 {print $4}')
info "free space in $HOME: ${home_free}MB"
if (( home_free < 1024 )); then fail "less than 1GB free for the uv python + sendspin venv (+ build cache)"; else pass "disk space ok"; fi
tmp_free=$(df -Pm /tmp | awk 'NR==2 {print $4}')
info "free space in /tmp: ${tmp_free}MB (source builds unpack here)"

echo "== Tools and libraries"
for bin in curl dbus-daemon aplay; do
  if command -v $bin >/dev/null; then pass "$bin present"; else warn "$bin missing"; fi
done
if command -v "$HOME/.local/bin/uv" >/dev/null || command -v uv >/dev/null; then pass "uv already installed"; else info "uv not installed yet (the installer fetches it)"; fi
if [[ -x "$HOME/.local/bin/sendspin" ]]; then info "sendspin already installed: $("$HOME/.local/bin/sendspin" --version 2>/dev/null)"; fi
if command -v gcc >/dev/null; then pass "gcc present ($(gcc -dumpversion)), source builds possible"; else warn "no gcc: any source build (sendspin's own C extension, numpy, pillow, cffi) will fail"; fi
for pkg in libportaudio2 libffi-dev libjpeg-dev zlib1g-dev build-essential; do
  if dpkg-query -W -f='${Status}' $pkg 2>/dev/null | grep -q "install ok installed"; then pass "$pkg installed"; else info "$pkg not installed"; fi
done

echo "== Network"
for url in https://pypi.org/simple/sendspin/ https://www.piwheels.org/simple/numpy/ https://astral.sh/uv/install.sh https://github.com; do
  code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$url")
  if [[ "$code" =~ ^(200|301|302)$ ]]; then pass "reach $url"; else fail "cannot reach $url (http $code)"; fi
done

echo "== AmpliPi runtime"
if aplay -L 2>/dev/null | grep -q '^lb0c$'; then pass "virtual source loopbacks present (lb0c ...)"; else warn "lb0c not found in 'aplay -L'"; fi
runtime_dir=$(systemctl --user show-environment 2>/dev/null | sed -n 's/^XDG_RUNTIME_DIR=//p')
runtime_dir=${runtime_dir:-$XDG_RUNTIME_DIR}
if [[ -n "$runtime_dir" && -w "$runtime_dir" ]]; then pass "XDG_RUNTIME_DIR=$runtime_dir (private dbus sockets go here)"; else warn "no writable XDG_RUNTIME_DIR, sockets fall back to ~/.config/amplipi/srcs"; fi
if systemctl is-active --quiet avahi-daemon; then pass "avahi-daemon running (mDNS discovery)"; else warn "avahi-daemon not running, Music Assistant may not discover players (set a server instead)"; fi
busy=$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -oE ':(89[23][0-9]|894[01])$' | tr '\n' ' ')
if [[ -z "$busy" ]]; then pass "ports 8927-8941 free"; else warn "ports in use: $busy (sendspin streams listen on 8930+vsrc)"; fi

if $DEEP; then
  echo "== Deep check (dry-run resolve in a temp dir)"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  export UV_CACHE_DIR=$tmp/cache UV_PYTHON_INSTALL_DIR=$tmp/python UV_NO_MODIFY_PATH=1
  if curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=$tmp/bin sh >/dev/null 2>&1; then
    for py in 3.12 3.13; do
      extra=()
      [[ $py == 3.13 ]] && extra=(--extra-index-url https://www.piwheels.org/simple)
      if ! "$tmp/bin/uv" venv -q --seed --python $py --python-preference only-managed "$tmp/venv$py" 2>/dev/null; then
        warn "python $py: uv has no managed build for this platform"
        continue
      fi
      deps=$(VIRTUAL_ENV=$tmp/venv$py "$tmp/bin/uv" pip install --dry-run "sendspin==$SENDSPIN_VERSION" 2>&1 | awk '/^ \+ / {print $2}')
      if [[ -z "$deps" ]]; then fail "python $py: could not resolve sendspin==$SENDSPIN_VERSION"; continue; fi
      # ask pip, running natively here, whether each pinned package has a wheel for this platform
      needs_build=()
      pure_src=()
      for dep in $deps; do
        # pure-python packages only published as sdists, they build instantly without a compiler
        if [[ "$dep" =~ ^(mpris-api|tunit)== ]]; then pure_src+=("$dep"); continue; fi
        "$tmp/venv$py/bin/python" -m pip install -q --dry-run --no-deps --only-binary=:all: --disable-pip-version-check \
          --cache-dir "$tmp/pipcache" "${extra[@]}" "$dep" >/dev/null 2>&1 || needs_build+=("$dep")
      done
      (( ${#pure_src[@]} )) && info "python $py: pure-python source packages (fine): ${pure_src[*]}"
      if (( ${#needs_build[@]} == 0 )); then
        pass "python $py$([[ $py == 3.13 ]] && echo ' + piwheels'): no compiled packages need building"
      else
        warn "python $py$([[ $py == 3.13 ]] && echo ' + piwheels'): would compile from source: ${needs_build[*]}"
      fi
    done
  else
    fail "could not download uv"
  fi
fi

echo
echo "== Verdict"
if [[ "$dpkg_arch" != "armhf" ]]; then
  echo "  $dpkg_arch: the planned 'uv tool install --python 3.12 sendspin' should install from wheels."
elif ! $GLIBC_231; then
  echo "  32-bit with glibc $glibc (< 2.31): not viable. Beyond numpy/pillow/cffi, av (PyAV, FFmpeg bindings) would also"
  echo "  have to be compiled against a newer FFmpeg than this OS ships. Use a native (non-Python) sendspin client instead."
elif $PIWHEELS_OK; then
  echo "  32-bit on a new enough OS: install with python 3.13 + piwheels to avoid compiling numpy:"
  echo "    uv tool install --python 3.13 --extra-index-url https://www.piwheels.org/simple --index-strategy unsafe-best-match sendspin==$SENDSPIN_VERSION"
  echo "  (sendspin's own small C extension still needs gcc)"
else
  echo "  32-bit with glibc < 2.41: numpy, pillow and cffi must be compiled on the Pi (slow, needs gcc and"
  echo "  libffi-dev/libjpeg-dev/zlib1g-dev, may run out of memory). Consider prebuilding wheels elsewhere."
fi
