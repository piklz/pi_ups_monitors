#!/usr/bin/env python3
"""
________________/\\\\\\\\\\\\\\\____/\\\\\\\\\_________/\\\\\\\\\____        
 _______________\/////////////\\\__/\\\///////\\\_____/\\\///////\\\__       
  __________________________/\\\/__\///______\//\\\___\/\\\_____\/\\\__      
   __/\\\____/\\\__________/\\\/______________/\\\/____\///\\\\\\\\\/___     
    _\///\\\/\\\/_________/\\\/_____________/\\\//_______/\\\///////\\\__    
     ___\///\\\/_________/\\\/____________/\\\//_________/\\\______\//\\\_   
      ____/\\\/\\\______/\\\/____________/\\\/___________\//\\\______/\\\__  
       __/\\\/\///\\\__/\\\/_____________/\\\\\\\\\\\\\\\__\///\\\\\\\\\/___ 
        _\///____\///__\///______________\///////////////_____\/////////_____

X728 UPS Monitor - Professional Docker Edition
Version: 3.0.2
Build: Professional Docker Edition
Author: Piklz
GitHub Repository: https://github.com/piklz/pi_ups_monitors
===============================================================================

DESCRIPTION:
x728 HW v1.2 HAT Battery Monitor for raspberry pi 3,4,5 (Docker Edition)
This script provides comprehensive monitoring and control for the X728 UPS 
HAT for Raspberry Pi. It includes features such as battery monitoring (and safe shutdown of Pi+x728), 
power state detection, GPIO handling, and a modern web-based dashboard.
ie.normal python scripts available for non docker setups /simple service 
   or live checking setups in /piklz/pi_ups_monitors root folder

FEATURES:
- Real-time battery level and voltage monitoring
- Power state detection (AC/Battery)
- Automatic shutdown on critical battery levels
- Configurable thresholds for battery, CPU temperature, and disk space
- Notifications via ntfy
- Web-based dashboard with live updates and historical data visualization
- Docker-compatible with safe shutdown overlay configuration
   (should  work  on all debians like dietpi,retropi,ubuntu that suport this overlaytree)


CHANGELOG:
- v3.0.2: Fixed low battery checkign logic + improved logging
- v3.0.1: flash status fixed for config save section + est time remaining for ntfy low battery alert  added
- v3.0.0: Added professional UI, enhanced GPIO handling, and Docker compatibility
- v2.5.0: Introduced ntfy notifications and improved error handling
- v2.0.0: Added web-based dashboard and historical data tracking
- v1.0.0: Initial release with basic battery monitoring and shutdown

USAGE TIPS:
1. Ensure the X728 UPS HAT is properly connected to your Raspberry Pi.
2. Access the web dashboard at `http://<your-pi-ip>:5000` after starting the script.
3. Use the configuration section in the dashboard to customize thresholds and settings.

IMPORTANT:
- This script requires Python 3.6+ and the following dependencies:
    Flask, Flask-SocketIO, smbus2, gpiod, psutil, requests
- Run the script with appropriate permissions to access GPIO and I2C.

LICENSE:
This project is licensed under the MIT License. See the LICENSE file for details.

For more information, visit the GitHub repository:
https://github.com/piklz/pi_ups_monitors
===============================================================================
"""

import socket
import os
import subprocess
import time
import struct
import json
import threading

from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template_string, jsonify, request, flash, redirect, url_for
from flask_socketio import SocketIO
import smbus2 as smbus
import gpiod
import requests
import psutil

# ============================================================================
# CONFIGURATION AND INITIALIZATION
# ============================================================================

VERSION_STRING = "X728 UPS Monitor v3.0.2"
VERSION_BUILD = "Professional Docker Edition"

app = Flask(__name__)
app.secret_key = 'x728_ups_secret_2025_v3'
app.config['SESSION_TYPE'] = 'null'
app.config['SESSION_PERMANENT'] = False

socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    manage_session=False, 
    transports=['websocket', 'polling'],
    async_mode='threading'
)

# Constants
I2C_ADDRS = [0x16, 0x36, 0x3b, 0x4b]
CONFIG_PATH = "/config/x728_config.json"
LOG_PATH = "/config/x728_debug.log"
HISTORY_PATH = "/config/battery_history.json"

# X728 GPIO Pins (BCM numbering)
GPIO_PLD_PIN = 6   # Power Loss Detection
GPIO_SHUTDOWN_PIN = 13 # Shutdown signal to UPS pi only
DEFAULT_CONFIG = {
    "low_battery_threshold": 30.0,
    "critical_low_threshold": 10.0,
    "cpu_temp_threshold": 70.0,
    "disk_space_threshold": 10.0,
    "enable_ntfy": 1,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "x728_UPS",
    "debug": 1,
    "monitor_interval": 10,
    "enable_auto_shutdown": 1,
    "shutdown_delay": 60,
    "idle_load_ma": 500  # Idle current draw in mA for time estimation 500-800mA typical
}

# Global state
config = DEFAULT_CONFIG.copy()
bus = None
current_i2c_addr = None
hardware_error = None
monitor_thread_running = False
monitor_thread_stop_event = threading.Event()
lock = threading.Lock()
previous_power_state = None 

# GPIO state
gpio_chip = None
pld_line = None
shutdown_line = None
gpio_error = None


# Battery history (last 100 readings)
battery_history = []
MAX_HISTORY = 100

# Alert debouncing
last_alerts = {
    'low_battery': 0,
    'critical_battery': 0,
    'high_cpu': 0,
    'low_disk': 0
}
ALERT_COOLDOWN = 300  # 5 minutes

# Disk path - for direct run, use '/'; for Docker, '/host' if mounted
DISK_PATH = '/' if not os.path.exists('/.dockerenv') else '/host'

# New, robust line:
#DISK_PATH = '/host' # for debug only 

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def log_message(message, level="INFO"):
    """Enhanced logging with levels"""
    global config
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] [{level}] {message}"
    print(log_entry)
    
    if config.get('debug', 0):
        with lock:
            try:
                os.makedirs(os.path.dirname(LOG_PATH) if LOG_PATH != '/config/x728_debug.log' else '.', exist_ok=True)
                with open(LOG_PATH, 'a') as f:
                    f.write(log_entry + '\n')
            except Exception as e:
                print(f"ERROR: Failed to write to log: {e}")

def load_config():
    """Load configuration from JSON file"""
    global config

    # print script name build verion 
    log_message(f"Starting {VERSION_STRING} - {VERSION_BUILD}")
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r') as f:
                loaded = json.load(f)
                config.update(loaded)
        
        # Type casting
        config['low_battery_threshold'] = float(config.get('low_battery_threshold', 30.0))
        config['critical_low_threshold'] = float(config.get('critical_low_threshold', 10.0))
        config['cpu_temp_threshold'] = float(config.get('cpu_temp_threshold', 70.0))
        config['disk_space_threshold'] = float(config.get('disk_space_threshold', 10.0))
        config['enable_ntfy'] = int(config.get('enable_ntfy', 1))
        config['debug'] = int(config.get('debug', 1))
        config['monitor_interval'] = int(config.get('monitor_interval', 10))
        config['enable_auto_shutdown'] = int(config.get('enable_auto_shutdown', 1))
        config['shutdown_delay'] = int(config.get('shutdown_delay', 60))
        
        log_message(f"Configuration loaded: {config}")
    except Exception as e:
        log_message(f"Failed to load config: {e}. Using defaults.", "ERROR")

def save_config():
    """Save configuration to JSON file"""
    global config
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH) if CONFIG_PATH != '/config/x728_config.json' else '.', exist_ok=True)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=4)
        log_message("Configuration saved successfully")
    except Exception as e:
        log_message(f"Failed to save config: {e}", "ERROR")
        raise

def load_battery_history():
    """Load battery history from file"""
    global battery_history
    try:
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, 'r') as f:
                battery_history = json.load(f)
                if len(battery_history) > MAX_HISTORY:
                    battery_history = battery_history[-MAX_HISTORY:]
    except Exception as e:
        log_message(f"Failed to load battery history: {e}", "WARNING")
        battery_history = []

def save_battery_history():
    """Save battery history to file"""
    global battery_history
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH) if HISTORY_PATH != '/config/battery_history.json' else '.', exist_ok=True)
        with open(HISTORY_PATH, 'w') as f:
            json.dump(battery_history[-MAX_HISTORY:], f)
    except Exception as e:
        log_message(f"Failed to save battery history: {e}", "WARNING")

def add_to_history(battery_level, voltage, power_state):
    """Add reading to battery history"""
    global battery_history
    entry = {
        "timestamp": datetime.now().isoformat(),
        "battery": battery_level,
        "voltage": voltage,
        "state": power_state
    }
    battery_history.append(entry)
    if len(battery_history) > MAX_HISTORY:
        battery_history = battery_history[-MAX_HISTORY:]
    
    # Save periodically (every 10 entries)
    if len(battery_history) % 10 == 0:
        save_battery_history()

def send_ntfy(message, priority="default", title="X728 UPS Alert"):
    """Send notification via ntfy with retry and longer timeout"""
    global config
    if not config.get('enable_ntfy', 0):
        return
    
    server = config.get('ntfy_server')
    topic = config.get('ntfy_topic')
    
    if not server or not topic:
        log_message("ntfy not configured", "WARNING")
        return
    #log_message(f"Attempting to send ntfy: {message[:100]}...", "DEBUG") #for debuging
    max_retries = 3
    for attempt in range(max_retries):
        try:
            url = f"{server}/{topic}"
            headers = {"Title": title, "Priority": priority}
            response = requests.post(url, data=message.encode('utf-8'), headers=headers, timeout=10)  # Increased to 10s
            response.raise_for_status()
            #log_message(f"ntfy sent [{priority}]: {message[:50]}...") #use :50  to truncate for debuging
            log_message(f"ntfy sent [{priority}]: {message}")
            return  # Success, exit
        except Exception as e:
            log_message(f"ntfy attempt {attempt + 1} failed: {e}", "WARNING")
            if attempt < max_retries - 1:
                time.sleep(2)  # Wait 2s before retry
            else:
                log_message(f"ntfy failed after {max_retries} attempts: {e}", "ERROR")



# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

# ... (existing functions like send_ntfy, load_config, etc.)

def configure_kernel_overlay():
    """Checks and configures the gpio-poweroff overlay for safe shutdown (V1.3).
    
    NOTE: This function is SKIPPED when running inside a Docker container.
    The host OS must be configured manually for safe shutdown to work.
    """
    global config
    
    # === CHECK FOR DOCKER ===
    # Check for the existence of the /.dockerenv file, which indicates running inside a container.
    if os.path.exists('/.dockerenv'):
        log_message("Skipping kernel overlay configuration: Detected container environment.", "WARNING")
        # In Docker, we assume the host is configured or we rely on explicit device mapping.
        return True
    # ===============================
    
    # Check common configuration file paths
    config_paths = ["/boot/firmware/config.txt", "/boot/config.txt"]
    
    # Target overlay settings (V1.3 hardware, 10s timeout)
    target_overlay_line = "dtoverlay=gpio-poweroff,gpiopin=13,active_low=0,timeout_ms=10000"
    target_comment_line = "# X728 Safe Shutdown Overlay (Added by Script)"
    
    config_file = None
    for path in config_paths:
        if os.path.exists(path):
            config_file = path
            break
            
    if not config_file:
        log_message("ERROR: Cannot find config.txt. Skipping overlay configuration.", "ERROR")
        return False

    try:
        # 1. Read the entire file content
        with open(config_file, 'r') as f:
            content = f.readlines()
        
        new_content = []
        found_config = False
        
        # 2. Iterate through lines to check/replace/clean up old entries
        for line in content:
            stripped_line = line.strip()
            
            # Skip old custom comments and empty lines created by previous runs
            if stripped_line == target_comment_line or stripped_line == "":
                continue
            
            # Check for the overlay directive
            if stripped_line.startswith("dtoverlay=gpio-poweroff"):
                # Check if the existing line matches the target line
                if stripped_line == target_overlay_line:
                    log_message(f"Kernel overlay found and matches target: {target_overlay_line}", "INFO")
                    # If it matches, we are done, but first, add the clean comment line before it 
                    # for future reference, unless it's already in new_content
                    if target_comment_line not in new_content:
                        new_content.append(target_comment_line + '\n')
                        
                    new_content.append(line)
                    found_config = True
                    continue
                else:
                    # Found a conflicting or different gpio-poweroff line, skip it (effective removal)
                    log_message(f"Skipping conflicting gpio-poweroff line: {stripped_line}", "WARNING")
                    continue
            
            # Keep all other, unrelated lines
            new_content.append(line)

        # 3. If the correct configuration was NOT found, append it cleanly
        if not found_config:
            log_message(f"Adding kernel overlay to {config_file}. REBOOT REQUIRED.", "WARNING")
            
            # Append the required configuration at the very end
            new_content.append('\n') # Ensure a blank line separation
            new_content.append(target_comment_line + '\n')
            new_content.append(target_overlay_line + '\n')
            
            # 4. Write the modified content back to the file (requires sudo/root permissions)
            temp_file = "/tmp/config_temp.txt"
            with open(temp_file, 'w') as tmp:
                tmp.writelines(new_content)
                
            # Use 'sudo cp' to write the temporary file back over the protected config file
            command = f"sudo cp {temp_file} {config_file}"
            subprocess.run(command, shell=True, check=True, timeout=5)
            
            log_message("Successfully configured kernel overlay. PLEASE REBOOT NOW for safe shutdown to take effect.", "CRITICAL")
            send_ntfy("⚠️ Kernel shutdown overlay configured. REBOOT REQUIRED for safe shutdown to work!", "max", "Configuration Change")
            return True
            
        return True # Configuration is correct or was just fixed

    except Exception as e:
        log_message(f"An error occurred during config file check/write: {e}", "CRITICAL")
        send_ntfy(f"❌ Failed to configure kernel overlay: {e}", "max", "Configuration Error")
        return False
        
# ============================================================================
# HARDWARE INITIALIZATION
# ============================================================================

def init_i2c():
    """Initialize I2C bus and detect X728"""
    global bus, current_i2c_addr, hardware_error
    
    log_message("Initializing I2C bus...")
    
    try:
        bus = smbus.SMBus(1)
        log_message("I2C bus 1 opened successfully")
        
        # Detect X728 at known addresses
        for addr in I2C_ADDRS:
            try:
                bus.read_i2c_block_data(addr, 0x04, 2)
                current_i2c_addr = addr
                log_message(f"X728 UPS detected at I2C address 0x{addr:02x}")
                hardware_error = None
                return True
            except Exception:
                pass
        
        hardware_error = "X728 UPS not detected on I2C bus"
        log_message(hardware_error, "ERROR")
        return False
        
    except Exception as e:
        bus = None
        hardware_error = f"I2C initialization failed: {e}"
        log_message(hardware_error, "ERROR")
        return False

def init_gpio():
    """Initialize GPIO using gpiod (libgpiod bindings)"""
    global gpio_chip, pld_line, gpio_error
    chip_name = 'gpiochip0'
    dev_path = f'/dev/{chip_name}'
    try:
        log_message(f"Attempting to open GPIO chip at '{chip_name}' (path: {dev_path})")
        if not os.path.exists(dev_path):
            log_message(f"GPIO chip '{chip_name}' not found at {dev_path}", "WARNING")
            raise RuntimeError(f"Device not found: {dev_path}")
        gpio_chip = gpiod.Chip(dev_path)
        if gpio_chip is None:
            raise RuntimeError(f"Failed to initialize '{chip_name}': Chip object is None")
        
        log_message(f"Requesting GPIO line {GPIO_PLD_PIN} (PLD) on '{chip_name}'")
        pld_line = gpio_chip.get_line(GPIO_PLD_PIN)  # Pin 6
        
        if pld_line is None:
            raise RuntimeError(f"Failed to get PLD GPIO line on '{chip_name}'")
        
        if pld_line.is_requested():
            raise RuntimeError(f"PLD GPIO line {GPIO_PLD_PIN} already in use on '{chip_name}'")
        
        pld_line.request(consumer="x728_pld", type=gpiod.LINE_REQ_DIR_IN)
        
        log_message(f"GPIO initialized successfully on '{chip_name}' (PLD only - shutdown handled by kernel overlay)")
        gpio_error = None  # Explicitly clear gpio_error
        return True  # Indicate success
    except Exception as e:
        gpio_error = str(e)
        log_message(f"Failed to initialize '{chip_name}' at {dev_path}: {e}", "ERROR")
        if gpio_chip:
            gpio_chip.close()
        return False  # Indicate failure

# Update the main initialization logic (likely in your script's startup)
def init_hardware():
    global hardware_error, bus, current_i2c_addr
    try:
        log_message("Initializing I2C bus...")
        bus = smbus.SMBus(1)
        log_message("I2C bus 1 opened successfully")
        
        # Detect X728 UPS
        for addr in I2C_ADDRS:
            try:
                bus.read_byte(addr)
                current_i2c_addr = addr
                log_message(f"X728 UPS detected at I2C address 0x{addr:02x}")
                break
            except:
                continue
        else:
            raise RuntimeError("X728 UPS not detected on any I2C address")
        
        # Initialize GPIO
        if init_gpio():
            log_message("Hardware initialization complete (I2C and GPIO)")
        else:
            raise RuntimeError("GPIO initialization failed, falling back to I2C only")
    except Exception as e:
        hardware_error = str(e)
        log_message(f"Hardware initialization failed: {e}", "ERROR")
        log_message("Hardware partially initialized (I2C only)", "WARNING")



# ============================================================================
# HARDWARE READING FUNCTIONS
# ============================================================================

def read_i2c_register(reg, count=2):
    """Read from X728 I2C register"""
    global bus, current_i2c_addr, hardware_error
    
    if not bus or current_i2c_addr is None:
        return 0
    
    try:
        with lock:
            data = bus.read_i2c_block_data(current_i2c_addr, reg, count)
            hardware_error = None
            return struct.unpack('>H', bytes(data))[0]
    except Exception as e:
        if not hardware_error or "Read Error" not in hardware_error:
            hardware_error = f"I2C Read Error (Reg 0x{reg:02x}): {e}"
            log_message(hardware_error, "ERROR")
        return 0

def get_battery_level():
    """Read battery percentage (0-100%)"""
    raw = read_i2c_register(0x04)
    capacity = raw / 256.0
    return max(0.0, min(100.0, capacity))

def get_voltage():
    """Read voltage in volts"""
    raw = read_i2c_register(0x02)
    voltage = raw * 1.25 / 1000 / 16
    return max(0.0, voltage)

def get_power_state():
    """Determine power state using GPIO and voltage"""
    global pld_line, gpio_error # Only need pld_line, not gpio_chip
    
    voltage = get_voltage()
    
    # Try GPIO first
    # Check if pld_line (the gpiod Line object) exists from init_gpio()
    if pld_line and not gpio_error:
        try:
            # The correct way to read the line value in gpiod
            pld_value = pld_line.get_value()
            
            # Logic: HIGH (1) = AC Power LOST (On Battery); LOW (0) = AC Power IS PRESENT
            if pld_value == 1:
                return "On Battery"
            else: # pld_value == 0
                return "On AC Power"
                
        except Exception as e:
            gpio_error = f"GPIO read error: {e}"
            log_message(gpio_error, "WARNING")
    
    # Fallback to voltage detection
    if voltage > 5.05:
        return "On AC Power"
    elif voltage > 3.3:
        return "On Battery"
    else:
        return "Critical/Off"


