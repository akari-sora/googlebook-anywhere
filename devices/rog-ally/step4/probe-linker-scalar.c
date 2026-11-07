/* Remote-only baseline loader. Native: write/exit/mmap only, no device I/O.
 * PROBE_GUEST: QEMU-only debug ports, never compile/run this variant natively.
 */
#define _GNU_SOURCE
#include <stdint.h>
#include <elf.h>
#include <signal.h>
#include <sys/ucontext.h>

extern unsigned char target_blob[];
extern unsigned char target_blob_end[];


/* Generated from a preflighted manifest; never infer addresses from a name. */
#define TARGET_BASE 0x40000000ULL
#ifndef PROBE_KIND
#error Define PROBE_KIND: 0 Tombstone, 1 Thread first, 2 Thread second, 3 Thread both
#endif
#if PROBE_KIND < 0 || PROBE_KIND > 3
#error Invalid PROBE_KIND
#endif
#define TARGET_ENTRY (PROBE_KIND == 0 ? 0x18e120ULL : 0x191fb0ULL)

static long sys(long n, long a, long b, long c, long d, long e, long f) {
    register long r10 __asm__("r10") = d, r8 __asm__("r8") = e, r9 __asm__("r9") = f;
    long ret;
    __asm__ volatile("syscall" : "=a"(ret)
                     : "a"(n), "D"(a), "S"(b), "d"(c), "r"(r10), "r"(r8), "r"(r9)
                     : "rcx", "r11", "memory");
    return ret;
}

static void put(const char *s) {
#ifdef PROBE_GUEST
    while (*s) __asm__ volatile("outb %0,$0xe9" : : "a"(*s++));
#else
    long n = 0;
    while (s[n]) n++;
    sys(1, 1, (long)s, n, 0, 0, 0);
#endif
}

static void hex(uint64_t x) {
    char s[20] = "0x";
    for (int i = 0; i < 16; i++) s[i + 2] = "0123456789abcdef"[(x >> (60 - 4 * i)) & 15];
    s[18] = '\n'; s[19] = 0;
    put(s);
}

__attribute__((noreturn)) static void done(int code) {
#ifdef PROBE_GUEST
    __asm__ volatile("outl %0,$0xf4" : : "a"((unsigned)code));
#endif
    sys(60, code, 0, 0, 0, 0, 0);
    for (;;) {}
}

__attribute__((naked)) static void restore(void) {
    __asm__ volatile("mov $15,%rax; syscall");
}

static void fault(int signal, siginfo_t *info, void *context) {
    ucontext_t *uc = context;
    put("SIGNAL="); hex(signal);
    put("SI_CODE="); hex(info->si_code);
    put("FAULT_RIP="); hex(uc->uc_mcontext.gregs[REG_RIP]);
    unsigned char *ip = (void *)uc->uc_mcontext.gregs[REG_RIP];
    uint64_t bytes = 0;
    for (unsigned i = 0; i < 5; i++) bytes = (bytes << 8) | ip[i];
    put("FAULT_BYTES5="); hex(bytes);
    put("FAULT_CAPTURED\n");
    done(0x40 + signal);
}

static void signals(void) {
    struct { void (*handler)(int, siginfo_t *, void *); unsigned long flags;
             void (*restorer)(void); uint64_t mask; } action = {fault, 0x04000004, restore, 0};
    if (sys(13, SIGILL, (long)&action, 0, 8, 0, 0) != 0 ||
        sys(13, SIGSEGV, (long)&action, 0, 8, 0, 0) != 0) {
        put("SIGNAL_SETUP_FAIL\n"); done(2);
    }
}

static void cpuid(unsigned leaf, unsigned sub, unsigned *a, unsigned *b, unsigned *c, unsigned *d) {
    __asm__ volatile("cpuid" : "=a"(*a), "=b"(*b), "=c"(*c), "=d"(*d) : "0"(leaf), "2"(sub));
}

static void load(uint64_t base) {
    Elf64_Ehdr *e = (void *)target_blob;
    uint64_t lo = UINT64_MAX, hi = 0;
    for (int i = 0; i < e->e_phnum; i++) {
        Elf64_Phdr *p = (void *)(target_blob + e->e_phoff + i * e->e_phentsize);
        if (p->p_type != PT_LOAD || !p->p_memsz) continue;
        uint64_t start = (base + p->p_vaddr) & ~4095ULL;
        uint64_t end = (base + p->p_vaddr + p->p_memsz + 4095) & ~4095ULL;
        if (start < lo) lo = start;
        if (end > hi) hi = end;
    }
    /* One fresh zero-filled mapping preserves shared PT_LOAD boundary pages.
     * NOREPLACE fails instead of destroying the probe or any existing mapping. */
    if (lo >= hi || (uint64_t)sys(9, lo, hi - lo, 7, 0x100022, -1, 0) != lo) {
        put("MMAP_FAIL\n"); done(2);
    }
    for (int i = 0; i < e->e_phnum; i++) {
        Elf64_Phdr *p = (void *)(target_blob + e->e_phoff + i * e->e_phentsize);
        if (p->p_type != PT_LOAD || !p->p_memsz) continue;
        for (uint64_t j = 0; j < p->p_filesz; j++)
            ((unsigned char *)(base + p->p_vaddr))[j] = target_blob[p->p_offset + j];
    }
}

