#!/usr/bin/env python3
#
#      _____ ____  ___          _   ____  
#__  _|___  |___ \( _ )  __   _/ | |___ \ 
#\ \/ /  / /  __) / _ \  \ \ / / |   __) |
# >  <  / /  / __/ (_) |  \ V /| |_ / __/ 
#/_/\_\/_/  |_____\___/    \_/ |_(_)_____|HW v1.2 HAT Battery Monitor for raspberry pi 3,4,5 [piOS/debian based OS]
#
# -----------------------------------------------
#  ! may work on more recent/latest  x728 models with some tweaks to ports 
#  -- (26 for v2.0+ for example) will update  code when I get an updated model
#  PS: Since 2024-09-02, Raspberry Pi has finally unified the gpipchip to 0 on 
#  all Raspberry Pis(Pi, 0, 1, 2, 3, 4, 5). Previously, the gpiochip on the Raspberry Pi 5 was 4.
# -----------------------------------------------
# x728 UPS Monitor Script
# Version: 1.1.0
# Author: piklz
# GitHub: https://github.com/piklz/pi_ups_monitor
# Geekworm Site: https://wiki.geekworm.com/X728-hardware#Power_Jack_and_Connectors
#                https://wiki.geekworm.com/X728-Software
#                https://wiki.geekworm.com/X728-script
# Description:
#   Monitors the x728 UPS HAT (v1.2, may work with others) on a Raspberry Pi, providing battery voltage,
#   percentage, and power loss detection via GPIO. Optionally sends notifications via ntfy if --enable-ntfy is used.
#   Installable as a systemd service. Logs to systemd-journald with rotation managed by journald.
#
# Changelog:
#   Version 1.1.0 (2025-09-14):
#     - Added est. time remaining(battery) to notifications + short args for command-line options for easier use.
#   Version 1.0.23 (2025-08-21):
#     - Fixed install_as_service reinstall tip to display actual argument values instead of placeholders.
#   Version 1.0.22 (2025-08-21):
#     - Improved install_as_service to check if service is loaded before running systemctl reset-failed, preventing non-critical error when unit is not loaded.
#     - Log reset-failed failures as WARNING instead of ERROR for clarity.
#   Version 1.0.21 (2025-08-21):
#     - Fixed ValueError in --help by escaping % in argparse help text for threshold arguments.
#   Version 1.0.20 (2025-08-21):
#     - Enhanced --install_as_service to stop, disable, and reset service state before reinstalling.
# -----------------------------------------------

import argparse
import os
import subprocess   
import sys
import time
import struct
import threading
import socket
from datetime import datetime, timedelta

# Color variables
COL_NC='\033[0m'
COL_INFO='\033[1;34m'
COL_WARNING='\033[1;33m'
COL_ERROR='\033[1;31m'

# Determine real user
USER = os.getenv("SUDO_USER") or os.getenv("USER") or os.popen("id -un").read().strip()

# Global settings
GPIO_PORT = 13
I2C_ADDR = 0x36
LOW_BATTERY_THRESHOLD = 30
CRITICAL_LOW_THRESHOLD = 10
RECONNECT_TIMEOUT = 60
RED = '\033[91m'
GREEN = '\033[92m'
ENDC = '\033[0m'
SCRIPT_VERSION = "1.1.0"
# Define the chip and lines
chipname = "gpiochip0"
line_offset = 6
out_line_offset = 13

# I2C lock for thread safety
i2c_lock = threading.Lock()

# Global debug flag
DEBUG_ENABLED = False

def log_message(log_level, console_message, log_file_message=None, exit_on_error=True):
    """_summary_

    Args:
        log_level (_type_): _description_
        console_message (_type_): _description_
        log_file_message (_type_, optional): _description_. Defaults to None.
        exit_on_error (bool, optional): _description_. Defaults to True.
    """    
    if log_level == "DEBUG" and not DEBUG_ENABLED:
        return
    if log_file_message is None:
        log_file_message = console_message
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    journal_message = f"[{timestamp}] [x728-UPS-service] [{log_level}] {log_file_message}"
    valid_priorities = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
    priority = log_level.lower() if log_level.lower() in valid_priorities else "info"
    try:
        subprocess.run(
            ["systemd-cat", "-t", "x728_ups", "-p", priority],
            input=journal_message,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"[x728-UPS-service] [ERROR] Failed to log to journald: {e}", file=sys.stderr)
    if sys.stdin.isatty():
        color = {"INFO": COL_INFO, "WARNING": COL_WARNING, "ERROR": COL_ERROR, "DEBUG": COL_INFO}.get(log_level, COL_NC)
        print(f"[x728-UPS-service] {color}[{log_level}]{COL_NC} {console_message}")
    if log_level == "ERROR" and exit_on_error:
        sys.exit(1)

def check_service_running():
    """_summary_

    Returns:
        _type_: _description_
    """    
    try:
        result = subprocess.run(["systemctl", "is-active", "--quiet", "x728_ups"], check=False)
        if result.returncode == 0:
            ps_result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
            current_pid = str(os.getpid())
            for line in ps_result.stdout.splitlines():
                if "/usr/local/bin/presto_x728_ups_monitor.py" in line and current_pid not in line:
                    log_message("DEBUG", f"Found running x728_ups process: {line}")
                    return True
        return False
    except subprocess.CalledProcessError as e:
        log_message("WARNING", f"Failed to check service status: {e.stderr}", exit_on_error=False)
        return False

