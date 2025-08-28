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
# Version: 1.4.6
# Author: piklz
# GitHub: https://github.com/piklz/pi_ups_monitor
# Description:
#   This script monitors the Presto UPS HAT on a Raspberry Pi Zero using the INA219
#   sensor, providing power, voltage, current, and battery percentage readings. It
#   sends notifications via ntfy for power events and can be installed as a systemd
#   service for continuous monitoring. Logs are sent to systemd-journald using
#   systemd-cat, with rotation managed by journald (configure in /etc/systemd/journald.conf).
#   Sampling occurs every 2 seconds asynchronously, with logging every 10 seconds.
#   Persistent journal storage (/var/log/journal/) is recommended, ideally with log2ram
#   to reduce SD card wear.
#
# Changelog:
#   Version 1.4.6 (2025-08-28):
#     - Fixed uninstall_service to robustly detect and remove service by checking both file existence and systemd loaded units, ensuring proper cleanup.
#     - Skipped systemctl reset-failed in install_as_service if service is not loaded to avoid unnecessary warning.
#   Version 1.4.5 (2025-08-28):
#     - Refactored notification logic to call send_ntfy_notification only when specific conditions are met, reducing unnecessary logs and aligning with x728 script behavior.
#     - Fixed residual process termination during installation by excluding lines containing "--install_as_service" to prevent killing the current installation process.
#   Version 1.4.4 (2025-08-28):
#     - Reduced verbosity of ntfy notification skips by logging "ntfy disabled" only once and suppressing non-critical skip logs, aligning with x728 script behavior.
#   Version 1.4.3 (2025-08-28):
#     - Fixed UTF-8 encoding issue in test_ntfy and send_ntfy_notification to handle emojis correctly, matching x728 script.
#   Version 1.4.2 (2025-08-28):
#     - Ported features from x728 script: added --enable-ntfy (default False), --test-ntfy, --uninstall, --debug.
#     - Made ntfy optional; improved install_as_service with robust reinstall handling (no --force-reinstall needed).
#     - Added notification queue for cooldown handling, enhanced notifications with system info.
#     - Added debug logging for raw I2C data; unified dependency checks for --help.
#     - Fixed service target to /usr/local/bin; added process cleanup during installation.
#     - Added epilog with journalctl tips; validated thresholds more robustly.
#
# Usage:
#   ./presto_hatc_monitor.py                     # Run monitoring directly with default settings
#   ./presto_hatc_monitor.py --install_as_service # Install as a systemd service
#   ./presto_hatc_monitor.py --uninstall         # Uninstall the service
#   ./presto_hatc_monitor.py --test-ntfy         # Send test ntfy notification (requires --enable-ntfy)
#   ./presto_hatc_monitor.py --addr 0x43 --enable-ntfy --ntfy-topic pizero_UPSc_TEST  # Run with custom settings
#   sudo systemctl status presto_ups.service    # Check service status
#   journalctl -t presto_ups                   # View logs
# -----------------------------------------------

import argparse
import os
import subprocess
import sys
import time
import smbus
import requests
import socket
import re
from datetime import datetime, timedelta
from collections import deque
import threading
import queue

# Color variables
COL_NC='\033[0m'
COL_INFO='\033[1;34m'
COL_WARNING='\033[1;33m'
COL_ERROR='\033[1;31m'

# Determine real user
USER = os.getenv("SUDO_USER") or os.getenv("USER") or os.popen("id -un").read().strip()

# Global debug flag
DEBUG_ENABLED = False

HAS_REQUESTS = False  # Will be set in check_dependencies

# Configuration for INA219
_REG_CONFIG                 = 0x00
_REG_SHUNTVOLTAGE           = 0x01
_REG_BUSVOLTAGE             = 0x02
_REG_POWER                  = 0x03
_REG_CURRENT                = 0x04
_REG_CALIBRATION            = 0x05

class BusVoltageRange:
    RANGE_16V               = 0x00
    RANGE_32V               = 0x01

class Gain:
    DIV_1_40MV              = 0x00
    DIV_2_80MV              = 0x01
    DIV_4_160MV             = 0x02
    DIV_8_320MV             = 0x03

class ADCResolution:
    ADCRES_9BIT_1S          = 0x00
    ADCRES_10BIT_1S         = 0x01
    ADCRES_11BIT_1S         = 0x02
    ADCRES_12BIT_1S         = 0x03
    ADCRES_12BIT_2S         = 0x09
    ADCRES_12BIT_4S         = 0x0A
    ADCRES_12BIT_8S         = 0x0B
    ADCRES_12BIT_16S        = 0x0C
    ADCRES_12BIT_32S        = 0x0D
    ADCRES_12BIT_64S        = 0x0E
    ADCRES_12BIT_128S       = 0x0F

class Mode:
    POWERDOW                = 0x00
    SVOLT_TRIGGERED         = 0x01
    BVOLT_TRIGGERED         = 0x02
    SANDBVOLT_TRIGGERED     = 0x03
    ADCOFF                  = 0x04
    SVOLT_CONTINUOUS        = 0x05
    BVOLT_CONTINUOUS        = 0x06
    SANDBVOLT_CONTINUOUS    = 0x07

