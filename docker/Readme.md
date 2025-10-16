<div align="center">
  
# X728 UPS Monitor 🔋⚡
  
</div>
<div align="center">

![X728 Logo](https://img.shields.io/badge/X728-UPS_Monitor-blue?style=for-the-badge&logo=raspberry-pi)
![Version](https://img.shields.io/badge/version-3.0.9-green?style=for-the-badge)
![Python](https://img.shields.io/badge/python-3.6+-yellow?style=for-the-badge&logo=python)
![Docker](https://img.shields.io/badge/docker-ready-blue?style=for-the-badge&logo=docker)

[![Docker Image CI - Native ARM64 Only](https://github.com/piklz/pi_ups_monitors/actions/workflows/docker-image.yml/badge.svg)](https://github.com/piklz/pi_ups_monitors/actions/workflows/docker-image.yml)

**Professional Battery Monitoring & Management Server for Raspberry Pi**

[Features](#-features) • [Installation](#-installation) • [Usage](#-usage) • [Configuration](#-configuration) • [Troubleshooting](#-troubleshooting)

</div>

---

## 📋 Overview

X728 UPS Monitor is a comprehensive monitoring solution for the X728 UPS HAT (v1.2+) designed for Raspberry Pi 3, 4, and 5. It provides real-time battery monitoring, automatic shutdown capabilities, and a beautiful web-based dashboard.

### ✨ Features

- 🔋 **Real-time Battery Monitoring** - Track battery level, voltage, and power state
- 📊 **Interactive Dashboard** - Modern web UI with live charts and metrics
- 🔔 **Smart Notifications** - ntfy integration for alerts and warnings
- ⚙️ **Auto-Shutdown** - Safe shutdown on critical battery levels
- 🌡️ **System Monitoring** - CPU temperature, disk space, and memory tracking
- 🐳 **Docker Ready** - Easy deployment with Docker/Docker Compose
- 🌓 **Dark Mode** - Beautiful light and dark themes
- 📈 **Historical Data** - Battery history with visual charts
- 🔐 **Safe Shutdown** - Kernel overlay configuration for UPS power-off

---

## 🎯 Compatibility

### Supported Hardware
- ✅ Raspberry Pi 3 (all models)
- ✅ Raspberry Pi 4 (all models)
- ✅ Raspberry Pi 5
- ✅ X728 UPS HAT v1.2+

### Supported Operating Systems
- 🍓 **Raspberry Pi OS** (Bullseye, Bookworm)
- 🥧 **DietPi**
- 🎮 **RetroPie**
- 🐧 **Ubuntu Server** (for Pi)

---

## 🚀 Quick Start

### Option 1: Docker Compose (Recommended)

```bash
# Create directory
mkdir -p ~/x728_monitor
cd ~/x728_monitor

# Download docker-compose.yml
wget https://raw.githubusercontent.com/piklz/pi_ups_monitors/main/docker/docker-compose.yml

# Start the service
docker compose up -d
```

### Option 2: Docker Run

```bash
docker run -d \
  --name x728_monitor \
  --privileged \
  --restart unless-stopped \
  -p 5000:5000 \
  -v /config:/config \
  -v /:/host:ro \
  -v /sys:/sys \
  -v /dev/gpiochip0:/dev/gpiochip0 \
  -e TZ=America/New_York \
  piklz/x728_monitor:latest
```

### Option 3: Direct Python Installation

```bash
# Install dependencies
sudo apt update
sudo apt install -y python3-pip python3-gpiod i2c-tools

# Clone repository
git clone https://github.com/piklz/pi_ups_monitors.git
cd pi_ups_monitors

# Install Python packages
pip3 install -r requirements.txt

# Run the script
sudo python3 x728_web.py
```

---

## 🔧 Installation Guide

### 1️⃣ Enable I2C Interface

<details>
<summary>Click to expand I2C setup instructions</summary>

```bash
# Enable I2C
sudo raspi-config nonint do_i2c 0

# Verify I2C is enabled
lsmod | grep i2c

# Test I2C devices (should see address 0x36 or similar)
sudo i2cdetect -y 1
```

Expected output:
```
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:          -- -- -- -- -- -- -- -- -- -- -- -- -- 
10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 
20: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 
30: -- -- -- -- -- -- 36 -- -- -- -- -- -- -- -- -- 
```

</details>

### 2️⃣ Configure Kernel Overlay (Critical!)

The script will **automatically** configure the kernel overlay on first run. However, you can verify manually:

```bash
# Check if overlay exists
grep "dtoverlay=gpio-poweroff" /boot/firmware/config.txt

# It should show:
# dtoverlay=gpio-poweroff,gpiopin=13,active_low=0,timeout_ms=10000
```

> ⚠️ **Important**: You must **reboot** after the overlay is configured for safe shutdown to work!

```bash
sudo reboot
```

### 3️⃣ Docker Installation (If using Docker)

```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Add user to docker group
sudo usermod -aG docker $USER

# Install Docker Compose
sudo apt install -y docker-compose

# Logout and login for group changes to take effect
```

---

## 🌐 Access the Dashboard

Once running, access the dashboard at:

```
http://YOUR_PI_IP:5000
```

Example:
- Local: `http://192.168.1.100:5000`
- Hostname: `http://raspberrypi.local:5000`

---

## ⚙️ Configuration

### Web Interface Configuration

All settings can be configured through the web dashboard:

1. **Battery Thresholds**
   - Low Battery Warning (default: 30%)
   - Critical Shutdown (default: 10%)

2. **System Thresholds**
   - CPU Temperature Alert (default: 70°C)
   - Disk Space Warning (default: 10 GB)

3. **Monitoring Settings**
   - Update Interval (default: 10 seconds)
   - Shutdown Delay (default: 60 seconds)
   - Auto-Shutdown Enable/Disable

4. **Notifications (ntfy)**
   - Enable/Disable notifications
   - ntfy Server URL (default: https://ntfy.sh)
   - ntfy Topic name

### Configuration File

Settings are stored in `/config/x728_config.json` (or `./config/x728_config.json` for local installations).

<details>
<summary>Example configuration file</summary>

```json
{
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
    "idle_load_ma": 500
}
```

</details>

---

## 📱 Notifications Setup (ntfy)

Get instant alerts on your phone or desktop!

### 1️⃣ Install ntfy App
- 📱 [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
- 🍎 [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)
- 🖥️ [Desktop](https://ntfy.sh)

### 2️⃣ Subscribe to Your Topic
- Open ntfy app
- Subscribe to: `x728_UPS` (or your custom topic)

### 3️⃣ Enable in Dashboard
- Open web dashboard
- Navigate to Configuration → Notifications
- Check "Enable ntfy Notifications"
- Set your topic name
- Save configuration

### 📬 Notification Types
- 🔋 Low Battery Warnings
- 🚨 Critical Battery Shutdown
- ⚡ Power State Changes (AC disconnect/reconnect)
- 🌡️ High CPU Temperature
- 💾 Low Disk Space
- 🚀 System Startup Summary

---

## 🎨 Dashboard Features

### Main Metrics
- 🔋 **Battery Level** - Real-time percentage with color-coded bar
- ⚡ **Voltage** - Current battery voltage
- 🔌 **Power State** - AC Power / On Battery / Critical
- ⏱️ **Time Remaining** - Estimated runtime on battery

### System Monitoring
- 🌡️ CPU/GPU Temperature
- 💾 Disk Usage (with label detection)
- 🧠 Memory Usage (Free/Total)
- ⏰ System Uptime

### Interactive Charts
- 📊 Battery History (last 50 readings)
- 📈 Voltage Trends
- 🔄 Real-time updates via WebSocket

### System Control
- 🔄 **Reboot** - Restart the system
- 🛑 **Shutdown** - Safe shutdown with delay
- ⏸️ **Cancel** - Cancel pending shutdown/reboot

---

## 🛠️ Troubleshooting

### I2C Not Detected

```bash
# Check if I2C is enabled
sudo raspi-config nonint do_i2c 0

# Verify I2C modules loaded
lsmod | grep i2c_dev

# Scan for devices
sudo i2cdetect -y 1
```

### GPIO Errors

```bash
# Check GPIO chip exists
ls -la /dev/gpiochip0

# Verify permissions (Docker needs --privileged)
groups $USER
```

### Docker Permission Issues

```bash
# Ensure container runs with --privileged flag
docker run --privileged ...

# Or use docker-compose with privileged: true
```

### Kernel Overlay Not Working

```bash
# Verify overlay in config
sudo cat /boot/firmware/config.txt | grep gpio-poweroff

# Expected line:
# dtoverlay=gpio-poweroff,gpiopin=13,active_low=0,timeout_ms=10000

# If missing, add manually and reboot
echo "dtoverlay=gpio-poweroff,gpiopin=13,active_low=0,timeout_ms=10000" | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

### Dashboard Not Loading

```bash
# Check if service is running
docker ps  # For Docker
sudo systemctl status x728_monitor  # For systemd

# Check logs
docker logs x728_monitor  # Docker
journalctl -u x728_monitor -f  # systemd

# Verify port is open
sudo netstat -tulpn | grep 5000
```

### Battery Not Charging

- Ensure AC power is connected
- Check X728 HAT is properly seated on GPIO pins
- Verify batteries are installed correctly
- Check battery connections on X728 board

---

## 🔒 Security Notes

### Docker Privileged Mode
The container requires `--privileged` flag for:
- GPIO access (`/dev/gpiochip0`)
- System shutdown/reboot capabilities
- Host filesystem access for safe shutdown

### Network Security
- Dashboard runs on port 5000 (HTTP)
- Consider using a reverse proxy (nginx, Caddy) for HTTPS
- Restrict access with firewall rules if exposed to internet

---

## 📚 Advanced Configuration

### Custom Port

```bash
# Change port in docker-compose.yml
ports:
  - "8080:5000"  # Host:Container

# Or in docker run
docker run -p 8080:5000 ...
```

### Persistent Data Location

Docker:
```yaml
volumes:
  - /custom/path:/config  # Configuration
  - /var/log/x728:/config  # Logs
```

Direct Install:
```python
# Edit in x728_web.py
CONFIG_PATH = "/your/custom/path/x728_config.json"
LOG_PATH = "/your/custom/path/x728_debug.log"
```

### Auto-Start on Boot

Docker Compose:
```yaml
restart: unless-stopped
```

Systemd Service:
```bash
# Create service file
sudo nano /etc/systemd/system/x728_monitor.service

[Unit]
Description=X728 UPS Monitor
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/x728_monitor
ExecStart=/usr/bin/python3 /opt/x728_monitor/x728_web.py
Restart=always

[Install]
WantedBy=multi-user.target

# Enable service
sudo systemctl enable x728_monitor
sudo systemctl start x728_monitor
```

---

## 📊 System Requirements

### Minimum Requirements
- Raspberry Pi 3/4/5
- 512MB RAM (1GB recommended)
- 100MB disk space
- X728 UPS HAT v1.2+
- 2x 18650 batteries (recommended: 3500mAh each)

### Software Requirements
- Python 3.6+
- Docker 20.10+ (for Docker installation)
- I2C kernel modules enabled
- GPIO access (gpiod)

---

## 🤝 Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Open a Pull Request

---

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- X728 UPS HAT hardware by Geekworm
- Flask framework for web interface
- Chart.js for data visualization
- Socket.IO for real-time updates
- ntfy for notification service

---

## 📞 Support

- 🐛 **Issues**: [GitHub Issues](https://github.com/piklz/pi_ups_monitors/issues)
- 💬 **Discussions**: [GitHub Discussions](https://github.com/piklz/pi_ups_monitors/discussions)
- 📧 **Email**: piklz@example.com

---

## 🌟 Star History

If you find this project useful, please consider giving it a ⭐!

---

<div align="center">

**Made with ❤️ for the Raspberry Pi Community**

![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-C51A4A?style=for-the-badge&logo=raspberry-pi&logoColor=white)
![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white)

</div>
