# Autotuner – guided calibration for Klipper

Autotuner adds an **Auto Tune** entry to the Klipper LCD menu and a pop-up
dialog in Mainsail / Fluidd. It holds a list of calibrations. Each one can be
switched on or off, and pressing **Start** runs the enabled ones in order:

| # | Item (`ITEM=`)   | What it does | Saved to |
|---|------------------|--------------|----------|
| 1 | `limits`         | You move every axis to its far end. Each axis then travels to its endstop switch, and the distance is measured (the switch is 0). | `[stepper_x/y/z]` `position_max` (and `position_endstop`/`position_min`) |
| 2 | `input_shaper`   | Runs `SHAPER_CALIBRATE` for X, then Y. With two accelerometers both run straight away. With one, it waits after X until you have moved the accelerometer to the Y axis and pressed Continue. The best shapers are applied. | `[input_shaper]` |
| 3 | `probe_offsets`  | The probe marks a post-it note, you move the nozzle over the mark, and the X/Y probe offsets are calculated. The Z offset is then found with a paper test, the same way as `PROBE_CALIBRATE`. | `[probe]` / `[bltouch]` … `x_offset`, `y_offset`, `z_offset` |

When everything is done, `SAVE_CONFIG` runs (this can be turned off) so all
the new values are written to `printer.cfg` and Klipper restarts.

## Installation

```bash
cd ~
git clone https://github.com/akkafali/Autotuner.git
cd Autotuner
./install.sh            # or: ./install.sh <klipper dir> <config dir>
```

`install.sh` symlinks `autotuner.py` into `~/klipper/klippy/extras/`. It also
copies `autotuner.cfg` and `autotuner_menu.cfg` into
`~/printer_data/config/`. Add this to `printer.cfg`:

```ini
[include autotuner.cfg]
[include autotuner_menu.cfg]   # only if the printer has an LCD ([display])
```

Then restart Klipper (`sudo systemctl restart klipper`).

Optionally, add this to `moonraker.conf` to get updates from Mainsail /
Fluidd:

```ini
[update_manager autotuner]
type: git_repo
path: ~/Autotuner
origin: https://github.com/akkafali/Autotuner.git
managed_services: klipper
```

Requirements: Klipper running on Python 3. Input shaping needs a working
`[resonance_tester]` with an accelerometer. Probe offsets need a probe
(`[probe]`, `[bltouch]`, …). Limit calibration supports cartesian, CoreXY,
CoreXZ and hybrid kinematics.

## Usage

### LCD
`Main → Auto Tune` lists:

* **Limits / Shaper / Probe ofs: ON|OFF**: click, turn, click to toggle.
* **Start**: runs every enabled item.
* While running: the current step, **Continue**, **Abort tune**, and
  **Jog X/Y/Z** (pick a distance and click to move). There are also
  **Motors off** (limit step) and **Test Z / Accept / Abort paper test**
  (Z offset step).

### Mainsail / Fluidd
Run `TUNE` in the console (or make a macro button for it). A dialog shows the
items with ON/OFF buttons and a **Start** button. Every step that needs you
opens a dialog with **Continue**, **Abort** and jog buttons. The paper test
uses the normal manual-probe dialog.

### Commands

| Command | Description |
|---------|-------------|
| `TUNE` | List the items and open the web dialog (`TUNE CLOSE=1` closes it) |
| `TUNE_ENABLE ITEM=<limits\|input_shaper\|probe_offsets> [ENABLE=0\|1]` | Enable/disable an item (toggles when `ENABLE` is omitted) |
| `TUNE_START` | Run all enabled items |
| `TUNE_CONTINUE` | Confirm the current step |
| `TUNE_ABORT` | Stop. Values from finished items stay pending until `SAVE_CONFIG` |
| `TUNE_JOG [X=] [Y=] [Z=] [SPEED=]` | Move the toolhead relative to where it is (works before homing during the limit step) |
| `TESTZ` / `ACCEPT` / `ABORT` | Klipper's normal manual-probe commands, used for the paper test |

`printer.autotuner` holds `active`, `waiting`, `manual_probe`, `item`,
`message`, and `limits` / `input_shaper` / `probe_offsets` (enabled flags),
for use in macros and menus.

## How each item works

