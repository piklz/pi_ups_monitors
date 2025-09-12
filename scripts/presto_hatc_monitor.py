#!/usr/bin/env python3
#
#             _   _   ___ _____
#            | | | | / _ \_   _|
#            | |_| |/ /_\ \| |  ___
#            |  _  ||  _  || | / __|
#            | | | || | | || |  (__
# waveshares \_| |_/\_| |_/\_/ \___| UPS for pizero (https://www.waveshare.com/ups-hat-c.htm)
# -----------------------------------------------
# Presto UPS Monitor Script
# Version: 1.4.24
# Author: piklz
# GitHub: https://github.com/piklz/pi_ups_monitor
# Description:
#   This script monitors the Presto UPS HAT on a Raspberry Pi Zero using the INA219
#   sensor, providing power, voltage, current, and battery percentage readings. It
#   sends notifications via ntfy for power events and can be installed as a systemd
#   service for continuous monitoring. Logs are sent to systemd-journald using
#   systemd-cat, with rotation managed by journald (configure in /etc/systemd/journald.conf).
#   Sampling occurs every 5 seconds asynchronously, with logging every 10 seconds.
#   Persistent journal storage (/var/log/journal/) is recommended, ideally with log2ram
#   to reduce SD card wear.
#
# Changelog:
#   Version 1.4.24 (2025-09-12):
#   - Fixed a race condition bug where rapid plugging/unplugging caused multiple
#     simultaneous state change logs. A 5-second debounce delay has been added to the
#     power state detection logic for stability.
#
#   Version 1.4.23 (2025-09-11):
#   - Fixed an OSError that occurred when running as a systemd service by removing
#     the call to os.getlogin(). This function fails in non-interactive environments.
#   - Added `User=pi` and `WorkingDirectory=/home/pi` to the systemd service file
#     template for improved stability and security.

# usage examples:
#   - default live view monitoring run (no notifications)in terminal:
#       sudo ~/pi_ups-monitors/presto_hatc_monitor.py  
#     
#   - To RUN LIVE interminal directly with ntfy notifications enabled and your custom topic:
#       sudo ~/pi_ups-monitors/presto_hatc_monitor.py --enable-ntfy --ntfy-topic YOUR-TOPIC-NAME     #eg PIZERO_SERVER_LIVINGROOM
#
#   - To INSTALL as a systemd service with your custom topicname (requires root):
#       sudo ~/pi_ups-monitors/presto_hatc_monitor.py --install_as_service --enable-ntfy  --ntfy-topic YOUR-TOPIC-NAME
#
#   - To UNINSTALL the systemd service (requires root):
#       sudo ~/pi_ups-monitors/presto_hatc_monitor.py --uninstall 
#
#   - One shot test notification (requires --enable-ntfy/-ntfy) using shortened args:
#       sudo ~/pi_ups-monitors/presto_hatc_monitor.py -ntfy -nt PIZERO_HATC_TEST -t
# -----------------------------------------------

# Standard library imports
import sys
import os
import argparse
import time
import socket
import subprocess
import threading
import shutil
from datetime import datetime, timedelta
from queue import Queue

# Third-party imports, may not be available on all systems
try:
    import smbus
    bus = smbus.SMBus(1)
except ImportError:
    smbus = None
    bus = None

try:
    import requests
except ImportError:
    requests = None

# Constants
VERSION = "1.4.24"
I2C_ADDRESS = 0x43
SERVICE_NAME = "presto_ups.service"
SERVICE_TEMPLATE = """
[Unit]
Description=Presto UPS HAT Monitor Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi
ExecStart=/usr/bin/python3 {script_path} --enable-ntfy --ntfy-server {ntfy_server} --ntfy-topic {ntfy_topic} --power_threshold {power_threshold} --percent_threshold {percent_threshold} --critical_low_threshold {critical_low_threshold} --critical_shutdown_delay {critical_shutdown_delay} --battery_capacity_mah {battery_capacity_mah} --ntfy_cooldown_seconds {ntfy_cooldown_seconds}
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
"""
SERVICE_FILE_PATH = f"/etc/systemd/system/{SERVICE_NAME}"

# INA219 Registers and Configuration (from original script)
_REG_CONFIG                 = 0x00
_REG_SHUNTVOLTAGE           = 0x01
_REG_BUSVOLTAGE             = 0x02
_REG_POWER                  = 0x03
_REG_CURRENT                = 0x04
_REG_CALIBRATION            = 0x05
R_SHUNT                     = 0.1  # Ohms (shunt resistor value on the INA219 board) https://www.waveshare.com/ups-hat-c.htm 

