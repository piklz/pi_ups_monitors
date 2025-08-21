#!/usr/bin/env python3
#
#             _   _   ___ _____    
#            | | | | / _ \_   _|   
#            | |_| |/ /_\ \| |  ___ 
#            |  _  ||  _  || | / __|
#            | | | || | | || |  (__ 
# waveshares \_| |_/\_| |_/\_/ \___| UPS for pizero   (https://www.waveshare.com/ups-hat-c.htm)                            
# -----------------------------------------------
# Presto UPS Monitor Script
# Version: 1.4.1
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
# Usage:
#   ./presto_hatc_monitor.py                     # Run monitoring directly with default settings
#   ./presto_hatc_monitor.py --install_as_service # Install as a systemd service
#   ./presto_hatc_monitor.py --force-reinstall   # Force reinstall the service without prompting
#   ./presto_hatc_monitor.py --addr 0x43 --ntfy-topic pizero_UPSc  # Run with custom settings
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
USER_HOME = os.popen(f"getent passwd {USER} | cut -d: -f6").read().strip()

# Interactive flag
INTERACTIVE = True

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

class INA219:
    def __init__(self, i2c_bus=1, addr=0x43, ntfy_server="https://ntfy.sh", ntfy_topic="pizero_UPSc", power_threshold=0.5, percent_threshold=20.0, battery_capacity_mAh=1000, battery_voltage=3.7):
        self.bus = smbus.SMBus(i2c_bus)
        self.addr = addr
        self.ntfy_server = ntfy_server
        self.ntfy_topic = ntfy_topic
        self.power_threshold = power_threshold
        self.percent_threshold = percent_threshold
        self.battery_capacity_mAh = battery_capacity_mAh
        self.battery_voltage = battery_voltage
        self.last_notification = None
        self.notification_cooldown = timedelta(minutes=5)
        self.power_readings = deque(maxlen=5)
        self.is_unplugged = False
        self.current_readings = deque(maxlen=3)
        self._cal_value = 0
        self._current_lsb = 0
        self._power_lsb = 0
        self.set_calibration_16V_5A()

    def read(self, address):
        data = self.bus.read_i2c_block_data(self.addr, address, 2)
        return ((data[0] * 256) + data[1])

    def write(self, address, data):
        temp = [0, 0]
        temp[1] = data & 0xFF
        temp[0] = (data & 0xFF00) >> 8
        self.bus.write_i2c_block_data(self.addr, address, temp)

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

    def send_ntfy_notification(self, power, percent, current):
        if len(self.power_readings) < self.power_readings.maxlen:
            return
        current_time = datetime.now()
        if self.last_notification is None or (current_time - self.last_notification) >= self.notification_cooldown:
            message = None
            self.current_readings.append(current)
            all_negative = all(c < -10 for c in self.current_readings)
            all_positive = all(c > 10 for c in self.current_readings)
            if all_negative and not self.is_unplugged:
                runtime = self.estimate_battery_runtime()
                runtime_str = f"{runtime:.1f} hours" if runtime else "unknown"
                message = f"USB charger unplugged on {self.get_hostname()}: Running on battery, estimated runtime {runtime_str}"
                self.is_unplugged = True
            elif all_positive and self.is_unplugged:
                message = f"USB charger reconnected on {self.get_hostname()}: System back on external power"
                self.is_unplugged = False
            elif all_positive and power < self.power_threshold:
                message = f"Low power alert on {self.get_hostname()}: {power:.3f} W (Threshold: {self.power_threshold} W)"
            elif all_positive and percent < self.percent_threshold:
                message = f"Low percent alert on {self.get_hostname()}: {percent:.1f}% (Threshold: {self.percent_threshold}%)"
            if message:
                try:
                    requests.post(
                        f"{self.ntfy_server}/{self.ntfy_topic}",
                        data=message,
                        headers={"Title": "Raspberry Pi Power Alert"}
                    )
                    log_message("INFO", f"Notification sent: {message}")
                    self.last_notification = current_time
                except Exception as e:
                    log_message("ERROR", f"Failed to send notification: {e}")