class PrestoMonitor:
    def __init__(self, i2c_bus=1, addr=0x43, enable_ntfy=False, ntfy_server="https://ntfy.sh", ntfy_topic="pizero_UPSc_TEST", power_threshold=0.5, percent_threshold=20.0, battery_capacity_mAh=1000, battery_voltage=3.7):
        self.bus = smbus.SMBus(i2c_bus)
        self.addr = addr
        self.enable_ntfy = enable_ntfy
        self.ntfy_server = ntfy_server
        self.ntfy_topic = ntfy_topic
        self.power_threshold = power_threshold
        self.percent_threshold = percent_threshold
        self.battery_capacity_mAh = battery_capacity_mAh
        self.battery_voltage = battery_voltage
        self.last_notification = None
        self.notification_cooldown = timedelta(minutes=5)
        self.power_readings = deque(maxlen=5)
        self.current_readings = deque(maxlen=3)
        self.is_unplugged = False
        self.low_power_notified = False
        self.low_percent_notified = False
        self._cal_value = 0
        self._current_lsb = 0
        self._power_lsb = 0
        self.notification_queue = []
        self.set_calibration_16V_5A()

    def read(self, address):
        data = self.bus.read_i2c_block_data(self.addr, address, 2)
        value = (data[0] * 256) + data[1]
        if DEBUG_ENABLED:
            log_message("DEBUG", f"Raw I2C read from addr {self.addr:#x}, reg {address:#x}: {value}")
        return value

    def write(self, address, data):
        temp = [0, 0]
        temp[1] = data & 0xFF
        temp[0] = (data & 0xFF00) >> 8
        self.bus.write_i2c_block_data(self.addr, address, temp)
        if DEBUG_ENABLED:
            log_message("DEBUG", f"Raw I2C write to addr {self.addr:#x}, reg {address:#x}: {data}")

    def set_calibration_16V_5A(self):
        self._current_lsb = 0.1524
        self._cal_value = 26868
        self._power_lsb = 0.003048
        self.write(_REG_CALIBRATION, self._cal_value)
        self.bus_voltage_range = BusVoltageRange.RANGE_16V
        self.gain = Gain.DIV_2_80MV
        self.bus_adc_resolution = ADCResolution.ADCRES_12BIT_32S
        self.shunt_adc_resolution = ADCResolution.ADCRES_12BIT_32S
        self.mode = Mode.SANDBVOLT_CONTINUOUS
        self.config = self.bus_voltage_range << 13 | \
                      self.gain << 11 | \
                      self.bus_adc_resolution << 7 | \
                      self.shunt_adc_resolution << 3 | \
                      self.mode
        self.write(_REG_CONFIG, self.config)

    def getShuntVoltage_mV(self):
        self.write(_REG_CALIBRATION, self._cal_value)
        value = self.read(_REG_SHUNTVOLTAGE)
        if value > 32767:
            value -= 65535
        return value * 0.01

    def getBusVoltage_V(self):
        self.write(_REG_CALIBRATION, self._cal_value)
        self.read(_REG_BUSVOLTAGE)
        return (self.read(_REG_BUSVOLTAGE) >> 3) * 0.004

    def getCurrent_mA(self):
        value = self.read(_REG_CURRENT)
        if value > 32767:
            value -= 65535
        return value * self._current_lsb

    def getPower_W(self):
        self.write(_REG_CALIBRATION, self._cal_value)
        value = self.read(_REG_POWER)
        if value > 32767:
            value -= 65535
        return value * self._power_lsb

    def get_cpu_temp(self):
        try:
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                temp = float(f.read()) / 1000.0
                return temp
        except Exception:
            return None

    def get_gpu_temp(self):
        try:
            result = subprocess.run(['vcgencmd', 'measure_temp'], capture_output=True, text=True)
            temp_str = result.stdout.strip().split('=')[1].split("'")[0]
            return float(temp_str)
        except Exception:
            return None

    def get_hostname(self):
        try:
            return socket.gethostname()
        except Exception:
            return "Unknown"

    def get_ip_address(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "Unknown"

    def get_pi_model(self):
        try:
            with open('/sys/firmware/devicetree/base/model', 'r') as f:
                return f.read().strip()
        except Exception:
            return "Unknown"

    def get_free_ram(self):
        try:
            result = subprocess.run(['free', '-m'], capture_output=True, text=True)
            lines = result.stdout.split('\n')
            mem_line = [line for line in lines if line.startswith('Mem:')][0]
            free_mem = int(mem_line.split()[3])
            return free_mem
        except Exception:
            return "Unknown"

    def get_uptime(self):
        try:
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.read().split()[0])
            days = int(uptime_seconds // (24 * 3600))
            hours = int((uptime_seconds % (24 * 3600)) // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            return f"{days}d {hours}h {minutes}m"
        except Exception:
            return "Unknown"

    def estimate_battery_runtime(self):
        if len(self.power_readings) < self.power_readings.maxlen:
            return None
        avg_power_W = sum(self.power_readings) / len(self.power_readings)
        if avg_power_W < 0.1:
            return None
        avg_power_mW = avg_power_W * 1000
        energy_mWh = self.battery_capacity_mAh * self.battery_voltage
        runtime_hours = energy_mWh / avg_power_mW
        return runtime_hours

    def send_ntfy_notification(self, event_type, power, percent, current):
        global HAS_REQUESTS
        if not self.enable_ntfy or not HAS_REQUESTS:
            log_message("INFO", f"ntfy notification ({event_type}) skipped: {'ntfy disabled' if not self.enable_ntfy else 'python3-requests not installed'}", exit_on_error=False)
            return
        current_time = datetime.now()
        if self.last_notification is not None and (current_time - self.last_notification) < self.notification_cooldown:
            if event_type in ["unplugged", "reconnected"]:
                self.notification_queue.append((current_time, event_type, power, percent, current))
                log_message("INFO", f"Notification ({event_type}) queued due to cooldown. Queue size: {len(self.notification_queue)}")
            else:
                log_message("INFO", f"Notification ({event_type}) skipped due to cooldown. Will send after {self.notification_cooldown.total_seconds() - (current_time - self.last_notification).total_seconds():.1f} seconds")
            return

        hostname = self.get_hostname()
        ip = self.get_ip_address()
        cpu_temp = self.get_cpu_temp()
        gpu_temp = self.get_gpu_temp()
        temp_info = (
            f"CPU: {cpu_temp:.1f}°C, GPU: {gpu_temp:.1f}°C"
            if cpu_temp is not None and gpu_temp is not None
            else "Temp unavailable"
        )
        message = None
        title = None
        if event_type == "unplugged":
            runtime = self.estimate_battery_runtime()
            runtime_str = f"{runtime:.1f} hours" if runtime else "unknown"
            message = f"⚠️🔌 USB Charger Unplugged on {hostname} (IP: {ip}): Running on battery, estimated runtime {runtime_str}, {percent:.1f}% ({power:.3f} W), Temps: {temp_info}"
            title = "Presto UPS Unplugged"
            self.is_unplugged = True
            self.low_power_notified = False
            self.low_percent_notified = False
        elif event_type == "reconnected":
            message = f"✅🔋 USB Charger Reconnected on {hostname} (IP: {ip}): System back on external power, {percent:.1f}% ({power:.3f} W), Temps: {temp_info}"
            title = "Presto UPS Reconnected"
            self.is_unplugged = False
            self.low_power_notified = False
            self.low_percent_notified = False
        elif event_type == "low_power":
            message = f"🪫 Low Power Alert on {hostname} (IP: {ip}): {power:.3f} W (Threshold: {self.power_threshold} W), {percent:.1f}%, Temps: {temp_info}"
            title = "Presto UPS Low Power"
            self.low_power_notified = True
        elif event_type == "low_percent":
            message = f"🪫 Low Percent Alert on {hostname} (IP: {ip}): {percent:.1f}% (Threshold: {self.percent_threshold}%), {power:.3f} W, Temps: {temp_info}"
            title = "Presto UPS Low Percent"
            self.low_percent_notified = True
        if message:
            try:
                requests.post(
                    f"{self.ntfy_server}/{self.ntfy_topic}",
                    data=message.encode('utf-8'),
                    headers={"Title": title.encode('utf-8')}
                )
                log_message("INFO", f"Notification sent: {message}")
                self.last_notification = current_time
            except Exception as e:
                log_message("ERROR", f"Failed to send notification: {e}", exit_on_error=False)

    def process_notification_queue(self):
        global HAS_REQUESTS
        if not self.enable_ntfy or not HAS_REQUESTS or not self.notification_queue or self.last_notification is None:
            return
        current_time = datetime.now()
        if (current_time - self.last_notification) >= self.notification_cooldown:
            self.notification_queue.sort(key=lambda x: x[0], reverse=True)
            events_to_send = self.notification_queue[:2]
            extra_events = len(self.notification_queue) - 2 if len(self.notification_queue) > 2 else 0
            for i, (timestamp, event_type, power, percent, current) in enumerate(events_to_send):
                hostname = self.get_hostname()
                ip = self.get_ip_address()
                cpu_temp = self.get_cpu_temp()
                gpu_temp = self.get_gpu_temp()
                temp_info = (
                    f"CPU: {cpu_temp:.1f}°C, GPU: {gpu_temp:.1f}°C"
                    if cpu_temp is not None and gpu_temp is not None
                    else "Temp unavailable"
                )
                message = None
                title = None
                if event_type == "unplugged":
                    runtime = self.estimate_battery_runtime()
                    runtime_str = f"{runtime:.1f} hours" if runtime else "unknown"
                    message = f"⚠️🔌 USB Charger Unplugged on {hostname} (IP: {ip}): Running on battery, estimated runtime {runtime_str}, {percent:.1f}% ({power:.3f} W), Temps: {temp_info}"
                    title = "Presto UPS Unplugged"
                elif event_type == "reconnected":
                    message = f"✅🔋 USB Charger Reconnected on {hostname} (IP: {ip}): System back on external power, {percent:.1f}% ({power:.3f} W), Temps: {temp_info}"
                    title = "Presto UPS Reconnected"
                if extra_events > 0 and i == 1:
                    message += f"\n...+{extra_events} similar events, please check journal"
                if message and title:
                    try:
                        requests.post(
                            f"{self.ntfy_server}/{self.ntfy_topic}",
                            data=message.encode('utf-8'),
                            headers={"Title": title.encode('utf-8')}
                        )
                        log_message("INFO", f"Queued notification sent: {message}")
                        self.last_notification = current_time
                    except Exception as e:
                        log_message("ERROR", f"Failed to send queued notification: {e}", exit_on_error=False)
            self.notification_queue = []
            log_message("INFO", "Notification queue cleared")

def log_message(log_level, console_message, log_file_message=None, exit_on_error=True):
    if log_level == "DEBUG" and not DEBUG_ENABLED:
        return
    if log_file_message is None:
        log_file_message = console_message
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    journal_message = f"[{timestamp}] [presto-UPSc-service] [{log_level}] {log_file_message}"
    valid_priorities = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
    priority = log_level.lower() if log_level.lower() in valid_priorities else "info"
    try:
        subprocess.run(
            ["systemd-cat", "-t", "presto_ups", "-p", priority],
            input=journal_message,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"[presto-UPSc-service] [ERROR] Failed to log to journald: {e}", file=sys.stderr)
    if sys.stdin.isatty():
        color = {"INFO": COL_INFO, "WARNING": COL_WARNING, "ERROR": COL_ERROR, "DEBUG": COL_INFO}.get(log_level, COL_NC)
        print(f"[presto-UPSc-service] {color}[{log_level}]{COL_NC} {console_message}")
    if log_level == "ERROR" and exit_on_error:
        sys.exit(1)

def check_service_running():
    try:
        result = subprocess.run(["systemctl", "is-active", "--quiet", "presto_ups"], check=False)
        if result.returncode == 0:
            ps_result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
            current_pid = str(os.getpid())
            for line in ps_result.stdout.splitlines():
                if "presto_hatc_monitor.py" in line and current_pid not in line:
                    log_message("DEBUG", f"Found running presto_ups process: {line}")
                    return True
        return False
    except subprocess.CalledProcessError as e:
        log_message("WARNING", f"Failed to check service status: {e.stderr}", exit_on_error=False)
        return False

def check_dependencies(requires_i2c=True):
    global HAS_REQUESTS
    log_message("INFO", f"Checking dependencies for user {USER}")
    smbus_module, bus = None, None
    HAS_REQUESTS = False

    if not os.path.exists("/usr/bin/python3"):
        log_message("ERROR", "python3 is not installed. Please install it with 'sudo apt install python3'")
        return None, None

    log_message("INFO", f"Python3 is installed: {subprocess.getoutput('python3 --version')}")

    try:
        import requests
        log_message("INFO", "python3-requests is installed")
        HAS_REQUESTS = True
    except ImportError:
        log_message("INFO", "python3-requests is not installed. ntfy notifications disabled", exit_on_error=False)

    if requires_i2c:
        try:
            import smbus as smbus_module
            log_message("INFO", "python3-smbus is installed")
            try:
                bus = smbus_module.SMBus(1)
                bus.read_byte(0x43)  # Default addr
                log_message("INFO", "smbus module is functional")
            except Exception as e:
                log_message("ERROR", f"smbus module failed to access I2C bus: {e}")
                return smbus_module, None
        except ImportError:
            log_message("ERROR", "python3-smbus is not installed. Please install it with 'sudo apt install python3-smbus'")
            return None, None

    if os.path.exists("/usr/bin/vcgencmd"):
        log_message("INFO", "libraspberrypi-bin is installed")
    else:
        log_message("WARNING", "libraspberrypi-bin not installed, GPU temp unavailable", exit_on_error=False)

    if not os.path.exists("/usr/bin/systemd-cat"):
        log_message("ERROR", "systemd-cat is not installed. Please install systemd")

    return smbus_module, bus

def enable_i2c():
    log_message("INFO", "Checking I2C status")
    with open("/boot/config.txt", "r") as f:
        if "dtparam=i2c_arm=on" in f.read():
            log_message("INFO", "I2C is already enabled")
        else:
            log_message("INFO", "Enabling I2C via raspi-config")
            result = subprocess.run(["raspi-config", "nonint", "do_i2c", "0"], capture_output=True, text=True)
            if result.returncode == 0:
                log_message("INFO", "I2C enabled successfully")
            else:
                log_message("ERROR", "Failed to enable I2C. Please enable manually via 'sudo raspi-config'")

def check_i2c_device(i2c_addr):
    if os.path.exists("/usr/sbin/i2cdetect"):
        log_message("INFO", f"Checking for INA219 at address {i2c_addr}")
        result = subprocess.run(["i2cdetect", "-y", "1"], capture_output=True, text=True)
        if i2c_addr[2:].lower() in result.stdout:
            log_message("INFO", f"INA219 detected at address {i2c_addr}")
        else:
            log_message("WARNING", f"INA219 not detected at address {i2c_addr}. Check wiring and address")
    else:
        log_message("WARNING", f"i2cdetect not found, cannot verify INA219 at address {i2c_addr}")

def install_as_service(args):
    if os.geteuid() != 0:
        log_message("ERROR", "Service installation must be run as root")
    log_message("INFO", f"Installing Presto UPS HAT monitor service for user {USER}")

    service_file = "/etc/systemd/system/presto_hatc_ups.service"
    target_script = "/usr/local/bin/presto_hatc_monitor.py"
    service_exists = os.path.exists(service_file)
    service_running = False
    service_status = "unknown"

    if service_exists:
        log_message("WARNING", "presto_ups service is already installed")
        try:
            result = subprocess.run(["systemctl", "is-active", "--quiet", "presto_ups"], check=False)
            service_running = result.returncode == 0
            if service_running:
                log_message("WARNING", "presto_ups service is currently running")
            else:
                log_message("INFO", "presto_ups service is installed but not running")
            result = subprocess.run(["systemctl", "status", "presto_ups.service"], capture_output=True, text=True)
            service_status = result.stdout
        except subprocess.CalledProcessError as e:
            log_message("WARNING", f"Failed to check service status: {e.stderr}", exit_on_error=False)

    if service_exists and sys.stdin.isatty():
        log_message("INFO", f"Current service status:\n{service_status}")
        log_message("INFO", f"New settings: addr={args.addr}, enable-ntfy={args.enable_ntfy}, ntfy-server={args.ntfy_server}, ntfy-topic={args.ntfy_topic}, power-threshold={args.power_threshold}, percent-threshold={args.percent_threshold}, battery-capacity={args.battery_capacity}, battery-voltage={args.battery_voltage}")
        try:
            response = input("[presto-UPSc-service] [INFO] Would you like to reinstall with new settings? (y/n): ").strip().lower()
            if response != 'y':
                log_message("INFO", "Installation aborted by user")
                sys.exit(0)
        except KeyboardInterrupt:
            log_message("INFO", "Installation aborted by user")
            sys.exit(0)

    if service_exists:
        log_message("INFO", "Ensuring presto_ups service is stopped and reset")
        try:
            if service_running:
                subprocess.run(["systemctl", "stop", "presto_ups.service"], check=True)
                log_message("INFO", "Service stopped successfully")
            subprocess.run(["systemctl", "disable", "presto_ups.service"], check=True)
            log_message("INFO", "Service disabled successfully")
            # Check if the service is loaded before attempting reset-failed
            result = subprocess.run(["systemctl", "list-units", "--full", "-all"], capture_output=True, text=True)
            if "presto_ups.service" in result.stdout:
                try:
                    subprocess.run(["systemctl", "reset-failed", "presto_ups.service"], check=True)
                    log_message("INFO", "Service failed state reset successfully")
                except subprocess.CalledProcessError as e:
                    log_message("WARNING", f"Failed to reset failed state: {e.stderr}", exit_on_error=False)
            else:
                log_message("INFO", "Skipping reset-failed as service is not loaded")
            time.sleep(1)
        except subprocess.CalledProcessError as e:
            log_message("ERROR", f"Failed to stop or disable service: {e.stderr}", exit_on_error=False)

    # Kill any residual processes
    try:
        ps_result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
        current_pid = str(os.getpid())
        for line in ps_result.stdout.splitlines():
            if "presto_hatc_monitor.py" in line and "--install_as_service" not in line and current_pid not in line:
                pid = line.split()[1]
                log_message("INFO", f"Terminating residual presto_ups process: PID {pid}")
                subprocess.run(["kill", "-9", pid], check=True)
    except subprocess.CalledProcessError as e:
        log_message("WARNING", f"Failed to terminate residual processes: {e.stderr}", exit_on_error=False)

    try:
        log_message("INFO", f"Copying script to {target_script}")
        if os.path.exists(target_script):
            backup_script = f"{target_script}.bak"
            log_message("INFO", f"Backing up existing script to {backup_script}")
            subprocess.run(["cp", target_script, backup_script], check=True)
        subprocess.run(["cp", os.path.abspath(__file__), target_script], check=True)
        subprocess.run(["chmod", "755", target_script], check=True)
        subprocess.run(["chown", f"{USER}:{USER}", target_script], check=True)
        log_message("INFO", f"Script copied successfully to {target_script}")
    except subprocess.CalledProcessError as e:
        log_message("ERROR", f"Failed to copy script to {target_script}: {e.stderr}")

    exec_start = f"/usr/bin/python3 {target_script} --addr {args.addr} --power-threshold {args.power_threshold} --percent-threshold {args.percent_threshold} --battery-capacity {args.battery_capacity} --battery-voltage {args.battery_voltage}"
    if args.enable_ntfy:
        exec_start += f" --enable-ntfy --ntfy-server {args.ntfy_server} --ntfy-topic {args.ntfy_topic}"
    if args.debug:
        exec_start += " --debug"
    service_content = f"""[Unit]
Description=Raspberry Pi Presto UPS Monitor Service
After=network.target

[Service]
ExecStart={exec_start}
WorkingDirectory=/usr/local/bin
StandardOutput=journal
StandardError=journal
Restart=always
User={USER}

[Install]
WantedBy=multi-user.target
"""
    try:
        with open(service_file, "w") as f:
            f.write(service_content)
        subprocess.run(["chmod", "644", service_file], check=True)
        log_message("INFO", "Service file created successfully")
    except Exception as e:
        log_message("ERROR", f"Failed to create service file {service_file}: {e}")

    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "presto_ups.service"], check=True)
        subprocess.run(["systemctl", "start", "presto_ups.service"], check=True)
    except subprocess.CalledProcessError as e:
        log_message("ERROR", f"Failed to set up service: {e}")

    log_message("INFO", "Checking service status")
    result = subprocess.run(["systemctl", "is-active", "--quiet", "presto_ups"], check=False)
    if result.returncode == 0:
        log_message("INFO", "Service presto_ups is running successfully")
        log_message("INFO", "Recent service logs:")
        subprocess.run(["journalctl", "-u", "presto_ups", "-n", "10", "--no-pager"], check=False)
        log_message("INFO", "Service status details:")
        subprocess.run(["systemctl", "status", "presto_ups", "--no-pager"], check=False)
    else:
        log_message("ERROR", "Service presto_ups failed to start")
        log_message("INFO", "Service status details:")
        subprocess.run(["systemctl", "status", "presto_ups", "--no-pager"], check=False)
        log_message("INFO", "Recent service logs:")
        subprocess.run(["journalctl", "-u", "presto_ups", "-n", "10", "--no-pager"], check=False)
        sys.exit(1)

    log_message("INFO", "Service Management Tips:")
    log_message("INFO", "  - Check recent voltage/current logs: journalctl -u presto_ups.service | grep -E \"Voltage|Current|Power|Percent\" -m 10")
    log_message("INFO", "  - Check power events: journalctl -u presto_ups.service | grep -E \"unplugged|reconnected|Low power|Low percent\" -m 10")
    log_message("INFO", "  - Check critical errors: journalctl -u presto_ups.service -p 0..3 -n 10")
    log_message("INFO", "  - Check debug logs (if enabled): journalctl -u presto_ups.service | grep DEBUG -m 10")
    log_message("INFO", "  - Uninstall service: sudo {} --uninstall".format(os.path.basename(__file__)))
    reinstall_cmd = f"sudo {os.path.basename(__file__)} --install_as_service --addr {args.addr} --power-threshold {args.power_threshold} --percent-threshold {args.percent_threshold} --battery-capacity {args.battery_capacity} --battery-voltage {args.battery_voltage}"
    if args.enable_ntfy:
        reinstall_cmd += f" --enable-ntfy --ntfy-server {args.ntfy_server} --ntfy-topic {args.ntfy_topic}"
    if args.debug:
        reinstall_cmd += " --debug"
    log_message("INFO", f"  - Reinstall with current settings: {reinstall_cmd}")
    log_message("INFO", "Installation complete")
    sys.exit(0)

