"""KANO vendor edits that need more than profile data (hooks.vendor in profile.toml). builder/vendor.sh calls each hook
at its place in the vendor rebuild with a context: ctx.tree (the extracted vendor tree), ctx.vmap (path -> erofs
metadata; new paths copy the metadata of a stock file), ctx.prof (the validated profile) and ctx.audio_src (the
fetched audio sources). expected(prof) tells vendor.sh which vendor paths the hooks add and change, so its final
tree comparison stays exact. A new model copies this file and rewrites the parts that differ."""
import re
import struct
import xml.dom.minidom as md
from pathlib import Path

ID = "KANO"
AUDIO_REF = "sof-soundwire_cs42l43_cs35l56"     # lapis' own audio SKU: licence header and volume-curve layout
KB_OLD = "/sys/devices/platform/GOOG0002:00/leds"  # where lapis expects the keyboard backlight LED


def expected(prof):
    """(new paths, changed paths) in the vendor tree, relative to /vendor."""
    new, changed = [], []
    a = prof["audio"]
    if a["config_dir"]:
        d = "etc/audio/" + a["config_dir"]
        new += [d] + [f"{d}/{f}" for f in ("audio_config.xml", "mixer_paths.xml", "volume_curves.xml")]
        changed.append("etc/data/hal_configs/audio_default.xml")
    if prof["input"]["kbd_backlight_leds"]:
        new.append(f"etc/ueventd/ueventd.{prof['model']['id']}.rc")
        changed.append("usr/idc/AT_Translated_Set_2_keyboard.idc")
    if prof["input"]["power_button"] == "acpi":
        changed.append("etc/excluded-input-devices.xml")
    return new, changed


def display(ctx):
    """build.prop: the profile's property changes and the hardware composer backend."""
    bp = ctx.tree / "build.prop"
    bt = bp.read_text()
    for k, old, new in ctx.prof["display"]["build_prop_set"]:
        line = f"{k}={old}"
        if bt.splitlines().count(line) != 1 or sum(1 for l in bt.splitlines() if l.startswith(k + "=")) != 1:
            raise SystemExit("prop not exactly once: " + line)
        bt = bt.replace("\n" + line + "\n", "\n" + f"{k}={new}" + "\n", 1)
    bp.write_text(bt)
    if ctx.prof["display"]["hwc_backend"] == "client":
        # drm_hwcomposer: every layer goes through SurfaceFlinger client composition (linear client target).
        bt = bp.read_text()
        if any(l.startswith("vendor.hwc.backend_override=") for l in bt.splitlines()):
            raise SystemExit("backend_override already set")
        bt = bt.rstrip("\n") + f"\n# {ID}: xe faults scanning Y-tiled buffers through DPT on ADL-P; compose everything into the client target\nvendor.hwc.backend_override=client\n"
        bp.write_text(bt)
    print("hwc backend:", [l for l in bt.splitlines() if "backend_override" in l])
    print("colour props:", [l for l in bt.splitlines() if "color" in l or "HDR" in l or "gamma" in l])


def wifi_rc(ctx):
    """Head of etc/init/kano-hw.rc: the Wi-Fi op-mode modules lapis does not load itself."""
    mods = ctx.prof["wifi"]["modprobe"]
    if not mods:
        return ""
    return (f"# {ID}: the AX211 needs the iwlmvm op-mode (the image only loads iwlmld, for the lapis BE200/BE211).\n"
            "on early-boot\n"
            + "".join(f"    exec_background u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/modules {m}\n" for m in mods)
            + "\n")


