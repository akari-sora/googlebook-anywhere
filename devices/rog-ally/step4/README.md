# Firststage linker64: three bounded scalar gate repairs

**Source-only implementation; actual Linux build/private-artifact execution is
pending.** No image assembly, C++ construction, full linker execution or boot
acceptance is claimed. This directory is independent of step3; neither its
patcher nor its TCG acceptance gate is modified.

## Exact artifact and changes

Only original `first_stage_ramdisk/system/bin/linker64`, size **2,542,872** bytes,
SHA256 `3631a5c65adecb306579974a74ea27cdfabf28ee264676136139f7b15922e3ea`.
All three VAs equal file offsets:

| Function | Gate | Old → new | Preserved fallthrough and full scalar tail |
|---|---|---|---|
| Tombstone::ByteSizeLong | `0x18e18e` | `73 12` → `90 90` | `esi=0; r14=count`; tail `0x18ec3d` |
| Thread::ByteSizeLong, first string field | `0x192151` | `73 0f` → `90 90` | `esi=0`; tail `0x19245c` |
| Thread::ByteSizeLong, second string field | `0x192453` | `73 5f` → `90 90` | `esi=0`; tail `0x19286c` |

Both Thread fields already add count to `r14` **before** the gate. NOPing only
the JAE preserves that initial wire-tag contribution and the accumulator from
earlier fields. No direct jump into an uninitialized tail, trampoline, register
scratch area or instruction emulation is used. Exactly six file bytes change.

The exact original PT_LOAD table is pinned (offset,VA,filesz,memsz,flags):

```
(0,       0,       414064,  414064, 4)
(425984,  425984,  1553712, 1553712,5)
(1982464, 1982464, 38736,   40960,  6)
(2031616, 2031616, 9776,    99808,  6)
```

Independently reconstructed from **every contiguous retained raw instruction**
in `work/catalog/core-catalog-001.json`; reconstruction checked each address,
byte length, gate old bytes, and the exact final bound (no gap filling):

| Function range | Length | Original whole-function SHA256 | Patched whole-function SHA256 |
|---|---:|---|---|
| `0x18e120..0x18ecc8` | 2984 | `dce46a5ad44ce2ffb06740130cdaa25ef6d70727c5022487b236df1362a16e64` | `4773a07d80594af2f24abbce5ebf2a46e578d4fb36dd054ecdc48a414cce2074` |
| `0x191fb0..0x1928f8` | 2376 | `0b2ba4a4d54c956b506fe0130d818b8c17439be523162ec1c7ea84d8bc7f90cf` | `3df086df0ae0d6fe0dd5168534b769a96319f14eb5e95e8aa359200d6183de47` |

These are **function** hashes. The patched **whole-file** hash must come from
actual patch/readback on the approved remote slot. The patcher pins original
whole-file SHA, both complete function hashes, executable mapping, entire
PT_LOAD table and all three old gates, and checks both resulting function hashes.
No force/hash override exists. It refuses already-patched files, existing
outputs, symlink components, hardlinked input, special mode bits and xattrs.
Original input is never mutated; a new exclusive output preserves mode, is
fsynced, and undergoes full readback. Input is also reread. Ownership/timestamps,
ACL/capability/container/signing integration are not implemented. As in step3,
this assumes an exclusively owned disposable namespace, not hostile concurrent
directory writers. Do not chain with the Adler patcher: it requires original
whole-file SHA too. Reviewed composition remains a separate gate.

## Real-entry machine fixtures

`probe-linker-scalar.c` has four compile-time kinds:

- 0: Tombstone entry `0x18e120`, hasbits=1, rep +0x18, count +0x20.
- 1: Thread entry `0x191fb0`, hasbits=8, rep +0x60, count +0x68.
- 2: Thread entry `0x191fb0`, hasbits=16, rep +0x78, count +0x80.
- 3: same Thread entry, hasbits=24, both fields active and independently summed.

All are **exact-machine ABI fixtures, not constructed C++ class objects**. The
1024-byte aligned object has metadata +0x08=0, hasbits uint32 +0x10, cache uint32
+0x14=sentinel. All unrelated fields are zero; bytes from +0x180 onward are
canaries and the prefetches through +0x2c0 remain inside allocated memory.
The retained full functions do contain calls, but those calls are in excluded
hasbits/repeated-message/map blocks. With the selected bits and all other
fields/counts zero, neither entry calls another function or needs TLS, a dynamic
relocation, or Android dependency initialization. This is a selected-path claim,
not a claim that the whole functions or linker are self-contained in general.

