# Guided printer calibration ("Auto Tune") for Klipper
#
# Adds a list of calibration items that can each be enabled or disabled
# and then run in sequence:
#
#   limits        - measure the travel of every axis by homing from the far
#                   end of the axis to the endstop switch
#   input_shaper  - run SHAPER_CALIBRATE for X and Y (one or two
#                   accelerometers) and apply the recommended shapers
#   probe_offsets - measure the probe x/y offset with a post-it note and the
#                   z offset with a paper test (like PROBE_CALIBRATE)
#
# All results are stored with the configfile module, so a SAVE_CONFIG (run
# automatically at the end unless disabled) writes them to printer.cfg.
#
# Copyright (C) 2026  Autotuner contributors
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import homing, manual_probe

ITEMS = ['limits', 'input_shaper', 'probe_offsets']
ITEM_TITLES = {
    'limits': "Limit calibration",
    'input_shaper': "Input shaper",
    'probe_offsets': "Bed probe offsets",
}
ITEM_HELP = {
    'limits': "measure the length of each axis",
    'input_shaper': "find and apply the best input shapers",
    'probe_offsets': "measure the probe X/Y/Z offsets",
}
ITEM_ALIASES = {
    'limits': 'limits', 'limit': 'limits', 'limit_calibrate': 'limits',
    'input_shaper': 'input_shaper', 'shaper': 'input_shaper',
    'inputshaper': 'input_shaper',
    'probe_offsets': 'probe_offsets', 'probe': 'probe_offsets',
    'bed_level': 'probe_offsets', 'bedlevel': 'probe_offsets',
}
PROBE_SECTIONS = ['probe', 'bltouch', 'smart_effector', 'load_cell_probe']
JOG_STEPS = [-50., -10., 10., 50.]
JOG_STEPS_FINE = [-1., -0.1, 0.1, 1.]

# Requests yielded by the calibration sequences to the runner
class WaitConfirm:
    def __init__(self, title, lines, short, jog=False, motors_off=False):
        self.title = title
        self.lines = lines
        self.short = short
        self.jog = jog
        self.motors_off = motors_off

class WaitManualProbe:
    def __init__(self, title, lines, short):
        self.title = title
        self.lines = lines
        self.short = short