def audio(ctx):
    """DRAS (the desktop audio HAL) configuration for the KANO card: sof-nau8825 (NAU8825 headset + 2x MAX98373, SOF IPC3).
    Translated from ChromeOS board-overlays overlay-brya/chromeos-base/chromeos-bsp-brya/files/kano/audio
    (ucm-config/sof-nau8825.kano/HiFi.conf, cras-config/sof-nau8825.card_settings; read 2026-10-12).
    The DSM/DTS tuning blobs (cset-tlv) are not public and are left out; PCM ids match /proc/asound/pcm on the KANO."""
    tree, vmap, src_audio = ctx.tree, ctx.vmap, ctx.audio_src
    card, cdir = ctx.prof["audio"]["card"], ctx.prof["audio"]["config_dir"]
    lic = (tree / f"etc/audio/{AUDIO_REF}/audio_config.xml").read_text().split("<!--", 1)[1].split("-->", 1)[0]
    adir = tree / "etc/audio" / cdir
    if adir.exists():
        raise SystemExit("kano audio dir already present")
    adir.mkdir(); adir.chmod(0o755)
    vmap["etc/audio/" + cdir] = dict(vmap["etc/audio/" + AUDIO_REF])
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
    spk = {int(m.group(1)): int(m.group(2)) for m in re.finditer(r"db_at_(\d+) = (-?\d+)", cs.split("[Headphone]")[0])}
    assert sorted(spk) == list(range(101)), "speaker curve"
    hp = cs.split("[Headphone]")[1]
    hp_step = int(re.search(r"volume_step = (\d+)", hp).group(1)); hp_max = int(re.search(r"max_volume = (-?\d+)", hp).group(1))
    # Without kano's DSM speaker-protection model (not public) the speaker curve is lowered 4.75 dB: -10 dB at 100 %.
    SPK_HEADROOM = 475

    def kano_db(ref, pct):
        return spk[pct] - SPK_HEADROOM if ref == "Speaker" else hp_max - hp_step * (100 - pct)
    vc = (tree / f"etc/audio/{AUDIO_REF}/volume_curves.xml").read_text()

    def fix_ref(m):
        ref, body = m.group(1), m.group(2)
        pts = [int(p) for p in re.findall(r"<point>(\d+),-?\d+</point>", body)]
        if ref not in ("Speaker", "Headphone"):
            return m.group(0)
        top = max(pts)
        new = body
        for p in pts:
            new = re.sub(rf"<point>{p},-?\d+</point>", f"<point>{p},{kano_db(ref, round(p * 100 / top))}</point>", new, count=1)
        return f'<reference name="{ref}">{new}</reference>'
    vc2, nref = re.subn(r'<reference name="([^"]+)">(.*?)</reference>', fix_ref, vc, flags=re.S)
    assert nref >= 2 and vc2 != vc, "volume curves"
    (adir / "volume_curves.xml").write_text(vc2)
    for f in ("audio_config.xml", "mixer_paths.xml", "volume_curves.xml"):
        (adir / f).chmod(0o644)
        vmap[f"etc/audio/{cdir}/" + f] = dict(vmap[f"etc/audio/{AUDIO_REF}/audio_config.xml"])
    # Point the default audio hal_config at the KANO card (lapis' own cs42l43 SKU file stays as it is).
    hc = tree / "etc/data/hal_configs/audio_default.xml"
    old_hc = f"<soundcard>sof-soundwire</soundcard>\n  <audio-config-dir>{AUDIO_REF}</audio-config-dir>"
    if hc.read_text().count(old_hc) != 1:
        raise SystemExit("audio_default.xml not as expected")
    hc.write_text(hc.read_text().replace(old_hc, f"<soundcard>{card}</soundcard>\n  <audio-config-dir>{cdir}</audio-config-dir>"))
    for f in ("audio_config.xml", "mixer_paths.xml", "volume_curves.xml"):
        md.parse(str(adir / f))
    md.parse(str(hc))
    print("kano audio:", sorted(p.name for p in adir.iterdir()), hc.read_text())


def kbd_backlight(ctx):
    """Keyboard backlight: on the KANO, ACPI GOOG0002 (\\_SB_.CREC.KBLT) sits under the EC node, so cros_kbd_led_backlight
    registers /sys/devices/platform/GOOG0004:00/GOOG0002:00/leds/chromeos::kbd_backlight (lapis: platform/GOOG0002:00).
    Verified on the KANO maintenance OS 2026-10-12: brightness 0/100/0/50/100 read back through ACPI."""
    tree, vmap = ctx.tree, ctx.vmap
    KB_NEW = ctx.prof["input"]["kbd_backlight_leds"]
    if not KB_NEW:
        return
    idc = tree / "usr/idc/AT_Translated_Set_2_keyboard.idc"
    it = idc.read_text()
    if it.count("device.additionalSysfsLedsNode = " + KB_OLD + "/chromeos::kbd_backlight") != 1:
        raise SystemExit("keyboard idc not as expected")
    idc.write_text(it.replace(KB_OLD, KB_NEW))
    ue = tree / f"etc/ueventd/ueventd.{ctx.prof['model']['id']}.rc"
    ue.write_text(f"# {ID}: keyboard backlight LED (GOOG0002 under the EC node GOOG0004)\n" + KB_NEW + "/* brightness 0660 system system\n")
    ue.chmod(0o644)
    vmap[f"etc/ueventd/{ue.name}"] = dict(vmap["etc/ueventd/ueventd.fatcat.rc"])
    cil2 = tree / "etc/selinux/vendor_sepolicy.cil"
    ct2 = cil2.read_text()
    gline = '(genfscon sysfs "/devices/platform/GOOG0002:00/leds" (u object_r sysfs_keyboard_backlight ((s0) (s0))))'
    if ct2.count(gline) != 1:
        raise SystemExit("keyboard genfscon not as expected")
    cil2.write_text(ct2.rstrip("\n") + f"\n;; {ID} keyboard backlight LED path (GOOG0002 under GOOG0004)\n" + gline.replace("/devices/platform/GOOG0002:00/leds", KB_NEW[4:]) + "\n")
    print("kbd idc:", [l for l in idc.read_text().splitlines() if "LedsNode" in l])
    print("kbd ueventd:", ue.read_text())


