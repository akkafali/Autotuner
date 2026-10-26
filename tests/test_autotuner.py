# Tests for autotuner.py using a small simulated printer.
#
# The simulation tracks the "physical" toolhead position separately from
# Klipper's coordinate frame so the limit measurement can be verified
# against known axis lengths.
#
# Run with: python3 -m unittest discover -s tests
import collections, importlib.util, os, sys, types, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class CommandError(Exception):
    pass

######################################################################
# Stub klippy/extras modules imported by autotuner.py
######################################################################

ProbeResult = collections.namedtuple('probe_result', [
    'bed_x', 'bed_y', 'bed_z', 'test_x', 'test_y', 'test_z'])

class FakeHomingMove:
    def __init__(self, printer, endstops, toolhead=None):
        self.printer = printer
        self.endstops = endstops
        self.toolhead = toolhead or printer.lookup_object('toolhead')
    def homing_move(self, movepos, speed, probe_pos=False,
                    triggered=True, check_triggered=True):
        th = self.toolhead
        endstop = self.endstops[0][0]
        axis = endstop.axis
        delta = movepos[axis] - th.pos[axis]
        th.check_move(movepos)
        switch = endstop.switch_phys(th)
        dist = (switch - th.phys[axis]) * (1. if delta > 0 else -1.)
        th.homing_moves.append((axis, delta, speed))
        if dist < 0. or dist > abs(delta):
            raise CommandError("No trigger on %s after full movement"
                               % ("xyz"[axis],))
        trig = th.pos[axis] + (switch - th.phys[axis])
        th.phys[axis] = switch
        th.pos[axis] = trig
        return list(th.pos)

class FakeManualProbeHelper:
    def __init__(self, printer, gcmd, finalize_callback):
        printer.manual_probe_cb = finalize_callback
        printer.lookup_object('manual_probe').status = {'is_active': True}

def verify_no_manual_probe(printer):
    if printer.lookup_object('manual_probe').status.get('is_active'):
        raise CommandError("Already in a manual Z probe")