def uninstall_service():
    if os.geteuid() != 0:
        log_message("ERROR", "Service uninstallation must be run as root")
    log_message("INFO", f"Uninstalling Presto UPS monitor service for user {USER}")

    service_file = "/etc/systemd/system/presto_hatc_ups.service"
    target_script = "/usr/local/bin/presto_hatc_monitor.py"
    service_exists = False
    service_running = False
    service_status = "unknown"

    # Check if service is loaded in systemd
    try:
        result = subprocess.run(["systemctl", "list-units", "--full", "-all"], capture_output=True, text=True)
        if "presto_ups.service" in result.stdout:
            service_exists = True
            log_message("INFO", "presto_ups service is loaded in systemd")
        result = subprocess.run(["systemctl", "is-active", "--quiet", "presto_ups"], check=False)
        service_running = result.returncode == 0
        if service_running:
            log_message("WARNING", "presto_ups service is currently running")
        else:
            log_message("INFO", "presto_ups service is loaded but not running")
        result = subprocess.run(["systemctl", "status", "presto_ups.service"], capture_output=True, text=True)
        service_status = result.stdout
    except subprocess.CalledProcessError as e:
        log_message("WARNING", f"Failed to check service status: {e.stderr}", exit_on_error=False)

    # Check if service file exists
    if os.path.exists(service_file):
        service_exists = True
        log_message("INFO", f"Service file found: {service_file}")
    else:
        log_message("INFO", f"Service file not found: {service_file}")

    if not service_exists:
        log_message("INFO", "presto_ups service is not installed or loaded")
        # Still attempt to clean up any residual files or processes
        try:
            if os.path.exists(target_script):
                subprocess.run(["rm", "-f", target_script], check=True)
                log_message("INFO", f"Script removed: {target_script}")
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            log_message("INFO", "Systemd daemon reloaded successfully")
            # Reset failed state for good measure
            subprocess.run(["systemctl", "reset-failed", "presto_ups.service"], check=False)
        except subprocess.CalledProcessError as e:
            log_message("WARNING", f"Failed to clean up residual files or reload daemon: {e.stderr}", exit_on_error=False)
        log_message("INFO", "Uninstallation complete. No service was loaded or installed.")
        sys.exit(0)

    if service_exists and sys.stdin.isatty():
        log_message("INFO", f"Current service status:\n{service_status}")
        try:
            response = input("[presto-UPSc-service] [INFO] Would you like to uninstall the presto_ups service? (y/n): ").strip().lower()
            if response != 'y':
                log_message("INFO", "Uninstallation aborted by user")
                sys.exit(0)
        except KeyboardInterrupt:
            log_message("INFO", "Uninstallation aborted by user")
            sys.exit(0)

    # Stop and disable the service if running
    if service_running:
        log_message("INFO", "Stopping presto_ups service")
        try:
            subprocess.run(["systemctl", "stop", "presto_ups.service"], check=True)
            log_message("INFO", "Service stopped successfully")
        except subprocess.CalledProcessError as e:
            log_message("ERROR", f"Failed to stop service: {e.stderr}", exit_on_error=False)
        try:
            subprocess.run(["systemctl", "disable", "presto_ups.service"], check=True)
            log_message("INFO", "Service disabled successfully")
        except subprocess.CalledProcessError as e:
            log_message("ERROR", f"Failed to disable service: {e.stderr}", exit_on_error=False)

    # Remove the service file
    if os.path.exists(service_file):
        try:
            subprocess.run(["rm", "-f", service_file], check=True)
            log_message("INFO", f"Service file removed: {service_file}")
        except subprocess.CalledProcessError as e:
            log_message("ERROR", f"Failed to remove service file: {e.stderr}", exit_on_error=False)

    # Reload systemd daemon (first pass)
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        log_message("INFO", "Systemd daemon reloaded successfully")
    except subprocess.CalledProcessError as e:
        log_message("ERROR", f"Failed to reload systemd daemon: {e.stderr}", exit_on_error=False)

    # Remove the script file
    if os.path.exists(target_script):
        try:
            subprocess.run(["rm", "-f", target_script], check=True)
            log_message("INFO", f"Script removed: {target_script}")
        except subprocess.CalledProcessError as e:
            log_message("ERROR", f"Failed to remove script: {e.stderr}", exit_on_error=False)

    # Second pass to ensure systemd fully purges the unit
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "reset-failed", "presto_ups.service"], check=False)
        log_message("INFO", "Systemd daemon reloaded again to ensure unit cleanup")
    except subprocess.CalledProcessError as e:
        log_message("WARNING", f"Failed to reload systemd daemon or reset failed state: {e.stderr}", exit_on_error=False)

    # Verify service is no longer loaded
    try:
        result = subprocess.run(["systemctl", "list-units", "--full", "-all"], capture_output=True, text=True)
        if "presto_ups.service" not in result.stdout:
            log_message("INFO", "presto_ups service is no longer loaded in systemd")
        else:
            log_message("WARNING", "presto_ups service is still loaded in systemd, manual cleanup may be required")
            log_message("INFO", "Run the following commands to clean up:")
            log_message("INFO", "  sudo systemctl daemon-reload")
            log_message("INFO", "  sudo systemctl reset-failed presto_ups.service")
    except subprocess.CalledProcessError as e:
        log_message("WARNING", f"Failed to verify service removal: {e.stderr}", exit_on_error=False)

    log_message("INFO", "Uninstallation complete. You can now run the script interactively.")
    sys.exit(0)