def estimate_time_remaining(battery_level, voltage, power_state="On Battery"):
    estimate_time_remaining.first_call = getattr(estimate_time_remaining, 'first_call', True)  # Static flag for first call check
    
    if power_state != "On Battery":
        return "∞ (On AC/Charging)"
    
    capacity_mah = 7000 
    load_ma = 800
    if load_ma < 200:
        return "N/A (Low Load)"
    
    # Use recent history for smoothing (last 3 readings)
    recent_readings = battery_history[-3:] if len(battery_history) >= 3 else battery_history
    avg_battery = sum(entry['battery'] for entry in recent_readings) / len(recent_readings) if recent_readings else battery_level
    avg_voltage = sum(entry['voltage'] for entry in recent_readings) / len(recent_readings) if recent_readings else voltage
    
    # Voltage-based SOC
    if avg_voltage > 4.2:
        soc_voltage = 100
    elif avg_voltage > 3.7:
        soc_voltage = 50 + ((avg_voltage - 3.7) / (4.2 - 3.7)) * 50
    elif avg_voltage > 3.3:
        soc_voltage = ((avg_voltage - 3.3) / (3.7 - 3.3)) * 50
    else:
        soc_voltage = 0
    soc_voltage = max(0, min(100, soc_voltage))
    
    # Blend current and historical data (weighted 70% current, 30% historical)
    blended_battery = 0.7 * battery_level + 0.3 * avg_battery
    blended_soc = (blended_battery + soc_voltage) / 2
    remaining_mah_blended = (blended_soc / 100.0) * capacity_mah
    hours_blended = remaining_mah_blended / load_ma
    
    # Log only in debug mode and on first call
    if config.get('debug', 0) and estimate_time_remaining.first_call:
        log_message(f"Time estimate - Battery: {battery_level:.1f}%, Avg Battery: {avg_battery:.1f}%, Voltage: {voltage:.2f}V, Avg Voltage: {avg_voltage:.2f}V, Blended SOC: {blended_soc:.1f}%, Estimated: {hours_blended:.1f} hours")
        estimate_time_remaining.first_call = False  # Disable further logging
    
    if hours_blended > 24:
        return f">24 hours"
    elif hours_blended > 1:
        return f"{hours_blended:.1f} hours"
    else:
        minutes = hours_blended * 60
        return f"{minutes:.0f} minutes"

def get_disk_label():
    """Get the label of the disk mounted at DISK_PATH, with fallback logic"""
    partitions = psutil.disk_partitions()
    for part in partitions:
        if part.mountpoint == DISK_PATH:
            device = part.device
            try:
                label = subprocess.check_output(['lsblk', '-no', 'LABEL', device]).decode().strip()
                if not label:
                    if 'mmcblk' in device:
                        label = "Micro SD Card"
                    else:
                        label = device.split('/')[-1]  # Fallback to device name like 'sda1'
                return label
            except Exception as e:
                log_message(f"Failed to get disk label: {e}", "WARNING")
                return "Unknown"
    return "Unknown"

def get_system_info():
    """Get system metrics"""
    
    try:
        temp_path = "/sys/class/thermal/thermal_zone0/temp"
        with open(temp_path, 'r') as f:
            temp = float(f.read().strip()) / 1000.0
    except Exception:
        temp = 0.0
    
    try:
        disk = psutil.disk_usage(DISK_PATH)
        disk_used = disk.percent
        disk_free_gb = disk.free / (1024**3)
    except Exception as e:
        log_message(f"Disk usage error: {e}", "WARNING")
        disk_used = 0.0
        disk_free_gb = 0.0
    
    disk_label = get_disk_label()
    
    memory = psutil.virtual_memory()
    memory_free_gb = memory.available / (1024**3)  # Free (available) in GB
    memory_total_gb = memory.total / (1024**3)     # Total in GB
    memory_info = f"Free: {memory_free_gb:.1f} GB / Total: {memory_total_gb:.1f} GB"

    try:
        uptime_seconds = time.time() - psutil.boot_time()
        uptime_str = str(timedelta(seconds=int(uptime_seconds)))
    except Exception:
        uptime_str = "Unknown"
    
    return {
        "cpu_temp": f"{temp:.1f}",
        "disk_usage": f"{disk_used:.1f}",
        "disk_free": f"{disk_free_gb:.1f}",
        "disk_label": disk_label,
        "memory_info": memory_info,  # New: "free / total" in GB
        "uptime": uptime_str
    }

def get_pi_model():
    """Detect Raspberry Pi model"""
    try:
        with open('/proc/device-tree/model', 'r') as f:
            model = f.read().strip().replace('\x00', '')
            return model
    except Exception:
        return "Unknown Pi Model"
        
        
# ============================================================================
# MONITORING AND CONTROL
# ============================================================================

def trigger_system_action(action="shutdown", reason="Critical condition"):
    """Initiate safe shutdown or reboot relying on the pre-configured kernel gpio-poweroff overlay."""
    global config
    global pending_action

    action = action.lower()
    if action not in ["shutdown", "reboot"]:
        raise ValueError("Action must be 'shutdown' or 'reboot'")

    # Cancel any previous pending action
    if pending_action.get("thread") and pending_action["thread"].is_alive():
        pending_action["cancel_event"].set()
        pending_action["thread"].join()

    cancel_event = threading.Event()
    delay_key = 'reboot_delay' if action == "reboot" else 'shutdown_delay'
    delay = config.get(delay_key, config.get('shutdown_delay', 60))
    pending_action.update({
        "type": action,
        "cancel_event": cancel_event,
        "remaining": delay
    })

    def countdown_and_execute():
        nonlocal delay
        log_message(f"{action.upper()} TRIGGERED: {reason}", "CRITICAL")
        send_ntfy(
            f"{'🚨' if action == 'shutdown' else '🔄'} {action.upper()} INITIATED: {reason}. System will {action} in {delay} seconds unless canceled.",
            "max",
            f"CRITICAL {action.upper()} WARNING"
        )
        while delay > 0:
            if cancel_event.is_set():
                log_message(f"{action.capitalize()} canceled by user.", "INFO")
                send_ntfy(f"❎ {action.capitalize()} canceled by user.", "default", "Action Canceled")
                pending_action["type"] = None
                socketio.emit('cancel_update', {"status": "canceled"})
                return
            pending_action["remaining"] = delay
            socketio.emit('cancel_update', {"status": "pending", "type": action, "remaining": delay})
            time.sleep(1)
            delay -= 1

        # Execute software action if enabled
        enable_key = 'enable_auto_reboot' if action == "reboot" else 'enable_auto_shutdown'
        if config.get(enable_key, config.get('enable_auto_shutdown', 1)):
            try:
                log_message(f"Syncing disks before {action}", "INFO")
                subprocess.run(["sync"], timeout=5, check=True)  # Sync disks

                is_docker = os.path.exists('/.dockerenv')
                if is_docker:
                    log_message(f"In Docker: Executing host {action} via nsenter", "INFO")
                    command = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "reboot"] if action == "reboot" else ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "shutdown", "-h", "now"]
                    subprocess.run(command, timeout=10, check=True)
                else:
                    log_message(f"On host: Executing direct {action} command", "INFO")
                    command = ["sudo", "reboot"] if action == "reboot" else ["sudo", "shutdown", "-h", "now"]
                    subprocess.run(command, timeout=10, check=True)

                # Delay for shutdown to allow OS halt and X728 power-off
                if action == "shutdown":
                    log_message("Waiting 20 seconds for OS to halt and X728 to power off via kernel overlay", "INFO")
                    time.sleep(20)

                send_ntfy(
                    f"✅ Software {action} command executed.{' X728 UPS power-off initiated via kernel overlay.' if action == 'shutdown' else ''}",
                    "default",
                    f"Safe {action.capitalize()} Initiated"
                )

            except Exception as e:
                log_message(f"Software {action} command failed: {e}", "ERROR")
                send_ntfy(
                    f"❌ Software {action} failed: {e}. Check OS configuration.",
                    "high",
                    f"{action.capitalize()} Error"
                )
        else:
            log_message(f"Auto-{action} disabled. System will rely on manual {action} or external handlers.", "WARNING")
        # After action, clear pending
        pending_action["type"] = None
        socketio.emit('cancel_update', {"status": "done"})

    t = threading.Thread(target=countdown_and_execute, daemon=True)
    pending_action["thread"] = t
    t.start()

def can_send_alert(alert_type):
    """Check if alert can be sent (debounce)"""
    global last_alerts
    now = time.time()
    if now - last_alerts.get(alert_type, 0) > ALERT_COOLDOWN:
        last_alerts[alert_type] = now
        return True
    return False