def log_message(log_level, console_message, log_file_message=None):
    if log_file_message is None:
        log_file_message = console_message
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    journal_message = f"[{timestamp}] [presto-UPSc-service] [{log_level}] {log_file_message}"
    try:
        subprocess.run(
            ["systemd-cat", "-t", "presto_ups", "-p", log_level.lower()],
            input=journal_message,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"[presto-UPSc-service] [ERROR] Failed to log to journald: {e}", file=sys.stderr)
    if INTERACTIVE:
        color = {"INFO": COL_INFO, "WARNING": COL_WARNING, "ERROR": COL_ERROR}.get(log_level, COL_NC)
        print(f"[presto-UPSc-service] {color}[{log_level}]{COL_NC} {console_message}")
    if log_level == "ERROR":
        sys.exit(1)

def check_dependencies():
    log_message("INFO", f"Checking dependencies for user {USER}")
    if not os.path.exists("/usr/bin/python3"):
        log_message("ERROR", "python3 is not installed. Please install it with 'sudo apt-get install python3'")
    log_message("INFO", f"Python3 is installed: {subprocess.getoutput('python3 --version')}")
    if subprocess.run(["dpkg", "-l"], capture_output=True, text=True).stdout.find("python3-smbus") == -1:
        log_message("ERROR", "python3-smbus is not installed. Please install it with 'sudo apt-get install python3-smbus'")
    log_message("INFO", "python3-smbus is installed")
    if os.path.exists("/usr/bin/pip3"):
        log_message("INFO", f"pip3 is installed: {subprocess.getoutput('pip3 --version')}")
        pip_list = subprocess.getoutput("pip3 list")
        if "smbus" in pip_list:
            log_message("INFO", "smbus is already installed")
        else:
            log_message("INFO", "Installing smbus")
            result = subprocess.run(["pip3", "install", "smbus"], capture_output=True, text=True)
            if result.returncode == 0:
                log_message("INFO", "smbus installed successfully")
            else:
                log_message("WARNING", "Failed to install smbus via pip3. Ensure python3-smbus is installed")
        if "requests" in pip_list:
            log_message("INFO", "requests is already installed")
        else:
            log_message("INFO", "Installing requests")
            result = subprocess.run(["pip3", "install", "requests"], capture_output=True, text=True)
            if result.returncode == 0:
                log_message("INFO", "requests installed successfully")
            else:
                log_message("WARNING", "Failed to install requests via pip3. Continuing, but notifications may fail")
    else:
        log_message("WARNING", "pip3 not found. Please install with 'sudo apt-get install python3-pip'")
    if os.path.exists("/usr/bin/vcgencmd"):
        log_message("INFO", "libraspberrypi-bin is installed (vcgencmd found)")
    else:
        log_message("WARNING", "libraspberrypi-bin not installed. Install with 'sudo apt-get install libraspberrypi-bin'")
    if os.path.exists("/usr/sbin/i2cdetect"):
        log_message("INFO", f"i2c-tools is installed: {subprocess.getoutput('i2cdetect -V')}")
    else:
        log_message("WARNING", "i2c-tools not installed. Install with 'sudo apt-get install i2c-tools'")
    if not os.path.exists("/usr/bin/systemd-cat"):
        log_message("ERROR", "systemd-cat is not installed. Please install systemd")

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