def test_ntfy(ntfy_server, ntfy_topic):
    log_message("INFO", "Testing ntfy connectivity")
    monitor = PrestoMonitor(enable_ntfy=True, ntfy_server=ntfy_server, ntfy_topic=ntfy_topic)
    hostname = monitor.get_hostname()
    ip = monitor.get_ip_address()
    message = f"🌟 Presto UPS Test Notification\nHostname: {hostname}\nIP: {ip}\nModel: {monitor.get_pi_model()}\nFree RAM: {monitor.get_free_ram()} MB\nUptime: {monitor.get_uptime()}\n◕‿◕"
    title = "Presto UPS Test Alert"
    try:
        requests.post(
            f"{ntfy_server}/{ntfy_topic}",
            data=message.encode('utf-8'),
            headers={"Title": title.encode('utf-8')}
        )
        log_message("INFO", f"ntfy test notification sent successfully. Check your topic ({ntfy_server}/{ntfy_topic})")
    except Exception as e:
        log_message("WARNING", f"Failed to send test ntfy notification: {e}")

def sample_ina219(monitor, data_queue, data_lock):
    while True:
        try:
            with data_lock:
                bus_voltage = monitor.getBusVoltage_V()
                shunt_voltage = monitor.getShuntVoltage_mV() / 1000
                current = monitor.getCurrent_mA()
                power = monitor.getPower_W()
                percent = (bus_voltage - 3) / 1.2 * 100
                if percent > 100:
                    percent = 100
                if percent < 0:
                    percent = 0
                data = {
                    'bus_voltage': bus_voltage,
                    'shunt_voltage': shunt_voltage,
                    'current': current,
                    'power': power,
                    'percent': percent
                }
            data_queue.put(data)
            monitor.power_readings.append(power)
            monitor.current_readings.append(current)
            if len(monitor.power_readings) < monitor.power_readings.maxlen:
                time.sleep(2)
                continue
            all_negative = all(c < -10 for c in monitor.current_readings)
            all_positive = all(c > 10 for c in monitor.current_readings)
            if all_negative and not monitor.is_unplugged:
                monitor.send_ntfy_notification("unplugged", power, percent, current)
            elif all_positive and monitor.is_unplugged:
                monitor.send_ntfy_notification("reconnected", power, percent, current)
            elif all_positive and power < monitor.power_threshold and not monitor.low_power_notified:
                monitor.send_ntfy_notification("low_power", power, percent, current)
            elif all_positive and percent < monitor.percent_threshold and not monitor.low_percent_notified:
                monitor.send_ntfy_notification("low_percent", power, percent, current)
            monitor.process_notification_queue()
        except Exception as e:
            log_message("ERROR", f"Sampling error: {e}")
        time.sleep(2)

