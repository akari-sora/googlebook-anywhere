# shellcheck shell=bash
# kano-session-lib.sh (v11.7): the ONLY code that changes the KANO's power state or decides which OS is running on it.
# Sessions (camera, S0ix, wake filter, internal-disk update) source this file in their MAIN shell (no `set -e`) and call
# kano_*; they never send EC power commands themselves, never use the harness's kano-boot.sh, never decide identity.
#
# Invariants (each has selftest cases in selftest/run.sh; check.py asserts P1/P2 on every scenario):
#  P1  NO hard power cut. `apshutdown` (the only EC command that can lose data) is never sent by this library. Every
#      power-off is a clean one requested from an OS identified in that same call (maintenance OS: `systemctl
#      poweroff` over ssh; an Android: `reboot -p` over adb). An OS that cannot be identified or does not power off
#      is left running and the call fails: the session stops and reports; a human decides. (TEST images power
#      themselves off when they have no Wi-Fi: the developer switch GBOS_TEST_AUTOOFF, so normal flows never need a cut.)
#  P2  `powerbtn` (exactly that word: a short press, no duration) is sent only by kano_boot, after two consecutive G3/S5
#      answers (re-checked for up to KANO_BOOT_OFF_WAIT when the EC is chatty right after power-off) and a third one
#      immediately before the press. Waking a sleeping Android is a Left-Shift key press (kano_wake, EC
#      `kbpress 7 5`): it produces no character anywhere, cannot power the AP on from G3/S5, and at most stops the stick
#      menu's countdown. `_kano_ec` sends only an exact allowlist of command shapes (P1/P2 cannot be bypassed by case,
#      spaces or arguments; refused while `nocasematch` is on); every command that changes something (powerbtn, kbpress
#      within the 18x8 key matrix, lidclose/lidopen, volatile keyboard-backlight PWM/GPIO/kblight writes) and every
#      clean power-off needs the session's hardware lock.
#  P3  Identity is decided fresh by kano_identify (boot_part_uuid for Android, the root disk's USB serial for the
#      maintenance OS); every adb/ssh step's exit status counts; it is forgotten at every power transition, every
#      mismatch, and whenever something answers that cannot be identified. "Nothing there" (the only reason to press
#      Left Shift) needs EXACT evidence about THIS address: adb connect exit 0, then get-state exit 1 with empty stdout and
#      only "error: device '<addr>' not found" lines; ssh exit 255 with empty stdout and one standard connection error
#      about KANO_IP (a grep that fails is a probe failure, never absence). Addresses must be IPv4:port; anything else
#      counts as a probe failure. The stick menu is driven in a closed loop (kano-menu-select.sh reads the highlighted
#      entry back before Enter; no Enter when it differs). The maintenance OS is also checked for the entry it came up
#      through (/proc/cmdline, tokenised on any blank: not the installer, not the keep-data update); whenever a wait for
#      any OS sees a maintenance OS that came up through another entry, it is powered off cleanly at once.
#      State-changing functions refuse to run in a subshell (their state would be lost).
#  P4  The EC state is read only from ONE complete `power state N = X, in 0xHHHH` ANSWER line, ended by the EC's "\r"
#      (a line cut by the read window has no "\r"; optionally after the "> " prompt; exactly 4 hex digits); after it, a
#      second answer, a different state, any complete line about power ("pow..."), a transition, a shutdown or a reset,
#      or ANY line without "\r"
#      other than the bare "> " prompt makes the reply "unknown", and nothing later clears that.
#  P5  kano_finish_guard: once the session's cleanup has started, INT/TERM/HUP no longer stop it.
# Limits: a human pressing the power button or opening the lid between the library's last EC read and its action is
#  not detected; the wall clock (`date +%s`) is used for timeouts only.
#
# Source it in the session's main shell WITHOUT `set -e` (every exit status is checked by hand; refused otherwise).
# Required before sourcing: OUT (run dir). Optional: KANO_HARNESS (default ~/gbos-phase/sleep/harness), KANO_TEST_GUID
# (boot_a GUID of the image on the stick's Googlebook region; empty = no stick Android is expected), KANO_STICK_ADDRS
# (space-separated extra adb host:port where the TEST Android may answer).
# Timing knobs (seconds): KANO_POLL=3 KANO_OFF_WAIT=240 KANO_WAKE_WAIT=90 KANO_BOOT_OFF_WAIT=30.

case $- in *e*) echo "kano-session-lib: source it without set -e (every exit status is checked by hand)" >&2; return 2 2> /dev/null || exit 2 ;; esac
KANO_LIB_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KANO_HARNESS=${KANO_HARNESS:-$HOME/gbos-phase/sleep/harness}
: "${OUT:?OUT (run dir) must be set before sourcing kano-session-lib.sh}"
# shellcheck disable=SC2034  # read by kano-env.sh
HERE=$KANO_HARNESS
# shellcheck disable=SC1091
. "$KANO_HARNESS/kano-env.sh"
kano_require KANO_ADB KANO_ADB_ADDR KANO_ANDROID_ID_PROP KANO_EC_TTY KANO_AP_TTY KANO_AP_UART_LOG KANO_IP KANO_STICK_SERIAL \
  KANO_NVME_BOOT_A_GUID
