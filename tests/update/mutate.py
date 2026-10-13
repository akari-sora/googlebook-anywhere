#!/usr/bin/env python3
"""Mutation check for the update engine's guards: each mutation reverts one guard of install/gbos-update.py in a temp
copy - never in place - and runs tests/update/selftest.py against the copy (GBOS_UPDATE_ENGINE). A mutation that
leaves the self-test green is a failure of this check, and so is one that does not compile (STALE: the list is out
of date) or turns it red without a single failed check (NO-FAIL: a crash proves nothing).

usage: tests/update/mutate.py [-j N] [ID ...]      (default: all mutations, 3 in parallel)
Needs what selftest.py needs. Each run makes its own synthetic disks (about 2 GB) under $TMPDIR.

Not here: checks that only fire when the writing code itself is wrong (the read-back of every written range, the
running slot's extents after the LP metadata write, the stick's menu after stick_prepare/stick_menu), and the
/sys/dev/block realpath check, which needs a block device.
"""
import os, shutil, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent.parent / "install" / "gbos-update.py"

# (id, review finding, [(original text, mutated text), ...])
MUTATIONS = [
    ("removed-target-entries", "Opus I2 M5",
     [('''        if s != c:                                   # the old previous version (slot t) or broken entries
            inst.fat.delete("/loader/entries/" + n)''', '''        if False:
            inst.fat.delete("/loader/entries/" + n)''')]),
    ("rollback-while-resuming", "Opus I2 M7",
     [('''    if inst.resume:
        raise Refuse("an update stopped halfway; finish it first (run the update again)")''', "")]),
    ("stick-room", "Opus I2 M8",
     [("        if not stick_needs(sb, bf, t, copy):", "        if False:")]),
    ("new-dynamic-partition", "Opus I2 M14",
     [('''            raise Refuse(f"the release has a new dynamic partition {b}; this release needs a fresh install")''',
       "            pass")]),
    ("slot-group-size", "Opus I2 M15",
     [("    if sum(sizes.values()) > inst.lp.group_max[gi] and inst.lp.group_max[gi]:", "    if False:")]),
    ("stick-prepare-skipped", "Opus I1",
     [("        stick_prepare(sb, t, c, stick_src, copy)", "        pass")]),
    ("stick-cur-not-reset", "Opus I1 (case B)",
     [('''    sb.write("/loader/entries/gbos-nvme.conf", entry_text("Googlebook (internal disk)", linux, initrd, src["options"]).encode())''',
       "")]),
    ("stick-copy-before-disk", "stale stick copy",
     [('''    run_e = inst.entries.get(inst.default)
    if run_e is not None and inst.slot_of.get(inst.default) == c:
        return run_e, False
    ents = stick_entries(sb)
    for n in ("gbos-nvme.conf", "gbos-nvme-prev.conf"):
        e, s = ents.get(n, (None, None))
        if e is not None and s == c:
            return e, True''', '''    ents = stick_entries(sb)
    for n in ("gbos-nvme.conf", "gbos-nvme-prev.conf"):
        e, s = ents.get(n, (None, None))
        if e is not None and s == c:
            return e, True
    run_e = inst.entries.get(inst.default)
    if run_e is not None and inst.slot_of.get(inst.default) == c:
        return run_e, False''')]),
    ("old-files-first", "Grok I2 / Opus m2",
     [('''        if not bf["new_first"]:''', "        if True:")]),
    ("window-not-reported", "Grok I2",
     [("            WINDOW.append(sb is not None)", "            pass")]),
    ("no-room-refusal", "v0.2 update path",
     [('''    if not bf["fits"] and bf["prev"] and not without_fallback and sb is None:''', "    if False:")]),
    ("shared-kernel-deleted", "v0.2 integration",
     [('''    old_files = [x for x in bf["keep"] if x not in bf["files"] and x not in new_refs]''',
       '''    old_files = [x for x in bf["keep"] if x not in bf["files"]]''')]),
    ("stale-entry-kept", "v0.2 plain entry",
     [('''    for n in bf["stale"]:
        inst.fat.delete("/loader/entries/" + n)''', "")]),
    ("fat-size-field", "Opus m3",
     [("    if total * bps > length:", "    if False:")]),
    ("confirmed-bytes", "Opus m1",
     [("        if nbytes is not None and self.size != int(nbytes):", "        if False:")]),
    ("rollback-build-change", "Opus m4",
     [("    if pb != inst.build and not allow_build_change:", "    if False:")]),
    ("rollback-stick-ignored", "Opus m4",
     [("    sb = open_stick(stick_boot, tmp) if stick_boot else None\n    sents = ", "    sb = None\n    sents = ")]),
    ("rollback-stick-content", "stale stick copy",
     [('''        if [sb.read(x) for x in [se["linux"], *se["initrd"]]] != [inst.fat.read(x) for x in [de["linux"], *de["initrd"]]]:''',
       "        if False:")]),
    ("window-cleared-early", "round 2 N1 / Grok n1",
     [('''        inst.fat.write(x, data)
    step(f"bootfiles: boot files of slot {t} written and read back")''', '''        inst.fat.write(x, data)
    WINDOW.clear()
    step(f"bootfiles: boot files of slot {t} written and read back")''')]),
    ("rerun-window-not-set", "round 2 N1",
     [("    if inst.resume and inst.slot_of.get(inst.default) is None:", "    if False:")]),
    ("stick-copy-from-stick", "round 2 N3 X1",
     [('''    ents = stick_entries(sb)
    for n in ("gbos-nvme.conf", "gbos-nvme-prev.conf"):
        e, s = ents.get(n, (None, None))
        if e is not None and s == c:
            return e, True
    raise Refuse("cannot find the running version's boot files''', '''    raise Refuse("cannot find the running version's boot files''')]),
    ("stick-fat-size", "round 2 N3 X5",
     [('''    fat_fits(bs, length, "the installer stick's boot partition")''', "    pass")]),
    ("rollback-stick-slots", "round 2 N3 X3",
     [('''        raise Refuse("the installer stick's internal-disk entries do not match this disk's menu")''', "        pass")]),
    ("rollback-stick-kernel", "round 2 N4",
     [('''        if [sb.read(x) for x in [se["linux"], *se["initrd"]]] != [inst.fat.read(x) for x in [de["linux"], *de["initrd"]]]:''',
       '''        if [sb.read(x) for x in se["initrd"]] != [inst.fat.read(x) for x in de["initrd"]]:''')]),
    ("write-guard", "v0.1 write guard",
     [("            if off < b and off + len(data) > a:", "            if False:")]),
]


