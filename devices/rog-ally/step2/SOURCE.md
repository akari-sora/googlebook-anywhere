# Restored Adler source

These two files are byte-for-byte copies of previously reviewed Adler patch
source and metadata, restored on 2026-10-25.
They contain source and reviewed metadata, not private ELF payloads.

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `patch-adler-step2.py` | 11100 | `f0dec511b0ef4e5aa69f7257e918d65a1eb7752843cc1258647f7279008c785a` |
| `adler-manifest-step2.json` | 36138 | `17705a5bf9250c8704a0d0d048e10bf213fb333e7c542d5b1c45d49af30d6b8e` |

The original standalone CLI remains unchanged. Its historical 15-artifact scope
does not establish image integration or whole-image CPU compatibility.
`builder/lib/ally_bootstrap.py` reuses only its bytes-level validator for the
two explicitly named initrd members; container parsing, metadata preservation,
final ESP readback, Android boot and physical acceptance are separate gates.