: "${KANO_TEST_GUID:=}" "${KANO_STICK_ADDRS:=}" "${KANO_POLL:=3}" "${KANO_OFF_WAIT:=240}" "${KANO_WAKE_WAIT:=90}" "${KANO_BOOT_OFF_WAIT:=30}"
case "$KANO_TEST_GUID" in ""|[0-9a-f]*-*-*-*-*) ;; *) echo "kano-session-lib: odd KANO_TEST_GUID" >&2; exit 2 ;; esac
[ "$KANO_TEST_GUID" != "$KANO_NVME_BOOT_A_GUID" ] || { echo "kano-session-lib: KANO_TEST_GUID is the internal disk's" >&2; exit 2; }
_kano_ipv4_ok() {  # four octets 0-255
  local -a o; local x
  [[ "$1" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 1
  IFS=. read -r -a o <<< "$1"
  for x in "${o[@]}"; do [ $((10#$x)) -le 255 ] || return 1; done
}
_kano_addr_ok() {  # IPv4:port, port 1-65535
  [[ "$1" =~ ^([0-9.]+):([0-9]{1,5})$ ]] || return 1
  local host=${BASH_REMATCH[1]} port=${BASH_REMATCH[2]}   # copy first: _kano_ipv4_ok runs its own match
  _kano_ipv4_ok "$host" && [ $((10#$port)) -ge 1 ] && [ $((10#$port)) -le 65535 ]
}
_kano_poll_ok() {  # canonical seconds > 0 and <= 60 (up to 3 fractional digits)
  [[ "$1" =~ ^(0|[1-9][0-9]{0,2})(\.([0-9]{1,3}))?$ ]] || return 1
  local f=${BASH_REMATCH[3]:-} s
  s="1${f//[0-9]/0}"
  KANO_POLL_SCALE=$s
  KANO_POLL_SCALED=$(( ${BASH_REMATCH[1]} * s + 10#${f:-0} ))
  [ "$KANO_POLL_SCALED" -gt 0 ] && [ "$KANO_POLL_SCALED" -le $(( 60 * s )) ]
}
_kano_ipv4_ok "$KANO_IP" || { echo "kano-session-lib: KANO_IP must be an IPv4 address" >&2; exit 2; }
[[ "$KANO_OFF_WAIT $KANO_WAKE_WAIT $KANO_BOOT_OFF_WAIT ${KANO_SELF_OFF_WAIT:-0} ${KANO_WRONG_ENTRY_WAIT:-0}" =~ ^(0|[1-9][0-9]{0,5})(\ (0|[1-9][0-9]{0,5})){4}$ ]] || {
  echo "kano-session-lib: KANO_OFF_WAIT, KANO_WAKE_WAIT, KANO_BOOT_OFF_WAIT, KANO_SELF_OFF_WAIT and KANO_WRONG_ENTRY_WAIT must be whole seconds" >&2; exit 2; }
_kano_poll_ok "$KANO_POLL" || { echo "kano-session-lib: KANO_POLL must be a number of seconds" >&2; exit 2; }
KANO_SHELL_LEVEL=${BASH_SUBSHELL:-0}

KANO_RUNNING=unknown        # off | maint | stick | internal | unknown
KANO_RUNNING_ADDR=""        # adb address of the identified Android
# shellcheck disable=SC2034  # read by sessions (reporting)
KANO_LAST_BOOT=""           # last entry kano_boot pressed the power button for (replaced, never cleared)
# shellcheck disable=SC2034  # read by sessions (reporting)
KANO_LAST_BOOT_OK=0         # 1 when that selection reported success
KANO_PROBE_ANSWERED=0       # set by kano_identify: 1 when anything answered (identified or not)
KANO_LOCKED=0
# shellcheck disable=SC2034  # read by sessions (reporting)
KANO_WAIT_WHY=""            # set by kano_wait_identity: why the last wait failed
mkdir -p "$OUT" || exit 1

kano_log() { echo "[$(date +%T)] $*" | tee -a "$OUT/hw.log" >&2; }
kano_now() { date +%s; }
_kano_main() { [ "${BASH_SUBSHELL:-0}" = "$KANO_SHELL_LEVEL" ] || { kano_log "REFUSED: $1 in a subshell"; return 2; }; }
_kano_need_lock() { [ "$KANO_LOCKED" = 1 ] || { kano_log "REFUSED: $1 without the hardware lock (kano_lock first)"; return 2; }; }
_kano_ec() {  # the EC console; exact command shapes only (P1, P2). Prints the RAW reply (CR kept: P4 needs it).
  local out rc
  if shopt -q nocasematch; then kano_log "REFUSED: EC '$1' while nocasematch is on (command shapes are case-exact)"; return 2; fi
  case "$1" in
    powerinfo|version|battery|lidstate|kblight|gpioget|pwmduty|"pwmduty 0"|sysinfo|cbi) ;;
    lidclose|lidopen|powerbtn) _kano_need_lock "EC $1" || return 2 ;;
    *) if [[ "$1" =~ ^gpioget\ [A-Za-z][A-Za-z0-9_]*$ ]]; then :
       elif [[ "$1" =~ ^gpioset\ EC_KB_BL_EN_L\ [01]$ ]]; then
         _kano_need_lock "EC $1" || return 2
       elif [[ "$1" =~ ^pwmduty\ 0\ (-1|[0-9]|[1-9][0-9]|100)$ ]]; then
         _kano_need_lock "EC $1" || return 2
       elif [[ "$1" =~ ^kblight\ ([0-9]|[1-9][0-9]|100)$ ]]; then
         _kano_need_lock "EC $1" || return 2
       elif [[ "$1" =~ ^kbpress\ ([0-9]{1,2})\ ([0-9])$ ]] && [ $((10#${BASH_REMATCH[1]})) -le 17 ] && [ "${BASH_REMATCH[2]}" -le 7 ]; then
         _kano_need_lock "EC $1" || return 2
       else
         kano_log "REFUSED: EC '$1' (not an allowed command shape; this library never cuts power)"; return 2
       fi ;;
  esac
  # Backlight reads: one private directory per call. Bash $(...) drops NUL, so the transport
  # bytes stay in that directory; the parser later reads those same bytes once. Transport, the log
  # append, and the raw copy each have to succeed. This branch does not install an EXIT trap (v11.7).
  if [ "$1" = "pwmduty 0" ] || [ "$1" = "gpioget EC_KB_BL_EN_L" ]; then
    _kano_kbl_ec_raw "$1" "${2:-1.5}"
    return $?
  fi
  out=$(python3 "$KANO_HARNESS/kano-ser.py" ec "$1" "${2:-1.5}" 2>&1); rc=$?
  printf '%s\n' "${out//$'\r'/}" >> "$OUT/ec.txt"
  printf '%s\n' "$out"
  return $rc
}
kano_ec_read() {  # read-only EC commands for sessions
  case "$1" in powerinfo|version|battery|lidstate|kblight|gpioget|"gpioget "*|pwmduty|"pwmduty 0"|sysinfo|cbi) _kano_ec "$@" ;;
    *) kano_log "REFUSED: kano_ec_read '$1'"; return 2 ;; esac
}
# Complete non-bracket EC line from smoke1, optional "> " prompt, CR required. Anything shorter stays a bad line.
export KANO_KBL_RTC=$'^(> )?RTC: 0x[0-9a-f]{8} [(][0-9]+[.][0-9][0-9] s[)]\r$'
# Bytes from the one private-file read. The parser uses this variable; it does not re-read a shared path.
KANO_KBL_SAME=""
_kano_kbl_drop() {
  case "$1" in
    "$OUT"/.kbl.*) rm -rf -- "$1" ;;
    *) return 1 ;;
  esac
}
_kano_kbl_mkpriv() {  # print a new 0700 directory; $dir/body is 0600. Atomic mktemp, never a fixed name.
  local d
  d=$(umask 077; mktemp -d "$OUT/.kbl.XXXXXX") || return 1
  (umask 077; : > "$d/body") || { _kano_kbl_drop "$d"; return 1; }
  chmod 700 "$d" || { _kano_kbl_drop "$d"; return 1; }
  chmod 600 "$d/body" || { _kano_kbl_drop "$d"; return 1; }
  printf '%s\n' "$d"
}
_kano_kbl_ec_raw() {  # $1 cmd, $2 seconds. Non-zero if transport, the log append, or the raw copy fails.
  local dir raw rc
  dir=$(_kano_kbl_mkpriv) || return 1
  raw=$dir/body
  python3 "$KANO_HARNESS/kano-ser.py" ec "$1" "$2" >| "$raw" 2>&1
  rc=$?
  if [ "$rc" != 0 ]; then
    _kano_kbl_drop "$dir"
    return "$rc"
  fi
  LC_ALL=C tr -d '\000\r' < "$raw" >> "$OUT/ec.txt" || { kano_log "EC '$1': logging the reply failed"; _kano_kbl_drop "$dir"; return 1; }
  cat -- "$raw" || { kano_log "EC '$1': copying the reply failed"; _kano_kbl_drop "$dir"; return 1; }
  _kano_kbl_drop "$dir"
  return 0
}
_kano_kbl_raw_ok() {  # $1: file of transport stdout. Non-zero if it contains NUL, a control other than CR/LF, or any byte >= 0x80.
  local data st
  # One read of this private file. On success the same bytes are kept for the parser.
  data=$(python3 - "$1" <<'PY'
import sys
data = open(sys.argv[1], "rb").read()
for c in data:
    if c in (10, 13) or 32 <= c < 127:
        continue
    raise SystemExit(1)
sys.stdout.buffer.write(data)
raise SystemExit(0)
PY
  )
  st=$?
  [ "$st" = 0 ] || return "$st"
  KANO_KBL_SAME=$data
}
_kano_kbl_capture() {  # _kano_kbl_capture <cmd> <varname>. Raw file first; reject bad bytes before any $(...).
  local dir st
  dir=$(_kano_kbl_mkpriv) || return 1
  _kano_kbl_capture_body "$1" "$2" "$dir"
  st=$?
  _kano_kbl_drop "$dir"
  return "$st"
}
_kano_kbl_capture_body() {
  local raw rc text
  raw=$3/body
  _kano_ec "$1" >| "$raw"
  rc=$?
  _kano_kbl_raw_ok "$raw" || { kano_log "kbl read: rejected non-text EC reply"; return 1; }
  [ "$rc" = 0 ] || { KANO_KBL_SAME=""; return 1; }
  text=$KANO_KBL_SAME
  KANO_KBL_SAME=""
  printf -v "$2" '%s' "$text"
}
_kano_kbl_pwm_is() {
  local r
  KANO_KBL_RAW_PWM=""
  _kano_kbl_capture "pwmduty 0" r || return 1
  KANO_KBL_RAW_PWM=$r
  printf '%s\n' "$r" | awk -v w="0: $1" '
    BEGIN { got = 0; bad = 0; rtc = ENVIRON["KANO_KBL_RTC"] }
    /^(> )?pwmduty( 0)?\r$/ || /^(> ?)?\r?$/ || (rtc != "" && $0 ~ rtc) { next }
    /^(> )?0: (([0-9]|[1-9][0-9]|100)%|disabled)\r$/ { sub(/^> /, ""); sub(/\r$/, ""); got = (got == 0 && $0 == w) ? 1 : -1; next }
    /^(> )?0:/ { bad = 1; next }
    /^(> )?(\[[0-9]+\.[0-9]+ [^\r]*\]|Setting channel 0 to ([0-9]|[1-9][0-9]|100)|1: (([0-9]|[1-9][0-9]|100)%|disabled))\r$/ { next }
    { bad = 1 }
    END { exit !(got == 1 && !bad) }'
}
_kano_kbl_gpio_is() {
  local r
  KANO_KBL_RAW_GPIO=""
  _kano_kbl_capture "gpioget EC_KB_BL_EN_L" r || return 1
  KANO_KBL_RAW_GPIO=$r
  printf '%s\n' "$r" | awk -v w="$1" '
    BEGIN { got = 0; bad = 0; rtc = ENVIRON["KANO_KBL_RTC"] }
    /^(> )?gpioget( EC_KB_BL_EN_L)?\r$/ || /^(> ?)?\r?$/ || (rtc != "" && $0 ~ rtc) { next }
    /^(> )? *[01]\*? +EC_KB_BL_EN_L\r$/ { sub(/^> /, ""); sub(/^ +/, ""); got = (got == 0 && substr($0, 1, 1) == w) ? 1 : -1; next }
    /^(> )?\[[0-9]+\.[0-9]+ [^\r]*\]\r$/ { next }
    /EC_KB_BL_EN_L/ { bad = 1; next }
    { bad = 1 }
    END { exit !(got == 1 && !bad) }'
}
_kano_kbl_gpio_diag() {  # phrase for a failed GPIO read-back. =0 only from the one complete target line that says 0.
  printf '%s\n' "${KANO_KBL_RAW_GPIO:-}" | awk '
    BEGIN { n = 0; lvl = ""; dirty = 0; rtc = ENVIRON["KANO_KBL_RTC"] }
    /^(> )?gpioget( EC_KB_BL_EN_L)?\r$/ || /^(> ?)?\r?$/ || (rtc != "" && $0 ~ rtc) { next }
    /^(> )?\[[0-9]+\.[0-9]+ [^\r]*\]\r$/ { next }
    /^(> )? *[01]\*? +EC_KB_BL_EN_L\r$/ {
      sub(/^> /, ""); sub(/^ +/, "")
      if (n == 0) lvl = substr($0, 1, 1)
      n++
      next
    }
    { dirty = 1 }
    END {
      if (n == 1 && lvl == "0") print "rail may still be ON: EC_KB_BL_EN_L=0"
      else if (n == 0) print "rail may still be ON: EC_KB_BL_EN_L=unconfirmed"
      else if (n == 1 && lvl == "1" && dirty) print "EC_KB_BL_EN_L=unconfirmed"
      else print "EC_KB_BL_EN_L=unconfirmed"
    }'
}
_kano_kbl_pwm_diag() {  # phrase for a failed PWM read-back. A dirty "disabled" is unconfirmed; a single duty is reported.
  printf '%s\n' "${KANO_KBL_RAW_PWM:-}" | awk '
    BEGIN { n = 0; v = ""; dirty = 0; rtc = ENVIRON["KANO_KBL_RTC"] }
    /^(> )?pwmduty( 0)?\r$/ || /^(> ?)?\r?$/ || (rtc != "" && $0 ~ rtc) { next }
    /^(> )?0: (([0-9]|[1-9][0-9]|100)%|disabled)\r$/ {
      sub(/^> /, ""); sub(/\r$/, "")
      if (n == 0) v = $0
      n++
      next
    }
    /^(> )?(\[[0-9]+\.[0-9]+ [^\r]*\]|Setting channel 0 to ([0-9]|[1-9][0-9]|100)|1: (([0-9]|[1-9][0-9]|100)%|disabled))\r$/ { next }
    { dirty = 1 }
    END {
      if (n == 1 && v != "" && !(dirty && v == "0: disabled")) print v
      else print "0: unconfirmed"
    }'
}
_kano_kbl_off() {
  local ok_g ok_p gpio_s pwm_s
  KANO_KBL_RAW_GPIO=""
  _kano_ec "gpioset EC_KB_BL_EN_L 1" > /dev/null && _kano_kbl_gpio_is 1 \
    || { KANO_KBL_RAW_GPIO=""; _kano_ec "gpioset EC_KB_BL_EN_L 1" > /dev/null && _kano_kbl_gpio_is 1; }
  ok_g=$?
  KANO_KBL_RAW_PWM=""
  _kano_ec "pwmduty 0 -1" > /dev/null && _kano_kbl_pwm_is disabled \
    || { KANO_KBL_RAW_PWM=""; _kano_ec "pwmduty 0 -1" > /dev/null && _kano_kbl_pwm_is disabled; }
  ok_p=$?
  [ "$ok_g" = 0 ] && [ "$ok_p" = 0 ] && return 0
  if [ "$ok_g" = 0 ]; then gpio_s="EC_KB_BL_EN_L=1"; else gpio_s=$(_kano_kbl_gpio_diag); fi
  : "${gpio_s:=rail may still be ON: EC_KB_BL_EN_L=unconfirmed}"
  if [ "$ok_p" = 0 ]; then
    pwm_s="0: disabled"
  else
    pwm_s=$(_kano_kbl_pwm_diag)
    : "${pwm_s:=0: unconfirmed}"
  fi
  kano_log "kbl_off: read-back check failed ($gpio_s; pwmduty $pwm_s)"
  return 1
}
kano_kbl_state() {  # read-only snapshot of the keyboard-backlight EC state (kblight, pwmduty 0, EC_KB_BL_EN_L)
  _kano_ec kblight && _kano_ec "pwmduty 0" && _kano_ec "gpioget EC_KB_BL_EN_L"
}
kano_kbl_set() {  # kano_kbl_set <0..100|off>: volatile keyboard-backlight PWM + enable GPIO with read-back check
  _kano_main kano_kbl_set || return 2
  _kano_need_lock "kano_kbl_set ${1:-}" || return 2
  if [ $# != 1 ] || shopt -q nocasematch || ! [[ "$1" =~ ^(off|[0-9]|[1-9][0-9]|100)$ ]]; then
    kano_log "REFUSED: kano_kbl_set '$*'"; return 2
  fi
  kano_log "kbl_set $1: running=$KANO_RUNNING"
  if [ "$1" = off ]; then
    _kano_kbl_off && return 0
    kano_log "kbl_set off: read-back check failed"
    return 1
  fi
  if _kano_ec "pwmduty 0 $1" > /dev/null && _kano_kbl_pwm_is "$1%" \
     && _kano_ec "gpioset EC_KB_BL_EN_L 0" > /dev/null && _kano_kbl_gpio_is 0; then
    return 0
  fi
  kano_log "kbl_set $1: read-back check failed; restoring off"
  if _kano_kbl_off; then
    kano_log "kbl_set $1: restore off succeeded (EC_KB_BL_EN_L=1; pwmduty 0: disabled)"
  else
    kano_log "kbl_set $1: restore off failed; rail/PWM state unconfirmed"
  fi
  return 1
}
KANO_SHIFT_KEY="7 5"        # Left Shift: MATRIX_KEY(0x05, 0x07) in both ChromeOS keymaps (kbpress <col> <row>)
kano_lid() {
  _kano_main kano_lid || return 2
  case "$1" in close) _kano_ec lidclose > /dev/null ;; open) _kano_ec lidopen > /dev/null ;; *) return 2 ;; esac
}
kano_key() {  # kano_key <col> <row>: one keyboard-matrix key press + release (EC kbpress), e.g. 11 4 = Enter
  _kano_main kano_key || return 2
  case "$1$2" in *[!0-9]*|"") kano_log "REFUSED: kano_key '$1' '$2'"; return 2 ;; esac
  _kano_ec "kbpress $1 $2" > /dev/null
}
kano_ssh() { timeout "${KANO_SSH_T:-120}" "$KANO_HARNESS/kano-ssh.sh" "$@" < /dev/null; }
_kano_adb() {  # exit status of adb itself; CR removed; stderr to $OUT/.adb-err (never mixed into identity data)
  local out rc
  out=$(timeout "${KANO_ADB_T:-20}" "$KANO_ADB" "$@" < /dev/null 2> "$OUT/.adb-err"); rc=$?
  printf '%s' "${out//$'\r'/}"
  return $rc
}
_kano_forget() { KANO_RUNNING=unknown; KANO_RUNNING_ADDR=""; }
_kano_in_list() { case "|$2|" in *"|$1|"*) return 0 ;; esac; return 1; }   # _kano_in_list <item> <a|b|c>

# P4
_kano_state_of() {
  # Complete lines end with "\r" (the EC's line end); a line cut by kano-ser.py's read window does not. Before the answer
  # anything goes (it is older). After it: a line without "\r" other than the bare prompt (a cut line: it may be the
  # start of a second answer), another answer, a history line with a different state, or any other complete line about
  # power, a transition or a shutdown → "unknown" (sticky). Other complete lines (RTC, PD, logs) are ignored.
  awk '
    BEGIN { ans = ""; bad = 0; n = 0 }
    /^(> )?power state [0-9]+ = [A-Z0-9>-]+, in 0x[0-9a-f][0-9a-f][0-9a-f][0-9a-f]\r$/ {
      n++; l = $0; sub(/^> /, "", l); split(l, a, " = "); split(a[2], b, ","); ans = b[1]; next }
    ans == "" { next }
    !/\r$/ { if ($0 !~ /^(> ?)?$/) bad = 1; next }
    /^(> )?\[[0-9]+\.[0-9]+ power state [0-9]+ = [A-Z0-9>-]+, in 0x[0-9a-f][0-9a-f][0-9a-f][0-9a-f]\]\r$/ {
      s = $0; sub(/.* = /, "", s); sub(/,.*/, "", s); if (s != ans) bad = 1; next }
    { l = tolower($0) } l ~ /pow|shutdown|chipset|reset|transition/ || l ~ /->/ { bad = 1; next }
    END { if (ans == "" || bad || n != 1) print "unknown"; else print ans }'
}
kano_power_state() {  # prints the state; rc != 0 (and "unknown") when the EC read or the parser failed
  local r s
  r=$(_kano_ec powerinfo) || { echo unknown; return 1; }
  s=$(printf '%s\n' "$r" | _kano_state_of) && [ -n "$s" ] || { echo unknown; return 1; }
  printf '%s\n' "$s"
}
_kano_off_once() { local s; s=$(kano_power_state) || return 1; case "$s" in G3|S5) return 0 ;; esac; return 1; }
kano_is_off() {  # two answers KANO_POLL apart, both G3/S5
  _kano_main kano_is_off || return 2
  _kano_off_once || return 1
  sleep "$KANO_POLL"
  _kano_off_once || return 1
  _kano_forget; KANO_RUNNING=off
}
kano_wait_off() {
  local end=$(( $(kano_now) + $1 ))
  while [ "$(kano_now)" -le "$end" ]; do kano_is_off && return 0; sleep "$KANO_POLL"; done
  return 1
}
_kano_s0_twice() { local s; s=$(kano_power_state) && [ "$s" = S0 ] && sleep "$KANO_POLL" && s=$(kano_power_state) && [ "$s" = S0 ]; }

# P3. _kano_probe <addr>: prints the boot_part_uuid of this project's Android there.
# rc 0 = identified; 1 = nothing connects there; 3 = something answers but is not usable for identity;
# 4 = the probe itself failed (adb missing or hung): treated like "something answers" (never a reason to press).
_kano_probe() {
  local st id u rc
  _kano_adb connect "$1" > /dev/null; rc=$?
  [ $rc = 0 ] || return 4                            # adb connect exits 0 even when it cannot connect: anything else is a tool failure
  st=$(_kano_adb -s "$1" get-state); rc=$?
  case $rc in
    0) ;;
    1) # absent only with no output and nothing but "not found" about THIS address (a wrong address or a daemon problem is not absence)
       if [ -z "$st" ] && [ -s "$OUT/.adb-err" ]; then
         grep -qvxF "error: device '$1' not found" "$OUT/.adb-err"; [ $? = 1 ] && return 1   # 1 = no other line; 0 = another line; 2+ = grep failed
       fi
       return 4 ;;
    *) return 4 ;;
  esac
  [ "$st" = device ] || { _kano_adb disconnect "$1" > /dev/null; return 3; }
  id=$(_kano_adb -s "$1" shell getprop "$KANO_ANDROID_ID_PROP") || return 3
  [ -n "$id" ] || return 3
  u=$(_kano_adb -s "$1" shell getprop ro.boot.boot_part_uuid) || return 3
  case "$u" in [0-9a-f]*-*-*-*-*) printf '%s\n' "$u" ;; *) return 3 ;; esac
}
_kano_maint_probe() {  # rc 0 = the maintenance OS on the stick; 1 = nothing listens; 3 = answers, not the stick; 4 = tool/auth failure
  local s rc
  s=$(kano_ssh 'D=$(lsblk -no PKNAME $(findmnt -nvo SOURCE /)); echo "/dev/$D $(lsblk -dno TRAN,SERIAL /dev/$D)"' 2> "$OUT/.ssh-err"); rc=$?
  if [ $rc != 0 ]; then
    # absent only with no output and one standard connection-level line about KANO_IP as ssh's whole complaint
    # (compared in bash, so no helper tool can fail silently; a CR, a second line or any other text is not absence)
    local e
    [ $rc = 255 ] && [ -z "$s" ] && ! shopt -q nocasematch && e=$(< "$OUT/.ssh-err") || return 4
    case "$e" in *$'\n'*|*$'\r'*) return 4 ;; esac
    [[ "$e" =~ ^ssh:\ connect\ to\ host\ ([0-9.]+)\ port\ [0-9]+:\ (Connection\ refused|Connection\ timed\ out|No\ route\ to\ host|Network\ is\ unreachable|Host\ is\ down)$ ]] \
      && [ "${BASH_REMATCH[1]}" = "$KANO_IP" ] && return 1
    return 4
  fi
  printf '%s\n' "$s" | awk -v s="$KANO_STICK_SERIAL" '{ gsub(/ +$/, "") } $1 == "/dev/sda" && $2 == "usb" && $3 == s { ok = 1 } END { exit !ok }' || return 3
}
kano_identify() {
  _kano_main kano_identify || return 2
  local a u rc
  KANO_RUNNING=unknown; KANO_RUNNING_ADDR=""; KANO_PROBE_ANSWERED=0
  _kano_maint_probe; rc=$?
  if [ $rc = 0 ]; then KANO_RUNNING=maint; KANO_PROBE_ANSWERED=1; return 0; fi
  [ $rc = 1 ] || KANO_PROBE_ANSWERED=1
  local -a addrs
  local list="$KANO_ADB_ADDR $KANO_STICK_ADDRS"
  case "$list" in *[!0-9.:\ ]*) kano_log "identify: odd characters in the adb address list (counts as a probe failure)"; KANO_PROBE_ANSWERED=1
    list=${list//[$'\n\r\t']/ } ;; esac
  IFS=' ' read -r -a addrs <<< "$list"
  local probed=0
  for a in "${addrs[@]}"; do
    _kano_addr_ok "$a" || { kano_log "identify: odd adb address '$a' (counts as a probe failure)"; KANO_PROBE_ANSWERED=1; continue; }
    probed=$((probed + 1))
    u=$(_kano_probe "$a"); rc=$?
    [ $rc = 1 ] && continue
    KANO_PROBE_ANSWERED=1
    [ $rc = 0 ] || continue
    if [ "$u" = "$KANO_NVME_BOOT_A_GUID" ]; then KANO_RUNNING=internal; KANO_RUNNING_ADDR=$a; return 0; fi
    if [ -n "$KANO_TEST_GUID" ] && [ "$u" = "$KANO_TEST_GUID" ]; then KANO_RUNNING=stick; KANO_RUNNING_ADDR=$a; return 0; fi
    _kano_adb disconnect "$a" > /dev/null
  done
  [ "$probed" -gt 0 ] || { kano_log "identify: no valid adb address was probed"; KANO_PROBE_ANSWERED=1; }
  return 1
}
_kano_recheck() {  # _kano_recheck <stick|internal> <uuid>: the identified Android at KANO_RUNNING_ADDR is still that one
  local u
  [ "$KANO_RUNNING" = "$1" ] && [ -n "$KANO_RUNNING_ADDR" ] || { kano_log "REFUSED: $1 command while running=$KANO_RUNNING"; return 2; }
  u=$(_kano_adb -s "$KANO_RUNNING_ADDR" shell getprop ro.boot.boot_part_uuid) && [ "$u" = "$2" ] \
    || { kano_log "REFUSED: $KANO_RUNNING_ADDR is no longer the $1 Android"; _kano_forget; return 2; }
}
kano_stick_sh() {  # kano_stick_sh <cmd> [timeout]: test commands only on the stick's TEST Android
  _kano_main kano_stick_sh || return 2
  _kano_recheck stick "$KANO_TEST_GUID" || return 2
  KANO_ADB_T=${2:-60} _kano_adb -s "$KANO_RUNNING_ADDR" shell "$1"
}
# The owner's daily system: only these read-only command shapes (plus the screen wake key), nothing that writes.
# (bare `dumpsys power`/`dumpsys battery` only: other services and flags can write, e.g. batterystats --reset, diskstats;
# paths without "..": a component may not start with a dot)
declare -r KANO_INTERNAL_RO='^(getprop( [A-Za-z0-9_.]+)?|dumpsys (power|battery)|settings get (system|secure|global) [A-Za-z0-9_.]+|cat /(sys|proc)(/[A-Za-z0-9_:-][A-Za-z0-9_:.-]*)+|ls( -[A-Za-z]+)? /([A-Za-z0-9_:-][A-Za-z0-9_:.-]*/?)*|input keyevent KEYCODE_WAKEUP)$'
kano_internal_sh() {  # read-only snapshots of the internal-disk Android
  _kano_main kano_internal_sh || return 2
  if shopt -q nocasematch || ! [[ "$1" =~ $KANO_INTERNAL_RO ]] || [[ "$1" =~ /proc/[^/]+/(root|cwd|fd|map_files)(/|$) ]]; then
    kano_log "REFUSED: kano_internal_sh '$1' (not a read-only command shape)"; return 2
  fi
  _kano_recheck internal "$KANO_NVME_BOOT_A_GUID" || return 2
  KANO_ADB_T=${2:-60} _kano_adb -s "$KANO_RUNNING_ADDR" shell "$1"
}
kano_stick_pull() {  # kano_stick_pull <remote path> <local dir> [timeout]: files from the stick's TEST Android
  _kano_main kano_stick_pull || return 2
  _kano_recheck stick "$KANO_TEST_GUID" || return 2
  KANO_ADB_T=${3:-180} _kano_adb -s "$KANO_RUNNING_ADDR" pull "$1" "$2" > /dev/null
}
kano_scan_adb() {  # prints "host:5555" for every 192.0.2.x with port 5555 open (where to ASK; identity is by uuid)
  local n
  for n in $(seq 1 254); do
    ( timeout 1 bash -c "echo > /dev/tcp/192.0.2.$n/5555" 2> /dev/null && echo "192.0.2.$n:5555" ) &
    [ $(( n % 32 )) = 0 ] && wait
  done
  wait
}
kano_android_awake() {  # of the identified Android; reads the whole dumpsys before matching
  local d
  [ -n "$KANO_RUNNING_ADDR" ] || return 1
  d=$(_kano_adb -s "$KANO_RUNNING_ADDR" shell "dumpsys power") || return 1
  [ "$(printf '%s\n' "$d" | sed -n 's/^ *mWakefulness=\([A-Za-z]*\)$/\1/p' | tail -n 1)" = Awake ]
}

