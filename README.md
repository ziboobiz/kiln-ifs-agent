# Kiln OS IFS Agent for OrcaSlicer

An OrcaSlicer printer plugin for the **Flashforge AD5X running Kiln OS**. It connects Orca to the printer over the local network (Moonraker). Orca then sees the four **IFS** slots as a multi-material unit, the same way it shows a Bambu AMS.

With it you can:

- see what is in each IFS slot (material, colour, which one is loaded) in Orca's **Device** tab;
- **change a slot's material, colour and temperature** from Orca;
- load / unload filament and turn auto-refill on or off from Orca;
- in the **Send print job** dialog, choose which IFS slot each filament in your model prints from;
- upload the sliced file and start the print in one click;
- watch temperatures, progress, layer, fan, speed and the chamber light, and pause / resume / stop the print.

> Status: early version (0.4.0), used on one printer so far. Bug reports are welcome.

---

## Requirements

| | |
|---|---|
| Printer | Flashforge AD5X with the IFS |
| Firmware | **Kiln OS** (provides Moonraker and the `IFS_*` commands this plugin uses) |
| Slicer | An OrcaSlicer build with **Python plugin** support (Plugins dialog) |
| Network | The computer running Orca must reach the printer, i.e. you can open Fluidd in a browser |

## Installation

1. Download [`kiln_ifs_agent.py`](kiln_ifs_agent.py).
2. In OrcaSlicer open the **Plugins** dialog and install the file.
3. When Orca asks, allow the plugin to use the **network**. It needs this to talk to the printer.
4. Open the plugin's **Config** tab and fill in:
   - **Printer IP or address**: the same address you use for Fluidd, e.g. `192.168.1.1`;
   - **Port**: leave empty unless Moonraker is not on port 80.
5. Click **Save**.
6. Open Orca's **Preferences** (Ctrl+P) and enable **Use printer agents instead of print hosts**. Without it, Orca ignores the plugin and keeps using the preset's print host. Restart Orca.
7. Select the **Flashforge AD5X** printer preset and open its settings (the edit icon next to the printer).
8. In **Basic information → Advanced → Printer agent**, choose **Kiln OS (IFS)**. Save the preset.
9. Open the **Device** tab. The printer appears as **Kiln OS AD5X** with the four IFS slots.

When the plugin loads, Orca shows a short notification with the plugin version and printer address.

## Using it

### Device tab

| On screen | What happens on the printer |
|---|---|
| Slots **A1–A4** | Read live from Kiln's `ifs_materials`; the loaded slot is highlighted |
| Click a slot → choose material / colour → confirm | `IFS_SET_MATERIAL SLOT=n TYPE=… COLOR=… TEMP=…` |
| Clear a slot's material | `IFS_SET_MATERIAL SLOT=n TYPE= COLOR=` |
| **Load** | `IFS_LOAD SLOT=n TEMP=…` (runs in the background) |
| **Unload** | `IFS_UNLOAD` |
| Auto refill / filament backup (IFS settings) | `IFS_AUTO_REFILL ENABLE=0/1` |
| Lamp | `SET_LED LED="chamber_led" …` |
| Speed | `M220 S50/100/124/166` |
| Pause / Resume / Stop | Moonraker `print/pause`, `print/resume`, `print/cancel` |
| Jog, home, bed and extruder moves | Sent as G-code lines |

The slots refresh every 10 seconds, and right after every command.

The temperature used for a slot is the middle of the min/max range picked in Orca. For example, 230–270 °C saves 250 °C.

### Sending a print

1. Slice as usual and click **Print**.
2. In **Send print job**, assign each filament of the model to an IFS slot (A1–A4).
3. Click **Send**. The plugin:
   1. extracts the plate's G-code from the sliced file;
   2. uploads it to the printer's `gcodes` folder;
   3. runs `IFS_MAP_TOOL TOOL=t SLOT=s` for each filament;
   4. starts the print.

**Send to printer storage** (upload only, no start) is also supported.

## Known limitations

- **"The selected printer (Unknown) has an unknown model…"** appears in orange in the Send dialog. It is only a warning and printing works. Orca only knows Bambu model ids, and reporting one of them would make Orca block the print instead.
- The Device tab is Orca's built-in Bambu screen. A plugin can only fill it with data, so some parts stay as they are:
  - the camera area (Bambu logo);
  - "Network plug-in v00.00.00.00";
  - the chamber temperature `_ / _`;
  - the external spool slot ("Ext").
- Only one IFS unit (4 slots) is supported.
- Progress bar stages during upload are best-effort.

## Troubleshooting

| Problem | Check |
|---|---|
| "cannot reach …" notification | The IP/port in the Config tab, and that Fluidd opens from this computer |
| Printer not in the Device tab / Kiln OS (IFS) not in the Printer agent list | **Preferences → Use printer agents instead of print hosts** is enabled, and Orca was restarted |
| Slots never appear | The plugin has network permission in Orca's Plugins dialog |
| Send fails at "Preparing G-code" | Orca blocked the plugin from reading the sliced file. Check the plugin's file permission |
| Anything else | Orca logs in `%APPDATA%\OrcaSlicer\log\` (`debug_*.log.0`, `python_*.log`) |

## Credits

- Plugin by **zib0**
- Made for the **Kiln OS** firmware for the Flashforge AD5X (not affiliated; Kiln OS is a separate project by its own authors)
- Built on [Moonraker](https://github.com/Arksine/moonraker) / [Klipper](https://github.com/Klipper3d/klipper) and [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer)

## License

Copyright (C) 2026 zib0

This program is free software: you can redistribute it and/or modify it under the terms of the **GNU General Public License v3.0 or later**. See [LICENSE](LICENSE).
