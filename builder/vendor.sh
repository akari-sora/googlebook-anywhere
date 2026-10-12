#!/bin/bash
# Stage 1 of the Googlebook-on-KANO build: rebuild the vendor partition of a lapis base image with every KANO change
# (see docs/HOW-IT-WORKS.md), re-sign vbmeta with your AVB key and refresh the ESP initrd bootconfig.
# History of the individual changes (safe2 ... safe25, all verified on the real KANO):
# Rebuild kano-safe25.img: safe24 + ACPI Power Button no longer excluded (KANO has no cros_ec_buttons); safe24 = safe23 + light on-disk diag (adb is the live channel); safe23 = safe22 + adbd on tcp:5555 (system pins localhost), udsattestation stopped; safe22 = safe21 + key-only adb over TCP 5555 for the harness (the builder's key), fsync'd heavy diag files; safe21 = safe20 + stop timberslide/gscd (EC/TPM crash loops), 5-min system_server watchdog setting, audio self-test only on request, battery/bluetooth/camera/sensors/power in the diag; safe20 = safe19 + one-shot audio self-test (dras_tool tone + mic), diag without global sync (small files via dd conv=fsync), audio dumpsys pipes; safe19 = safe18 + DRAS audio config for the KANO card (sof-nau8825, from ChromeOS kano UCM), keyboard backlight LED path for the KANO (idc, ueventd, genfscon), serial logcat only on the serial-debug entry, lighter diag (60 s, io-top, audio/input dumps, reader timeouts); safe18 = safe17 + cmd/dumpsys fd/pipe access for the kano debug domain, timeouts and display/backlight reads in kano-diag, Wi-Fi join via pipes; safe16 without the firmware updaters/flash tools (fwupd + cabs, flashrom, futility, ectool, crossystem, ap-ec-fw.zip); safe15 + KANO firmware (AX211 Wi-Fi, AX211 Bluetooth, SOF IPC3 max98373+nau8825), iwlmvm load, Wi-Fi autoconnect from /data/kano-wifi.conf; safe13 + kano_diag (dmesg and logcat snapshots in /data/kano-diag every 20 s, for boots where the serial console dies); safe12 + kano_bootfix (sets persist.vold.dont_commit_checkpoint=1 every boot, so a fresh /data does not fall back into the 'Checkpoint commit failed' loop); safe11 without the IWeaver/ISecretkeeper VINTF declarations (their HALs need the protected VM, so LockSettings waited forever and the watchdog killed system_server); safe10 with /data on f2fs (4K fsync on the Kingston: f2fs median 4 ms vs ext4 p90 18 s) and without the rejected cross-treble overrides; safe9 + stop the GSC/EC/Trusty crash loops (each crash writes a tombstone to the slow stick and system_server fsyncs stall past the watchdog) + quieter serial logcat; safe8 + no cppreopts (system_server watchdog loop on the slow stick) + serial logcat (permissive kano_kmsg_logcat domain, on-device policy compile); safe7 + userdata without checkpoint=block (no dm-bow rollback after power cuts); experiment A (colour pipeline off) + drm_hwcomposer client backend (no tiled buffer on a plane; xe faults on DPT scanout) + unencrypted userdata, no USB voldmanaged, ro.debuggable=1 (serial console).
# Vendor drops fwupd's on-boot starts and desktop_kernel_max_rollforward_boot.rc.
# Other logical partitions stay byte-identical. vbmeta is re-signed.
# ESP initrd keeps the current archive and takes the standalone bootconfig,
# with vbmeta digest, size, and boot_a UUID filled in.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${GBOS_BASE:?GBOS_BASE = base image (output of base/build-base.sh)}"
: "${GBOS_INITRD:?GBOS_INITRD = standalone initrd with bootconfig (output of base/build-base.sh)}"
: "${GBOS_OUT:?GBOS_OUT = work/output directory}"
: "${GBOS_OTATOOLS:?GBOS_OTATOOLS = AOSP otatools directory (bin/avbtool, bin/mkfs.erofs, bin/fsck.erofs, lib64)}"
: "${GBOS_AVB_KEY:?GBOS_AVB_KEY = your RSA-4096 AVB private key (PEM)}"
: "${GBOS_PROFILE:?GBOS_PROFILE = device profile directory, e.g. profiles/kano}"
REAL="$GBOS_BASE"
STANDALONE="$GBOS_INITRD"
WORK="$GBOS_OUT/vendor"
IMG="$WORK/stage-vendor.img"
TMP="$WORK/tmp"
OT="$GBOS_OTATOOLS"
KEY="$GBOS_AVB_KEY"
FW="${GBOS_FIRMWARE:?GBOS_FIRMWARE = firmware directory (builder/fetch-inputs.sh <dir> fills <dir>/firmware)}"
export GBOS_AUDIO_SRC="${GBOS_AUDIO_SRC:?GBOS_AUDIO_SRC = audio sources of the profile (builder/fetch-inputs.sh <dir> fills <dir>/audio-src)}"
export GBOS_PROFILE GBOS_ADB_PUBKEY="${GBOS_ADB_PUBKEY:-}"
if [ -n "$GBOS_ADB_PUBKEY" ]; then
    # an adb public key is exactly one line "<base64 of the 524-byte RSAPublicKey> [user@host]"; anything else (a private
    # key, a second line, control characters) never goes into the image
    python3 - "$GBOS_ADB_PUBKEY" << 'PY' || { echo "GBOS_ADB_PUBKEY must be an adb public key file (~/.android/adbkey.pub), not $GBOS_ADB_PUBKEY"; exit 2; }
import base64, binascii, re, struct, sys
try:
    raw = open(sys.argv[1], "rb").read()
    text = raw.decode("ascii")
except (OSError, UnicodeDecodeError):
    sys.exit(1)
if len(raw) > 2048 or not re.fullmatch(r"[A-Za-z0-9+/=]+( [\x21-\x7e]+)?\n?", text):
    sys.exit(1)
try:
    key = base64.b64decode(text.split()[0], validate=True)
except (binascii.Error, ValueError):
    sys.exit(1)
# RSAPublicKey: u32 words (64), u32 n0inv, u32 n[64], u32 rr[64], u32 exponent
if len(key) != 524 or struct.unpack_from("<I", key, 0)[0] != 64 or struct.unpack_from("<I", key, 520)[0] not in (3, 65537):
    sys.exit(1)
PY
fi
grep -v '^#' "$GBOS_PROFILE/audio-src.list" | awk 'NF == 3 {print $3 "  " $1}' | ( cd "$GBOS_AUDIO_SRC" && sha256sum -c ) >/dev/null \
    || { echo "audio sources in $GBOS_AUDIO_SRC do not match $GBOS_PROFILE/audio-src.list"; exit 1; }
SALT=b14f17c0007cfc878a5cdac13f0514ac3b7d22996d4db3d254082387c6705124

# otatools binaries get their own lib64 (avbtool runs fec from PATH and passes it on); python3, mtools, zstd etc. do not
ot() { local b=$1; shift; LD_LIBRARY_PATH="$OT/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$OT/bin/$b" "$@"; }
export PATH="$OT/bin:$PATH"
export MTOOLS_SKIP_CHECK=1

rm -rf "$TMP"
mkdir -p "$TMP" "$WORK/logs"

echo "copy base image"
cp --reflink=auto "$REAL" "$IMG"

python3 - "$IMG" "$TMP" "$HERE/lib" << 'PY'
import pickle, struct, sys, uuid
from pathlib import Path

img = Path(sys.argv[1])
tmp = Path(sys.argv[2])
sys.path.insert(0, sys.argv[3])
import rebuild

def gpt(path):
    with path.open("rb") as f:
        f.seek(512)
        hdr = f.read(92)
        if hdr[:8] != b"EFI PART":
            raise SystemExit("not gpt")
        ents, nent, esz = struct.unpack_from("<QII", hdr, 72)
        f.seek(ents * 512)
        data = f.read(nent * esz)

    def guid(b):
        d1, d2, d3 = struct.unpack_from("<IHH", b, 0)
        rest = b[8:16]
        return f"{d1:08x}-{d2:04x}-{d3:04x}-{rest[:2].hex()}-{rest[2:].hex()}"

    out = []
    for i in range(nent):
        e = data[i * esz:(i + 1) * esz]
        if e[:16] == b"\x00" * 16:
            continue
        name = e[56:128].decode("utf-16le").rstrip("\x00")
        first, last = struct.unpack_from("<QQ", e, 32)
        out.append({
            "name": name,
            "guid": guid(e[16:32]),
            "start": first * 512,
            "size": (last - first + 1) * 512,
        })
    return out

def vendor_extent(path, super_off):
    with path.open("rb") as f:
        f.seek(super_off + 4096)
        geo = f.read(52)
        if geo[:4] != b"gDla":
            raise SystemExit("no LP geometry")
        f.seek(super_off + 12288)
        hdr = f.read(256)
        if hdr[:4] != b"0PLA":
            raise SystemExit("no LP header")
        # LpMetadataHeader: header size at 8, tables size at 44, partitions and extents table descriptors at 80 and 92
        hsize = struct.unpack_from("<I", hdr, 8)[0]
        tables_size = struct.unpack_from("<I", hdr, 44)[0]
        p_off, p_num, p_size = struct.unpack_from("<III", hdr, 80)
        e_off, e_num, e_size = struct.unpack_from("<III", hdr, 92)
        f.seek(super_off + 12288 + hsize)
        tables = f.read(tables_size)
    for i in range(p_num):
        e = tables[p_off + i * p_size:p_off + (i + 1) * p_size]
        name = e[:36].split(b"\x00", 1)[0].decode()
        _attr, first, num, _group = struct.unpack_from("<IIII", e, 36)
        if name != "vendor_a":
            continue
        if num != 1 or first >= e_num:
            raise SystemExit(f"vendor extents {num}")
        nsec, ttype, tdata, _tsource = struct.unpack_from("<QIQI", tables, e_off + first * e_size)
        if ttype != 0:
            raise SystemExit("vendor extent is not linear")
        return tdata * 512, nsec * 512
    raise SystemExit("vendor_a missing")

