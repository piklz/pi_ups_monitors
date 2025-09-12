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
# Version: 1.5.4
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
#   Version 1.5.4 (2025-09-12):
#   - Added a new "test_info" notification type that provides a detailed status report
#   Version 1.5.3 (2025-09-12):
#   - Corrected state change detection in the continuous loop to use current (mA)
#     instead of power (W), which was causing false positive 'plugged in' readings
#     and preventing unplug/reconnect notifications from being sent.
#   Version 1.5.2 (2025-09-12):
#   - Corrected the initial status check to use the current (mA) value instead
#     of the power (W) value for a more accurate determination of the power state
#     on startup. This prevents false "plugged in" reports when the device is
#     actually running on battery.
#
#
# -----------------------------------------------
# usage examples:
#   - default live view monitoring run (no notifications)in terminal:
#       python3 presto_hatc_monitor.py
#
#   - To RUN LIVE interminal directly with ntfy notifications enabled and your custom topic:
#       python3 presto_hatc_monitor.py --enable-ntfy --ntfy-topic YOUR-TOPIC-NAME
#
#   - To INSTALL as a systemd service with your custom topicname (requires root):
#       sudo python3 presto_hatc_monitor.py --install_as_service --enable-ntfy --ntfy-topic YOUR-TOPIC-NAME
#
#   - To UNINSTALL the systemd service (requires root):
#       sudo python3 presto_hatc_monitor.py --uninstall
#
#   - One shot test notification using shortened args:
#       python3 presto_hatc_monitor.py -ntfy -nt PIZERO_HATC_TEST -t
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
VERSION = "1.5.4"
I2C_ADDRESS = 0x43
SERVICE_NAME = "presto_hatc_monitor.service"
SCRIPT_NAME = "presto_hatc_monitor.py"
INSTALL_PATH = f"/usr/local/bin/{SCRIPT_NAME}"
SERVICE_FILE_PATH = f"/etc/systemd/system/{SERVICE_NAME}"
USER = os.getenv("SUDO_USER") or os.getenv("USER") or os.popen("id -un").read().strip()

# INA219 Registers and Configuration
_REG_CONFIG                 = 0x00
_REG_SHUNTVOLTAGE           = 0x01
_REG_BUSVOLTAGE             = 0x02
_REG_POWER                  = 0x03
_REG_CURRENT                = 0x04
_REG_CALIBRATION            = 0x05
R_SHUNT                     = 0.1  # Ohms (shunt resistor value on the INA219 board)

# Configuration thresholds (can be changed via command-line arguments)
POWER_THRESHOLD = 0.5
PERCENT_THRESHOLD = 10
CRITICAL_LOW_THRESHOLD = 5
CRITICAL_SHUTDOWN_DELAY = 60
VOLTAGE_THRESHOLD_PLUGGED_IN = 4.1
CURRENT_THRESHOLD_CHARGING = 100
CURRENT_THRESHOLD_DISCHARGING = -100
NTFY_COOLDOWN_SECONDS = 120
BATTERY_CAPACITY_MAH = 1000
STATE_CHANGE_DEBOUNCE_SECONDS = 5

def log_message(level, message, exit_on_error=True):
    """
    Logs a message to the terminal and journald with a consistent format.
    """
    script_name = "presto-UPSc-service"
    
    if level:
        log_level_str = f"[{script_name}] [{level}]"
    else:
        log_level_str = f"[{script_name}]"
        
    log_line = f"{log_level_str} {message}"
    
    print(log_line)
    
    if level == "ERROR" and exit_on_error:
        sys.exit(1)

