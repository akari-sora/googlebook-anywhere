/* Interpose __system_property_find so one process's libvulkan (android::base::GetProperty)
 * reads vendor.gbos.radv_probe instead of ro.hardware.vulkan. The rest of the system keeps
 * the gated property. LD_PRELOAD only on radv-probe.
 * Bionic's __system_property_read_callback(pi, cb, cookie) passes pi->value (inline char[92],
 * PROP_VALUE_MAX = 92) and pi->serial to the callback; android::base::GetProperty ignores the
 * prop_info name field, so returning the prop_info* for "vendor.gbos.radv_probe" is ABI-safe. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <string.h>
#include <unistd.h>

static void kmsg(const char *s) {
    int fd = open("/dev/kmsg", O_WRONLY | O_CLOEXEC);
    if (fd < 0)
        return;
    (void)write(fd, s, strlen(s));
    close(fd);
}

__attribute__((visibility("default")))
const void *__system_property_find(const char *name) {
    typedef const void *(*find_fn)(const char *);
    static find_fn real_find = 0;
    find_fn fn = __atomic_load_n(&real_find, __ATOMIC_ACQUIRE);
    if (!fn) {
        fn = (find_fn)dlsym(RTLD_NEXT, "__system_property_find");
        __atomic_store_n(&real_find, fn, __ATOMIC_RELEASE);
    }
    if (name && strcmp(name, "ro.hardware.vulkan") == 0) {
        kmsg("<6>vkprop: ro.hardware.vulkan -> vendor.gbos.radv_probe\n");
        return fn("vendor.gbos.radv_probe");
    }
    return fn(name);
}