def check_thresholds():
    """Monitor and alert on threshold violations"""
    global config, hardware_error, previous_power_state
    
    if hardware_error:
        return
    
    battery_level = get_battery_level()
    voltage = get_voltage()
    power_state = get_power_state()
    
    # Power state change detection (AC disconnect/reconnect)
    if previous_power_state is None:
        previous_power_state = power_state  # Set initial on first run
        # IMPORTANT FIX: Check thresholds immediately on startup if on battery
        if power_state == "On Battery":
            time_remaining = estimate_time_remaining(battery_level, voltage, power_state)
            
            # Check critical battery immediately on startup
            if battery_level <= config['critical_low_threshold']:
                send_ntfy(f"🚨 Critical Battery: {battery_level:.1f}%  | Est. time remaining: {time_remaining} - Shutting down", "max", "Shutdown Alert")
                log_message(f"STARTUP: Critical battery detected: {battery_level:.1f}%", "CRITICAL")
                last_alerts['critical_battery'] = time.time()
                trigger_system_action(action="shutdown", reason=f"Critical battery: {battery_level:.1f}%")
                return
            
            # Check low battery immediately on startup
            elif battery_level <= config['low_battery_threshold']:
                send_ntfy(f"⚠️ Low Battery: {battery_level:.1f}% | {voltage:.2f}V  | Est. time remaining: {time_remaining}", "high", "Low Battery Warning")
                log_message(f"STARTUP: Low battery detected: {battery_level:.1f}%", "WARNING")
                last_alerts['low_battery'] = time.time()
                
    elif power_state != previous_power_state:
        if power_state == "On Battery":
            time_remaining = estimate_time_remaining(battery_level, voltage, power_state)
            send_ntfy(f"🔌 AC Power Disconnected - Switched to Battery. Estimated time remaining: {time_remaining}", "high", "Power Alert")
            log_message(f"AC Power lost - switched to battery, Estimated time remaining: {time_remaining}", "WARNING")
        else:
            send_ntfy("🔌 AC Power Reconnected", "default", "Power Alert")
            log_message("AC Power restored", "INFO")
            # Reset critical battery alert cooldown on AC restore
            last_alerts['critical_battery'] = 0
            last_alerts['low_battery'] = 0
        previous_power_state = power_state
    
    # Add to history
    add_to_history(battery_level, voltage, power_state)
    
    # Merged thresholds: Voltage critical <3.0V
    if voltage < 3.0:
        if can_send_alert('critical_voltage'):
            send_ntfy(f"⚡ Critical Voltage: {voltage:.2f}V - Shutdown imminent", "max", "Voltage Alert")
            trigger_system_action(action="shutdown", reason="Shutdown Batt @  critical levels!")
        return
    
    # Critical battery check (ALWAYS check if on battery, regardless of previous checks)
    if power_state == "On Battery" and battery_level <= config['critical_low_threshold']:
        if can_send_alert('critical_battery'):
            time_remaining = estimate_time_remaining(battery_level, voltage, power_state)
            send_ntfy(f"🚨 Critical Battery: {battery_level:.1f}%  | Est. time remaining: {time_remaining} - Shutting down", "max", "Shutdown Alert")
            log_message(f"Critical battery: {battery_level:.1f}%", "CRITICAL")
            trigger_system_action(action="shutdown", reason=f"Critical battery: {battery_level:.1f}%")
        return
    
    # Low battery check (ALWAYS check if on battery)
    if power_state == "On Battery" and battery_level <= config['low_battery_threshold']:
        time_remaining = estimate_time_remaining(battery_level, voltage, power_state)
        if can_send_alert('low_battery'):
            send_ntfy(f"⚠️ Low Battery: {battery_level:.1f}% | {voltage:.2f}V  | Est. time remaining: {time_remaining}", "high", "Low Battery Warning")
            log_message(f"Low battery: {battery_level:.1f}%", "WARNING")
    
    # Existing CPU temp
    system_info = get_system_info()
    cpu_temp = float(system_info['cpu_temp'])
    if cpu_temp >= config['cpu_temp_threshold']:
        if can_send_alert('high_cpu'):
            send_ntfy(f"🌡️ High CPU Temperature: {cpu_temp:.1f}°C", "high", "Temperature Alert")
    
    # Existing disk space
    disk_free = float(system_info['disk_free'])
    disk_label = system_info['disk_label']
    if disk_free <= config['disk_space_threshold']:
        if can_send_alert('low_disk'):
            send_ntfy(f"💾 Low Disk Space on {disk_label}: {disk_free:.1f} GB remaining", "high", "Disk Space Alert")

def monitor_thread_func():
    """Background monitoring thread"""
    global monitor_thread_running
    
    log_message("Monitor thread started")
    monitor_thread_running = True
    
    while not monitor_thread_stop_event.is_set():
        try:
            check_thresholds()
            
            # Emit status update
            battery_level = get_battery_level()
            voltage = get_voltage()
            power_state = get_power_state()
            
            status = {
                "battery_level": f"{battery_level:.1f}",
                "voltage": f"{voltage:.2f}",
                "power_state": power_state,
                "time_remaining": estimate_time_remaining(battery_level, voltage, power_state),
                "system_info": get_system_info(),
                "hardware_error": hardware_error,
                "gpio_status": "OK" if not gpio_error else gpio_error,
                "i2c_addr": f"0x{current_i2c_addr:02x}" if current_i2c_addr else "N/A"
            }
            
            socketio.emit('status_update', status)
            
            # Dynamic interval: faster polling when on battery for critical checks
            if power_state == "On Battery":
                interval = 2  # Faster (2s) for real-time critical monitoring
            else:
                interval = config.get('monitor_interval', 10)  # Normal configurable interval
            
        except Exception as e:
            log_message(f"Monitor error: {e}", "ERROR")
            interval = config.get('monitor_interval', 10)  # Fallback to normal on error
        
        monitor_thread_stop_event.wait(interval)
    
    monitor_thread_running = False
    log_message("Monitor thread stopped")

def start_monitor():
    """Start background monitoring"""
    global monitor_thread_running, monitor_thread_stop_event
    
    if not monitor_thread_running:
        monitor_thread_stop_event.clear()
        thread = threading.Thread(target=monitor_thread_func, daemon=True)
        thread.start()

def send_startup_ntfy():
    """Send startup summary via ntfy"""
    if hardware_error:
        send_ntfy(f"⚠️ Startup with hardware error: {hardware_error}", "high", "UPS Startup")
        return
    
    battery_level = get_battery_level()
    voltage = get_voltage()
    power_state = get_power_state()  # Ensure initial check
    system_info = get_system_info()
    cpu_temp = system_info['cpu_temp']
    disk_free = system_info['disk_free']
    disk_label = system_info['disk_label']
    
    # Determine time remaining based on power state
    time_remaining = estimate_time_remaining(battery_level, voltage, power_state) if power_state == "On Battery" else "∞"
    
    message = (
        f"🚀 Presto x728 UPS Monitor powered up\n"
        f"Initial Power State: {power_state}\n"
        f"Estimated Time Remaining: {time_remaining}\n"
        f"Battery: {battery_level:.1f}%\n"
        f"Voltage: {voltage:.2f}V\n"
        f"CPU/GPU Temp: {cpu_temp}°C\n"
        f"Disk ({disk_label}): {disk_free} GB free\n"
        f"Thresholds:\n"
        f" - Low Battery: {config['low_battery_threshold']}%\n"
        f" - Critical Battery: {config['critical_low_threshold']}%\n"
        f" - CPU Temp: {config['cpu_temp_threshold']}°C\n"
        f" - Disk Space: {config['disk_space_threshold']} GB\n"
        f" - Critical Voltage: 3.0V"
    )
    
    send_ntfy(message, "default", "UPS Startup Summary")


# ============================================================================
# FLASK ROUTES
# ============================================================================
pending_action = {
    "type": None,      # "shutdown" or "reboot"
    "thread": None,    # Thread object
    "cancel_event": None,  # Initialize as None to avoid threading issues
    "remaining": 0
}

@app.route('/system/cancel', methods=['POST'])
def system_cancel():
    """Cancel pending shutdown/reboot"""
    global pending_action
    if pending_action["type"] and pending_action["thread"] and pending_action["thread"].is_alive():
        pending_action["cancel_event"].set()
        return {"status": "Canceled"}, 200
    return {"status": "No pending action"}, 200

@app.route('/system/pending')
def system_pending():
    """Get pending action status"""
    if pending_action["type"]:
        return jsonify({
            "type": pending_action["type"],
            "remaining": pending_action["remaining"]
        })
    return jsonify({"type": None})
@app.route('/')
def dashboard():
    """Main dashboard"""
    global hardware_error, gpio_error, current_i2c_addr, config
    
    battery_level = get_battery_level()
    voltage = get_voltage()
    # Removed: current = get_current()
    power_state = get_power_state()
    system_info = get_system_info()
    pi_model = get_pi_model()
    
    # Format history for chart
    history_chart = []
    for entry in battery_history[-50:]:  # Last 50 points
        try:
            dt = datetime.fromisoformat(entry['timestamp'])
            history_chart.append({
                'time': dt.strftime('%H:%M'),
                'battery': entry['battery'],
                'voltage': entry['voltage']
            })
        except Exception:
            pass
    
    return render_template_string(DASHBOARD_TEMPLATE,
        VERSION_STRING=VERSION_STRING,
        VERSION_BUILD=VERSION_BUILD,
        battery_level=f"{battery_level:.1f}",
        voltage=f"{voltage:.2f}",
        # Removed: current=f"{current:.3f}",
        power_state=power_state,
        time_remaining=estimate_time_remaining(battery_level, 0),  # Dummy current=0 since removed
        system_info=system_info,
        pi_model=pi_model,
        hardware_error=hardware_error,
        gpio_error=gpio_error,
        i2c_addr=f"0x{current_i2c_addr:02x}" if current_i2c_addr else "N/A",
        config=config,
        history=json.dumps(history_chart),
        timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    )

@app.route('/api/status')
def api_status():
    """API endpoint for status"""
    battery_level = get_battery_level()
    voltage = get_voltage()
    
    return jsonify({
        "battery_level": battery_level,
        "voltage": voltage,
        "power_state": get_power_state(),
        "system_info": get_system_info(),
        "hardware_status": {
            "i2c": "OK" if not hardware_error else hardware_error,
            "gpio": "OK" if not gpio_error else gpio_error
        }
    })

@app.route('/configure', methods=['POST'])
def configure():
    """Save configuration"""
    global config
    
    try:
        config['low_battery_threshold'] = float(request.form.get('low_battery_threshold'))
        config['critical_low_threshold'] = float(request.form.get('critical_low_threshold'))
        config['cpu_temp_threshold'] = float(request.form.get('cpu_temp_threshold'))
        config['disk_space_threshold'] = float(request.form.get('disk_space_threshold'))
        config['monitor_interval'] = int(request.form.get('monitor_interval'))
        config['shutdown_delay'] = int(request.form.get('shutdown_delay'))
        config['enable_ntfy'] = 1 if request.form.get('enable_ntfy') else 0
        config['enable_auto_shutdown'] = 1 if request.form.get('enable_auto_shutdown') else 0
        config['ntfy_server'] = request.form.get('ntfy_server', '').strip()
        config['ntfy_topic'] = request.form.get('ntfy_topic', '').strip()
        config['debug'] = 1 if request.form.get('debug') else 0
        
        
        save_config()
        log_message("Configuration updated via web UI")
        flash('✅ Configuration saved successfully!', 'success')
        emit_flash('success', '✅ Configuration saved successfully!')
        return jsonify({'status': 'success', 'message': 'Configuration saved successfully!'})
       
    except Exception as e:
        flash(f'❌ Error saving configuration: {e}', 'error')
        emit_flash('error', f'❌ Error saving configuration: {e}')
        log_message(f"Configuration save failed: {e}", "ERROR")
    #return redirect(url_for('dashboard'))
        return jsonify({'status': 'error', 'message': f'Error saving configuration: {str(e)}'}), 400

