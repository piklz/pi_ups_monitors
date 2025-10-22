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
Version: 3.1.5
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


CHANGELOG:
- v3.1.5 :  redo file init to ensure correct permissions/ownership on docker vs local runs
- v3.1.4 :  new check updates button placement
- v3.1.3 :  fixed/changed path for local python run vs docker run config dirs and logs
- v3.1.2 :  removed emoji tag and added darkmode to top right (flex vert stack)
- v3.1.1 :  fixed mqtt publish interval env var parsing issue and docker-vs-script run issues  + less log chatter




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
VERSION_NUMBER= "3.1.5"
VERSION_STRING = "Prestos X728 UPS Monitor"
VERSION_BUILD = "Professional Docker Edition"


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

# Constants
I2C_ADDRS = [0x16, 0x36, 0x3b, 0x4b]
CONFIG_PATH = "/config/x728_config.json"
LOG_PATH = "/config/x728_debug.log"
HISTORY_PATH = "/config/battery_history.json"

# X728 GPIO Pins (BCM numbering)
GPIO_PLD_PIN = 6   # Power Loss Detection
GPIO_SHUTDOWN_PIN = 13 # Shutdown signal to UPS pi only
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
# UTILITY FUNCTIONS
# ============================================================================

# Global MQTT Client instance
mqtt_client = None

def init_mqtt():
    """Initializes the MQTT client with a unique ID and attempts to connect to the broker."""
    global mqtt_client, FIRST_CONNECTION_MADE
    
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




def log_message(message, level="INFO"):
    """Enhanced logging with levels, respecting LOG_LEVEL for file writing."""
    global config, LOG_LEVEL
    
    level_upper = level.upper()
    message_level_value = LOG_LEVEL_MAP.get(level_upper, 0)
    # NOTE: LOG_LEVEL is now a global controlled by load_config() and the UI toggle
    configured_level_value = LOG_LEVEL_MAP.get(LOG_LEVEL, 2) 

    # Check if the message's level is verbose enough to be printed
    # (The custom map logic works as intended: DEBUG=1, INFO=2, WARN=3 -> 2 >= 2 prints INFO)
    should_print = (message_level_value >= configured_level_value)
    
    # 1. Console / Docker Output Check
    if should_print:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level_upper}] {message}")

       
    # FIX: ONLY write to file if the message was verbose enough to print 
    # AND the debug file-write flag (config['debug']) is enabled
    if should_print and config.get('debug', 0) == 1:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = f"[{timestamp}] [{level_upper}] {message}"
        
        with lock:
            try:
                # The file is expected to exist from initialize_files()
                with open(LOG_PATH, 'a') as f:
                    f.write(log_entry + '\n')
            except Exception as e:
                print(f"ERROR: Failed to write to log: {e}")                

def load_config():
    """Load configuration from JSON file"""
    global config, LOG_LEVEL

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
  
    log_message("Monitor thread started")
    monitor_thread_running = True
    
    # ADDED: Initialize interval outside try block
    interval = config.get('monitor_interval', 10) 

    while not monitor_thread_stop_event.is_set():
        try:
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
        f"🚀 Presto x728 UPS Monitor powered up [v{CURRENT_VERSION}] \n"
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

# (Ensures MQTT runs under Gunicorn)


# Define the setup function:
@app.before_request
def startup_setup():
    """Initializes MQTT and the monitoring thread safely under Gunicorn."""
    global monitor_thread, monitor_thread_running, SETUP_COMPLETE
    
    # Check the flag: Run initialization ONLY on the first request for this worker
    if not SETUP_COMPLETE:
        log_message("Attempting one-time startup (MQTT/Thread)...", "INFO")
        
        # 1. Initialize MQTT
        init_mqtt() 
        
        # 2. Start Monitoring Thread
        # Check monitor_thread_running flag (which should be set to False initially)
        if not monitor_thread_running: 
            monitor_thread = threading.Thread(target=monitor_thread_func, daemon=True)
            monitor_thread.start()
            log_message("Monitoring thread started successfully.", "INFO")
        
        SETUP_COMPLETE = True
        log_message("One-time startup complete.", "INFO")





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
# GUNICORN/MODULE INITIALIZATION 
# ============================================================================


# ----------------------------------------------------------------------
# STAGE 1: SYSTEM FOUNDATION (Files, Config, OS Prep)
# ----------------------------------------------------------------------
initialize_files()        # Create necessary file structures.
load_config()             # Load all application settings and thresholds.
load_battery_history()    # Load historical battery data for tracking.
configure_kernel_overlay()# Prepare the OS for hardware access (I2C/GPIO drivers).