def check_dependencies(requires_i2c=True):
    """_summary_

    Args:
        requires_i2c (bool, optional): _description_. Defaults to True.

    Returns:
        _type_: _description_
    """    
    log_message("INFO", f"Checking dependencies for user {USER}")
    smbus, bus, has_requests = None, None, False

    if not os.path.exists("/usr/bin/python3"):
        log_message("ERROR", "python3 is not installed. Please install it with 'sudo apt install python3'")
        return None, None, False

    log_message("INFO", f"Python3 is installed: {subprocess.getoutput('python3 --version')}")

    try:
        import requests
        log_message("INFO", "python3-requests is installed")
        has_requests = True
    except ImportError:
        log_message("INFO", "python3-requests is not installed. ntfy notifications disabled", exit_on_error=False)

    if requires_i2c:
        try:
            import smbus
            log_message("INFO", "python3-smbus is installed")
            try:
                bus = smbus.SMBus(1)
                bus.read_byte(I2C_ADDR)
                log_message("INFO", "smbus module is functional")
            except Exception as e:
                log_message("ERROR", f"smbus module failed to access I2C bus: {e}")
                return smbus, None, has_requests
        except ImportError:
            log_message("ERROR", "python3-smbus is not installed. Please install it with 'sudo apt install python3-smbus'")
            return None, None, has_requests

    try:
        import gpiod
        log_message("INFO", "python3-gpiod is installed")
    except ImportError:
        log_message("ERROR", "python3-gpiod is not installed. Please install it with 'sudo apt install python3-gpiod'")
        return smbus, bus, has_requests

    if os.path.exists("/usr/bin/vcgencmd"):
        log_message("INFO", "libraspberrypi-bin is installed")
    else:
        log_message("WARNING", "libraspberrypi-bin not installed, GPU temp unavailable", exit_on_error=False)

    return smbus, bus, has_requests

def enable_i2c():
    
    config_file = "/boot/firmware/config.txt"
    log_message("INFO", f"Checking I2C status in {config_file}")
    try:
        with open(config_file, "r") as f:
            if "dtparam=i2c_arm=on" not in f.read():
                log_message("ERROR", f"I2C is not enabled in {config_file}. Please enable it with 'sudo raspi-config'")
        log_message("INFO", f"I2C is enabled in {config_file}")
    except FileNotFoundError:
        log_message("ERROR", f"{config_file} not found. Please enable I2C manually")

    result = subprocess.run(["lsmod"], capture_output=True, text=True)
    if "i2c_dev" not in result.stdout:
        log_message("ERROR", "i2c-dev module is not loaded. Please load it with 'sudo modprobe i2c-dev'")
    else:
        log_message("INFO", "i2c-dev module is loaded")

# Check dependencies for --help (no I2C/GPIO needed)
if "--help" in sys.argv or "-h" in sys.argv:
    _, _, HAS_REQUESTS = check_dependencies(requires_i2c=False)
else:
    smbus, bus, HAS_REQUESTS = check_dependencies(requires_i2c=True)
    enable_i2c()

try:
    import gpiod
except ImportError:
    if "--help" not in sys.argv and "-h" not in sys.argv:
        log_message("ERROR", "python3-gpiod is required but not installed. Please install it")
        sys.exit(1)