### 1. Limit calibration
1. All axes are temporarily marked as "homed" so you can jog them with the
   buttons or `TUNE_JOG`. You can also press **Motors off** and move them by
   hand.
2. Move each axis to the end **away from** its switch (normally the maximum),
   then press **Continue**.
3. X and Y move towards their switches. When a switch triggers, the axis
   backs off by `homing_retract_dist` and touches it again at
   `second_homing_speed`, just like normal homing. The distance travelled is
   the axis length.
4. Z is measured last. If Z homes with the probe
   (`probe:z_virtual_endstop`), the toolhead first moves over the bed (your
   `[safe_z_home]` point or the bed centre).
5. New values:
   * X/Y with the switch at the minimum: `position_endstop: 0`,
     `position_min: 0`, `position_max: <length>`. Set
     `zero_at_switch: False` to keep your `position_endstop` and only set
     `position_max = position_endstop + length`.
   * Z with the switch at the minimum: `position_max = position_endstop +
     length`. Your `position_endstop` (nozzle height) is not changed. With a
     probe as the Z endstop, the probe's `z_offset` is used instead.
   * Any axis with the switch at the maximum: `position_endstop =
     position_max = position_min + length`.

After the measurement all axes are properly homed. If you abort during
this step, the motors are turned off so the fake "homed" state can't be used
for printing.

### 2. Input shaper
* **Two accelerometers** are detected when `[resonance_tester]` has
  different `accel_chip_x` and `accel_chip_y` (or set `accelerometers: 2`
  and `accel_chip_x/y` in `[autotuner]`). X and Y run one after the other.
* **One accelerometer**: X runs first. The dialog then asks you to mount the
  accelerometer for the Y axis (on the bed for bed slingers) and waits for
  **Continue** before running Y.
* `SHAPER_CALIBRATE` picks the best shaper for each axis, applies it right
  away and stores it for `SAVE_CONFIG`.

### 3. Bed probe offsets
1. The toolhead moves to the middle of the bed (`probe_point`). Put a post-it
   note under the probe and press **Continue**.
2. The probe goes down onto the note, then moves up again. A touch probe
   (BLTouch, CR-Touch, Klicky …) leaves a small mark.
3. Move the nozzle exactly over the mark with the jog buttons and press
   **Continue**. `x_offset = nozzle X − probe X` and
   `y_offset = nozzle Y − probe Y`.
4. Remove the post-it and press **Continue**. The bed is probed, the nozzle
   moves over the probed spot (using the new X/Y offsets), and the paper test
   starts. Use `TESTZ` until the nozzle just grips the paper, then `ACCEPT`.
   `z_offset = probe trigger height − nozzle height`.

## Configuration

Every option is optional. See [`autotuner.cfg`](autotuner.cfg) for the full,
commented list:

```ini
[autotuner]
#limits: True              # default ON/OFF state of each item
#input_shaper: True
#probe_offsets: True
#save_config: True         # run SAVE_CONFIG at the end
#web_prompts: True         # Mainsail/Fluidd dialogs
#limit_axes: xyz
#zero_at_switch: True
#limit_max_travel:         # default 1.5 x configured axis length
#accelerometers: auto      # auto, 1 or 2
#accel_chip_x:
#accel_chip_y:
#max_smoothing:
#probe_section:            # auto: probe, bltouch, smart_effector, ...
#probe_point:              # default: bed centre
#probe_lift: 5
#safe_z: 10
#jog_speed: 50
#travel_speed: 100
```

## Notes and limitations
* `SAVE_CONFIG` can only overwrite options that are in `printer.cfg` itself,
  not in an `[include]`d file. This is a Klipper rule and stock
  `PROBE_CALIBRATE` / `SHAPER_CALIBRATE` have it too. If your
  `[stepper_*]` or probe sections are in an included file, the save fails
  and Klipper says which option conflicts. Move those options into
  `printer.cfg`, or copy the printed values by hand.
* Keep a hand near the power switch the first time you run the limit
  calibration. The axes move with endstop detection the whole way, like
  normal homing, but the search distance is longer than your configured
  axis.
* Klipper's idle timeout (10 minutes by default) turns the motors off if you
  leave a step waiting too long. The input shaper and probe steps home again
  if needed.

## Development
The tests run the plugin against a simulated printer (no Klipper checkout
needed):

```bash
python3 -m unittest discover -s tests -v
```
