# <p align="center">Raspberry Pi UPS Monitors [X728 | Waveshare UPS HAT (C)] ⚡🔋</p>

<div align="center">

[![GitHub License](https://img.shields.io/github/license/piklz/pi_ups_monitors?style=flat-square&color=blue)](https://github.com/piklz/pi_ups_monitors/blob/main/LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.7%2B-blue?style=flat-square)](https://www.python.org/)
[![GitHub Stars](https://img.shields.io/github/stars/piklz/pi_ups_monitors?style=flat-square)](https://github.com/piklz/pi_ups_monitors/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/piklz/pi_ups_monitors?style=flat-square)](https://github.com/piklz/pi_ups_monitors/network/members)
[![GitHub Issues](https://img.shields.io/github/issues/piklz/pi_ups_monitors?style=flat-square)](https://github.com/piklz/pi_ups_monitors/issues)

## [Support me! ☕](https://buymeacoffee.com/pixelpiklz)

</div>

<p align="center"> <img width="520" height="382" alt="piklz_ups_monitors_logo" src="https://github.com/user-attachments/assets/2ea1f180-bb89-4302-8931-576cd7aebd09" /></p>

<br></br>
<p align="center"> <img width="300" height="300" src="https://github.com/user-attachments/assets/cfdc4e0d-7ec1-4560-94fe-9bb576172f34"/>
<img width="300" height="226" src="https://github.com/user-attachments/assets/c67341a6-6d0c-4368-8f3f-67c879ca9918"/></p>

***

**_A collection of Python scripts for monitoring Uninterruptible Power Supply (UPS) HATs on Raspberry Pi devices_**.<br></br>
The **Presto UPS Monitor** focuses on the **Geekworm X728** (v1.2+) and **Waveshare UPS HAT (C)** for Raspberry Pi Zero, ensuring safe operation for your projects. These scripts monitor critical metrics like battery voltage, current, and power status, enabling automated graceful shutdowns to prevent data corruption and sending real-time notifications via [ntfy.sh](https://ntfy.sh) to keep you informed. 🚀

[Explore the docs »](https://github.com/piklz/pi_ups_monitors/wiki)

[Report Bug](https://github.com/piklz/pi_ups_monitors/issues/new?labels=bug) · [Request Feature](https://github.com/piklz/pi_ups_monitors/issues/new?labels=enhancement)

## Table of Contents

- [About the Project](#about-the-project)
  - [Built With](#built-with)
  - [Test a Notification on Your Phone](#lets-test-a-notification-to-your-phone)
  - [Wiki / Help / FAQ](https://github.com/piklz/pi_ups_monitors/wiki)

## About the Project

📋 **presto_x728_monitor.py**: Designed for the **Geekworm X728 UPS HAT**, leveraging its INA219 sensor, S8261/FS8205 BMS, and GPIO controls (e.g., GPIO6 for AC loss detection).  
📋 **presto_hatc_monitor.py**: Tailored for the **Waveshare UPS HAT (C)** for Raspberry Pi Zero, using INA219, ETA6003 charger, and TPS61088 boost with pogo pin integration.

### Key Benefits
- 🛡️ Prevents abrupt power failures with safe shutdowns.
- 📊 Logs battery health (voltage, current, percentage) for analysis.
- ⏰ Runs as a systemd service or cron job for set-and-forget operation.
- 🔔 Sends instant ntfy notifications for power loss, low battery, or shutdown events.
- 🔗 Supports stacking with X-series storage boards (e.g., X825/X857).

<p align="right">(<a href="#raspberry-pi-ups-monitors-x728--hat-c">back to top</a>)</p>

### Built With
- ![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white) - Core scripting language
- SMBus/I2C - For INA219 and hardware communication
- Raspberry Pi OS - Tested on Bookworm; compatible with Pi Zero to 5, Debian-based OSes like DietPi and Ubuntu (testing ongoing)

<p align="right">(<a href="#raspberry-pi-ups-monitors-x728--hat-c">back to top</a>)</p>

## Getting Started

Set up the Presto UPS Monitor on your Raspberry Pi with these steps. Quick and easy! ⏱️

### Prerequisites
- Raspberry Pi (Zero, 1, 2, 3, 4, or 5)
- Geekworm X728 UPS HAT or Waveshare UPS HAT (C)
- Raspberry Pi OS or Debian-based OS (e.g., DietPi, Ubuntu)
- Basic terminal knowledge and SSH access

### Installation
1. Clone the repository:
   ```bash
   git clone https://github.com/piklz/pi_ups_monitors/
   ```
2. Navigate to the scripts directory:
   ```bash
   cd ~/pi_ups_monitors/scripts
   ```
3. Check usage for your UPS HAT:
   ```bash
   ./presto_x728_monitor.py --help
   ```
   or
   ```bash
   ./presto_hatc_monitor.py --help
   ```

4. Test the script live (without arguments):
   ```bash
   ./presto_x728_monitor.py
   ```
   or
   ```bash
   ./presto_hatc_monitor.py
   ```

### Command-Line Options

#### For Geekworm X728 (`presto_x728_monitor.py v1.5.1`)
```bash
usage: presto_x728_monitor.py [-h] [-i] [-u] [-t] [-ntfy] [-ns NTFY_SERVER] [-nt NTFY_TOPIC] [--low-battery-threshold LOW_BATTERY_THRESHOLD]
                              [--critical-low-threshold CRITICAL_LOW_THRESHOLD] [-d]

x728 UPS HAT Monitor with Service Installation (Version 1.5.1)

options:
  -h, --help            show this help message and exit
  -i, --install_as_service
                        Install as a systemd service
  -u, --uninstall       Uninstall the x728_ups service
  -t, --test-ntfy       Send a test ntfy notification (requires --enable-ntfy)
  -ntfy, --enable-ntfy  Enable ntfy notifications (default: False)
  -ns NTFY_SERVER, --ntfy-server NTFY_SERVER
                        ntfy server URL (default: https://ntfy.sh)
  -nt NTFY_TOPIC, --ntfy-topic NTFY_TOPIC
                        ntfy topic for notifications (default: x728_UPS)
  --low-battery-threshold LOW_BATTERY_THRESHOLD
                        Low battery threshold percentage (default: 30%)
  --critical-low-threshold CRITICAL_LOW_THRESHOLD
                        Critical low battery threshold percentage (default: 10%)
  -d, --debug           Enable debug logging for raw I2C data (default: False)

Useful journalctl commands for monitoring:
  - Recent battery/voltage logs: journalctl -u presto_x728_ups.service | grep -E "Battery level|Voltage" -m 10
  - Power event logs: journalctl -u presto_x728_ups.service | grep -E "Power Loss|Power Restored|Shutdown" -m 10
  - Critical errors: journalctl -u presto_x728_ups.service -p 0..3 -n 10
  - Debug logs (if --debug enabled): journalctl -u presto_x728_ups.service | grep DEBUG -m 10
```

#### For Waveshare UPS HAT (C) (`presto_hatc_monitor.py v1.5.5`)
```bash
usage: presto_hatc_monitor.py [-h] [-i] [-u] [-ntfy] [-nt NTFY_TOPIC] [-ns NTFY_SERVER] [-t] [--power-threshold POWER_THRESHOLD] [--percent-threshold PERCENT_THRESHOLD]
                              [--critical-low-threshold CRITICAL_LOW_THRESHOLD] [--critical-shutdown-delay CRITICAL_SHUTDOWN_DELAY]
                              [--battery-capacity-mah BATTERY_CAPACITY_MAH] [--ntfy-cooldown-seconds NTFY_COOLDOWN_SECONDS]

Presto HAT C UPS Monitor v1.5.5

options:
  -h, --help            show this help message and exit
  -i, --install_as_service
                        Install the script as a systemd service.
  -u, --uninstall       Uninstall the systemd service and script.
  -ntfy, --enable-ntfy  Enable ntfy notifications.
  -nt NTFY_TOPIC, --ntfy-topic NTFY_TOPIC
                        ntfy topic name. Defaults to "presto_hatc_ups".
  -ns NTFY_SERVER, --ntfy-server NTFY_SERVER
                        ntfy server URL. Defaults to "https://ntfy.sh".
  -t, --test-ntfy       Send a test notification and exit.
  --power-threshold POWER_THRESHOLD
                        Power threshold in Watts to detect power loss.
  --percent-threshold PERCENT_THRESHOLD
                        Battery percentage threshold for low battery alert.
  --critical-low-threshold CRITICAL_LOW_THRESHOLD
                        Battery percentage threshold for critical low alert, triggering shutdown.
  --critical-shutdown-delay CRITICAL_SHUTDOWN_DELAY
                        Delay in seconds before shutdown on critical battery level.
  --battery-capacity-mah BATTERY_CAPACITY_MAH
                        Battery capacity in mAh for time remaining estimation.
  --ntfy-cooldown-seconds NTFY_COOLDOWN_SECONDS
                        Cooldown in seconds between repeated notifications for the same event.

Useful journalctl commands for monitoring:
  - View real-time logs: journalctl -u presto_hatc_monitor.service -f
  - View last 50 logs: journalctl -u presto_hatc_monitor.service -n 50
  - Recent voltage/current logs: journalctl -u presto_hatc_monitor.service | grep -E "Voltage|Current|Power|Percent" -m 10
  - Power event logs: journalctl -u presto_hatc_monitor.service | grep -E "unplugged|reconnected|Low power|Low percent" -m 10
  - Critical errors: journalctl -u presto_hatc_monitor.service -p 0..3 -n 10
  - Check service status: systemctl status presto_hatc_monitor.service
```

### Let's Test a Notification to Your Phone
![ntfy](https://play-lh.googleusercontent.com/O9uRWkaFLCzl7wkpeUWFuJfllrvykC6wOCR3sy8sZkrCyIMs-DPv7j7D710QY8VSc7KN=w200-h440-rw)

**Step 1: Install ntfy**
- Download the ntfy app:
  - **Android**: [Google Play Store](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
  - **iOS**: [App Store](https://apps.apple.com/us/app/ntfy/id1625396347)
- Open the app, click "+", and subscribe to a topic (e.g., `presto_hatc_ups` for default testing).

**Step 2: Test Notification**
- If the script is already running as a service (`systemctl status x728_ups.service` or `presto_hatc_monitor.service`), stop it first:
  ```bash
  sudo systemctl stop x728_ups.service
  ```
  or
  ```bash
  sudo systemctl stop presto_hatc_monitor.service
  ```
- Send a test notification:
  ```bash
  ~/pi_ups_monitors/scripts/presto_x728_monitor.py --enable-ntfy --test-ntfy --ntfy-topic presto_hatc_ups
  ```
  or
  ```bash
  ~/pi_ups_monitors/scripts/presto_hatc_monitor.py --enable-ntfy --test-ntfy --ntfy-topic presto_hatc_ups
  ```
- Try a custom topic:
  ```bash
  ~/pi_ups_monitors/scripts/presto_x728_monitor.py --enable-ntfy --test-ntfy --ntfy-topic UPS_PI_OFFICE
  ```
  - via ntfy website (and mobile ) you'll get nice notifications like this :
<div align="center">  
<img width="500" height="700" alt="image" src="https://github.com/user-attachments/assets/9908b0b6-07a5-40f1-a934-d03f2e9422c8" />
</div>

**Step 3: Install as a Service**
- Install with custom settings (e.g., custom topic or thresholds):
  ```bash
  sudo ~/pi_ups_monitors/scripts/presto_x728_monitor.py --install_as_service --enable-ntfy --ntfy-topic presto_hatc_ups --low-battery-threshold 40
  ```
  or
  ```bash
  sudo ~/pi_ups_monitors/scripts/presto_hatc_monitor.py --install_as_service --enable-ntfy --ntfy-topic presto_hatc_ups --percent-threshold 30
  ```
- Confirm settings (defaults apply for unspecified options):
  ```
  [INFO] New settings: enable-ntfy=True, ntfy-server=https://ntfy.sh, ntfy-topic=presto_hatc_ups, low-battery-threshold=40%, critical-low-threshold=10%
  ```
- The service will reload with your settings. Test by unplugging the power to receive a battery power alert with estimated time remaining.

### Tips
#### Running in Terminal (If Service is Active)
- Stop the service to test in terminal:
  ```bash
  sudo systemctl stop x728_ups.service
  ```
  or
  ```bash
  sudo systemctl stop presto_hatc_monitor.service
  ```
- Run with desired options, then restart the service:
  ```bash
  sudo systemctl start x728_ups.service
  ```
  or
  ```bash
  sudo systemctl start presto_hatc_monitor.service
  ```

#### Uninstalling the Service
- Remove the service completely:
  ```bash
  sudo ~/pi_ups_monitors/scripts/presto_x728_monitor.py --uninstall
  ```
  or
  ```bash
  sudo ~/pi_ups_monitors/scripts/presto_hatc_monitor.py --uninstall
  ```

#### Checking Logs for Issues
- **X728 Logs**:
  - Recent battery/voltage: `journalctl -u x728_ups.service | grep -E "Battery level|Voltage" -m 10`
  - Power events: `journalctl -u x728_ups.service | grep -E "Power Loss|Power Restored|Shutdown" -m 10`
  - Critical errors: `journalctl -u x728_ups.service -p 0..3 -n 10`
  - Debug logs: `journalctl -u x728_ups.service | grep DEBUG -m 10`
- **Waveshare UPS HAT (C) Logs**:
  - Real-time logs: `journalctl -u presto_hatc_monitor.service -f`
  - Last 50 logs: `journalctl -u presto_hatc_monitor.service -n 50`
  - Voltage/current: `journalctl -u presto_hatc_monitor.service | grep -E "Voltage|Current|Power|Percent" -m 10`
  - Power events: `journalctl -u presto_hatc_monitor.service | grep -E "unplugged|reconnected|Low power|Low percent" -m 10`
  - Critical errors: `journalctl -u presto_hatc_monitor.service -p 0..3 -n 10`
  - Service status: `systemctl status presto_hatc_monitor.service`
- General: Check `/var/log` or use `dmesg -f` for system logs.

<p align="right">(<a href="#raspberry-pi-ups-monitors-x728--hat-c">back to top</a>)</p>



