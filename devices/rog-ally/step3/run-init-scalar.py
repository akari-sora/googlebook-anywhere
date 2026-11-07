#!/usr/bin/env python3
"""Linux-only real-entry four-cell acceptance, never executes the guest natively."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess

TOOLS = Path(__file__).resolve().parent
# Deliberate GFNI-capable/no-VNNI test CPU; capabilities are checked from each guest.
TCG_CPU = 'EPYC-Genoa-v3,+gfni'
spec = importlib.util.spec_from_file_location('patch_init', TOOLS / 'patch-init-scalar.py')
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)
FLAGS = ['-O1', '-march=x86-64', '-mno-avx', '-fno-tree-vectorize', '-nostdlib',
         '-static', '-fno-builtin', '-fno-stack-protector', '-fno-tree-loop-distribute-patterns',
         '-fno-pie', '-no-pie', '-mno-red-zone', '-Wall', '-Wextra', '-Werror',
         '-Wl,-Ttext-segment=0x1000000']


def cpio(exe):
    data = bytearray()
    for name, payload, mode in [('init', exe.read_bytes(), stat.S_IFREG | 0o755), ('TRAILER!!!', b'', 0)]:
        fields = [1, mode, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(name) + 1, 0]
        data.extend(b'070701' + b''.join(f'{x:08x}'.encode() for x in fields))
        data.extend(name.encode() + b'\0'); data.extend(b'\0' * (-len(data) % 4))
        data.extend(payload); data.extend(b'\0' * (-len(data) % 4))
    target = exe.with_suffix('.cpio')
    target.write_bytes(data)
    return target


def value(text, key):
    matches = re.findall(r'^' + re.escape(key) + r'=(0x[0-9a-f]+)$', text, re.M)
    patch.require(len(matches) == 1, 'missing/duplicate ' + key)
    return int(matches[0], 16)


def verify(rows):
    patch.require(len(rows) == 4 and {(r['state'], r['mode']) for r in rows} ==
                  {('original', 'native'), ('original', 'tcg'), ('patched', 'native'), ('patched', 'tcg')},
                  'four distinct cells required')
    completed = []
    for row in rows:
        out = row['output']
        native = row['mode'] == 'native'
        vnni = value(out, 'CPUID.7.1.EAX') & 16
        patch.require(bool(vnni) == native, 'wrong native/TCG VNNI capability')
        patch.require(value(out, 'CPUID.7.0.ECX') & (1 << 8), 'GFNI absent')
        ecx = value(out, 'CPUID.1.ECX')
        patch.require(ecx & (1 << 28) and ecx & (1 << 27), 'AVX/OSXSAVE absent')
        patch.require(value(out, 'CPUID.80000001.ECX') & (1 << 5), 'LZCNT absent')
        if row['state'] == 'original' and not native:
            patch.require(row['exit'] == ((0x40 + 4) * 2 + 1), 'wrong original guest exit')
            patch.require('ALL_PASS' not in out and 'FAULT_CAPTURED' in out, 'negative control did not fault')
            patch.require(value(out, 'SIGNAL') == 4 and value(out, 'SI_CODE') == 2, 'not SIGILL/ILL_ILLOPN')
            patch.require(value(out, 'FAULT_RIP') == 0x40000000 + 0x4b86e4, 'unexpected first fault (GFNI is not accepted)')
            patch.require(value(out, 'FAULT_BYTES5') == 0xc4c27152f5, 'unexpected fault bytes')
            cases = re.findall(r'^COMPLETED_CASES=(0x[0-9a-f]+)$', out, re.M)
            starts = re.findall(r'^BEGIN_COUNT=(0x[0-9a-f]+)$', out, re.M)
            patch.require(cases and int(cases[-1], 16) == 90 and starts and int(starts[-1], 16) == 8,
                          'tiny-count controls did not complete before count8')
        else:
            patch.require(row['exit'] == (0 if native else 1) and 'ALL_PASS\n' in out, 'consumer matrix failed')
            patch.require('FAULT_CAPTURED' not in out and value(out, 'CASES') == 250, 'incomplete matrix')
            completed.append(value(out, 'RESULT_DIGEST'))
    patch.require(len(completed) == 3 and len(set(completed)) == 1, 'consumer digests differ')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', required=True, type=Path)
    parser.add_argument('--patched', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path, help='new directory only')
    parser.add_argument('--qemu', required=True, type=Path)
    parser.add_argument('--kernel', required=True, type=Path)
    parser.add_argument('--firmware', required=True, type=Path)
    args = parser.parse_args()
    patch.require(os.uname().sysname == 'Linux' and os.uname().machine == 'x86_64', 'Linux x86-64 only')
    original = patch.plain_path(args.original).read_bytes()
    updated = patch.plain_path(args.patched).read_bytes()
    patch.require(updated == patch.transform(original), 'patched bytes do not equal exact reviewed two-byte edit')
    for path in (args.qemu, args.kernel, args.firmware):
        patch.plain_path(path)
    output = patch.plain_path(args.output, output=True)
    patch.require(not any(c in str(output) for c in (',', '\n', '\r')), 'unsafe QEMU log path')
    output.mkdir(mode=0o700)
    env = {**os.environ, 'LC_ALL': 'C'}
    rows = []
    commands = []
    def command(argv, timeout=120):
        commands.append([str(x) for x in argv])
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    try:
        tools = {}
        for name, argv in [('gcc', ['gcc', '--version']), ('qemu', [args.qemu, '--version']),
                           ('objdump', ['objdump', '--version'])]:
            proc = command(argv)
            patch.require(proc.returncode == 0, name + ' unavailable')
            tools[name] = proc.stdout
        for state, source in [('original', args.original), ('patched', args.patched)]:
            # Embed authenticated held bytes, not a path that may change between preflight and assembler.
            blob = output / (state + '.bin')
            blob.write_bytes(original if state == 'original' else updated)
            patch.require(not any(c in str(blob) for c in ('"', '\\', '\n')), 'unsafe incbin path')
            asm = output / (state + '.S')
            asm.write_text('.section .rodata\n.global target_blob,target_blob_end\ntarget_blob:\n.incbin "' +
                           str(blob) + '"\ntarget_blob_end:\n.section .note.GNU-stack,"",@progbits\n')
            for mode in ('native', 'tcg'):
                exe = output / (state + '-' + mode)
                defines = [] if mode == 'native' else ['-DPROBE_GUEST']
                result = command(['gcc', *FLAGS, *defines, TOOLS / 'probe-init-scalar.c', asm, '-o', exe])
                (output / (exe.name + '.compile.log')).write_text(result.stdout + result.stderr)
                patch.require(result.returncode == 0, 'compile failed: ' + exe.name)
                if mode == 'native':
                    dis = command(['objdump', '-d', '-M', 'intel', exe])
                    patch.require(dis.returncode == 0 and not re.search(r'\t(?:in|out|ins|outs)(?:b|w|l)?\s', dis.stdout),
                                  'native executable contains port I/O or disassembly failed')
                    result = command([exe])
                    text = result.stdout + result.stderr
                else:
                    archive = cpio(exe)
                    debug = output / (exe.name + '.debug.log')
                    result = command([args.qemu, '-no-user-config', '-nodefaults', '-bios', args.firmware,
                        '-machine', 'q35', '-accel', 'tcg,thread=single', '-cpu', TCG_CPU,
                        '-m', '1024', '-smp', '1', '-display', 'none', '-monitor', 'none', '-serial', 'none',
                        '-parallel', 'none', '-nic', 'none', '-no-reboot', '-kernel', args.kernel,
                        '-initrd', archive, '-append', 'console=ttynull rdinit=/init panic=-1 loglevel=3 nokaslr',
                        '-chardev', 'file,id=proof,path=' + str(debug),
                        '-device', 'isa-debugcon,iobase=0xe9,chardev=proof',
                        '-device', 'isa-debug-exit,iobase=0xf4,iosize=4'], timeout=180)
                    (output / (exe.name + '.qemu.log')).write_text(result.stdout + result.stderr)
                    text = debug.read_text() if debug.exists() else ''
                (output / (exe.name + '.log')).write_text(text)
                rows.append({'state': state, 'mode': mode, 'exit': result.returncode, 'output': text})
        verify(rows)
        verdict = 'PASS'
    finally:
        (output / 'result.json').write_text(json.dumps({'source_sha256': patch.digest(original),
            'patched_sha256': patch.digest(updated), 'kernel_sha256': patch.digest(args.kernel.read_bytes()),
            'firmware_sha256': patch.digest(args.firmware.read_bytes()), 'qemu_sha256': patch.digest(args.qemu.read_bytes()),
            'commands': commands, 'rows': rows, 'verdict': locals().get('verdict', 'FAIL_OR_INCOMPLETE'),
            'tool_versions': locals().get('tools', {})}, indent=2) + '\n')
    print('PASS: actual entry, 3 x 250 oracle/canary cases; original no-VNNI exact-site SIGILL after 90 tiny cases')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit('FAIL: ' + str(error))