Heap reps have a low-bit tag and pointer slots starting at block+8; singleton
also tests direct untagged string-object-pointer rep. The unread header is not
claimed to reconstruct protobuf capacity. Strings have >=24-byte headers on an
aligned stride and real bounded payloads, short tag `2*length` for lengths<=15,
or odd long tag plus uint64 length at +8. Conventional long data pointer +16 is
supplied but **not consumed** by this entry. Every string, rep, object and
canary byte is compared with its snapshot after the call, except the expected
cached-size write. Oracle independently computes
`sum(1 + shift7_varint_bytes(length) + length)` for each active field; cache must
match its uint32 result. It does not copy the LZCNT formula.

Each kind covers counts 0..17,31,32,33,63,64,65 and homogeneous lengths
0,1,7,15,31,127,128,16383,16384 plus mixed lengths; singleton adds inline rep.
This gives **250 cases** for each of kinds0/1/2. Kind3 has the same 250 with
independent second-field length pattern, then **10 asymmetric count pairs**
(0/1,1/0,1/7,7/1,7/8,8/7,8/9,9/8,31/65,65/31), total **260**. Its first 90
complete cases use counts<8 in both fields. An original count8 fault is reached
separately for every kind, not hidden by an earlier Tombstone failure.

Every native/guest build retains `PR_SET_DUMPABLE=0` and
`PR_SET_NO_NEW_PRIVS=1` before target mapping/call; either error exits nonzero.
Native transport core/resource limits, alarms, exact binary memfd validation
and staging are caller-owned safeguards. No assertion about swap/audit/system
logs is made. Guest executables contain port I/O: **never execute them natively**.

## Linux acceptance commands (root-owned remote slot only)

Stage these source files, not private ELF bytes. Use resolved paths without
symlinks, a new disposable `$RUN`, and already-validated QEMU/kernel/firmware.

```sh
export PYTHONDONTWRITEBYTECODE=1
python3 test-linker-scalar.py
python3 -O test-linker-scalar.py
python3 patch-linker-scalar.py "$ORIGINAL_LINKER64" "$RUN/linker64.scalar" > "$RUN/patch.json"
python3 run-linker-scalar.py \
  --original "$ORIGINAL_LINKER64" --patched "$RUN/linker64.scalar" \
  --output "$RUN/matrix" --qemu "$QEMU" --kernel "$KERNEL" --firmware "$FIRMWARE"
```

The runner authenticates patched bytes against the exact transform, then builds
native/original, native/patched, TCG/original, TCG/patched for **each kind**:
16 distinct cells. Compiler baseline, PT_LOAD loader, signal capture and guest
archive follow step3. All three complete cells per kind must match oracle,
cache, canaries, full case count and digest. Native original needs GFNI and
AVX-VNNI. TCG must lack AVX-VNNI; AVX/OSXSAVE and LZCNT remain required. The
TCG CPU stays baseline `EPYC-Genoa-v3`: **no feature-request/fix loops**.

Original TCG negative controls require SIGILL/ILL_ILLOPN, exit137, 90 completed
tiny cases and next count8. Exact first fault classification is bound to actual
CPUID.GFNI:

| Kind | GFNI absent: exact first GFNI site | GFNI present: exact first VNNI site |
|---|---|---|
| 0 | `0x18e310` | `0x18e3e3` |
| 1 | `0x1922d1` | `0x1923a4` |
| 2 | `0x192621` | `0x1926f4` |
| 3 | `0x1922d1` | `0x1923a4` |

Add load base `0x40000000` to fault RIP. The first5 fault bytes must be
`c4c3f1cec9` (GFNI) or `c4c27152f5` (VNNI), respectively. Unrelated sites,
signals, bytes, partial results or capability mismatches fail. A GFNI-first
negative is explicitly `GFNI_FIRST_UNSUPPORTED_NOT_VNNI_ISOLATED`; it must not
be called VNNI isolation. Complete bounded semantic proof with that control
uses verdict `PASS_SCALAR_CONSUMERS_GFNI_NEGATIVE_CONTROL_NOT_VNNI_ISOLATED`.
Only exact VNNI-first controls with actual GFNI may produce
`PASS_SCALAR_CONSUMERS_VNNI_ISOLATED`. Neither verdict proves generic x86-64
compatibility, remaining linker helpers, full-image CPU compatibility or boot.
All logs, build commands, tool versions, artifact hashes and classifications
are retained; incomplete runs record `FAIL_OR_INCOMPLETE`.

## Local validation boundary

Synthetic tests pin test fixture bytes only inside the test process. They cover
six-byte-only mutation, both function pins, all old gates, no overwrite/input
mutation, mode/readback/fsync, unsafe paths, privacy source ordering and strict
verdict classifications. They do **not** execute the actual linker entry. On
macOS without Python `os.listxattr`, only tests mock that syscall; production
fails closed. Real Linux GCC/header build and actual private-artifact native/TCG
runs remain pending until root records their outputs. No private ELF has been
copied locally and no remote/device/build/integration action is done by this
source-only slice.