def create_service_file(target_script, target_dir, args):
    log_message("INFO", "Creating systemd service file")
    service_file = "/etc/systemd/system/presto_ups.service"
    service_content = f"""[Unit]
Description=Raspberry Pi Presto UPS Monitor Service
After=network.target

[Service]
ExecStart=/usr/bin/python3 {target_script} --addr {args.addr} --ntfy-server "{args.ntfy_server}" --ntfy-topic "{args.ntfy_topic}" --power-threshold {args.power_threshold} --percent-threshold {args.percent_threshold} --battery-capacity {args.battery_capacity} --battery-voltage {args.battery_voltage}
WorkingDirectory={target_dir}
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

def setup_service():
    log_message("INFO", "Setting up service permissions")
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

def test_ntfy(ntfy_server, ntfy_topic):
    log_message("INFO", "Testing ntfy connectivity")
    if os.path.exists("/usr/bin/curl"):
        try:
            response = requests.post(f"{ntfy_server}/{ntfy_topic}", data="Test message from presto_ups", headers={"Title": "Test Alert"})
            if response.status_code == 200:
                log_message("INFO", f"ntfy test notification sent successfully. Check your topic ({ntfy_server}/{ntfy_topic})")
            else:
                log_message("WARNING", "Failed to send test ntfy notification. Check network or ntfy server/topic")
        except requests.RequestException:
            log_message("WARNING", "Failed to send test ntfy notification. Check network or ntfy server/topic")
    else:
        log_message("WARNING", "curl not found, cannot test ntfy connectivity. Install with 'sudo apt-get install curl'")

def sample_ina219(ina219, data_queue, data_lock):
    while True:
        try:
            with data_lock:
                bus_voltage = ina219.getBusVoltage_V()
                shunt_voltage = ina219.getShuntVoltage_mV() / 1000
                current = ina219.getCurrent_mA()
                power = ina219.getPower_W()
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
            ina219.power_readings.append(power)
        except Exception as e:
            log_message("ERROR", f"Sampling error: {e}")
        time.sleep(2)

def install_as_service(args):
    if os.geteuid() != 0:
        log_message("ERROR", "Service installation must be run as root (use sudo)")
    
    log_message("INFO", f"Installing Presto UPS HAT monitor service for user {USER}")
    log_message("INFO", f"I2C Address: {args.addr}")
    log_message("INFO", f"ntfy Server: {args.ntfy_server}")
    log_message("INFO", f"ntfy Topic: {args.ntfy_topic}")
    log_message("INFO", f"Power Threshold: {args.power_threshold} W")
    log_message("INFO", f"Percent Threshold: {args.percent_threshold} %")
    log_message("INFO", f"Battery Capacity: {args.battery_capacity} mAh")
    log_message("INFO", f"Battery Voltage: {args.battery_voltage} V")
    log_message("INFO", f"Original Script: {os.path.abspath(__file__)}")
    
    result = subprocess.run(["systemctl", "is-active", "--quiet", "presto_ups"], check=False)
    if result.returncode == 0 and not args.force_reinstall:
        log_message("WARNING", "The presto_ups service is already running")
        log_message("INFO", "Current service status:")
        subprocess.run(["systemctl", "status", "presto_ups", "--no-pager"], check=False)
        if INTERACTIVE:
            response = input("[presto-UPSc-service] Do you want to reinstall with new settings? [y/N]: ").strip().lower()
            if response != 'y':
                log_message("INFO", "Installation aborted. Service is already running")
                sys.exit(0)
            log_message("INFO", "Stopping existing service for reinstallation")
            subprocess.run(["systemctl", "stop", "presto_ups"], check=True)
    
    check_dependencies()
    enable_i2c()
    check_i2c_device(args.addr)
    
    target_dir = os.path.join(USER_HOME, "presto_UPS")
    target_script = os.path.join(target_dir, "presto_hatc_monitor.py")
    success = False
    try:
        log_message("INFO", f"Copying script to {target_script}")
        os.makedirs(target_dir, exist_ok=True)
        if os.path.exists(target_script):
            backup_script = f"{target_script}.bak"
            log_message("INFO", f"Backing up existing script to {backup_script}")
            subprocess.run(["cp", target_script, backup_script], check=True)
        subprocess.run(["cp", os.path.abspath(__file__), target_script], check=True)
        subprocess.run(["chmod", "755", target_script], check=True)
        subprocess.run(["chown", f"{USER}:{USER}", target_script], check=True)
        log_message("INFO", f"Script copied successfully to {target_script}")
        success = True
    except Exception as e:
        log_message("WARNING", f"Failed to copy script to {target_script}: {e}")
    
    if not success:
        target_dir = "/usr/local/bin"
        target_script = os.path.join(target_dir, "presto_hatc_monitor.py")
        log_message("INFO", f"Falling back to copying script to {target_script}")
        try:
            if os.path.exists(target_script):
                backup_script = f"{target_script}.bak"
                log_message("INFO", f"Backing up existing script to {backup_script}")
                subprocess.run(["cp", target_script, backup_script], check=True)
            subprocess.run(["cp", os.path.abspath(__file__), target_script], check=True)
            subprocess.run(["chmod", "755", target_script], check=True)
            subprocess.run(["chown", f"{USER}:{USER}", target_script], check=True)
            log_message("INFO", f"Script copied successfully to {target_script}")
            success = True
        except Exception as e:
            log_message("ERROR", f"Failed to copy script to {target_script}: {e}")
    
    create_service_file(target_script, target_dir, args)
    setup_service()
    test_ntfy(args.ntfy_server, args.ntfy_topic)
    
    log_message("INFO", "Installation complete")
    log_message("INFO", "The Presto UPS HAT monitor is running as a service (presto_ups)")
    log_message("INFO", "To check logs: journalctl -t presto_ups or journalctl -u presto_ups.service")
    log_message("INFO", "To stop the service: sudo systemctl stop presto_ups.service")
    log_message("INFO", "To disable the service: sudo systemctl disable presto_ups.service")
    log_message("INFO", "To reinstall with new settings, rerun with --install_as_service and desired arguments")
    sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="Presto UPS HAT Monitor with Service Installation")
    parser.add_argument("--install_as_service", action="store_true", help="Install as a systemd service")
    parser.add_argument("--addr", default="0x43", help="I2C address of INA219 (e.g., 0x43)")
    parser.add_argument("--ntfy-server", default="https://ntfy.sh", help="ntfy server URL")
    parser.add_argument("--ntfy-topic", default="pizero_UPSc", help="ntfy topic for notifications")
    parser.add_argument("--power-threshold", type=float, default=0.5, help="Power threshold for alerts in watts")
    parser.add_argument("--percent-threshold", type=float, default=20.0, help="Battery percentage threshold for alerts")
    parser.add_argument("--battery-capacity", type=int, default=1000, help="Battery capacity in mAh")
    parser.add_argument("--battery-voltage", type=float, default=3.7, help="Battery nominal voltage in volts")
    parser.add_argument("--force-reinstall", action="store_true", help="Force reinstallation of the service without prompting")
    args = parser.parse_args()
    
    global INTERACTIVE
    INTERACTIVE = sys.stdin.isatty()
    
    if args.install_as_service:
        install_as_service(args)
        return
    
    if not re.match(r"^0x[0-9A-Fa-f]{2}$", args.addr):
        log_message("ERROR", "Invalid I2C address format. Use hex (e.g., 0x43)")
    if args.power_threshold <= 0:
        log_message("ERROR", "Power threshold must be positive")
    if args.percent_threshold <= 0 or args.percent_threshold > 100:
        log_message("ERROR", "Percent threshold must be between 0 and 100")
    if args.battery_capacity <= 0:
        log_message("ERROR", "Battery capacity must be positive")
    if args.battery_voltage <= 0:
        log_message("ERROR", "Battery voltage must be positive")
    
    ina219 = INA219(
        i2c_bus=1,
        addr=int(args.addr, 16),
        ntfy_server=args.ntfy_server,
        ntfy_topic=args.ntfy_topic,
        power_threshold=args.power_threshold,
        percent_threshold=args.percent_threshold,
        battery_capacity_mAh=args.battery_capacity,
        battery_voltage=args.battery_voltage
    )
    
    data_queue = queue.Queue()
    data_lock = threading.Lock()
    sampling_thread = threading.Thread(target=sample_ina219, args=(ina219, data_queue, data_lock), daemon=True)
    sampling_thread.start()
    
    last_log_time = datetime.now()
    log_interval = timedelta(seconds=10)
    
    while True:
        try:
            data = data_queue.get_nowait()
            ina219.send_ntfy_notification(data['power'], data['percent'], data['current'])
            if datetime.now() - last_log_time >= log_interval:
                log_message("INFO", f"Load Voltage: {data['bus_voltage']:>6.3f} V")
                log_message("INFO", f"Current:      {data['current']/1000:>6.3f} A")
                log_message("INFO", f"Power:        {data['power']:>6.3f} W")
                log_message("INFO", f"Percent:     {data['percent']:>6.1f}%")
                log_message("INFO", "System Info:")
                log_message("INFO", f"Hostname:    {ina219.get_hostname()}")
                log_message("INFO", f"IP Address:  {ina219.get_ip_address()}")
                cpu_temp = ina219.get_cpu_temp()
                gpu_temp = ina219.get_gpu_temp()
                log_message("INFO", f"CPU Temp:    {cpu_temp if cpu_temp else 'Unknown':>6.1f} °C" if cpu_temp else "CPU Temp:    Unknown")
                log_message("INFO", f"GPU Temp:    {gpu_temp if gpu_temp else 'Unknown':>6.1f} °C" if gpu_temp else "GPU Temp:    Unknown")
                log_message("INFO", "---")
                last_log_time = datetime.now()
        except queue.Empty:
            pass
        time.sleep(0.1)

if __name__ == "__main__":
    main()