def load_autotuner():
    pkg = types.ModuleType('extras')
    pkg.__path__ = []
    homing = types.ModuleType('extras.homing')
    homing.HomingMove = FakeHomingMove
    mprobe = types.ModuleType('extras.manual_probe')
    mprobe.ManualProbeHelper = FakeManualProbeHelper
    mprobe.verify_no_manual_probe = verify_no_manual_probe
    pkg.homing, pkg.manual_probe = homing, mprobe
    sys.modules.update({'extras': pkg, 'extras.homing': homing,
                        'extras.manual_probe': mprobe})
    spec = importlib.util.spec_from_file_location(
        'extras.autotuner', os.path.join(ROOT, 'autotuner.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

autotuner = load_autotuner()

######################################################################
# Simulated printer
######################################################################

class FakeConfig:
    def __init__(self, printer, options):
        self.printer = printer
        self.options = options
    def error(self, msg):
        return Exception(msg)
    def get_printer(self):
        return self.printer
    def get_name(self):
        return 'autotuner'
    def get(self, option, default=None):
        return self.options.get(option, default)
    def getboolean(self, option, default=None):
        v = self.options.get(option, default)
        return v if isinstance(v, bool) else str(v).lower() in ('1', 'true')
    def getfloat(self, option, default=None, **kw):
        v = self.options.get(option, default)
        return None if v is None else float(v)
    def getfloatlist(self, option, default=None, count=None):
        v = self.options.get(option)
        if v is None:
            return default
        return tuple(float(p) for p in v.split(','))
    def getchoice(self, option, choices, default=None):
        return choices[self.options.get(option, default)]

HomingInfo = collections.namedtuple('homing_info', [
    'speed', 'position_endstop', 'retract_speed', 'retract_dist',
    'positive_dir', 'second_homing_speed'])

class FakeEndstop:
    def __init__(self, axis, phys_switch, probe=None):
        self.axis = axis
        self.phys_switch = phys_switch
        self.probe = probe
    def switch_phys(self, toolhead):
        return self.phys_switch

class FakeRail:
    def __init__(self, name, axis, pos_min, pos_max, endstop_pos,
                 positive_dir, endstop, retract=5.):
        self.name = name
        self.range = (pos_min, pos_max)
        self.hi = HomingInfo(50., endstop_pos, 50., retract, positive_dir,
                             25.)
        self.endstops = [(endstop, name)]
    def get_name(self):
        return self.name
    def get_range(self):
        return self.range
    def get_homing_info(self):
        return self.hi
    def get_endstops(self):
        return self.endstops

class FakeKin:
    def __init__(self, rails):
        self.rails = rails

class FakeToolhead:
    def __init__(self, rails, phys):
        self.kin = FakeKin(rails)
        self.phys = list(phys)
        self.pos = [0., 0., 0., 0.]
        self.limits = [(1., -1.)] * 3
        self.homing_moves = []
    def get_kinematics(self):
        return self.kin
    def get_position(self):
        return list(self.pos)
    def set_position(self, newpos, homing_axes=""):
        if not isinstance(homing_axes, str):
            raise TypeError("homing_axes must be a string")
        self.pos = list(newpos)
        for a in homing_axes:
            i = "xyz".index(a)
            self.limits[i] = self.kin.rails[i].get_range()
    def clear_homing(self):
        self.limits = [(1., -1.)] * 3
    def check_move(self, newpos):
        for i in range(3):
            if newpos[i] == self.pos[i]:
                continue
            lo, hi = self.limits[i]
            if lo > hi:
                raise CommandError("Must home axis first")
            if newpos[i] < lo or newpos[i] > hi:
                raise CommandError("Move out of range: %s=%.3f"
                                   % ("xyz"[i], newpos[i]))
    def manual_move(self, coord, speed):
        newpos = list(self.pos)
        for i, v in enumerate(coord):
            if v is not None:
                newpos[i] = v
        self.check_move(newpos)
        for i in range(3):
            self.phys[i] += newpos[i] - self.pos[i]
        self.pos = newpos
    def get_status(self, eventtime):
        rails = self.kin.rails
        return {
            'homed_axes': "".join(a for a, (l, h) in zip("xyz", self.limits)
                                  if l <= h),
            'axis_minimum': [r.get_range()[0] for r in rails],
            'axis_maximum': [r.get_range()[1] for r in rails],
        }

class FakeGCodeCommand:
    def __init__(self, gcode, params):
        self.gcode = gcode
        self.params = params
    def error(self, msg):
        return CommandError(msg)
    def respond_info(self, msg):
        self.gcode.respond_info(msg)
    def get(self, name, default=Exception):
        if name in self.params:
            return self.params[name]
        if default is Exception:
            raise CommandError("Missing %s" % (name,))
        return default
    def get_int(self, name, default=Exception, **kw):
        v = self.get(name, default)
        return None if v is None else int(v)
    def get_float(self, name, default=Exception, **kw):
        v = self.get(name, default)
        return None if v is None else float(v)

class FakeConfigfile:
    def __init__(self, settings):
        self.settings = settings
        self.pending = {}
    def set(self, section, option, value):
        self.pending.setdefault(section, {})[option] = value
    def get_status(self, eventtime):
        return {'settings': self.settings,
                'save_config_pending_items': self.pending}

class FakeProbe:
    def __init__(self, sim):
        self.sim = sim
        self.last_z = 0.
    def get_status(self, eventtime):
        return {'last_z_result': self.last_z}
    def get_probe_params(self, gcmd=None):
        return {'lift_speed': 10.}

class FakeManualProbe:
    def __init__(self):
        self.status = {}

class FakeReactor:
    def monotonic(self):
        return 0.

class FakeGCode:
    def __init__(self, sim):
        self.sim = sim
        self.commands = {}
        self.scripts = []
        self.info = []
        self.raw = []
        self.display = []
    def register_command(self, cmd, func, desc=None):
        self.commands[cmd] = func
    def respond_info(self, msg):
        self.info.append(msg)
    def respond_raw(self, msg):
        self.raw.append(msg)
    def run_script_from_command(self, script):
        if script.startswith('M117 '):
            self.display.append(script[5:])
            return
        self.scripts.append(script)
        self.sim.run_script(script)
    def cmd(self, line):
        parts = line.split()
        params = dict(p.split('=', 1) for p in parts[1:])
        self.commands[parts[0]](FakeGCodeCommand(self, params))

class Sim:
    """A printer with known physical dimensions.

    X: switch at the minimum, real length 235 (config says 220)
    Y: switch at the maximum, real length 250 (config says 240)
    Z: probe as virtual endstop (probe z_offset 2.0), real top at 300
    Probe: 30mm left of and 10mm behind the nozzle, triggers 2.0mm above bed
    """
    PROBE_OFFSET = (-30., 10., 2.)
    def __init__(self, options=None, settings=None, accel_dual=False):
        self.reactor = FakeReactor()
        self.objects = {}
        self.manual_probe_cb = None
        x_end = FakeEndstop(0, 0.)
        y_end = FakeEndstop(1, 250.)
        z_end = FakeEndstop(2, self.PROBE_OFFSET[2])
        rails = [FakeRail('stepper_x', 0, -5., 220., -5., False, x_end),
                 FakeRail('stepper_y', 1, 0., 240., 240., True, y_end),
                 FakeRail('stepper_z', 2, -2., 250., self.PROBE_OFFSET[2],
                          False, z_end, retract=0.)]
        # Physical start: somewhere in the middle
        self.toolhead = FakeToolhead(rails, [100., 100., 100.])
        # Frame offsets after a G28 with the *configured* endstops
        self.home_offset = [-5. - 0., 240. - 250., 0.]
        rt = {'accel_chip': 'adxl345'}
        if accel_dual:
            rt = {'accel_chip_x': 'adxl345 hotend',
                  'accel_chip_y': 'adxl345 bed'}
        base_settings = {
            'stepper_z': {'endstop_pin': 'probe:z_virtual_endstop'},
            'bltouch': {'x_offset': 0.}, 'resonance_tester': rt}
        base_settings.update(settings or {})
        self.configfile = FakeConfigfile(base_settings)
        self.gcode = FakeGCode(self)
        self.probe = FakeProbe(self)
        self.objects.update({
            'toolhead': self.toolhead, 'gcode': self.gcode,
            'configfile': self.configfile, 'probe': self.probe,
            'manual_probe': FakeManualProbe(), 'display_status': object(),
            'resonance_tester': object()})
        config = FakeConfig(self, options or {})
        self.tuner = autotuner.load_config(config)
        self.objects['autotuner'] = self.tuner
    # Printer API
    command_error = CommandError
    def lookup_object(self, name, default=Exception):
        if name in self.objects:
            return self.objects[name]
        if default is Exception:
            raise Exception("Unknown object %s" % (name,))
        return default
    def load_object(self, config, name):
        return self.objects.get(name)
    def get_reactor(self):
        return self.reactor
    def is_shutdown(self):
        return False
    # Simulated g-code
    def run_script(self, script):
        th = self.toolhead
        parts = script.split()
        cmd = parts[0]
        if cmd == 'G28':
            axes = [a.lower() for a in parts[1:]] or list('xyz')
            for a in axes:
                i = 'xyz'.index(a)
                rail = th.kin.rails[i]
                th.phys[i] = rail.endstops[0][0].phys_switch
                th.pos[i] = rail.get_homing_info().position_endstop
                th.limits[i] = rail.get_range()
        elif cmd == 'M84':
            th.clear_homing()
        elif cmd == 'PROBE':
            # Descend until the probe triggers
            target = self.PROBE_OFFSET[2]
            th.pos[2] += target - th.phys[2]
            th.phys[2] = target
            self.probe.last_z = th.pos[2]
            self.last_probe_xy = (th.phys[0] + self.PROBE_OFFSET[0],
                                  th.phys[1] + self.PROBE_OFFSET[1])
        elif cmd == 'SHAPER_CALIBRATE':
            axis = parts[1].split('=')[1].lower()
            if 'xyz' != th.get_status(0)['homed_axes']:
                raise CommandError("Must home axis first")
            self.configfile.set('input_shaper', 'shaper_type_' + axis, 'mzv')
            self.configfile.set('input_shaper', 'shaper_freq_' + axis,
                                {'x': '52.4', 'y': '38.2'}[axis])
        elif cmd in ('SAVE_CONFIG', 'ABORT'):
            if cmd == 'ABORT' and self.manual_probe_cb:
                self.objects['manual_probe'].status = {}
                cb, self.manual_probe_cb = self.manual_probe_cb, None
                cb(None)
        else:
            raise CommandError("Unknown command %s" % (cmd,))
    def accept_manual_probe(self, z):
        self.objects['manual_probe'].status = {}
        cb, self.manual_probe_cb = self.manual_probe_cb, None
        cb(ProbeResult(0., 0., z, 0., 0., z))

######################################################################
# Tests
######################################################################

class AutoTunerTest(unittest.TestCase):
    def jog_physical(self, sim, target):
        # Move each axis to a physical position with TUNE_JOG steps
        th = sim.toolhead
        for i, a in enumerate('XYZ'):
            while abs(target[i] - th.phys[i]) > 1e-9:
                step = max(-50., min(50., target[i] - th.phys[i]))
                sim.gcode.cmd("TUNE_JOG %s=%s" % (a, step))

    def test_limits_only(self):
        sim = Sim({'input_shaper': False, 'probe_offsets': False})
        sim.gcode.cmd("TUNE_START")
        self.assertTrue(sim.tuner.get_status()['waiting'])
        self.assertIn("// action:prompt_show", sim.gcode.raw)
        # User jogs X to its max, Y to its min (switch at max), Z to the top
        self.jog_physical(sim, [235., 0., 300.])
        sim.gcode.cmd("TUNE_CONTINUE")
        st = sim.tuner.get_status()
        self.assertFalse(st['active'])
        p = sim.configfile.pending
        self.assertEqual(p['stepper_x'], {'position_endstop': '0.000',
                                          'position_min': '0.000',
                                          'position_max': '235.000'})
        self.assertEqual(p['stepper_y'], {'position_endstop': '250.000',
                                          'position_max': '250.000'})
        self.assertEqual(p['stepper_z'], {'position_max': '300.000'})
        self.assertEqual(sim.gcode.scripts[-1], "SAVE_CONFIG")
        # All axes homed in the configured frame (same as a G28 would give)
        th = sim.toolhead
        self.assertEqual(th.get_status(0)['homed_axes'], 'xyz')
        for i in range(3):
            self.assertAlmostEqual(th.pos[i] - th.phys[i],
                                   sim.home_offset[i])
        # Z (probe endstop) was measured over the middle of the bed
        self.assertEqual(th.pos[:3], [107.5, 120., 2.])

    def test_limits_short_axis_and_motors_off(self):
        # Hand-moved axes (motors off) and an axis much shorter than the
        # search distance
        sim = Sim({'input_shaper': False, 'probe_offsets': False,
                   'limit_axes': 'x', 'zero_at_switch': False})
        sim.gcode.cmd("TUNE_START")
        sim.toolhead.clear_homing()           # M84
        sim.toolhead.phys[0] = 60.            # moved by hand
        sim.gcode.cmd("TUNE_CONTINUE")
        p = sim.configfile.pending
        self.assertEqual(p['stepper_x'], {'position_max': '55.000'})
        self.assertIn("G28 Y", sim.gcode.scripts)
        self.assertIn("G28 Z", sim.gcode.scripts)

    def test_limits_rejects_unmoved_axis(self):
        sim = Sim({'input_shaper': False, 'probe_offsets': False,
                   'limit_axes': 'x'})
        sim.gcode.cmd("TUNE_START")
        sim.toolhead.phys[0] = 3.
        with self.assertRaises(CommandError) as cm:
            sim.gcode.cmd("TUNE_CONTINUE")
        self.assertIn("only travelled", str(cm.exception))
        self.assertFalse(sim.tuner.get_status()['active'])
        # Positions can't be trusted any more - motors are turned off
        self.assertEqual(sim.gcode.scripts[-1], "M84")

    def test_input_shaper_single_accel(self):
        sim = Sim({'limits': False, 'probe_offsets': False,
                   'save_config': False})
        sim.gcode.cmd("TUNE_START")
        self.assertEqual(sim.gcode.scripts[:2],
                         ["G28", "SHAPER_CALIBRATE AXIS=X"])
        self.assertTrue(sim.tuner.get_status()['waiting'])
        self.assertNotIn("SHAPER_CALIBRATE AXIS=Y", sim.gcode.scripts)
        sim.gcode.cmd("TUNE_CONTINUE")
        self.assertIn("SHAPER_CALIBRATE AXIS=Y", sim.gcode.scripts)
        self.assertNotIn("SAVE_CONFIG", sim.gcode.scripts)
        self.assertFalse(sim.tuner.get_status()['active'])
        self.assertIn("shaper_freq_y: 38.2", "\n".join(sim.gcode.info))

    def test_input_shaper_dual_accel(self):
        sim = Sim({'limits': False, 'probe_offsets': False},
                  accel_dual=True)
        sim.gcode.cmd("TUNE_START")
        self.assertEqual(sim.gcode.scripts[:3],
                         ["G28", "SHAPER_CALIBRATE AXIS=X",
                          "SHAPER_CALIBRATE AXIS=Y"])
        self.assertEqual(sim.gcode.scripts[-1], "SAVE_CONFIG")

    def test_input_shaper_explicit_chips(self):
        sim = Sim({'limits': False, 'probe_offsets': False,
                   'accelerometers': '2', 'accel_chip_x': 'adxl345 a',
                   'accel_chip_y': 'lis2dw b'})
        sim.gcode.cmd("TUNE_START")
        self.assertIn("SHAPER_CALIBRATE AXIS=X CHIPS=adxl345 a",
                      sim.gcode.scripts)
        self.assertIn("SHAPER_CALIBRATE AXIS=Y CHIPS=lis2dw b",
                      sim.gcode.scripts)

    def test_probe_offsets(self):
        sim = Sim({'limits': False, 'input_shaper': False})
        th = sim.toolhead
        sim.gcode.cmd("TUNE_START")
        # Waiting for the post-it, toolhead over the bed center
        self.assertEqual(th.pos[:2], [107.5, 120.])
        sim.gcode.cmd("TUNE_CONTINUE")
        self.assertEqual(sim.gcode.scripts[-1], "PROBE SAMPLES=1")
        self.assertEqual(sim.gcode.display[-1], "Nozzle to mark")
        mark = sim.last_probe_xy
        # User moves the nozzle over the mark and lowers Z
        self.jog_physical(sim, [mark[0], mark[1], 0.5])
        sim.gcode.cmd("TUNE_CONTINUE")
        # Remove the post-it
        sim.gcode.cmd("TUNE_CONTINUE")
        self.assertTrue(sim.tuner.get_status()['manual_probe'])
        with self.assertRaises(CommandError):
            sim.gcode.cmd("TUNE_CONTINUE")
        # Nozzle is above the point the probe just measured
        self.assertAlmostEqual(th.phys[0], 107.5 + 5. - 30.)
        self.assertAlmostEqual(th.phys[1], 120. + 10. + 10.)
        # Paper test: nozzle touches the bed at physical z=0
        sim.accept_manual_probe(th.pos[2] - th.phys[2])
        p = sim.configfile.pending['bltouch']
        self.assertEqual(p, {'x_offset': '-30.000', 'y_offset': '10.000',
                             'z_offset': '2.000'})
        self.assertEqual(sim.gcode.scripts[-1], "SAVE_CONFIG")
        self.assertFalse(sim.tuner.get_status()['active'])

    def test_probe_paper_test_abort(self):
        sim = Sim({'limits': False, 'input_shaper': False})
        sim.gcode.cmd("TUNE_START")
        for i in range(3):
            sim.gcode.cmd("TUNE_CONTINUE")
        sim.gcode.cmd("TUNE_ABORT")
        self.assertFalse(sim.tuner.get_status()['active'])
        self.assertIn("ABORT", sim.gcode.scripts)
        self.assertNotIn('bltouch', sim.configfile.pending)

    def test_full_run(self):
        sim = Sim()
        sim.gcode.cmd("TUNE_START")
        self.jog_physical(sim, [235., 0., 300.])
        sim.gcode.cmd("TUNE_CONTINUE")          # limits -> shaper X
        self.assertEqual(sim.tuner.get_status()['item'], 'input_shaper')
        sim.gcode.cmd("TUNE_CONTINUE")          # shaper Y -> post-it
        self.assertEqual(sim.tuner.get_status()['item'], 'probe_offsets')
        sim.gcode.cmd("TUNE_CONTINUE")
        mark = sim.last_probe_xy
        self.jog_physical(sim, [mark[0], mark[1], 1.])
        sim.gcode.cmd("TUNE_CONTINUE")
        sim.gcode.cmd("TUNE_CONTINUE")
        th = sim.toolhead
        sim.accept_manual_probe(th.pos[2] - th.phys[2])
        self.assertEqual(sorted(sim.configfile.pending),
                         ['bltouch', 'input_shaper', 'stepper_x',
                          'stepper_y', 'stepper_z'])
        self.assertEqual(sim.gcode.scripts.count("SAVE_CONFIG"), 1)

    def test_enable_disable(self):
        sim = Sim()
        sim.gcode.cmd("TUNE_ENABLE ITEM=shaper ENABLE=0")
        sim.gcode.cmd("TUNE_ENABLE ITEM=limits")
        st = sim.tuner.get_status()
        self.assertEqual((st['limits'], st['input_shaper'],
                          st['probe_offsets']), (False, False, True))
        sim.gcode.cmd("TUNE_ENABLE ITEM=probe ENABLE=0")
        with self.assertRaises(CommandError):
            sim.gcode.cmd("TUNE_START")
        with self.assertRaises(CommandError):
            sim.gcode.cmd("TUNE_ENABLE ITEM=bogus")
        sim.gcode.cmd("TUNE_MENU")
        self.assertIn("// action:prompt_begin Auto Tune", sim.gcode.raw)
        self.assertIn("// action:prompt_button Bed probe offsets: OFF|"
                      "TUNE_ENABLE ITEM=probe_offsets MENU=1|secondary",
                      sim.gcode.raw)

    def test_menu_reopens_waiting_step(self):
        sim = Sim({'limits': False, 'input_shaper': False})
        sim.gcode.cmd("TUNE_START")
        sim.gcode.cmd("TUNE_MENU CLOSE=1")
        self.assertEqual(sim.gcode.raw[-1], "// action:prompt_end")
        del sim.gcode.raw[:]
        sim.gcode.cmd("TUNE_MENU")
        self.assertIn("// action:prompt_begin Probe offsets: post-it",
                      sim.gcode.raw)
        self.assertIn("// action:prompt_footer_button Continue|"
                      "TUNE_CONTINUE|primary", sim.gcode.raw)

    def test_abort_limits_turns_motors_off(self):
        sim = Sim()
        sim.gcode.cmd("TUNE_START")
        sim.gcode.cmd("TUNE_JOG X=10")
        sim.gcode.cmd("TUNE_ABORT")
        self.assertEqual(sim.gcode.scripts[-1], "M84")
        self.assertEqual(sim.gcode.display[-1], "Auto Tune aborted")
        self.assertEqual(sim.toolhead.get_status(0)['homed_axes'], '')

if __name__ == '__main__':
    unittest.main()