parts = {p["name"]: p for p in gpt(img)}
super_off = parts["super"]["start"]
rel_off, vendor_size = vendor_extent(img, super_off)
if rel_off + vendor_size > parts["super"]["size"]:
    raise SystemExit("vendor extent runs past the super partition")
vendor_off = super_off + rel_off
with img.open("rb") as f:
    f.seek(vendor_off)
    blob = f.read(vendor_size)
idx = blob.rfind(b"AVBf")
if idx < 0:
    raise SystemExit("vendor has no AVB footer")
orig = struct.unpack_from(">Q", blob, idx + 12)[0]
erofs = blob[:orig]
(tmp / "vendor.erofs.src").write_bytes(erofs)
u = str(uuid.UUID(bytes=erofs[1024 + 48:1024 + 64]))
if u != "7aead136-f6d4-508e-9bf2-2517134fe885":
    raise SystemExit("unexpected vendor uuid " + u + ": this builder supports only lapis CL3B.260622.271.R1 (build 16452207)")

data, blksz, meta_off, xattr, root = rebuild.load_image(tmp / "vendor.erofs.src")
vmap, _hard = rebuild.walk_image(str(tmp / "vendor.erofs.src"), root, data, blksz, meta_off, xattr)
(tmp / "vendor.meta.pickle").write_bytes(pickle.dumps(vmap))
(tmp / "offsets.env").write_text(
    f"ESP_OFF={parts['EFI-SYSTEM']['start']}\n"
    f"VENDOR_OFF={vendor_off}\n"
    f"VENDOR_SIZE={vendor_size}\n"
    f"VBMETA_OFF={parts['vbmeta_a']['start']}\n"
    f"BOOT_UUID={parts['boot_a']['guid']}\n"
    f"VENDOR_UUID={u}\n"
)
print((tmp / "offsets.env").read_text())
PY

# shellcheck disable=SC1091
source "$TMP/offsets.env"

echo "KANO firmware (RAM-loaded by the drivers, nothing is flashed): linux-firmware 20260810, sof-bin v2025.12.2"
# GBOS_FIRMWARE is a linux-firmware layout (plain or .zst files, symlinks allowed), e.g. /usr/lib/firmware on Arch or
# the <dir>/firmware that builder/fetch-inputs.sh fills from the upstream releases. Contents are pinned; other versions were
# never tested on the KANO, so they are refused unless GBOS_ALLOW_FIRMWARE_DRIFT=1.
fw() {
    local p="$FW/$1"
    mkdir -p "$(dirname "$2")"
    if [ -e "$p" ]; then cp "$(readlink -f "$p")" "$2"
    elif [ -e "$p.zst" ]; then zstd -q -d -f "$(readlink -f "$p.zst")" -o "$2"
    else echo "firmware missing: $p (or $p.zst); run builder/fetch-inputs.sh or set GBOS_FIRMWARE"; exit 1; fi
}
fw i915/adlp_dmc.bin "$TMP/fw/adlp_dmc.bin"
fw i915/adlp_guc_70.bin "$TMP/fw/adlp_guc_70.bin"
fw i915/tgl_huc.bin "$TMP/fw/tgl_huc.bin"
fw iwlwifi-so-a0-gf-a0-89.ucode "$TMP/kfw/iwlwifi-so-a0-gf-a0-89.ucode"
fw iwlwifi-so-a0-gf-a0.pnvm "$TMP/kfw/iwlwifi-so-a0-gf-a0.pnvm"
fw intel/ibt-0040-0041.sfi "$TMP/kfw/intel/ibt-0040-0041.sfi"
fw intel/ibt-0040-0041.ddc "$TMP/kfw/intel/ibt-0040-0041.ddc"
fw intel/sof/community/sof-adl.ri "$TMP/kfw/intel/sof/community/sof-adl.ri"
fw intel/sof-tplg/sof-adl-max98373-nau8825.tplg "$TMP/kfw/intel/sof-tplg/sof-adl-max98373-nau8825.tplg"
if ! ( cd "$TMP" && sha256sum -c ) << 'SUMS'
3516de2e134ddcf3b319c75d2e437779fecbd58cbb77234bd6f297c544e92ccb  fw/adlp_dmc.bin
b7a800cbdcdf687e7b1c81f3316d040ab75fa3e5c698ebc356f077af17462ef8  fw/adlp_guc_70.bin
dbb1316bac13a76427ffa7827e52b5b9affe8a4cadd23dc22e2a54e35c53bdad  fw/tgl_huc.bin
7b53254bb28115f91b6eaa33b1f9a3526e32966105eaac587e259dd17790f384  kfw/iwlwifi-so-a0-gf-a0-89.ucode
2aee7592ddbd6c88f1aa51be4ada188fce44765405e52d7f3056b46e62cf3f91  kfw/iwlwifi-so-a0-gf-a0.pnvm
8dcfb3a7c1592c11c3e80505d21ee78dbde3de0c830d8e3e856371c37717b62c  kfw/intel/ibt-0040-0041.sfi
fe272982577efdc289cfe3e8bedafca607c0b7d6c9df1aab1880e22ef930d077  kfw/intel/ibt-0040-0041.ddc
e3489dcb277e87eee8bc091ab7d51163a63c85541c22522775122b54263769f3  kfw/intel/sof/community/sof-adl.ri
4beb29400d9beed0c80bcc11b3f4e82aad70a54d6e851638c81eb087a10d4de5  kfw/intel/sof-tplg/sof-adl-max98373-nau8825.tplg
SUMS
then
    [ "${GBOS_ALLOW_FIRMWARE_DRIFT:-}" = 1 ] || { echo "firmware differs from the tested versions (see above); set GBOS_ALLOW_FIRMWARE_DRIFT=1 to use it anyway"; exit 1; }
    echo "WARNING: building with untested firmware versions (GBOS_ALLOW_FIRMWARE_DRIFT=1)"
fi
( cd "$TMP" && find fw kfw -type f -exec sha256sum {} + )

echo "extract vendor tree"
mkdir -p "$TMP/vendor-tree"
ot fsck.erofs --extract="$TMP/vendor-tree" --overwrite --preserve-perms "$TMP/vendor.erofs.src"

python3 - "$TMP" "$HERE/lib" << 'PY'
import os, pickle, sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
import rebuild

tmp = Path(sys.argv[1])
tree = tmp / "vendor-tree"
vmap = pickle.loads((tmp / "vendor.meta.pickle").read_bytes())
fw = tree / "etc/init/fwupd.rc"
text = fw.read_text()
block = (
    "on boot\n"
    "    start fwupd\n"
    "    # Start the daemon in the background first\n"
    "    start fwupd_script\n"
    "\n"
)
if block not in text:
    raise SystemExit("fwupd on boot block not found")
fw.write_text(text.replace(block, "", 1))
roll = tree / "etc/init/desktop_kernel_max_rollforward_boot.rc"
if not roll.is_file():
    raise SystemExit("rollforward rc missing")
roll.unlink()
import shutil
for f in ("adlp_dmc.bin", "adlp_guc_70.bin", "tgl_huc.bin"):
    dst = tree / "firmware/i915" / f
    if dst.exists():
        raise SystemExit("already present: " + f)
    shutil.copyfile(tmp / "fw" / f, dst)
    dst.chmod(0o644)
    vmap["firmware/i915/" + f] = dict(vmap["firmware/i915/xe2lpd_dmc.bin"])
import re as _re
fst = tree / "etc/fstab.android-desktop"
ft = fst.read_text()
ud = [l for l in ft.splitlines() if l.startswith("/dev/block/by-name/userdata")]
if len(ud) != 1 or "fileencryption=" not in ud[0]:
    raise SystemExit("userdata line not as expected")
new_ud = _re.sub(r"(fileencryption|keydirectory|metadata_encryption)=[^,\s]*,?", "", ud[0]).replace(",,", ",").rstrip(",")
assert new_ud.count(",checkpoint=block") == 1, "checkpoint flag"
new_ud = new_ud.replace(",checkpoint=block", "")
old_fs = " /data ext4 rw,nodev,noatime,nosuid,errors=panic "
assert new_ud.count(old_fs) == 1, "userdata ext4 options"
new_ud = new_ud.replace(old_fs, " /data f2fs nodev,noatime,nosuid,discard ")
ft = ft.replace(ud[0], new_ud)
ft = "\n".join(l for l in ft.splitlines() if "voldmanaged=usb" not in l) + "\n"
fst.write_text(ft)
bp = tree / "build.prop"
bp.write_text(bp.read_text().rstrip("\n") + "\n# KANO debug: serial console shell\nro.debuggable=1\n")
# Experiment A: drm_hwcomposer colour pipeline, HDR and log gamma LUT off; SF treats the panel as sRGB SDR.
bt = bp.read_text()
for k, old, new in [
    ("vendor.hwc.drm.enable_color_pipeline", "true", "false"),
    ("vendor.hwc.drm.force_color_mode", "13", "0"),
    ("vendor.hwc.drm.persistent_hdr", "true", "false"),
    ("vendor.hwc.use_log_gamma_lut", "true", "false"),
    ("ro.surface_flinger.has_wide_color_display", "true", "false"),
    ("ro.surface_flinger.has_HDR_display", "true", "false"),
]:
    line = f"{k}={old}"
    if bt.splitlines().count(line) != 1 or sum(1 for l in bt.splitlines() if l.startswith(k + "=")) != 1:
        raise SystemExit("prop not exactly once: " + line)
    bt = bt.replace("\n" + line + "\n", "\n" + f"{k}={new}" + "\n", 1)