def check_dependencies():
    """
    Checks for essential dependencies and exits if they are not met.
    """
    log_message("INFO", "Checking dependencies...")
    
    python_version = sys.version.split()[0]
    log_message("INFO", f"Python3 is installed: Python {python_version}")

    if requests is None:
        log_message("ERROR", "python3-requests is not installed. Please install it with 'sudo apt install python3-requests'")

    if smbus is None:
        log_message("ERROR", "python3-smbus is not installed. Please install it with 'sudo apt install python3-smbus'")

    try:
        bus.read_byte(I2C_ADDRESS)
        log_message("INFO", "smbus module is functional")
    except Exception as e:
        log_message("ERROR", f"Failed to communicate with I2C bus at address {hex(I2C_ADDRESS)}. Check your hardware connections and make sure I2C is enabled with 'sudo raspi-config'. Error: {e}")

    try:
        subprocess.run(["vcgencmd", "version"], check=True, capture_output=True)
        log_message("INFO", "libraspberrypi-bin is installed")
    except FileNotFoundError:
        log_message("ERROR", "libraspberrypi-bin is not installed. Please install it with 'sudo apt install libraspberrypi-bin'")

# ----------------------------------------------
#  INSTALLATION AND UNINSTALLATION FUNCTIONS
# ----------------------------------------------

def install_as_service(args):
    """
    Installs the script as a systemd service.
    This function must be run with sudo.
    """
    if os.geteuid() != 0:
        log_message("ERROR", "This script must be run with sudo to install the service.")

    # Check if the service file already exists and prompt the user.
    if os.path.exists(SERVICE_FILE_PATH):
        response = input(f"A service file for {SERVICE_NAME} already exists. Do you want to overwrite it? (yes/no): ")
        if response.lower() not in ['yes', 'y']:
            log_message("INFO", "Installation aborted by user.")
            sys.exit(0)

    # Stop and disable the service if it's already running
    print(f"Checking for existing service...")
    os.system(f"sudo systemctl stop {SERVICE_NAME}")
    os.system(f"sudo systemctl disable {SERVICE_NAME}")
    os.system("sudo systemctl daemon-reload")
    
    print("Installing Presto HAT C monitor as a systemd service...")

    # Capture the original command line arguments, but filter out the install flag
    original_args = [arg for arg in sys.argv[1:] if arg not in ['-i', '--install_as_service']]
    full_exec_start = " ".join([INSTALL_PATH] + original_args)

    # Define the service file content
    service_content = f"""[Unit]
Description=Presto UPS HAT Monitor Service
After=network.target

[Service]
Type=simple
User={USER}
WorkingDirectory=/home/{USER}
ExecStart={full_exec_start}
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
"""

    # Copy the script to /usr/local/bin and make it executable
    try:
        script_path = os.path.abspath(__file__)
        shutil.copyfile(script_path, INSTALL_PATH)
        os.system(f"chmod +x {INSTALL_PATH}")
        print(f"Script copied to {INSTALL_PATH}")
    except Exception as e:
        log_message("ERROR", f"Error copying script: {e}")

    # Write the systemd service file
    try:
        with open(SERVICE_FILE_PATH, "w") as f:
            f.write(service_content)
        print(f"Service file created at {SERVICE_FILE_PATH}")
    except Exception as e:
        log_message("ERROR", f"Error creating service file: {e}")

    # Enable and start the service
    print("Enabling and starting the service...")
    os.system("systemctl daemon-reload")
    os.system(f"systemctl enable {SERVICE_NAME}")
    os.system(f"systemctl start {SERVICE_NAME}")
    print("Service installed and started successfully.")
    print(f"You can check its status with: sudo systemctl status {SERVICE_NAME}")

