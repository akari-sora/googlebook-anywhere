// SPDX-License-Identifier: GPL-2.0-only
/* KANO keyboard backlight through the existing ChromeOS EC transport. */
#include <linux/acpi.h>
#include <linux/device.h>
#include <linux/errno.h>
#include <linux/leds.h>
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/platform_data/cros_ec_commands.h>
#include <linux/platform_data/cros_ec_proto.h>
#include <linux/slab.h>

struct gbos_kbd {
	struct led_classdev led;
	struct cros_ec_device *ec;
};

static int gbos_kbd_get(struct gbos_kbd *kbd)
{
	struct ec_params_pwm_get_duty *params;
	struct ec_response_pwm_get_duty *response;
	struct cros_ec_command *msg;
	int ret;

	msg = kzalloc(sizeof(*msg) + max(sizeof(*params), sizeof(*response)),
		      GFP_KERNEL);
	if (!msg)
		return -ENOMEM;
	msg->command = EC_CMD_PWM_GET_DUTY;
	msg->outsize = sizeof(*params);
	msg->insize = sizeof(*response);
	params = (void *)msg->data;
	params->pwm_type = EC_PWM_TYPE_KB_LIGHT;
	params->index = 0;
	ret = cros_ec_cmd_xfer_status(kbd->ec, msg);
	if (ret == sizeof(*response)) {
		response = (void *)msg->data;
		ret = DIV_ROUND_CLOSEST((unsigned int)response->duty * 100,
					EC_PWM_MAX_DUTY);
	} else if (ret >= 0) {
		ret = -EPROTO;
	}
	kfree(msg);
	return ret;
}

static enum led_brightness gbos_kbd_brightness_get(struct led_classdev *led)
{
	return gbos_kbd_get(container_of(led, struct gbos_kbd, led));
}

static int gbos_kbd_brightness_set(struct led_classdev *led,
				   enum led_brightness brightness)
{
	struct gbos_kbd *kbd = container_of(led, struct gbos_kbd, led);
	struct ec_params_pwm_set_duty *params;
	struct cros_ec_command *msg;
	int ret;

	if (brightness > 100)
		return -EINVAL;
	msg = kzalloc(sizeof(*msg) + sizeof(*params), GFP_KERNEL);
	if (!msg)
		return -ENOMEM;
	msg->command = EC_CMD_PWM_SET_DUTY;
	msg->outsize = sizeof(*params);
	params = (void *)msg->data;
	params->duty = DIV_ROUND_UP((unsigned int)brightness *
				   EC_PWM_MAX_DUTY, 100);
	params->pwm_type = EC_PWM_TYPE_KB_LIGHT;
	params->index = 0;
	ret = cros_ec_cmd_xfer_status(kbd->ec, msg);
	if (ret > 0)
		ret = -EPROTO;
	kfree(msg);
	return ret;
}

/* Held from setup until both drivers have been unregistered. */
static struct device *gbos_kbd_child;
static int gbos_kbd_setup_result = -ENODEV;

static bool gbos_kbd_is_target(struct device *dev)
{
	return dev->parent && !strcmp(dev_name(dev), "GOOG0002:00") &&
		!strcmp(dev_name(dev->parent), "GOOG0004:00");
}

static int gbos_kbd_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct device *parent = dev->parent;
	struct gbos_kbd *kbd;
	int brightness;

	/* The managed supplier link was installed before driver-core probe. */
	if (dev != gbos_kbd_child || !gbos_kbd_is_target(dev))
		return -ENODEV;
	if (!parent->driver || strcmp(parent->driver->name, "cros_ec_lpcs"))
		return -ENODEV;
	kbd = devm_kzalloc(dev, sizeof(*kbd), GFP_KERNEL);
	if (!kbd)
		return -ENOMEM;
	kbd->ec = dev_get_drvdata(parent);
	if (!kbd->ec)
		return -EPROBE_DEFER;

	/* Fail probe if the actual EC state cannot be read. */
	brightness = gbos_kbd_get(kbd);
	if (brightness < 0)
		return dev_err_probe(dev, brightness, "cannot read keyboard backlight\n");
	kbd->led.name = "chromeos::kbd_backlight";
	kbd->led.max_brightness = 100;
	kbd->led.brightness = brightness;
	kbd->led.brightness_get = gbos_kbd_brightness_get;
	kbd->led.brightness_set_blocking = gbos_kbd_brightness_set;
	kbd->led.flags = LED_CORE_SUSPENDRESUME | LED_REJECT_NAME_CONFLICT;
	platform_set_drvdata(pdev, kbd);
	return devm_led_classdev_register(dev, &kbd->led);
}

static void gbos_kbd_shutdown(struct platform_device *pdev)
{
	struct gbos_kbd *kbd = platform_get_drvdata(pdev);
	int ret;

	flush_work(&kbd->led.set_brightness_work);
	ret = gbos_kbd_brightness_set(&kbd->led, LED_OFF);
	if (ret)
		dev_warn(&pdev->dev, "cannot turn off keyboard backlight: %d\n", ret);
}

static const struct acpi_device_id gbos_kbd_ids[] = {
	{ "GOOG0002", 0 },
	{ }
};
MODULE_DEVICE_TABLE(acpi, gbos_kbd_ids);

static struct platform_driver gbos_kbd_driver = {
	.probe = gbos_kbd_probe,
	.shutdown = gbos_kbd_shutdown,
	.driver = {
		.name = "gbos_kbd_backlight",
		.acpi_match_table = gbos_kbd_ids,
	},
};
/*
 * Install the persistent managed link before the real driver's probe enters
 * the driver core.  Returning -ENODEV leaves the ACPI device unbound.  The
 * link is a structural dependency and is managed by the driver core for the
 * lifetime of the devices, including across this module's unload.
 */
static int gbos_kbd_setup_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;

	if (!gbos_kbd_is_target(dev) || gbos_kbd_child)
		return -ENODEV;
	if (!device_link_add(dev, dev->parent, DL_FLAG_AUTOPROBE_CONSUMER)) {
		gbos_kbd_setup_result = -ENOMEM;
		return -ENODEV;
	}
	gbos_kbd_child = get_device(dev);
	gbos_kbd_setup_result = 0;
	return -ENODEV;
}

static struct platform_driver gbos_kbd_setup_driver = {
	.probe = gbos_kbd_setup_probe,
	.driver = {
		.name = "gbos_kbd_link_setup",
		.acpi_match_table = gbos_kbd_ids,
		.probe_type = PROBE_FORCE_SYNCHRONOUS,
	},
};

static int __init gbos_kbd_init(void)
{
	int ret;

	ret = platform_driver_register(&gbos_kbd_setup_driver);
	if (ret)
		return ret;
	platform_driver_unregister(&gbos_kbd_setup_driver);
	if (gbos_kbd_setup_result)
		return gbos_kbd_setup_result;

	ret = platform_driver_register(&gbos_kbd_driver);
	if (ret) {
		put_device(gbos_kbd_child);
		gbos_kbd_child = NULL;
	}
	return ret;
}

static void __exit gbos_kbd_exit(void)
{
	platform_driver_unregister(&gbos_kbd_driver);
	put_device(gbos_kbd_child);
	gbos_kbd_child = NULL;
}
module_init(gbos_kbd_init);
module_exit(gbos_kbd_exit);

MODULE_DESCRIPTION("KANO ChromeOS EC keyboard backlight");
MODULE_LICENSE("GPL");