# Configuration thresholds (can be changed via command-line arguments)
POWER_THRESHOLD = 0.5  # Minimum power (Watts) to consider as plugged in (0.5W is very low, but the UPS HAT draws ~0.3W when idle)
PERCENT_THRESHOLD = 10  # Battery percentage threshold for low battery alert (10% is a common threshold)
CRITICAL_LOW_THRESHOLD = 5  # Battery percentage for critical low alert (5% is very low, but gives some buffer for a shutdown)
CRITICAL_SHUTDOWN_DELAY = 60  # Delay (seconds) before shutdown on critical battery level
VOLTAGE_THRESHOLD_PLUGGED_IN = 4.1  # Voltage threshold to detect external power (4.1V is a good threshold to distinguish between battery and external power)
CURRENT_THRESHOLD_CHARGING = 100  # Minimum current (mA) to detect charging (100mA is a good threshold to distinguish between charging and idle)
CURRENT_THRESHOLD_DISCHARGING = -100  # Current (mA) threshold to detect discharging (-100mA is a good threshold to distinguish between discharging and idle)
NTFY_COOLDOWN_SECONDS = 120  #2 mins Cooldown (seconds) between repeated notifications for the same event    
BATTERY_CAPACITY_MAH = 1000 # Default capacity of the lipo included battery (can be adjusted if you add a bigger lipo battery eg. 3000mah- modify this value and your power wattage/time left will be more accurate)
STATE_CHANGE_DEBOUNCE_SECONDS = 5 # Prevents rapid-fire state changes

def log_message(level, message, exit_on_error=True):
    """
    Logs a message to the terminal and journald with a consistent format.
    """
    script_name = "presto-UPSc-service"
    
    # Check if a log level is provided and add brackets
    if level:
        log_level_str = f"[{script_name}] [{level}]"
    else:
        log_level_str = f"[{script_name}]"
        
    log_line = f"{log_level_str} {message}"
    
    # Use print for all output, as systemd-cat will capture it
    print(log_line)
    
    # Exit on critical error
    if level == "ERROR" and exit_on_error:
        sys.exit(1)

def check_dependencies():
    """
    Checks for essential dependencies and exits if they are not met.
    """
    log_message("INFO", "Checking dependencies...")
    
    # Check for python3
    python_version = sys.version.split()[0]
    log_message("INFO", f"Python3 is installed: Python {python_version}")

    # Check for requests library
    if requests is None:
        log_message("ERROR", "python3-requests is not installed. Please install it with 'sudo apt install python3-requests'")

    # Check for smbus
    if smbus is None:
        log_message("ERROR", "python3-smbus is not installed. Please install it with 'sudo apt install python3-smbus'")

    # Check if smbus is functional
    try:
        bus.read_byte(I2C_ADDRESS)
        log_message("INFO", "smbus module is functional")
    except Exception as e:
        log_message("ERROR", f"Failed to communicate with I2C bus at address {hex(I2C_ADDRESS)}. Check your hardware connections and make sure I2C is enabled with 'sudo raspi-config'. Error: {e}")

    # Check for libraspberrypi-bin
    try:
        subprocess.run(["vcgencmd", "version"], check=True, capture_output=True)
        log_message("INFO", "libraspberrypi-bin is installed")
    except FileNotFoundError:
        log_message("ERROR", "libraspberrypi-bin is not installed. Please install it with 'sudo apt install libraspberrypi-bin'")