def uninstall_service():
    """
    Uninstalls the systemd service and removes the script.
    This function must be run with sudo.
    """
    if os.geteuid() != 0:
        log_message("ERROR", "This script must be run with sudo to uninstall the service.")

    print(f"Stopping and disabling the service: {SERVICE_NAME}...")
    os.system(f"systemctl stop {SERVICE_NAME}")
    os.system(f"systemctl disable {SERVICE_NAME}")
    os.system("systemctl daemon-reload")

    print("Removing the service file and script...")
    if os.path.exists(SERVICE_FILE_PATH):
        os.remove(SERVICE_FILE_PATH)
    if os.path.exists(INSTALL_PATH):
        os.remove(INSTALL_PATH)
    
    print("Service uninstalled successfully.")

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
        self.last_power_state_change_time = time.time()
        self.low_power_notified = False
        self.low_percent_notified = False
        self.critical_low_timer_started = False
        self.critical_shutdown_timer_start_time = None
        self.last_ntfy_notification_time = 0
        self.unplugged_start_time = None

        self.ntfy_notification_queue = Queue()
        self.power_readings = []
        self.current_readings = []

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
        if voltage > 4.18: return 100.0
        elif voltage > 4.15: return 99.0
        elif voltage > 4.10: return 95.0
        elif voltage > 4.05: return 90.0
        elif voltage > 3.98: return 80.0
        elif voltage > 3.90: return 70.0
        elif voltage > 3.82: return 60.0
        elif voltage > 3.75: return 50.0
        elif voltage > 3.68: return 40.0
        elif voltage > 3.60: return 30.0
        elif voltage > 3.52: return 20.0
        elif voltage > 3.45: return 10.0
        else:
            if voltage > 3.42: return 5.0
            elif voltage > 3.39: return 4.0
            elif voltage > 3.36: return 3.0
            elif voltage > 3.33: return 2.0
            elif voltage > 3.30: return 1.0
            else: return 0.0
    
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
            return None
        
        mAh_remaining = self.battery_capacity_mah * (percent / 100)
        discharge_current_mA = abs(current_mA)
        if discharge_current_mA == 0:
            return None

        hours_remaining = mAh_remaining / discharge_current_mA
        minutes_remaining = int(hours_remaining * 60)
        hours = minutes_remaining // 60
        minutes = minutes_remaining % 60
        return f"{hours}h {minutes}m"

    def send_ntfy_notification(self, event_type, power, percent, current_mA):
        """Sends an ntfy notification if enabled and not on cooldown."""
        if not self.enable_ntfy or requests is None:
            log_message("INFO", f"ntfy notification ({event_type}) skipped: ntfy disabled")
            return

        current_time = time.time()
        is_cooldown_event = event_type in ["low_power", "low_percent"]
        if is_cooldown_event and (current_time - self.last_ntfy_notification_time) < self.ntfy_cooldown_seconds:
            log_message("INFO", f"ntfy notification ({event_type}) skipped: on cooldown", exit_on_error=False)
            return
        
        try:
            hostname = self.get_hostname()
            time_on_battery_str = self.get_time_on_battery()
            time_remaining_str = self.get_estimated_time_remaining(percent, current_mA)

            battery_info = f"Time on Battery: {time_on_battery_str}" if time_on_battery_str else "Time on Battery: N/A"
            eta_info = f"ETA: {time_remaining_str}" if time_remaining_str else "ETA: N/A"

            message = ""
            title = ""
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
            elif event_type == "low_percent":
                message = f"🪫 Low Battery Alert on {hostname}! Battery is at {percent:.1f}%. {eta_info}"
                title = "Low Battery"
                priority = 4
                tags = "low_battery"
            elif event_type == "critical_low":
                # Changed to warn that shutdown is pending, not that it has initiated.
                message = f"🚨 Critical Battery Alert on {hostname}! Battery at {percent:.1f}%. A shutdown will begin in {self.critical_shutdown_delay} seconds."
                title = "Critical Battery"
                priority = 5
                tags = "critical_battery"
            elif event_type == "shutdown":
                # Final notification confirming shutdown is initiated.
                message = f"🔴 Shutdown Initiated on {hostname} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.\n◕︵◕"
                title = "Shutdown Initiated"
                priority = 5
                tags = "shutdown"
            elif event_type == "test":
                message = f"🌟 Test Notification from {hostname}!"
                title = "Test"
                priority = 5
                tags = "test"
            elif event_type == "test_info":
                message = f"✅ Presto UPS Monitor Test Notification from {hostname}\n\n- V:    {self.getBusVoltage_V():.2f} V\n- I:    {self.getCurrent_mA()/1000:.2f} A\n- W:    {self.getPower_W():.2f} W\n- P:    {self.get_percent(self.getBusVoltage_V()):.1f}%\n- Hostname:  {self.get_hostname()}\n- IP Address:  {self.get_ip_address()}\n- Uptime:    {self.get_uptime()}\n- Free RAM:  {self.get_ram_info()}\n- CPU Temp:  {self.get_cpu_temp():.1f} °C\n◕‿◕"
                title = "Test Notification - Full Report"
                priority = 3
                tags = "test,info"
            else:
                return # Do nothing for unhandled events
            
            if message and title:
                response = requests.post(f"{self.ntfy_server}/{self.ntfy_topic}",
                                         data=message.encode('utf-8'),
                                         headers={"Title": title.encode('utf-8'), "Tags": tags, "Priority": str(priority)})
                
                if response.status_code == 200:
                    log_message("INFO", f"Notification sent successfully: {message}")
                    self.last_ntfy_notification_time = current_time
                    if event_type == "low_percent":
                        self.low_percent_notified = True
                else:
                    log_message("WARNING", f"Failed to send notification: HTTP {response.status_code} - {response.text}", exit_on_error=False)

        except requests.exceptions.RequestException as e:
            log_message("WARNING", f"Failed to send notification: Network error - {e}", exit_on_error=False)
        except Exception as e:
            log_message("WARNING", f"Failed to send notification: Unexpected error - {e}", exit_on_error=False)


    def handle_critical_low(self, percent):
        """
        Handles the critical low battery event, initiating a shutdown.
        """
        if not self.critical_low_timer_started:
            self.critical_low_timer_started = True
            self.critical_shutdown_timer_start_time = time.time()
            self.send_ntfy_notification("critical_low", 0, percent, 0)
            log_message("CRITICAL", f"Battery at critical level: {percent:.1f}%. Initiating shutdown in {self.critical_shutdown_delay} seconds.")
            
        time_elapsed = time.time() - self.critical_shutdown_timer_start_time
        
        if time_elapsed >= self.critical_shutdown_delay:
            self.send_ntfy_notification("shutdown", 0, percent, 0)
            log_message("CRITICAL", f"Shutdown initiated. Battery at {percent:.1f}% after {time_elapsed:.2f} seconds.")
            subprocess.run(["sudo", "shutdown", "now"])
            sys.exit(0)

