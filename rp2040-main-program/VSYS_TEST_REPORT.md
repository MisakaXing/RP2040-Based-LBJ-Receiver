# W voltage source: VSYS (2026-10-06)

- W board identity confirmed before writes: Waveshare RP2350B PLUS W with RP2350.
- Runtime and POST now use the same GP46 ADC object and onboard /3 divider.
- Conversion: raw / 65535 * 3.3 * 3; removed external-battery 1.07 gain.
- GP41 is no longer sampled. GP14 remains high; sampling no longer toggles its external divider gate or waits 5 ms.
- USB CHRG behavior, 3.45 V empty threshold and 1-second USB detection remain unchanged. Percent and protection now refer to VSYS, not the battery terminals.
- Host regression: 278 tests passed. RF/PIO/DMA source files untouched.
- Device main.py SHA-256: 9cbf63396986662cb2696b3119a156115e304f3e3f3a4b4d108d8786c44f308c.
- Device boot_post.py SHA-256: d3ad2807eaded3d14a5f3593885fe62d7788626db8f97342f4e627794481ccef.
- Both hashes matched local source after flashing.
- USB-connected baseline: raw median 30535, computed VSYS 4.612749 V.
- Installed runtime: 5.14-W, DASHBOARD, cached voltage 4.6V, USB present.
- Web state: battery_voltage 4.6, usb_power true, battery_percent null (CHRG).
- Runtime sampling: 4.6V, GP46 raw 30455. PIO and DMA active; words 128, DMA overruns 0, SPI errors 0. No decoded train confirmed during this brief observation.
- Original device main.py and boot_post.py backed up outside the repository; history/configuration were not cleared. Hardware reset used to restore the complete main loop after diagnostics.

Limits: battery-only accuracy and screen/button appearance still need physical confirmation. VSYS under USB is not battery voltage. Conversion assumes ADC reference is 3.3 V; reference droop cannot be fixed merely by switching ADC channels. Power-path voltage drop may cause earlier protection when running on battery.