# Wake a sleeping Android (Wi-Fi off while asleep): one Left-Shift press, then identify. Harmless in any state (P2).
kano_wake() {
  _kano_main kano_wake || return 2
  if kano_identify; then kano_log "wake: $KANO_RUNNING answers; no key"; return 0; fi
  [ "$KANO_PROBE_ANSWERED" = 0 ] || { kano_log "wake: something answers but is not identified (or a probe tool failed); no key"; return 1; }
  kano_log "wake: nothing answers: Left Shift once (wakes a sleeping Android; harmless otherwise)"
  _kano_forget
  _kano_ec "kbpress $KANO_SHIFT_KEY" > /dev/null || kano_log "wake: kbpress returned non-zero (the key may still have been sent)"
  local end=$(( $(kano_now) + KANO_WAKE_WAIT ))
  while [ "$(kano_now)" -le "$end" ]; do sleep "$KANO_POLL"; kano_identify && return 0; done
  return 1
}

# Clean power-off of whatever runs, then G3/S5. Returns 0 only when off is confirmed; never cuts power (P1).
kano_shutdown() {
  _kano_main kano_shutdown || return 2
  _kano_need_lock kano_shutdown || return 2
  if kano_is_off; then kano_log "shutdown: already off"; return 0; fi
  local who addr why="nothing answers"
  if kano_identify; then who=$KANO_RUNNING
  else
    [ "$KANO_PROBE_ANSWERED" = 0 ] || why="no OS identified (something answers or a probe tool failed)"
    if [ "${KANO_SELF_OFF_WAIT:-0}" -gt 0 ] && { kano_log "shutdown: $why; waiting up to ${KANO_SELF_OFF_WAIT}s for the TEST image to power itself off"; kano_wait_off "$KANO_SELF_OFF_WAIT"; }; then
      kano_log "shutdown: the TEST image powered itself off (G3/S5)"; return 0
    elif kano_wake; then who=$KANO_RUNNING
    elif kano_is_off; then kano_log "shutdown: the machine powered off by itself meanwhile (G3/S5)"; return 0
    else kano_log "shutdown: no OS identified and no G3/S5 confirmed: leaving it as it is (no power cut)"; return 1; fi
  fi
  addr=$KANO_RUNNING_ADDR
  case "$who" in
    maint) kano_ssh 'sync; systemctl poweroff' > /dev/null 2>&1 ;;   # ssh drops when it powers off: rc is not checked
    internal|stick) _kano_adb -s "$addr" shell 'reboot -p' > /dev/null ;;
    *) kano_log "shutdown: running=$who: leaving it as it is"; return 1 ;;
  esac
  _kano_forget
  kano_log "shutdown: clean power-off requested from $who"
  if kano_wait_off "$KANO_OFF_WAIT"; then kano_log "shutdown: $who is off (G3/S5)"; return 0; fi
  kano_log "shutdown: $who did not reach G3/S5 within ${KANO_OFF_WAIT}s: leaving it as it is (no power cut)"
  return 1
}