bp.write_text(bt)
# drm_hwcomposer: every layer goes through SurfaceFlinger client composition (linear client target).
bt = bp.read_text()
if any(l.startswith("vendor.hwc.backend_override=") for l in bt.splitlines()):
    raise SystemExit("backend_override already set")
bt = bt.rstrip("\n") + "\n# KANO: xe faults scanning Y-tiled buffers through DPT on ADL-P; compose everything into the client target\nvendor.hwc.backend_override=client\n"
bp.write_text(bt)
print("hwc backend:", [l for l in bt.splitlines() if "backend_override" in l])
print("colour props:", [l for l in bt.splitlines() if "color" in l or "HDR" in l or "gamma" in l])
# No cppreopts: copying system_other odex to /data took 540 s on the stick and pushed PMS past the watchdog.
bt = bp.read_text()
if bt.splitlines().count("ro.cp_system_other_odex=1") != 1:
    raise SystemExit("cp_system_other_odex not exactly once")
bt = bt.replace("\nro.cp_system_other_odex=1\n", "\nro.cp_system_other_odex=0\n", 1)
bp.write_text(bt)
# Serial logcat. init compiles the split policy when precompiled_sepolicy is missing ("No precompiled sepolicy").
pre = tree / "etc/selinux/precompiled_sepolicy"
if not pre.is_file():
    raise SystemExit("precompiled_sepolicy missing")
pre.unlink()
cil = tree / "etc/selinux/vendor_sepolicy.cil"
cil.write_text(cil.read_text().rstrip("\n") + "\n" + """;; KANO serial logcat: a permissive vendor domain for /vendor/etc/kano-logcat.sh (run by /system/bin/sh).
;; Only init (enforcing) needs rules; what kano_kmsg_logcat itself does is allowed by typepermissive,
;; so the Treble exec/entrypoint neverallows stay clean (checked with secilc without -N).
(type kano_kmsg_logcat)
(roletype object_r kano_kmsg_logcat)
(typeattributeset domain (kano_kmsg_logcat))
(typepermissive kano_kmsg_logcat)
(allow init_202604 kano_kmsg_logcat (process (transition siginh rlimitinh)))
(dontaudit init_202604 kano_kmsg_logcat (process (noatsecure)))
;; KANO boot fix: one setprop at post-fs-data. vold would otherwise call the boot-control HAL's markBootSuccessful,
;; which runs crossystem against firmware NVRAM (blocked here on purpose) and reboots with 'Checkpoint commit failed'.
(type kano_bootfix)
(roletype object_r kano_bootfix)
(typeattributeset domain (kano_bootfix))
(typepermissive kano_bootfix)
(allow init_202604 kano_bootfix (process (transition siginh rlimitinh)))
(dontaudit init_202604 kano_bootfix (process (noatsecure)))
;; cmd/dumpsys hand their stdio fds to the service: let system_server and surfaceflinger use the kano debug domain's fds and pipes
(allow system_server_202604 kano_kmsg_logcat (fd (use)))
(allow system_server_202604 kano_kmsg_logcat (fifo_file (read write getattr ioctl append)))
(allow surfaceflinger_202604 kano_kmsg_logcat (fd (use)))
(allow surfaceflinger_202604 kano_kmsg_logcat (fifo_file (read write getattr ioctl append)))
""")
(tree / "etc/kano-logcat.sh").write_text("""#!/system/bin/sh
# Copy a filtered logcat to the kernel log (KANO serial console, 115200 baud): one kmsg record per line, at most ~30 lines/s.
exec 3>/dev/kmsg
while true; do
  /system/bin/logcat -b main,system,crash -v time '*:E' SystemServer:I SystemServerTiming:I ActivityManager:I PackageManager:W Watchdog:I AndroidRuntime:I SystemServiceManager:I BootReceiver:I DEBUG:S cutils-trace:S prefetch:S timberslide:S DesktopChargeControlHal:S gscd:S secretkeeper-hal-trusty:S SystemServiceRegistry:S 2>&1 |
  while IFS= read -r line; do
    printf '<14>kano-logcat: %s\\n' "$line" >&3
    sleep 0.03
  done
  sleep 1
done
""")
(tree / "etc/init/kano-logcat.rc").write_text("""# KANO: filtered logcat on the serial console (see /vendor/etc/kano-logcat.sh)
service kano_logcat /system/bin/sh /vendor/etc/kano-logcat.sh
    class core
    user root
    group log
    seclabel u:r:kano_kmsg_logcat:s0
""")
for f in ("etc/kano-logcat.sh", "etc/init/kano-logcat.rc"):
    (tree / f).chmod(0o644)
    vmap[f] = dict(vmap["etc/init/fwupd.rc"])
# Daemons that need the Cr50 GSC, the ChromeOS EC or the Trusty secretkeeper port crash every few seconds here.
sk = tree / "etc/init/android.hardware.security.secretkeeper.trusty.desktop.rc"
st = sk.read_text()
iface = "    interface aidl android.hardware.security.secretkeeper.ISecretkeeper/default\n"
if st.count(iface) != 1 or "    class hal\n" not in st:
    raise SystemExit("secretkeeper rc not as expected")
st = st.replace(iface, "    # KANO: no Trusty secretkeeper port without the protected VM; not started, no lazy interface start\n", 1)
st = st.replace("    class hal\n", "    class hal\n    disabled\n", 1)
sk.write_text(st)
print("secretkeeper rc:", st)
cc = tree / "etc/init/hal_desktop_charge_control_default.rc"
ct = cc.read_text()
if ct.count("    class hal\n") != 1 or "disabled" in ct:
    raise SystemExit("charge control rc not as expected")
cc.write_text(ct.replace("    class hal\n", "    class hal\n    # KANO: needs the ChromeOS EC (cros_ec is blacklisted)\n    disabled\n", 1))
print("charge control rc:", cc.read_text())
# Weaver and Secretkeeper only start with a protected VM, which the KANO lacks; undeclare them so waitForDeclaredService returns null.
for f in ("etc/vintf/manifest/android.hardware.weaver-service.android-desktop.xml",
          "etc/vintf/manifest/android.hardware.security.secretkeeper.trusty.xml"):
    x = tree / f
    if not x.is_file():
        raise SystemExit("missing " + f)
    x.unlink()
(tree / "etc/init/kano-bootfix.rc").write_text("""# KANO: vold treats every boot as a checkpoint boot and its commit would call the boot-control HAL's
# markBootSuccessful, which runs crossystem (firmware NVRAM, deliberately unreachable here) and fails, rebooting
# the device. Tell vold not to commit; nothing on this image uses checkpoints (/data is f2fs without checkpoint).
service kano_bootfix /system/bin/setprop persist.vold.dont_commit_checkpoint 1
    oneshot
    disabled
    user root
    seclabel u:r:kano_bootfix:s0

on post-fs-data
    start kano_bootfix
""")
(tree / "etc/init/kano-bootfix.rc").chmod(0o644)
vmap["etc/init/kano-bootfix.rc"] = dict(vmap["etc/init/fwupd.rc"])
(tree / "etc/kano-diag.sh").write_text("""#!/system/bin/sh
# KANO debug: dmesg/logcat/display/input/audio/Wi-Fi snapshots on /data every 20 s (for boots whose serial console dies).
D=/data/kano-diag
mkdir -p $D
n=0
snap() { f=$1; shift; timeout 20 "$@" < /dev/null 2>&1 | cat > $D/$f.tmp; mv $D/$f.tmp $D/$f; }
while true; do
  snap dmesg.txt /system/bin/dmesg
  snap logcat.txt /system/bin/logcat -d -b main,system,crash -v time '*:W'
  snap sf-displays.txt /system/bin/dumpsys SurfaceFlinger --display-id
  snap display.txt /system/bin/dumpsys display
  snap wifi-status.txt /system/bin/cmd wifi status
  { ls -l /dev/dri /sys/class/drm; for c in /sys/class/drm/card*-*; do echo "$c status=$(cat $c/status) enabled=$(cat $c/enabled) dpms=$(cat $c/dpms)"; head -n 3 $c/modes; done
    for b in /sys/class/backlight/*; do echo "$b brightness=$(cat $b/brightness) actual=$(cat $b/actual_brightness) max=$(cat $b/max_brightness) bl_power=$(cat $b/bl_power)"; done; } > $D/drm.txt 2>&1
  cat /proc/bus/input/devices > $D/input.txt 2>&1
  cat /proc/asound/cards > $D/asound.txt 2>&1
  ip addr > $D/ip.txt 2>&1
  for d in /sys/bus/pci/devices/0000:00:1e.0 /sys/bus/pci/devices/0000:00:15.*  /sys/bus/pci/devices/0000:00:19.*; do echo "$d -> $(readlink $d/driver)"; done > $D/lpss.txt 2>&1
  n=$((n + 1)); echo "$n $(cat /proc/uptime)" > $D/heartbeat.txt
  sync
  sleep 20
done
""")
(tree / "etc/init/kano-diag.rc").write_text("""# KANO debug: dmesg/logcat snapshots in /data/kano-diag (see /vendor/etc/kano-diag.sh)
service kano_diag /system/bin/sh /vendor/etc/kano-diag.sh
    class late_start
    user root
    group log
    seclabel u:r:kano_kmsg_logcat:s0
""")
for f in ("etc/kano-diag.sh", "etc/init/kano-diag.rc"):
    (tree / f).chmod(0o644)
    vmap[f] = dict(vmap["etc/init/fwupd.rc"])