def main():
    """
    Main function to run the monitor logic.
    """
    check_dependencies()
    
    # Instantiate the monitor class
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
    
    # --- INITIAL STATUS CHECK ---
    initial_current_mA = monitor.getCurrent_mA()

    if initial_current_mA > 0:
        log_message("INFO", "Script started: Device is currently plugged in and charging.")
    else:
        log_message("WARNING", "Script started: Device is currently running on battery.")
        # Manually set the state variables since we missed the 'unplugged' event
        monitor.is_unplugged = True
        monitor.unplugged_start_time = time.time()

    # --- END OF INITIAL STATUS CHECK ---
    
    log_counter = 0

    while True:
        try:
            power_status = monitor.getPower_W()
            bus_voltage = monitor.getBusVoltage_V()
            current_mA = monitor.getCurrent_mA()
            percent = monitor.get_percent(bus_voltage)

            # --- CORRECTED STATE-CHANGE LOGIC ---
            # Use current to reliably check if the device is plugged in or not.
            # A positive current means it's charging, negative means discharging.
            is_plugged_in = current_mA > 50 # Using 50mA to prevent noise from triggering a false positive.
            current_time = time.time()

            # Debounce logic for state changes
            if is_plugged_in and monitor.is_unplugged and (current_time - monitor.last_power_state_change_time) > STATE_CHANGE_DEBOUNCE_SECONDS:
                log_message("INFO", "Power reconnected!")
                monitor.send_ntfy_notification("reconnected", power_status, percent, current_mA)
                monitor.is_unplugged = False
                monitor.last_power_state_change_time = current_time
                monitor.unplugged_start_time = None
                monitor.low_power_notified = False
                monitor.low_percent_notified = False
                monitor.critical_low_timer_started = False
            elif not is_plugged_in and not monitor.is_unplugged and (current_time - monitor.last_power_state_change_time) > STATE_CHANGE_DEBOUNCE_SECONDS:
                log_message("WARNING", "Power unplugged!")
                monitor.send_ntfy_notification("unplugged", power_status, percent, current_mA)
                monitor.is_unplugged = True
                monitor.last_power_state_change_time = current_time
                monitor.unplugged_start_time = time.time()
                monitor.low_power_notified = False
                monitor.low_percent_notified = False

            # Low battery alert
            if monitor.is_unplugged and percent < monitor.percent_threshold and not monitor.low_percent_notified:
                monitor.send_ntfy_notification("low_percent", power_status, percent, current_mA)
                monitor.low_percent_notified = True

            # Critical low battery check
            if monitor.is_unplugged and percent < monitor.critical_low_threshold:
                monitor.handle_critical_low(percent)
            else:
                monitor.critical_low_timer_started = False
                monitor.critical_shutdown_timer_start_time = None
                
            # Log every 10 seconds
            if log_counter % 2 == 0:
                time_on_battery_str = monitor.get_time_on_battery()
                time_remaining_str = monitor.get_estimated_time_remaining(percent, current_mA)

                log_message("INFO", "---------------------------------")
                log_message("INFO", f"V: {bus_voltage:>6.2f} V, I: {current_mA/1000:>6.2f} A, W: {power_status:>6.2f} W, P: {percent:>6.1f}%")
                
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

            log_counter += 1

        except Exception as e:
            log_message("ERROR", f"An error occurred: {e}", exit_on_error=True)

        time.sleep(5)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f'Presto HAT C UPS Monitor v{VERSION}',
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="""\
Useful journalctl commands for monitoring:
  - View real-time logs: journalctl -u presto_hatc_monitor.service -f
  - View last 50 logs: journalctl -u presto_hatc_monitor.service -n 50
  - Recent voltage/current logs: journalctl -u presto_hatc_monitor.service | grep -E "Voltage|Current|Power|Percent" -m 10
  - Power event logs: journalctl -u presto_hatc_monitor.service | grep -E "unplugged|reconnected|Low power|Low percent" -m 10
  - Critical errors: journalctl -u presto_hatc_monitor.service -p 0..3 -n 10
  - Check service status: systemctl status presto_hatc_monitor.service
""")
    parser.add_argument('-i', '--install_as_service', action='store_true', help='Install the script as a systemd service.')
    parser.add_argument('-u', '--uninstall', action='store_true', help='Uninstall the systemd service and script.')
    parser.add_argument('-ntfy', '--enable-ntfy', action='store_true', help='Enable ntfy notifications.')
    parser.add_argument('-nt', '--ntfy-topic', type=str, default='presto_hatc_ups', help='ntfy topic name. Defaults to "presto_hatc_ups".')
    parser.add_argument('-ns', '--ntfy-server', type=str, default='https://ntfy.sh', help='ntfy server URL. Defaults to "https://ntfy.sh".')
    parser.add_argument('-t', '--test-ntfy', action='store_true', help='Send a test notification and exit.')
    parser.add_argument('--power-threshold', type=float, default=POWER_THRESHOLD, help='Power threshold in Watts to detect power loss.')
    parser.add_argument('--percent-threshold', type=float, default=PERCENT_THRESHOLD, help='Battery percentage threshold for low battery alert.')
    parser.add_argument('--critical-low-threshold', type=float, default=CRITICAL_LOW_THRESHOLD, help='Battery percentage threshold for critical low alert, triggering shutdown.')
    parser.add_argument('--critical-shutdown-delay', type=int, default=CRITICAL_SHUTDOWN_DELAY, help='Delay in seconds before shutdown on critical battery level.')
    parser.add_argument('--battery-capacity-mah', type=int, default=BATTERY_CAPACITY_MAH, help='Battery capacity in mAh for time remaining estimation.')
    parser.add_argument('--ntfy-cooldown-seconds', type=int, default=NTFY_COOLDOWN_SECONDS, help='Cooldown in seconds between repeated notifications for the same event.')
    args = parser.parse_args()

    # Handle installation/uninstallation first
    if args.uninstall:
        uninstall_service()
        sys.exit(0)

    if args.install_as_service:
        install_as_service(args)
        sys.exit(0)

    # Handle test notification
    if args.test_ntfy:
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
        monitor.send_ntfy_notification("test_info", 0, 0, 0)
        sys.exit(0)

    # Fallback to main monitoring loop
    main()