def get_time_remaining(battery_level):
    """
    Estimates the remaining battery life in hours, minutes, and seconds.
    Note: This is a linear estimate based on a full-charge run time of 7 hours,
    which assumes an average current draw of ~1000mA for a 7000mAh battery pack.
    A more accurate calculation would require real-time current draw data from
    the I2C chip, which is not read by this script.
    """
    # Total run time at 100% capacity in seconds (7 hours based on 7000mAh total capacity)
    TOTAL_RUN_TIME_SECONDS = 7 * 60 * 60
    
    remaining_seconds = (battery_level / 100.0) * TOTAL_RUN_TIME_SECONDS
    
    hours = int(remaining_seconds // 3600)
    minutes = int((remaining_seconds % 3600) // 60)
    seconds = int(remaining_seconds % 60)
    
    return f"{hours}h {minutes}m {seconds}s"


class X728Monitor:
    
    def __init__(self, enable_ntfy=False, ntfy_server="https://ntfy.sh", ntfy_topic="x728_UPS_TEST", low_battery_threshold=30, critical_low_threshold=10):
        """_summary_

        Args:
            enable_ntfy (bool, optional): _description_. Defaults to False.
            ntfy_server (str, optional): _description_. Defaults to "https://ntfy.sh".
            ntfy_topic (str, optional): _description_. Defaults to "x728_UPS_TEST".
            low_battery_threshold (int, optional): _description_. Defaults to 30.
            critical_low_threshold (int, optional): _description_. Defaults to 10.
        """        
        self.enable_ntfy = enable_ntfy
        self.ntfy_server = ntfy_server
        self.ntfy_topic = ntfy_topic
        self.low_battery_threshold = low_battery_threshold
        self.critical_low_threshold = critical_low_threshold
        self.is_unplugged = False
        self.last_notification = None
        self.notification_cooldown = timedelta(minutes=5)
        self.low_battery_notified = False
        self.shutdown_timer_active = False
        self.notification_queue = []

        # GPIO setup
        self.chip = None
        self.line = None
        self.out_line = None
        try:
            self.chip = gpiod.Chip(chipname)
            self.line = self.chip.get_line(line_offset)
            self.line.request(consumer="x728_ups_monitor", type=gpiod.LINE_REQ_EV_BOTH_EDGES)
            self.out_line = self.chip.get_line(out_line_offset)
            self.out_line.request(consumer="x728_ups_monitor", type=gpiod.LINE_REQ_DIR_OUT)
        except Exception as e:
            log_message("ERROR", f"Failed to initialize GPIO: {e}")
            raise

        # Check initial power state
        self.check_initial_power_state()

    def check_initial_power_state(self):
        try:
            power_state = self.line.get_value()
            if power_state == 1:
                self.is_unplugged = True
                with i2c_lock:
                    battery_level = self.read_battery_level()
                    voltage = self.read_voltage()
                time_remaining = get_time_remaining(battery_level)
                log_message("WARNING", f"{RED}System started on battery power. Battery level: {battery_level:.1f}% ({time_remaining}){ENDC}")
                if self.enable_ntfy:
                    self.send_ntfy_notification("power_loss", battery_level, voltage)
                if battery_level < self.low_battery_threshold:
                    self.handle_low_battery(battery_level, voltage)
            else:
                self.is_unplugged = False
                log_message("INFO", "System started on AC power")
        except Exception as e:
            log_message("ERROR", f"Failed to check initial power state: {e}", exit_on_error=False)

    def read_battery_level(self):
        
        if bus is None:
            log_message("ERROR", "I2C bus is not available. Cannot read battery level", exit_on_error=False)
            return 0
        for attempt in range(3):
            try:
                read = bus.read_word_data(I2C_ADDR, 4)
                swapped = struct.unpack("<H", struct.pack(">H", read))[0]
                log_message("DEBUG", f"Raw battery level data: read={read}, swapped={swapped}")
                capacity = min(swapped / 256.0, 100.0)  # Clamp to 100%
                if 0 <= capacity <= 100:
                    return capacity
                else:
                    log_message("WARNING", f"Invalid battery level read: {capacity}. Retrying...", exit_on_error=False)
            except Exception as e:
                log_message("ERROR", f"Failed to read battery level (attempt {attempt + 1}/3): {e}", exit_on_error=False)
            time.sleep(0.1)
        log_message("ERROR", "Failed to read valid battery level after 3 attempts", exit_on_error=False)
        return 0

    def read_voltage(self):
        if bus is None:
            log_message("ERROR", "I2C bus is not available. Cannot read voltage", exit_on_error=False)
            return 0
        for attempt in range(3):
            try:
                read = bus.read_word_data(I2C_ADDR, 2)
                swapped = struct.unpack("<H", struct.pack(">H", read))[0]
                log_message("DEBUG", f"Raw voltage data: read={read}, swapped={swapped}")
                voltage = swapped * 1.25 / 1000 / 16
                if 2.5 <= voltage <= 5.5:
                    return voltage
                else:
                    log_message("WARNING", f"Invalid voltage read: {voltage}. Retrying...", exit_on_error=False)
            except Exception as e:
                log_message("ERROR", f"Failed to read voltage (attempt {attempt + 1}/3): {e}", exit_on_error=False)
            time.sleep(0.1)
        log_message("ERROR", "Failed to read valid voltage after 3 attempts", exit_on_error=False)
        return 0

    @staticmethod
    def get_cpu_temp():
        try:
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                temp = float(f.read()) / 1000.0
                return temp
        except Exception:
            return None

    @staticmethod
    def get_gpu_temp():
        try:
            result = subprocess.run(['vcgencmd', 'measure_temp'], capture_output=True, text=True)
            temp_str = result.stdout.strip().split('=')[1].split("'")[0]
            return float(temp_str)
        except Exception:
            return None

    @staticmethod
    def get_hostname():
        try:
            return socket.gethostname()
        except Exception:
            return "Unknown"

    @staticmethod
    def get_ip_address():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "Unknown"

    @staticmethod
    def get_pi_model():
        try:
            with open('/sys/firmware/devicetree/base/model', 'r') as f:
                return f.read().strip()
        except Exception:
            return "Unknown"

    @staticmethod
    def get_free_ram():
        try:
            result = subprocess.run(['free', '-m'], capture_output=True, text=True)
            lines = result.stdout.split('\n')
            mem_line = [line for line in lines if line.startswith('Mem:')][0]
            free_mem = int(mem_line.split()[3])
            return free_mem
        except Exception:
            return "Unknown"

    @staticmethod
    def get_uptime():
        try:
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.read().split()[0])
            days = int(uptime_seconds // (24 * 3600))
            hours = int((uptime_seconds % (24 * 3600)) // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            return f"{days}d {hours}h {minutes}m"
        except Exception:
            return "Unknown"

    def send_ntfy_notification(self, event_type, battery_level, voltage):
        """_summary_

        Args:
            event_type (_type_): _description_
            battery_level (_type_): _description_
            voltage (_type_): _description_
        """
        if not self.enable_ntfy or not HAS_REQUESTS:
            log_message("INFO", f"ntfy notification ({event_type}) skipped: {'ntfy disabled' if not self.enable_ntfy else 'python3-requests not installed'}", exit_on_error=False)
            return
        current_time = datetime.now()
        if event_type in ["critical_battery", "shutdown_initiated"] or self.last_notification is None or (current_time - self.last_notification) >= self.notification_cooldown:
            try:
                import requests
                hostname = self.get_hostname()
                ip = self.get_ip_address()
                cpu_temp = self.get_cpu_temp()
                gpu_temp = self.get_gpu_temp()
                est_time_remaining = self.calculate_estimated_run_time(battery_level)
                temp_info = (
                    f"CPU: {cpu_temp:.1f}°C, GPU: {gpu_temp:.1f}°C"
                    if cpu_temp is not None and gpu_temp is not None
                    else "Temp unavailable"
                )
                message = None
                title = None
                tags = []
                if event_type == "power_loss":
                    message = f"⚠️🔌 AC Power Loss on {hostname} (IP: {ip}): Battery at {battery_level:.1f}% ({voltage:.3f}V), Est Time Remaining: {est_time_remaining}, Temps: {temp_info}"
                    title = "x728 UPS Power Loss"
                    self.is_unplugged = True
                elif event_type == "power_restored":
                    message = f"✅🔋 AC Power Restored on {hostname} (IP: {ip}): Battery at {battery_level:.1f}% ({voltage:.3f}V), Temps: {temp_info}"
                    title = "x728 UPS Power Restored"
                    self.is_unplugged = False
                    self.low_battery_notified = False
                    self.shutdown_timer_active = False
                elif event_type == "low_battery" and self.is_unplugged:
                    message = f"🪫 Low Battery Alert on {hostname} (IP: {ip}): {battery_level:.1f}% ({voltage:.3f}V,Est Time Remaining: {est_time_remaining} Threshold: {self.low_battery_threshold}%), Temps: {temp_info}"
                    title = "x728 UPS Low Battery"
                    tags.append("low_battery")
                elif event_type == "critical_battery":                    
                    message = f"🚨 Critical Battery Alert on {hostname} (IP: {ip}): {battery_level:.1f}% ({voltage:.3f}V, Critical Threshold: {self.critical_low_threshold}%), Temps: {temp_info}.\n\n60 secs is commencing. Please reconnect to stop shutdown."
                    title = "x728 UPS Critical Battery"
                    tags.append("critical_battery")
                elif event_type == "shutdown_initiated":
                    message = f"🔴 Shutdown Initiated on {hostname} (IP: {ip}): {battery_level:.1f}% ({voltage:.3f}V), Temps: {temp_info}. Shutting down via GPIO."
                    title = "x728 UPS Shutdown Initiated"
                    tags.append("shutdown_initiated")
                    tags.append("critical_battery")
                elif event_type == "test":
                    message = f"🌟 x728 UPS Test Notification\nHostname: {hostname}\nIP: {ip}\nModel: {self.get_pi_model()}\nFree RAM: {self.get_free_ram()} MB\nUptime: {self.get_uptime()}\n◕‿◕"
                    title = "x728 UPS Test Alert"
                if message and title:
                    headers = {"Title": title.encode('utf-8')}
                    if tags:
                        headers["Tags"] = ",".join(tags)
                    response = requests.post(
                        f"{self.ntfy_server}/{self.ntfy_topic}",
                        data=message.encode('utf-8'),
                        headers=headers
                    )
                    if response.status_code == 200:
                        log_message("INFO", f"Notification sent successfully: {message}")
                        self.last_notification = current_time
                        if event_type == "low_battery":
                            self.low_battery_notified = True
                    else:
                        log_message("WARNING", f"Failed to send notification: HTTP {response.status_code} - {response.text}", exit_on_error=False)
            except requests.exceptions.RequestException as e:
                log_message("WARNING", f"Failed to send notification: Network error - {e}", exit_on_error=False)
            except Exception as e:
                log_message("WARNING", f"Failed to send notification: Unexpected error - {e}", exit_on_error=False)
        else:
            if event_type in ["power_loss", "power_restored"]:
                self.notification_queue.append((current_time, event_type, battery_level, voltage))
                log_message("INFO", f"Notification ({event_type}) queued due to cooldown. Queue size: {len(self.notification_queue)}")
            else:
                log_message("INFO", f"Notification ({event_type}) skipped due to cooldown. Will send after {self.notification_cooldown.total_seconds() - (current_time - self.last_notification).total_seconds():.1f} seconds")

    def process_notification_queue(self):
        if not self.enable_ntfy or not HAS_REQUESTS or not self.notification_queue or self.last_notification is None:
            return
        current_time = datetime.now()
        if (current_time - self.last_notification) >= self.notification_cooldown:
            self.notification_queue.sort(key=lambda x: x[0], reverse=True)
            events_to_send = self.notification_queue[:2]
            extra_events = len(self.notification_queue) - 2 if len(self.notification_queue) > 2 else 0
            for i, (timestamp, event_type, battery_level, voltage) in enumerate(events_to_send):
                try:
                    import requests
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
                    if event_type == "power_loss":
                        time_remaining = get_time_remaining(battery_level)
                        message = f"⚠️🔌 AC Power Loss on {hostname} (IP: {ip}): Battery at {battery_level:.1f}% ({voltage:.3f}V), Est. Time Remaining: {time_remaining}, Temps: {temp_info}"
                        title = "x728 UPS Power Loss"
                    elif event_type == "power_restored":
                        message = f"✅🔋 AC Power Restored on {hostname} (IP: {ip}): Battery at {battery_level:.1f}% ({voltage:.3f}V), Temps: {temp_info}"
                        title = "x728 UPS Power Restored"
                    if extra_events > 0 and i == 1:
                        message += f"\n...+{extra_events} similar events, please check journal"
                    if message and title:
                        response = requests.post(
                            f"{self.ntfy_server}/{self.ntfy_topic}",
                            data=message.encode('utf-8'),
                            headers={"Title": title.encode('utf-8')}
                        )
                        if response.status_code == 200:
                            log_message("INFO", f"Queued notification sent successfully: {message}")
                            self.last_notification = current_time
                        else:
                            log_message("WARNING", f"Failed to send queued notification: HTTP {response.status_code} - {response.text}", exit_on_error=False)
                except requests.exceptions.RequestException as e:
                    log_message("WARNING", f"Failed to send queued notification: Network error - {e}", exit_on_error=False)
                except Exception as e:
                    log_message("WARNING", f"Failed to send queued notification: Unexpected error - {e}", exit_on_error=False)
            self.notification_queue = []
            log_message("INFO", "Notification queue cleared")

    def handle_low_battery(self, battery_level, voltage):
        if not self.low_battery_notified:
            time_remaining = get_time_remaining(battery_level)
            log_message("WARNING", f"{RED}Battery level is low ({battery_level:.1f}%). Est. Time Remaining: {time_remaining}. Monitoring for critical threshold...{ENDC}")
            if self.enable_ntfy:
                self.send_ntfy_notification("low_battery", battery_level, voltage)

    def start_shutdown_timer(self, battery_level, voltage):
        self.shutdown_timer_active = True
        time_remaining = get_time_remaining(battery_level)
        log_message("WARNING", f"{RED}Battery level is critically low ({battery_level:.1f}%). Est. Time Remaining: {time_remaining}. Waiting {RECONNECT_TIMEOUT} seconds for power restoration...{ENDC}")
        if self.enable_ntfy:
            self.send_ntfy_notification("critical_battery", battery_level, voltage)
        start_time = time.time()
        while time.time() - start_time < RECONNECT_TIMEOUT:
            time_remaining = RECONNECT_TIMEOUT - (time.time() - start_time)
            power_state = self.line.get_value()
            if power_state == 0:
                with i2c_lock:
                    battery_level = self.read_battery_level()
                    voltage = self.read_voltage()
                log_message("INFO", f"{GREEN}---AC Power Restored---{ENDC}")
                log_message("INFO", f"Battery level: {battery_level:.1f}%, Voltage: {voltage:.3f}V")
                log_message("INFO", "Shutdown timer cancelled due to AC power restoration")
                if self.enable_ntfy:
                    self.send_ntfy_notification("power_restored", battery_level, voltage)
                self.shutdown_timer_active = False
                return
            with i2c_lock:
                current_battery_level = self.read_battery_level()
                current_voltage = self.read_voltage()
            time_remaining = get_time_remaining(current_battery_level)
            log_message("INFO", f"Shutdown timer: {time_remaining:.1f}s remaining, Battery: {current_battery_level:.1f}%, Voltage: {current_voltage:.3f}V, Est. Time Remaining: {time_remaining}")
            time.sleep(1)
        if self.line.get_value() == 1:
            with i2c_lock:
                current_battery_level = self.read_battery_level()
                current_voltage = self.read_voltage()
            self.shutdown_sequence(current_battery_level, current_voltage)
        self.shutdown_timer_active = False

    def shutdown_sequence(self, battery_level, voltage):
        log_message("WARNING", f"{RED}SHUTDOWN SEQUENCE COMMENCING...{ENDC}")
        if self.enable_ntfy:
            self.send_ntfy_notification("shutdown_initiated", battery_level, voltage)
        try:
            self.out_line.set_value(1)
            log_message("INFO", "Shutdown signal sent via GPIO 13. System shutting down...")
            while True:
                with i2c_lock:
                    current_battery_level = self.read_battery_level()
                    current_voltage = self.read_voltage()
                time_remaining = get_time_remaining(current_battery_level)
                log_message("INFO", f"System shutting down, Battery: {current_battery_level:.1f}%, Voltage: {current_voltage:.3f}V, Est. Time Remaining: {time_remaining}")
                time.sleep(2)
        except Exception as e:
            log_message("ERROR", f"Shutdown sequence failed: {e}", exit_on_error=False)

    def close(self):
        try:
            if hasattr(self, 'line') and self.line:
                self.line.release()
            if hasattr(self, 'out_line') and self.out_line:
                self.out_line.release()
            if hasattr(self, 'chip') and self.chip:
                self.chip.close()
            log_message("INFO", "GPIO resources released successfully")
        except Exception as e:
            log_message("ERROR", f"Failed to close resources: {e}", exit_on_error=False)

def test_ntfy(ntfy_server, ntfy_topic):
    log_message("INFO", "Testing ntfy connectivity")
    monitor = X728Monitor(enable_ntfy=True, ntfy_server=ntfy_server, ntfy_topic=ntfy_topic)
    try:
        monitor.send_ntfy_notification("test", 0, 0)
    finally:
        monitor.close()

def sample_x728(monitor):
    """Main sampling loop to monitor the X728 HAT status."""
    while True:
        try:
            battery_level = monitor.read_battery_level()
            voltage = monitor.read_voltage()
            
            log_message("INFO", f"Battery level: {battery_level:.1f}%, Voltage: {voltage:.3f}V")

            if monitor.is_unplugged and not monitor.shutdown_timer_active:
                if battery_level <= monitor.critical_low_threshold:
                    log_message("WARNING", f"Critical low battery threshold ({monitor.critical_low_threshold}%) reached. Starting shutdown timer...")
                    monitor.start_shutdown_timer()
                    monitor.send_ntfy_notification("critical_battery", battery_level, voltage)
                elif battery_level <= monitor.low_battery_threshold and not monitor.low_battery_notified:
                    log_message("WARNING", f"Low battery threshold ({monitor.low_battery_threshold}%) reached. Sending alert.")
                    monitor.send_ntfy_notification("low_battery", battery_level, voltage)
            
            # Check for power state changes
            is_ac_power = monitor.is_ac_power_connected()
            if is_ac_power and monitor.is_unplugged:
                log_message("INFO", "AC power restored. Resetting shutdown timer.")
                monitor.cancel_shutdown_timer()
                monitor.send_ntfy_notification("power_restored", battery_level, voltage)
            elif not is_ac_power and not monitor.is_unplugged:
                log_message("WARNING", "AC power lost!")
                monitor.send_ntfy_notification("power_loss", battery_level, voltage)

            # Check for queued notifications to be sent after cooldown
            if monitor.notification_queue:
                current_time = datetime.now()
                if monitor.last_notification is None or (current_time - monitor.last_notification) >= monitor.notification_cooldown:
                    _time, _event, _bat, _volt = monitor.notification_queue.pop(0)
                    monitor.send_ntfy_notification(_event, _bat, _volt)
            
            time.sleep(monitor.sleep_interval)

        except Exception as e:
            log_message("ERROR", f"Sampling error: {e}. Retrying in {monitor.sleep_interval}s...", exit_on_error=False)
            time.sleep(monitor.sleep_interval)

def pld_event(monitor, event):
    try:
        for attempt in range(3):
            try:
                with i2c_lock:
                    battery_level = monitor.read_battery_level()
                    voltage = monitor.read_voltage()
                break
            except Exception as e:
                log_message("ERROR", f"GPIO event I2C read failed (attempt {attempt + 1}/3): {e}", exit_on_error=False)
                if attempt < 2:
                    time.sleep(0.1)
                else:
                    battery_level, voltage = 0, 0
                    break
        if event.type == gpiod.LineEvent.RISING_EDGE:
            monitor.is_unplugged = True
            time_remaining = get_time_remaining(battery_level)
            log_message("WARNING", f"{RED}---AC Power Loss OR Power Adapter Failure---{ENDC}")
            log_message("INFO", f"Battery level: {battery_level:.1f}%, Voltage: {voltage:.3f}V, Est. Time Remaining: {time_remaining}")
            if monitor.enable_ntfy:
                monitor.send_ntfy_notification("power_loss", battery_level, voltage)
            if battery_level < monitor.low_battery_threshold:
                monitor.handle_low_battery(battery_level, voltage)
            if battery_level < monitor.critical_low_threshold and not monitor.shutdown_timer_active:
                monitor.start_shutdown_timer(battery_level, voltage)
            monitor.out_line.set_value(0)
        elif event.type == gpiod.LineEvent.FALLING_EDGE:
            monitor.is_unplugged = False
            monitor.low_battery_notified = False
            monitor.shutdown_timer_active = False
            log_message("INFO", f"{GREEN}---AC Power Restored---{ENDC}")
            log_message("INFO", f"Battery level: {battery_level:.1f}%, Voltage: {voltage:.3f}V")
            log_message("INFO", "Shutdown timer cancelled due to AC power restoration")
            if monitor.enable_ntfy:
                monitor.send_ntfy_notification("power_restored", battery_level, voltage)
            monitor.out_line.set_value(0)
        if sys.stdin.isatty():
            time_remaining = get_time_remaining(battery_level)
            log_message("INFO", f"Current state: {'Battery' if monitor.is_unplugged else 'AC Power'}, Battery level: {battery_level:.1f}%, Voltage: {voltage:.3f}V, Est. Time Remaining: {time_remaining}")
    except Exception as e:
        log_message("ERROR", f"GPIO event handling failed: {e}", exit_on_error=False)

def gpio_event_thread(monitor):
    while True:
        try:
            event = monitor.line.event_wait()
            if event:
                event = monitor.line.event_read()
                pld_event(monitor, event)
            time.sleep(0.01)
        except KeyboardInterrupt:
            log_message("INFO", "Exiting GPIO event thread...")
            break
        except Exception as e:
            log_message("ERROR", f"GPIO event thread error: {e}. Restarting thread...", exit_on_error=False)
            time.sleep(0.5)

def install_as_service(args):
    if os.geteuid() != 0:
        log_message("ERROR", "Service installation must be run as root")
    log_message("INFO", f"Installing x728 UPS monitor service for user {USER}")

    service_file = "/etc/systemd/system/presto_x728_ups.service"
    target_script = "/usr/local/bin/presto_x728_ups_monitor.py"
    service_exists = os.path.exists(service_file)
    service_running = False
    service_status = "unknown"

    # Check if service is installed and running
    if service_exists:
        log_message("WARNING", "x728_ups service is already installed")
        try:
            result = subprocess.run(["systemctl", "is-active", "--quiet", "x728_ups"], check=False)
            service_running = result.returncode == 0
            if service_running:
                log_message("WARNING", "x728_ups service is currently running")
            else:
                log_message("INFO", "x728_ups service is installed but not running")
            result = subprocess.run(["systemctl", "status", "presto_x728_ups.service"], capture_output=True, text=True)
            service_status = result.stdout
        except subprocess.CalledProcessError as e:
            log_message("WARNING", f"Failed to check service status: {e.stderr}", exit_on_error=False)

    # Prompt for reinstallation if service exists
    proceed = True
    if service_exists and sys.stdin.isatty():
        log_message("INFO", f"Current service status:\n{service_status}")
        log_message("INFO", f"New settings: enable-ntfy={args.enable_ntfy}, ntfy-server={args.ntfy_server}, ntfy-topic={args.ntfy_topic}, low-battery-threshold={args.low_battery_threshold}%%, critical-low-threshold={args.critical_low_threshold}%%")
        try:
            response = input("[x728-UPS-service] [INFO] Would you like to reinstall with new settings? (y/n): ").strip().lower()
            if response != 'y':
                log_message("INFO", "Installation aborted by user")
                sys.exit(0)
        except KeyboardInterrupt:
            log_message("INFO", "Installation aborted by user")
            sys.exit(0)

    # Stop, disable, and reset service to ensure clean state
    if service_exists:
        log_message("INFO", "Ensuring x728_ups service is stopped and reset")
        try:
            if service_running:
                subprocess.run(["systemctl", "stop", "presto_x728_ups.service"], check=True)
                log_message("INFO", "Service stopped successfully")
            subprocess.run(["systemctl", "disable", "presto_x728_ups.service"], check=True)
            log_message("INFO", "Service disabled successfully")
            # Check if service is loaded before resetting failed state
            result = subprocess.run(["systemctl", "is-active", "--quiet", "x728_ups"], check=False)
            if result.returncode == 0 or os.path.exists(service_file):
                try:
                    subprocess.run(["systemctl", "reset-failed", "presto_x728_ups.service"], check=True)
                    log_message("WARNING", "Service failed state reset successfully")
                except subprocess.CalledProcessError as e:
                    log_message("WARNING", f"Failed to reset failed state: {e.stderr}", exit_on_error=False)
            else:
                log_message("INFO", "Skipping reset-failed as service is not loaded")
            time.sleep(1)  # Wait for systemd to release resources
        except subprocess.CalledProcessError as e:
            log_message("ERROR", f"Failed to stop or disable service: {e.stderr}", exit_on_error=False)

    # Kill any residual processes
    try:
        ps_result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
        current_pid = str(os.getpid())
        for line in ps_result.stdout.splitlines():
            if "/usr/local/bin/presto_x728_ups_monitor.py" in line and current_pid not in line:
                pid = line.split()[1]
                log_message("INFO", f"Terminating residual x728_ups process: PID {pid}")
                subprocess.run(["kill", "-9", pid], check=True)
    except subprocess.CalledProcessError as e:
        log_message("WARNING", f"Failed to terminate residual processes: {e.stderr}", exit_on_error=False)

    # Copy script to target location
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

    # Create service file with new parameters
    exec_start = f"/usr/bin/python3 {target_script} --low-battery-threshold {args.low_battery_threshold} --critical-low-threshold {args.critical_low_threshold}"
    if args.enable_ntfy:
        exec_start += f" --enable-ntfy --ntfy-server {args.ntfy_server} --ntfy-topic {args.ntfy_topic}"
    if args.debug:
        exec_start += " --debug"
    service_file_content = f"""[Unit]
Description=Presto x728 UPS Monitor Service
After=network.target

[Service]
ExecStart={exec_start}
Restart=always
RestartSec=5
User={USER}
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
    try:
        with open(service_file, "w") as f:
            f.write(service_file_content)
        subprocess.run(["chmod", "644", service_file], check=True)
        log_message("INFO", f"Service file created at {service_file}")
    except Exception as e:
        log_message("ERROR", f"Failed to create service file: {e}")

    # Reload, enable, and start service
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        log_message("INFO", "Systemd daemon reloaded successfully")
        subprocess.run(["systemctl", "enable", "presto_x728_ups.service"], check=True)
        log_message("INFO", "Service enabled successfully")
        subprocess.run(["systemctl", "start", "presto_x728_ups.service"], check=True)
        log_message("INFO", "Service started successfully")
    except subprocess.CalledProcessError as e:
        log_message("ERROR", f"Failed to manage service: {e.stderr}")

    # Display service status
    try:
        result = subprocess.run(["systemctl", "is-active", "x728_ups"], capture_output=True, text=True, check=True)
        active_status = result.stdout.strip()
        log_message("INFO", f"Service active status: {active_status}")
        result = subprocess.run(["systemctl", "status", "presto_x728_ups.service"], capture_output=True, text=True)
        log_message("INFO", f"Service detailed status:\n{result.stdout}")
    except subprocess.CalledProcessError as e:
        log_message("ERROR", f"Failed to retrieve service status: {e.stderr}", exit_on_error=False)

    # Display management tips
    log_message("INFO", "Service Management Tips:")
    log_message("INFO", "  - Check recent battery/voltage logs: journalctl -u presto_x728_ups.service | grep -E \"Battery level|Voltage\" -m 10")
    log_message("INFO", "  - Check power events: journalctl -u presto_x728_ups.service | grep -E \"Power Loss|Power Restored|Shutdown\" -m 10")
    log_message("INFO", "  - Check critical errors: journalctl -u presto_x728_ups.service -p 0..3 -n 10")
    log_message("INFO", "  - Check debug logs (if enabled): journalctl -u presto_x728_ups.service | grep DEBUG -m 10")
    log_message("INFO", "  - Uninstall service: sudo {} --uninstall".format(os.path.basename(__file__)))
    reinstall_cmd = f"sudo {os.path.basename(__file__)} --install_as_service --low-battery-threshold {args.low_battery_threshold} --critical-low-threshold {args.critical_low_threshold}"
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
    log_message("INFO", f"Uninstalling x728 UPS monitor service for user {USER}")

    service_file = "/etc/systemd/system/presto_x728_ups.service"
    target_script = "/usr/local/bin/presto_x728_ups_monitor.py"
    service_exists = os.path.exists(service_file)
    service_running = False
    service_status = "unknown"

    # Check if service is installed and running
    if service_exists:
        try:
            result = subprocess.run(["systemctl", "is-active", "--quiet", "x728_ups"], check=False)
            service_running = result.returncode == 0
            if service_running:
                log_message("WARNING", "x728_ups service is currently running")
            else:
                log_message("INFO", "x728_ups service is installed but not running")
            result = subprocess.run(["systemctl", "status", "presto_x728_ups.service"], capture_output=True, text=True)
            service_status = result.stdout
        except subprocess.CalledProcessError as e:
            log_message("WARNING", f"Failed to check service status: {e.stderr}", exit_on_error=False)
    else:
        log_message("INFO", "x728_ups service is not installed")
        sys.exit(0)

    # Prompt for uninstallation if service exists
    if service_exists and sys.stdin.isatty():
        log_message("INFO", f"Current service status:\n{service_status}")
        try:
            response = input("[x728-UPS-service] [INFO] Would you like to uninstall the x728_ups service? (y/n): ").strip().lower()
            if response != 'y':
                log_message("INFO", "Uninstallation aborted by user")
                sys.exit(0)
        except KeyboardInterrupt:
            log_message("INFO", "Uninstallation aborted by user")
            sys.exit(0)

    # Stop and disable service if running
    if service_running:
        log_message("INFO", "Stopping x728_ups service")
        try:
            subprocess.run(["systemctl", "stop", "presto_x728_ups.service"], check=True)
            log_message("INFO", "Service stopped successfully")
        except subprocess.CalledProcessError as e:
            log_message("ERROR", f"Failed to stop service: {e.stderr}", exit_on_error=False)
        try:
            subprocess.run(["systemctl", "disable", "presto_x728_ups.service"], check=True)
            log_message("INFO", "Service disabled successfully")
        except subprocess.CalledProcessError as e:
            log_message("ERROR", f"Failed to disable service: {e.stderr}", exit_on_error=False)

    # Remove service file
    if service_exists:
        try:
            subprocess.run(["rm", "-f", service_file], check=True)
            log_message("INFO", f"Service file removed: {service_file}")
        except subprocess.CalledProcessError as e:
            log_message("ERROR", f"Failed to remove service file: {e.stderr}", exit_on_error=False)

    # Reload systemd daemon
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        log_message("INFO", "Systemd daemon reloaded successfully")
    except subprocess.CalledProcessError as e:
        log_message("ERROR", f"Failed to reload systemd daemon: {e.stderr}", exit_on_error=False)

    # Optionally, remove script
    if os.path.exists(target_script):
        try:
            subprocess.run(["rm", "-f", target_script], check=True)
            log_message("INFO", f"Script removed: {target_script}")
        except subprocess.CalledProcessError as e:
            log_message("ERROR", f"Failed to remove script: {e.stderr}", exit_on_error=False)

    log_message("INFO", "Uninstallation complete. You can now run the script interactively.")
    sys.exit(0)

def main():
    parser = argparse.ArgumentParser(
        description=f"x728 UPS HAT Monitor with Service Installation (Version {SCRIPT_VERSION})",
        epilog=f"""
Useful journalctl commands for monitoring:
  - Recent battery/voltage logs: journalctl -u presto_x728_ups.service | grep -E "Battery level|Voltage" -m 10
  - Power event logs: journalctl -u presto_x728_ups.service | grep -E "Power Loss|Power Restored|Shutdown" -m 10
  - Critical errors: journalctl -u presto_x728_ups.service -p 0..3 -n 10
  - Debug logs (if --debug enabled): journalctl -u presto_x728_ups.service | grep DEBUG -m 10
""",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('-i', '--install_as_service', action="store_true", help="Install as a systemd service")
    parser.add_argument('-u', '--uninstall', action="store_true", help="Uninstall the x728_ups service")
    parser.add_argument('-t', '--test-ntfy', action="store_true", help="Send a test ntfy notification (requires --enable-ntfy)")
    parser.add_argument('-ntfy', '--enable-ntfy', action="store_true", help="Enable ntfy notifications (default: False)")
    parser.add_argument('-ns', '--ntfy-server', default="https://ntfy.sh", help="ntfy server URL (default: https://ntfy.sh)")
    parser.add_argument('-nt', '--ntfy-topic', default="x728_UPS", help="ntfy topic for notifications (default: x728_UPS)")
    parser.add_argument('--low-battery-threshold', type=float, default=LOW_BATTERY_THRESHOLD, help=f"Low battery threshold percentage (default: {LOW_BATTERY_THRESHOLD}%%)")
    parser.add_argument('--critical-low-threshold', type=float, default=CRITICAL_LOW_THRESHOLD, help=f"Critical low battery threshold percentage (default: {CRITICAL_LOW_THRESHOLD}%%)")
    parser.add_argument('-d','--debug', action="store_true", help="Enable debug logging for raw I2C data (default: False)")
    args = parser.parse_args()
    
    # Set global debug flag
    global DEBUG_ENABLED
    DEBUG_ENABLED = args.debug

    # Validate argument combinations
    if args.install_as_service and (args.uninstall or args.test_ntfy):
        log_message("ERROR", "Cannot use --install_as_service with --uninstall or --test-ntfy")
    if args.uninstall and args.test_ntfy:
        log_message("ERROR", "Cannot use --uninstall with --test-ntfy")
    if args.test_ntfy and not args.enable_ntfy:
        log_message("ERROR", "--test-ntfy requires --enable-ntfy")

    if args.low_battery_threshold <= args.critical_low_threshold:
        log_message("ERROR", f"Low battery threshold ({args.low_battery_threshold}%%) must be greater than critical low threshold ({args.critical_low_threshold}%%)")
    if not (0 <= args.low_battery_threshold <= 100) or not (0 <= args.critical_low_threshold <= 100):
        log_message("ERROR", "Battery thresholds must be between 0 and 100%%")

    # Check for running service if GPIO access is needed
    requires_gpio = not (args.install_as_service or args.uninstall or ("-h" in sys.argv or "--help" in sys.argv))
    if requires_gpio and check_service_running():
        log_message("ERROR", "The x728_ups service is running, which is using GPIO resources. Stop the service first with: sudo systemctl stop presto_x728_ups.service")

    if args.uninstall:
        uninstall_service()

    if args.test_ntfy:
        test_ntfy(args.ntfy_server, args.ntfy_topic)
        sys.exit(0)

    if args.install_as_service:
        install_as_service(args)

    if "-h" in sys.argv or "--help" in sys.argv:
        parser.print_help()
        sys.exit(0)

    if smbus is None or bus is None:
        log_message("ERROR", "Critical dependency smbus or I2C bus is missing. Exiting.")
        sys.exit(1)

    log_message("WARNING", f"CRITICAL_LOW_THRESHOLD is set to {args.critical_low_threshold}%% for testing.")

    monitor = X728Monitor(
        enable_ntfy=args.enable_ntfy,
        ntfy_server=args.ntfy_server,
        ntfy_topic=args.ntfy_topic,
        low_battery_threshold=args.low_battery_threshold,
        critical_low_threshold=args.critical_low_threshold
    )

    sampling_thread = threading.Thread(target=sample_x728, args=(monitor,), daemon=True)
    sampling_thread.start()

    gpio_thread = threading.Thread(target=gpio_event_thread, args=(monitor,), daemon=True)
    gpio_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log_message("INFO", "Exiting...")
    finally:
        monitor.close()

if __name__ == "__main__":
    main()