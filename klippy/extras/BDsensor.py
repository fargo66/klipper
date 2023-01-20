#import libraries
#/home/pi/klippy-env/bin/pip2 install RPi.GPIO
import RPi.GPIO as GPIO 
import time
import logging
from . import probe
from . import bus
import chelper
import mcu

#GPIO Basic initialization
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

SIGNAL_PERIOD = 0.020
MIN_CMD_TIME = 5 * SIGNAL_PERIOD

TEST_TIME = 5 * 60.
RETRY_RESET_TIME = 1.
ENDSTOP_REST_TIME = .001
ENDSTOP_SAMPLE_TIME = .000015
ENDSTOP_SAMPLE_COUNT = 4

Commands = {
    'pin_down': 0.000650, 'touch_mode': 0.001165,
    'pin_up': 0.001475, 'self_test': 0.001780, 'reset': 0.002190,
    'set_5V_output_mode' : 0.001988, 'set_OD_output_mode' : 0.002091,
    'output_mode_store' : 0.001884,
}

# Calculate a move's accel_t, cruise_t, and cruise_v
def calc_move_time(dist, speed, accel):
    axis_r = 1.
    if dist < 0.:
        axis_r = -1.
        dist = -dist
    if not accel or not dist:
        return axis_r, 0., dist / speed, speed
    max_cruise_v2 = dist * accel
    if max_cruise_v2 < speed**2:
        speed = math.sqrt(max_cruise_v2)
    accel_t = speed / accel
    accel_decel_d = accel_t * speed
    cruise_t = (dist - accel_decel_d) / speed
    return axis_r, accel_t, cruise_t, speed

######################################################################
# I2C BD_SENSOR
######################################################################

# Helper code for working with devices connected to an MCU via an i2c software bus

class MCU_I2C_BD:
    def __init__(self,mcu,   sda_pin,scl_pin, delay_t):
        
        self.mcu = mcu
     
        # Config  
        self.oid = self.mcu.create_oid()

        # Generate I2C bus config message

        self.config_fmt = (
            "config_I2C_BD oid=%d sda_pin=%s scl_pin=%s delay=%s" % (self.oid, sda_pin,scl_pin, delay_t))
        
        self.cmd_queue = mcu.alloc_command_queue()
        mcu.register_config_callback(self.build_config)
        self.I2C_BD_send_cmd = self.I2C_BD_receive_cmd = None
      
    def build_config(self):      
        self.mcu.add_config_cmd(self.config_fmt)
        self.I2C_BD_send_cmd = self.mcu.lookup_command(
            "I2C_BD_send oid=%c data=%*s", cq=self.cmd_queue)
        self.I2C_BD_receive_cmd = self.mcu.lookup_query_command(
            "I2C_BD_receive oid=%c data=%*s",
            "I2C_BD_receive_response oid=%c response=%*s", oid=self.oid, cq=self.cmd_queue)   
    def get_oid(self):
        return self.oid       
    def get_mcu(self):
        return self.mcu
    def get_command_queue(self):
        return self.cmd_queue    
    def I2C_BD_send(self, data):
      #  if self.I2C_BD_send_cmd is None:
            # Send setup message via mcu initialization
       #     data_msg = "".join(["%02x" % (x,) for x in data])
       #     self.mcu.add_config_cmd("I2C_BD_send oid=%d data0=%u data1=%u" % (self.oid, data[0],data[1]), is_init=True)
       #     print ("I2C_BD_send oid=%d data0=%u data1=%u" % (self.oid, data[0],data[1]))
       #     return
        print ("I2C_BD_send0 oid=%c %s " % (self.oid,data))    
        self.I2C_BD_send_cmd.send([self.oid, data])
    def I2C_BD_receive(self,  data):
        return self.I2C_BD_receive_cmd.send([self.oid, data])
       # return self.I2C_BD_receive_cmd.send([self.oid, data],minclock=minclock, reqclock=reqclock)


       
# Helper to setup an spi bus from settings in a config section
def MCU_BD_I2C_from_config(mcu,config):
    # Determine pin from config
    ppins = config.get_printer().lookup_object("pins")
  
    
    # Create MCU_SPI object
    return MCU_I2C_BD(mcu,config.get('sda_pin'),config.get('scl_pin'),config.get('delay'))