# P2 (a): power on from G3 and select a stick menu entry (exact title "=..."). A failed selection leaves the machine on.
kano_boot() {  # kano_boot <tag> <=exact title>
  _kano_main kano_boot || return 2
  _kano_need_lock "boot $1" || return 2
  case "$2" in =?*) ;; *) kano_log "boot $1: the entry must be an exact title (=...)"; return 2 ;; esac
  [[ "$KANO_BOOT_OFF_WAIT" =~ ^(0|[1-9][0-9]{0,5})$ ]] && _kano_poll_ok "$KANO_POLL" \
    || { kano_log "boot $1: invalid KANO_POLL or KANO_BOOT_OFF_WAIT: not booting"; return 1; }
  local end=""
  if ! kano_is_off; then
    local now prev scale=$KANO_POLL_SCALE ps=$KANO_POLL_SCALED ws step cap tries=0 ok=0
    now=$(kano_now) && [[ "$now" =~ ^[0-9]+$ ]] || { kano_log "boot $1: kano_now failed or non-numeric ($now); the EC does not report G3/S5 twice: not booting"; return 1; }
    prev=$now; end=$(( now + KANO_BOOT_OFF_WAIT ))
    ws=$(( KANO_BOOT_OFF_WAIT * scale ))
    step=$(( (ps + scale - 1) / scale ))
    cap=$(( (ws + ps - 1) / ps + 1 ))
    while :; do
      now=$(kano_now) && [[ "$now" =~ ^[0-9]+$ ]] || { kano_log "boot $1: kano_now failed or non-numeric ($now); the EC does not report G3/S5 twice: not booting"; return 1; }
      [ "$now" -ge "$prev" ] || { kano_log "boot $1: wall clock rolled back ($now < $prev); the EC does not report G3/S5 twice: not booting"; return 1; }
      prev=$now
      if [ "$ok" = 1 ]; then [ "$now" -le "$end" ] || { kano_log "boot $1: the EC does not report G3/S5 twice: not booting"; return 1; }; break; fi
      tries=$(( tries + 1 ))
      [ "$tries" -le "$cap" ] || { kano_log "boot $1: retry cap ($cap) reached; the EC does not report G3/S5 twice: not booting"; return 1; }
      [ "$KANO_BOOT_OFF_WAIT" -gt 0 ] && [ "$now" -lt "$end" ] && [ $(( now + step )) -le "$end" ] \
        || { kano_log "boot $1: the EC does not report G3/S5 twice: not booting"; return 1; }
      kano_log "boot $1: the EC did not report G3/S5 twice (chatty EC after power-off?); retrying in ${KANO_POLL}s"
      sleep "$KANO_POLL"
      kano_is_off && ok=1
    done
  fi
  local off rc
  : > "$KANO_AP_UART_LOG" || { kano_log "boot $1: cannot empty the UART log"; return 1; }
  _kano_off_once || { kano_log "boot $1: not off right before the press: not booting"; return 1; }
  if [ -n "$end" ]; then
    now=$(kano_now) && [[ "$now" =~ ^[0-9]+$ ]] \
      || { kano_log "boot $1: kano_now failed or non-numeric ($now); the EC does not report G3/S5 twice: not booting"; return 1; }
    [ "$now" -ge "$prev" ] \
      || { kano_log "boot $1: wall clock rolled back ($now < $prev); the EC does not report G3/S5 twice: not booting"; return 1; }
    [ "$now" -le "$end" ] || { kano_log "boot $1: the EC does not report G3/S5 twice: not booting"; return 1; }
  fi
  _kano_forget
  _kano_ec powerbtn > /dev/null || kano_log "boot $1: powerbtn returned non-zero (the press may still have been sent); running the menu selection anyway"
  # shellcheck disable=SC2034  # read by sessions (reporting)
  KANO_LAST_BOOT=$2; KANO_LAST_BOOT_OK=0
  bash "${KANO_MENU_SELECT:-$KANO_LIB_DIR/kano-menu-select.sh}" "$2" > "$OUT/boot-$1.txt" 2>&1 < /dev/null; rc=$?
  kano_log "boot $1: $(grep -v '^uart-offset ' "$OUT/boot-$1.txt" | tail -n 1) (rc $rc)"
  off=$(sed -n 's/^uart-offset \([1-9][0-9]*\)$/\1/p' "$OUT/boot-$1.txt" | tail -n 1)   # 0 is never a real size after a menu
  # shellcheck disable=SC2034  # read by sessions (reporting)
  if [ $rc = 0 ] && grep -q "^selected #" "$OUT/boot-$1.txt" && [ -n "$off" ]; then KANO_LAST_BOOT_OK=1; return 0; fi
  kano_log "boot $1: the selection failed; the machine was powered on (the menu, or its default entry after the countdown)"
  [ $rc = 0 ] && rc=1
  return $rc
}
kano_wait_identity() {  # kano_wait_identity <maint|stick|internal> <seconds>; on failure KANO_WAIT_WHY says why
  _kano_main kano_wait_identity || return 2
  local end=$(( $(kano_now) + $2 )) e
  KANO_WAIT_WHY="nothing answered as $1 within $2 s"
  while [ "$(kano_now)" -le "$end" ]; do
    if kano_identify; then
      if [ "$KANO_RUNNING" = maint ]; then
        # a maintenance OS counts only when it runs the plain entry; the installer / keep-data update is powered off
        # now; an unread command line is asked again on the next poll
        kano_maint_entry_is plain; e=$?
        if [ $e = 0 ]; then
          [ "$1" = maint ] && { KANO_WAIT_WHY=""; return 0; }
        elif [ $e = 3 ]; then
          KANO_WAIT_WHY="a maintenance OS answered, but its command line was not read within $2 s"
        else
          kano_log "a maintenance OS came up through another entry while waiting for $1: powering it off cleanly"
          if kano_shutdown; then KANO_WAIT_WHY="a maintenance OS of another entry came up and was powered off cleanly"
          else KANO_WAIT_WHY="a maintenance OS of another entry came up and did not power off: left as it is (no power cut)"; fi
          kano_log "$KANO_WAIT_WHY"; return 1
        fi
      elif [ "$KANO_RUNNING" = "$1" ]; then
        KANO_WAIT_WHY=""; return 0
      fi
    fi
    sleep "$KANO_POLL"
  done
  return 1
}
kano_to_maint() {  # kano_to_maint <tag>: clean off, boot the maintenance OS, require that its root is the stick
  _kano_main kano_to_maint || return 2
  kano_shutdown || return 1
  kano_boot "maint-$1" "=KANO maintenance Linux (Wi-Fi + SSH)" || { _kano_stop_wrong_maint "maint-$1"; return 1; }
  kano_wait_identity maint 420 || { kano_log "maintenance OS ($1) not confirmed: $KANO_WAIT_WHY"; _kano_stop_wrong_maint "maint-$1"; return 1; }
  # second line of defence after the closed-loop menu: prove the entry from the running kernel's command line (the
  # installer and the keep-data update boot the same maintenance kernel with gbos.install=1 / gbos.update=1)
  local e
  kano_maint_entry_is plain; e=$?
  if [ $e != 0 ]; then
    if [ $e = 3 ]; then kano_log "maintenance OS ($1): its command line could not be read again: powering it off cleanly"
    else kano_log "maintenance OS ($1) came up through another entry: powering it off cleanly"; fi
    kano_shutdown; return 1
  fi
  kano_log "maintenance OS up ($1)"
}
_kano_stop_wrong_maint() {  # after a failed or unconfirmed boot: a maintenance OS that came up through another entry
  # (installer, keep-data update) is powered off cleanly; anything else (an Android, the menu, nothing) is left as it is.
  # rc 0 = nothing wrong is running (or it was powered off); 1 = still unidentified at the end, or the power-off failed
  local end=$(( $(kano_now) + ${KANO_WRONG_ENTRY_WAIT:-420} )) e
  while [ "$(kano_now)" -le "$end" ]; do
    kano_is_off && return 0                       # already off (e.g. a wait powered the wrong entry off): nothing runs
    if kano_identify; then
      [ "$KANO_RUNNING" = maint ] || return 0
      kano_maint_entry_is plain; e=$?
      [ $e = 0 ] && return 0
      if [ $e = 3 ]; then kano_log "$1: the maintenance OS's command line was not read: powering it off cleanly"
      else kano_log "$1: the maintenance OS runs another entry: powering it off cleanly"; fi
      kano_shutdown && return 0
      kano_log "$1: that maintenance OS did not power off: left as it is (no power cut)"; return 1
    fi
    sleep "$KANO_POLL"
  done
  kano_log "$1: no OS identified within ${KANO_WRONG_ENTRY_WAIT:-420}s after the failed boot: left as it is"
  return 1
}
kano_maint_entry_is() {  # kano_maint_entry_is <plain|install|update>: the identified maintenance OS booted through that entry
  # rc 0 yes; 1 another entry (or an odd command line); 2 refused (not a maintenance OS); 3 the command line was not read
  _kano_main kano_maint_entry_is || return 2
  [ "$KANO_RUNNING" = maint ] || { kano_log "REFUSED: entry check while running=$KANO_RUNNING"; return 2; }
  local c x inst=0 upd=0 odd=0 got
  local -a t
  c=$(kano_ssh 'cat /proc/cmdline' 2> /dev/null) && [ -n "$c" ] || { kano_log "entry check: /proc/cmdline not read"; return 3; }
  # one line of plain tokens split on any blank (the kernel splits on isspace); quotes or line breaks inside: odd
  case "$c" in *$'\n'*|*$'\r'*|*\"*|*\'*) odd=1 ;; esac
  IFS=$' \t\v\f' read -r -a t <<< "$c"
  for x in "${t[@]}"; do
    case "$x" in gbos.install=1) inst=1 ;; gbos.update=1) upd=1 ;; gbos.install*|gbos.update*) odd=1 ;; esac
  done
  if [ $odd = 1 ]; then got=odd; elif [ $upd = 1 ]; then got=update; elif [ $inst = 1 ]; then got=install; else got=plain; fi
  [ "$got" = "$1" ] || { kano_log "entry check: the maintenance OS runs the '$got' entry, not '$1'"; return 1; }
}
# End of every session: the internal disk booted through the stick's explicit entry (not the menu default), awake.
kano_boot_internal() {
  _kano_main kano_boot_internal || return 2
  kano_shutdown || { kano_log "END: could not reach G3/S5; the internal disk was NOT booted"; return 1; }
  kano_boot end "=Googlebook (internal disk)" || { kano_log "END: the stick menu has no 'Googlebook (internal disk)' entry or the selection failed"; _kano_stop_wrong_maint end; return 1; }
  kano_wait_identity internal 900 || { kano_log "END: the internal disk not confirmed: $KANO_WAIT_WHY"; _kano_stop_wrong_maint end; return 1; }
  if ! kano_android_awake; then
    kano_internal_sh "input keyevent KEYCODE_WAKEUP" > /dev/null; sleep 3
    kano_android_awake || { kano_log "END: the internal disk is up but not awake"; return 1; }
  fi
  kano_log "END: internal disk up and awake at $KANO_RUNNING_ADDR"
}
kano_finish_guard() { trap '' INT TERM HUP; }   # P5: call first thing in the session's EXIT handler

