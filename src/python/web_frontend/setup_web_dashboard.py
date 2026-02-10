#!/usr/bin/env python3
"""Setup script for the web dashboard.

Installs Python dependencies from web_requirements.txt, validates that all required
modules can be imported (flask, matplotlib, zmq, etc.), and checks for configuration
files. Run this before starting the dashboard for the first time.
"""

import subprocess
import sys
import os


def install_requirements():
    """Install required packages for the web dashboard"""
    print("Installing web dashboard dependencies...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", "web_requirements.txt"]
        )
        print("✓ Dependencies installed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"✗ Failed to install dependencies: {e}")
        return False


def test_imports():
    """Test that all required modules can be imported"""
    print("Testing imports...")
    required_modules = [
        "flask",
        "flask_socketio",
        "matplotlib",
        "pydantic",
        "yaml",
        "zmq",
    ]

    for module in required_modules:
        try:
            __import__(module)
            print(f"✓ {module}")
        except ImportError as e:
            print(f"✗ {module}: {e}")
            return False

    return True


def check_config():
    """Check if configuration files exist"""
    print("Checking configuration files...")
    config_files = [
        "client_config.yaml",
        "client_config_debug_1cam.yaml",
        "server_config_gcloud.yaml",
    ]

    found_config = False
    for config_file in config_files:
        if os.path.exists(config_file):
            print(f"✓ Found config file: {config_file}")
            found_config = True
        else:
            print(f"- Config file not found: {config_file}")

    if not found_config:
        print("⚠ No config files found, will use default configuration")

    return True


def main():
    """Main setup function"""
    print("=" * 60)
    print("ML Inference Offloading Web Dashboard Setup")
    print("=" * 60)

    success = True

    # Install dependencies
    if not install_requirements():
        success = False

    print()

    # Test imports
    if not test_imports():
        success = False

    print()

    # Check config
    if not check_config():
        success = False

    print()
    print("=" * 60)

    if success:
        print("✓ Setup completed successfully!")
        print()
        print("To start the web dashboard:")
        print("  python start_web_dashboard.py")
        print()
        print("Then open your browser to: http://localhost:5000")
    else:
        print("✗ Setup failed. Please check the errors above.")
        return 1

    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
