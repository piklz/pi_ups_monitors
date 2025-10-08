#!/bin/bash

# File: docker/build.sh
# Purpose: Build and deploy the Presto X728 UPS Monitor Docker container
# Version: 1.0.0    
# Author: Piklz
# License: MIT
# 2025-06-20
# Description:
# This script automates the process of building and deploying a Docker container for the Presto X728 UPS Monitor.  
# It is designed  to run on a Raspberry Pi and includes checks for necessary dependencies, user group memberships, and hardware features.
#   
# Usage:
#       ./build.sh [all|deps|build|deploy|test|clean]   
# 
# Dependencies:
#   - Docker, Docker Compose, I2C tools, GPIO access ,flask, python
#  
# 
# Notes:
#   - Ensure I2C and GPIO are enabled on the Raspberry Pi.
#   - Add the current user to the 'i2c', 'gpio', and 'docker' groups for proper access.
#   - After adding to groups, a logout/login may be required for changes to take effect.
#   - The script creates necessary directories for configuration and data persistence.
#   - The Docker container is built with appropriate tags and started with necessary privileges.
#   - Basic tests are run to verify I2C detection and web interface accessibility.
#   - The script provides useful commands for managing the container and accessing logs.
#   - Cleanup option is available to stop and remove the container and images.  

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
IMAGE_NAME="presto_x728"
VERSION="1.0.0"
CONTAINER_NAME="presto_x728"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Presto X728 UPS Monitor - Build & Deploy v${VERSION}${NC}"
echo -e "${BLUE}========================================${NC}"

# Check if running on Raspberry Pi
if ! grep -q "Raspberry Pi" /proc/device-tree/model 2>/dev/null; then
    echo -e "${YELLOW}Warning: Not running on Raspberry Pi. Hardware features may not work.${NC}"
fi

# Function to check dependencies
check_dependencies() {
    echo -e "\n${BLUE}Checking dependencies...${NC}"
    
    if ! command -v docker &> /dev/null; then
        echo -e "${RED}Error: Docker is not installed${NC}"
        exit 1
    fi
    
    if ! command -v docker-compose &> /dev/null; then
        echo -e "${YELLOW}Warning: docker-compose not found, using 'docker compose'${NC}"
        COMPOSE_CMD="docker compose"
    else
        COMPOSE_CMD="docker-compose"
    fi
    
    # Check if I2C is enabled
    if [ ! -e /dev/i2c-1 ]; then
        echo -e "${YELLOW}Warning: /dev/i2c-1 not found. Enable I2C with 'sudo raspi-config'${NC}"
    fi
    
    # Check if GPIO is accessible
    if [ ! -e /dev/gpiochip0 ] && [ ! -e /dev/gpiochip4 ]; then
        echo -e "${YELLOW}Warning: GPIO chips not found. Some features may not work.${NC}"
    fi
    
    echo -e "${GREEN}✓ Dependencies checked${NC}"
}

# Function to create necessary directories
create_directories() {
    echo -e "\n${BLUE}Creating directories...${NC}"
    
    mkdir -p volumes/presto_x728/config
    chmod 755 volumes/presto_x728/config
    
    echo -e "${GREEN}✓ Directories created${NC}"
}

# Function to check user groups
check_groups() {
    echo -e "\n${BLUE}Checking user groups...${NC}"
    
    CURRENT_USER=$(whoami)
    
    if ! groups $CURRENT_USER | grep -q "i2c"; then
        echo -e "${YELLOW}Warning: User not in 'i2c' group. Add with: sudo usermod -aG i2c $CURRENT_USER${NC}"
    fi
    
    if ! groups $CURRENT_USER | grep -q "gpio"; then
        echo -e "${YELLOW}Warning: User not in 'gpio' group. Add with: sudo usermod -aG gpio $CURRENT_USER${NC}"
    fi
    
    if ! groups $CURRENT_USER | grep -q "docker"; then
        echo -e "${YELLOW}Warning: User not in 'docker' group. Add with: sudo usermod -aG docker $CURRENT_USER${NC}"
        echo -e "${YELLOW}You may need to log out and back in for group changes to take effect.${NC}"
    fi
}

# Function to build Docker image
build_image() {
    echo -e "\n${BLUE}Building Docker image...${NC}"
    
    docker build -t ${IMAGE_NAME}:${VERSION} -t ${IMAGE_NAME}:latest .
    
    echo -e "${GREEN}✓ Docker image built successfully${NC}"
    
    # Show image size
    IMAGE_SIZE=$(docker images ${IMAGE_NAME}:latest --format "{{.Size}}")
    echo -e "${GREEN}Image size: ${IMAGE_SIZE}${NC}"
}