@app.route('/system/control', methods=['POST'])
def system_control():
    """System control (shutdown/reboot)"""
    action = request.form.get('action')
    
    try:
        if action == "shutdown":
            log_message("Manual shutdown requested via web UI", "WARNING")
            send_ntfy("System shutdown initiated via web UI", "urgent")
            trigger_system_action(action="shutdown", reason="Manual shutdown from UI")
            msg = '🔴 System will shutdown in {} seconds'.format(config.get('shutdown_delay', 60))
            flash(msg, 'warning')
            emit_flash('warning', msg)
            return {"status": "Shutdown initiated"}, 200
            
        elif action == "reboot":
            log_message("Manual reboot requested via web UI", "WARNING")
            send_ntfy("System reboot initiated via web UI", "urgent")
            trigger_system_action(action="reboot", reason="Manual reboot from UI")
            msg = '🔄 System will reboot in {} seconds'.format(config.get('reboot_delay', config.get('shutdown_delay', 60)))
            flash(msg, 'warning')
            emit_flash('warning', msg)
            return {"status": "Reboot initiated"}, 200
            
        else:
            msg = f'❌ Invalid action: {action}'
            flash(msg, 'error')
            emit_flash('error', msg)
            log_message(f"Invalid action requested: {action}", "ERROR")
            return {"status": msg}, 400
            
    except Exception as e:
        msg = f'❌ System control failed: {e}'
        flash(msg, 'error')
        emit_flash('error', msg)
        log_message(f"System control failed: {e}", "ERROR")
        return {"status": msg}, 500

@app.route('/logs')
def get_logs():
    """Retrieve recent logs"""
    try:
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, 'r') as f:
                lines = f.readlines()
                logs = [line.rstrip() for line in lines[-100:]]
            return jsonify({"logs": logs})
            log_message("Logs fetched for web interface", "INFO")    
        else:
            return jsonify({"logs": ["Log file not found."]})
    except Exception as e:
        log_message(f"Error reading logs: {e}", "ERROR")
        return jsonify({"logs": [f"Error reading logs: {e}"]})


def emit_flash(category, message):
    """Emit a flash message to all connected clients via Socket.IO"""
    socketio.emit('flash_message', {'category': category, 'message': message})




# ============================================================================
# GUNICORN/MODULE INITIALIZATION (MUST BE IN GLOBAL SCOPE)
# ============================================================================

# 1. Load configuration and history (Needs to run before hardware init/monitor thread)
load_config()
load_battery_history()

# 2. Check and configure kernel overlay on startup
configure_kernel_overlay() 

# 3. Initialize hardware (I2C/GPIO)
init_hardware()

# 4. Start monitoring thread (This monitors hardware and emits SocketIO events)
start_monitor()

# 5. Send startup ntfy
send_startup_ntfy()






# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

