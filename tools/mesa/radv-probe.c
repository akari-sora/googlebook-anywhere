/* Ask the system Vulkan loader to open the ICD named by ro.hardware.vulkan.
 * radv-probe is started with LD_PRELOAD=libvkprop.so, which makes that lookup
 * return "radeon" even when the gated property is pastel. Logs are error-priority
 * so the serial logcat filter ('*:E') keeps them. */
#include <android/log.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <vulkan/vulkan.h>

static void log_both(const char *fmt, ...) {
    char buf[900];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    __android_log_print(ANDROID_LOG_ERROR, "radv-probe", "%s", buf);
    int fd = open("/dev/kmsg", O_WRONLY | O_CLOEXEC);
    if (fd < 0)
        return;
    char line[960];
    snprintf(line, sizeof line, "<3>radv-probe: %s\n", buf);
    (void)write(fd, line, strlen(line));
    close(fd);
}

#define LOG(...) log_both(__VA_ARGS__)

static void log_maps(void) {
    FILE *f = fopen("/proc/self/maps", "r");
    char line[512];
    if (!f)
        return;
    while (fgets(line, sizeof line, f)) {
        if (strstr(line, "vulkan.radeon.so") || strstr(line, "libvulkan.so")) {
            size_t n = strlen(line);
            if (n && line[n - 1] == '\n')
                line[n - 1] = 0;
            LOG("mapped %s", line);
        }
    }
    fclose(f);
}

int main(void) {
    VkInstance inst = VK_NULL_HANDLE;
    VkInstanceCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    VkResult r = vkCreateInstance(&ci, NULL, &inst);
    LOG("vkCreateInstance -> %d", (int)r);
    log_maps();
    if (r != VK_SUCCESS)
        return 1;
    uint32_t n = 0;
    r = vkEnumeratePhysicalDevices(inst, &n, NULL);
    LOG("vkEnumeratePhysicalDevices -> %d count=%u", (int)r, n);
    if (r != VK_SUCCESS) {
        vkDestroyInstance(inst, NULL);
        return 3;
    }
    VkResult r2 = VK_SUCCESS;
    if (n) {
        VkPhysicalDevice devs[8];
        uint32_t m = n > 8 ? 8 : n;
        r2 = vkEnumeratePhysicalDevices(inst, &m, devs);
        LOG("vkEnumeratePhysicalDevices(devs) -> %d enumerated=%u", (int)r2, m);
        if (r2 == VK_SUCCESS || r2 == VK_INCOMPLETE) {
            for (uint32_t i = 0; i < m; i++) {
                VkPhysicalDeviceProperties p;
                vkGetPhysicalDeviceProperties(devs[i], &p);
                LOG("device %u vendor=0x%x device=0x%x name=%s", i, p.vendorID, p.deviceID, p.deviceName);
            }
        }
    }
    vkDestroyInstance(inst, NULL);
    return (r2 == VK_SUCCESS || r2 == VK_INCOMPLETE) ? 0 : 4;
}
