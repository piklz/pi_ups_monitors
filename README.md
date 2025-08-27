# <p align="center">Raspberry Pi UPS Monitors [x728 | Hat-c]⚡🔋</p>

<div align="center">
  
[![GitHub License](https://img.shields.io/github/license/piklz/pi_ups_monitors?style=flat-square&color=blue)](https://github.com/piklz/pi_ups_monitors/blob/main/LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.7%2B-blue?style=flat-square)](https://www.python.org/)
[![GitHub Stars](https://img.shields.io/github/stars/piklz/pi_ups_monitors?style=flat-square)](https://github.com/piklz/pi_ups_monitors/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/piklz/pi_ups_monitors?style=flat-square)](https://github.com/piklz/pi_ups_monitors/network/members)
[![GitHub Issues](https://img.shields.io/github/issues/piklz/pi_ups_monitors?style=flat-square)](https://github.com/piklz/pi_ups_monitors/issues) 

</div>


<p align="center "> <img width="600" height="400" alt="piklz_ups_monitors_logo" src="https://github.com/user-attachments/assets/69d541fc-9509-4275-98db-4790f772b444" /></p>
<br> </br>
<p align="center"> <img width="300" height="300" src="https://github.com/user-attachments/assets/cfdc4e0d-7ec1-4560-94fe-9bb576172f34"/>
<img width="300" height="226" src="https://github.com/user-attachments/assets/c67341a6-6d0c-4368-8f3f-67c879ca9918"/></p>

A collection of Python scripts designed for monitoring Uninterruptible Power Supply (UPS) HATs on Raspberry Pi devices. The primary focus is on the Geekworm X728 v1.2 and Waveshare PiZero-HAT-C models for now  , which are essential for ensuring the safe operation of your projects. 

> My Raspberry Pi projects tend to be in headless mode in remote locations alot of the time, These scripts monitor critical metrics like battery voltage and power status, enabling automated, graceful shutdowns to prevent data corruption from sudden power loss. The scripts can also send real-time notifications via Ntfy, keeping you informed of any power-related issues. .🚀


[Explore the docs »](https://github.com/piklz/pi_ups_monitors/wiki)

[Report Bug](https://github.com/piklz/pi_ups_monitors/issues/new?labels=bug) · [Request Feature](https://github.com/piklz/pi_ups_monitors/issues/new?labels=enhancement)


## Table of Contents

- [About the Project](#about-the-project)
  - [Built With](#built-with)
  - [Test A ntfy Alert on Phone](#lets-test-a-notification-to-your-phone)


## About the Project

📋 **Pi UPS Monitors/presto_x728_monitor.py** is for UPS HATs like the Geekworm X728. `presto_x728_monitor.py`.<br>-</br>
📋 **Pi UPS Monitors/presto_hatc_monitor.py** is for Pizero Hat-c by waveshare. `presto_hatc_monitor.py`.

Key benefits:
- Prevents abrupt power failures 🛡️
- Logs battery health for analysis 📊
- Easy integration with cron jobs or systemd services ⏰
- ntfy for android /ios instant notifications or low battery or powerdown issues!
- can run in background as a system service set-and-forget!


<p align="right">(<a href="#readme-top">back to top</a>)</p>

### Built With

- ![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white) - Core scripting language
- SMBus/I2C - For hardware communication
- Raspberry Pi OS - Tested on Bookworm  pi5 should be ok on debian os's like dietpi and ubuntu (i am testing when possible )and other pi's zero to 5

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Getting Started

To set up this monitor on your Raspberry Pi, follow these steps. It's quick and straightforward! ⏱️

### Prerequisites

- Raspberry Pi (any model , e.g., Pi 01234 or 5)
- X728 UPS HAT (or compatible) or waveshare pizero hat-c
- Raspberry Pi OS installed or debian based os (will test dietpi and others)
- Basic terminal knowledge + ssh to remote into it

<br> </br>
- in your user home dir Enter & run:
<pre><code>git clone https://github.com/piklz/pi_ups_monitors/</code></pre>
- Enter the directory:
<pre><code>cd ~/pi_ups_monitors/scripts</code></pre>

- run the one you want for your hat , to see its usage and tips with options run this cmd:
<pre><code> ~/pi_ups_monitors/scripts/presto_x728_monitor.py --help</code></pre>



-Test it live just run code without and args :
<pre><code>~/pi_ups_monitors/scripts/presto_x728_monitor.py</code></pre>
- to test your ntfy topicnotiffication tyr this :
<pre><code>~/pi_ups_monitors/scripts/presto_x728_monitor.py --enable-ntfy --test-ntfy --ntfy-topic x728_TEST</code></pre>


<br> </br>

- heres the options you can use for [geekworm] **x728** :
<pre><code>usage: presto_x728_monitor.py [-h] [--install_as_service] [--uninstall] [--test-ntfy] [--enable-ntfy] [--ntfy-server NTFY_SERVER] [--ntfy-topic NTFY_TOPIC] [--low-battery-threshold LOW_BATTERY_THRESHOLD]
                              [--critical-low-threshold CRITICAL_LOW_THRESHOLD] [--debug]</code></pre>
                              
- heres the options you can use for [waveshare] **hat-c** :
<pre><code>usage: presto_hatc_monitor.py [-h] [--install_as_service] [--addr ADDR] [--ntfy-server NTFY_SERVER] [--ntfy-topic NTFY_TOPIC] [--power-threshold POWER_THRESHOLD] [--percent-threshold PERCENT_THRESHOLD]
                              [--battery-capacity BATTERY_CAPACITY] [--battery-voltage BATTERY_VOLTAGE] [--force-reinstall]</code></pre>

<br> </br>
x728 UPS HAT Monitor with Service Installation 

<pre><code>
options:
  -h, --help            show this help message and exit
  --install_as_service  Install as a systemd service
  --uninstall           Uninstall the x728_ups service
  --test-ntfy           Send a test ntfy notification (requires --enable-ntfy)
  --enable-ntfy         Enable ntfy notifications
  --ntfy-server NTFY_SERVER
                        ntfy server URL (default: https://ntfy.sh)
  --ntfy-topic NTFY_TOPIC
                        ntfy topic for notifications (default: x728_UPS)
  --low-battery-threshold LOW_BATTERY_THRESHOLD
                        Low battery threshold percentage (default: 30%)
  --critical-low-threshold CRITICAL_LOW_THRESHOLD
                        Critical low battery threshold percentage (default: 10%)
  --debug               Enable debug logging for raw I2C data

Useful journalctl commands for monitoring:
  - Recent battery/voltage logs: journalctl -u x728_ups.service | grep -E "Battery level|Voltage" -m 10
  - Power event logs: journalctl -u x728_ups.service | grep -E "Power Loss|Power Restored|Shutdown" -m 10
  - Critical errors: journalctl -u x728_ups.service -p 0..3 -n 10
  - Debug logs (if --debug enabled): journalctl -u x728_ups.service | grep DEBUG -m 10
  - </code></pre>
  
<br> </br>



waveshare(pizero)UPS HAT-c Monitor with Service Installation

<pre><code>


options:
  -h, --help            show this help message and exit
  --install_as_service  Install as a systemd service
  --addr ADDR           I2C address of INA219 (e.g., 0x43)
  --ntfy-server NTFY_SERVER
                        ntfy server URL
  --ntfy-topic NTFY_TOPIC
                        ntfy topic for notifications
  --power-threshold POWER_THRESHOLD
                        Power threshold for alerts in watts
  --percent-threshold PERCENT_THRESHOLD
                        Battery percentage threshold for alerts
  --battery-capacity BATTERY_CAPACITY
                        Battery capacity in mAh
  --battery-voltage BATTERY_VOLTAGE
                        Battery nominal voltage in volts
  --force-reinstall     Force reinstallation of the service without prompting
</code></pre>


### Lets TEST a NOTIFICATION to your Phone:

firstly you'll need the app on your phone you can grab it from here :
**STEP 1:**


![ntfy](https://play-lh.googleusercontent.com/O9uRWkaFLCzl7wkpeUWFuJfllrvykC6wOCR3sy8sZkrCyIMs-DPv7j7D710QY8VSc7KN=w240-h480-rw)

ANDROID:
        https://play.google.com/store/apps/details?id=io.heckel.ntfy

IOS:
                 https://apps.apple.com/us/app/ntfy/id1625396347


**STEP 2:**

 1. once you have installed ntfy  , 
 2. go in the app click + & set up (or subscribe) a topic name for this test
 3. lets use : **x728_TEST**

**TIP*** if you haven't installed as service yet ***skip to*** ***STEP 3***

> if you have already installed it as a service .it wont run.. this is
> normal.  (The x728_ups service is running checked via ***systemctl
> status x728_ups.service***) it will be  using GPIO resources - and you
> wont be able to run script live in the terminal to test it options .
> We will  Stop the service first with: ***sudo systemctl stop
> x728_ups.service***  then run this command to send out a test ntfy to
> your phone

**STEP 3:**
we use the options :
1/ enable ntfy mode 
2/ tell it we want a test message only 
3/ Topic name {x728_TEST}

command to use :

    ~/pi_ups_monitors/scripts/presto_x728_monitor.py --enable-ntfy --test-ntfy --ntfy-topic x728_TEST


you should receive a alert pretty quick like this :

now you can make you're own topics and install as a service the script (with your own thresholds) will ask you if you want to overwrite the settings this is how we change topics or thresholds any time in the future  -just add the options to the command and it use them

    sudo ~/pi_ups_monitors/scripts/presto_x728_monitor.py --install_as_service --enable-ntfy  --ntfy-topic x728_YOUR_AWESOME_TOPIC --low-battery-threshold 40 

it will show you confirmations (any command options you dont use will just be the default values) :

     [x728-UPS-service] [INFO] New settings: enable-ntfy=True, ntfy-server=https://ntfy.sh, ntfy-topic=x728_YOUR_AWESOME_TOPIC, low-battery-threshold=40%%, critical-low-threshold=10%%

    
it will then update it and reload daemon.. with your new settings

    
    [x728-UPS-service] [INFO] Would you like to reinstall with new settings? (y/n): 
now you can test your Pi by removing of power usb cable from the x728 (or hat-c in pizero's  case)  and you should see an alert warning you its on battery power now .. and how much time left (approx I may tweak this code further)

## *tips:
#### in order to run it on terminal to test values (and its already running as a service) you must stop service first?

 1. `sudo systemctl stop x728_ups.service` 
run you script  as normal in terminal with  your flags options ,then when done testing  

 -  *to bring it back up just redo command but with **start*** 
 . `sudo systemctl start x728_ups.service` 


#### how do i stop the service or uninstall it completely ?

*you can temporarily stop it with command above or use the --uninstall flag (**sudo** needed i believe)*

    sudo ~/pi_ups_monitors/scripts/presto_x728_monitor.py --uninstall

#### if something went wrong or pi UPS crashed how do i check the logs? 
  - Recent battery/voltage logs: 
       -  ***journalctl -u x728_ups.service | grep -E "Battery level|Voltage" -m 10***
  - Power event logs: 
     - ***journalctl -u x728_ups.service | grep -E "Power Loss|Power Restored|Shutdown" -m 10***
  - Critical errors: 
      - ***journalctl -u x728_ups.service -p 0..3 -n 10***
  - Debug logs (if --**debug** enabled): 
    - ***journalctl -u x728_ups.service | grep DEBUG -m 10***
    - and ofcourse the usual dmesg -f daemon     
    - and check your  '/var/logs'   cat /var/logs/....etc 
