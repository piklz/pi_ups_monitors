#!/usr/bin/env python3
#
#      _____ ____  ___          _   ____  
#__  _|___  |___ \( _ )  __   _/ | |___ \ 
#\ \/ /  / /  __) / _ \  \ \ / / |   __) |
# >  <  / /  / __/ (_) |  \\ V /| |_ / __/ 
#/_/\_\/_/  |_____\___/    \_/ |_(_)_____|HW v1.2 HAT Battery Monitor for raspberry pi 3,4,5 [piOS/debian based OS]
#
# -----------------------------------------------
# x728 UPS Monitor Script
# Version: 1.5.1
# Author: piklz
# GitHub: https://github.com/piklz/pi_ups_monitor
# Description:
#   Monitors the x728 UPS HAT (v1.2, may work with others) on a Raspberry Pi.
#   Implements a dedicated I2C Sampler Thread to prevent service hang (v1.5.0 fix).
#
# Changelog:
#   Version 1.5.1 (2025-09-28):
#     - **CRITICAL TRANSITION FIX**: Added logic to the periodic check to initiate the shutdown countdown and send the critical notification when the battery level drops
#       below the critical threshold *while* the system is already running on battery power.(if x728 is powered up for first time on battery . it will check battery level and initiate shutdown if critical)
#       This ensures that the shutdown sequence is always initiated correctly, even if the battery level crosses the critical threshold during normal operation.
#     - Improved logging and notification clarity(journal).
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
I2C_SAMPLE_INTERVAL = 30 # Time in seconds between I2C samples for battery/voltage updates
LOW_BATTERY_THRESHOLD = 30
CRITICAL_LOW_THRESHOLD = 10
RECONNECT_TIMEOUT = 60
RED = '\033[91m'
GREEN = '\033[92m'
ENDC = '\033[0m'
SCRIPT_VERSION = "1.5.1"
# Define the chip and lines
chipname = "gpiochip0"
line_offset = 6
out_line_offset = 13

# I2C lock for thread safety (only used by the sampler thread)
i2c_lock = threading.Lock()

# Global debug flag
DEBUG_ENABLED = False