# The hardware lock for the whole session (fd 9; children inherit the descriptor, so kano_unlock releases the lock with
# flock -u before closing it: a background child left behind cannot keep the hardware locked).
kano_lock() {  # kano_lock <owner name>
  _kano_main kano_lock || return 2
  [ "$KANO_LOCKED" = 0 ] || { kano_log "kano_lock: already held by this session"; return 2; }
  local f=${KANO_LOCK:-/tmp/kano-hw.lock}
  exec 9> "$f" || exit 1
  flock -w "${KANO_LOCK_WAIT:-10800}" 9 || { echo "kano-session-lib: hardware lock $f not free" >&2; exit 1; }
  KANO_LOCKED=1
  echo "$1 $$ $(date +%T) $OUT" > "${f%.lock}.owner" || kano_log "kano_lock: owner file not written (lock still held)"
}
kano_unlock() {
  _kano_main kano_unlock || return 2
  [ "$KANO_LOCKED" = 1 ] || return 0
  local f=${KANO_LOCK:-/tmp/kano-hw.lock}
  rm -f "${f%.lock}.owner"; flock -u 9; exec 9>&-; KANO_LOCKED=0
}

kano_sha_ok() {  # kano_sha_ok <file>: <file>.sha256 holds its SHA-256
  local got want
  [ -f "$1" ] && [ -f "$1.sha256" ] || return 1
  got=$(sha256sum < "$1") || return 1; got=${got%% *}
  want=$(cut -c1-64 "$1.sha256") || return 1
  case "$got" in [0-9a-f]*) [ ${#got} = 64 ] && [ "$got" = "$want" ] ;; *) return 1 ;; esac
}
kano_image_boot_a() {  # boot_a partition GUID of a GPT disk image; fails unless exactly one boot_a exists
  python3 - "$1" << 'PY'
import struct, sys, uuid
import zlib
f = open(sys.argv[1], "rb"); f.seek(512); h = f.read(512)
if len(h) < 92 or h[:8] != b"EFI PART": sys.exit("no GPT header")
hs, = struct.unpack_from("<I", h, 12)
if not 92 <= hs <= 512: sys.exit("odd GPT header size")
hdr = bytearray(h[:hs]); want, = struct.unpack_from("<I", hdr, 16); hdr[16:20] = bytes(4)
if zlib.crc32(hdr) & 0xFFFFFFFF != want: sys.exit("GPT header CRC mismatch")
lba, n, sz, ecrc = struct.unpack_from("<QIII", h, 72)
f.seek(0, 2); sectors = f.tell() // 512
if not (0 < n <= 1024 and 128 <= sz <= 1024): sys.exit("odd GPT entry table")
f.seek(lba * 512); t = f.read(n * sz)
if len(t) != n * sz: sys.exit("GPT entry table cut short")
if zlib.crc32(t) & 0xFFFFFFFF != ecrc: sys.exit("GPT entry table CRC mismatch")
hits = [i for i in range(n) if t[i * sz:i * sz + 16] != bytes(16)
        and t[i * sz + 56:i * sz + 128].decode("utf-16le", "replace").rstrip("\0") == "boot_a"]
if len(hits) == 1:
    i = hits[0]; first, last = struct.unpack_from("<QQ", t, i * sz + 32)
    if t[i * sz + 16:i * sz + 32] == bytes(16): sys.exit("boot_a has a nil unique GUID")
    if not 0 < first <= last < sectors: sys.exit("boot_a lies outside the image")
    hits = [uuid.UUID(bytes_le=t[i * sz + 16:i * sz + 32])]
if len(hits) != 1: sys.exit(f"{len(hits)} boot_a partitions")
print(hits[0])
PY
}