def power_button(ctx):
    """lapis excludes the ACPI "Power Button" input device because its power key arrives through cros_ec_buttons
    (cros_ec_keyb). On the KANO cros_ec_keyb is blacklisted and the i8042 keyboard carries no power key (EC powerbtn left
    Android asleep), so the ACPI device is the only source: stop excluding it."""
    if ctx.prof["input"]["power_button"] != "acpi":
        return
    ex = ctx.tree / "etc/excluded-input-devices.xml"
    et = ex.read_text()
    if et.count('<device name="Power Button"/>') != 1:
        raise SystemExit("excluded-input-devices.xml not as expected")
    ex.write_text(et.replace('    <device name="Power Button"/>\n',
                             f'    <!-- {ID}: cros_ec_keyb (cros_ec_buttons) is blacklisted, so the ACPI Power Button is the only power key -->\n', 1))
    print("excluded-input-devices:", ex.read_text())


# --- GBOS_EC (opt-in EC features, builder/image.py and docs/SAFETY.md "EC 功能變體"): the KANO's vendor side ---
def arsc_values(b):
    """{type/name: offset of the 32-bit Res_value data} for the simple (non-bag) entries of a resources.arsc"""
    def pool(off):
        _t, _h, _sz, cnt, _sc, flags, sstart, _ss = struct.unpack_from("<HHIIIIII", b, off)
        out = []
        for o in struct.unpack_from("<%dI" % cnt, b, off + _h):
            q = off + sstart + o
            if flags & 0x100:
                q += 2 if b[q] & 0x80 else 1
                n = b[q]
                if n & 0x80: n = ((n & 0x7f) << 8) | b[q + 1]; q += 2
                else: q += 1
                out.append(b[q:q + n].decode())
            else:
                n = struct.unpack_from("<H", b, q)[0]; q += 2
                if n & 0x8000: n = ((n & 0x7fff) << 16) | struct.unpack_from("<H", b, q)[0]; q += 2
                out.append(b[q:q + 2 * n].decode("utf-16le"))
        return out
    _t, hsz, _sz, _np = struct.unpack_from("<HHII", b, 0)
    gsz = struct.unpack_from("<I", b, hsz + 4)[0]
    vals, p = {}, hsz + gsz
    while p < len(b):
        ctyp, chsz, csz = struct.unpack_from("<HHI", b, p)
        if ctyp == 0x200:
            tstr, _l, kstr = struct.unpack_from("<III", b, p + 268)
            tn, kn = pool(p + tstr), pool(p + kstr)
            q = p + chsz
            while q < p + csz:
                t2, h2, s2 = struct.unpack_from("<HHI", b, q)
                if t2 == 0x201:
                    tid, tflags = b[q + 8], b[q + 9]
                    ecount, estart = struct.unpack_from("<II", b, q + 12)
                    if tflags & 1:                                   # sparse: (index, offset/4) pairs
                        offs = [struct.unpack_from("<HH", b, q + h2 + 4 * i)[1] * 4 for i in range(ecount)]
                    elif tflags & 2:                                 # 16-bit offsets/4, 0xFFFF = no entry
                        offs = [0xFFFFFFFF if o == 0xFFFF else o * 4 for o in struct.unpack_from("<%dH" % ecount, b, q + h2)]
                    else:
                        offs = struct.unpack_from("<%dI" % ecount, b, q + h2)
                    for o in offs:
                        if o == 0xFFFFFFFF: continue
                        e = q + estart + o
                        esz, eflags, key = struct.unpack_from("<HHI", b, e)
                        if eflags & 1: continue                      # bag
                        if eflags & 8: vals[tn[tid - 1] + "/" + kn[esz]] = (e + 4, eflags >> 8)   # compact entry
                        else: vals[tn[tid - 1] + "/" + kn[key]] = (e + esz + 4, b[e + esz + 3])
                q += s2
        p += csz
    return vals