# KANO device firmware (AX211 Wi-Fi/BT, SOF DSP + topology). Directories copy the firmware/i915 directory metadata.
for d in ("firmware/intel/sof", "firmware/intel/sof/community", "firmware/intel/sof-tplg"):
    (tree / d).mkdir(exist_ok=True)
    (tree / d).chmod(0o755)
    vmap[d] = dict(vmap["firmware/i915"])
for rel in ("iwlwifi-so-a0-gf-a0-89.ucode", "iwlwifi-so-a0-gf-a0.pnvm", "intel/ibt-0040-0041.sfi", "intel/ibt-0040-0041.ddc",
            "intel/sof/community/sof-adl.ri", "intel/sof-tplg/sof-adl-max98373-nau8825.tplg"):
    dst = tree / "firmware" / rel
    if dst.exists():
        raise SystemExit("already present: " + rel)
    shutil.copyfile(tmp / "kfw" / rel, dst)
    dst.chmod(0o644)
    vmap["firmware/" + rel] = dict(vmap["firmware/i915/xe2lpd_dmc.bin"])
(tree / "etc/init/kano-hw.rc").write_text("""# KANO: the AX211 needs the iwlmvm op-mode (the image only loads iwlmld, for the lapis BE200/BE211).
on early-boot
    exec_background u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/modules iwlmvm

# KANO: join the Wi-Fi network described in /data/kano-wifi.conf (written by the maintenance OS, never in this image).
service kano_wifi /system/bin/sh /vendor/etc/kano-wifi.sh
    oneshot
    disabled
    user root
    seclabel u:r:kano_kmsg_logcat:s0

on property:sys.boot_completed=1
    start kano_wifi
""")
(tree / "etc/kano-wifi.sh").write_text("""#!/system/bin/sh
# KANO: /data/kano-wifi.conf holds two lines, SSID and WPA2 passphrase (root 0600, written by the maintenance OS).
# cmd passes its stdio fds to system_server, so always pipe through cat (the policy lets system_server use our pipes).
C=/data/kano-wifi.conf
D=/data/kano-diag
mkdir -p $D
[ -r $C ] || { echo "no $C" > $D/wifi.txt; exit 0; }
SSID=$(sed -n 1p $C); PSK=$(sed -n 2p $C)
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  timeout 20 /system/bin/cmd wifi set-wifi-enabled enabled < /dev/null 2>&1 | cat > /dev/null
  timeout 30 /system/bin/cmd wifi connect-network "$SSID" wpa2 "$PSK" < /dev/null 2>&1 | cat > $D/wifi-connect.txt
  sleep 15
  timeout 20 /system/bin/cmd wifi status < /dev/null 2>&1 | cat > $D/wifi.txt
  grep -q -i "connected to" $D/wifi.txt && exit 0
done
exit 1
""")
for f in ("etc/init/kano-hw.rc", "etc/kano-wifi.sh"):
    (tree / f).chmod(0o644)
    vmap[f] = dict(vmap["etc/init/fwupd.rc"])
# (safe16's diag additions are part of the safe19 kano-diag.sh above)
# Remove every firmware updater / flash tool from vendor (touch I2C-HID and Wi-Fi are live now; "disabled" is not enough).
REMOVE = ["bin/fwupd-binder", "bin/fwupd_blocking_update", "bin/fwupdmgr", "bin/fw-updater-wrapper", "etc/init/fwupd.rc",
          "etc/fwupd", "etc/pki/fwupd", "etc/pki/fwupd-metadata", "usr/share/fwupd",
          "bin/flashrom", "lib64/libflashrom.so", "etc/init/flashrom.rc",
          "bin/futility", "bin/hw/vendor.google.desktop.futility-executor", "etc/init/futility-executor.rc",
          "etc/vintf/manifest/vendor.google.desktop.futility-executor.xml",
          "bin/ectool", "bin/crossystem", "firmware/ap-ec-fw.zip"]
removed = []
for rel in REMOVE:
    p = tree / rel
    if not (p.exists() or p.is_symlink()):
        raise SystemExit("expected to remove, missing: " + rel)
    if p.is_dir() and not p.is_symlink():
        for sub in sorted(p.rglob("*"), reverse=True):
            removed.append(str(sub.relative_to(tree)))
            sub.unlink() if (sub.is_file() or sub.is_symlink()) else sub.rmdir()
        p.rmdir()
    else:
        p.unlink()
    removed.append(rel)
(tmp / "removed.txt").write_text("\n".join(sorted(removed)) + "\n")
print("removed", len(removed), "paths")
print("kano logcat label:", vmap["etc/init/kano-logcat.rc"].get("selinux"))
print("userdata:", new_ud)
# --- safe19 additions (inserted before rebuild.write_configs in build-safe19.sh) ---
# Serial logcat only on the serial-debug entry: the 115200-baud console is synchronous and slows everything else.
(tree / "etc/kano-logcat.sh").write_text("""#!/system/bin/sh
# Copy a filtered logcat to the kernel log (KANO serial console, 115200 baud): one kmsg record per line, at most ~30 lines/s.
# Only when booted from the serial-debug entry (keep_bootcon); otherwise idle so init does not restart us.
if ! grep -q keep_bootcon /proc/cmdline; then
  while true; do sleep 86400; done
fi
exec 3>/dev/kmsg
while true; do
  /system/bin/logcat -b main,system,crash -v time '*:E' SystemServer:I SystemServerTiming:I ActivityManager:I PackageManager:W Watchdog:I AndroidRuntime:I SystemServiceManager:I BootReceiver:I dras:I DEBUG:S cutils-trace:S prefetch:S timberslide:S DesktopChargeControlHal:S gscd:S secretkeeper-hal-trusty:S SystemServiceRegistry:S StatsCompanionService:S 2>&1 |
  while IFS= read -r line; do
    printf '<14>kano-logcat: %s\\n' "$line" >&3
    sleep 0.03
  done
  sleep 1
done
""")
# Diag: cheap state every 60 s, heavy dumps every third round. Readers have their own timeout: dumpsys hands the
# pipe to the service, so a stuck service would otherwise keep 'cat' (and the whole loop) waiting forever.
(tree / "etc/kano-diag.sh").write_text("""#!/system/bin/sh
# KANO debug: snapshots in /data/kano-diag (read later from the maintenance OS).
D=/data/kano-diag
mkdir -p $D
n=0
snap() { f=$1; shift; timeout -k 5 20 "$@" < /dev/null 2>&1 | timeout -k 5 30 cat > $D/$f.tmp; mv $D/$f.tmp $D/$f; }
iotop() {
  for p in /proc/[0-9]*; do
    w=$(sed -n 's/^write_bytes: //p' $p/io 2>/dev/null); r=$(sed -n 's/^read_bytes: //p' $p/io 2>/dev/null)
    [ -n "$w" ] && echo "$w $r ${p#/proc/} $(cat $p/comm 2>/dev/null)"
  done | sort -rn | head -n 30
}
while true; do
  { cat /proc/uptime /proc/loadavg; grep -E " sd[a-z]+ " /proc/diskstats; grep -E "^(cpu |procs_)" /proc/stat; head -n 4 /proc/meminfo; } > $D/load.txt 2>&1
  { echo "# write_bytes read_bytes pid comm (cumulative)"; iotop; } > $D/io-top.txt 2>&1
  { ls -l /dev/dri /sys/class/drm; for c in /sys/class/drm/card*-*; do echo "$c status=$(cat $c/status) enabled=$(cat $c/enabled) dpms=$(cat $c/dpms)"; head -n 3 $c/modes; done
    for b in /sys/class/backlight/* /sys/class/leds/*; do echo "$b brightness=$(cat $b/brightness) max=$(cat $b/max_brightness)"; done; } > $D/drm.txt 2>&1
  cat /proc/bus/input/devices > $D/input.txt 2>&1
  { cat /proc/asound/cards /proc/asound/pcm; ls -l /dev/snd; } > $D/asound.txt 2>&1
  ip addr > $D/ip.txt 2>&1
  for d in /sys/bus/pci/devices/0000:00:1e.0 /sys/bus/pci/devices/0000:00:15.* /sys/bus/pci/devices/0000:00:19.*; do echo "$d -> $(readlink $d/driver)"; done > $D/lpss.txt 2>&1
  if [ $((n % 3)) -eq 0 ]; then
    snap dmesg.txt /system/bin/dmesg
    snap logcat.txt /system/bin/logcat -d -b main,system,crash -v time '*:W'
    snap audio-log.txt /system/bin/logcat -d -b main,system -v time -s dras:V AudioFlinger:V AudioPolicyService:V APM_AudioPolicyManager:V AudioService:I
    snap display.txt /system/bin/dumpsys display
    snap wifi-status.txt /system/bin/cmd wifi status
    snap input-dump.txt /system/bin/dumpsys input
    snap audio-hal.txt /system/bin/dumpsys android.hardware.audio.core.IModule/default
    snap audioflinger.txt /system/bin/dumpsys media.audio_flinger
    snap props.txt /system/bin/getprop
  fi
  n=$((n + 1)); echo "$n $(cat /proc/uptime)" > $D/heartbeat.txt
  sync
  sleep 60
done
""")
# KANO audio for the desktop audio HAL (DRAS): card sof-nau8825 (NAU8825 headset + 2x MAX98373, SOF IPC3).
# Translated from ChromeOS board-overlays overlay-brya/chromeos-base/chromeos-bsp-brya/files/kano/audio
# (ucm-config/sof-nau8825.kano/HiFi.conf, cras-config/sof-nau8825.card_settings; read 2026-10-12).
# The DSM/DTS tuning blobs (cset-tlv) are not public and are left out; PCM ids match /proc/asound/pcm on the KANO.
import re as _re2
src_audio = Path(os.environ["GBOS_AUDIO_SRC"])
lic = (tree / "etc/audio/sof-soundwire_cs42l43_cs35l56/audio_config.xml").read_text().split("<!--", 1)[1].split("-->", 1)[0]
adir = tree / "etc/audio/sof-nau8825_kano"
if adir.exists():
    raise SystemExit("kano audio dir already present")
