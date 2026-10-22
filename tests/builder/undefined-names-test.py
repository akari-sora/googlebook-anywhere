#!/usr/bin/env python3
"""Static check: every name a builder Python script reads at run time is bound somewhere.

py_compile accepts a script that reads a name nothing defines (builder/image.py once read
DROP after its definition was removed; the NameError surfaced only in a real stage-2 build).
This test uses the stdlib symtable: for each script it collects the module-scope bindings
(assignments, imports, def/class, for/with/except targets, walrus, and names a function
declares `global` and assigns), then walks every scope (module, class, function,
comprehension, lambda) and reports each name that the scope reads from the global
namespace but that is neither bound at module scope nor a builtin.

Scripts that create globals dynamically (exec, globals()[...] =, from x import *) cannot be
checked this way; they must be listed in ALLOW with a reason, and the test refuses an
unlisted one instead of guessing. No network, no imports of the scripts themselves.
"""
import ast
import builtins
import symtable
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
GLOBS = ["builder/*.py", "builder/lib/*.py", "builder/kmod/*.py", "builder/ifma/*.py"]
# path relative to ROOT -> reason. Empty: no builder script uses exec/globals()/star imports today.
ALLOW: dict[str, str] = {}
# module attributes the interpreter provides to every module
IMPLICIT = {"__name__", "__file__", "__doc__", "__builtins__", "__spec__", "__loader__",
            "__package__", "__annotations__", "__cached__", "__dict__"}
BUILTINS = set(dir(builtins))


def is_dunder(name):
    """Every __x__ name counts as bound. The compiler adds hidden module-level names that change
    between Python versions (3.14, PEP 649, adds __conditional_annotations__ and __annotate__).
    symtable lists them like normal reads and marks nothing as compiler-made, so we cannot tell
    a compiler-added dunder from a user-written one that is never defined. We skip every dunder,
    so no current or future implicit name gives a false positive. The cost: the test misses a
    user-written undefined dunder, which no builder script reads anyway."""
    return len(name) > 4 and name.startswith("__") and name.endswith("__") and name[2:-2].strip("_") != ""


def unbound(reads, bound):
    return {n for n, _ in reads if n not in bound and not is_dunder(n)}


def scripts(root):
    out = []
    for g in GLOBS:
        out += sorted(root.glob(g))
    return out


def dynamic_tricks(tree):
    """Constructs that bind globals symtable cannot see."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            found.append(f"line {node.lineno}: from {node.module} import *")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("exec", "globals"):
            found.append(f"line {node.lineno}: {node.func.id}()")
    return found


def _walk(table):
    yield table
    for child in table.get_children():
        yield from _walk(child)


def module_bindings(top):
    bound = set()
    for s in top.get_symbols():
        if s.is_assigned() or s.is_imported() or s.is_namespace() or s.is_parameter():
            bound.add(s.get_name())
    for t in _walk(top):
        if t is top:
            continue
        for s in t.get_symbols():
            if s.is_declared_global() and (s.is_assigned() or s.is_imported() or s.is_namespace()):
                bound.add(s.get_name())
    return bound


def global_reads(top):
    """(name, scope description) for every read that resolves in the module namespace."""
    reads = []
    for t in _walk(top):
        where = "module" if t is top else f"{t.get_type()} {t.get_name()} (line {t.get_lineno()})"
        for s in t.get_symbols():
            if not s.is_referenced():
                continue
            if t is top:
                if not s.is_assigned() and not s.is_imported() and not s.is_namespace():
                    reads.append((s.get_name(), where))
            elif s.is_global():
                reads.append((s.get_name(), where))
            elif t.get_type() == "class" and not s.is_local() and not s.is_free():
                reads.append((s.get_name(), where))
    return reads


def undefined_names(path):
    src = path.read_text()
    top = symtable.symtable(src, str(path), "exec")
    bound = module_bindings(top) | BUILTINS | IMPLICIT
    return sorted({(n, w) for n, w in global_reads(top) if n not in bound and not is_dunder(n)})


def main():
    passed = failed = 0
    files = scripts(ROOT)
    if not files:
        print("FAIL no builder scripts found under", ROOT)
        failed += 1
    for p in files:
        rel = str(p.relative_to(ROOT))
        tricks = dynamic_tricks(ast.parse(p.read_text(), rel))
        if tricks and rel not in ALLOW:
            print(f"FAIL {rel}: binds names dynamically ({'; '.join(tricks)}); list it in ALLOW with a reason")
            failed += 1
            continue
        if rel in ALLOW:
            print(f"SKIP-ALLOWED {rel}: {ALLOW[rel]}")
            passed += 1
            continue
        bad = undefined_names(p)
        if bad:
            for n, w in bad:
                print(f"FAIL {rel}: name {n!r} is read in {w} but never bound")
            failed += 1
        else:
            print(f"PASS {rel}: no undefined names")
            passed += 1
    for rel in ALLOW:
        if not (ROOT / rel).exists():
            print(f"FAIL stale ALLOW entry {rel}")
            failed += 1
    # self-check: the detector finds a removed definition and accepts the usual binding forms
    probe = ("import os\nfrom re import sub as S\nA = 1\nfor B in []: pass\nwith open(__file__) as C: pass\n"
             "try: pass\nexcept Exception as E: pass\ndef f(x):\n    global G\n    G = x\n    return [y for y in (A, B, C, G, S, os, x, len)]\n"
             "class K:\n    v = A\n    def m(self): return K, DROP_GONE\n(W := 2)\nprint(W, MISSING_TOP)\n")
    top = symtable.symtable(probe, "<probe>", "exec")
    got = unbound(global_reads(top), module_bindings(top) | BUILTINS | IMPLICIT)
    if got == {"DROP_GONE", "MISSING_TOP"}:
        print("PASS self-check: detector reports exactly the two unbound names"); passed += 1
    else:
        print("FAIL self-check: got", sorted(got)); failed += 1
    # self-check: the 3.14 (PEP 649) implicit name and any future dunder are not flagged; a plain
    # unbound name in the same sample still is
    probe = ("x: int = 1\nif x:\n    y: str = 'a'\nprint(__conditional_annotations__, __annotate__, __future_implicit__)\n"
             "def g():\n    return __conditional_annotations__, DROP\n")
    top = symtable.symtable(probe, "<probe-dunder>", "exec")
    reads = {n for n, _ in global_reads(top)}
    got = unbound(global_reads(top), module_bindings(top) | BUILTINS | IMPLICIT)
    if "__conditional_annotations__" in reads and got == {"DROP"}:
        print("PASS self-check: implicit dunders (__conditional_annotations__) not flagged, DROP still flagged"); passed += 1
    else:
        print("FAIL self-check dunder: reads", sorted(reads), "flagged", sorted(got)); failed += 1
    print(f"undefined-name tests: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
