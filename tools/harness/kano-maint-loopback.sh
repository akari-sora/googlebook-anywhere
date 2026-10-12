#!/usr/bin/env bash
# Runs ON the KANO maintenance Linux (LPSS-I2C test entry). Speaker -> internal DMIC loopback at a low level (-20 dBFS, 1 kHz, 3 s).
set -uo pipefail
C=$(awk '/sofnau8825/ {print $1; exit}' /proc/asound/cards); [ -n "$C" ] || { echo NO_CARD; exit 1; }
A() { amixer -q -c "$C" cset name="$1" "$2" && echo "set $1 = $2" || echo "FAIL $1"; }
echo "jack: $(amixer -c "$C" cget name='Headphone Jack' | grep ': values' ) mic-jack: $(amixer -c "$C" cget name='Headset Mic Jack' | grep ': values')"
# kano HiFi.conf verb + speaker enable sequence (ChromeOS board-overlays, read 2026-10-12)
A 'Left DAI Sel Mux' Right; A 'Right DAI Sel Mux' Left
A 'Left Digital Volume' 127; A 'Right Digital Volume' 127; A 'Left FS Max Volume' 6; A 'Right FS Max Volume' 6
A 'Left Spk Switch' on; A 'Right Spk Switch' on; A 'Left VI Sense Switch' on; A 'Right VI Sense Switch' on
cd /tmp || exit 1
python3 - <<'PY'
import math, struct, wave
for name, amp in (("tone.wav", 0.1), ("silence.wav", 0.0)):
    w = wave.open(name, "wb"); w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
    fr = bytearray()
    for i in range(48000 * 3):
        s = int(amp * 32767 * math.sin(2 * math.pi * 1000 * i / 48000)) if 4800 <= i < 48000 * 3 - 4800 else 0
        fr += struct.pack("<hh", s, s)
    w.writeframes(bytes(fr)); w.close()
PY
for t in silence tone; do
  arecord -D hw:$C,99 -f S32_LE -c 4 -r 48000 -d 4 /tmp/rec-$t.wav 2>/tmp/arec-$t.err &
  R=$!; sleep 0.4
  aplay -D hw:$C,0 /tmp/$t.wav 2>/tmp/aplay-$t.err || { echo "aplay $t failed:"; cat /tmp/aplay-$t.err; }
  wait $R || { echo "arecord $t failed:"; cat /tmp/arec-$t.err; }
done
python3 - <<'PY'
import math, struct, wave
def g(x, f, fs=48000):
    k = 2 * math.cos(2 * math.pi * f / fs); s1 = s2 = 0.0
    for v in x: s1, s2 = v + k * s1 - s2, s1
    return math.sqrt(max(s1 * s1 + s2 * s2 - k * s1 * s2, 0)) / len(x)
for t in ("silence", "tone"):
    w = wave.open(f"/tmp/rec-{t}.wav"); n = w.getnframes(); ch = w.getnchannels(); d = w.readframes(n)
    v = struct.unpack("<%di" % (n * ch), d)
    seg = slice(48000, 48000 * 3)
    for c in range(2):
        x = [s / 2**31 for s in v[c::ch][seg]]
        rms = math.sqrt(sum(s * s for s in x) / len(x))
        print(f"{t} ch{c}: rms {20*math.log10(rms+1e-12):.1f} dBFS, 1kHz {20*math.log10(g(x,1000)+1e-12):.1f} dB, 700Hz {20*math.log10(g(x,700)+1e-12):.1f} dB")
PY
A 'Left Spk Switch' off; A 'Right Spk Switch' off
echo LOOPBACK_DONE