adir.mkdir(); adir.chmod(0o755)
vmap["etc/audio/sof-nau8825_kano"] = dict(vmap["etc/audio/sof-soundwire_cs42l43_cs35l56"])
hdmi = "".join(f"""		<HDMI{i}>
			<PlaybackPCM>hw:sofnau8825,{i + 1}</PlaybackPCM>
			<JackDev>sof-nau8825 HDMI/DP,pcm={i + 1}</JackDev>
			<PlaybackChannels>2</PlaybackChannels>
		</HDMI{i}>
""" for i in (1, 2, 3, 4))
(adir / "audio_config.xml").write_text(f"""<?xml version='1.0' encoding='utf-8'?>
<audio_config>
	<!--{lic}-->
	<!-- KANO (Acer Chromebook Spin 714, brya/kano): from ChromeOS ucm-config/sof-nau8825.kano/HiFi.conf -->
	<SoundCardName>hw:sofnau8825</SoundCardName>
	<HiFi>
		<Speaker>
			<PlaybackPCM>hw:sofnau8825,0</PlaybackPCM>
		</Speaker>
		<Headphone>
			<PlaybackPCM>hw:sofnau8825,1</PlaybackPCM>
			<JackDev>sof-nau8825 Headset Jack</JackDev>
			<JackSwitch>2</JackSwitch>
		</Headphone>
		<InternalMic>
			<CapturePCM>hw:sofnau8825,99</CapturePCM>
			<CaptureChannelMap>0 1 -1 -1 -1 -1 -1 -1 -1 -1 -1</CaptureChannelMap>
			<IntrinsicSensitivity>-2600</IntrinsicSensitivity>
		</InternalMic>
		<Mic>
			<CapturePCM>hw:sofnau8825,1</CapturePCM>
			<CaptureMixerElem>Headset Mic</CaptureMixerElem>
			<JackDev>sof-nau8825 Headset Jack</JackDev>
		</Mic>
{hdmi}		<SCOLineOut>
			<PlaybackPCM>hw:sofnau8825,7</PlaybackPCM>
		</SCOLineOut>
		<SCOLineIn>
			<CapturePCM>hw:sofnau8825,7</CapturePCM>
		</SCOLineIn>
	</HiFi>
</audio_config>
""")
(adir / "mixer_paths.xml").write_text(f"""<?xml version='1.0' encoding='utf-8'?>
<mixer enum_mixer_numeric_fallback="true">
	<!--{lic}-->
	<!-- KANO: SectionVerb EnableSequence of sof-nau8825.kano/HiFi.conf (the cset-tlv tuning blobs are not public) -->
	<ctl name="Frontend PGA Volume" value="12" />
	<ctl name="Mic Volume" value="255" />
	<ctl name="Left DAI Sel Mux" value="Right" />
	<ctl name="Right DAI Sel Mux" value="Left" />
	<ctl name="Left Digital Volume" value="127" />
	<ctl name="Right Digital Volume" value="127" />
	<ctl name="Left FS Max Volume" value="6" />
	<ctl name="Right FS Max Volume" value="6" />
	<!-- device switches start off; each path turns its own on -->
	<ctl name="Left Spk Switch" value="0" />
	<ctl name="Right Spk Switch" value="0" />
	<ctl name="Left VI Sense Switch" value="0" />
	<ctl name="Right VI Sense Switch" value="0" />
	<ctl name="Headphone Jack Switch" value="0" />
	<ctl name="Headset Mic Switch" value="0" />
	<path name="Speaker">
		<ctl name="Left Spk Switch" value="1" />
		<ctl name="Right Spk Switch" value="1" />
		<ctl name="Left VI Sense Switch" value="1" />
		<ctl name="Right VI Sense Switch" value="1" />
	</path>
	<path name="Headphone">
		<ctl name="Headphone Jack Switch" value="1" />
	</path>
	<path name="InternalMic" />
	<path name="Mic">
		<ctl name="Headset Mic Switch" value="1" />
	</path>
	<path name="HDMI1" />
	<path name="HDMI2" />
	<path name="HDMI3" />
	<path name="HDMI4" />
	<path name="SCOLineOut" />
	<path name="SCOLineIn" />
</mixer>
""")
# Volume curves: keep lapis' point indices, take the dB values from kano's CRAS card_settings.
cs = (src_audio / "sof-nau8825.card_settings").read_text()
spk = {int(m.group(1)): int(m.group(2)) for m in _re2.finditer(r"db_at_(\d+) = (-?\d+)", cs.split("[Headphone]")[0])}
assert sorted(spk) == list(range(101)), "speaker curve"
hp = cs.split("[Headphone]")[1]
hp_step = int(_re2.search(r"volume_step = (\d+)", hp).group(1)); hp_max = int(_re2.search(r"max_volume = (-?\d+)", hp).group(1))
# Without kano's DSM speaker-protection model (not public) the speaker curve is lowered 4.75 dB: -10 dB at 100 %.
SPK_HEADROOM = 475
def kano_db(ref, pct):
    return spk[pct] - SPK_HEADROOM if ref == "Speaker" else hp_max - hp_step * (100 - pct)
vc = (tree / "etc/audio/sof-soundwire_cs42l43_cs35l56/volume_curves.xml").read_text()
def fix_ref(m):
    ref, body = m.group(1), m.group(2)
    pts = [int(p) for p in _re2.findall(r"<point>(\d+),-?\d+</point>", body)]
    if ref not in ("Speaker", "Headphone"):
        return m.group(0)
    top = max(pts)
    new = body
    for p in pts:
        new = _re2.sub(rf"<point>{p},-?\d+</point>", f"<point>{p},{kano_db(ref, round(p * 100 / top))}</point>", new, count=1)
    return f'<reference name="{ref}">{new}</reference>'
vc2, nref = _re2.subn(r'<reference name="([^"]+)">(.*?)</reference>', fix_ref, vc, flags=_re2.S)
assert nref >= 2 and vc2 != vc, "volume curves"
(adir / "volume_curves.xml").write_text(vc2)
for f in ("audio_config.xml", "mixer_paths.xml", "volume_curves.xml"):
    (adir / f).chmod(0o644)
    vmap["etc/audio/sof-nau8825_kano/" + f] = dict(vmap["etc/audio/sof-soundwire_cs42l43_cs35l56/audio_config.xml"])
# Point the default audio hal_config at the KANO card (lapis' own cs42l43 SKU file stays as it is).
hc = tree / "etc/data/hal_configs/audio_default.xml"
old_hc = "<soundcard>sof-soundwire</soundcard>\n  <audio-config-dir>sof-soundwire_cs42l43_cs35l56</audio-config-dir>"
if hc.read_text().count(old_hc) != 1:
    raise SystemExit("audio_default.xml not as expected")
hc.write_text(hc.read_text().replace(old_hc, "<soundcard>sof-nau8825</soundcard>\n  <audio-config-dir>sof-nau8825_kano</audio-config-dir>"))
import xml.dom.minidom as _md
for f in ("audio_config.xml", "mixer_paths.xml", "volume_curves.xml"):
    _md.parse(str(adir / f))
_md.parse(str(hc))
print("kano audio:", sorted(p.name for p in adir.iterdir()), hc.read_text())
# Keyboard backlight: on the KANO, ACPI GOOG0002 (\_SB_.CREC.KBLT) sits under the EC node, so cros_kbd_led_backlight
# registers /sys/devices/platform/GOOG0004:00/GOOG0002:00/leds/chromeos::kbd_backlight (lapis: platform/GOOG0002:00).
# Verified on the KANO maintenance OS 2026-10-12: brightness 0/100/0/50/100 read back through ACPI.
KB_OLD = "/sys/devices/platform/GOOG0002:00/leds"
KB_NEW = "/sys/devices/platform/GOOG0004:00/GOOG0002:00/leds"
idc = tree / "usr/idc/AT_Translated_Set_2_keyboard.idc"
it = idc.read_text()
if it.count("device.additionalSysfsLedsNode = " + KB_OLD + "/chromeos::kbd_backlight") != 1:
    raise SystemExit("keyboard idc not as expected")
idc.write_text(it.replace(KB_OLD, KB_NEW))
(tree / "etc/ueventd/ueventd.kano.rc").write_text("# KANO: keyboard backlight LED (GOOG0002 under the EC node GOOG0004)\n" + KB_NEW + "/* brightness 0660 system system\n")
(tree / "etc/ueventd/ueventd.kano.rc").chmod(0o644)
vmap["etc/ueventd/ueventd.kano.rc"] = dict(vmap["etc/ueventd/ueventd.fatcat.rc"])
cil2 = tree / "etc/selinux/vendor_sepolicy.cil"
ct2 = cil2.read_text()
gline = '(genfscon sysfs "/devices/platform/GOOG0002:00/leds" (u object_r sysfs_keyboard_backlight ((s0) (s0))))'
if ct2.count(gline) != 1:
    raise SystemExit("keyboard genfscon not as expected")