def log_message(log_level, console_message, log_file_message=None, exit_on_error=True):
    """Logs messages to the console and systemd-journald."""    
    if log_level == "DEBUG" and not DEBUG_ENABLED:
        return
    if log_file_message is None:
        log_file_message = console_message
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    journal_message = f"[{timestamp}] [x728-UPS-service] [{log_level}] {log_file_message}"
    valid_priorities = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
    priority = log_level.lower() if log_level.lower() in valid_priorities else "info"
    try:
        # Added a short timeout for systemd-cat to prevent potential service hang
        subprocess.run(
            ["systemd-cat", "-t", "x728_ups", "-p", priority],
            input=journal_message,
            text=True,
            check=True,
            timeout=5
        )
    except subprocess.CalledProcessError as e:
        # Fallback print to stderr if systemd-cat fails
        print(f"[x728-UPS-service] [ERROR] Failed to log to journald via systemd-cat: {e}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"[x728-UPS-service] [ERROR] systemd-cat timeout (5s) for message: {log_file_message}", file=sys.stderr)
    
    if sys.stdin.isatty():
        color = {"INFO": COL_INFO, "WARNING": COL_WARNING, "ERROR": COL_ERROR, "DEBUG": COL_INFO}.get(log_level, COL_NC)
        print(f"[x728-UPS-service] {color}[{log_level}]{COL_NC} {console_message}")
    if log_level == "ERROR" and exit_on_error:
        sys.exit(1)

def check_service_running():
    """Checks if the systemd service is already running."""    
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

# -------------------------------------------------------------
# Dependency Imports and Setup (Non-Blocking)
# -------------------------------------------------------------
smbus, bus, HAS_REQUESTS = None, None, False

# Try importing critical I2C and requests libraries
try:
    import smbus
    log_message("DEBUG", "smbus imported successfully.")
    try:
        bus = smbus.SMBus(1)
        # We don't attempt a dummy read here to prevent blocking initial startup.
        log_message("DEBUG", "I2C bus object created successfully.")
    except Exception:
        bus = None # I2C bus is not functional
        log_message("WARNING", "I2C bus not accessible (might not be enabled).", exit_on_error=False)
except ImportError:
    smbus = None
    log_message("WARNING", "python3-smbus not installed. I2C features disabled.", exit_on_error=False)

try:
    import requests
    HAS_REQUESTS = True
    log_message("DEBUG", "requests imported successfully.")
except ImportError:
    log_message("WARNING", "python3-requests not installed. ntfy notifications disabled.", exit_on_error=False)
    pass

try:
    import gpiod
    log_message("DEBUG", "gpiod imported successfully.")
except ImportError:
    # Only exit if not using --help and gpiod is needed for core logic
    if "--help" not in sys.argv and "-h" not in sys.argv:
        log_message("ERROR", "python3-gpiod is required but not installed. Exiting.", exit_on_error=True)
        sys.exit(1)

# -------------------------------------------------------------

def get_time_remaining(battery_level):
    """
    Estimates the remaining battery life in hours, minutes, and seconds.
    """
    # Total run time at 100% capacity in seconds (7 hours based on 7000mAh total capacity)
    TOTAL_RUN_TIME_SECONDS = 7 * 60 * 60
    
    remaining_seconds = (battery_level / 100.0) * TOTAL_RUN_TIME_SECONDS
    
    hours = int(remaining_seconds // 3600)
    minutes = int((remaining_seconds % 3600) // 60)
    seconds = int(remaining_seconds % 60)
    
    return f"{hours}h {minutes}m {seconds}s"

class X728Monitor:
    
    def __init__(self, enable_ntfy=False, ntfy_server="https://ntfy.sh", ntfy_topic="x728_UPS", low_battery_threshold=30, critical_low_threshold=10):
        """Initializes the monitor, GPIO, and power state."""        
        self.enable_ntfy = enable_ntfy
        self.ntfy_server = ntfy_server
        self.ntfy_topic = ntfy_topic
        self.low_battery_threshold = low_battery_threshold
        self.critical_low_threshold = critical_low_threshold
        
        # Power/State variables
        self.is_unplugged = False
        self.last_notification = None
        self.notification_cooldown = timedelta(minutes=5)
        self.low_battery_notified = False
        self.shutdown_at_time = None
        self.notification_queue = []

        # I2C Sampled State (updated by sampler thread)
        self._battery_level = 0.0
        self._voltage = 0.0
        self._i2c_ready = False # Flag indicating the sampler thread has a first successful read
        self._i2c_stop_event = threading.Event()
        
        # GPIO setup
        self.chip = None
        self.line = None
        self.out_line = None
        try:
            if 'gpiod' not in sys.modules:
                 raise ImportError("gpiod module failed to load.")
            self.chip = gpiod.Chip(chipname)
            self.line = self.chip.get_line(line_offset)
            self.line.request(consumer="x728_ups_monitor", type=gpiod.LINE_REQ_EV_BOTH_EDGES)
            self.out_line = self.chip.get_line(out_line_offset)
            self.out_line.request(consumer="x728_ups_monitor", type=gpiod.LINE_REQ_DIR_OUT)
            log_message("DEBUG", "GPIO initialized successfully.")
        except Exception as e:
            # Do not log here, as the main function's try/except handles the logging and graceful exit
            raise

        # Start the non-blocking I2C sampler thread
        self.i2c_thread = threading.Thread(target=self._i2c_sampler_thread, daemon=True)
        self.i2c_thread.start()

        # Check initial power state (will wait for first I2C sample)
        self.check_initial_power_state()

    def _direct_i2c_read(self, register_addr):
        """
        Performs a single, blocking I2C read from the bus with basic error handling.
        This should ONLY be called from the dedicated sampler thread.
        Returns the processed value or None on failure.
        """
        if bus is None:
            log_message("DEBUG", "I2C bus not available for direct read.", exit_on_error=False)
            return None
        
        try:
            read = bus.read_word_data(I2C_ADDR, register_addr)
            swapped = struct.unpack("<H", struct.pack(">H", read))[0]
            return swapped
        except Exception as e:
            log_message("DEBUG", f"Direct I2C read failed at 0x{register_addr:02X}: {e}", exit_on_error=False)
            return None

    def _sample_i2c_data(self):
        """
        Attempts to sample battery level and voltage up to 3 times.
        Updates internal state variables upon success.
        Returns True if a valid sample was obtained, False otherwise.
        """
        log_message("DEBUG", "Sampler thread attempting I2C read...", exit_on_error=False)
        for attempt in range(3):
            with i2c_lock:
                # Read Battery Level (Register 0x04)
                level_data = self._direct_i2c_read(0x04)
                # Read Voltage (Register 0x02)
                voltage_data = self._direct_i2c_read(0x02)
            
            if level_data is not None and voltage_data is not None:
                capacity = min(level_data / 256.0, 100.0)
                voltage = voltage_data * 1.25 / 1000 / 16
                
                # Basic sanity check
                if 0 <= capacity <= 100 and 2.5 <= voltage <= 5.5:
                    self._battery_level = capacity
                    self._voltage = voltage
                    self._i2c_ready = True
                    log_message("DEBUG", f"I2C Sample Success. Level: {capacity:.1f}%, Voltage: {voltage:.3f}V", exit_on_error=False)
                    return True
                else:
                    log_message("WARNING", f"I2C Sample Invalid Data (Level: {capacity:.1f}%, Voltage: {voltage:.3f}V). Retrying...", exit_on_error=False)
            
            time.sleep(0.1) # Short delay between retries
            
        log_message("WARNING", "I2C Sample Failed after 3 attempts.", exit_on_error=False)
        return False
        
    def _i2c_sampler_thread(self):
        """Dedicated thread to periodically sample I2C data without blocking core logic."""
        log_message("INFO", "I2C Sampler thread started.")
        while not self._i2c_stop_event.is_set():
            # If the sampler fails, it keeps the old value (default 0.0) and tries again after the interval.
            self._sample_i2c_data()
            self._i2c_stop_event.wait(I2C_SAMPLE_INTERVAL)
        log_message("INFO", "I2C Sampler thread stopped.")

    def read_battery_level(self):
        """Non-blocking accessor for the last sampled battery percentage."""
        return self._battery_level

    def read_voltage(self):
        """Non-blocking accessor for the last sampled voltage."""
        return self._voltage

    def check_initial_power_state(self):
        """Checks the power state at startup and logs/notifies as needed."""
        log_message("INFO", "First Run Check initiated.") 
        
        # Wait up to 5 seconds for the I2C sampler thread to get its first reading
        start_time = time.time()
        while not self._i2c_ready and time.time() - start_time < 5:
            log_message("DEBUG", "Waiting for first I2C sample...", exit_on_error=False)
            time.sleep(0.5)

        if not self._i2c_ready:
            log_message("ERROR", "I2C sampler failed to get a reading within 5 seconds. Using 0.0%% for initialization.", exit_on_error=False)

        try:
            power_state = self.line.get_value()
            battery_level = self.read_battery_level() # Non-blocking read
            voltage = self.read_voltage()             # Non-blocking read
            
            if power_state == 1:
                self.is_unplugged = True
                log_message("INFO", "Running on Battery. Checking levels now:") 
                
                time_remaining = get_time_remaining(battery_level)
                
                status = 'CRITICAL' if battery_level < self.critical_low_threshold else ('LOW' if battery_level < self.low_battery_threshold else 'NORMAL')
                log_message("WARNING", 
                            f"{RED}First Run Check: System started on BATTERY POWER. Status: {status}. Level: {battery_level:.1f}%% ({time_remaining}){ENDC}",
                            log_file_message=f"First Run Check: System started on BATTERY POWER. Status: {status}. Level: {battery_level:.1f}% ({time_remaining})")
                
                if self.enable_ntfy:
                    self.send_ntfy_notification("power_loss", battery_level, voltage)
                if battery_level < self.low_battery_threshold:
                    self.handle_low_battery(battery_level, voltage)
                if battery_level < self.critical_low_threshold:
                    self.shutdown_at_time = time.time() + RECONNECT_TIMEOUT
                    log_message("WARNING", f"{RED}Battery is critically low on startup. Initiating shutdown countdown...{ENDC}")
                    if self.enable_ntfy:
                        self.send_ntfy_notification("critical_battery", battery_level, voltage)
            else:
                self.is_unplugged = False
                log_message("INFO", "First Run Check: System started on AC power. Status: NORMAL.")
        except Exception as e:
            log_message("ERROR", f"Failed to check initial power state: {e}", exit_on_error=False)

    @staticmethod
    def get_time_remaining_static(battery_level):
        """Static wrapper for estimation."""
        return get_time_remaining(battery_level)

    @staticmethod
    def get_cpu_temp():
        """Returns the CPU temperature."""
        try:
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                temp = float(f.read()) / 1000.0
                return temp
        except Exception:
            return None

    @staticmethod
    def get_gpu_temp():
        """Returns the GPU temperature."""
        try:
            result = subprocess.run(['vcgencmd', 'measure_temp'], capture_output=True, text=True)
            temp_str = result.stdout.strip().split('=')[1].split("'")[0]
            return float(temp_str)
        except Exception:
            return None

    @staticmethod
    def get_hostname():
        """Returns the system hostname."""
        try:
            return socket.gethostname()
        except Exception:
            return "Unknown"

    @staticmethod
    def get_ip_address():
        """Returns the system IP address."""
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
        """Returns the Raspberry Pi model."""
        try:
            with open('/sys/firmware/devicetree/base/model', 'r') as f:
                return f.read().strip()
        except Exception:
            return "Unknown"

    @staticmethod
    def get_free_ram():
        """Returns the amount of free RAM in MB."""
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
        """Returns the system uptime."""
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
        """Sends a notification via ntfy."""
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
                est_time_remaining = self.get_time_remaining_static(battery_level)
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
                    self.shutdown_at_time = None
                elif event_type == "low_battery" and self.is_unplugged:
                    message = f"🪫 Low Battery Alert on {hostname} (IP: {ip}): {battery_level:.1f}% ({voltage:.3f}V, Est. Time Remaining: {est_time_remaining}, Threshold: {self.low_battery_threshold}%), Temps: {temp_info}"
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

    def handle_low_battery(self, battery_level, voltage):
        """Handles the low battery event."""
        if not self.low_battery_notified:
            time_remaining = self.get_time_remaining_static(battery_level)
            log_message("WARNING", f"{RED}Battery level is low ({battery_level:.1f}%%). Est. Time Remaining: {time_remaining}. Monitoring for critical threshold...{ENDC}")
            if self.enable_ntfy:
                self.send_ntfy_notification("low_battery", battery_level, voltage)
    
    def shutdown_sequence(self, battery_level, voltage):
        """
        Initiates the system shutdown process.
        """
        log_message("WARNING", f"{RED}SHUTDOWN SEQUENCE COMMENCING...{ENDC}")
        if self.enable_ntfy:
            self.send_ntfy_notification("shutdown_initiated", battery_level, voltage)
        try:
            self.out_line.set_value(1)
            log_message("INFO", "Shutdown signal sent via GPIO 13. System shutting down...")
            subprocess.run(["sudo", "shutdown", "-h", "now"], check=True)
            sys.exit(0)
        except Exception as e:
            log_message("ERROR", f"Shutdown sequence failed: {e}", exit_on_error=False)

    def close(self):
        """Releases resources."""
        self._i2c_stop_event.set() # Stop the sampler thread
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

def pld_event(monitor, event):
    """
    Handles GPIO power loss/restoration events.
    Uses non-blocking accessors for battery/voltage.
    """
    try:
        battery_level = monitor.read_battery_level()
        voltage = monitor.read_voltage()

        if event.type == gpiod.LineEvent.RISING_EDGE:
            monitor.is_unplugged = True
            time_remaining = monitor.get_time_remaining_static(battery_level)
            log_message("WARNING", f"{RED}---AC Power Loss OR Power Adapter Failure---{ENDC}")
            log_message("INFO", f"Battery level: {battery_level:.1f}%%, Voltage: {voltage:.3f}V, Est. Time Remaining: {time_remaining}")
            if monitor.enable_ntfy:
                monitor.send_ntfy_notification("power_loss", battery_level, voltage)
            if battery_level < monitor.low_battery_threshold:
                monitor.handle_low_battery(battery_level, voltage)
            if battery_level < monitor.critical_low_threshold:
                monitor.shutdown_at_time = time.time() + RECONNECT_TIMEOUT
                log_message("WARNING", f"{RED}Battery level is critically low ({monitor.critical_low_threshold}%%) starting shutdown countdown...{ENDC}")
                if monitor.enable_ntfy:
                    monitor.send_ntfy_notification("critical_battery", battery_level, voltage)
            monitor.out_line.set_value(0)
        elif event.type == gpiod.LineEvent.FALLING_EDGE:
            monitor.is_unplugged = False
            monitor.low_battery_notified = False
            monitor.shutdown_at_time = None # Cancel shutdown timer
            log_message("INFO", f"{GREEN}---AC Power Restored---{ENDC}")
            log_message("INFO", f"Battery level: {battery_level:.1f}%%, Voltage: {voltage:.3f}V")
            log_message("INFO", "Shutdown timer cancelled due to AC power restoration")
            if monitor.enable_ntfy:
                monitor.send_ntfy_notification("power_restored", battery_level, voltage)
            monitor.out_line.set_value(0)

        if sys.stdin.isatty():
            time_remaining = monitor.get_time_remaining_static(battery_level)
            log_message("INFO", f"Current state: {'Battery' if monitor.is_unplugged else 'AC Power'}, Battery level: {battery_level:.1f}%%, Voltage: {voltage:.3f}V, Est. Time Remaining: {time_remaining}")
    except Exception as e:
        log_message("ERROR", f"GPIO event handling failed: {e}", exit_on_error=False)

def gpio_and_shutdown_thread(monitor):
    """
    Thread to listen for GPIO events and handle the shutdown countdown/periodic logging.
    This thread is non-blocking with respect to I2C.
    """
    last_periodic_log = time.time()
    
    while True:
        try:
            # 1. Check for a pending shutdown
            if monitor.shutdown_at_time and time.time() >= monitor.shutdown_at_time:
                battery_level = monitor.read_battery_level()
                voltage = monitor.read_voltage()
                monitor.shutdown_sequence(battery_level, voltage) # This will exit the script

            # 2. Read GPIO events (Blocking wait with timeout)
            event = monitor.line.event_wait(1) # Wait for 1 second
            if event:
                event = monitor.line.event_read()
                pld_event(monitor, event)
            
            # 3. Periodic status check/logging (every 60 seconds)
            if time.time() - last_periodic_log >= 60:
                battery_level = monitor.read_battery_level()
                voltage = monitor.read_voltage()
                log_message("INFO", f"Periodic Status: Battery level: {battery_level:.1f}%%, Voltage: {voltage:.3f}V")
                
                # Check low battery condition again if running on battery
                if monitor.is_unplugged:
                    if battery_level <= monitor.low_battery_threshold:
                        monitor.handle_low_battery(battery_level, voltage)

                    # *** NEW CRITICAL TRANSITION CHECK (v1.5.1 fix) ***
                    # If battery is critical AND shutdown timer has NOT been initiated, start the countdown.
                    if battery_level <= monitor.critical_low_threshold and monitor.shutdown_at_time is None:
                        monitor.shutdown_at_time = time.time() + RECONNECT_TIMEOUT
                        log_message("WARNING", f"{RED}Periodic Check: Battery dropped to critical level ({battery_level:.1f}%%). Initiating shutdown countdown...{ENDC}")
                        if monitor.enable_ntfy:
                            monitor.send_ntfy_notification("critical_battery", battery_level, voltage)
                    # **************************************************
                
                last_periodic_log = time.time()
            
        except KeyboardInterrupt:
            log_message("INFO", "Exiting GPIO event thread...")
            break
        except Exception as e:
            log_message("ERROR", f"GPIO event thread error: {e}. Restarting loop...", exit_on_error=False)
            time.sleep(1)


# --- Installation and Uninstallation functions remain the same as 1.4.3 ---

def test_ntfy(ntfy_server, ntfy_topic):
    log_message("INFO", "Testing ntfy connectivity")
    try:
        monitor = X728Monitor(enable_ntfy=True, ntfy_server=ntfy_server, ntfy_topic=ntfy_topic)
        monitor.send_ntfy_notification("test", monitor.read_battery_level(), monitor.read_voltage())
    except Exception as e:
        log_message("ERROR", f"ntfy test failed during X728Monitor initialization: {e}")
    finally:
        if 'monitor' in locals():
            monitor.close()

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
            log_message("ERROR", f"Failed to stop or disable service: {e.stderr}")

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
            subprocess.run(["cp", os.path.abspath(__file__), backup_script], check=True)
        # Note: This line uses the temporary file of the currently running script, which holds the v1.5.1 code
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

    # V1.4.2 ADDITION: Immediate Log Check to confirm execution is past argparser.
    log_message("INFO", f"Script started successfully (Version {SCRIPT_VERSION}). Proceeding with initialization.")


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
    requires_gpio = not (args.install_as_service or args.uninstall or ("-h" in sys.argv or "--help" in sys.argv or args.test_ntfy))
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

    if smbus is None:
        log_message("ERROR", "Critical dependency smbus is missing. Exiting.", exit_on_error=True)
    if bus is None:
        log_message("WARNING", "I2C bus object could not be created. I2C Sampler will likely fail.", exit_on_error=False)


    log_message("WARNING", f"CRITICAL_LOW_THRESHOLD is set to {args.critical_low_threshold}%% for testing.")

    try:
        monitor = X728Monitor(
            enable_ntfy=args.enable_ntfy,
            ntfy_server=args.ntfy_server,
            ntfy_topic=args.ntfy_topic,
            low_battery_threshold=args.low_battery_threshold,
            critical_low_threshold=args.critical_low_threshold
        )
    except Exception as e:
        log_message("ERROR", f"Failed to initialize X728Monitor. The underlying error was: {e}. Exiting.")
        sys.exit(1)
    
    gpio_thread = threading.Thread(target=gpio_and_shutdown_thread, args=(monitor,), daemon=True)
    gpio_thread.start()

    try:
        while True:
            # Main thread keeps running to prevent script from exiting
            time.sleep(1)
    except KeyboardInterrupt:
        log_message("INFO", "Exiting...")
    finally:
        monitor.close()

if __name__ == "__main__":
    main()