# Function to stop existing container
stop_container() {
    echo -e "\n${BLUE}Stopping existing container...${NC}"
    
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        docker stop ${CONTAINER_NAME} 2>/dev/null || true
        docker rm ${CONTAINER_NAME} 2>/dev/null || true
        echo -e "${GREEN}✓ Existing container removed${NC}"
    else
        echo -e "${YELLOW}No existing container found${NC}"
    fi
}

# Function to start container
start_container() {
    echo -e "\n${BLUE}Starting container...${NC}"
    
    ${COMPOSE_CMD} up -d
    
    echo -e "${GREEN}✓ Container started${NC}"
    
    # Wait for container to be healthy
    echo -e "${BLUE}Waiting for container to be healthy...${NC}"
    sleep 5
    
    # Check container status
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        echo -e "${GREEN}✓ Container is running${NC}"
        
        # Show container logs
        echo -e "\n${BLUE}Recent container logs:${NC}"
        docker logs ${CONTAINER_NAME} --tail 20
    else
        echo -e "${RED}Error: Container failed to start${NC}"
        docker logs ${CONTAINER_NAME}
        exit 1
    fi
}

# Function to show access information
show_info() {
    echo -e "\n${GREEN}========================================${NC}"
    echo -e "${GREEN}PRESTO x728 Deployment Complete!${NC}"
    echo -e "${GREEN}========================================${NC}"
    
    # Get IP addresses
    LOCAL_IP=$(hostname -I | awk '{print $1}')
    
    echo -e "\n${BLUE}Access the web interface at:${NC}"
    echo -e "  ${GREEN}http://localhost:5000${NC}"
    echo -e "  ${GREEN}http://${LOCAL_IP}:5000${NC}"
    
    echo -e "\n${BLUE}Useful commands:${NC}"
    echo -e "  View logs:      ${YELLOW}docker logs -f ${CONTAINER_NAME}${NC}"
    echo -e "  Stop:           ${YELLOW}${COMPOSE_CMD} down${NC}"
    echo -e "  Restart:        ${YELLOW}${COMPOSE_CMD} restart${NC}"
    echo -e "  Shell access:   ${YELLOW}docker exec -it ${CONTAINER_NAME} sh${NC}"
    echo -e "  View config:    ${YELLOW}cat volumes/presto_x728/config/config.json${NC}"
    
    echo -e "\n${BLUE}Configuration directory:${NC}"
    echo -e "  ${GREEN}$(pwd)/volumes/presto_x728/config/${NC}"
    
    echo -e "\n${YELLOW}Note: Ensure I2C and GPIO are enabled in raspi-config${NC}"
    echo -e "${YELLOW}If shutdown/reboot don't work, ensure container has proper privileges${NC}"
}

# Function to run tests
run_tests() {
    echo -e "\n${BLUE}Running basic tests...${NC}"
    
    # Test I2C detection
    if [ -e /dev/i2c-1 ]; then
        echo -e "${BLUE}Testing I2C bus...${NC}"
        if command -v i2cdetect &> /dev/null; then
            sudo i2cdetect -y 1 | grep -E "36|3b|4b" && echo -e "${GREEN}✓ X728 detected on I2C${NC}" || echo -e "${YELLOW}⚠ X728 not detected${NC}"
        fi
    fi
    
    # Test web interface
    echo -e "${BLUE}Testing web interface...${NC}"
    sleep 2
    if curl -s http://localhost:5000 > /dev/null; then
        echo -e "${GREEN}✓ Web interface is accessible${NC}"
    else
        echo -e "${RED}✗ Web interface not accessible${NC}"
    fi
}

# Main execution
main() {
    case "${1:-all}" in
        deps)
            check_dependencies
            check_groups
            ;;
        build)
            check_dependencies
            build_image
            ;;
        deploy)
            create_directories
            stop_container
            start_container
            show_info
            ;;
        test)
            run_tests
            ;;
        clean)
            echo -e "${BLUE}Cleaning up...${NC}"
            echo -e "running: ${COMPOSE_CMD} down"
            #${COMPOSE_CMD} down now owkring yet
            docker compose down  
            docker rmi ${IMAGE_NAME}:latest ${IMAGE_NAME}:${VERSION} 2>/dev/null || true
            echo -e "${GREEN}✓ Cleanup complete${NC}"
            ;;
        all)
            check_dependencies
            check_groups
            create_directories
            build_image
            stop_container
            start_container
            run_tests
            show_info
            ;;
        *)
            echo "Usage: $0 {all|deps|build|deploy|test|clean}"
            echo ""
            echo "  all     - Full build and deployment (default)"
            echo "  deps    - Check dependencies and groups"
            echo "  build   - Build Docker image only"
            echo "  deploy  - Deploy container only"
            echo "  test    - Run basic tests"
            echo "  clean   - Stop and remove container/images"
            exit 1
            ;;
    esac
}

# Run main function
main "$@"