cil2.write_text(ct2.rstrip("\n") + "\n;; KANO keyboard backlight LED path (GOOG0002 under GOOG0004)\n" + gline.replace("/devices/platform/GOOG0002:00/leds", KB_NEW[4:]) + "\n")
print("kbd idc:", [l for l in idc.read_text().splitlines() if "LedsNode" in l])
print("kbd ueventd:", (tree / "etc/ueventd/ueventd.kano.rc").read_text())
# --- safe20 additions (inserted before rebuild.write_configs in build-safe20.sh, after the safe19 additions) ---
# One-shot audio self-test: 120 s after boot_completed, dras_tool (the audio HAL's own CLI, same audio_config.xml) plays a
# 1 kHz tone at -20 dBFS on the Speaker and records the InternalMic; outputs and --help texts go to /data/kano-diag/audiotest.
import math as _math, struct as _struct, wave as _wave
tone = tree / "etc/kano-tone-1k.wav"
w = _wave.open(str(tone), "wb"); w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
fr = bytearray()
for i in range(48000 * 3):
    s = int(0.1 * 32767 * _math.sin(2 * _math.pi * 1000 * i / 48000)) if 4800 <= i < 48000 * 3 - 4800 else 0
    fr += _struct.pack("<hh", s, s)
w.writeframes(bytes(fr)); w.close()
(tree / "etc/kano-audiotest.sh").write_text("""#!/system/bin/sh
# KANO: one-shot audio self-test (see kano-audiotest.rc). Plays /vendor/etc/kano-tone-1k.wav (1 kHz, -20 dBFS, 3 s).
D=/data/kano-diag/audiotest
mkdir -p $D
T=/apex/com.android.hardware.audio.desktop/bin/dras_tool
W=/vendor/etc/kano-tone-1k.wav
run() { n=$1; shift; echo "\\$ $* (uptime $(cut -d' ' -f1 /proc/uptime))" > $D/$n.txt; timeout -k 5 30 "$@" < /dev/null >> $D/$n.txt 2>&1; echo "rc=$?" >> $D/$n.txt; }
sleep 120
run help $T --help
run help-aplay $T aplay --help
run help-arecord $T arecord --help
run help-aplayrecord $T aplayrecord --help
run aplayrecord $T aplayrecord $W $D/rec-tone.wav --play-device Speaker --record-device InternalMic
sleep 3
run arecord $T arecord $D/rec-silence.wav --device InternalMic --duration-sec 3
sleep 3
run aplay1 $T aplay $W --device Speaker
sleep 2
run aplay2 $T aplay $W --play-device Speaker
sleep 2
run aplay3 $T aplay $W
ls -l $D > $D/ls.txt
""")
(tree / "etc/init/kano-audiotest.rc").write_text("""# KANO: one-shot audio self-test 120 s after boot (see /vendor/etc/kano-audiotest.sh)
service kano_audiotest /system/bin/sh /vendor/etc/kano-audiotest.sh
    oneshot
    disabled
    user root
    group audio
    seclabel u:r:kano_kmsg_logcat:s0

on property:sys.boot_completed=1
    start kano_audiotest
""")
for f in ("etc/kano-tone-1k.wav", "etc/kano-audiotest.sh", "etc/init/kano-audiotest.rc"):
    (tree / f).chmod(0o644)
    vmap[f] = dict(vmap["etc/init/fwupd.rc"])
# Diag: no global sync (it stalls for minutes behind the stick's write queue and froze the loop at 406 s).
dg = tree / "etc/kano-diag.sh"
dt = dg.read_text()
if dt.count("\n  sync\n  sleep 60\n") != 1:
    raise SystemExit("diag sync line not as expected")
dg.write_text(dt.replace("\n  sync\n  sleep 60\n", "\n  sleep 60\n"))
# dumpsys of the audio HAL / audioserver hand our pipe to them (FAILED_TRANSACTION before)
cil3 = tree / "etc/selinux/vendor_sepolicy.cil"
cil3.write_text(cil3.read_text().rstrip("\n") + """
;; KANO diag: dumpsys of the audio HAL and audioserver write into the kano debug domain's pipes
(allow hal_audio_default kano_kmsg_logcat (fd (use)))
(allow hal_audio_default kano_kmsg_logcat (fifo_file (read write getattr ioctl append)))
(allow audioserver_202604 kano_kmsg_logcat (fd (use)))
(allow audioserver_202604 kano_kmsg_logcat (fifo_file (read write getattr ioctl append)))
""")
print("audiotest:", (tree / "etc/init/kano-audiotest.rc").read_text().splitlines()[1], tone.stat().st_size)
# The small state files are written with dd conv=fsync (one fsync each), so a power cut leaves them readable.
dt = dg.read_text()
for old, new in [
    ('head -n 4 /proc/meminfo; } > $D/load.txt 2>&1', 'head -n 4 /proc/meminfo; } 2>&1 | dd of=$D/load.txt conv=fsync status=none'),
    ('iotop; } > $D/io-top.txt 2>&1', 'iotop; } 2>&1 | dd of=$D/io-top.txt conv=fsync status=none'),
    ('echo "$n $(cat /proc/uptime)" > $D/heartbeat.txt', 'echo "$n $(cat /proc/uptime)" | dd of=$D/heartbeat.txt conv=fsync status=none'),
]:
    if dt.count(old) != 1:
        raise SystemExit("diag line not as expected: " + old)
    dt = dt.replace(old, new)
dg.write_text(dt)
# --- safe21 additions (after the safe19/safe20 additions) ---
# timberslide (EC log reader) exits 1 every few seconds without cros_ec; as an "updatable" crasher it makes init run
# flags_health_check synchronously ~200x per boot. gscd aborts without the Cr50 TPM and writes a tombstone each time.
# Neither can work on the KANO (EC/TPM blocked on purpose); stop both from a vendor action (stop is not an override).
(tree / "etc/init/kano-services.rc").write_text("""# KANO: stop system services that need the blocked ChromeOS EC / Cr50 TPM (they only crash-loop here)
on boot
    stop timberslide
    stop android.system.desktop.security.gscd

on property:sys.boot_completed=1
    stop timberslide
    stop android.system.desktop.security.gscd
""")
(tree / "etc/init/kano-services.rc").chmod(0o644)
vmap["etc/init/kano-services.rc"] = dict(vmap["etc/init/fwupd.rc"])
# Slow stick: give system_server's watchdog 5 minutes (Settings.Global, applies once SettingsProvider is up and persists).
ws = tree / "etc/kano-wifi.sh"
wt = ws.read_text()
anchor_w = "[ -r $C ] || {"
if wt.count(anchor_w) != 1:
    raise SystemExit("kano-wifi.sh not as expected")
wt = wt.replace(anchor_w, """timeout 20 /system/bin/cmd settings put global system_server_watchdog_timeout_ms 300000 < /dev/null 2>&1 | cat > $D/watchdog-setting.txt
timeout 20 /system/bin/cmd settings get global system_server_watchdog_timeout_ms < /dev/null 2>&1 | cat >> $D/watchdog-setting.txt
""" + anchor_w, 1)
ws.write_text(wt)
# Audio self-test only when the maintenance OS asked for it (/data/kano-audiotest.enable), so normal boots stay silent.
at = tree / "etc/kano-audiotest.sh"
t2 = at.read_text()
if t2.count("mkdir -p $D\n") != 1:
    raise SystemExit("audiotest script not as expected")
at.write_text(t2.replace("mkdir -p $D\n", "[ -e /data/kano-audiotest.enable ] || exit 0\nrm -f /data/kano-audiotest.enable\nmkdir -p $D\n", 1))
# More hardware state in the heavy diag round.
dg2 = tree / "etc/kano-diag.sh"
d2 = dg2.read_text()
anchor_d = "    snap props.txt /system/bin/getprop\n"
if d2.count(anchor_d) != 1:
    raise SystemExit("diag props line not as expected")
