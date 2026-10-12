# shellcheck shell=sh
# Settings of the KANO developer harness: the single source for the shell scripts (which source this file from their
# own directory, with HERE set to that directory) and the Python scripts (which parse the same lines).
#
# Every setting is one line of the form   : "${NAME:=default}"   and the first non-empty value wins:
#   1. the environment,
#   2. kano-env.local.sh next to this file (optional; your own values, never published; same line format),
#   3. the defaults below.
# The Python scripts only read lines of exactly that form and only expand $HOME and names set earlier (no command
# substitution, no other shell syntax in values).
#
# The identities below have no default on purpose: they name one particular machine, stick and disk. A script that
# needs one refuses to start while it is empty, before any ssh, adb, serial or disk action.
if [ -n "${HERE:-}" ] && [ -f "$HERE/kano-env.local.sh" ]; then . "$HERE/kano-env.local.sh"; fi

# network
: "${KANO_IP:=}"                                      # maintenance Linux on the stick: root ssh, key only
: "${KANO_SSH_KEY:=}"                                 # private key for root@KANO_IP
: "${KANO_ADB_ADDR:=}"                                # Android adb over TCP, host:port; one fixed address, never scanned
: "${KANO_ADB:=adb}"                                  # adb executable
: "${KANO_ANDROID_ID_PROP:=init.svc.kano_bootfix}"     # property that only this project's vendor image defines

# directories on the development host
: "${KANO_RUNS:=$HOME/kano-harness/runs}"              # run dirs, AP UART log, camera frames
: "${KANO_AP_UART_LOG:=$KANO_RUNS/ap-uart.log}"        # written by `kano-ser.py ap-log`
: "${KANO_WORK:=$HOME/kano-harness/work}"              # kano-final<N>/ build outputs
: "${KANO_NVME_SRC:=}"                                # kano-nvme-install.sh: build dir of the image on the stick
: "${KANO_PYSERIAL_ROOT:=}"                           # dir with third_party/pyserial; empty = the system's pyserial

# Cr50 (SuzyQ / CCD) serial ports, e.g. /dev/serial/by-id/usb-Google_Inc._Cr50_<serial>-if01-port0 (AP), -if02 (EC)
: "${KANO_CR50_SERIAL:=}"
: "${KANO_AP_TTY:=}"
: "${KANO_EC_TTY:=}"

# disk identities checked by every script that writes a disk
: "${KANO_STICK_SERIAL:=}"                            # the USB stick with Googlebook + the maintenance Linux
: "${KANO_NVME_SERIAL:=}"                             # the internal NVMe
: "${KANO_NVME_PCI:=}"                                # its PCI vendor:device, e.g. 0x1234:0x5678
: "${KANO_NVME_BOOT_A_GUID:=}"                        # unique GUID of boot_a on the NVMe (after kano-nvme-install.sh)

export KANO_IP KANO_SSH_KEY KANO_ADB_ADDR KANO_ADB KANO_ANDROID_ID_PROP KANO_RUNS KANO_AP_UART_LOG KANO_WORK \
  KANO_NVME_SRC KANO_PYSERIAL_ROOT KANO_CR50_SERIAL KANO_AP_TTY KANO_EC_TTY KANO_STICK_SERIAL KANO_NVME_SERIAL KANO_NVME_PCI \
  KANO_NVME_BOOT_A_GUID

# kano_require NAME...: refuse (exit 2) unless every NAME is set, before the caller does anything else
kano_require() {
  for _kano_n in "$@"; do
    eval "_kano_v=\${$_kano_n:-}"
    if [ -z "$_kano_v" ]; then
      echo "refusing: $_kano_n is not set; put it in kano-env.local.sh or the environment (see README.md)" >&2
      exit 2
    fi
  done
}
