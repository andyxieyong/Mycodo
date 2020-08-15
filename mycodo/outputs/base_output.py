# coding=utf-8
"""
This module contains the AbstractOutput Class which acts as a template
for all outputs. It is not to be used directly. The AbstractOutput Class
ensures that certain methods and instance variables are included in each
Output.

All Outputs should inherit from this class and overwrite methods that raise
NotImplementedErrors
"""
import datetime
import logging
import threading
import time
import timeit

from sqlalchemy import and_
from sqlalchemy import or_

from mycodo.abstract_base_controller import AbstractBaseController
from mycodo.databases.models import Trigger
from mycodo.mycodo_client import DaemonControl
from mycodo.utils.database import db_retrieve_table_daemon
from mycodo.utils.influx import write_influxdb_value
from mycodo.utils.outputs import output_types


class AbstractOutput(AbstractBaseController):
    """
    Base Output class that ensures certain methods and values are present
    in outputs.
    """
    def __init__(self, output, testing=False, name=__name__):
        if not testing:
            super(AbstractOutput, self).__init__(output.unique_id, testing=testing, name=__name__)
        else:
            super(AbstractOutput, self).__init__(None, testing=testing, name=__name__)

        self.startup_timer = timeit.default_timer()
        self.output_setup = False

        self.logger = None
        self.setup_logger(testing=testing, name=name, output_dev=output)
        self.control = DaemonControl()

        self.OUTPUT_INFORMATION = None
        self.output_state = None
        self.output_on_until = datetime.datetime.now()
        self.output_off_until = None
        self.output_last_duration = 0
        self.output_on_duration = False
        self.output_off_triggered = False
        self.output_time_turned_on = None
        self.output_types = []

        if not testing:
            self.output = output
            self.output_types = output_types()
            self.unique_id = output.unique_id
            self.output_name = self.output.name
            self.output_type = self.output.output_type
            self.output_force_command = self.output.force_command

        self.running = True

    def __iter__(self):
        """ Support the iterator protocol """
        return self

    def __repr__(self):
        """  Representation of object """
        return_str = '<{cls}'.format(cls=type(self).__name__)
        return_str += '>'
        return return_str

    def __str__(self):
        """ Return measurement information """
        return_str = ''
        return return_str

    def output_switch(self, state, output_type=None, amount=None):
        self.logger.error(
            "{cls} did not overwrite the output_switch() method. All "
            "subclasses of the AbstractOutput class are required to overwrite "
            "this method".format(cls=type(self).__name__))
        raise NotImplementedError

    def is_on(self):
        self.logger.error(
            "{cls} did not overwrite the is_on() method. All "
            "subclasses of the AbstractOutput class are required to overwrite "
            "this method".format(cls=type(self).__name__))
        raise NotImplementedError

    def is_setup(self):
        self.logger.error(
            "{cls} did not overwrite the is_setup() method. All "
            "subclasses of the AbstractOutput class are required to overwrite "
            "this method".format(cls=type(self).__name__))
        raise NotImplementedError

    def setup_output(self):
        self.logger.error(
            "{cls} did not overwrite the setup_output() method. All "
            "subclasses of the AbstractOutput class are required to overwrite "
            "this method".format(cls=type(self).__name__))
        raise NotImplementedError

    def stop_output(self):
        """ Called when Output is stopped """
        self.running = False
        try:
            # Release all locks
            for lockfile, lock_state in self.lockfile.locked.items():
                if lock_state:
                    self.lock_release(lockfile)
        except:
            pass

    #
    # Do not overwrite the function below
    #

    def init_post(self):
        self.logger.info("Initialized in {:.1f} ms".format(
            (timeit.default_timer() - self.startup_timer) * 1000))

    def setup_logger(self, testing=None, name=None, output_dev=None):
        name = name if name else __name__
        if not testing and output_dev:
            log_name = "{}_{}".format(name, output_dev.unique_id.split('-')[0])
        else:
            log_name = name
        self.logger = logging.getLogger(log_name)
        if not testing and output_dev:
            if output_dev.log_level_debug:
                self.logger.setLevel(logging.DEBUG)
            else:
                self.logger.setLevel(logging.INFO)

    def setup_on_off_output(self, output_information):
        self.OUTPUT_INFORMATION = output_information
        self.output_state = None
        self.output_time_turned_on = None
        self.output_on_duration = False
        self.output_last_duration = 0
        self.output_on_until = datetime.datetime.now()
        self.output_off_until = 0
        self.output_off_triggered = False

    def shutdown(self, shutdown_timer):
        self.stop_output()
        self.logger.info("Stopped in {:.1f} ms".format(
            (timeit.default_timer() - shutdown_timer) * 1000))

    def output_on_off(self,
                      state,
                      output_type=None,
                      amount=0.0,
                      min_off=0.0,
                      trigger_conditionals=True):
        """
        Manipulate an output by passing on/off, a volume, or a PWM duty cycle
        to the output module.

        :param state: What state is desired? 'on', 1, True or 'off', 0, False
        :type state: str or int or bool
        :param output_type: The type of output ('sec', 'vol', 'pwm')
        :type output_type: str
        :param amount: If state is 'on', an amount can be set (e.g. duration to stay on, volume to output, etc.)
        :type amount: float
        :param min_off: Don't allow on again for at least this amount (0 = disabled)
        :type min_off: float
        :param trigger_conditionals: Whether to allow trigger conditionals to act or not
        :type trigger_conditionals: bool
        """
        msg = ''

        self.logger.debug("output_on_off({}, {}, {}, {}, {})".format(
            state,
            output_type,
            amount,
            min_off,
            trigger_conditionals))

        if state not in ['on', 1, True, 'off', 0, False]:
            return 1, 'state not "on", 1, True, "off", 0, or False'
        elif state in ['on', 1, True]:
            state = 'on'
        elif state in ['off', 0, False]:
            state = 'off'

        current_time = datetime.datetime.now()

        if amount is None:
            amount = 0

        output_is_on = self.is_on()

        # Check if output is set up
        if not self.is_setup():
            msg = "Cannot manipulate Output: not set up."
            self.logger.error(msg)
            return 1, msg

        #
        # Signaled to turn output on
        #
        if state == 'on':

            # Checks if device is not on and is instructed to turn on
            if (output_type == 'on_off' and
                    'output_types' in self.OUTPUT_INFORMATION and
                    not output_is_on):

                # Check if time is greater than off_until to allow an output on.
                # If the output is supposed to be off for a minimum duration and that amount
                # of time has not passed, do not allow the output to be turned on.
                if self.output_off_until and self.output_off_until > current_time:
                    off_seconds = (self.output_off_until - current_time).total_seconds()
                    msg = "Output {id} ({name}) instructed to turn on, " \
                          "however the output has been instructed to stay " \
                          "off for {off_sec:.2f} more seconds.".format(
                            id=self.unique_id,
                            name=self.output_name,
                            off_sec=off_seconds)
                    self.logger.debug(msg)
                    return 1, msg

            # Output type: Volume, set amount
            if output_type == 'vol' and self.output_type in self.output_types['volume']:
                self.output_switch('on', output_type='vol', amount=amount)

                msg = "Command sent: Output {id} ({name}) volume: {v:.1f} ".format(
                    id=self.unique_id,
                    name=self.output_name,
                    v=amount)

            # Output type: PWM, set duty cycle
            elif output_type == 'pwm' and self.output_type in self.output_types['pwm']:
                self.output_switch('on', output_type='pwm', amount=amount)

                msg = "Command sent: Output {id} ({name}) duty cycle: {dc:.2f} ".format(
                    id=self.unique_id,
                    name=self.output_name,
                    dc=amount)

            # Output type: On/Off, set duration for on state
            elif (output_type in ['sec', None] and
                    self.output_type in self.output_types['on_off'] and
                    amount != 0):
                # If a minimum off duration is set, determine the time the output is allowed to turn on again
                if min_off:
                    self.output_off_until = current_time + datetime.timedelta(seconds=abs(amount) + min_off)

                # Output is already on for an amount, update duration on with new end time
                if output_is_on and self.output_on_duration:
                    if self.output_on_until > current_time:
                        remaining_time = (self.output_on_until - current_time).total_seconds()
                    else:
                        remaining_time = 0

                    time_on = abs(self.output_last_duration) - remaining_time
                    msg = "Output {id} ({name}) is already on for an " \
                          "amount of {on:.2f} seconds (with {remain:.2f} " \
                          "seconds remaining). Recording the amount of time " \
                          "the output has been on ({beenon:.2f} sec) and " \
                          "updating the amount to {newon:.2f} " \
                          "seconds.".format(
                            id=self.unique_id,
                            name=self.output_name,
                            on=abs(self.output_last_duration),
                            remain=remaining_time,
                            beenon=time_on,
                            newon=abs(amount))
                    self.logger.debug(msg)
                    self.output_on_until = (current_time + datetime.timedelta(seconds=abs(amount)))
                    self.output_last_duration = amount

                    # Write the amount the output was ON to the
                    # database at the timestamp it turned ON
                    if time_on > 0:
                        # Make sure the recorded value is recorded negative
                        # if instructed to do so
                        if self.output_last_duration < 0:
                            duration_on = float(-time_on)
                        else:
                            duration_on = float(time_on)
                        timestamp = datetime.datetime.utcnow() - datetime.timedelta(seconds=abs(duration_on))

                        write_db = threading.Thread(
                            target=write_influxdb_value,
                            args=(self.unique_id,
                                  's',
                                  duration_on,),
                            kwargs={'measure': 'duration_time',
                                    'channel': 0,
                                    'timestamp': timestamp})
                        write_db.start()

                    return 0, msg

                # Output is on, but not for an amount
                elif output_is_on and not self.output_on_duration:

                    self.output_on_duration = True
                    self.output_on_until = (current_time + datetime.timedelta(seconds=abs(amount)))
                    self.output_last_duration = amount
                    msg = "Output {id} ({name}) is currently on without an " \
                          "amount. Turning into an amount of {dur:.1f} " \
                          "seconds.".format(
                            id=self.unique_id,
                            name=self.output_name,
                            dur=abs(amount))
                    self.logger.debug(msg)
                    return 0, msg

                # Output is not already on
                else:
                    msg = "Output {id} ({name}) on for {dur:.1f} " \
                          "seconds.".format(
                            id=self.unique_id,
                            name=self.output_name,
                            dur=abs(amount))
                    self.logger.debug(msg)

                    self.output_switch('on', output_type='sec', amount=amount)
                    self.output_on_until = (current_time + datetime.timedelta(seconds=abs(amount)))
                    self.output_last_duration = amount
                    self.output_on_duration = True

            # No duration specific, so just turn output on
            elif ('output_types' in self.OUTPUT_INFORMATION and
                    'on_off' in self.OUTPUT_INFORMATION['output_types'] and
                    amount in [None, 0] and
                    output_type in ['sec', None]):

                # Don't turn on if already on, except if it can be forced on
                if output_is_on and not self.output_force_command:
                    msg = "Output {id} ({name}) is already on.".format(
                        id=self.unique_id,
                        name=self.output_name)
                    self.logger.debug(msg)
                    return 1, msg
                else:
                    # Record the time the output was turned on in order to
                    # calculate and log the total amount is was on, when
                    # it eventually turns off.
                    if not self.output_time_turned_on:
                        self.output_time_turned_on = current_time
                    msg = "Output {id} ({name}) ON at {timeon}.".format(
                        id=self.unique_id,
                        name=self.output_name,
                        timeon=self.output_time_turned_on)
                    self.logger.debug(msg)
                    self.output_switch('on', output_type='sec')

        #
        # Signaled to turn output off
        #
        elif state == 'off':

            self.output_switch('off', output_type=output_type)

            timestamp = datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')
            msg = "Output {id} ({name}) OFF at {timeoff}.".format(
                id=self.unique_id,
                name=self.output_name,
                timeoff=timestamp)
            self.logger.debug(msg)

            # Write output amount to database
            if self.output_time_turned_on is not None or self.output_on_duration:
                duration_sec = None
                timestamp = None

                if self.output_on_duration:
                    remaining_time = 0
                    if self.output_on_until > current_time:
                        remaining_time = (self.output_on_until - current_time).total_seconds()
                    duration_sec = (abs(self.output_last_duration) - remaining_time)
                    timestamp = (datetime.datetime.utcnow() - datetime.timedelta(seconds=duration_sec))

                    # Store negative amount if a negative amount is received
                    if self.output_last_duration < 0:
                        duration_sec = -duration_sec

                    self.output_on_duration = False
                    self.output_on_until = current_time

                if self.output_time_turned_on is not None:
                    # Write the amount the output was ON to the database
                    # at the timestamp it turned ON
                    duration_sec = (current_time - self.output_time_turned_on).total_seconds()
                    timestamp = datetime.datetime.utcnow() - datetime.timedelta(seconds=duration_sec)
                    self.output_time_turned_on = None

                write_db = threading.Thread(
                    target=write_influxdb_value,
                    args=(self.unique_id,
                          's',
                          duration_sec,),
                    kwargs={'measure': 'duration_time',
                            'channel': 0,
                            'timestamp': timestamp})
                write_db.start()

            self.output_off_triggered = False

        if trigger_conditionals:
            self.check_triggers(amount=amount)

        return 0, msg

    def check_triggers(self, amount=None):
        """
        This function is executed whenever an output is turned on or off
        It is responsible for executing Output Triggers
        """
        #
        # Check On/Off Outputs
        #
        trigger_output = db_retrieve_table_daemon(Trigger)
        trigger_output = trigger_output.filter(Trigger.trigger_type == 'trigger_output')
        trigger_output = trigger_output.filter(Trigger.unique_id_1 == self.unique_id)
        trigger_output = trigger_output.filter(Trigger.is_activated == True)

        # Find any Output Triggers with the output_id of the output that
        # just changed its state
        if self.is_on():
            trigger_output = trigger_output.filter(
                or_(Trigger.output_state == 'on_duration_none',
                    Trigger.output_state == 'on_duration_any',
                    Trigger.output_state == 'on_duration_none_any',
                    Trigger.output_state == 'on_duration_equal',
                    Trigger.output_state == 'on_duration_greater_than',
                    Trigger.output_state == 'on_duration_equal_greater_than',
                    Trigger.output_state == 'on_duration_less_than',
                    Trigger.output_state == 'on_duration_equal_less_than'))

            on_duration_none = and_(
                Trigger.output_state == 'on_duration_none',
                amount == 0.0)

            on_duration_any = and_(
                Trigger.output_state == 'on_duration_any',
                bool(amount))

            on_duration_none_any = Trigger.output_state == 'on_duration_none_any'

            on_duration_equal = and_(
                Trigger.output_state == 'on_duration_equal',
                Trigger.output_duration == amount)

            on_duration_greater_than = and_(
                Trigger.output_state == 'on_duration_greater_than',
                amount > Trigger.output_duration)

            on_duration_equal_greater_than = and_(
                Trigger.output_state == 'on_duration_equal_greater_than',
                amount >= Trigger.output_duration)

            on_duration_less_than = and_(
                Trigger.output_state == 'on_duration_less_than',
                amount < Trigger.output_duration)

            on_duration_equal_less_than = and_(
                Trigger.output_state == 'on_duration_equal_less_than',
                amount <= Trigger.output_duration)

            trigger_output = trigger_output.filter(
                or_(on_duration_none,
                    on_duration_any,
                    on_duration_none_any,
                    on_duration_equal,
                    on_duration_greater_than,
                    on_duration_equal_greater_than,
                    on_duration_less_than,
                    on_duration_equal_less_than))
        else:
            trigger_output = trigger_output.filter(
                Trigger.output_state == 'off')

        # Execute the Trigger Actions for each Output Trigger
        # for this particular Output device
        for each_trigger in trigger_output.all():
            timestamp = datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')
            message = "{ts}\n[Trigger {cid} ({cname})] Output {oid} ({name}) {state}".format(
                ts=timestamp,
                cid=each_trigger.unique_id.split('-')[0],
                cname=each_trigger.name,
                name=each_trigger.name,
                oid=self.unique_id,
                state=each_trigger.output_state)

            self.control.trigger_all_actions(
                each_trigger.unique_id, message=message)

        #
        # Check PWM Outputs
        #
        trigger_output_pwm = db_retrieve_table_daemon(Trigger)
        trigger_output_pwm = trigger_output_pwm.filter(Trigger.trigger_type == 'trigger_output_pwm')
        trigger_output_pwm = trigger_output_pwm.filter(Trigger.unique_id_1 == self.unique_id)
        trigger_output_pwm = trigger_output_pwm.filter(Trigger.is_activated == True)

        # Execute the Trigger Actions for each Output Trigger
        # for this particular Output device
        for each_trigger in trigger_output_pwm.all():
            trigger_trigger = False
            duty_cycle = self.output_state()

            if duty_cycle == 'off':
                if (
                        (each_trigger.output_state == 'equal' and
                         each_trigger.output_duty_cycle == 0) or
                        (each_trigger.output_state == 'below' and
                         each_trigger.output_duty_cycle != 0)
                        ):
                    trigger_trigger = True
            elif (
                    (each_trigger.output_state == 'above' and
                     duty_cycle > each_trigger.output_duty_cycle) or
                    (each_trigger.output_state == 'below' and
                     duty_cycle < each_trigger.output_duty_cycle) or
                    (each_trigger.output_state == 'equal' and
                     duty_cycle == each_trigger.output_duty_cycle)
                    ):
                trigger_trigger = True

            if not trigger_trigger:
                continue

            timestamp = datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')
            message = "{ts}\n[Trigger {cid} ({cname})] Output {oid} " \
                      "({name}) Duty Cycle {actual_dc} {state} {duty_cycle}".format(
                        ts=timestamp,
                        cid=each_trigger.unique_id.split('-')[0],
                        cname=each_trigger.name,
                        name=each_trigger.name,
                        oid=self.unique_id,
                        actual_dc=duty_cycle,
                        state=each_trigger.output_state,
                        duty_cycle=each_trigger.output_duty_cycle)

            # Check triggers whenever an output is manipulated
            self.control.trigger_all_actions(each_trigger.unique_id, message=message)

    def output_sec_currently_on(self):
        """ Return how many seconds an output has been currently on for """
        if not self.is_on():
            return 0
        else:
            now = datetime.datetime.now()
            sec_currently_on = 0
            if self.output_on_duration:
                left = 0
                if self.output_on_until > now:
                    left = (self.output_on_until - now).total_seconds()
                sec_currently_on = abs(self.output_last_duration) - left
            elif self.output_time_turned_on:
                sec_currently_on = (now - self.output_time_turned_on).total_seconds()
            return sec_currently_on
