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
Version: 3.1.9
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
   (should  work  on all debians like dietpi,retropi,ubuntu that support this overlaytree)
- MQTT PUBLISHING! for Home Automation integration (Home Assistant, Node-RED, etc.) as of v3.1.8

CHANGELOG:
- v3.1.9 :  MQTT support added + runs script as a service -install/uninstall script included for host mode (non-docker) 
             |_  1-direct,2-as a service or  3-docker  modes ¬! 
- v3.1.8 :  fixing race conditions and optimising as testing both docker and python direct modes
- v3.1.7 :  using x728s built-in RTC (Maxim DS323) now if network goes offline logs and dates use RTC as fallback 
             |_ then back to os clock when back online and sync.Added ntfy message on network drop/recovery  





USAGE TIPS:
1. Ensure the X728 UPS HAT is properly connected to your Raspberry Pi.
2. Access the web dashboard at `http://<your-pi-ip>:7728` after starting the script.
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
# ============================================================================
# IMPORTS
# ============================================================================

import socket
import sys
import os
import subprocess
import time
import struct
import json
import threading
import secrets
import paho.mqtt.client as mqtt
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template_string, jsonify, request, flash, redirect, url_for
from flask_socketio import SocketIO
import smbus2 as smbus
import gpiod
import requests
import psutil
import argparse

# ============================================================================
# CONFIGURATION AND INITIALIZATION
# ============================================================================

VERSION_NUMBER= "3.1.9"
VERSION_STRING = "Presto X728-UPS Monitor"

def get_execution_mode():
    """Determine the execution mode and return the appropriate VERSION_BUILD string."""
    # Check for Docker first
    if os.path.exists('/.dockerenv'):
        return "Docker Edition"
    
    try:
        # Use psutil to check the parent process
        current_process = psutil.Process()
        parent_process = current_process.parent()
        
        # If parent process is 'systemd', we're running as a service
        if parent_process.name() == 'systemd':
            return "Python Service"
        
        # Additional check: Look for systemd-specific environment variable
        if os.environ.get('INVOCATION_ID') and os.path.exists('/etc/systemd/system/presto_x728-ups-monitor.service'):
            return "Python Service"
        
        # Default to Python Direct if not Docker or systemd
        return "Python Direct"
    
    except (psutil.NoSuchProcess, psutil.AccessDenied, FileNotFoundError):
        # Fallback to Python Direct if process inspection fails
        return "Python Direct"

VERSION_BUILD = get_execution_mode()





# --- GLOBAL VERSION CHECK STATE (display on ui top right as emoji)---

GITHUB_REPO = "piklz/pi_ups_monitors"

# Use the new VERSION_NUMBER directly
CURRENT_VERSION = VERSION_NUMBER 
LATEST_VERSION_INFO = {
    "latest": CURRENT_VERSION,
    "last_check": 0,
    "update_available": False
}




# Check every 1 hour (3600 seconds) in the background

#VERSION_CHECK_INTERVAL = 3600 
# Check every 12 hours (43200 seconds) to stay well under the 60/hour limit
# Check every 24 hours (86400 seconds) for maximum safety
VERSION_CHECK_INTERVAL = 86400
# -------------------------------------





# Set to 'INFO' for clean output, 'DEBUG' for full publishing details.
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper() 

# Map levels for comparison
LOG_LEVEL_MAP = {
    'DEBUG': 1,
    'INFO': 2,
    'WARNING': 3,
    'ERROR': 4,
    'CRITICAL': 5
}



# --- MQTT flask setup --
SETUP_COMPLETE = False
MONITOR_INTERVAL_SEC = 1.0  # How often system stats are collected for MQTT PUBLISHING
MQTT_PUBLISH_INTERVAL_SEC = int(os.environ.get('MQTT_PUBLISH_INTERVAL', 10)) # Default to 10 seconds
FIRST_CONNECTION_MADE = False
# ---  MQTT Configuration ---
MQTT_BROKER = os.environ.get('MQTT_BROKER', '') # Or the IP address of your HA/Mosquitto
MQTT_PORT = int(os.environ.get('MQTT_PORT', 1883))
MQTT_USER = os.environ.get('MQTT_USER', None)
MQTT_PASSWORD = os.environ.get('MQTT_PASSWORD', None)
MQTT_BASE_TOPIC = os.environ.get('MQTT_BASE_TOPIC', 'presto_x728_ups')
# -------------------------------





app = Flask(__name__)

app.secret_key = os.environ.get(
    'FLASK_PRESTO_X728_SECRET_KEY', 
    secrets.token_hex(32) # Generate a strong 64-character (32-byte) random key as fallback
)
app.config['SESSION_TYPE'] = 'null'
app.config['SESSION_PERMANENT'] = False

socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    manage_session=False, 
    transports=['websocket', 'polling'],
    async_mode='threading'
)





# ============================================================================
# HARDWARE GPIO PINS BCM GLOBALS
# ============================================================================


# Time Synchronization Controls SYSTEM/RTC GLOBALS
I2C_ADDRS = [0x16, 0x36, 0x3b, 0x4b]
I2C_BUS = 1
RTC_I2C_ADDR = 0x68            # Standard address for DS3231 (often used in X728)
RTC_SYNC_INTERVAL_HRS = 24     # Check network time every 24 hour
LAST_NETWORK_SYNC_TIME = 0     # Timestamp of the last successful network time check
LAST_TIME_SOURCE = "UNKNOWN"   # Tracks the source used in the previous check
LAST_NETWORK_DROP_TIME = None  # Stores the time.time() timestamp of the last confirmed network drop
#for config changes live updates rechecks load_config()
LAST_CONFIG_MTIME = 0

# X728 GPIO Pins (BCM numbering) - Initial Definition
GPIO_PLD_PIN = 6          # Power Loss Detection - Constant
GPIO_SHUTDOWN_PIN = 13    # Shutdown signal to UPS (Default V1.x)
X728_HW_VERSION = 1       # Default 1  (for Models V1.x/GPIO 13, 2 for Models V2.x+/GPIO 26) 