d2 = d2.replace(anchor_d, anchor_d + """    snap battery.txt /system/bin/dumpsys battery
    snap bluetooth.txt /system/bin/dumpsys bluetooth_manager
    snap camera.txt /system/bin/dumpsys media.camera
    snap sensors.txt /system/bin/dumpsys sensorservice
    snap power.txt /system/bin/dumpsys power
    /system/bin/getprop | grep -E 'init.svc.(timberslide|android.system.desktop.security.gscd)|updatable_crashing' > $D/services.txt 2>&1
""", 1)
dg2.write_text(d2)
print("kano-services.rc:", (tree / "etc/init/kano-services.rc").read_text())
# --- safe22 additions (after the safe19..safe21 additions) ---
# Key-only adb over TCP 5555 for the remote harness: the GBOS_ADB_PUBKEY public key goes to /data/misc/adb/adb_keys
# (ro.adb.secure stays 1, so only that key can connect). Lets the harness read state live and shut down cleanly
# instead of cutting power (which left the last diag files zero-filled on f2fs).
(tree / "etc/kano-adb_keys").write_text(Path(os.environ["GBOS_ADB_PUBKEY"]).read_text() if os.environ.get("GBOS_ADB_PUBKEY") else "")
(tree / "etc/kano-adb.sh").write_text("""#!/system/bin/sh
# KANO: install the harness adb key and start adbd on TCP 5555 (see /vendor/etc/init/kano-adb.rc).
D=/data/kano-diag
mkdir -p $D /data/misc/adb
cat /vendor/etc/kano-adb_keys > /data/misc/adb/adb_keys
chown system:shell /data/misc/adb/adb_keys
chmod 0640 /data/misc/adb/adb_keys
restorecon /data/misc/adb/adb_keys
for k in development_settings_enabled adb_enabled; do
  timeout 20 /system/bin/cmd settings put global $k 1 < /dev/null 2>&1 | cat
done
setprop service.adb.tcp.port 5555
sleep 5
[ "$(getprop init.svc.adbd)" = running ] || setprop ctl.start adbd
sleep 5
{ getprop init.svc.adbd; getprop service.adb.tcp.port; ls -lZ /data/misc/adb; } > $D/adb.txt 2>&1
""")
(tree / "etc/init/kano-adb.rc").write_text("""# KANO: key-only adb over TCP for the remote harness (see /vendor/etc/kano-adb.sh)
service kano_adb /system/bin/sh /vendor/etc/kano-adb.sh
    oneshot
    disabled
    user root
    seclabel u:r:kano_kmsg_logcat:s0

on property:sys.boot_completed=1
    start kano_adb
""")
for f in ("etc/kano-adb_keys", "etc/kano-adb.sh", "etc/init/kano-adb.rc"):
    (tree / f).chmod(0o644)
    vmap[f] = dict(vmap["etc/init/fwupd.rc"])
# Heavy diag files are written with one fsync each as well.
dg3 = tree / "etc/kano-diag.sh"
d3 = dg3.read_text()
old_snap = '| timeout -k 5 30 cat > $D/$f.tmp; mv $D/$f.tmp $D/$f; }'
if d3.count(old_snap) != 1:
    raise SystemExit("diag snap() not as expected")
dg3.write_text(d3.replace(old_snap, '| timeout -k 5 30 dd of=$D/$f.tmp conv=fsync status=none; mv $D/$f.tmp $D/$f; }'))
print("kano-adb key:", ((tree / "etc/kano-adb_keys").read_text().split() or ["(none)"])[-1])
# --- safe23 additions (after the safe19..safe22 additions) ---
# system build.prop pins service.adb.listen_addrs=tcp:localhost:5555 (lapis' SSH forwarder), so adbd never listened on
# the LAN. Point it at tcp:5555 (key auth unchanged) and restart adbd.
ka = tree / "etc/kano-adb.sh"
kt = ka.read_text()
old_a = "setprop service.adb.tcp.port 5555\nsleep 5\n[ \"$(getprop init.svc.adbd)\" = running ] || setprop ctl.start adbd\n"
if kt.count(old_a) != 1:
    raise SystemExit("kano-adb.sh not as expected")
ka.write_text(kt.replace(old_a, """setprop service.adb.listen_addrs tcp:5555
setprop service.adb.tcp.port 5555
setprop ctl.stop adbd
sleep 3
setprop ctl.start adbd
""", 1))
ka.write_text(ka.read_text().replace("{ getprop init.svc.adbd; getprop service.adb.tcp.port;", "{ getprop init.svc.adbd; getprop service.adb.listen_addrs; getprop service.adb.tcp.port;"))
# udsattestation asks servicemanager to start the (stopped) GSC HAL once a second; it cannot work without the Cr50 either.
ks = tree / "etc/init/kano-services.rc"
st = ks.read_text()
if st.count("    stop android.system.desktop.security.gscd\n") != 2:
    raise SystemExit("kano-services.rc not as expected")
ks.write_text(st.replace("    stop android.system.desktop.security.gscd\n",
                         "    stop android.system.desktop.security.gscd\n    stop android.system.desktop.security.udsattestation\n"))
print("kano-adb.sh:", ka.read_text())
print("kano-services.rc:", ks.read_text())
# --- safe24 additions (after the safe19..safe23 additions) ---
# adb now gives live access, so the on-disk diag shrinks to what helps when adb is down: load/io-top/heartbeat every
# 120 s (heartbeat fsync'd), dmesg + logcat once at ~4 min. Less I/O on the slow stick (Bluetooth aborts after 2 s stalls).
(tree / "etc/kano-diag.sh").write_text("""#!/system/bin/sh
# KANO debug (light): state in /data/kano-diag for boots where adb is not reachable; read from the maintenance OS.
D=/data/kano-diag
mkdir -p $D
n=0
snap() { f=$1; shift; timeout -k 5 20 "$@" < /dev/null 2>&1 | timeout -k 5 30 dd of=$D/$f.tmp conv=fsync status=none; mv $D/$f.tmp $D/$f; }
while true; do
  { cat /proc/uptime /proc/loadavg; grep -E " sd[a-z]+ " /proc/diskstats; grep -E "^(cpu |procs_)" /proc/stat; } > $D/load.txt 2>&1
  ip addr show wlan0 > $D/ip.txt 2>&1
  if [ $n -eq 2 ]; then
    snap dmesg.txt /system/bin/dmesg
    snap logcat.txt /system/bin/logcat -d -b main,system,crash -v time '*:W'
  fi
  n=$((n + 1)); echo "$n $(cat /proc/uptime)" | dd of=$D/heartbeat.txt conv=fsync status=none
  sleep 120
done
""")
print("diag light:", len((tree / "etc/kano-diag.sh").read_text()), "bytes")
# --- safe25 additions (after the safe19..safe24 additions) ---
# Power button: lapis excludes the ACPI "Power Button" input device because its power key arrives through cros_ec_buttons
# (cros_ec_keyb). On the KANO cros_ec_keyb is blacklisted and the i8042 keyboard carries no power key (EC powerbtn left
# Android asleep), so the ACPI device is the only source: stop excluding it.
ex = tree / "etc/excluded-input-devices.xml"
et = ex.read_text()
if et.count('<device name="Power Button"/>') != 1:
    raise SystemExit("excluded-input-devices.xml not as expected")
ex.write_text(et.replace('    <device name="Power Button"/>\n',
                         '    <!-- KANO: cros_ec_keyb (cros_ec_buttons) is blacklisted, so the ACPI Power Button is the only power key -->\n', 1))
print("excluded-input-devices:", ex.read_text())
# adb over TCP is opt-in: without GBOS_ADB_PUBKEY the three adb files are not part of the image.
if not os.environ.get("GBOS_ADB_PUBKEY"):
    for f in ("etc/kano-adb_keys", "etc/kano-adb.sh", "etc/init/kano-adb.rc"):
        (tree / f).unlink()
        vmap.pop(f, None)
    # none of this project's other vendor files (and not build.prop) may start adbd or open it to the network
    for f in [tree / "build.prop", *tree.glob("etc/init/kano-*.rc"), *tree.glob("etc/kano-*.sh")]:
        if f.is_file():
            t = f.read_bytes()
            if b"service.adb.listen_addrs" in t or b"kano-adb" in t or b"ro.adb.secure=0" in t:
                raise SystemExit(f"adb left enabled in {f.relative_to(tree)}")
    print("adb: not included (GBOS_ADB_PUBKEY not set)")
n = rebuild.write_configs(
    "/vendor",
    tree,
    vmap,
    tmp / "vendor_file_contexts",
    tmp / "vendor_fs_config",
)
print(f"file_contexts {n}")
PY

echo "mkfs vendor"
rm -f "$TMP/vendor.erofs"
ot mkfs.erofs \
    -z lz4hc,level=9 \
    -b 4096 \
    -T 1230768000 \
    -U "$VENDOR_UUID" \
    --mount-point /vendor \
    --fs-config-file "$TMP/vendor_fs_config" \
    --file-contexts "$TMP/vendor_file_contexts" \
    "$TMP/vendor.erofs" \
    "$TMP/vendor-tree"

python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import rebuild; u = rebuild.fs_uuid(sys.argv[2]); print("Filesystem UUID:", u); sys.exit(u != sys.argv[3])' \
    "$HERE/lib" "$TMP/vendor.erofs" "$VENDOR_UUID"

echo "compare extracted trees"
rm -rf "$TMP/vendor-tree-base" "$TMP/vendor-tree-new"
mkdir -p "$TMP/vendor-tree-base" "$TMP/vendor-tree-new"
ot fsck.erofs --extract="$TMP/vendor-tree-base" --overwrite --preserve-perms "$TMP/vendor.erofs.src"
ot fsck.erofs --extract="$TMP/vendor-tree-new" --overwrite --preserve-perms "$TMP/vendor.erofs"

python3 - "$TMP" "$HERE/lib" << 'PY'
import hashlib, os, sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
import rebuild

tmp = Path(sys.argv[1])

def index(root: Path):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        real_dirs = []
        for name in dirnames:
            p = Path(dirpath) / name
            rel = name if rel_dir == "" else rel_dir + "/" + name
            if p.is_symlink():
                out[rel] = ("symlink", os.readlink(p))
            else:
                real_dirs.append(name)
                out[rel] = ("dir", None)
        dirnames[:] = real_dirs
        for name in filenames:
            p = Path(dirpath) / name
            rel = name if rel_dir == "" else rel_dir + "/" + name
            if p.is_symlink():
                out[rel] = ("symlink", os.readlink(p))
            elif p.is_file():
                out[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest())
            else:
                raise SystemExit("special file " + rel)
    return out

