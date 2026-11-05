#!/usr/bin/env python3
"""Check the driver's 101 software conversions, not actual EC hardware."""

MAX = 65535
for level in range(101):
    raw = (level * MAX + 99) // 100  # DIV_ROUND_UP
    kernel_get = (raw * 100 + MAX // 2) // MAX  # DIV_ROUND_CLOSEST
    ec_floor_percent = raw * 100 // MAX
    assert kernel_get == level, (level, raw, kernel_get)
    assert ec_floor_percent == level, (level, raw, ec_floor_percent)
print("101/101 levels round-trip; floor-percent EC model also passes")
