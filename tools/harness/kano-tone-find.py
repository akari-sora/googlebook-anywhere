#!/usr/bin/env python3
"""Find 1 kHz tone bursts in a mono 16 kHz WAV recorded by the development host's mic while the KANO runs.

usage: kano-tone-find.py rec.wav [start_epoch]
"""
import math, struct, sys, wave
w = wave.open(sys.argv[1]); fs = w.getframerate(); n = w.getnframes()
x = struct.unpack("<%dh" % n, w.readframes(n))
t0 = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
def g(seg, f):
    k = 2 * math.cos(2 * math.pi * f / fs); s1 = s2 = 0.0
    for v in seg: s1, s2 = v + k * s1 - s2, s1
    return math.sqrt(max(s1 * s1 + s2 * s2 - k * s1 * s2, 0)) / len(seg) / 32768
win = fs // 4
lv = []
for i in range(0, n - win, win):
    seg = x[i:i + win]
    lv.append((i / fs, 20 * math.log10(g(seg, 1000) + 1e-12), 20 * math.log10(g(seg, 1300) + 1e-12)))
med = sorted(l[1] for l in lv)[len(lv) // 2]
hits = [l for l in lv if l[1] > med + 15 and l[1] > l[2] + 15]
print(f"windows {len(lv)}, median 1 kHz {med:.1f} dB, hits {len(hits)}")
prev = -9
for t, a, b in hits:
    if t - prev > 1:
        print(f"burst at +{t:.1f}s (epoch {t0 + t:.0f}): 1 kHz {a:.1f} dB vs 1.3 kHz {b:.1f} dB")
    prev = t