class AutoTuner:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.gcode = self.printer.lookup_object('gcode')
        self.printer.load_object(config, 'manual_probe')
        # Items enabled by default (can be toggled at runtime)
        self.enabled = {
            name: config.getboolean(name, True) for name in ITEMS}
        # General options
        self.save_config = config.getboolean('save_config', True)
        self.web_prompts = config.getboolean('web_prompts', True)
        self.jog_speed = config.getfloat('jog_speed', 50., above=0.)
        self.travel_speed = config.getfloat('travel_speed', 100., above=0.)
        self.safe_z = config.getfloat('safe_z', 10., minval=0.)
        # Limit calibration options
        limit_axes = config.get('limit_axes', 'xyz').lower().strip()
        for axis in limit_axes:
            if axis not in 'xyz':
                raise config.error("Invalid limit_axes '%s' in [%s]"
                                   % (limit_axes, self.name))
        self.limit_axes = sorted(set('xyz'.index(a) for a in limit_axes))
        if not self.limit_axes:
            raise config.error("limit_axes in [%s] must name at least one"
                               " axis" % (self.name,))
        self.zero_at_switch = config.getboolean('zero_at_switch', True)
        self.limit_max_travel = config.getfloat('limit_max_travel', None,
                                                above=0.)
        self.limit_min_travel = config.getfloat('limit_min_travel', 10.,
                                                minval=0.)
        # Input shaper options
        self.accelerometers = config.getchoice(
            'accelerometers', {'auto': 0, '1': 1, '2': 2}, 'auto')
        self.accel_chip_x = config.get('accel_chip_x', None)
        self.accel_chip_y = config.get('accel_chip_y', None)
        self.max_smoothing = config.getfloat('max_smoothing', None,
                                             minval=0.05)
        # Probe offset options
        self.probe_section = config.get('probe_section', None)
        self.probe_point = config.getfloatlist('probe_point', None, count=2)
        self.probe_lift = config.getfloat('probe_lift', 5., above=0.)
        self.mark_gcode = config.get('mark_gcode', '')
        self.mark_done_gcode = config.get('mark_done_gcode', '')
        # Runtime state
        self.sequence = None
        self.gcmd = None
        self.item = None
        self.waiting = None
        self.message = ""
        self.fake_homed = False
        self.free_jog = False
        self.results = []
        self.step_prompt = None
        # Register commands. The TUNE button in Mainsail / Fluidd is a
        # [gcode_macro TUNE] (see autotuner.cfg) that runs TUNE_MENU.
        cmds = [('TUNE_MENU', self.cmd_TUNE_MENU,
                 "Show the Auto Tune menu"),
                ('TUNE_ENABLE', self.cmd_TUNE_ENABLE,
                 "Enable or disable an Auto Tune item"),
                ('TUNE_START', self.cmd_TUNE_START,
                 "Run all enabled Auto Tune items"),
                ('TUNE_CONTINUE', self.cmd_TUNE_CONTINUE,
                 "Confirm the current Auto Tune step"),
                ('TUNE_ABORT', self.cmd_TUNE_ABORT, "Abort Auto Tune"),
                ('TUNE_JOG', self.cmd_TUNE_JOG,
                 "Move the toolhead relative to its current position")]
        for cmd, func, desc in cmds:
            self.gcode.register_command(cmd, func, desc=desc)

    # Status for the LCD menu and macros (printer.autotuner)
    def get_status(self, eventtime=None):
        status = {
            'active': self.sequence is not None,
            'waiting': self.waiting == 'confirm',
            'manual_probe': self.waiting == 'manual_probe',
            'item': self.item or "",
            'message': self.message,
        }
        for name in ITEMS:
            status[name] = self.enabled[name]
        return status

    # Helpers
    def _toolhead(self):
        return self.printer.lookup_object('toolhead')
    def _settings(self):
        configfile = self.printer.lookup_object('configfile')
        return configfile.get_status(None).get('settings', {})
    def _respond(self, msg):
        self.gcode.respond_info(msg)
    def _run(self, script):
        self.gcode.run_script_from_command(script)
    def _set_position(self, pos, axes):
        # Older Klipper versions take a tuple of axis indexes, newer
        # versions take a string of axis names.
        toolhead = self._toolhead()
        try:
            toolhead.set_position(pos, homing_axes="".join(
                "xyz"[i] for i in axes))
        except TypeError:
            toolhead.set_position(pos, homing_axes=tuple(axes))
    def _homed_axes(self):
        toolhead = self._toolhead()
        eventtime = self.printer.get_reactor().monotonic()
        return toolhead.get_status(eventtime).get('homed_axes', "")
    def _ensure_homed(self):
        if self.fake_homed:
            raise self.printer.command_error(
                "Internal error: axes still in limit calibration mode")
        if any(a not in self._homed_axes() for a in 'xyz'):
            self._respond("Auto Tune: homing all axes")
            self._run("G28")
    def _axis_range(self, axis):
        toolhead = self._toolhead()
        eventtime = self.printer.get_reactor().monotonic()
        status = toolhead.get_status(eventtime)
        return status['axis_minimum'][axis], status['axis_maximum'][axis]
    def _get_rails(self):
        kin = self._toolhead().get_kinematics()
        rails = getattr(kin, 'rails', None)
        if (rails is None or len(rails) < 3
            or not all(hasattr(r, 'get_homing_info') for r in rails[:3])):
            raise self.printer.command_error(
                "Limit calibration only supports cartesian style"
                " kinematics (cartesian, corexy, corexz, hybrid)")
        return rails[:3]
    def _set_display(self, msg):
        if self.printer.lookup_object('display_status', None) is not None:
            self._run("M117 %s" % (msg,))
    def _web_prompt(self, title, lines, groups=(), footer=()):
        if not self.web_prompts:
            return
        out = ["// action:prompt_end",
               "// action:prompt_begin %s" % (title,)]
        for line in lines:
            out.append("// action:prompt_text %s" % (line,))
        for group in groups:
            out.append("// action:prompt_button_group_start")
            for label, cmd, style in group:
                out.append("// action:prompt_button %s|%s|%s"
                           % (label, cmd, style))
            out.append("// action:prompt_button_group_end")
        for label, cmd, style in footer:
            out.append("// action:prompt_footer_button %s|%s|%s"
                       % (label, cmd, style))
        out.append("// action:prompt_show")
        for line in out:
            self.gcode.respond_raw(line)
    def _close_prompt(self):
        if self.web_prompts:
            self.gcode.respond_raw("// action:prompt_end")
    def _jog_groups(self, fine=False):
        steps = JOG_STEPS_FINE if fine else JOG_STEPS
        groups = []
        for axis in 'XYZ':
            groups.append([("%s%+g" % (axis, d),
                            "TUNE_JOG %s=%g" % (axis, d), "secondary")
                           for d in steps])
        return groups

    # Menu (web prompt) listing all items
    def _show_menu(self):
        lines = ["Click an item to switch it ON or OFF, then press Start."]
        lines += ["%s: %s" % (ITEM_TITLES[n], ITEM_HELP[n]) for n in ITEMS]
        if self.sequence is not None:
            lines = ["Auto Tune is running: %s" % (self.message,)]
        buttons = [("%s: %s" % (ITEM_TITLES[n], "ON" if self.enabled[n]
                                else "OFF"),
                    "TUNE_ENABLE ITEM=%s MENU=1" % (n,),
                    "primary" if self.enabled[n] else "secondary")
                   for n in ITEMS]
        footer = [("Close", "TUNE_MENU CLOSE=1", "secondary")]
        if self.sequence is None:
            footer.append(("Start", "TUNE_START", "primary"))
        else:
            footer.append(("Abort", "TUNE_ABORT", "error"))
        self._web_prompt("Auto Tune", lines, [[b] for b in buttons], footer)

    # Sequence runner
    def _step(self, gcmd, value=None):
        self.gcmd = gcmd
        self.waiting = None
        try:
            req = self.sequence.send(value)
        except StopIteration:
            self._finish()
            return
        except self.printer.command_error as e:
            self._cleanup()
            raise gcmd.error("Auto Tune aborted: %s" % (str(e),))
        except Exception as e:
            logging.exception("Auto Tune internal error")
            self._cleanup()
            raise gcmd.error("Auto Tune internal error: %s" % (str(e),))
        self.message = req.short
        self._set_display(req.short)
        if isinstance(req, WaitManualProbe):
            self.waiting = 'manual_probe'
            self._close_prompt()
            self._respond("\n".join([req.title] + req.lines))
            try:
                manual_probe.ManualProbeHelper(self.printer, gcmd,
                                               self._manual_probe_done)
            except self.printer.command_error as e:
                self._cleanup()
                raise gcmd.error("Auto Tune aborted: %s" % (str(e),))
            return
        self.waiting = 'confirm'
        self.free_jog = req.motors_off
        self._respond("\n".join(
            [req.title] + req.lines
            + ["Run TUNE_CONTINUE to continue or TUNE_ABORT to cancel."]))
        footer = []
        if req.motors_off:
            footer.append(("Motors off", "M84", "warning"))
        footer += [("Abort", "TUNE_ABORT", "error"),
                   ("Continue", "TUNE_CONTINUE", "primary")]
        groups = self._jog_groups(fine=not req.motors_off) if req.jog else []
        self.step_prompt = (req.title, req.lines, groups, footer)
        self._web_prompt(*self.step_prompt)
    def _manual_probe_done(self, result):
        if self.sequence is None or self.waiting != 'manual_probe':
            return
        zpos = None
        if result is not None:
            zpos = getattr(result, 'bed_z', None)
            if zpos is None:
                zpos = result[2]
        self._step(self.gcmd, zpos)
    def _cleanup(self):
        if self.sequence is not None:
            seq = self.sequence
            self.sequence = None
            seq.close()
        was_manual_probe = self.waiting == 'manual_probe'
        self.waiting = None
        self.item = None
        self.message = ""
        self.free_jog = False
        self._close_prompt()
        mprobe = self.printer.lookup_object('manual_probe', None)
        if was_manual_probe and getattr(mprobe, 'status', {}).get(
                'is_active'):
            self._run("ABORT")
        if self.fake_homed:
            # Axis positions are not trustworthy - force a re-home
            self.fake_homed = False
            self._run("M84")
            self._respond("Auto Tune: motors disabled, home before printing")
    def _finish(self):
        self.sequence = None
        self.waiting = None
        self.item = None
        self.message = "Done"
        self._close_prompt()
        configfile = self.printer.lookup_object('configfile')
        pending = configfile.get_status(None).get(
            'save_config_pending_items', {})
        lines = ["Auto Tune complete."] + self.results
        if not pending:
            self._respond("\n".join(lines + ["Nothing to save."]))
            self._set_display("Auto Tune done")
            return
        if self.save_config:
            self._respond("\n".join(
                lines + ["Saving configuration and restarting..."]))
            self._run("SAVE_CONFIG")
            return
        self._respond("\n".join(
            lines + ["Run SAVE_CONFIG to store the new values."]))
        self._set_display("Auto Tune done")
    def _run_sequence(self, items):
        for name in items:
            self.item = name
            self._respond("Auto Tune: starting %s" % (ITEM_TITLES[name],))
            if name == 'limits':
                yield from self._limits()
            elif name == 'input_shaper':
                yield from self._input_shaper()
            elif name == 'probe_offsets':
                yield from self._probe_offsets()

    # Limit calibration
    def _measure_axis(self, axis):
        toolhead = self._toolhead()
        rail = self._get_rails()[axis]
        hi = rail.get_homing_info()
        pos_min, pos_max = rail.get_range()
        direction = 1. if hi.positive_dir else -1.
        max_travel = self.limit_max_travel
        if max_travel is None:
            max_travel = 1.5 * (pos_max - pos_min)
        endstops = rail.get_endstops()
        name = "xyz"[axis].upper()
        self._respond("Auto Tune: moving %s towards its endstop" % (name,))
        # Pretend the axis is max_travel away from the switch and home it.
        # The distance moved until the switch triggers is the axis length.
        pos = toolhead.get_position()
        origin = pos[axis] = hi.position_endstop - direction * max_travel
        self._set_position(pos, [axis])
        trig = self._homing_move(endstops, axis, hi.position_endstop,
                                 hi.speed)
        if hi.retract_dist:
            # Shift the coordinates so the switch is at position_endstop,
            # then back off and approach the switch again slowly
            shift = hi.position_endstop - trig
            origin += shift
            pos = toolhead.get_position()
            pos[axis] += shift
            self._set_position(pos, [axis])
            pos[axis] -= direction * hi.retract_dist
            toolhead.manual_move(pos, hi.retract_speed)
            pos = toolhead.get_position()
            new_pos = hi.position_endstop - direction * 2. * hi.retract_dist
            origin += new_pos - pos[axis]
            pos[axis] = new_pos
            self._set_position(pos, [axis])
            trig = self._homing_move(endstops, axis, hi.position_endstop,
                                     hi.second_homing_speed)
        travel = abs(trig - origin)
        # Switch to the regular (configured) coordinate system
        pos = toolhead.get_position()
        pos[axis] += hi.position_endstop - trig
        self._set_position(pos, [axis])
        if travel < self.limit_min_travel:
            raise self.printer.command_error(
                "%s only travelled %.3fmm before hitting the switch. Move the"
                " axis to the far end before continuing." % (name, travel))
        self._respond("Auto Tune: %s travel is %.3fmm" % (name, travel))
        return travel
    def _homing_move(self, endstops, axis, target, speed):
        toolhead = self._toolhead()
        movepos = toolhead.get_position()
        movepos[axis] = target
        hmove = homing.HomingMove(self.printer, endstops, toolhead)
        try:
            epos = hmove.homing_move(movepos, speed, probe_pos=True)
        except self.printer.command_error as e:
            if self.printer.is_shutdown():
                raise self.printer.command_error(
                    "Limit calibration failed due to printer shutdown")
            raise self.printer.command_error(
                "%s (if the axis is longer than expected, set"
                " limit_max_travel in [%s])" % (str(e), self.name))
        if not epos:
            epos = toolhead.get_position()
        return epos[axis]
    def _z_uses_probe(self, rail):
        sconfig = self._settings().get(rail.get_name(), {})
        return str(sconfig.get('endstop_pin', '')).strip().startswith(
            'probe:')
    def _safe_xy(self):
        sconfig = self._settings().get('safe_z_home', {})
        xy = sconfig.get('home_xy_position')
        if xy:
            if isinstance(xy, str):
                xy = xy.split(',')
            return [float(v) for v in xy[:2]]
        if self.probe_point is not None:
            return list(self.probe_point)
        return [sum(self._axis_range(i)) / 2. for i in (0, 1)]
    def _enter_free_jog(self):
        # Mark all axes as homed at the middle of their range so the toolhead
        # can be jogged before the real positions are known
        toolhead = self._toolhead()
        pos = toolhead.get_position()
        for i, rail in enumerate(self._get_rails()):
            pos[i] = sum(rail.get_range()) / 2.
        self._set_position(pos, [0, 1, 2])
        self.fake_homed = True
    def _limits(self):
        rails = self._get_rails()
        self._enter_free_jog()
        names = " ".join("XYZ"[i] for i in self.limit_axes)
        ends = []
        for i in self.limit_axes:
            hi = rails[i].get_homing_info()
            ends.append("%s to its %s" % ("XYZ"[i], "minimum"
                                          if hi.positive_dir else "maximum"))
        yield WaitConfirm(
            "Limit calibration",
            ["Move %s (the end away from the endstop switch)."
             % (", ".join(ends),),
             "Use the jog buttons, or turn the motors off and move the"
             " axes by hand.",
             "When you continue, each axis moves until it hits its switch"
             " and the distance is measured."],
            "Move %s to max" % (names,), jog=True, motors_off=True)
        self.free_jog = False
        results = {}
        for i in (0, 1):
            if i in self.limit_axes:
                results[i] = self._measure_axis(i)
        missing = "".join("XY"[i] for i in (0, 1) if i not in results)
        if missing:
            self._run("G28 %s" % (" ".join(missing),))
        if 2 in self.limit_axes:
            if self._z_uses_probe(rails[2]):
                x, y = self._safe_xy()
                self._toolhead().manual_move([x, y], self.travel_speed)
            results[2] = self._measure_axis(2)
        else:
            self._run("G28 Z")
        self.fake_homed = False
        # Store the new axis limits
        configfile = self.printer.lookup_object('configfile')
        for i, travel in sorted(results.items()):
            rail = rails[i]
            section = rail.get_name()
            hi = rail.get_homing_info()
            pos_min, pos_max = rail.get_range()
            values = []
            if hi.positive_dir:
                values.append(('position_endstop', pos_min + travel))
                values.append(('position_max', pos_min + travel))
            elif i < 2 and self.zero_at_switch:
                values.append(('position_endstop', 0.))
                values.append(('position_min', 0.))
                values.append(('position_max', travel))
            else:
                values.append(('position_max', hi.position_endstop + travel))
            for option, value in values:
                configfile.set(section, option, "%.3f" % (value,))
            self.results.append("[%s] %s" % (section, ", ".join(
                "%s: %.3f" % v for v in values)))
            self._respond("Auto Tune: [%s] %s" % (section, ", ".join(
                "%s=%.3f" % v for v in values)))
        self._set_display("Limits measured")

    # Input shaper calibration
    def _accel_setup(self):
        if self.printer.lookup_object('resonance_tester', None) is None:
            raise self.printer.command_error(
                "Input shaper calibration requires a [resonance_tester]"
                " section")
        rt = self._settings().get('resonance_tester', {})
        chip_x = self.accel_chip_x or rt.get('accel_chip_x')
        chip_y = self.accel_chip_y or rt.get('accel_chip_y')
        distinct = bool(chip_x and chip_y
                        and chip_x.strip() != chip_y.strip())
        if self.accelerometers:
            dual = self.accelerometers == 2
        else:
            dual = distinct
        params = {'x': "", 'y': ""}
        if self.accel_chip_x:
            params['x'] = " CHIPS=%s" % (self.accel_chip_x.strip(),)
        if self.accel_chip_y:
            params['y'] = " CHIPS=%s" % (self.accel_chip_y.strip(),)
        if self.max_smoothing is not None:
            for axis in params:
                params[axis] += " MAX_SMOOTHING=%.3f" % (self.max_smoothing,)
        return dual, params
    def _input_shaper(self):
        dual, params = self._accel_setup()
        self._ensure_homed()
        self._respond("Auto Tune: using %s"
                      % ("two accelerometers" if dual
                         else "one accelerometer"))
        self._set_display("Shaper X...")
        self._run("SHAPER_CALIBRATE AXIS=X" + params['x'])
        if not dual:
            yield WaitConfirm(
                "Input shaper: Y axis",
                ["X axis done. Mount the accelerometer for the Y axis now"
                 " (on the bed for bed slinger printers).",
                 "Press Continue when it is mounted."],
                "Mount accel on Y")
            self._ensure_homed()
        self._set_display("Shaper Y...")
        self._run("SHAPER_CALIBRATE AXIS=Y" + params['y'])
        configfile = self.printer.lookup_object('configfile')
        pending = configfile.get_status(None).get(
            'save_config_pending_items', {}).get('input_shaper', {})
        if pending:
            self.results.append("[input_shaper] %s" % (", ".join(
                "%s: %s" % kv for kv in sorted(pending.items())),))
        self._set_display("Shaper done")

    # Probe offset calibration
    def _find_probe_section(self):
        if self.printer.lookup_object('probe', None) is None:
            raise self.printer.command_error(
                "Probe offset calibration requires a probe")
        if self.probe_section is not None:
            return self.probe_section
        settings = self._settings()
        for section in PROBE_SECTIONS:
            if section in settings:
                return section
        for section in settings:
            if section.startswith('probe_eddy_current'):
                return section
        raise self.printer.command_error(
            "Unable to find the probe config section, set probe_section in"
            " [%s]" % (self.name,))
    def _probe(self, samples=None):
        toolhead = self._toolhead()
        cmd = "PROBE"
        if samples is not None:
            cmd += " SAMPLES=%d" % (samples,)
        self._run(cmd)
        pos = toolhead.get_position()
        probe = self.printer.lookup_object('probe')
        eventtime = self.printer.get_reactor().monotonic()
        zpos = probe.get_status(eventtime).get('last_z_result')
        if zpos is None:
            zpos = pos[2]
        return pos[0], pos[1], zpos
    def _move_to_probe_point(self):
        toolhead = self._toolhead()
        pos = toolhead.get_position()
        if pos[2] < self.safe_z:
            toolhead.manual_move([None, None, self.safe_z],
                                 self._z_speed())
        if self.probe_point is not None:
            x, y = self.probe_point
        else:
            x, y = [sum(self._axis_range(i)) / 2. for i in (0, 1)]
        toolhead.manual_move([x, y], self.travel_speed)
    def _z_speed(self):
        probe = self.printer.lookup_object('probe')
        try:
            return probe.get_probe_params()['lift_speed']
        except Exception:
            return 5.
    def _lift(self, dist):
        toolhead = self._toolhead()
        pos = toolhead.get_position()
        toolhead.manual_move([None, None, pos[2] + dist], self._z_speed())
    def _probe_offsets(self):
        section = self._find_probe_section()
        self._ensure_homed()
        self._move_to_probe_point()
        # Step 1: mark the probe location on a post-it note
        yield WaitConfirm(
            "Probe offsets: post-it",
            ["Place a post-it note on the bed directly below the probe.",
             "Press Continue and the probe will be lowered onto it."],
            "Place post-it note")
        probe_x, probe_y, probe_z = self._probe(samples=1)
        if self.mark_gcode.strip():
            self._run(self.mark_gcode)
        # The probe stays down until the user has marked its position
        yield WaitConfirm(
            "Probe offsets: mark the spot",
            ["The probe is touching the post-it note.",
             "Mark the spot under the probe tip (for example with a pen),"
             " then press Continue to raise the probe."],
            "Mark probe spot")
        if self.mark_done_gcode.strip():
            self._run(self.mark_done_gcode)
        self._lift(self.probe_lift)
        # Step 2: user moves the nozzle over the mark
        yield WaitConfirm(
            "Probe offsets: align nozzle",
            ["Move the nozzle so it is exactly above the mark on the"
             " post-it note.",
             "Lower Z carefully to get a good view, then press Continue."],
            "Nozzle to mark", jog=True)
        pos = self._toolhead().get_position()
        x_offset = pos[0] - probe_x
        y_offset = pos[1] - probe_y
        self._respond("Auto Tune: probe x_offset=%.3f y_offset=%.3f"
                      % (x_offset, y_offset))
        if pos[2] < probe_z + self.probe_lift:
            self._lift(probe_z + self.probe_lift - pos[2])
        # Step 3: z offset (same procedure as PROBE_CALIBRATE)
        yield WaitConfirm(
            "Probe offsets: Z offset",
            ["Remove the post-it note from the bed.",
             "Press Continue to probe the bed and start the paper test for"
             " the Z offset."],
            "Remove post-it")
        self._ensure_homed()
        self._move_to_probe_point()
        probe_x, probe_y, probe_z = self._probe()
        self._lift(self.probe_lift)
        nozzle_x = probe_x + x_offset
        nozzle_y = probe_y + y_offset
        for i, v in enumerate((nozzle_x, nozzle_y)):
            lo, hi = self._axis_range(i)
            if v < lo or v > hi:
                raise self.printer.command_error(
                    "The nozzle can't reach the probed point (%.3f, %.3f),"
                    " set probe_point in [%s]"
                    % (nozzle_x, nozzle_y, self.name))
        self._toolhead().manual_move([nozzle_x, nozzle_y], self.travel_speed)
        nozzle_z = yield WaitManualProbe(
            "Probe offsets: paper test",
            ["Place a sheet of paper under the nozzle and use TESTZ (or the"
             " manual probe dialog) to lower the nozzle until it lightly"
             " grips the paper.",
             "Finish with ACCEPT (or ABORT to cancel)."],
            "Paper test: TESTZ")
        if nozzle_z is None:
            raise self.printer.command_error("Z offset calibration aborted")
        z_offset = probe_z - nozzle_z
        self._lift(self.probe_lift)
        if z_offset <= 0.:
            self._respond("Auto Tune: WARNING: measured z_offset %.3f is not"
                          " positive" % (z_offset,))
        configfile = self.printer.lookup_object('configfile')
        values = [('x_offset', x_offset), ('y_offset', y_offset),
                  ('z_offset', z_offset)]
        for option, value in values:
            configfile.set(section, option, "%.3f" % (value,))
        self.results.append("[%s] %s" % (section, ", ".join(
            "%s: %.3f" % v for v in values)))
        self._respond("Auto Tune: [%s] %s" % (section, ", ".join(
            "%s=%.3f" % v for v in values)))
        self._set_display("Probe offsets done")

    # G-Code commands
    def _check_not_printing(self, gcmd):
        print_stats = self.printer.lookup_object('print_stats', None)
        if print_stats is None:
            return
        eventtime = self.printer.get_reactor().monotonic()
        if print_stats.get_status(eventtime).get('state') in ('printing',
                                                               'paused'):
            raise gcmd.error("Can't run Auto Tune during a print")
    def cmd_TUNE_MENU(self, gcmd):
        if gcmd.get_int('CLOSE', 0):
            self._close_prompt()
            return
        if self.waiting == 'confirm' and self.step_prompt is not None:
            # Re-open the dialog of the step that is waiting for the user
            self._web_prompt(*self.step_prompt)
            return
        if self.waiting == 'manual_probe':
            gcmd.respond_info("Auto Tune is waiting for the paper test: use"
                              " TESTZ to move the nozzle, then ACCEPT")
            return
        lines = ["Auto Tune items:"]
        for i, name in enumerate(ITEMS):
            lines.append("  %d. %s (%s): %s" % (
                i + 1, ITEM_TITLES[name], name,
                "enabled" if self.enabled[name] else "disabled"))
        if self.sequence is not None:
            lines.append("Running: %s - %s" % (
                ITEM_TITLES.get(self.item, ""), self.message))
        else:
            lines.append("TUNE_ENABLE ITEM=<name> ENABLE=<0|1> toggles an"
                         " item, TUNE_START runs them.")
        gcmd.respond_info("\n".join(lines))
        self._show_menu()
    def cmd_TUNE_ENABLE(self, gcmd):
        raw = gcmd.get('ITEM').lower()
        name = ITEM_ALIASES.get(raw)
        if name is None:
            raise gcmd.error("Unknown item '%s', choose from: %s"
                             % (raw, ", ".join(ITEMS)))
        if self.sequence is not None:
            raise gcmd.error("Can't change items while Auto Tune is running")
        enable = gcmd.get_int('ENABLE', None, minval=0, maxval=1)
        if enable is None:
            enable = not self.enabled[name]
        self.enabled[name] = bool(enable)
        gcmd.respond_info("%s %s" % (ITEM_TITLES[name], "enabled"
                                     if enable else "disabled"))
        if gcmd.get_int('MENU', 0):
            self._show_menu()
    def cmd_TUNE_START(self, gcmd):
        if self.sequence is not None:
            raise gcmd.error("Auto Tune is already running")
        self._check_not_printing(gcmd)
        manual_probe.verify_no_manual_probe(self.printer)
        raw = gcmd.get('ITEM', None)
        if raw is not None:
            # Run only the named items, even if they are switched off
            names = set()
            for r in raw.lower().split(','):
                name = ITEM_ALIASES.get(r.strip())
                if name is None:
                    raise gcmd.error("Unknown item '%s', choose from: %s"
                                     % (r.strip(), ", ".join(ITEMS)))
                names.add(name)
            items = [name for name in ITEMS if name in names]
        else:
            items = [name for name in ITEMS if self.enabled[name]]
        if not items:
            raise gcmd.error("All Auto Tune items are disabled")
        self._close_prompt()
        self.results = []
        self.sequence = self._run_sequence(items)
        self._step(gcmd)
    def cmd_TUNE_CONTINUE(self, gcmd):
        if self.sequence is None:
            raise gcmd.error("Auto Tune is not running")
        if self.waiting == 'manual_probe':
            raise gcmd.error("Use TESTZ to adjust the nozzle and ACCEPT to"
                             " finish the paper test")
        if self.waiting != 'confirm':
            raise gcmd.error("Auto Tune is busy")
        self._close_prompt()
        self._step(gcmd)
    def cmd_TUNE_ABORT(self, gcmd):
        if self.sequence is None:
            self._close_prompt()
            gcmd.respond_info("Auto Tune is not running")
            return
        self._cleanup()
        self._set_display("Auto Tune aborted")
        msg = "Auto Tune aborted."
        configfile = self.printer.lookup_object('configfile')
        if configfile.get_status(None).get('save_config_pending_items'):
            msg += (" Values from finished steps are pending, run"
                    " SAVE_CONFIG to keep them.")
        gcmd.respond_info(msg)
    def cmd_TUNE_JOG(self, gcmd):
        if self.waiting == 'manual_probe':
            raise gcmd.error("Use TESTZ during the paper test")
        toolhead = self._toolhead()
        deltas = [gcmd.get_float(a, 0.) for a in 'XYZ']
        speed = gcmd.get_float('SPEED', self.jog_speed, above=0.)
        moving = [i for i, d in enumerate(deltas) if d]
        if not moving:
            return
        pos = toolhead.get_position()
        if self.free_jog:
            # Axes are not homed yet - re-center the moved axes so that the
            # move is always within the axis limits
            for i in moving:
                lo, hi = self._get_rails()[i].get_range()
                if abs(deltas[i]) > (hi - lo) / 2.:
                    raise gcmd.error("Jog distance too large")
                pos[i] = (lo + hi) / 2.
            self._set_position(pos, moving)
        newpos = list(pos)
        for i in moving:
            newpos[i] += deltas[i]
        toolhead.manual_move(newpos[:3], speed)

def load_config(config):
    return AutoTuner(config)