/* Exact-machine ABI fixtures, NOT proof of a constructed C++ object. The entry
 * reads only the tagged rep/string lengths in these selected hasbits=1 cases.
 * Real payload storage is supplied even though this entry does not read it.
 */
#define MAX_COUNT 65
#define MAX_LENGTH 16384
struct string_fixture { unsigned char before[32], body[24], payload[MAX_LENGTH + 8], after[32]; };
_Static_assert(sizeof(struct string_fixture) % 8 == 0, "aligned string stride");
static struct string_fixture strings[2][MAX_COUNT] __attribute__((aligned(32)));
static unsigned char saved_strings[sizeof(strings)];
static uint64_t rep[2][MAX_COUNT + 9] __attribute__((aligned(32)));
static uint64_t saved_rep[2][MAX_COUNT + 9];
static unsigned char object[1024] __attribute__((aligned(32)));
static unsigned char saved_object[1024];
static const unsigned lengths[] = {0, 1, 7, 15, 31, 127, 128, 16383, 16384};
static const unsigned counts[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
                                 13, 14, 15, 16, 17, 31, 32, 33, 63, 64, 65};
static void copy(void *dst, const void *src, uint64_t n) {
    unsigned char *d = dst; const unsigned char *s = src;
    for (uint64_t i = 0; i < n; i++) d[i] = s[i];
}
static int equal(const void *a, const void *b, uint64_t n) {
    const unsigned char *x = a, *y = b;
    for (uint64_t i = 0; i < n; i++) if (x[i] != y[i]) return 0;
    return 1;
}
static void store64(void *p, uint64_t x) { copy(p, &x, sizeof(x)); }
static void store32(void *p, uint32_t x) { copy(p, &x, sizeof(x)); }
static uint32_t read32(void *p) { uint32_t x; copy(&x, p, sizeof(x)); return x; }
static unsigned varint_bytes(unsigned n) {
    unsigned bytes = 1;
    while (n >= 128) { n >>= 7; bytes++; }
    return bytes;
}
static uint64_t fill_field(unsigned field, unsigned n, unsigned pattern, unsigned inline_rep,
                           unsigned object_offset) {
    rep[field][0] = n; /* Unread header, not a reconstructed capacity ABI. */
    uint64_t expected = 0;
    for (unsigned i = 0; i < n; i++) {
        unsigned len = lengths[pattern < 9 ? pattern : (i + field * 3) % 9];
        unsigned char *s = strings[field][i].body;
        for (unsigned j = 0; j < sizeof(strings[field][i].body); j++) s[j] = 0;
        if (len <= 15) {
            s[0] = (unsigned char)(len * 2);
            for (unsigned j = 0; j < len; j++) s[j + 1] = 's';
        } else {
            store64(s, (len + 1) | 1ULL); /* Only low tag bit is consumed. */
            store64(s + 8, len);
            store64(s + 16, (uint64_t)strings[field][i].payload); /* Conventional, unread. */
        }
        for (unsigned j = 0; j < len; j++) strings[field][i].payload[j] = 'p';
        strings[field][i].payload[len] = 0;
        rep[field][i + 1] = (uint64_t)s;
        expected += 1 + varint_bytes(len) + len;
    }
    store64(object + object_offset, inline_rep ? (uint64_t)strings[field][0].body : ((uint64_t)rep[field] | 1));
    store32(object + object_offset + 8, n);
    return expected;
}
static uint64_t fixture(unsigned first, unsigned second, unsigned pattern, unsigned inline_rep) {
    for (unsigned i = 0; i < sizeof(strings); i++) ((unsigned char *)strings)[i] = 0xa5;
    for (unsigned i = 0; i < sizeof(object); i++) object[i] = 0;
    for (unsigned i = 384; i < sizeof(object); i++) object[i] = 0xc7;
    for (unsigned f = 0; f < 2; f++) for (unsigned i = 0; i < MAX_COUNT + 9; i++)
        rep[f][i] = 0xababababababababULL;
    uint64_t expected = 0;
#if PROBE_KIND == 0
    store32(object + 0x10, 1);
    expected = fill_field(0, first, pattern, inline_rep, 0x18);
#elif PROBE_KIND == 1
    store32(object + 0x10, 8);
    expected = fill_field(0, first, pattern, inline_rep, 0x60);
#elif PROBE_KIND == 2
    store32(object + 0x10, 16);
    expected = fill_field(1, second, pattern, inline_rep, 0x78);
#else
    store32(object + 0x10, 24);
    expected = fill_field(0, first, pattern, inline_rep && first == 1, 0x60);
    expected += fill_field(1, second, pattern < 9 ? (pattern + 3) % 9 : pattern,
                           inline_rep && second == 1, 0x78);
#endif
    (void)second; (void)first;
    store32(object + 0x14, 0xdeadbeef);
    copy(saved_object, object, sizeof(object));
    copy(saved_rep, rep, sizeof(rep));
    copy(saved_strings, strings, sizeof(strings));
    return expected;
}
static uint64_t run_case(uint64_t (*consumer)(void *), unsigned first, unsigned second,
                         unsigned pattern, unsigned inline_rep) {
    uint64_t want = fixture(first, second, pattern, inline_rep);
    put("BEGIN_FIRST_COUNT="); hex(first); put("BEGIN_SECOND_COUNT="); hex(second);
    put("BEGIN_PATTERN="); hex(pattern);
    uint64_t got = consumer(object);
    if (got != want || read32(object + 0x14) != (uint32_t)want) {
        put("GOT="); hex(got); put("WANT="); hex(want); put("VALUE_FAIL\n"); done(1);
    }
    store32(saved_object + 0x14, (uint32_t)want);
    if (!equal(object, saved_object, sizeof(object)) || !equal(rep, saved_rep, sizeof(rep)) ||
        !equal(strings, saved_strings, sizeof(strings))) { put("CANARY_FAIL\n"); done(1); }
    return got;
}
__attribute__((used, noinline, noreturn)) static void probe_main(void) {
#ifdef PROBE_GUEST
    if (sys(173, 0xe9, 1, 1, 0, 0, 0) || sys(173, 0xf4, 4, 1, 0, 0, 0)) done(2);
#endif
    /* Linux x86-64 prctl: block private embedded-ELF core dumps before any
     * target mapping/call, and forbid privilege gain. Never continue on error.
     * Native transport resource limits are a separate caller responsibility. */
    if (sys(157, 4, 0, 0, 0, 0, 0) != 0 ||
        sys(157, 38, 1, 0, 0, 0, 0) != 0) {
        put("PRIVACY_GUARD_FAIL\n"); done(2);
    }
    signals();
    unsigned a, b, c, d;
    cpuid(1, 0, &a, &b, &c, &d); put("CPUID.1.ECX="); hex(c);
    cpuid(7, 0, &a, &b, &c, &d); put("CPUID.7.0.ECX="); hex(c);
    cpuid(7, 1, &a, &b, &c, &d); put("CPUID.7.1.EAX="); hex(a);
    cpuid(0x80000001, 0, &a, &b, &c, &d); put("CPUID.80000001.ECX="); hex(c);
    load(TARGET_BASE);
    uint64_t (*consumer)(void *) = (void *)(TARGET_BASE + TARGET_ENTRY);
    uint64_t cases = 0, hash = 1469598103934665603ULL;
    put("PROBE_KIND="); hex(PROBE_KIND);
    for (unsigned ci = 0; ci < sizeof(counts) / sizeof(counts[0]); ci++) {
        unsigned n = counts[ci];
        for (unsigned pattern = 0; pattern < 10; pattern++) {
            for (unsigned inline_rep = 0; inline_rep <= (n == 1); inline_rep++) {
                uint64_t got = run_case(consumer, n, n, pattern, inline_rep);
                hash = (hash ^ got) * 1099511628211ULL; cases++;
                put("COMPLETED_CASES="); hex(cases);
            }
        }
    }
#if PROBE_KIND == 3
    /* Asymmetric fields prove both count contributions/rep offsets independently. */
    const unsigned pairs[][2] = {{0,1}, {1,0}, {1,7}, {7,1}, {7,8}, {8,7}, {8,9}, {9,8}, {31,65}, {65,31}};
    for (unsigned i = 0; i < sizeof(pairs) / sizeof(pairs[0]); i++) {
        uint64_t got = run_case(consumer, pairs[i][0], pairs[i][1], 9, 1);
        hash = (hash ^ got) * 1099511628211ULL; cases++;
        put("COMPLETED_CASES="); hex(cases);
    }
#endif
    put("CASES="); hex(cases); put("RESULT_DIGEST="); hex(hash);
    put("ALL_PASS\n"); done(0);
}
__attribute__((naked, noreturn)) void _start(void) {
    __asm__ volatile("xor %ebp,%ebp; andq $-16,%rsp; call probe_main; ud2");
}