# BDsensor wrapper that enables probe specific features
class BDsensorEndstopWrapper:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.position_endstop = config.getfloat('z_offset')
        self.stow_on_each_sample = config.getboolean(
            'deactivate_on_each_sample', True)
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.activate_gcode = gcode_macro.load_template(
            config, 'activate_gcode', '')
        self.deactivate_gcode = gcode_macro.load_template(
            config, 'deactivate_gcode', '')
        # Create an "endstop" object to handle the probe pin
        ppins = self.printer.lookup_object('pins')
        pin = config.get('sda_pin')
        pin_params = ppins.lookup_pin(pin, can_invert=True, can_pullup=True)
      #  self.mcu = pin_params['chip']
        self.mcu = mcu.get_printer_mcu(self.printer, 'mcu')      
        pin_params['pullup']=2
        self.mcu_endstop = self.mcu.setup_pin('endstop', pin_params)
        self.printer.register_event_handler('klippy:mcu_identify',
                                            self._handle_mcu_identify)
        self.oid = self.mcu.create_oid()
        self.cmd_queue = self.mcu.alloc_command_queue()
        
        # Setup iterative solver
        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.stepper_kinematics = ffi_main.gc(
            ffi_lib.cartesian_stepper_alloc(b'x'), ffi_lib.free)

        self.bd_sensor=MCU_BD_I2C_from_config(self.mcu,config) 
        self.distance=5;
        # Register PROBE/QUERY_PROBE commands
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('M102', self.cmd_M102)

        
        # Wrappers
        self.get_mcu = self.mcu_endstop.get_mcu
        self.add_stepper = self.mcu_endstop.add_stepper
        self.get_steppers = self.mcu_endstop.get_steppers
        self.home_start = self.mcu_endstop.home_start
        self.home_wait = self.mcu_endstop.home_wait
        self.query_endstop = self.mcu_endstop.query_endstop
        # multi probes state
        self.multi = 'OFF'

        self.mcu.register_config_callback(self.build_config)
        
    def build_config(self):      
       self.I2C_BD_receive_cmd = self.mcu.lookup_query_command(
           "I2C_BD_receive oid=%c data=%*s",
           "I2C_BD_receive_response oid=%c response=%*s", oid=self.oid, cq=self.cmd_queue) 

    def _force_enable(self,stepper):
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        stepper_enable = self.printer.lookup_object('stepper_enable')
        enable = stepper_enable.lookup_enable(stepper.get_name())
        was_enable = enable.is_motor_enabled()
        STALL_TIME = 0.100
        if not was_enable:
            enable.motor_enable(print_time)
            toolhead.dwell(STALL_TIME)
        return was_enable

    def manual_move(self, stepper, dist, speed, accel=0.):
         toolhead = self.printer.lookup_object('toolhead')
         toolhead.flush_step_generation()
         prev_sk = stepper.set_stepper_kinematics(self.stepper_kinematics)
         prev_trapq = stepper.set_trapq(self.trapq)
         stepper.set_position((0., 0., 0.))
         axis_r, accel_t, cruise_t, cruise_v = calc_move_time(dist, speed, accel)
         print_time = toolhead.get_last_move_time()
         self.trapq_append(self.trapq, print_time, accel_t, cruise_t, accel_t,
                           0., 0., 0., axis_r, 0., 0., 0., cruise_v, accel)
         print_time = print_time + accel_t + cruise_t + accel_t
         stepper.generate_steps(print_time)
         self.trapq_finalize_moves(self.trapq, print_time + 99999.9)
         stepper.set_trapq(prev_trapq)
         stepper.set_stepper_kinematics(prev_sk)
         toolhead.note_kinematic_activity(print_time)
         toolhead.dwell(accel_t + cruise_t + accel_t)
        
    def cmd_M102(self, gcmd, wait=False):
        # Set Extruder Temperature
        CMD_BD = gcmd.get_int('S', None)
        toolhead = self.printer.lookup_object('toolhead')
        if CMD_BD == -6:            
            kin = toolhead.get_kinematics()
            for stepper in kin.get_steppers():
                if stepper.is_active_axis('z'):                    
                    self.bd_sensor.I2C_BD_send("1019") #CMD_START_CALIBRATE=1019 
                  #  toolhead.dwell(1) 
                  #  stepper = self._lookup_stepper(gcod)
                    distance = 0.5#gcmd.get_float('DISTANCE')
                    speed = 10#gcmd.get_float('VELOCITY', above=0.)
                    accel = 2000#gcmd.get_float('ACCEL', 0., minval=0.)
                    #logging.info("FORCE_MOVE %s distance=%.3f velocity=%.3f accel=%.3f",
                    #             stepper.get_name(), distance, speed, accel)
                    #print"self.distance===%.3f"%self.distance
                    self.distance=0.1
                    self._force_enable(stepper)
                   # self.manual_move(stepper, self.distance, speed)
                  #  self.manual_move(stepper, -self.distance, speed)
                   # toolhead.dwell(1)
                    toolhead.wait_moves()
                    ncount=0

                    while 1:
                        #self.bd_sensor.I2C_BD_send(str(ncount))                        
                        toolhead.dwell(0.5)
                        self.bd_sensor.I2C_BD_send(str(ncount))
                        toolhead.dwell(0.5)
                        self.bd_sensor.I2C_BD_send(str(ncount))
                        toolhead.dwell(0.5)
                        self._force_enable(stepper)
                        self.manual_move(stepper, self.distance, speed)
                        toolhead.wait_moves()
                       # toolhead.dwell(0.5)
                        ncount=ncount+1
                        
                        if ncount>=40: 
                            self.bd_sensor.I2C_BD_send("1021")
                            break
        if  CMD_BD == -5:                           
            self.bd_sensor.I2C_BD_send("1017")#1017 // start reading raw calibration data
            ncount1=0
            while 1:
                pr = self.I2C_BD_receive_cmd.send([self.oid, "32"])
              #  print"params:%s" % pr['response']
                intd=int(pr['response'])
                strd=str(intd)
                gcmd.respond_raw(strd)
                toolhead.dwell(0.1)
                ncount1=ncount1+1
                if ncount1>=40: 
                    break
        if  CMD_BD == -1:                           
            self.bd_sensor.I2C_BD_send("1016")#1016 // // read sensor version
            ncount1=0
            x=[]
            while 1:
                pr = self.I2C_BD_receive_cmd.send([self.oid, "32"])
              #  print"params:%s" % pr['response']
                intd=int(pr['response'])
                if intd>127:
                    intd=127
                if intd<0x20:
                    intd=0x20
                x.append(intd)
                toolhead.dwell(0.3)
                ncount1=ncount1+1
                if ncount1>=20: 
                    self.bd_sensor.I2C_BD_send("1018")#1018   // finish reading   data  
                    res = ''.join(map(chr, x))
                    gcmd.respond_raw(res)
                    break
        self.bd_sensor.I2C_BD_send("1018")#1018               
    def _handle_mcu_identify(self):
        print("_handle_mcu_identify")
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        for stepper in kin.get_steppers():
            if stepper.is_active_axis('z'):
                self.add_stepper(stepper)
    def raise_probe(self):
        print("raise_probe")
        
        toolhead = self.printer.lookup_object('toolhead')
        start_pos = toolhead.get_position()
        self.deactivate_gcode.run_gcode_from_command()
        if toolhead.get_position()[:3] != start_pos[:3]:
            raise self.printer.command_error(
                "Toolhead moved during probe activate_gcode script")
    def lower_probe(self):
        print("lower_probe")
        toolhead = self.printer.lookup_object('toolhead')
        start_pos = toolhead.get_position()
        self.activate_gcode.run_gcode_from_command()
        if toolhead.get_position()[:3] != start_pos[:3]:
            raise self.printer.command_error(
                "Toolhead moved during probe deactivate_gcode script")
    def multi_probe_begin(self):
        print("multi_probe_begin")
        if self.stow_on_each_sample:
            return
        self.multi = 'FIRST'
    def multi_probe_end(self):
        print("multi_probe_end")
        if self.stow_on_each_sample:
            return
        self.raise_probe()
        self.multi = 'OFF'
    def probe_prepare(self, hmove):
        print("probe_prepare")
       # self.bd_sensor.I2C_BD_send("890")
        if self.multi == 'OFF' or self.multi == 'FIRST':
            self.lower_probe()
            if self.multi == 'FIRST':
                self.multi = 'ON'
    def probe_finish(self, hmove):
        print("probe_finish")
        if self.multi == 'OFF':
            self.raise_probe()
    def get_position_endstop(self):
        print("get_position_endstop")
        return self.position_endstop

   
def load_config(config):
    bdl=BDsensorEndstopWrapper(config)
    config.get_printer().add_object('probe', probe.PrinterProbe(config, bdl))
    return bdl