# ----------------------------------------------------------------------
# STAGE 2: CORE RESOURCE ACQUISITION (Hardware & Network Setup)
# ----------------------------------------------------------------------
init_hardware()           # Initialize the physical X728 HAT (I2C bus and GPIO).
init_mqtt()               # Initialize the unique MQTT client and connect to the broker.(mosquitto container)

# ----------------------------------------------------------------------
# STAGE 3: APPLICATION START ( Monitoring & Notifications)
# ----------------------------------------------------------------------
start_monitor()           # Start the continuous monitoring thread (relies on hardware and MQTT).
send_startup_ntfy()       # Send final confirmation notification that the service is running.





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
                        <a href="https://github.com/piklz/pi_ups_monitors/tree/main/docker"><img width="75%" src="data:image/png;base64,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"alt="Embedded Image"/> </a>
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
                    // Apply flashing class to the lightbulb emoji
                    versionStatusElement.innerHTML = `
                        <span class="flashing">💡</span>
                        <span style="color: #9ca3af;">New:</span>
                        <a href="https://github.com/{{ GITHUB_REPO }}/releases/latest" 
                           target="_blank" 
                           style="color: yellow; text-decoration: none;">
                            ${versionInfo.latest}
                        </a>
                    `;
                    versionStatusElement.className = 'status-indicator text-warning';
                } else {
                    // Green dot emoji for up-to-date
                    versionStatusElement.innerHTML = `🟢`;
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
        <div align="center" style="padding: 20px; font-size: 1.1em; color: #6b7280;">
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
      

      
 # --- RUN SERVER --- 
if __name__ == '__main__':
    print(f"Starting {VERSION_STRING} {CURRENT_VERSION} - {VERSION_BUILD}")

    # ----------------------------------------------------------------------
    # I.  FOUNDATIONAL SETUP (MUST BE OUTSIDE ANY GUARD)
    # This initializes files/directories and loads config variables (like LOG_LEVEL)
    # ----------------------------------------------------------------------
    initialize_files()    # Ensures config/log files/directories exist (using print() safely)
    load_config()         # Loads config, setting global LOG_LEVEL
    load_battery_history()# Load history data (relies on config/file paths)
    
    # --- Argument Parsing must run here (outside guard) ---
    parser = argparse.ArgumentParser(description="X728 UPS Monitor and MQTT Publisher")
    parser.add_argument('--mqtt-broker', type=str, help='Override MQTT broker hostname/IP.')
    parser.add_argument('--mqtt-port', type=int, help='Override MQTT broker port.')
    parser.add_argument('--log-level', type=str, default=LOG_LEVEL, help='Set logging verbosity (DEBUG, INFO, WARNING, ERROR).')
    
    args = parser.parse_args()

    # Apply overrides (Correctly outside the guard)
    if args.mqtt_broker:
        MQTT_BROKER = args.mqtt_broker
    if args.mqtt_port:
        MQTT_PORT = args.mqtt_port
    # You can apply a command-line LOG_LEVEL override here if needed
    if args.log_level:
        LOG_LEVEL = args.log_level.upper()
    
    
    # ----------------------------------------------------------------------
    # II. CORE SERVICES INITIALIZATION (RUNS ONLY ONCE IN THE MAIN PROCESS)
    # ----------------------------------------------------------------------
    # This check prevents re-running hardware initialization in Flask's reloader or worker processes.
    if os.environ.get('WERKZEUG_RUN_MAIN') or os.environ.get('GUNICORN_PID'):
        log_message("Main application process started. Initializing core services...", "INFO")
        
        # --- YOUR OPTIMIZED INITIALIZATION SEQUENCE ---
        configure_kernel_overlay()
        init_hardware()
        init_mqtt()
        start_monitor()
        send_startup_ntfy()
        # ---------------------------------------------
        
        log_message("All core services initialized successfully.", "INFO")
    else:
        # Optional: Print for the reloader process (only if you want to see it running)
        log_message("Flask reloader process detected. Skipping hardware and monitor thread initialization.", "DEBUG")


    # ----------------------------------------------------------------------
    # III. START SERVER (ALWAYS RUNS)
    # ----------------------------------------------------------------------
    try:
        log_message(f"Starting web server on port 7728...", "INFO")
        
        socketio.run(
            app, 
            host='0.0.0.0', 
            port=7728, 
            debug=False, # Must be False or the reloader runs even more erratically
            allow_unsafe_werkzeug=True
        )
    except Exception as e:
        log_message(f"Flask/SocketIO server failed to start: {e}", "CRITICAL")