def main():
    parser = argparse.ArgumentParser(
        description="Presto UPS HAT Monitor with Service Installation (Version 1.4.6)",
        epilog="""
Useful journalctl commands for monitoring:
  - Recent voltage/current logs: journalctl -u presto_ups.service | grep -E "Voltage|Current|Power|Percent" -m 10
  - Power event logs: journalctl -u presto_ups.service | grep -E "unplugged|reconnected|Low power|Low percent" -m 10
  - Critical errors: journalctl -u presto_ups.service -p 0..3 -n 10
  - Debug logs (if --debug enabled): journalctl -u presto_ups.service | grep DEBUG -m 10
""",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--install_as_service", action="store_true", help="Install as a systemd service")
    parser.add_argument("--uninstall", action="store_true", help="Uninstall the presto_ups service")
    parser.add_argument("--test-ntfy", action="store_true", help="Send a test ntfy notification (requires --enable-ntfy)")
    parser.add_argument("--enable-ntfy", action="store_true", help="Enable ntfy notifications")
    parser.add_argument("--addr", default="0x43", help="I2C address of INA219 (e.g., 0x43)")
    parser.add_argument("--ntfy-server", default="https://ntfy.sh", help="ntfy server URL")
    parser.add_argument("--ntfy-topic", default="pizero_UPSc_TEST", help="ntfy topic for notifications")
    parser.add_argument("--power-threshold", type=float, default=0.5, help="Power threshold for alerts in watts")
    parser.add_argument("--percent-threshold", type=float, default=20.0, help="Battery percentage threshold for alerts")
    parser.add_argument("--battery-capacity", type=int, default=1000, help="Battery capacity in mAh")
    parser.add_argument("--battery-voltage", type=float, default=3.7, help="Battery nominal voltage in volts")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging for raw I2C data")
    args = parser.parse_args()

    global DEBUG_ENABLED
    DEBUG_ENABLED = args.debug

    if args.install_as_service and (args.uninstall or args.test_ntfy):
        log_message("ERROR", "Cannot use --install_as_service with --uninstall or --test-ntfy")
    if args.uninstall and args.test_ntfy:
        log_message("ERROR", "Cannot use --uninstall with --test-ntfy")
    if args.test_ntfy and not args.enable_ntfy:
        log_message("ERROR", "--test-ntfy requires --enable-ntfy")

    if args.power_threshold <= 0:
        log_message("ERROR", "Power threshold must be positive")
    if args.percent_threshold <= 0 or args.percent_threshold > 100:
        log_message("ERROR", "Percent threshold must be between 0 and 100")
    if args.battery_capacity <= 0:
        log_message("ERROR", "Battery capacity must be positive")
    if args.battery_voltage <= 0:
        log_message("ERROR", "Battery voltage must be positive")
    if not re.match(r"^0x[0-9A-Fa-f]{2}$", args.addr):
        log_message("ERROR", "Invalid I2C address format. Use hex (e.g., 0x43)")

    requires_i2c = not (args.install_as_service or args.uninstall or ("-h" in sys.argv or "--help" in sys.argv))
    if requires_i2c and check_service_running():
        log_message("ERROR", "The presto_ups service is running. Stop the service first with: sudo systemctl stop presto_ups.service")

    if args.uninstall:
        uninstall_service()

    if args.test_ntfy:
        test_ntfy(args.ntfy_server, args.ntfy_topic)
        sys.exit(0)

    if args.install_as_service:
        check_dependencies(requires_i2c=True)
        enable_i2c()
        check_i2c_device(args.addr)
        install_as_service(args)

    check_dependencies(requires_i2c=True)

    monitor = PrestoMonitor(
        i2c_bus=1,
        addr=int(args.addr, 16),
        enable_ntfy=args.enable_ntfy,
        ntfy_server=args.ntfy_server,
        ntfy_topic=args.ntfy_topic,
        power_threshold=args.power_threshold,
        percent_threshold=args.percent_threshold,
        battery_capacity_mAh=args.battery_capacity,
        battery_voltage=args.battery_voltage
    )

    data_queue = queue.Queue()
    data_lock = threading.Lock()
    sampling_thread = threading.Thread(target=sample_ina219, args=(monitor, data_queue, data_lock), daemon=True)
    sampling_thread.start()

    last_log_time = datetime.now()
    log_interval = timedelta(seconds=10)

    while True:
        try:
            data = data_queue.get_nowait()
            if datetime.now() - last_log_time >= log_interval:
                log_message("INFO", f"Load Voltage: {data['bus_voltage']:>6.3f} V")
                log_message("INFO", f"Current:      {data['current']/1000:>6.3f} A")
                log_message("INFO", f"Power:        {data['power']:>6.3f} W")
                log_message("INFO", f"Percent:     {data['percent']:>6.1f}%")
                log_message("INFO", "System Info:")
                log_message("INFO", f"Hostname:    {monitor.get_hostname()}")
                log_message("INFO", f"IP Address:  {monitor.get_ip_address()}")
                cpu_temp = monitor.get_cpu_temp()
                gpu_temp = monitor.get_gpu_temp()
                log_message("INFO", f"CPU Temp:    {cpu_temp if cpu_temp else 'Unknown':>6.1f} °C" if cpu_temp else "CPU Temp:    Unknown")
                log_message("INFO", f"GPU Temp:    {gpu_temp if gpu_temp else 'Unknown':>6.1f} °C" if gpu_temp else "GPU Temp:    Unknown")
                log_message("INFO", "---")
                last_log_time = datetime.now()
        except queue.Empty:
            pass
        time.sleep(0.1)

if __name__ == "__main__":
    main()