def ec(ctx, features):
    """GBOS_EC on the KANO: sensors = the LPC EC's IIO sensors labelled for SELinux and lapis' auto-rotation overlay value
    flipped to true; buttons = a keylayout that maps only the volume keys of cros_ec_buttons (the power key stays ACPI)."""
    import struct, zipfile, zlib
    tree, vmap = ctx.tree, ctx.vmap
    if "sensors" in features:
        # The KANO's EC is on LPC/eSPI (ACPI GOOG0004, cros_ec_lpcs), so its IIO sensors live under
        # /devices/platform/GOOG0004:00/cros-ec-dev.*/...; lapis labels only the ISH/PCI and ARM SPI paths.
        cil = tree / "etc/selinux/vendor_sepolicy.cil"; ct = cil.read_text()
        pci = [l for l in ct.splitlines() if l.startswith('(genfscon sysfs "/devices/pci0000:00/0000:00:*/cros-*-dev*/cros-ec-sensorhub*/')]
        if len(pci) != 10:
            raise SystemExit(f"cros-ec IIO genfscon not as expected ({len(pci)} lines)")
        lpc = [l.replace('"/devices/pci0000:00/0000:00:*/', '"/devices/platform/GOOG0004:00/', 1) for l in pci]
        cil.write_text(ct.rstrip("\n") + "\n;; KANO (GBOS_EC=sensors): cros-ec IIO sensors on the LPC EC GOOG0004\n" + "\n".join(lpc) + "\n")
        # lapis' vendor RRO sets config_supportAutoRotation=false for every lapis device; the KANO is a convertible.
        # resources.arsc is stored uncompressed: flip the one value in place (same length) and fix both zip CRC32 fields.
        # APKs on the read-only partitions are not re-verified at boot (certificates are collected without the digest check).
        # Not checked at build time: whether PackageManager still enables the overlay (on the device: cmd overlay list;
        # if it does not, the RRO's other values, e.g. automatic brightness and power_profile, are gone too).
        rro = tree / "overlay/framework-res__lapis__auto_generated_rro_vendor.apk"
        z = bytearray(rro.read_bytes())
        with zipfile.ZipFile(rro) as zf:
            zi = zf.getinfo("resources.arsc")
            if zi.compress_type != zipfile.ZIP_STORED: raise SystemExit("RRO resources.arsc is compressed")
            before = {n: zf.read(n) for n in zf.namelist()}
        lh = zi.header_offset
        if z[lh:lh + 4] != b"PK\x03\x04": raise SystemExit("RRO local header not found")
        data0 = lh + 30 + struct.unpack_from("<HH", z, lh + 26)[0] + struct.unpack_from("<H", z, lh + 28)[0]
        arsc = bytearray(z[data0:data0 + zi.file_size])
        v = arsc_values(bytes(arsc))
        want = {"bool/config_supportAutoRotation", "bool/config_automatic_brightness_available",
                "bool/config_isCameraCompatOverrideLensFacingTreatmentEnabled", "xml/power_profile"}
        if set(v) != want: raise SystemExit(f"RRO resources not as expected: {sorted(v)}")
        off, dt = v["bool/config_supportAutoRotation"]
        if dt != 0x12 or struct.unpack_from("<I", arsc, off)[0] != 0: raise SystemExit("config_supportAutoRotation is not false")
        struct.pack_into("<I", arsc, off, 0xFFFFFFFF)
        z[data0:data0 + len(arsc)] = arsc
        crc = zlib.crc32(arsc) & 0xFFFFFFFF
        struct.pack_into("<I", z, lh + 14, crc)
        hits = [i for i in range(len(z)) if z[i:i + 4] == b"PK\x01\x02" and struct.unpack_from("<I", z, i + 42)[0] == lh]
        if len(hits) != 1: raise SystemExit("RRO central directory entry not found")
        struct.pack_into("<I", z, hits[0] + 16, crc)
        rro.write_bytes(bytes(z))
        with zipfile.ZipFile(rro) as zf:
            if zf.testzip() is not None: raise SystemExit("patched RRO fails its CRC check")
            after = {n: zf.read(n) for n in zf.namelist()}
        if [n for n in before if before[n] != after[n]] != ["resources.arsc"]: raise SystemExit("RRO patch changed more than resources.arsc")
        v2 = arsc_values(after["resources.arsc"])
        if struct.unpack_from("<I", after["resources.arsc"], v2["bool/config_supportAutoRotation"][0])[0] != 0xFFFFFFFF:
            raise SystemExit("config_supportAutoRotation not true after the patch")
        print("EC sensors: LPC IIO genfscon added,", len(lpc), "lines; RRO config_supportAutoRotation=true")
    if "buttons" in features:
        # cros_ec_keyb (ACPI GOOG0007, input device cros_ec_buttons) brings the side volume keys and the EC's lid/tablet
        # switches. The power key stays the ACPI Power Button (the KANO's verified path, see safe25 above): this layout maps
        # no power key for cros_ec_buttons, so its KEY_POWER becomes KEYCODE_UNKNOWN instead of a second press.
        kl = tree / "usr/keylayout"; kl.mkdir()
        (kl / "cros_ec_buttons.kl").write_text("# KANO (GBOS_EC=buttons): volume keys from the EC; the power key is the ACPI Power Button\n"
                                               "key 114   VOLUME_DOWN\nkey 115   VOLUME_UP\n")
        vmap["usr/keylayout"] = dict(vmap["usr/idc"], selinux="u:object_r:vendor_keylayout_file:s0")
        vmap["usr/keylayout/cros_ec_buttons.kl"] = dict(vmap["usr/idc/Elan_Touchpad.idc"], selinux="u:object_r:vendor_keylayout_file:s0")
        print("EC buttons: usr/keylayout/cros_ec_buttons.kl (volume keys only)")


