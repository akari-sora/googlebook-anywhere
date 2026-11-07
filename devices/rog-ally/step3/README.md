# `/init` repeated-string scalar gate: narrow repair

**Scope:** exact original `/init` only, SHA256
`02c8c532e922d215e0516ac967bd719072b24861ee3de7a9d81bc1934b00037f`,
3,137,864 bytes. Change file offsets `0x2b8495..0x2b8496` from `73 13`
to `90 90`. No other bytes, no trampoline, no original-file mutation.

The original count>=8 JAE at VA `0x4b8495` goes to a vector block containing
VNNI/GFNI. Removing that jump leaves the original `r9=0; rax=count` setup and
jump to the existing scalar tail at `0x4b87e1`. The complete real function is
VA `0x4b8440..0x4b8878` (1080 bytes); its independently reconstructed original
SHA256 is `094ff3994dd8294e4d6731183f2d62bd87ffde5a840bb5101d8b1894689deefc`.
Its patched function SHA256 is
`d78b932d6d453c3406177f17c1580ca1da14e126ada86e1d3d492c9d591cad0c`.
These are **function** hashes, not the patched whole-file hash. The latter is
produced by actual patch/readback, not invented here.

Evidence: retained `work/catalog/core-catalog-003.json` exact entry disassembly,
reviewed by the architect. Root separately bound Build7/Build13 ramdisk bytes.
The selected entry contains no calls/TLS/syscalls. The probe does not apply
relocations or load Android dependencies; it only calls this bounded entry.

## Run only on the approved Linux x86-64 research slot

Stage these source files only. Do not copy the private original ELF to the Mac.
Use a fresh disposable run directory, resolved paths without symlink components,
and the existing validated private original/kernel/QEMU/firmware paths. Never
execute the `*-tcg` executable natively: it contains guest-only port I/O.

```sh
export PYTHONDONTWRITEBYTECODE=1
python3 test-init-scalar.py
python3 -O test-init-scalar.py
# $RUN is an already-created fresh disposable directory, not the extracted tree.
python3 patch-init-scalar.py "$ORIGINAL_INIT" "$RUN/init.scalar" > "$RUN/patch.json"
python3 run-init-scalar.py \
  --original "$ORIGINAL_INIT" --patched "$RUN/init.scalar" \
  --output "$RUN/matrix" --qemu "$QEMU" --kernel "$KERNEL" --firmware "$FIRMWARE"
```

The patcher rejects unknown or already-modified input, changed function bytes,
wrong ELF executable mapping, wrong gate bytes, existing destinations, symlink
components, hardlinked inputs, special permissions and xattrs. It preserves the
input and output mode; writes and fsyncs a new file; performs full output and
input readback. Ownership, timestamps, ACL/capability/container/signing integration
are outside this repair. Exclusive local namespace ownership is required: this
is not a hostile concurrent-directory sandbox. No `--force`/hash override exists.
The earlier Adler patcher also requires original whole-file hashes: do not
blindly chain the two patchers. Reviewed composition is a separate gate.

## Private-payload fault hardening

Before mapping or calling the target, every probe build (native and guest) calls
Linux x86-64 `prctl(PR_SET_DUMPABLE, 0)` and `prctl(PR_SET_NO_NEW_PRIVS, 1)`.
Either error emits `PRIVACY_GUARD_FAIL` and exits nonzero without a target call.
This prevents a target SIGILL from creating a private embedded-ELF core dump;
it is not a claim about swap, audit/system logs or all kernel observability.
Native-only binary transport, exact-size/SHA memfd verification, core/resource
limits and alarms are separate caller-owned safeguards, not implemented here.
The transport must never execute a guest/port-I/O binary on physical hardware.

## Real-entry fixture contract

This is an **exact-machine ABI fixture**, not proof of a constructed C++ object,
process boot, or a reconstructed protobuf class. A zeroed 1024-byte aligned object
makes the callee's prefetch range legal. Fields are metadata +0x08=0,
hasbits +0x10=1, cache +0x14=sentinel, rep +0x18, int32 count +0x20. Other
fields stay zero. The cached size is the sole permitted changed input field.

For heap reps, the low tag bit is 1 and pointers begin at block+8. The header is
initialized but not consumed; no capacity ABI is claimed. Singleton cases test
both this heap rep and direct untagged string-object-pointer rep. Zero count does
not dereference the rep. Each string object occupies at least 24 bytes on an
aligned stride; short tag byte is length*2 for lengths<=15, long tag is odd and
uint64 length is at +8. Long data pointer at +16 is conventional and **not read**
by this entry; bounded real payload storage is supplied regardless. Every string,
rep, object and surrounding canary byte is compared with a snapshot after each
call, except the expected cache write.

Counts: 0..17,31,32,33,63,64,65. Length patterns: homogeneous
0,1,7,15,31,127,128,16383,16384 and a mixed pattern; singleton adds an inline
variant. Total **250 cases per complete cell**. Oracle is
`sum(1 + shift7_varint_bytes(length) + length)`, independent of the consumer's
LZCNT formula. Probe/oracle build is baseline `-march=x86-64 -mno-avx` with
compiler auto-vectorization disabled. The called entry still needs LZCNT and
AVX OS state (`vzeroupper`); **not generic x86-64 compatibility**.

## Acceptance, fail closed

Four distinct cells: original/native, patched/native, original/no-VNNI TCG,
patched/no-VNNI TCG. Native must report AVX-VNNI; TCG deliberately uses
`EPYC-Genoa-v3,+gfni` and must lack AVX-VNNI. This requests GFNI for the test CPU,
not to the binary or real hardware. Every native and TCG cell must report GFNI
(CPUID.7.0.ECX bit8), AVX/OSXSAVE and LZCNT; capabilities are checked from
actual probe output, never assumed from the model name. In particular the
first baseline model run reported `CPUID.7.0.ECX=0x0041021c` without GFNI
and correctly failed at GFNI before VNNI. The explicit `+gfni` attempt also
failed: this QEMU TCG build warned that GFNI is unsupported and dropped the
requested bit. Preserve both failed runs; neither is a successful four-cell
acceptance. Do not weaken this verifier or continue emulator experimentation.
An independently reviewed actual-Ally native route is separate evidence, not
a relabeling of either failed TCG matrix.
All three complete cells must pass 250 independent oracle/cache/canary cases
and share a result digest. Original TCG must first complete all 90 tiny-count
cases, then SIGILL/ILL_ILLOPN at count8, exact RIP `0x404b86e4` and bytes
`c4 c2 71 52 f5`. An earlier GFNI fault is **failure**, not accepted as VNNI
proof. QEMU timeout, missing feature, wrong signal/site, partial matrix or
changed output are failure. Guest execution is only via QEMU initramfs.
Tool versions, artifact hashes, commands, compile logs, signals and results
are retained in the new matrix directory. `result.json` records FAIL_OR_INCOMPLETE
on caught runtime failures rather than falsely reporting PASS.

## Local validation boundary

`test-init-scalar.py` uses synthetic ELF bytes and test-only in-process pins;
these pins cannot be supplied through the shipping CLI. It tests refusal,
exact two-byte changes, mode/readback, no-overwrite, fsync cleanup and matrix
verdict parsing, **not** real consumer execution. On macOS Python without
`os.listxattr`, that syscall is mocked only inside tests; production fails closed.
The source requires Linux GCC/headers for the C build and real private artifact
native/TCG results before acceptance. No image rebuild, whole-ELF VNNI coverage,
other `/init` helper repair, firststage linker repair or Ally boot is proven here.