old = index(tmp / "vendor-tree-base")
new = index(tmp / "vendor-tree-new")
only_old = sorted(set(old) - set(new))
only_new = sorted(set(new) - set(old))
changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
print("only in kano-real vendor:", only_old)
print("only in rebuilt vendor:", only_new)
print("content differs:", changed)
expect_old = sorted(["etc/init/desktop_kernel_max_rollforward_boot.rc", "etc/selinux/precompiled_sepolicy", "etc/vintf/manifest/android.hardware.security.secretkeeper.trusty.xml", "etc/vintf/manifest/android.hardware.weaver-service.android-desktop.xml"] + (tmp / "removed.txt").read_text().split())
expect_changed = ["build.prop", "etc/data/hal_configs/audio_default.xml", "etc/excluded-input-devices.xml", "etc/fstab.android-desktop", "etc/init/android.hardware.security.secretkeeper.trusty.desktop.rc", "etc/init/hal_desktop_charge_control_default.rc", "etc/selinux/vendor_sepolicy.cil", "usr/idc/AT_Translated_Set_2_keyboard.idc"]
expect_new = sorted(["etc/kano-adb_keys", "etc/kano-adb.sh", "etc/init/kano-adb.rc", "etc/init/kano-services.rc", "etc/kano-tone-1k.wav", "etc/kano-audiotest.sh", "etc/init/kano-audiotest.rc", "etc/audio/sof-nau8825_kano", "etc/audio/sof-nau8825_kano/audio_config.xml", "etc/audio/sof-nau8825_kano/mixer_paths.xml", "etc/audio/sof-nau8825_kano/volume_curves.xml", "etc/ueventd/ueventd.kano.rc", "etc/init/kano-bootfix.rc", "etc/init/kano-diag.rc", "etc/init/kano-hw.rc", "etc/init/kano-logcat.rc", "etc/kano-diag.sh", "etc/kano-logcat.sh", "etc/kano-wifi.sh", "firmware/i915/adlp_dmc.bin", "firmware/i915/adlp_guc_70.bin", "firmware/i915/tgl_huc.bin", "firmware/iwlwifi-so-a0-gf-a0-89.ucode", "firmware/iwlwifi-so-a0-gf-a0.pnvm", "firmware/intel/ibt-0040-0041.sfi", "firmware/intel/ibt-0040-0041.ddc", "firmware/intel/sof", "firmware/intel/sof/community", "firmware/intel/sof/community/sof-adl.ri", "firmware/intel/sof-tplg", "firmware/intel/sof-tplg/sof-adl-max98373-nau8825.tplg"])
if not os.environ.get("GBOS_ADB_PUBKEY"):
    expect_new = sorted(x for x in expect_new if "kano-adb" not in x)
if only_old != expect_old or only_new != expect_new or changed != expect_changed:
    raise SystemExit("vendor tree diff is not the expected set of KANO changes (see the three lists above)")

block = (
    "on boot\n"
    "    start fwupd\n"
    "    # Start the daemon in the background first\n"
    "    start fwupd_script\n"
    "\n"
)
if (tmp / "vendor-tree-new/etc/init/fwupd.rc").exists() or (tmp / "vendor-tree-new/bin/fwupd-binder").exists():
    raise SystemExit("fwupd still present in the rebuilt vendor")

def meta(path):
    data, blksz, meta_off, xattr, root = rebuild.load_image(path)
    m, _h = rebuild.walk_image(str(path), root, data, blksz, meta_off, xattr)
    return m

oldm = meta(tmp / "vendor.erofs.src")
newm = meta(tmp / "vendor.erofs")
gone = sorted(set(oldm) - set(newm))
extra = sorted(set(newm) - set(oldm))
bad = []
for key in set(oldm) & set(newm):
    a, b = oldm[key], newm[key]
    if (a["mode"], a["uid"], a["gid"], a["selinux"], a["caps"]) != (b["mode"], b["uid"], b["gid"], b["selinux"], b["caps"]):
        bad.append(key)
print("metadata only old:", gone)
print("metadata only new:", extra)
print("metadata differs:", bad[:20], "count", len(bad))
if gone != expect_old or extra != expect_new or bad:
    raise SystemExit("vendor uid/gid/mode/selinux/capabilities drifted")
print("vendor tree ok")
PY

echo "avb hashtree footer"
cp "$TMP/vendor.erofs" "$TMP/vendor.img"
ot avbtool add_hashtree_footer \
    --image "$TMP/vendor.img" \
    --partition_name vendor \
    --partition_size "$VENDOR_SIZE" \
    --hash_algorithm sha256 \
    --salt "$SALT" \
    --block_size 4096 \
    --fec_num_roots 2 \
    --prop com.android.build.vendor.os_version:17 \
    --prop "com.android.build.vendor.fingerprint:ASUS/lapis/lapis:17/CL3B.260622.271.R1/16452207:user/release-keys" \
    --prop com.android.build.vendor.security_patch:2026-09-01

python3 -c 'import os,sys; n=os.path.getsize(sys.argv[1]); w=int(sys.argv[2]); print(n,w); assert n==w' \
    "$TMP/vendor.img" "$VENDOR_SIZE"

echo "re-sign vbmeta"
dd if="$IMG" of="$TMP/vbmeta.old.img" bs=1 skip="$VBMETA_OFF" count=6720 status=none
ot avbtool update_partition_descriptor \
    --image "$TMP/vbmeta.old.img" \
    --partition_image "$TMP/vendor.img" \
    --output "$TMP/vbmeta.new.img" \
    --algorithm SHA256_RSA4096 \
    --key "$KEY" \
    --rollback_index 1339
ot avbtool info_image --image "$TMP/vbmeta.new.img" > "$TMP/vbmeta.new.info"
ot avbtool calculate_vbmeta_digest --image "$TMP/vbmeta.new.img" --hash_algorithm sha256 \
    | tee "$WORK/logs/vbmeta.digest.txt"

echo "read ESP initrd"
mcopy -i "$IMG@@$ESP_OFF" ::/initrd.img "$TMP/esp-initrd.img"

python3 - "$IMG" "$TMP" "$STANDALONE" << 'PY'
import struct, sys
from pathlib import Path

img = Path(sys.argv[1])
tmp = Path(sys.argv[2])
standalone = Path(sys.argv[3])
env = {}
for line in (tmp / "offsets.env").read_text().splitlines():
    k, v = line.split("=", 1)
    env[k] = v
vendor_off = int(env["VENDOR_OFF"])
vendor_size = int(env["VENDOR_SIZE"])
vbmeta_off = int(env["VBMETA_OFF"])
boot_uuid = env["BOOT_UUID"]
vendor = (tmp / "vendor.img").read_bytes()
if len(vendor) != vendor_size:
    raise SystemExit("vendor size")
vb = (tmp / "vbmeta.new.img").read_bytes()
if vb[:4] != b"AVB0":
    raise SystemExit("vbmeta magic")
auth, aux = struct.unpack_from(">QQ", vb, 12)
vb_size = 256 + auth + aux
if len(vb) != vb_size:
    raise SystemExit(f"vbmeta file {len(vb)} != struct {vb_size}")
digest = (tmp.parent / "logs/vbmeta.digest.txt").read_text().strip().split()[0]
if len(digest) != 64:
    raise SystemExit("bad digest")

def split_bc(data: bytes):
    magic = b"#BOOTCONFIG\n"
    if not data.endswith(magic):
        raise SystemExit("bootconfig magic")
    end = len(data) - len(magic)
    size, csum = struct.unpack_from("<II", data, end - 8)
    start = end - 8 - size
    bc = data[start:start + size]
    if (sum(bc) & 0xFFFFFFFF) != csum:
        raise SystemExit("bootconfig checksum")
    return data[:start], bc.decode()

archive, _esp_bc = split_bc((tmp / "esp-initrd.img").read_bytes())
_std_arch, std_bc = split_bc(standalone.read_bytes())

def set_line(text, key, value):
    lines = text.split("\n")
    found = False
    out = []
    for line in lines:
        if line.startswith(key + "="):
            if found:
                raise SystemExit("duplicate " + key)
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        raise SystemExit("missing " + key)
    return "\n".join(out)

text = set_line(std_bc, "androidboot.vbmeta.digest", digest)
text = set_line(text, "androidboot.vbmeta.size", str(vb_size))
text = set_line(text, "androidboot.boot_part_uuid", boot_uuid)
for need in (
    "androidboot.hypervisor.protected_vm.supported=0\n",
    "androidboot.vendor.apex.com.android.hardware.keymint=com.android.hardware.keymint.rust_nonsecure\n",
    "androidboot.vendor.apex.com.android.hardware.gatekeeper=com.android.hardware.gatekeeper.nonsecure\n",
):
    if need not in text:
        raise SystemExit("bootconfig missing " + need.strip())
raw = text.encode()
footer = struct.pack("<II", len(raw), sum(raw) & 0xFFFFFFFF) + b"#BOOTCONFIG\n"
(tmp / "initrd.new.img").write_bytes(archive + raw + footer)
print("initrd bytes", (tmp / "initrd.new.img").stat().st_size, "vbmeta", vb_size)

with img.open("r+b") as f:
    f.seek(vendor_off)
    f.write(vendor)
    f.seek(vbmeta_off)
    f.write(b"\x00" * 8192)
    f.seek(vbmeta_off)
    f.write(vb)
print("spliced vendor and vbmeta")
PY

echo "replace ESP initrd"
mcopy -o -i "$IMG@@$ESP_OFF" "$TMP/initrd.new.img" ::/initrd.img

echo "sha256"
sha256sum "$IMG" | tee "$IMG.sha256"

rm -rf "$TMP"
echo "build done"