def camera(ctx, overlay):
    """The built-in camera (GBOS_CAMERA_OVERLAY = the out/vendor directory of builder/camera/build-libcamera.sh): upstream
    libcamera (simple pipeline + SoftISP on the CPU) and its Android camera3 HAL as camera.kano.so. lapis' own HAL
    (camera.libcamera_hal_base.so) only has an IPU7 pipeline; the kernel side (intel-ipu6, -isys, ov2740) and the SELinux
    and ueventd rules are in lapis already. Private libraries live in /vendor/lib64/kanocam (RUNPATH): nothing of lapis'
    is replaced. Every file is checked against the overlay's SHA256SUMS first."""
    import hashlib
    tree, vmap = ctx.tree, ctx.vmap
    sums = {}
    for line in (overlay.parent / "SHA256SUMS").read_text().splitlines():
        h, rel = line.split("  ", 1)
        sums[rel] = h
    files = sorted(p for p in overlay.rglob("*") if p.is_file() or p.is_symlink())
    if sorted("vendor/" + p.relative_to(overlay).as_posix() for p in files) != sorted(sums):
        raise SystemExit("camera overlay: files differ from its SHA256SUMS")
    for p in files:
        if p.is_symlink() or not p.is_file():
            raise SystemExit(f"camera overlay: not a regular file: {p}")
        if hashlib.sha256(p.read_bytes()).hexdigest() != sums["vendor/" + p.relative_to(overlay).as_posix()]:
            raise SystemExit(f"camera overlay: checksum mismatch {p}")
    for p in sorted(overlay.rglob("*")):
        rel = p.relative_to(overlay).as_posix()
        dst = tree / rel
        if p.is_dir():
            if not dst.exists():
                dst.mkdir()
                dst.chmod(0o755)
                vmap[rel] = dict(vmap["lib64/libcamera"])
            continue
        if dst.exists():
            raise SystemExit(f"camera overlay would replace a lapis file: {rel}")
        dst.write_bytes(p.read_bytes())
        dst.chmod(0o644)
        if rel.startswith("lib64/hw/"):
            vmap[rel] = dict(vmap["lib64/hw/camera.libcamera_hal_base.so"])
        elif rel.startswith("lib64/"):
            vmap[rel] = dict(vmap["lib64/libcamera_internal.so"])
        else:
            vmap[rel] = dict(vmap["etc/init/fwupd.rc"])
    fr = tree / "etc/init/init.fatcat.rc"
    ft = fr.read_text()
    old = "    setprop ro.hardware.camera libcamera_hal_base\n"
    if ft.count(old) != 1:
        raise SystemExit("init.fatcat.rc camera line not as expected")
    fr.write_text(ft.replace(old, "    # KANO: upstream libcamera HAL (IPU6 + OV2740); lapis' libcamera_hal_base is IPU7-only\n"
                                  "    setprop ro.hardware.camera kano\n"))
    print("camera:", len(files), "files from", overlay)


def camera_expected(overlay, old):
    """(new paths, changed paths) the camera() hook adds to the vendor tree (old: the paths of the stock tree)."""
    new = [p.relative_to(overlay).as_posix() for p in overlay.rglob("*")]
    return sorted(x for x in new if x not in old), ["etc/init/init.fatcat.rc"]


def ec_expected(features):
    """(new paths, changed paths) the ec() hook adds to the vendor tree."""
    new, changed = [], []
    if "sensors" in features:
        changed.append("overlay/framework-res__lapis__auto_generated_rro_vendor.apk")
    if "buttons" in features:
        new += ["usr/keylayout", "usr/keylayout/cros_ec_buttons.kl"]
    return new, changed