def run_one(m, work):
    mid, finding, edits = m
    t = time.time()
    top = Path(tempfile.mkdtemp(prefix=mid + ".", dir=work))
    try:
        src = ENGINE.read_text()
        for old, new in edits:
            if src.count(old) != 1:
                return mid, finding, "STALE", f"the original text occurs {src.count(old)} times (update this list)", 0
            src = src.replace(old, new)
        (top / "gbos-update.py").write_text(src)
        try:
            compile(src, str(top / "gbos-update.py"), "exec")
        except SyntaxError as e:
            return mid, finding, "STALE", f"the mutated engine does not compile: {e}", 0
        try:
            r = subprocess.run([sys.executable, str(HERE / "selftest.py"), str(top / "w")], capture_output=True, text=True,
                               env=dict(os.environ, GBOS_UPDATE_ENGINE=str(top / "gbos-update.py")), timeout=3600,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return mid, finding, "killed", "the self-test timed out (counts as red)", time.time() - t
        fails = [l[5:] for l in r.stdout.splitlines() if l.startswith("FAIL ")]
        if r.returncode == 0:
            return mid, finding, "SURVIVED", "", time.time() - t
        if not fails:          # red without a failed check (a crash of the self-test itself) proves nothing
            return mid, finding, "NO-FAIL", ((r.stderr.strip().splitlines() or [""])[-1])[:150], time.time() - t
        detail = fails[0] if fails else ((r.stderr.strip().splitlines() or [""])[-1])
        return mid, finding, "killed", f"{len(fails)} FAIL: {detail}"[:150], time.time() - t
    finally:
        shutil.rmtree(top, True)


def main(argv):
    jobs = 3
    if argv[:1] == ["-j"]:
        jobs, argv = int(argv[1]), argv[2:]
    todo = [m for m in MUTATIONS if not argv or m[0] in argv]
    work = Path(tempfile.mkdtemp(prefix="gbos-update-mutants-"))
    bad = 0
    try:
        with ThreadPoolExecutor(jobs) as ex:
            for mid, finding, verdict, detail, secs in ex.map(lambda m: run_one(m, work), todo):
                print(f"{verdict:9} {mid:26} {finding:22} {secs:5.0f}s  {detail}", flush=True)
                bad += verdict != "killed"
    finally:
        shutil.rmtree(work, True)
    print(f"{len(todo) - bad}/{len(todo)} mutations killed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