# Dashboard HTML template with professional UI
DASHBOARD_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ VERSION_STRING }} - UPS Monitor</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        :root {
            --primary: #3b82f6;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --dark-bg: #0f172a;
            --dark-card: #1e293b;
            --dark-border: #334155;
        }
        
        * { transition: all 0.2s ease; }
        
        body { 
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
        }
        
        html.dark body {
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
        }
        
        .glass-card {
            background: rgba(255, 255, 255, 0.9);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.2);
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
        }
        
        html.dark .glass-card {
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid rgba(51, 65, 85, 0.3);
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
        }
        /* --- LIGHT MODE FIX: Comprehensive Override --- */
        /* --- START OF LIGHT MODE FIX: FINAL COMPREHENSIVE OVERRIDE --- */
        
        /* 1. Target generic dark mode background/text classes used in inner divs */
        html:not(.dark) .dark\:bg-gray-800,
        html:not(.dark) .dark\:bg-gray-900,
        html:not(.dark) .dark\:text-white {
            background-color: #F3F4F6 !important; /* Light Grey Background */
            color: #1F2937 !important; /* Dark Text Color */
        }
        
        /* 2. Target the main card and configuration elements */
        html:not(.dark) .glass-card,
        html:not(.dark) .collapsible-content,
        html:not(.dark) .collapsible-content > *,
        html:not(.dark) #config-container {
            background-color: #FFFFFF !important; /* Force all containers to white */
            color: #1F2937 !important;
            border-color: #d1d5db !important; /* Light border */
        }

        /* 3. FIX FOR INPUT FIELDS (low_battery_threshold etc.) */
        /* Target the specific dark background class found on inputs/textareas */
        html:not(.dark) .dark\:bg-gray-700 {
            background-color: #FFFFFF !important; /* White background for input fields */
            color: #1F2937 !important; /* Dark text in input fields */
        }

        /* --- END OF LIGHT MODE FIX --- */
        .metric-card {
            position: relative;
            overflow: hidden;
        }
        
        .metric-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 4px;
            background: linear-gradient(90deg, var(--primary), var(--success));
        }
        
        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 1rem;
            border-radius: 9999px;
            font-weight: 600;
            font-size: 0.875rem;
        }
        
        .status-pulse {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        .battery-bar {
            height: 32px;
            border-radius: 8px;
            background: linear-gradient(90deg, #10b981 0%, #3b82f6 50%, #ef4444 100%);
            position: relative;
            overflow: hidden;
        }
        
        .battery-bar::after {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.3), transparent);
            animation: shimmer 2s infinite;
        }
        
        @keyframes shimmer {
            0% { transform: translateX(-100%); }
            100% { transform: translateX(100%); }
        }
        
        .collapsible-content {
            max-height: 0;
            overflow: hidden;
            transition: max-height 0.5s ease;
        }
        
        .collapsible-content.open {
            max-height: 2000px;
        }
        
        .toggle-switch {
            position: relative;
            width: 48px;
            height: 24px;
            background: #cbd5e1;
            border-radius: 24px;
            cursor: pointer;
        }
        
        .toggle-switch::after {
            content: '';
            position: absolute;
            top: 2px;
            left: 2px;
            width: 20px;
            height: 20px;
            background: white;
            border-radius: 50%;
            transition: transform 0.2s;
        }
        
        .toggle-switch.active {
            background: var(--primary);
        }
        
        .toggle-switch.active::after {
            transform: translateX(24px);
        }
        
        .alert-banner {
            padding: 1rem;
            border-radius: 0.5rem;
            margin-bottom: 1rem;
            animation: slideIn 0.3s ease;
        }
        
        @keyframes slideIn {
            from { transform: translateY(-20px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
        }
        
        .btn-primary {
            background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: white;
            padding: 0.75rem 1.5rem;
            border-radius: 0.5rem;
            font-weight: 600;
            border: none;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(59, 130, 246, 0.4);
        }
        
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 16px rgba(59, 130, 246, 0.5);
        }
        
        .chart-container {
            position: relative;
            height: 200px;
            width: 100%;
        }
    </style>
</head>
<body class="text-gray-900">
    <div class="min-h-screen p-4 md:p-8">
        <div class="max-w-7xl mx-auto">
            
            <!-- Header with Version and Dark Mode -->
            <header class="glass-card rounded-2xl p-6 mb-6">
                <div class="flex justify-between items-center">
                    <div>
                        <img src="data:image/png;base64,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"alt="Embedded Image"/>
                        <p class="text-sm text-gray-600 dark:text-gray-400 mt-1">{{ pi_model }}</p>
                    </div>
                    <div class="flex items-center gap-4">
                        <div class="text-right">
                            <p class="text-xs text-gray-500 dark:text-gray-400">Version</p>
                            <p class="text-sm font-semibold text-gray-700 dark:text-gray-300">{{ VERSION_STRING }}</p>
                            <p class="text-xs text-gray-500 dark:text-gray-400">{{ VERSION_BUILD }}</p>
                        </div>
                        <button onclick="toggleDarkMode()" class="p-3 rounded-full hover:bg-gray-200 dark:hover:bg-gray-700">
                            <svg id="darkModeIcon" class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"></path>
                        </button>
                    </div>
                </div>
            </header>
            
            <!-- Main Metrics Grid -->
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-6">
                
                <!-- Battery Level -->
                <div class="glass-card metric-card rounded-2xl p-6">
                    <div class="flex justify-between items-start mb-4">
                        <div>
                            <p class="text-sm text-gray-600 dark:text-gray-400 mb-1">Battery Level</p>
                            <p id="battery-level" class="text-4xl font-bold text-blue-600 dark:text-blue-400">{{ battery_level }}%</p>
                        </div>
                        <div class="p-3 bg-blue-100 dark:bg-blue-900 rounded-xl">
                            <svg class="w-8 h-8 text-blue-600 dark:text-blue-400" fill="currentColor" viewBox="0 0 20 20">
                                <path d="M5 2a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V4a2 2 0 00-2-2H5zm0 3a1 1 0 011-1h8a1 1 0 011 1v10a1 1 0 01-1 1H6a1 1 0 01-1-1V5z"></path>
                            </svg>
                        </div>
                    </div>
                    <div class="battery-bar rounded-lg overflow-hidden">
                        <div id="battery-fill" class="h-full bg-gradient-to-r from-green-500 to-blue-500 transition-all duration-500" style="width: {{ battery_level }}%"></div>
                    </div>
                </div>
                
                <!-- Voltage -->
                <div class="glass-card metric-card rounded-2xl p-6">
                    <div class="flex justify-between items-start mb-4">
                        <div>
                            <p class="text-sm text-gray-600 dark:text-gray-400 mb-1">Voltage</p>
                            <p id="voltage" class="text-4xl font-bold text-purple-600 dark:text-purple-400">{{ voltage }}V</p>
                        </div>
                        <div class="p-3 bg-purple-100 dark:bg-purple-900 rounded-xl">
                            <svg class="w-8 h-8 text-purple-600 dark:text-purple-400" fill="currentColor" viewBox="0 0 20 20">
                                <path fill-rule="evenodd" d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573L7.586 10 5.293 7.707a1 1 0 010-1.414l2-2.5a1 1 0 011.12-.38z" clip-rule="evenodd"></path>
                            </svg>
                        </div>
                    </div>
                </div>
                
                <!-- Power State -->
                <div class="glass-card metric-card rounded-2xl p-6">
                    <div class="flex justify-between items-start mb-4">
                        <div>
                            <p class="text-sm text-gray-600 dark:text-gray-400 mb-1">Power State</p>
                            <div id="power-state-badge" class="status-badge mt-2 {% if power_state == 'On AC Power' %}bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200{% elif power_state == 'On Battery' %}bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200{% else %}bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200{% endif %}">
                                <div class="status-pulse {% if power_state == 'On AC Power' %}bg-green-500{% elif power_state == 'On Battery' %}bg-yellow-500{% else %}bg-red-500{% endif %}"></div>
                                <span id="power-state">{{ power_state }}</span>
                            </div>
                        </div>
                        <div class="p-3 bg-green-100 dark:bg-green-900 rounded-xl">
                            <svg class="w-8 h-8 text-green-600 dark:text-green-400" fill="currentColor" viewBox="0 0 20 20">
                                <path fill-rule="evenodd" d="M6 2a1 1 0 00-1 1v1H4a2 2 0 00-2 2v10a2 2 0 002 2h12a2 2 0 002-2V6a2 2 0 00-2-2h-1V3a1 1 0 10-2 0v1H7V3a1 1 0 00-1-1zm0 5a1 1 0 000 2h8a1 1 0 100-2H6z" clip-rule="evenodd"></path>
                            </svg>
                        </div>
                    </div>
                    <p id="time-remaining" class="text-sm text-gray-600 dark:text-gray-400">⏱️ {{ time_remaining }}</p>
                </div>
                
                <!-- System Info -->
                <div class="glass-card metric-card rounded-2xl p-6">
                    <div class="flex justify-between items-start mb-4">
                        <div>
                            <p class="text-sm text-gray-600 dark:text-gray-400 mb-1">System Status</p>
                            <div class="status-badge bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200 mt-2">
                                <div class="status-pulse bg-green-500"></div>
                                <span>Running</span>
                            </div>
                        </div>
                        <div class="p-3 bg-orange-100 dark:bg-orange-900 rounded-xl">
                            <svg class="w-8 h-8 text-orange-600 dark:text-orange-400" fill="currentColor" viewBox="0 0 20 20">
                                <path fill-rule="evenodd" d="M2 5a2 2 0 012-2h12a2 2 0 012 2v10a2 2 0 01-2 2H4a2 2 0 01-2-2V5zm3.293 1.293a1 1 0 011.414 0l3 3a1 1 0 010 1.414l-3 3a1 1 0 01-1.414-1.414L7.586 10 5.293 7.707a1 1 0 010-1.414l2-2.5a1 1 0 011.12-.38z" clip-rule="evenodd"></path>
                        </svg>
                        </div>
                    </div>
                    <div class="text-sm space-y-1">
                        <p class="text-gray-600 dark:text-gray-400">🌡️ CPU: <span id="cpu-temp" class="font-semibold">{{ system_info.cpu_temp }}°C</span></p>
                        <p class="text-gray-600 dark:text-gray-400">💾 Disk (<span id="disk-label">{{ system_info.disk_label }}</span>): <span id="disk-usage" class="font-semibold">{{ system_info.disk_usage }}%</span> used (<span id="disk-free" class="font-semibold">{{ system_info.disk_free }}</span> GB free)</p>
                        <p class="text-gray-600 dark:text-gray-400">🧠 RAM: <span id="memory-info" class="font-semibold">{{ system_info.memory_info }}</span></p>
                    </div>
                </div>
            </div>
            
            <!-- Charts and Detailed Info -->
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
                
                <!-- Battery History Chart -->
                <div class="glass-card rounded-2xl p-6">
                    <h3 class="text-xl font-bold mb-4 flex items-center gap-2">
                        <svg class="w-6 h-6 text-blue-500" fill="currentColor" viewBox="0 0 20 20">
                            <path d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zM8 7a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zM14 4a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z"></path>
                        </svg>
                        Battery History
                    </h3>
                    <div class="chart-container">
                        <canvas id="batteryChart"></canvas>
                    </div>
                </div>
                
                <!-- Hardware Status -->
                <div class="glass-card rounded-2xl p-6">
                    <h3 class="text-xl font-bold mb-4 flex items-center gap-2">
                        <svg class="w-6 h-6 text-purple-500" fill="currentColor" viewBox="0 0 20 20">
                            <path fill-rule="evenodd" d="M3 3a1 1 0 000 2v8a2 2 0 002 2h2.586l-1.293 1.293a1 1 0 101.414 1.414L10 15.414l2.293 2.293a1 1 0 001.414-1.414L12.414 15H15a2 2 0 002-2V5a1 1 0 100-2H3zm11.707 4.707a1 1 0 00-1.414-1.414L10 9.586 8.707 8.293a1 1 0 00-1.414 0l-2 2a1 1 0 101.414 1.414L8 10.414l1.293 1.293a1 1 0 001.414 0l4-4z" clip-rule="evenodd"></path>
                        </svg>
                        Hardware Status
                    </h3>
                    <div class="space-y-4">
                        <div class="flex justify-between items-center p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                            <span class="font-semibold">I2C Bus</span>
                            <span id="i2c-status" class="{% if not hardware_error %}text-green-500{% else %}text-red-500{% endif %}">
                                {% if not hardware_error %}✅ {{ i2c_addr }}{% else %}❌ Error{% endif %}
                            </span>
                        </div>
                        <div class="flex justify-between items-center p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                            <span class="font-semibold">GPIO Interface</span>
                            <span id="gpio-status" class="{% if not gpio_error %}text-green-500{% else %}text-yellow-500{% endif %}">
                                {% if not gpio_error %}✅ Active{% else %}⚠️ Limited{% endif %}
                            </span>
                        </div>
                        <div class="flex justify-between items-center p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                            <span class="font-semibold">Uptime</span>
                            <span id="uptime" class="text-blue-500 font-mono">{{ system_info.uptime }}</span>
                        </div>
                        <div class="flex justify-between items-center p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                            <span class="font-semibold">Last Update</span>
                            <span id="last-update" class="text-gray-500 text-sm">{{ timestamp }}</span>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Configuration and System Control Sections (Two-Column Layout) -->
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
                <!-- Configuration Section -->
                <div class="glass-card rounded-2xl p-6">
                    <button onclick="toggleSection('config')" class="w-full flex justify-between items-center">
                        <h3 class="text-xl font-bold flex items-center gap-2">
                            <svg class="w-6 h-6 text-indigo-500" fill="currentColor" viewBox="0 0 20 20">
                                <path fill-rule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clip-rule="evenodd"></path>
                            </svg>
                            Configuration
                        </h3>
                        <svg id="config-arrow" class="w-6 h-6 transform transition-transform" fill="currentColor" viewBox="0 0 20 20">
                            <path fill-rule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clip-rule="evenodd"></path>
                        </svg>
                    </button>
                    
                    <div id="config-content" class="collapsible-content mt-6">
                        <form id="config-form" method="POST" action="/configure" class="grid grid-cols-1 md:grid-cols-2 gap-6">
                            
                            <!-- Battery Thresholds -->
                            <div class="space-y-4">
                                <h4 class="font-semibold text-lg border-b pb-2">Battery Thresholds</h4>
                                
                                <div>
                                    <label class="block text-sm font-medium mb-2">Low Battery Warning (%)</label>
                                    <input type="number" step="0.1" name="low_battery_threshold" 
                                        value="{{ config.low_battery_threshold }}" required
                                        class="w-full p-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600">
                                </div>
                                
                                <div>
                                    <label class="block text-sm font-medium mb-2">Critical Threshold (%)</label>
                                    <input type="number" step="0.1" name="critical_low_threshold" 
                                        value="{{ config.critical_low_threshold }}" required
                                        class="w-full p-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600">
                                </div>
                            </div>
                            
                            <!-- System Thresholds -->
                            <div class="space-y-4">
                                <h4 class="font-semibold text-lg border-b pb-2">System Thresholds</h4>
                                
                                <div>
                                    <label class="block text-sm font-medium mb-2">CPU Temperature (°C)</label>
                                    <input type="number" step="0.1" name="cpu_temp_threshold" 
                                        value="{{ config.cpu_temp_threshold }}" required
                                        class="w-full p-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600">
                                </div>
                                
                                <div>
                                    <label class="block text-sm font-medium mb-2">Disk Space Warning (GB)</label>
                                    <input type="number" step="0.1" name="disk_space_threshold" 
                                        value="{{ config.disk_space_threshold }}" required
                                        class="w-full p-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600">
                                </div>
                            </div>
                            
                            <!-- Monitoring Settings -->
                            <div class="space-y-4">
                                <h4 class="font-semibold text-lg border-b pb-2">Monitoring Settings</h4>
                                
                                <div>
                                    <label class="block text-sm font-medium mb-2">Update Interval (seconds) on AC </label>
                                    <input type="number" step="1" name="monitor_interval" 
                                        value="{{ config.monitor_interval }}" required
                                        class="w-full p-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600">
                                </div>
                                
                                <div>
                                    <label class="block text-sm font-medium mb-2">Shutdown Delay (seconds)</label>
                                    <input type="number" step="1" name="shutdown_delay" 
                                        value="{{ config.shutdown_delay }}" required
                                        class="w-full p-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600">
                                </div>
                                
                                <div class="flex items-center gap-3">
                                    <input type="checkbox" name="enable_auto_shutdown" value="1" 
                                        {% if config.enable_auto_shutdown %}checked{% endif %}
                                        class="w-5 h-5 text-blue-600 rounded">
                                    <label class="font-medium">Enable Auto-Shutdown</label>
                                </div>
                                <div class="flex items-center gap-3">
                                    <input type="checkbox" name="debug" value="1" 
                                        {% if config.debug %}checked{% endif %}
                                        class="w-5 h-5 text-blue-600 rounded">
                                    <label class="font-medium">Enable Debug Logging</label>
                                </div>                               
                            </div>
                            
                            <!-- Notification Settings -->
                            <div class="space-y-4">
                                <h4 class="font-semibold text-lg border-b pb-2">Notifications (ntfy)</h4>
                                
                                <div class="flex items-center gap-3">
                                    <input type="checkbox" name="enable_ntfy" value="1" 
                                        {% if config.enable_ntfy %}checked{% endif %}
                                        class="w-5 h-5 text-blue-600 rounded">
                                    <label class="font-medium">Enable ntfy Notifications</label>
                                </div>
                                
                                <div>
                                    <label class="block class="block text-sm font-medium mb-2">ntfy Server</label>
                                    <input type="text" name="ntfy_server" 
                                        value="{{ config.ntfy_server }}"
                                        placeholder="https://ntfy.sh"
                                        class="w-full p-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600">
                                </div>
                                
                                <div>
                                    <label class="block text-sm font-medium mb-2">ntfy Topic</label>
                                    <input type="text" name="ntfy_topic" 
                                        value="{{ config.ntfy_topic }}"
                                        placeholder="x728_UPS"
                                        class="w-full p-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600">
                                </div>
                            </div>
                            
                            <!-- Submit Button -->
                            <div class="md:col-span-2">
                                <button type="submit" class="btn-primary w-full">
                                    💾 Save Configuration
                                </button>
                            </div>
                        </form>
                        
                        
                    </div>
                </div>
                
                <!-- System Control Section -->
                <div class="glass-card rounded-2xl p-6">
                    <button onclick="toggleSection('control')" class="w-full flex justify-between items-center">
                        <h3 class="text-xl font-bold flex items-center gap-2">
                            <svg class="w-6 h-6 text-red-500" fill="currentColor" viewBox="0 0 20 20">
                                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z" clip-rule="evenodd"></path>
                            </svg>
                            System Control
                        </h3>
                        <svg id="control-arrow" class="w-6 h-6 transform transition-transform" fill="currentColor" viewBox="0 0 20 20">
                            <path fill-rule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clip-rule="evenodd"></path>
                        </svg>
                    </button>
                    <div id="control-content" class="collapsible-content mt-6">
                        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                            <form id="reboot-form">
                                <input type="hidden" name="action" value="reboot">
                                <button type="submit" class="w-full p-4 bg-yellow-500 hover:bg-yellow-600 text-white font-bold rounded-lg transition duration-200 flex items-center justify-center gap-2">
                                    <svg class="w-6 h-6" fill="currentColor" viewBox="0 0 20 20">
                                        <path fill-rule="evenodd" d="M4 2a1 1 0 011 1v2.101a7.002 7.002 0 0111.601 2.566 1 1 0 11-1.885.666A5.002 5.002 0 005.999 7H9a1 1 0 010 2H4a1 1 0 01-1-1V3a1 1 0 011-1zm.008 9.057a1 1 0 011.276.61A5.002 5.002 0 0014.001 13H11a1 1 0 110-2h5a1 1 0 011 1v5a1 1 0 11-2 0v-2.101a7.002 7.002 0 01-11.601-2.566 1 1 0 01.61-1.276z" clip-rule="evenodd"></path>
                                </svg>
                                Reboot System
                            </button>
                        </form>
                        <form id="shutdown-form">
                            <input type="hidden" name="action" value="shutdown">
                            <button type="submit" class="w-full p-4 bg-red-600 hover:bg-red-700 text-white font-bold rounded-lg transition duration-200 flex items-center justify-center gap-2">
                                <svg class="w-6 h-6" fill="currentColor" viewBox="0 0 20 20">
                                    <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm-1-7V7a1 1 0 112 0v4a1 1 0 01-2 0zm1 4a1.5 1.5 0 100-3 1.5 1.5 0 000 3z" clip-rule="evenodd"></path>
                                </svg>
                                Shutdown System
                            </button>
                        </form>
                    </div>
                    <div id="cancel-action-panel" class="mt-4 hidden flex flex-col items-center">
                        <button id="cancel-action-btn" class="w-full p-4 bg-blue-600 hover:bg-blue-700 text-white font-bold rounded-lg transition duration-200 flex items-center justify-center gap-2">
                            <svg class="w-6 h-6" fill="currentColor" viewBox="0 0 20 20">
                                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8 7a1 1 0 00-1 1v4a1 1 0 001 1h4a1 1 0 001-1V8a1 1 0 00-1-1H8z" clip-rule="evenodd"></path>
                            </svg>
                            Cancel Pending Action (<span id="cancel-timer">--</span>s)
                        </button>
                        <div id="cancel-action-type" class="mt-2 text-blue-700 dark:text-blue-300 font-semibold"></div>
                    </div>                   
                        
                        <div class="mt-4 p-4 bg-yellow-50 dark:bg-yellow-900 border-l-4 border-yellow-500 rounded">
                            <p class="text-sm text-yellow-800 dark:text-yellow-200">
                                <strong>⚠️ Warning:</strong> System control requires proper Docker privileges (--privileged or sudo access). 
                                Shutdown will occur after a {{ config.shutdown_delay }}-second delay.
                            </p>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Logs Section (Single-Column Layout) -->
            <div class="glass-card rounded-2xl p-6">
                <button onclick="toggleSection('logs')" class="w-full flex justify-between items-center">
                    <h3 class="text-xl font-bold flex items-center gap-2">
                        <svg class="w-6 h-6 text-gray-500" fill="currentColor" viewBox="0 0 20 20">
                            <path d="M9 2a1 1 0 000 2h2a1 1 0 100-2H9z"></path>
                            <path fill-rule="evenodd" d="M4 5a2 2 0 012-2 3 3 0 003 3h2a3 3 0 003-3 2 2 0 012 2v11a2 2 0 01-2 2H6a2 2 0 01-2-2V5zm3 4a1 1 0 000 2h.01a1 1 0 100-2H7zm3 0a1 1 0 000 2h3a1 1 0 100-2h-3zm-3 4a1 1 0 100 2h.01a1 1 0 100-2H7zm3 0a1 1 0 100 2h3a1 1 0 100-2h-3z" clip-rule="evenodd"></path>
                        </svg>
                        Application Logs
                    </h3>
                    <svg id="logs-arrow" class="w-6 h-6 transform transition-transform" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clip-rule="evenodd"></path>
                    </svg>
                </button>
                
                <div id="logs-content" class="collapsible-content mt-6">
                    <div class="bg-gray-900 text-green-400 font-mono text-xs p-4 rounded-lg h-96 overflow-y-auto" id="log-display">
                        Loading logs...
                    </div>
                    <div class="mt-4 flex items-center justify-between">
                        <label class="flex items-center gap-2">
                            <input type="checkbox" id="auto-refresh-toggle" checked class="w-5 h-5 text-blue-600 rounded">
                            <span class="text-sm font-medium">Auto Refresh Logs (30s)</span>
                        </label>
                        <button onclick="refreshLogs()" class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg">
                            🔄 Refresh Now
                        </button>
                    </div>
                </div>
            </div>
        
    <script>
        // WebSocket Connection
        const socket = io();
        
        // Chart.js Configuration
        let batteryChart;
        const historyData = {{ history|safe }};
        
        function initChart() {
            const ctx = document.getElementById('batteryChart');
            if (!ctx) return;
            
            batteryChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: historyData.map(d => d.time),
                    datasets: [
                        {
                            label: 'Battery %',
                            data: historyData.map(d => d.battery),
                            borderColor: 'rgb(59, 130, 246)',
                            backgroundColor: 'rgba(59, 130, 246, 0.1)',
                            tension: 0.4,
                            yAxisID: 'y'
                        },
                        {
                            label: 'Voltage',
                            data: historyData.map(d => d.voltage),
                            borderColor: 'rgb(168, 85, 247)',
                            backgroundColor: 'rgba(168, 85, 247, 0.1)',
                            tension: 0.4,
                            yAxisID: 'y1'
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { display: true, position: 'top' }
                    },
                    scales: {
                        y: {
                            type: 'linear',
                            display: true,
                            position: 'left',
                            title: { display: true, text: 'Battery %' }
                        },
                        y1: {
                            type: 'linear',
                            display: true,
                            position: 'right',
                            title: { display: true, text: 'Voltage (V)' },
                            grid: { drawOnChartArea: false }
                        }
                    }
                }
            });
        }
        
        // Update UI with WebSocket data
        function updateUI(data) {
            // Battery
            const battery = parseFloat(data.battery_level);
            document.getElementById('battery-level').textContent = battery.toFixed(1) + '%';
            const batteryFill = document.getElementById('battery-fill');
            batteryFill.style.width = battery + '%';
            
            // Color coding
            if (battery <= 10) {
                batteryFill.className = 'h-full bg-gradient-to-r from-red-600 to-red-500 transition-all duration-500';
            } else if (battery <= 30) {
                batteryFill.className = 'h-full bg-gradient-to-r from-yellow-500 to-orange-500 transition-all duration-500';
            } else {
                batteryFill.className = 'h-full bg-gradient-to-r from-green-500 to-blue-500 transition-all duration-500';
            }
            
            // Voltage & Current
            document.getElementById('voltage').textContent = parseFloat(data.voltage).toFixed(2) + 'V';
                        
            // Power State
            const powerState = data.power_state;
            const powerBadge = document.getElementById('power-state-badge');
            document.getElementById('power-state').textContent = powerState;
            
            powerBadge.className = 'status-badge mt-2';
            if (powerState === 'On AC Power') {
                powerBadge.classList.add('bg-green-100', 'text-green-800', 'dark:bg-green-900', 'dark:text-green-200');
                document.querySelector('#power-state-badge .status-pulse').classList.remove('bg-yellow-500', 'bg-red-500');
                document.querySelector('#power-state-badge .status-pulse').classList.add('bg-green-500');
            } else if (powerState === 'On Battery') {
                powerBadge.classList.add('bg-yellow-100', 'text-yellow-800', 'dark:bg-yellow-900', 'dark:text-yellow-200');
                document.querySelector('#power-state-badge .status-pulse').classList.remove('bg-green-500', 'bg-red-500');
                document.querySelector('#power-state-badge .status-pulse').classList.add('bg-yellow-500');
            } else {
                powerBadge.classList.add('bg-red-100', 'text-red-800', 'dark:bg-red-900', 'dark:text-red-200');
                document.querySelector('#power-state-badge .status-pulse').classList.remove('bg-green-500', 'bg-yellow-500');
                document.querySelector('#power-state-badge .status-pulse').classList.add('bg-red-500');
            }
            
            // Time remaining
            document.getElementById('time-remaining').textContent = '⏱️ ' + data.time_remaining;
            
            // System info
            document.getElementById('cpu-temp').textContent = data.system_info.cpu_temp + '°C';
            document.getElementById('disk-label').textContent = data.system_info.disk_label;
            document.getElementById('disk-usage').textContent = data.system_info.disk_usage + '%';
            document.getElementById('disk-free').textContent = data.system_info.disk_free;
            document.getElementById('memory-info').textContent = data.system_info.memory_info;  // New
            
            // Last update
            document.getElementById('last-update').textContent = new Date().toLocaleString();
            
            // Update chart
            if (batteryChart) {
                const now = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                batteryChart.data.labels.push(now);
                batteryChart.data.datasets[0].data.push(parseFloat(data.battery_level));
                batteryChart.data.datasets[1].data.push(parseFloat(data.voltage));
                
                if (batteryChart.data.labels.length > 50) {
                    batteryChart.data.labels.shift();
                    batteryChart.data.datasets[0].data.shift();
                    batteryChart.data.datasets[1].data.shift();
                }
                
                batteryChart.update();
            }
        }
        
        // Dark mode toggle
        function toggleDarkMode() {
            document.documentElement.classList.toggle('dark');
            localStorage.setItem('darkMode', document.documentElement.classList.contains('dark'));
            const icon = document.getElementById('darkModeIcon');
            if (document.documentElement.classList.contains('dark')) {
                icon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"></path>';
            } else {
                icon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"></path>';
            }
        }
        
        // Load dark mode preference
        if (localStorage.getItem('darkMode') === 'true') {
            document.documentElement.classList.add('dark');
            document.getElementById('darkModeIcon').innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"></path>';
        }
        
        // Section collapse toggle
        function toggleSection(section) {
            const content = document.getElementById(section + '-content');
            const arrow = document.getElementById(section + '-arrow');
            
            content.classList.toggle('open');
            arrow.classList.toggle('rotate-180');
        }
        
        // Refresh logs
        function refreshLogs() {
            fetch('/logs')
                .then(response => response.json())
                .then(data => {
                    const logDisplay = document.getElementById('log-display');
                    logDisplay.innerHTML = data.logs.map(line => line + '<br>').join('');
                    logDisplay.scrollTop = logDisplay.scrollHeight;
                })
                .catch(err => {
                    console.error('Failed to fetch logs:', err);
                    document.getElementById('log-display').innerHTML = 'Failed to load logs: ' + err + '<br>';
                });
        }

        // Auto refresh toggle
        let autoRefreshInterval;
        document.addEventListener('DOMContentLoaded', () => {
            initChart();
            refreshLogs();
            
            const toggle = document.getElementById('auto-refresh-toggle');
            autoRefreshInterval = setInterval(refreshLogs, 30000);  // Start auto by default
            
            toggle.addEventListener('change', () => {
                if (toggle.checked) {
                    autoRefreshInterval = setInterval(refreshLogs, 30000);
                } else {
                    clearInterval(autoRefreshInterval);
                }
            });
        });
        
        // WebSocket event handlers
        socket.on('connect', () => {
            console.log('Connected to server');
            refreshLogs();
        });
        
        socket.on('status_update', (data) => {
            updateUI(data);
        });
        
        // Initialize on load
        document.addEventListener('DOMContentLoaded', () => {
            initChart();
            refreshLogs();
            
            // Auto-refresh logs every 30 seconds
            setInterval(refreshLogs, 30000);
        });
        
        // Cancel action polling and button logic
        function pollPendingAction() {
            fetch('/system/pending')
                .then(res => res.json())
                .then(data => {
                    const panel = document.getElementById('cancel-action-panel');
                    const timer = document.getElementById('cancel-timer');
                    const type = document.getElementById('cancel-action-type');
                    if (data.type) {
                        panel.classList.remove('hidden');
                        timer.textContent = data.remaining;
                        type.textContent = "Pending " + data.type.charAt(0).toUpperCase() + data.type.slice(1);
                    } else {
                        panel.classList.add('hidden');
                    }
                });
        }
        setInterval(pollPendingAction, 1000);
        document.addEventListener('DOMContentLoaded', pollPendingAction);

        document.getElementById('cancel-action-btn').onclick = function() {
            fetch('/system/cancel', {method: 'POST'})
                .then(() => setTimeout(pollPendingAction, 500));
        };

        if (typeof io !== "undefined") {
            socket.on('cancel_update', pollPendingAction);
        }
        
    // Intercept reboot form submit
    document.getElementById('reboot-form').onsubmit = function(e) {
        e.preventDefault();
        if (!confirm('⚠️ Are you sure you want to REBOOT the system?')) return false;
        fetch('/system/control', {
            method: 'POST',
            body: new FormData(this)
        }).then(res => res.json())
          .then(data => {
              pollPendingAction();
          });
        return false;
    };

    // Intercept shutdown form submit
    document.getElementById('shutdown-form').onsubmit = function(e) {
        e.preventDefault();
        if (!confirm('🚨 Are you sure you want to SHUTDOWN the system?')) return false;
        fetch('/system/control', {
            method: 'POST',
            body: new FormData(this)
        }).then(res => res.json())
          .then(data => {
              pollPendingAction();
          });
        return false;
    };
    // Intercept config form submit to handle asynchronously
    document.getElementById('config-form').onsubmit = function(e) {
        e.preventDefault();
        fetch('/configure', {
            method: 'POST',
            body: new FormData(this)
        })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                console.log('Configuration saved:', data.message);
                // Do not collapse the section; keep it open
                refreshLogs(); // Update logs to show debug messages if enabled
            } else {
                console.error('Configuration save failed:', data.message);
                showFlashMessage('error', data.message);
            }
        })
        .catch(err => {
            console.error('Failed to save configuration:', err);
            showFlashMessage('error', 'Failed to save configuration: ' + err);
        });
        return false;
    };
    // Cancel action polling and button logic (already present, but ensure it's here)
    function pollPendingAction() {
        fetch('/system/pending')
            .then(res => res.json())
            .then(data => {
                const panel = document.getElementById('cancel-action-panel');
                const timer = document.getElementById('cancel-timer');
                const type = document.getElementById('cancel-action-type');
                if (data.type) {
                    panel.classList.remove('hidden');
                    timer.textContent = data.remaining;
                    type.textContent = "Pending " + data.type.charAt(0).toUpperCase() + data.type.slice(1);
                } else {
                    panel.classList.add('hidden');
                }
            });
    }
    setInterval(pollPendingAction, 1000);
    document.addEventListener('DOMContentLoaded', pollPendingAction);

    document.getElementById('cancel-action-btn').onclick = function() {
        fetch('/system/cancel', {method: 'POST'})
            .then(() => setTimeout(pollPendingAction, 500));
    };

    if (typeof io !== "undefined") {
        socket.on('cancel_update', pollPendingAction);
    }

    // Listen for flash_message events from the server
    socket.on('flash_message', function(data) {
        showFlashMessage(data.category, data.message);
    });

    // Dynamically show flash message (with auto-close and ramp color)
    function showFlashMessage(category, message) {
        // Remove any existing flash
        let old = document.getElementById('flash-message');
        if (old) old.remove();

        // Color classes
        let colorClass = '';
        if (category === 'error') colorClass = 'bg-red-100 text-red-800 border-l-4 border-red-500 dark:bg-red-900 dark:text-red-200';
        else if (category === 'warning') colorClass = 'bg-yellow-100 text-yellow-800 border-l-4 border-yellow-500 dark:bg-yellow-900 dark:text-yellow-200';
        else colorClass = 'bg-green-100 text-green-800 border-l-4 border-green-500 dark:bg-green-900 dark:text-green-200';

        // Create flash message element
        let div = document.createElement('div');
        div.id = 'flash-message';
        div.className = `alert-banner relative px-4 py-3 rounded-lg flex items-center gap-3 shadow-lg animate-fade-in ${colorClass}`;
        div.innerHTML = `
            <span class="font-semibold">${message}</span>
            <span id="flash-timer" class="ml-3 text-xs font-bold px-2 py-1 rounded bg-white bg-opacity-40 text-gray-700 dark:text-gray-200"></span>
            <button onclick="closeFlash()" class="absolute top-2 right-2 text-xl font-bold text-gray-400 hover:text-gray-700 dark:hover:text-white" aria-label="Close">&times;</button>
        `;

        // Insert below header
        let container = document.querySelector('.max-w-7xl');
        container.insertBefore(div, container.children[1]);

        // Timer and ramp color
        let flashSeconds = 5;
        let timerSpan = div.querySelector('#flash-timer');
        function rampFlash() {
            timerSpan.textContent = flashSeconds + 's';
            div.style.transition = 'background 0.5s';
            if (flashSeconds === 5) div.style.background = '#f59e0b';
            else if (flashSeconds === 4) div.style.background = '#fbbf24';
            else if (flashSeconds === 3) div.style.background = '#fde68a';
            else if (flashSeconds === 2) div.style.background = '#bbf7d0';
            else if (flashSeconds === 1) div.style.background = '#a7f3d0';
            flashSeconds--;
            if (flashSeconds >= 0) setTimeout(rampFlash, 1000);
            else closeFlash();
        }
        rampFlash();
    }

    function closeFlash() {
        let flashMsg = document.getElementById('flash-message');
        if (flashMsg) {
            flashMsg.style.opacity = '0';
            setTimeout(() => {
                if (flashMsg) flashMsg.remove();
            }, 300);
        }
    }
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    print(f"Starting {VERSION_STRING} - {VERSION_BUILD}")
   # Start Flask application
    try:
        log_message(f"Starting web server on port 5000...")
        
        socketio.run(
            app, 
            host='0.0.0.0', 
            port=5000, 
            debug=False, 
            allow_unsafe_werkzeug=True
        )
    except Exception as e:
        log_message(f"CRITICAL: Server failed to start: {e}", "CRITICAL")
        raise