class Monitor:
    """
    Class to manage all monitoring functions.
    """
    def __init__(self, enable_ntfy=False, ntfy_server=None, ntfy_topic=None, power_threshold=POWER_THRESHOLD, percent_threshold=PERCENT_THRESHOLD, critical_low_threshold=CRITICAL_LOW_THRESHOLD, critical_shutdown_delay=CRITICAL_SHUTDOWN_DELAY, battery_capacity_mah=BATTERY_CAPACITY_MAH, ntfy_cooldown_seconds=NTFY_COOLDOWN_SECONDS):
        self.enable_ntfy = enable_ntfy
        self.ntfy_server = ntfy_server
        self.ntfy_topic = ntfy_topic
        self.power_threshold = power_threshold
        self.percent_threshold = percent_threshold
        self.critical_low_threshold = critical_low_threshold
        self.critical_shutdown_delay = critical_shutdown_delay
        self.battery_capacity_mah = battery_capacity_mah
        self.ntfy_cooldown_seconds = ntfy_cooldown_seconds

        self.is_unplugged = False
        self.last_power_state_change_time = time.time() # New debounce variable
        self.low_power_notified = False
        self.low_percent_notified = False
        self.critical_low_timer_started = False
        self.critical_shutdown_timer_start_time = None
        self.last_ntfy_notification_time = 0
        self.unplugged_start_time = None

        self.ntfy_notification_queue = Queue()
        self.power_readings = []
        self.current_readings = []

        # Configure INA219 using original script's methods
        self.set_calibration_16V_5A()

    def read(self, address):
        """Helper function to read from I2C bus (from original script)."""
        data = bus.read_i2c_block_data(I2C_ADDRESS, address, 2)
        value = (data[0] * 256) + data[1]
        return value

    def write(self, address, data):
        """Helper function to write to I2C bus (from original script)."""
        temp = [0, 0]
        temp[1] = data & 0xFF
        temp[0] = (data & 0xFF00) >> 8
        bus.write_i2c_block_data(I2C_ADDRESS, address, temp)

    def set_calibration_16V_5A(self):
        """Sets INA219 calibration register using values from original script."""
        self._current_lsb = 0.1524
        self._cal_value = 26868
        self._power_lsb = 0.003048
        self.write(_REG_CALIBRATION, self._cal_value)
        config_value = 0x4127
        self.write(_REG_CONFIG, config_value)

    def getBusVoltage_V(self):
        """Returns the bus voltage in Volts (from original script)."""
        self.write(_REG_CALIBRATION, self._cal_value)
        self.read(_REG_BUSVOLTAGE)
        return (self.read(_REG_BUSVOLTAGE) >> 3) * 0.004

    def getCurrent_mA(self):
        """Returns the current in milliamps (from original script)."""
        value = self.read(_REG_CURRENT)
        if value > 32767:
            value -= 65535
        return value * self._current_lsb

    def getPower_W(self):
        """Returns the power in Watts (from original script)."""
        self.write(_REG_CALIBRATION, self._cal_value)
        value = self.read(_REG_POWER)
        if value > 32767:
            value -= 65535
        return value * self._power_lsb

    def get_percent(self, voltage):
        """
        Calculates battery percentage based on voltage.
        Note: This is an estimation and may not be perfectly accurate.
        """
        if voltage > 4.18:
            return 100.0
        elif voltage > 4.15:
            return 99.0
        elif voltage > 4.10:
            return 95.0
        elif voltage > 4.05:
            return 90.0
        elif voltage > 3.98:
            return 80.0
        elif voltage > 3.90:
            return 70.0
        elif voltage > 3.82:
            return 60.0
        elif voltage > 3.75:
            return 50.0
        elif voltage > 3.68:
            return 40.0
        elif voltage > 3.60:
            return 30.0
        elif voltage > 3.52:
            return 20.0
        elif voltage > 3.45:
            return 10.0
        else:
            # A more granular calculation for the final percentage range
            # based on the voltage equivalent of 5% and the safe shutdown voltage
            # For example, mapping 3.45V to 10% and 3.3V to 0%
            if voltage > 3.42:
                return 5.0
            elif voltage > 3.39:
                return 4.0
            elif voltage > 3.36:
                return 3.0
            elif voltage > 3.33:
                return 2.0
            elif voltage > 3.30:
                return 1.0
            else:
                return 0.0
    
    def get_hostname(self):
        """Returns the device hostname."""
        return socket.gethostname()

    def get_ip_address(self):
        """Returns the device's IP address on the network."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except:
            return "Unknown"

    def get_uptime(self):
        """Returns the system uptime in days, hours, and minutes."""
        try:
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.readline().split()[0])
            uptime_timedelta = timedelta(seconds=uptime_seconds)
            days = uptime_timedelta.days
            hours, remainder = divmod(uptime_timedelta.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            return f"{days}d {hours}h {minutes}m"
        except FileNotFoundError:
            return "Unknown"

    def get_ram_info(self):
        """Returns free RAM in MB."""
        try:
            with open('/proc/meminfo', 'r') as f:
                mem_info = f.read()
            for line in mem_info.splitlines():
                if 'MemFree:' in line:
                    free_mem_kb = int(line.split()[1])
                    return f"{free_mem_kb // 1024} MB"
        except FileNotFoundError:
            return "Unknown"
    
    def get_cpu_temp(self):
        """Returns CPU temperature in Celsius."""
        try:
            temp_output = subprocess.check_output(["vcgencmd", "measure_temp"]).decode()
            return float(temp_output.split('=')[1].split("'")[0])
        except (subprocess.CalledProcessError, IndexError, ValueError):
            return None

    def get_gpu_temp(self):
        """Returns GPU temperature in Celsius."""
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                temp_raw = f.read()
            return int(temp_raw) / 1000.0
        except (IOError, ValueError):
            return None
    
    def get_time_on_battery(self):
        """Returns formatted string of time on battery, or None if plugged in."""
        if self.unplugged_start_time is None:
            return None
        
        duration_seconds = time.time() - self.unplugged_start_time
        duration_timedelta = timedelta(seconds=duration_seconds)
        days = duration_timedelta.days
        hours, remainder = divmod(duration_timedelta.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        
        return f"{days}d {hours}h {minutes}m"

    def get_estimated_time_remaining(self, percent, current_mA):
        """Estimates time remaining on battery based on current draw."""
        if current_mA >= 0:
            return None # Not discharging
        
        mAh_remaining = self.battery_capacity_mah * (percent / 100)
        discharge_current_mA = abs(current_mA)
        
        if discharge_current_mA == 0:
            return None # Avoid division by zero
        
        hours_remaining = mAh_remaining / discharge_current_mA
        
        # Convert to hours, minutes format
        minutes_remaining = int(hours_remaining * 60)
        hours = minutes_remaining // 60
        minutes = minutes_remaining % 60
        
        return f"{hours}h {minutes}m"
        
    def send_ntfy_notification(self, event_type, power, percent, current_mA):
        """Sends an ntfy notification if enabled and not on cooldown."""
        if not self.enable_ntfy or requests is None:
            log_message("INFO", f"ntfy notification ({event_type}) skipped: ntfy disabled")
            return
            
        # Implement cooldown unless it's a critical event or a state change
        current_time = time.time()
        is_cooldown_event = event_type in ["low_power", "low_percent"]
        if is_cooldown_event and (current_time - self.last_ntfy_notification_time) < self.ntfy_cooldown_seconds:
            log_message("INFO", f"ntfy notification ({event_type}) skipped: on cooldown", exit_on_error=False)
            return
            
        try:
            hostname = self.get_hostname()
            time_on_battery_str = self.get_time_on_battery()
            time_remaining_str = self.get_estimated_time_remaining(percent, current_mA)
            
            # Additional info for the messages
            battery_info = f"Time on Battery: {time_on_battery_str}" if time_on_battery_str else "Time on Battery: N/A"
            eta_info = f"ETA: {time_remaining_str}" if time_remaining_str else "ETA: N/A"

            # Prepare notification payload based on event type
            if event_type == "unplugged":
                message = f"🔌 Power Unplugged on {hostname}. Running on Battery! Current: {current_mA/1000:.2f}A, Power: {power:.2f}W, Battery: {percent:.1f}%. {eta_info}"
                title = "Power Unplugged"
                priority = 4
                tags = "unplugged"
            elif event_type == "reconnected":
                message = f"✅ Power Reconnected on {hostname}. Battery is charging. {battery_info}"
                title = "Power Reconnected"
                priority = 3
                tags = "reconnected"
            elif event_type == "low_power":
                message = f"🪫 Low Power Alert on {hostname}! Power draw is below {self.power_threshold}W. Current: {current_mA/1000:.2f}A, Power: {power:.2f}W, Battery: {percent:.1f}%. {eta_info}"
                title = "Low Power Alert"
                priority = 4
                tags = "low_power,warning"
            elif event_type == "low_percent":
                message = f"🪫 Low Battery Alert on {hostname}! Battery is at {percent:.1f}%. Current: {current_mA/1000:.2f}A, Power: {power:.2f}W. {eta_info}"
                title = "Low Battery Alert"
                priority = 5
                tags = "low_battery,warning"
            elif event_type == "critical_low":
                message = f"🔴 CRITICAL LOW BATTERY on {hostname}! Battery at {percent:.1f}%. Shutdown in {self.critical_shutdown_delay} seconds if not reconnected. {eta_info}"
                title = "CRITICAL LOW BATTERY"
                priority = 5
                tags = "critical_low,warning"
            elif event_type == "test":
                message = f"This is a simple test notification from {hostname}."
                title = "Test Notification"
                priority = 3
                tags = "test"
            elif event_type == "test_info":
                message = f"✅ Presto UPS Monitor Test Notification from {hostname}\n\n- V:    {self.getBusVoltage_V():.2f} V\n- I:    {self.getCurrent_mA()/1000:.2f} A\n- W:    {self.getPower_W():.2f} W\n- P:    {self.get_percent(self.getBusVoltage_V()):.1f}%\n- Hostname:  {self.get_hostname()}\n- IP Address:  {self.get_ip_address()}\n- Uptime:    {self.get_uptime()}\n- Free RAM:  {self.get_ram_info()}\n- CPU Temp:  {self.get_cpu_temp():.1f} °C"
                title = "Test Notification - Full Report"
                priority = 3
                tags = "test,info"
            else:
                return # Do nothing for unhandled events

            url = f"https://{self.ntfy_server}/{self.ntfy_topic}"
            headers = {
                "Title": title,
                "Priority": str(priority),
                "Tags": tags
            }
            
            requests.post(url, data=message.encode('utf-8'), headers=headers)
            log_message("INFO", f"ntfy notification ({event_type}) sent successfully")
            
            # Update last notification time for cooldown events
            if is_cooldown_event:
                self.last_ntfy_notification_time = current_time

        except Exception as e:
            log_message("ERROR", f"Failed to send ntfy notification for '{event_type}': {e}", exit_on_error=False)

def install_as_service(script_path, args):
    """Installs the script as a systemd service with detailed output."""
    if os.geteuid() != 0:
        log_message("ERROR", "Service installation requires root privileges. Please run with 'sudo'.")

    log_message("INFO", "Starting Presto UPS Monitor service installation.")
    
    # Backup existing service file if it exists and ask for confirmation
    if os.path.exists(SERVICE_FILE_PATH):
        confirmation = input(f"❗ A service file already exists at {SERVICE_FILE_PATH}.\n   Do you want to overwrite it? (y/n): ")
        if confirmation.lower() not in ['y', 'yes']:
            log_message("INFO", "Installation cancelled by user.")
            sys.exit(0)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_path = f"{SERVICE_FILE_PATH}.bak_{timestamp}"
        log_message("INFO", f"Existing service file found. Creating backup at {backup_path}...")
        shutil.copyfile(SERVICE_FILE_PATH, backup_path)
        log_message("INFO", "Backup created.")
    
    service_content = SERVICE_TEMPLATE.format(
        script_path=script_path,
        ntfy_server=args.ntfy_server,
        ntfy_topic=args.ntfy_topic,
        power_threshold=args.power_threshold,
        percent_threshold=args.percent_threshold,
        critical_low_threshold=args.critical_low_threshold,
        critical_shutdown_delay=args.critical_shutdown_delay,
        battery_capacity_mah=args.battery_capacity_mah,
        ntfy_cooldown_seconds=args.ntfy_cooldown_seconds
    )
    
    try:
        log_message("INFO", f"Creating new service file at {SERVICE_FILE_PATH}...")
        with open(SERVICE_FILE_PATH, "w") as f:
            f.write(service_content)
        log_message("INFO", "Service file created successfully.")

        # Show the user the new file content
        log_message("INFO", "--- NEW SERVICE FILE CONTENT ---")
        print(service_content.strip())
        log_message("INFO", "--------------------------------")

        log_message("INFO", "Reloading systemd daemon...")
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
        log_message("INFO", "Daemon reloaded.")

        log_message("INFO", f"Enabling {SERVICE_NAME} to run on boot...")
        subprocess.run(["sudo", "systemctl", "enable", SERVICE_NAME], check=True)
        log_message("INFO", f"{SERVICE_NAME} enabled.")
        
        log_message("INFO", f"Starting {SERVICE_NAME}...")
        subprocess.run(["sudo", "systemctl", "start", SERVICE_NAME], check=True)
        log_message("INFO", f"{SERVICE_NAME} started.")

        # Final status check
        log_message("INFO", f"Checking service status...")
        status_check = subprocess.run(["sudo", "systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True)
        if status_check.returncode == 0:
            log_message("SUCCESS", "✅ Installation complete. The service is now active and running.")
        else:
            log_message("ERROR", "Installation finished, but the service is not active. Please check with 'sudo systemctl status presto_ups.service'")
            log_message("ERROR", f"Output: {status_check.stdout.strip()}")
            log_message("ERROR", f"Error: {status_check.stderr.strip()}")
            sys.exit(1)
            
    except subprocess.CalledProcessError as e:
        log_message("ERROR", f"Failed to install service. A command returned an error: {e}", exit_on_error=True)
    except Exception as e:
        log_message("ERROR", f"An unexpected error occurred during service installation: {e}", exit_on_error=True)

def uninstall_service():
    """Uninstalls the systemd service with a more robust error handling."""
    if os.geteuid() != 0:
        log_message("ERROR", "Service uninstallation requires root privileges. Please run with 'sudo'.")

    log_message("INFO", "Starting Presto UPS Monitor service uninstallation.")

    try:
        # Stop the service (do not crash if it's already stopped)
        log_message("INFO", f"Stopping {SERVICE_NAME}...")
        stop_result = subprocess.run(["sudo", "systemctl", "stop", SERVICE_NAME], check=False)
        if stop_result.returncode == 0:
            log_message("INFO", f"{SERVICE_NAME} stopped successfully.")
        elif stop_result.returncode == 5:
            log_message("WARNING", f"Service {SERVICE_NAME} was not loaded. Skipping stop command.", exit_on_error=False)
        else:
            log_message("ERROR", f"Failed to stop {SERVICE_NAME}. Exit code: {stop_result.returncode}", exit_on_error=True)

        # Disable the service (do not crash if it's already disabled)
        log_message("INFO", f"Disabling {SERVICE_NAME}...")
        disable_result = subprocess.run(["sudo", "systemctl", "disable", SERVICE_NAME], check=False)
        if disable_result.returncode == 0:
            log_message("INFO", f"{SERVICE_NAME} disabled successfully.")
        elif disable_result.returncode == 1:
            log_message("WARNING", f"Service {SERVICE_NAME} was already disabled. Skipping disable command.", exit_on_error=False)
        else:
            log_message("ERROR", f"Failed to disable {SERVICE_NAME}. Exit code: {disable_result.returncode}", exit_on_error=True)

        log_message("INFO", f"Removing service file {SERVICE_FILE_PATH}...")
        os.remove(SERVICE_FILE_PATH)
        log_message("INFO", "Service file removed.")
        
        log_message("INFO", "Reloading systemd daemon...")
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
        log_message("INFO", "Daemon reloaded.")

        log_message("SUCCESS", "✅ Service uninstalled successfully.")
        
    except FileNotFoundError:
        log_message("WARNING", f"Service file not found at {SERVICE_FILE_PATH}. Was it already removed?", exit_on_error=False)
        log_message("SUCCESS", "✅ Uninstallation complete (service file was already gone).")
    except subprocess.CalledProcessError as e:
        log_message("ERROR", f"Failed to uninstall service. A command returned an error: {e}", exit_on_error=True)
    except Exception as e:
        log_message("ERROR", f"An unexpected error occurred during service uninstallation: {e}", exit_on_error=True)

def sample_ina219(monitor, data_queue, data_lock):
    """Samples the INA219 sensor in a separate thread."""
    while True:
        try:
            current = monitor.getCurrent_mA()
            power = monitor.getPower_W()
            bus_voltage = monitor.getBusVoltage_V()
            percent = monitor.get_percent(bus_voltage)

            current_time = time.time()
            time_since_last_change = current_time - monitor.last_power_state_change_time

            # The new, robust power state logic
            if not monitor.is_unplugged:
                # We are currently in "plugged in" state. Look for a clear sign of unplugging.
                if bus_voltage < VOLTAGE_THRESHOLD_PLUGGED_IN and current < CURRENT_THRESHOLD_DISCHARGING and time_since_last_change > STATE_CHANGE_DEBOUNCE_SECONDS:
                    monitor.send_ntfy_notification("unplugged", power, percent, current)
                    monitor.unplugged_start_time = time.time()
                    log_message("INFO", "🔌 Power Unplugged - Running on Battery")
                    monitor.is_unplugged = True
                    monitor.low_power_notified = False
                    monitor.low_percent_notified = False
                    monitor.critical_low_timer_started = False
                    monitor.last_power_state_change_time = current_time
            else:
                # We are currently in "unplugged" state. Look for a clear sign of reconnecting.
                if (bus_voltage > VOLTAGE_THRESHOLD_PLUGGED_IN and current > CURRENT_THRESHOLD_CHARGING) and time_since_last_change > STATE_CHANGE_DEBOUNCE_SECONDS:
                    time_on_battery_str = monitor.get_time_on_battery()
                    log_message("INFO", f"✅ Power Reconnected - Battery ran for {time_on_battery_str}")
                    monitor.send_ntfy_notification("reconnected", power, percent, current)
                    monitor.is_unplugged = False
                    monitor.low_power_notified = False
                    monitor.low_percent_notified = False
                    monitor.critical_low_timer_started = False
                    monitor.unplugged_start_time = None
                    monitor.last_power_state_change_time = current_time
                elif (bus_voltage > VOLTAGE_THRESHOLD_PLUGGED_IN and current < CURRENT_THRESHOLD_DISCHARGING) and time_since_last_change > STATE_CHANGE_DEBOUNCE_SECONDS:
                    # This is the tricky case: power is back on, but battery is still discharging (possibly due to high load).
                    time_on_battery_str = monitor.get_time_on_battery()
                    log_message("INFO", f"✅ Power Reconnected - System on external power, battery may be full or under high load. Battery ran for {time_on_battery_str}")
                    monitor.send_ntfy_notification("reconnected", power, percent, current)
                    monitor.is_unplugged = False
                    monitor.low_power_notified = False
                    monitor.low_percent_notified = False
                    monitor.critical_low_timer_started = False
                    monitor.unplugged_start_time = None
                    monitor.last_power_state_change_time = current_time
            
            # Check for low power and low percent
            if monitor.is_unplugged:
                if power < monitor.power_threshold and not monitor.low_power_notified:
                    monitor.send_ntfy_notification("low_power", power, percent, current)
                    log_message("WARNING", "🪫 Low Power Alert! Check your power source.", exit_on_error=False)
                    monitor.low_power_notified = True
                
                if percent < monitor.percent_threshold and not monitor.low_percent_notified:
                    monitor.send_ntfy_notification("low_percent", power, percent, current)
                    log_message("WARNING", f"🪫 Low Battery Alert! Battery at {percent:.1f}%.", exit_on_error=False)
                    monitor.low_percent_notified = True

                # New Critical Low Battery Logic
                if percent < monitor.critical_low_threshold:
                    if not monitor.critical_low_timer_started:
                        monitor.send_ntfy_notification("critical_low", power, percent, current)
                        log_message("CRITICAL", f"🔴 CRITICAL LOW BATTERY! Battery at {percent:.1f}%. System will shutdown in {monitor.critical_shutdown_delay}s.", exit_on_error=False)
                        monitor.critical_low_timer_started = True
                        monitor.critical_shutdown_timer_start_time = time.time()
                    
                    if time.time() - monitor.critical_shutdown_timer_start_time > monitor.critical_shutdown_delay:
                        log_message("CRITICAL", "🔴 CRITICAL: Shutdown initiated. Goodbye!", exit_on_error=False)
                        subprocess.run(["sudo", "shutdown", "-h", "now"])
                        
            
            # Put the latest data into the queue for the main loop
            data = {
                'bus_voltage': bus_voltage,
                'current': current,
                'power': power,
                'percent': percent
            }
            with data_lock:
                while not data_queue.empty():
                    data_queue.get_nowait()
                data_queue.put(data)
            
        except Exception as e:
            log_message("ERROR", f"Error in sampling thread: {e}", exit_on_error=False)
        time.sleep(5)

def main():
    """
    Main function to parse arguments and run the monitoring loop.
    """
    parser = argparse.ArgumentParser(
        description=f"Presto UPS Monitor Script (Version {VERSION})",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Useful journalctl commands for monitoring:
  - View real-time logs: journalctl -u presto_ups.service -f
  - View last 50 logs: journalctl -u presto_ups.service -n 50
  - Recent voltage/current logs: journalctl -u presto_ups.service | grep -E "Voltage|Current|Power|Percent" -m 10
  - Power event logs: journalctl -u presto_ups.service | grep -E "unplugged|reconnected|Low power|Low percent" -m 10
  - Critical errors: journalctl -u presto_ups.service -p 0..3 -n 10
  - Check service status: systemctl status presto_ups.service
"""
    )
    
    parser.add_argument("-ntfy", "--enable-ntfy", action="store_true", help="Enable ntfy notifications for power events.")
    parser.add_argument("-ns", "--ntfy-server", type=str, default="ntfy.sh", help="The ntfy server address.")
    parser.add_argument("-nt", "--ntfy-topic", type=str, default="pi_ups_monitor", help="The ntfy topic to send notifications to.")
    parser.add_argument("-i", "--install_as_service", action="store_true", help="Install the script as a systemd service to run on boot.")
    parser.add_argument("-u", "--uninstall", action="store_true", help="Uninstall the systemd service.")
    parser.add_argument("-t", "--test-ntfy", action="store_true", help="Send a test ntfy notification and exit.")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")

    # Add new configurable threshold arguments
    parser.add_argument("--power_threshold", type=float, default=POWER_THRESHOLD, help=f"Low power threshold for ntfy notifications. (default: {POWER_THRESHOLD})")
    parser.add_argument("--percent_threshold", type=float, default=PERCENT_THRESHOLD, help=f"Low battery percentage threshold for ntfy notifications. (default: {PERCENT_THRESHOLD})")
    parser.add_argument("--critical_low_threshold", type=float, default=CRITICAL_LOW_THRESHOLD, help=f"Critical battery percentage threshold for shutdown. (default: {CRITICAL_LOW_THRESHOLD})")
    parser.add_argument("--critical_shutdown_delay", type=int, default=CRITICAL_SHUTDOWN_DELAY, help=f"Delay in seconds before shutdown at critical level. (default: {CRITICAL_SHUTDOWN_DELAY})")
    parser.add_argument("--battery_capacity_mah", type=int, default=BATTERY_CAPACITY_MAH, help=f"The capacity of the battery in mAh for ETA calculations. (default: {BATTERY_CAPACITY_MAH})")
    parser.add_argument("--ntfy_cooldown_seconds", type=int, default=NTFY_COOLDOWN_SECONDS, help=f"Cooldown in seconds between repeated low battery notifications. (default: {NTFY_COOLDOWN_SECONDS})")

    args = parser.parse_args()
    
    script_path = os.path.abspath(__file__)

    # Handle service management options first
    if args.install_as_service:
        install_as_service(script_path, args)
        sys.exit(0)
    
    if args.uninstall:
        uninstall_service()
        sys.exit(0)
        
    # Handle test ntfy option
    if args.test_ntfy:
        monitor = Monitor(
            enable_ntfy=True,
            ntfy_server=args.ntfy_server,
            ntfy_topic=args.ntfy_topic,
            power_threshold=args.power_threshold,
            percent_threshold=args.percent_threshold,
            critical_low_threshold=args.critical_low_threshold,
            critical_shutdown_delay=args.critical_shutdown_delay,
            battery_capacity_mah=args.battery_capacity_mah,
            ntfy_cooldown_seconds=args.ntfy_cooldown_seconds
        )
        monitor.send_ntfy_notification("test_info", 0, 0, 0)
        sys.exit(0)

    # If no service options are selected, run the main monitoring loop
    check_dependencies()
    
    log_message("INFO", f"Starting Presto UPS Monitor Script v{VERSION}...")
    log_message("INFO", f"I2C Address: {hex(I2C_ADDRESS)}")

    monitor = Monitor(
        enable_ntfy=args.enable_ntfy,
        ntfy_server=args.ntfy_server,
        ntfy_topic=args.ntfy_topic,
        power_threshold=args.power_threshold,
        percent_threshold=args.percent_threshold,
        critical_low_threshold=args.critical_low_threshold,
        critical_shutdown_delay=args.critical_shutdown_delay,
        battery_capacity_mah=args.battery_capacity_mah,
        ntfy_cooldown_seconds=args.ntfy_cooldown_seconds
    )
    
    # Check initial power state and set the flag correctly BEFORE starting the thread
    initial_voltage = monitor.getBusVoltage_V()
    if initial_voltage < VOLTAGE_THRESHOLD_PLUGGED_IN:
        log_message("INFO", "System started on battery power.")
        monitor.is_unplugged = True
        monitor.unplugged_start_time = time.time()
    else:
        log_message("INFO", "System started on external power.")
        monitor.is_unplugged = False

    data_queue = Queue()
    data_lock = threading.Lock()
    sampling_thread = threading.Thread(target=sample_ina219, args=(monitor, data_queue, data_lock), daemon=True)
    sampling_thread.start()

    last_log_time = datetime.now()
    log_interval = timedelta(seconds=10)

    while True:
        try:
            data = data_queue.get_nowait()
            if datetime.now() - last_log_time >= log_interval:
                time_on_battery_str = monitor.get_time_on_battery()
                time_remaining_str = monitor.get_estimated_time_remaining(data['percent'], data['current'])

                log_message("INFO", "---------------------------------")
                log_message("INFO", f"V:    {data['bus_voltage']:>6.2f} V, I:  {data['current']/1000:>6.2f} A, W:    {data['power']:>6.2f} W, P:    {data['percent']:>6.1f}%")
                
                if time_on_battery_str:
                    log_message("INFO", f"Time on Battery: {time_on_battery_str}")
                if time_remaining_str:
                    log_message("INFO", f"Time Remaining:  {time_remaining_str}")

                log_message("INFO", "System Info:")
                log_message("INFO", f"Hostname:    {monitor.get_hostname()}")
                log_message("INFO", f"IP Address:  {monitor.get_ip_address()}")
                log_message("INFO", f"Uptime:      {monitor.get_uptime()}")
                log_message("INFO", f"Free RAM:    {monitor.get_ram_info()}")
                cpu_temp = monitor.get_cpu_temp()
                gpu_temp = monitor.get_gpu_temp()
                log_message("INFO", f"CPU Temp:    {cpu_temp:>6.1f} °C" if cpu_temp else "CPU Temp:    Unknown")
                log_message("INFO", f"GPU Temp:    {gpu_temp:>6.1f} °C" if gpu_temp else "GPU Temp:    Unknown")
                log_message("INFO", "---------------------------------")
                last_log_time = datetime.now()
        except Exception as e:
            time.sleep(1)

if __name__ == "__main__":
    main()