DEFAULT_CONFIG = {
    "low_battery_threshold": 10.0,
    "critical_low_threshold": 2.0,
    "cpu_temp_threshold": 65.0,
    "disk_space_threshold": 10.0,
    "enable_ntfy": 1,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "x728_UPS",
    "debug": 1,
    "monitor_interval": 10,
    "enable_auto_shutdown": 1,
    "shutdown_delay": 60,
    "idle_load_ma": 800  # Idle current draw in mA for time estimation 500-800mA typical
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


# --- Dynamic Path Definition for Docker and Local Execution ---


#GLOBAL PATHS 
# paths for configs
CONFIG_PATH = "/config/x728_config.json"
LOG_PATH = "/config/x728_debug.log"
HISTORY_PATH = "/config/battery_history.json"


IS_DOCKER = os.path.exists('/.dockerenv')

#  Set the Base Configuration Directory:
# If IS_DOCKER is True, use the absolute path for the volume mount ('/config').
# If IS_DOCKER is False (host/direct run), use the relative path ('./config').
CONFIG_DIR = '/config' if IS_DOCKER else './config'

#  Define the final file paths using the determined base directory.
CONFIG_PATH  = os.path.join(CONFIG_DIR, 'x728_config.json')
HISTORY_PATH = os.path.join(CONFIG_DIR, 'x728_history.json')
LOG_PATH     = os.path.join(CONFIG_DIR, 'x728_debug.log')

# Disk path - for direct run, use '/'; for Docker, '/host' if mounted
DISK_PATH = '/' if not os.path.exists('/.dockerenv') else '/host'

# --- End Dynamic Path Definition ---







# ============================================================================
# SERVICE INSTALLATION FUNCTIONS (Host-only, not Docker)
# ============================================================================

def install_service():
    """Install systemd service for auto-start (Host mode only)"""
    if os.path.exists('/.dockerenv'):
        print("❌ Service installation not needed in Docker mode.")
        print("   Use 'docker-compose up -d' or 'docker run --restart=unless-stopped' instead.")
        return False
    
    service_name = "presto_x728-ups-monitor.service"
    service_path = f"/etc/systemd/system/{service_name}"
    script_path = os.path.abspath(__file__)
    # Stable script location for the service
    stable_script_path = "/usr/local/bin/presto_x728_sysmon.py"
    
    # Copy the script to the stable location
    try:
        import shutil
        shutil.copy2(script_path, stable_script_path)
        os.chmod(stable_script_path, 0o755)  # Ensure executable permissions
        print(f"📝 Copied script to stable location: {stable_script_path}")
    except Exception as e:
        print(f"❌ Failed to copy script to {stable_script_path}: {e}")
        return False
    
    # Build ExecStart with ONLY runtime args (hw-version, MQTT, log-level)
    exec_args = []
    
    # Hardware version (runtime)
    if args.hw_version in [1, 2]:
        exec_args.append(f"--hw-version {args.hw_version}")
    
    # MQTT settings (runtime globals, not in config file)
    if args.mqtt_broker:
        exec_args.append(f"--mqtt-broker {args.mqtt_broker}")
    if args.mqtt_port:
        exec_args.append(f"--mqtt-port {args.mqtt_port}")
    if args.mqtt_user is not None:  # Include even if empty string
        exec_args.append(f"--mqtt-user \"{args.mqtt_user}\"")  # Quote to handle spaces/special chars
    if args.mqtt_password is not None:  # Include even if empty
        safe_pass = args.mqtt_password.replace('"', '\\"')
        exec_args.append(f"--mqtt-password \"{safe_pass}\"")
    if args.mqtt_topic:
        exec_args.append(f"--mqtt-topic {args.mqtt_topic}")
    if args.mqtt_publish_interval:
        exec_args.append(f"--mqtt-publish-interval {args.mqtt_publish_interval}")
    
    # Logging (console level, runtime)
    if args.log_level and args.log_level != 'INFO':
        exec_args.append(f"--log-level {args.log_level}")
    
    # Construct the full ExecStart command using the stable script path
    exec_start = f"/usr/bin/python3 {stable_script_path}"
    if exec_args:
        exec_start += " " + " ".join(exec_args)
    
    service_content = f"""[Unit]
Description=X728 UPS Monitor Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory={os.path.dirname(script_path)}
ExecStart={exec_start}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    
    try:
        print("🔧 Installing systemd service...")
        print(f"📝 Service will run with: {exec_start}")
        
        # Check if the service is already running
        service_running = False
        try:
            result = subprocess.run(['systemctl', 'is-active', service_name], capture_output=True, text=True, check=False)
            service_running = result.stdout.strip() == 'active'
            if service_running:
                print(f"🛑 Service {service_name} is currently running. Stopping it to apply new configuration...")
                subprocess.run(['systemctl', 'stop', service_name], check=True)
        except subprocess.CalledProcessError as e:
            print(f"[INFO] Could not check service status: {e}. Assuming service is not running.")
        
        # Write service file
        with open(service_path, 'w') as f:
            f.write(service_content)
        
        # Apply config-related args to the config file (persistent)
        print("📝 Applying configuration overrides to config file...")
        initialize_files()  # Ensure files exist
        load_config()  # Load current config
        updated = False
        
        # Apply config overrides (these affect config dict)
        if args.low_battery is not None:
            config['low_battery_threshold'] = args.low_battery
            updated = True
        if args.critical_battery is not None:
            config['critical_low_threshold'] = args.critical_battery
            updated = True
        if args.cpu_temp is not None:
            config['cpu_temp_threshold'] = args.cpu_temp
            updated = True
        if args.disk_space is not None:
            config['disk_space_threshold'] = args.disk_space
            updated = True
        if args.monitor_interval is not None:
            config['monitor_interval'] = args.monitor_interval
            updated = True
        if args.shutdown_delay is not None:
            config['shutdown_delay'] = args.shutdown_delay
            updated = True
        if args.disable_auto_shutdown:
            config['enable_auto_shutdown'] = 0
            updated = True
        if args.enable_ntfy:
            config['enable_ntfy'] = 1
            updated = True
        if args.disable_ntfy:
            config['enable_ntfy'] = 0
            updated = True
        if args.ntfy_server:
            config['ntfy_server'] = args.ntfy_server
            updated = True
        if args.ntfy_topic:
            config['ntfy_topic'] = args.ntfy_topic
            updated = True
        if args.enable_debug:
            config['debug'] = 1
            updated = True
        if args.disable_debug:
            config['debug'] = 0
            updated = True
        
        if updated:
            save_config()
            print("✅ Configuration overrides applied and saved to config file.")
        else:
            print("No configuration overrides to apply.")
        
        # Reload systemd daemon
        print("🔄 Reloading systemd daemon...")
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        
        # Enable the service
        print(f"🔧 Enabling service {service_name}...")
        subprocess.run(['systemctl', 'enable', service_name], check=True)
        
        # Start or restart the service
        if service_running:
            print(f"🔄 Restarting service {service_name} to apply new arguments...")
            subprocess.run(['systemctl', 'restart', service_name], check=True)
        else:
            print(f"▶️ Starting service {service_name}...")
            subprocess.run(['systemctl', 'start', service_name], check=True)
        
        print(f"✅ Service installed successfully!")
        print(f"\n📋 Runtime configuration preserved in service:")
        for arg in exec_args:
            print(f"   • {arg}")
        print(f"\n🔧 Service Management Commands:")
        print(f"   Status:  sudo systemctl status {service_name}")
        print(f"   Stop:    sudo systemctl stop {service_name}")
        print(f"   Restart: sudo systemctl restart {service_name}")
        print(f"   Logs:    sudo journalctl -u {service_name} -f")
        print(f"   Config:  cat {service_path}")
        
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to install service: {e}")
        print("   Make sure you run this with sudo privileges.")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False
        
        
        
        
        
def uninstall_service():
    """Uninstall systemd service (Host mode only)"""
    if os.path.exists('/.dockerenv'):
        print("❌ No service to uninstall in Docker mode.")
        return False
    
    service_name = "presto_x728-ups-monitor.service"
    service_path = f"/etc/systemd/system/{service_name}"
    
    try:
        print("🔧 Uninstalling systemd service...")
        
        # Stop and disable service
        subprocess.run(['systemctl', 'stop', service_name], check=False)
        subprocess.run(['systemctl', 'disable', service_name], check=False)
        
        # Remove service file
        if os.path.exists(service_path):
            os.remove(service_path)
        
        # Reload systemd
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        
        print(f"✅ Service uninstalled successfully!")
        return True
        
    except Exception as e:
        print(f"❌ Failed to uninstall service: {e}")
        return False        

# for RTC FUNCS
def bcd_to_dec(bcd):
    """Convert BCD to Decimal"""
    return (bcd // 16) * 10 + (bcd % 16)

def dec_to_bcd(dec):
    """Convert Decimal to BCD"""
    return (dec // 10) * 16 + (dec % 10)


def read_rtc_time():
    """Reads time from the RTC chip via I2C."""
    try:
        # Initialize bus access (assuming it's not initialized globally elsewhere)
        bus = smbus.SMBus(I2C_BUS)
        
        # Read 7 bytes starting from register 0x00 (Seconds)
        data = bus.read_i2c_block_data(RTC_I2C_ADDR, 0x00, 7)
        
        # Convert BCD to standard time components
        second = bcd_to_dec(data[0] & 0x7F)
        minute = bcd_to_dec(data[1])
        hour = bcd_to_dec(data[2] & 0x3F) # Mask to handle 12/24 hour format if necessary
        day = bcd_to_dec(data[3])
        date = bcd_to_dec(data[4])
        month = bcd_to_dec(data[5] & 0x7F)
        year = bcd_to_dec(data[6]) + 2000
        
        # Create a datetime object
        rtc_dt = datetime(year, month, date, hour, minute, second)
        return rtc_dt, "RTC"
        
    except Exception as e:
        log_message(f"RTC Read Error: {e}", "ERROR", bypass_rtc_check=True)
        return None, "SYSTEM_FALLBACK"

def sync_system_time_to_rtc():
    """Writes the current system time to the RTC chip."""
    global LAST_NETWORK_SYNC_TIME
    log_message("Attempting to write current system time to RTC...", "INFO", bypass_rtc_check=True)
    try:
        now = datetime.now()
        
        # Convert system time components to BCD format
        data = [
            dec_to_bcd(now.second),
            dec_to_bcd(now.minute),
            dec_to_bcd(now.hour),
            dec_to_bcd(now.isoweekday()), # Day of week (1=Monday)
            dec_to_bcd(now.day),
            dec_to_bcd(now.month),
            dec_to_bcd(now.year - 2000)
        ]
        
        bus = smbus.SMBus(I2C_BUS)
        bus.write_i2c_block_data(RTC_I2C_ADDR, 0x00, data)
        
        # Update the global sync time marker
        LAST_NETWORK_SYNC_TIME = time.time()
        # PASS bypass_rtc_check=True HERE
        log_message(f"RTC synchronized to system time: {now.strftime('%Y-%m-%d %H:%M:%S')}", "INFO", bypass_rtc_check=True) 
        return True
        
    except Exception as e:
        # PASS bypass_rtc_check=True HERE
        log_message(f"RTC Write Error: Failed to synchronize RTC: {e}", "ERROR", bypass_rtc_check=True)
        return False


def check_network_connection():
    """Simple check for network connectivity (e.g., DNS resolution)."""
    try:
        # Pings a known reliable public DNS server
        import socket
        socket.setdefaulttimeout(3) 
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("1.1.1.1", 53))
        return True
    except:
        return False

def get_current_time_str(include_source=False):
    """
    Returns the current time string, dynamically selecting between Network/System
    and RTC. Logs a WARNING when the source changes due to network failure.
    """
    global LAST_NETWORK_SYNC_TIME, LAST_TIME_SOURCE, LAST_NETWORK_DROP_TIME 
    
    # We must use datetime.now() inside here to preserve the system time reference
    system_time = datetime.now() 
    current_time = system_time 
    new_source = "SYSTEM/NETWORK" 

    # 1. Check for Network Connection
    network_available = check_network_connection()

    if network_available:
        # Network UP: Use System Time & Sync RTC
        if time.time() - LAST_NETWORK_SYNC_TIME > (RTC_SYNC_INTERVAL_HRS * 3600):
            sync_system_time_to_rtc() 
        
    else:
        # Network DOWN: Fallback to RTC
        rtc_dt, rtc_source = read_rtc_time()
        if rtc_dt:
            current_time = rtc_dt
            new_source = rtc_source # "RTC"
        else:
            # Fallback to unreliable system time if RTC fails
            new_source = "SYSTEM_FALLBACK"

    # --- NEW: STATE CHANGE DETECTION AND WARNING ---
    if new_source != LAST_TIME_SOURCE:
        
        # --- SCENARIO A: NETWORK JUST DROPPED (Switching to RTC) ---
        if new_source == "RTC":
            # 1. Log warning and save drop time
            
            # Use basic log to prevent recursion
            log_message("NETWORK OFFLINE. Saving drop time and switching to RTC.", "WARNING", bypass_rtc_check=True)
            
            # Store the system time when the network was lost
            LAST_NETWORK_DROP_TIME = time.time() 
            
            # Log time discrepancy (already implemented in previous step)
            rtc_time_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
            sys_time_str = system_time.strftime("%Y-%m-%d %H:%M:%S")
            
            warning_msg = (
                f"NETWORK OFFLINE! Switching to {new_source}. "
                f"System time ({sys_time_str}) vs RTC time ({rtc_time_str}). "
                f"Time difference: {abs((system_time - current_time).total_seconds()):.1f}s."
            )
            log_message(warning_msg, "WARNING", bypass_rtc_check=True)
            
        # --- SCENARIO B: NETWORK JUST RECOVERED (Switching from RTC/Fallback) ---
        elif LAST_TIME_SOURCE != "UNKNOWN" and new_source == "SYSTEM/NETWORK":
            # 1. Log the immediate recovery
            log_message(f"NETWORK RESTORED. Switching time source back to {new_source}.", "INFO", bypass_rtc_check=True)
            
            # --- CRITICAL FIX: PROCESS AND CLEAR STATE IMMEDIATELY ---
            drop_time_to_process = LAST_NETWORK_DROP_TIME
            
            # 2. LOCK IN THE NEW SOURCE STATE TO PREVENT REPETITION
            LAST_TIME_SOURCE = new_source
            LAST_NETWORK_DROP_TIME = None # Clear this flag to prevent re-triggering
            #time.sleep(1) # Wait 1 second for network stability
            
            # 3. NTFY Logic runs only if we had a prior drop time
            if drop_time_to_process:
                
                outage_duration = time.time() - drop_time_to_process
                outage_hours = outage_duration / 3600
                
                # Check current system time against the time the RTC was providing
                # We need the last known RTC time to calculate drift, 
                # but we'll use the system time for simplicity since the RTC was providing it moments ago.
                time_drift_s = abs((system_time - current_time).total_seconds())

                ntfy_title = "X728 UPS: Network Restored: Outage Summary"
                ntfy_priority = "default" # Use default priority
                ntfy_message = (
                    f"✅ Network connection re-established after being down for {outage_hours:.2f} hours.\n"
                    f"⏰ Log time source switched back to SYSTEM/NETWORK.\n"
                    f"⏱️ Observed time drift (RTC vs System): **{time_drift_s:.1f} seconds**\n"
                    f"Action: Time synchronized and monitoring resumed."
                )
                
                
                
                send_ntfy(ntfy_message, ntfy_priority, ntfy_title)
                
                # Reset the drop time tracker
                LAST_NETWORK_DROP_TIME = None

    # Update the state for the next check
    LAST_TIME_SOURCE = new_source

    # Final Return (clean string for log_message to put brackets around)
    time_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
    if include_source:
        return f"{time_str} ({new_source})"
    else:
        return time_str
        
        
        
        

# Global MQTT Client instance
mqtt_client = None

def init_mqtt():
    """Initializes the MQTT client with a unique ID and attempts to connect to the broker."""
    global mqtt_client, FIRST_CONNECTION_MADE
    
    # Prevent re-initialization
    if mqtt_client is not None:
        log_message("MQTT client already initialized, skipping.", "DEBUG")
        return
        
    # --- 1. Generate Unique ID ---
    import random
    import string
    
    random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    
    # CRITICAL: Use the base topic AND the random suffix for uniqueness
    unique_client_id = f"{MQTT_BASE_TOPIC}_{random_suffix}"
    
    # If the broker is the default value, skip initialization
    if MQTT_BROKER == '' and not os.environ.get('MQTT_BROKER'):
        log_message("MQTT Broker not explicitly set, skipping initialization.", "INFO")
        return
    
    try:
        log_message(f"Initializing MQTT client ({unique_client_id}) for broker: {MQTT_BROKER}:{MQTT_PORT}", "INFO")
        
        # --- 2. Create the ONE client object using the unique ID ---
        # The paho.mqtt.client alias is typically just 'mqtt' if imported as 'import paho.mqtt.client as mqtt'
        # Assume your original definition was correct here (using the paho.mqtt.client directly)
        mqtt_client = mqtt.Client(
            client_id=unique_client_id, 
            protocol=mqtt.MQTTv311, 
            clean_session=False
        ) 
        
        if MQTT_USER and MQTT_PASSWORD:
            mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
            
        # Define connection handler for logging
        def on_connect(client, userdata, flags, rc):
            global FIRST_CONNECTION_MADE
            if rc == 0:
                if not FIRST_CONNECTION_MADE:
                    log_message("MQTT connection successful (First Time).", "INFO")
                    FIRST_CONNECTION_MADE = True
                else:
                    log_message("MQTT connection re-established.", "DEBUG")
            else:
                log_message(f"MQTT connection failed with code {rc}.", "ERROR")

        mqtt_client.on_connect = on_connect
        
        # Start a background thread and connect
        mqtt_client.loop_start()  
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 180) # Keepalive 180 seconds

    except Exception as e:
        log_message(f"Failed to initialize or connect MQTT: {e}", "ERROR")
        mqtt_client = None

def publish_mqtt_data(topic_suffix, payload, retain=False):
    """Publishes a payload to the full topic."""
    global mqtt_client
    if mqtt_client:
        full_topic = f"{MQTT_BASE_TOPIC}/{topic_suffix}"
        try:
            mqtt_client.publish(full_topic, payload, qos=1, retain=retain)
            log_message(f"Published to {full_topic}: {payload}", "DEBUG")
        except Exception as e:
            log_message(f"Error publishing MQTT data: {e}", "WARNING")
            

def get_network_info():
    """
    Get network connection info: (display_text, status). 
    Status is 'connected' or 'disconnected' based on public internet.
    Works robustly on host (DietPi/Ubuntu) and inside a container.
    """
    
    display_text = "Unknown"
    status = 'disconnected'
    
    
    # Determine Execution Environment
    
    is_running_in_container = os.path.exists("/.dockerenv")

    
    # Determine Network Type (Device-dependent)
    
    if is_running_in_container:
        # Inside Container: Cannot reliably detect Wi-Fi/Ethernet. 
        # Assume generic 'Container Network' for display.
        display_text = "Container Network"
    else:
        # On Host (DietPi, Ubuntu, RetroPie): Attempt physical device detection
        try:
            # Attempt to get WiFi SSID using iwgetid -r
            # iwgetid should be available on most modern Linux hosts with Wi-Fi
            ssid = subprocess.check_output(['iwgetid', '-r']).decode('utf-8').strip()
            if ssid:
                display_text = f"🛜WiFi: {ssid}"
            else:
                # If iwgetid runs but returns no SSID (often Ethernet)
                display_text = "Ethernet"
        except (subprocess.CalledProcessError, FileNotFoundError):
            # iwgetid not installed or fails (e.g., on a headless Ethernet-only machine)
            display_text = "Ethernet"
        except Exception as e:
            # log_message(f"Network info error: {e}", "WARNING")
            display_text = "Unknown Host"

    
    # Liveness Check (Public Internet) - run on both host and container
    
    try:
        # no ping no curl in image no problem! Attempt a pure Python socket connection to a reliable public IP on a common port.
        host = '1.1.1.1' # Cloudflare DNS
        port = 53
        timeout = 2

        s = socket.create_connection((host, port), timeout)
        s.close()
        
        status = 'connected'
    
    except socket.error:
        # Catch specific socket errors (timeout, refusal, OSError)
        status = 'disconnected'
    
    except Exception as e:
        # Catch any other unexpected errors during the check
        # log_message(f"Internet check error: {e}", "WARNING")
        status = 'disconnected'

    return display_text, status
        



# Define the constants for the user/group defined in the Dockerfile       

APPUSER_UID = 1000  # appuser UID
GPIO_GID = 993      # gpio GID
FILE_MODE = 0o664   # rw-rw-r-- (Read/Write for owner/group, Read-only for others)

def _init_file(path, default_content, mode, uid, gid, log_message, is_log_file=False):
    """Helper function to initialize a single file, set its permissions, and ownership."""
    
    # 1. Create file with default content if it doesn't exist
    if not os.path.exists(path):
        with open(path, 'w') as f:
            if is_log_file:
                # Log files get a simple initial message
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Log file created.\n")
            else:
                # Config/History files get JSON content
                json.dump(default_content, f, indent=4)
        print(f"[INFO] Initialized default {log_message} file.")

    # 2. Set permissions (mode) and ownership (chown)
    # The file is always chown'd and chmod'd to ensure correct permissions 
    # and owner mapping (especially when run as root/UID 0 in Docker)
    try:
        os.chown(path, uid, gid)
        os.chmod(path, mode)
        # Optional: Print final ownership/permissions for debugging
        # stat = os.stat(path)
        # print(f"[DEBUG] {log_message} owner: {stat.st_uid}:{stat.st_gid}, mode: {oct(stat.st_mode)[-4:]}")
    except Exception as e:
        # NOTE: This can fail if running as an unprivileged user (not root)
        print(f"[WARNING] Failed to set owner/permissions for {path}: {e}")

def initialize_files():
    """Create config, history, and log files with defaults, set correct permissions (0o664), and ownership."""
    
    print("-" * 50)
    print("STAGE 1: FILE AND DIRECTORY INITIALIZATION")
    
    # 1. Create necessary directories
    try:
        # Create directory for CONFIG_PATH/HISTORY_PATH
        config_dir = os.path.dirname(CONFIG_PATH)
        os.makedirs(config_dir, exist_ok=True)
        # Create directory for LOG_PATH (if different, otherwise safe)
        os.makedirs(os.path.dirname(LOG_PATH) if os.path.dirname(LOG_PATH) else '.', exist_ok=True)
        print(f"[INFO] Data directory created: {config_dir}")
        
    except Exception as e:
        print(f"[CRITICAL] Failed to create directories: {e}")
        print("Please check volume mounts.")
        return # Stop execution if directories fail
        
    print(f"[INFO] CONFIG_PATH: {CONFIG_PATH}")
    print(f"[INFO] HISTORY_PATH: {HISTORY_PATH}")
    print(f"[INFO] LOG_PATH: {LOG_PATH}")

    # 2. Initialize files using the helper function
    try:
        _init_file(CONFIG_PATH, DEFAULT_CONFIG, FILE_MODE, APPUSER_UID, GPIO_GID, "configuration")
        _init_file(HISTORY_PATH, [], FILE_MODE, APPUSER_UID, GPIO_GID, "history")
        _init_file(LOG_PATH, None, FILE_MODE, APPUSER_UID, GPIO_GID, "debug log", is_log_file=True)
        
    except Exception as e:
        print(f"[CRITICAL] Failed to initialize data files: {e}")
        
    print("-" * 50)




def log_message(message, level="INFO", bypass_rtc_check=False, show_time_source=False):
    """Enhanced logging with levels, respecting LOG_LEVEL for file writing."""
    global config, LOG_LEVEL
    
    if bypass_rtc_check:
        # If true, use simple, non-recursive system time
        timestamp_content = datetime.now().strftime("%Y-%m-%d %H:%M:%S (SYSTEM_BASIC)")
    else:
        # Otherwise, use the full dynamic time function
        timestamp_content = get_current_time_str(include_source=show_time_source)
    
    
    level_upper = level.upper()
    message_level_value = LOG_LEVEL_MAP.get(level_upper, 0)
    # NOTE: LOG_LEVEL is now a global controlled by load_config() and the UI toggle
    configured_level_value = LOG_LEVEL_MAP.get(LOG_LEVEL, 2) 

    # Check if the message's level is verbose enough to be printed
    # (The custom map logic works as intended: DEBUG=1, INFO=2, WARN=3 -> 2 >= 2 prints INFO)
    should_print = (message_level_value >= configured_level_value)
    
    # 1. Console / Docker Output Check
    if should_print:
        print(f"[{timestamp_content}] [{level.upper():<5}] {message}")

       
    # FIX: ONLY write to file if the message was verbose enough to print 
    # AND the debug file-write flag (config['debug']) is enabled
    if should_print and config.get('debug', 0) == 1:
        
        log_entry = f"[{timestamp_content}] [{level_upper}] {message}"
        
        with lock:
            try:
                # The file is expected to exist from initialize_files()
                with open(LOG_PATH, 'a') as f:
                    f.write(log_entry + '\n')
            except Exception as e:
                print(f"ERROR: Failed to write to log: {e}")                

def load_config():
    """Load configuration from JSON file"""
    global config, LOG_LEVEL, LAST_CONFIG_MTIME

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
        config['debug'] = int(config.get('debug', 1))
        
        # --- TIE UI DEBUG TOGGLE TO CONSOLE VERBOSITY (LOG_LEVEL) --
        if config['debug'] == 1:
            # If UI Debug is ON, force maximum console verbosity
            LOG_LEVEL = 'DEBUG'
        else:
            # If UI Debug is OFF, respect the environment variable, but cap it at INFO
            env_log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
            # If environment is set to DEBUG (1), but config['debug'] is 0, cap it at INFO (2)
            if LOG_LEVEL_MAP.get(env_log_level, 2) < LOG_LEVEL_MAP.get('INFO', 2):
                 LOG_LEVEL = 'INFO'
            else:
                 LOG_LEVEL = env_log_level
                 
        # Only update the timestamp if the file was successfully read
        if os.path.exists(CONFIG_PATH):
            LAST_CONFIG_MTIME = os.path.getmtime(CONFIG_PATH)         
        
        log_message(f"Configuration loaded. Console LOG_LEVEL set to: {LOG_LEVEL}", "INFO")
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

# ... (functions like send_ntfy, load_config, VERSION check etc.)



def _parse_version(version_tag): #GITHUB REPO UPDATE CHECKING
    """Parses a version tag (e.g., 'v3.0.10') into a comparable tuple (3, 0, 10)."""
    # Remove 'v' or 'V' prefix if present
    version_str = version_tag.lstrip('v').lstrip('V')
    try:
        # Split by '.' and convert to integers for robust comparison
        return tuple(map(int, version_str.split('.')))
    except ValueError:
        log_message(f"Could not parse version tag: {version_tag}", "ERROR")
        return (0, 0, 0) # Fallback

def check_latest_version(manual=False):
    """
    Checks the GitHub API for the latest release version.
    Updates the global LATEST_VERSION_INFO, flashes messages, and sends an ntfy notification.
    """
    global LATEST_VERSION_INFO, CURRENT_VERSION, GITHUB_REPO, VERSION_CHECK_INTERVAL

    now = time.time()
    
    if not manual:
        if now - LATEST_VERSION_INFO["last_check"] < VERSION_CHECK_INTERVAL:
            return  # Skip the check entirely if the time hasn't passed
    
    #  Rate Limit Check 
    if manual and (now - LATEST_VERSION_INFO["last_check"] < 60):
        log_message("Manual version check rate limit: skipping, last check less than 60s ago.", "WARNING")
        showFlashMessage( "info", "Version check rate limited. Please wait 60 seconds between manual checks.")
        return
    
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    log_message(f"Checking latest release from GitHub API: {api_url}", "INFO")

    try:
        response = requests.get(api_url, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        latest_tag = data.get('tag_name')
        
        if latest_tag:
            current_ver_tuple = _parse_version(CURRENT_VERSION)
            latest_ver_tuple = _parse_version(latest_tag)
            
            # Update info
            LATEST_VERSION_INFO["latest"] = latest_tag
            LATEST_VERSION_INFO["last_check"] = now
            
            if latest_ver_tuple > current_ver_tuple:
                # --- NEW: Check if this is the first time detecting the update ---
                # This prevents spamming notifications on every hourly check
                if LATEST_VERSION_INFO["update_available"] is False:
                    ntfy_message = (
                        f"🚀 Update Available! "
                        f"{VERSION_STRING} {current_ver_tuple} can be updated to {latest_tag}. "
                        f"Check GitHub for details."
                    )
                    # Assuming your existing send_ntfy function accepts title/priority
                    send_ntfy(
                        message=ntfy_message, 
                        priority="high", 
                        title="X728 UPS Monitor Update"
                    )
                # -----------------------------------------------------------------
                
                LATEST_VERSION_INFO["update_available"] = True
                log_message(f"New version available! Current: {CURRENT_VERSION}, Latest: {latest_tag}", "WARNING")
            else:
                LATEST_VERSION_INFO["update_available"] = False
                if manual:
                    flash(f"You are running the latest version: {CURRENT_VERSION}", "success")
        else:
            raise ValueError("No 'tag_name' found in GitHub response.")

    except requests.exceptions.RequestException as e:
        log_message(f"GitHub API request failed: {e}", "ERROR")
        if manual: flash(f"Version check failed: Cannot reach GitHub API ({e}).", "error")
    except Exception as e:
        log_message(f"Version check failed: {e}", "ERROR")
        if manual: flash(f"Version check failed: An unexpected error occurred ({e}).", "error")



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
    load_ma = 1300  # Use configured load current
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
        
    try:
        network_text, network_status = get_network_info()
    except Exception as e:
        log_message(f"Network info error: {e}", "WARNING")
        network_text = "Unknown"
        network_status = "disconnected"
        
    return {
        "cpu_temp": f"{temp:.1f}",
        'network': network_text,
        'network_status': network_status,
        "disk_usage": f"{disk_used:.1f}",
        "disk_free": f"{disk_free_gb:.1f}",
        "disk_label": disk_label,
        "memory_info": memory_info,  # New: "free / total" in GB
        "uptime": uptime_str,
        "network": get_network_info()
    }


def get_pi_model():
    """Detect Raspberry Pi model"""
    try:
        with open('/proc/device-tree/model', 'r') as f:
            model = f.read().strip().replace('\x00', '')
            return model
    except Exception:
        return "Unknown Pi Model"
        
 




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
  
  
    # Prevent multiple instances
    if monitor_thread_running:
        log_message("Monitor thread already running, exiting duplicate.", "WARNING")
        return
        
        
    log_message("Monitor thread started")
    monitor_thread_running = True
    
    # ADDED: Initialize interval outside try block
    interval = config.get('monitor_interval', 10) 

    while not monitor_thread_stop_event.is_set():
        try:
        
            # --- DYNAMIC CONFIG RELOAD CHECK ---
            # 1. Check if the config file exists
            if os.path.exists(CONFIG_PATH):
                current_mtime = os.path.getmtime(CONFIG_PATH)
                
                # 2. Compare it to the last time we loaded it
                if current_mtime > LAST_CONFIG_MTIME:
                    log_message("Configuration file modified on disk. Reloading settings dynamically...", "INFO")
                    # load_config() updates: config, LOG_LEVEL, and LAST_CONFIG_MTIME
                    load_config()  
            # ---------------------------------------------
        
            check_thresholds()
            
            # --- 3.0.11 git Version Check ---
            check_latest_version()
            
            # 1. DATA COLLECTION (Runs every loop)
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
                "i2c_addr": f"0x{current_i2c_addr:02x}" if current_i2c_addr else "N/A",
                "latest_version_info": LATEST_VERSION_INFO
            }
            
            # 2. UI EMIT (Runs every loop)
            socketio.emit('status_update', status)
            
            # 3. MQTT PUBLISH (Runs every loop, now matching UI refresh rate)
            # ---  MQTT Data Publishing ---
            publish_mqtt_data("battery_level", f"{battery_level:.1f}")
            publish_mqtt_data("voltage", f"{voltage:.2f}")
            publish_mqtt_data("power_state", power_state)
            
            full_payload = json.dumps(status)
            publish_mqtt_data("state", full_payload)
            # -----------------------------------
            
            # 4. SET NEXT INTERVAL
            if power_state == "On Battery":
                interval = 2 # Faster (2s) for real-time critical monitoring
            else:
                interval = config.get('monitor_interval', 10) # Normal configurable interval
        

        except Exception as e:
            log_message(f"Monitor error: {e}", "ERROR")
            interval = config.get('monitor_interval', 10) # Fallback to normal on error
            
        # 5. THREAD SLEEP (Waits for the dynamically set 'interval')
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
    log_message(f"Sending startup ntfy in PID: {os.getpid()}", "DEBUG")
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
        f"🚀 Presto x728 UPS Monitor (v{CURRENT_VERSION}) \n"
        f"☝ Mode:{VERSION_BUILD} \n"
        f"🔌 Initial Power State: {power_state}\n"
        f"🔋 Estimated Time Remaining: {time_remaining}\n"
        f"Battery: {battery_level:.1f}%\n"
        f"Voltage: {voltage:.2f}V\n"
        f"CPU/GPU Temp: {cpu_temp}°C\n"
        f"Disk ({disk_label}): {disk_free} GB free\n"
        f"Thresholds:\n"
        f" 🪫 Low Battery: {config['low_battery_threshold']}%\n"
        f" ⚰️ Critical Battery: {config['critical_low_threshold']}%\n"
        f" 🔥 CPU Temp: {config['cpu_temp_threshold']}°C\n"
        f" 💽 Disk Space: {config['disk_space_threshold']} GB\n"
        f" ⚡ Critical Voltage: 3.0V"
        
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
    # Explicitly unpack network info to ensure system_info['network'] is a string
    network_text, network_status = get_network_info()
    system_info['network'] = network_text
    system_info['network_status'] = network_status
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
        CURRENT_VERSION=CURRENT_VERSION,
        GITHUB_REPO=GITHUB_REPO,
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



@app.route('/version/check', methods=['POST', 'GET'])
def check_version_manual():
    """Trigger a manual version check, flash the result, and return success JSON."""
    global CURRENT_VERSION, LATEST_VERSION_INFO

    # The check_latest_version function already handles the flash() call
    # based on the result (success, failure, or update available).
    check_latest_version(manual=True)
    
    # Flash a warning message if an update is available (from the manual check)
    if LATEST_VERSION_INFO["update_available"]:
        flash(f"🚀 New version available! Current: {CURRENT_VERSION}, Latest: {LATEST_VERSION_INFO['latest']} - Please update.", "warning")
    
    # --- CHANGE: Return a simple JSON response instead of a redirect ---
    # This keeps the client on the same page but registers the server-side flash message.
    return jsonify({"status": "success", "message": "Version check complete."})









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

        .metric-card {
            position: relative;
            overflow: hidden;
            background: #FFFFFF; /* Assumed white background for light mode */
        }

        html.dark .metric-card {
            background: #1E293B; /* slate-800 for dark mode */
        }

        /* Robust Light and Dark Mode Text Overrides */
        .glass-card h3,
        .glass-card label,
        .glass-card input,
        .glass-card textarea,
        .metric-card h2,
        .metric-card h3,
        .metric-card p,
        .metric-card span {
            color: #111827; /* gray-900 for headers, small texts, and inputs in light mode */
        }

        html.dark .glass-card h3,
        html.dark .glass-card label,
        html.dark .glass-card input,
        html.dark .glass-card textarea,
        html.dark .metric-card h2,
        html.dark .metric-card h3, 
        html.dark .metric-card p,
        html.dark .metric-card span {
            color: #9ca3af; /* #F3F4F6 gray-100 for headers, small texts, and inputs in dark mode */
        }

        /* Ensure input fields have consistent backgrounds */
        .glass-card input,
        .glass-card textarea {
            background-color: #FFFFFF; /* White background for inputs in light mode */
            border-color: #D1D5DB; /* gray-300 border */
        }

        html.dark .glass-card input,
        html.dark .glass-card textarea {
            background-color: #1E293B; /* slate-800 background for inputs in dark mode */
            border-color: #475569; /* slate-600 border */
        }

        /* Override Tailwind dark mode classes to prevent conflicts */
        html:not(.dark) .glass-card .dark\:bg-gray-700,
        html:not(.dark) .glass-card .dark\:bg-gray-800,
        html:not(.dark) .glass-card .dark\:bg-gray-900,
        html:not(.dark) .metric-card .dark\:bg-gray-700,
        html:not(.dark) .metric-card .dark\:bg-gray-800,
        html:not(.dark) .metric-card .dark\:bg-gray-900 {
            background-color: #FFFFFF !important; /* White background for inputs and metric cards in light mode */
        }

        html:not(.dark) .glass-card .dark\:text-gray-100,
        html:not(.dark) .glass-card .dark\:text-gray-200,
        html:not(.dark) .glass-card .dark\:text-white,
        html:not(.dark) .metric-card .dark\:text-gray-100,
        html:not(.dark) .metric-card .dark\:text-gray-200,
        html:not(.dark) .metric-card .dark\:text-white {
            color: #111827 !important; /* gray-900 for text in light mode */
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
        
        @keyframes flash-bulb {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.1; } /* Slightly less aggressive than 0 to make it pulse */
        }

        .flashing {
            animation: flash-bulb 1s infinite;
            
        }   
         /* NEW: Spinning Icon CSS for Manual Check Feedback */
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        .spinning {
            animation: spin 1s linear infinite;
        }  
        
        /* Heartbeat Animation */
        @keyframes heartbeat {
            0% { transform: scale(1); opacity: 1; }
            30% { transform: scale(1.15); opacity: 0.8; }
            50% { transform: scale(1); opacity: 1; }
            100% { transform: scale(1); opacity: 1; }
        }

        .heartbeat {
            display: inline-block; /* Required for the transform scale to work */
            animation: heartbeat 2s ease-in-out infinite; /* 2 seconds duration, smooth transition, loops forever */
        }   
        
        /* Bright Swipe/Glare Animation for Text */
        @keyframes swipe-glare {
            0% { background-position: 200% 0; }
            100% { background-position: -200% 0; }
        }

        .swipe-text {
            /* Define the moving gradient and size */
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.8), transparent);
            background-size: 200% 100%;

            /* Clip the background to the shape of the text */
            -webkit-background-clip: text; 
            background-clip: text;

            /* CRITICAL FIX: Make the text visually transparent, revealing the background, 
               but preserve the text color (set by Tailwind) for context. */
            -webkit-text-fill-color: transparent; 
            
            /* Ensure the element behaves correctly */
            display: inline-block; 

            /* Animation application */
            animation: swipe-glare 12s linear infinite;
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
                        <a href="https://github.com/piklz/pi_ups_monitors/tree/main/docker"><img width="60%" src="data:image/png;base64,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"alt="PRESTO x718 Embedded Image"/> </a>
                        <p class="text-sm mt-1">
                             
                            <span class="swipe-text text-sm font-semibold text-gray-800 dark:text-gray-300">{{ pi_model }}</span>
                        </p>                       
                    </div>
                    <div class="flex flex-col items-end">
                        <button onclick="toggleDarkMode()" class="p-3 rounded-full hover:bg-gray-200 dark:hover:bg-gray-700 mb-2">
                            <svg id="darkModeIcon" class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"></path>
                        </button>
                        <div class="text-right">
                            <p class="text-xs text-gray-900 dark:text-gray-400">Version {{ CURRENT_VERSION}}</p>
                            <p class="text-sm font-semibold text-gray-800 dark:text-gray-300">{{ VERSION_STRING}}  </p>
                            <p class="text-xs text-gray-800 dark:text-gray-400">{{ VERSION_BUILD }}</p>
                        <div class="info-section">
                            <p>{{ VERSION_NUMBER }} <span id="version-status" class="status-indicator"></span></p>                    
                            
                        </div>                            
                        </div>
                        
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
                            <p class="text-sm slate-900 dark:text-gray-400 mb-1">Power State</p>
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
                        <p class="text-gray-600 dark:text-gray-400">🌡️ CPU: <span id="cpu-temp" class="font-semibold">{{ system_info.cpu_temp }}°C </span>
                        <p class="text-gray-600 dark:text-gray-400">🌐︎ <span id="network">{{ system_info.network }} {% if system_info.network_status == 'connected' %}🟢{% else %}🔴{% endif %}</span></p>
                        <p class="text-gray-600 dark:text-gray-400">💾 Disk (<span id="disk-label">{{ system_info.disk_label }}</span>): <span id="disk-usage" class="font-semibold">{{ system_info.disk_usage }}%</span> used (<span id="disk-free" class="font-semibold">{{ system_info.disk_free }}</span> GB free)</p>
                        <p class="text-gray-600 dark:text-gray-400">🧠 RAM: <span id="memory-info" class="font-semibold">{{ system_info.memory_info }}</span></p>
                    </div>
                </div>
            </div>
            
            <!-- Charts and Detailed Info -->
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
                
                <!-- Battery History Chart -->
                <div class="glass-card rounded-2xl p-6">
                    <h3 class="text-xl font-bold mb-4 flex items-center gap-2 text-gray-100 dark:text-gray-400">
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
                    <h3 class="text-xl font-bold mb-4 flex items-center gap-2 text-gray-900 dark:text-gray-400">
                        <svg class="w-6 h-6 text-purple-500" fill="currentColor" viewBox="0 0 20 20">
                            <path fill-rule="evenodd" d="M3 3a1 1 0 000 2v8a2 2 0 002 2h2.586l-1.293 1.293a1 1 0 101.414 1.414L10 15.414l2.293 2.293a1 1 0 001.414-1.414L12.414 15H15a2 2 0 002-2V5a1 1 0 100-2H3zm11.707 4.707a1 1 0 00-1.414-1.414L10 9.586 8.707 8.293a1 1 0 00-1.414 0l-2 2a1 1 0 101.414 1.414L8 10.414l1.293 1.293a1 1 0 001.414 0l4-4z" clip-rule="evenodd"></path>
                        </svg>
                        Hardware Status
                    </h3>
                    <div class="space-y-4">
                        <div class="flex justify-between items-center p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                            <span class="font-semibold text-gray-900 dark:text-gray-400">I2C Bus</span>
                            <span id="i2c-status" class="{% if not hardware_error %}text-green-500{% else %}text-red-500{% endif %}">
                                {% if not hardware_error %}✅ {{ i2c_addr }}{% else %}❌ Error{% endif %}
                            |</span>
                        </div>
                        <div class="flex justify-between items-center p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                            <span class="font-semibold text-gray-900 dark:text-gray-400">GPIO Interface</span>
                            <span id="gpio-status" class="{% if not gpio_error %}text-green-500{% else %}text-yellow-500{% endif %}">
                                {% if not gpio_error %}✅ Active{% else %}⚠️ Limited{% endif %}
                            </span>
                        </div>
                        <div class="flex justify-between items-center p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                            <span class="font-semibold text-gray-900 dark:text-gray-400 ">Uptime</span>
                            <span id="uptime" class="text-blue-500 font-mono">{{ system_info.uptime }}</span>
                        </div>
                        <div class="flex justify-between items-center p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                            <span class="font-semibold text-gray-900 dark:text-gray-400">Last Update</span>
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
                        <h3 class="text-xl font-bold flex items-center gap-2 text-gray-900 dark:text-gray-400">
                            <svg class="w-6 h-6 text-indigo-500" fill="currentColor" viewBox="0 0 20 20">
                                <path fill-rule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clip-rule="evenodd"></path>
                            </svg>
                            Configuration
                        </h3>
                        <svg id="config-arrow" class="w-6 h-6 transform transition-transform text-gray-900 dark:text-gray-200" fill="currentColor" viewBox="0 0 20 20">
                            <path fill-rule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clip-rule="evenodd"></path>
                        </svg>
                    </button>
                    
                    <div id="config-content" class="collapsible-content mt-6">
                        <form id="config-form" method="POST" action="/configure" class="grid grid-cols-1 md:grid-cols-2 gap-6">
                            
                            <!-- Battery Thresholds -->
                            <div class="space-y-4">
                                <h4 class="font-semibold text-lg border-b pb-2 text-gray-900 dark:text-gray-200">Battery Thresholds</h4>
                                
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
                                <h4 class="font-semibold text-lg border-b pb-2 text-gray-900 dark:text-gray-200">System Thresholds</h4>
                                
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
                                <h4 class="font-semibold text-lg border-b pb-2 text-gray-900 dark:text-gray-200">Monitoring Settings</h4>
                                
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
                                    <label class="relative inline-flex items-center cursor-pointer">
                                        <input type="checkbox" name="enable_auto_shutdown" value="1" 
                                            {{ 'checked' if config.enable_auto_shutdown else '' }} 
                                            class="sr-only peer">
                                        <div class="w-14 h-7 bg-gray-200 peer-checked:bg-gradient-to-r peer-checked:from-red-400 peer-checked:to-red-600 rounded-full peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-red-300 dark:peer-focus:ring-red-800 transition-all duration-300"></div>
                                        <span class="absolute w-5 h-5 bg-white rounded-full top-1 left-1 peer-checked:translate-x-7 transition-transform duration-300"></span>
                                    </label>
                                    <label class="font-medium">Enable Auto-Shutdown</label>
                                </div>
                                <div class="flex items-center gap-3">
                                    <label class="relative inline-flex items-center cursor-pointer">
                                        <input type="checkbox" name="debug" value="1" 
                                            {{ 'checked' if config.debug else '' }} 
                                            class="sr-only peer">
                                        <div class="w-14 h-7 bg-gray-200 peer-checked:bg-gradient-to-r peer-checked:from-blue-400 peer-checked:to-blue-600 rounded-full peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 transition-all duration-300"></div>
                                        <span class="absolute w-5 h-5 bg-white rounded-full top-1 left-1 peer-checked:translate-x-7 transition-transform duration-300"></span>
                                    </label>
                                    <label class="font-medium">Enable Debug Logging</label>
                                </div>
                            </div>
                            
                            <!-- Notification Settings -->
                            <div class="space-y-4">
                                <h4 class="font-semibold text-lg border-b pb-2 text-gray-900 dark:text-gray-200">Notifications (ntfy)</h4>
                                
                                <div class="flex items-center gap-3">
                                    <label class="relative inline-flex items-center cursor-pointer">
                                        <input type="checkbox" name="enable_ntfy" value="1" 
                                            {{ 'checked' if config.enable_ntfy else '' }} 
                                            class="sr-only peer">
                                        <div class="w-14 h-7 bg-gray-200 peer-checked:bg-gradient-to-r peer-checked:from-green-400 peer-checked:to-green-600 rounded-full peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-green-300 dark:peer-focus:ring-green-800 transition-all duration-300"></div>
                                        <span class="absolute w-5 h-5 bg-white rounded-full top-1 left-1 peer-checked:translate-x-7 transition-transform duration-300"></span>
                                    </label>
                                    <label class="font-medium">Enable ntfy Notifications</label>
                                </div>
                                
                                <div>
                                    <label class="block text-sm font-medium mb-2">ntfy Server</label>
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
                                <strong>⚠️ Warning:</strong> Shutdown will occur after a {{ config.shutdown_delay }}-second delay.
                            </p>
                        </div>
                    <div class="info-section">
                        <div class="mt-4 p-4 bg-purple-50 dark:bg-purple-900 border-l-4 border-purple-500 rounded">
                            
                            <div class="flex items-center justify-between">
                                <p class="text-sm text-purple-800 dark:text-purple-200">
                                    <span class="font-semibold">CURRENT VERSION:</span> {{ VERSION_STRING }} {{ CURRENT_VERSION }}
                                    <span id="version-status" class="status-indicator"></span>
                                </p>
                                
                                <form method="POST" action="{{ url_for('check_version_manual') }}" id="version-check-form">
                                    
                                    <button id="manual-version-check" type="submit" title="Manually Check for Update" 
                                            class="p-2 bg-purple-600 hover:bg-purple-700 text-white font-bold rounded-lg transition duration-200 flex items-center justify-center gap-2 text-sm">
                                        
                                        <span>
                                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path>
                                            </svg>
                                        </span>
                                        Check
                                    </button>
                                </form>
                            </div>
                        </div>
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
            document.getElementById('network').innerHTML = `${data.system_info.network} ${data.system_info.network_status === 'connected' ? '🟢' : '🔴'}`;
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
            
            // --- Version Check Status Update with Flashing Emojis ---
            var versionStatusElement = document.getElementById('version-status');
            var versionInfo = data.latest_version_info;

            if (versionInfo && versionStatusElement) {
                if (versionInfo.update_available) {
                    // Apply flashing class to the lightbulb or star emoji
                    versionStatusElement.innerHTML = `
                        <span class="flashing">✨</span>
                        <span style="color: #9ca3af;">New:</span>
                        <a href="https://github.com/{{ GITHUB_REPO }}/releases/latest" 
                           target="_blank" 
                           style="color: yellow; text-decoration: none;">
                            ${versionInfo.latest}
                        </a>
                    `;
                    versionStatusElement.className = 'status-indicator text-warning';
                } else {
                    // Green dot or robot emoji for up-to-date
                    versionStatusElement.innerHTML = `🤖`;
                    // Ensure the flashing class is removed
                    versionStatusElement.className = 'status-indicator text-success';
                }
            }            
        });
        
        //This is for check-update button
        document.addEventListener('DOMContentLoaded', function() {
            const checkButton = document.getElementById('manual-version-check');
            const checkIcon = checkButton ? checkButton.querySelector('span') : null;
            const checkForm = document.getElementById('version-check-form');
            
            if (checkButton && checkForm && checkIcon) {
                checkButton.addEventListener('click', function(event) {
                    event.preventDefault(); 

                    // 1. Client-side message starts immediately (default 5 seconds)
                    showFlashMessage('info','Checking updates on Git hub now'); 
                    
                    // 2. Visual feedback: Spinning gear and disable button
                    checkIcon.innerHTML = '⚙️'; 
                    checkIcon.classList.add('spinning');
                    checkButton.style.pointerEvents = 'none'; 
                    
                    // 3. FIX: Use Fetch/AJAX to perform the check without redirecting
                    fetch('{{ url_for("check_version_manual") }}', {
                        method: 'POST'
                    })
                    .then(response => {
                        // Check success is handled by the server (Python route returns 200)
                    })
                    .catch(error => {
                        // Handle network errors client-side
                        showFlashMessage('error','Version check failed due to a network error.');
                    })
                    .finally(() => {
                        // 4. FIX: After the check is complete, reload the page after 5 seconds.
                        // This ensures the client-side flash message timer runs out (5s), 
                        // and then the page reloads to show the server-side flash message 
                        // stored by the Python route.
                        
                        setTimeout(() => {
                            // Remove client-side flash message
                            closeFlash();
                            // Load the dashboard to display the server's final flash message
                            window.location.href = '{{ url_for("dashboard") }}'; 
                        }, 5000); // Wait for the flash timer (5 seconds)
                    });
                });
            }
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
    
    
    
    document.addEventListener('DOMContentLoaded', function() {
        const container = document.getElementById('mouse-glare-test'); 

        if (container) {
            const overlay = container.querySelector('.mouse-glare-overlay');

            container.addEventListener('mousemove', (e) => {
                const rect = container.getBoundingClientRect();
                // Calculate mouse position relative to the container (0, 0 is top-left)
                const x = e.clientX - rect.left; 
                const y = e.clientY - rect.top;

                // Dynamically update the radial gradient center point (the 'light source')
                overlay.style.background = `radial-gradient(
                    circle at ${x}px ${y}px,
                    rgba(255, 255, 255, 0.15), /* Brighter light at the center */
                    transparent 50%
                )`;
            });
            
            // When the mouse leaves the container, the CSS ':hover' transition handles the fade-out.
        }
    });    
    </script>
    
    <footer class="mt-12 mb-4">
        <div align="center" style="padding: 20px; font-size: 1.1em; color: #222944;">
            <strong>Made with <span class="heartbeat">❤️</span> for the Raspberry Pi Community</strong>
            
            <div style="display: flex; justify-content: center; gap: 10px; margin-top: 10px;">
                <img alt="Raspberry Pi" src="https://img.shields.io/badge/Raspberry%20Pi-C51A4A?style=for-the-badge&logo=raspberry-pi&logoColor=white" />
                <img alt="Python" src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" />
                <img alt="Docker" src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white" />
            </div>
            
        </div>
    </footer>
</body>
</html>
'''


# Module-level initialization with lock for Gunicorn multi-worker safety

init_lock = threading.Lock()
_SERVICES_INITIALIZED = False

def initialize_core_services():
    """Initialize hardware and monitoring services exactly once."""
    global _SERVICES_INITIALIZED, SETUP_COMPLETE
    with init_lock:  # Ensure single execution across Gunicorn workers
        if _SERVICES_INITIALIZED:
            log_message("Core services already initialized, skipping.", "DEBUG")
            return
        log_message("Initializing core services...", "INFO")
        try:
            # STAGE 1: System Foundation
            initialize_files()
            load_config()
            load_battery_history()
            configure_kernel_overlay()
            # STAGE 2: Core Resource Acquisition
            init_hardware()
            time_status = get_current_time_str(include_source=True)
            log_message(f"Time source initialized. Current log time is derived from: {time_status}", "INFO")
            init_mqtt()
            # STAGE 3: Application Start
            start_monitor()
            send_startup_ntfy()
            _SERVICES_INITIALIZED = True
            SETUP_COMPLETE = True
            log_message("All core services initialized successfully.", "INFO")
        except Exception as e:
            log_message(f"Failed to initialize core services: {e}", "CRITICAL")
            raise




# Module-level initialization for Gunicorn/import cases
if __name__ != '__main__':
    initialize_core_services()
    
    
    
# Main block for direct execution (args and server only)
if __name__ == '__main__':
    
    # Argument parsing and overrides
    
    # --- Argument Parsing must run here  ---
    parser = argparse.ArgumentParser(
        description="X728 UPS Monitor - Docker & Host Compatible",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
    Examples:
      # Direct execution (Host):
      python3 presto_x728_sysmon.py
      
      # Install as systemd service (Host):
      sudo python3 presto_x728_sysmon.py --install-service
      
      # Run with custom thresholds:
      python3 presto_x728_sysmon.py --low-battery 20 --critical-battery 5
      
      # Docker mode (use docker-compose or environment variables):
      docker-compose up -d
        """
    )

    # Service Management (Host-only)
    service_group = parser.add_argument_group('Service Management (Host Only)')
    service_group.add_argument('--install-service', action='store_true',
                              help='Install as systemd service (requires sudo)')
    service_group.add_argument('--uninstall-service', action='store_true',
                              help='Uninstall systemd service (requires sudo)')

    # Hardware Configuration
    hw_group = parser.add_argument_group('Hardware Configuration')
    hw_group.add_argument('--hw-version', 
                         type=int, 
                         default=0,
                         choices=[1, 2],
                         metavar='VERSION',
                         help='X728 hardware version (1=V1.x/GPIO13, 2=V2.x+/GPIO26)')

    # Battery Thresholds
    battery_group = parser.add_argument_group('Battery Thresholds')
    battery_group.add_argument('--low-battery', 
                              type=float,
                              metavar='PERCENT',
                              help='Low battery warning threshold (default: 10.0%%)')
    battery_group.add_argument('--critical-battery', 
                              type=float,
                              metavar='PERCENT',
                              help='Critical battery shutdown threshold (default: 2.0%%)')

    # System Thresholds
    system_group = parser.add_argument_group('System Thresholds')
    system_group.add_argument('--cpu-temp', 
                             type=float,
                             metavar='CELSIUS',
                             help='CPU temperature warning threshold (default: 65.0°C)')
    system_group.add_argument('--disk-space', 
                             type=float,
                             metavar='GB',
                             help='Disk space warning threshold in GB (default: 10.0)')

    # Monitoring Settings
    monitor_group = parser.add_argument_group('Monitoring Settings')
    monitor_group.add_argument('--monitor-interval', 
                              type=int,
                              metavar='SECONDS',
                              help='Status update interval on AC power (default: 10s)')
    monitor_group.add_argument('--shutdown-delay', 
                              type=int,
                              metavar='SECONDS',
                              help='Delay before shutdown on critical battery (default: 60s)')
    monitor_group.add_argument('--disable-auto-shutdown', 
                              action='store_true',
                              help='Disable automatic shutdown on critical battery')

    # MQTT Configuration
    mqtt_group = parser.add_argument_group('MQTT Configuration')
    mqtt_group.add_argument('--mqtt-broker', 
                           type=str,
                           metavar='HOST',
                           help='MQTT broker hostname/IP (use Pi IP if not in Docker)')
    mqtt_group.add_argument('--mqtt-port', 
                           type=int,
                           metavar='PORT',
                           help='MQTT broker port (default: 1883)')
    mqtt_group.add_argument('--mqtt-user', 
                           type=str,
                           metavar='USERNAME',
                           help='MQTT authentication username')
    mqtt_group.add_argument('--mqtt-password', 
                           type=str,
                           metavar='PASSWORD',
                           help='MQTT authentication password')
    mqtt_group.add_argument('--mqtt-topic', 
                           type=str,
                           metavar='TOPIC',
                           help='MQTT base topic (default: presto_x728_ups)')
    mqtt_group.add_argument('--mqtt-publish-interval', 
                           type=int,
                           metavar='SECONDS',
                           help='MQTT publish interval (default: 10s)')

    # Notification Settings
    ntfy_group = parser.add_argument_group('Notification Settings (ntfy)')
    ntfy_group.add_argument('--enable-ntfy', 
                           action='store_true',
                           help='Enable ntfy push notifications')
    ntfy_group.add_argument('--disable-ntfy', 
                           action='store_true',
                           help='Disable ntfy push notifications')
    ntfy_group.add_argument('--ntfy-server', 
                           type=str,
                           metavar='URL',
                           help='ntfy server URL (default: https://ntfy.sh)')
    ntfy_group.add_argument('--ntfy-topic', 
                           type=str,
                           metavar='TOPIC',
                           help='ntfy topic name (default: x728_UPS)')

    # Logging
    log_group = parser.add_argument_group('Logging')
    log_group.add_argument('--log-level', 
                          type=str,
                          default=LOG_LEVEL,
                          choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                          metavar='LEVEL',
                          help='Console logging verbosity (default: INFO)')
    log_group.add_argument('--enable-debug', 
                          action='store_true',
                          help='Enable debug file logging')
    log_group.add_argument('--disable-debug', 
                          action='store_true',
                          help='Disable debug file logging')

    args = parser.parse_args()
    
    
    
    if args.install_service:
        if os.geteuid() != 0:
            print("❌ Service installation requires root privileges.")
            print("   Please run: sudo python3 presto_x728_sysmon.py --install-service")
            sys.exit(1)
        sys.exit(0 if install_service() else 1)

    if args.uninstall_service:
        if os.geteuid() != 0:
            print("❌ Service uninstallation requires root privileges.")
            print("   Please run: sudo python3 presto_x728_sysmon.py --uninstall-service")
            sys.exit(1)
        sys.exit(0 if uninstall_service() else 1)
    
    
    
    
    
    
    # --- DYNAMIC PIN SETTING LOGIC ---
    version_source = "Default (V1.x)"

    # Priority is already set in the override section above
    # This section just applies the final GPIO pin
    if X728_HW_VERSION >= 2:
        GPIO_SHUTDOWN_PIN = 26 
    else: 
        GPIO_SHUTDOWN_PIN = 13 

    log_message(f"X728 HW Version V{X728_HW_VERSION} detected via {version_source}. Using Shutdown GPIO BCM {GPIO_SHUTDOWN_PIN}", "INFO")
    # --- End DYNAMIC PIN SETTING LOGIC ---
    
    
    
    # Apply overrides
    # --- Apply Command-Line Overrides to Configuration ---
    if not (args.install_service or args.uninstall_service):
        # Only apply config overrides if NOT installing/uninstalling service
        
        # Hardware version
        if args.hw_version in [1, 2]:
            X728_HW_VERSION = args.hw_version
            version_source = "Command-Line Argument (--hw-version)"
        
        # Battery thresholds
        if args.low_battery is not None:
            config['low_battery_threshold'] = args.low_battery
            log_message(f"Low battery threshold set to {args.low_battery}% via CLI", "INFO")
        
        if args.critical_battery is not None:
            config['critical_low_threshold'] = args.critical_battery
            log_message(f"Critical battery threshold set to {args.critical_battery}% via CLI", "INFO")
        
        # System thresholds
        if args.cpu_temp is not None:
            config['cpu_temp_threshold'] = args.cpu_temp
            log_message(f"CPU temp threshold set to {args.cpu_temp}°C via CLI", "INFO")
        
        if args.disk_space is not None:
            config['disk_space_threshold'] = args.disk_space
            log_message(f"Disk space threshold set to {args.disk_space}GB via CLI", "INFO")
        
        # Monitoring settings
        if args.monitor_interval is not None:
            config['monitor_interval'] = args.monitor_interval
            log_message(f"Monitor interval set to {args.monitor_interval}s via CLI", "INFO")
        
        if args.shutdown_delay is not None:
            config['shutdown_delay'] = args.shutdown_delay
            log_message(f"Shutdown delay set to {args.shutdown_delay}s via CLI", "INFO")
        
        if args.disable_auto_shutdown:
            config['enable_auto_shutdown'] = 0
            log_message("Auto-shutdown disabled via CLI", "WARNING")
        
        # MQTT settings
        if args.mqtt_broker:
            MQTT_BROKER = args.mqtt_broker
            log_message(f"MQTT broker set to {args.mqtt_broker} via CLI", "INFO")
        
        if args.mqtt_port:
            MQTT_PORT = args.mqtt_port
            log_message(f"MQTT port set to {args.mqtt_port} via CLI", "INFO")
        
        if args.mqtt_user:
            MQTT_USER = args.mqtt_user
            log_message(f"MQTT username set via CLI", "INFO")
        
        if args.mqtt_password:
            MQTT_PASSWORD = args.mqtt_password
            log_message(f"MQTT password set via CLI", "INFO")
        
        if args.mqtt_topic:
            MQTT_BASE_TOPIC = args.mqtt_topic
            log_message(f"MQTT base topic set to {args.mqtt_topic} via CLI", "INFO")
        
        if args.mqtt_publish_interval:
            MQTT_PUBLISH_INTERVAL_SEC = args.mqtt_publish_interval
            log_message(f"MQTT publish interval set to {args.mqtt_publish_interval}s via CLI", "INFO")
        
        # Notification settings
        if args.enable_ntfy:
            config['enable_ntfy'] = 1
            log_message("ntfy notifications enabled via CLI", "INFO")
        
        if args.disable_ntfy:
            config['enable_ntfy'] = 0
            log_message("ntfy notifications disabled via CLI", "INFO")
        
        if args.ntfy_server:
            config['ntfy_server'] = args.ntfy_server
            log_message(f"ntfy server set to {args.ntfy_server} via CLI", "INFO")
        
        if args.ntfy_topic:
            config['ntfy_topic'] = args.ntfy_topic
            log_message(f"ntfy topic set to {args.ntfy_topic} via CLI", "INFO")
        
        # Logging settings
        if args.log_level:
            LOG_LEVEL = args.log_level.upper()
            log_message(f"Console log level set to {LOG_LEVEL} via CLI", "INFO")
        
        if args.enable_debug:
            config['debug'] = 1
            LOG_LEVEL = 'DEBUG'
            log_message("Debug file logging enabled via CLI", "INFO")
        
        if args.disable_debug:
            config['debug'] = 0
            log_message("Debug file logging disabled via CLI", "INFO")

    # --- End Configuration Overrides ---
    
    
    
    # Perform initialization
    initialize_core_services()
    
    
    
    # Start server
    try:
        # Detect execution mode for user clarity
        if os.path.exists('/.dockerenv'):
            mode = "Docker Container"
        elif os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
            mode = "Flask Reloader"
        else:
            mode = "Direct Python / Systemd Service"
        
        log_message(f"Starting web server on port 7728... (Mode: {mode})", "INFO")
        
        # For direct execution, explicitly initialize if not already done
        if not _SERVICES_INITIALIZED:
            initialize_core_services()
        
        socketio.run(
            app, 
            host='0.0.0.0', 
            port=7728, 
            debug=False,
            allow_unsafe_werkzeug=True,
            use_reloader=False
        )
    except Exception as e:
        log_message(f"Flask/SocketIO server failed to start: {e}", "CRITICAL")