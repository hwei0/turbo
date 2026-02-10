# ML Inference Offloading Web Dashboard

> **Note:** For setup and usage instructions, see the main [README.md](../../../README.md). This document provides supplementary technical details about the web dashboard component.

This web dashboard provides real-time visualization of your ML inference offloading system's performance metrics through a modern web interface.

## Features

- **Real-time Updates**: Displays matplotlib figures as they update in your plotting system
- **Multiple Plot Types**: 
  - Bandwidth allocation and utility plots
  - Service status plots (success/failure rates)
  - Service utilization plots (network usage)
- **Responsive Design**: Works on desktop and mobile devices
- **Live Connection Status**: Shows connection status and last update time
- **Auto-refresh**: Automatically updates plots every 2 seconds

## Quick Start

1. **Install Dependencies** (if using `uv`, dependencies are already installed via `uv sync` from the project root):
   ```bash
   # Alternative: using pip
   pip install -r web_requirements.txt
   ```

2. **Start the Web Dashboard**:
   ```bash
   uv run start_web_dashboard.py
   ```
   (or `python start_web_dashboard.py` if using a pip-installed environment)

3. **Open in Browser**:
   Navigate to `http://localhost:5000` in your web browser

## Configuration

The web dashboard automatically loads configuration from your existing YAML config files in this order:
1. `client_config.yaml`
2. `client_config_debug_1cam.yaml`
3. `server_config_gcloud.yaml`

If no config file is found, it uses default settings.

### Manual Configuration

You can specify a custom config file:
```bash
uv run start_web_dashboard.py --config your_config.yaml
```

### Custom Port/Host

```bash
uv run start_web_dashboard.py --port 8080 --host 127.0.0.1
```

## How It Works

1. **Data Source**: The web dashboard connects to the same ZMQ socket that your existing `util/plotting_main.py` uses
2. **Plot Generation**: It runs your existing plotting classes but renders them as PNG images for web display
3. **Real-time Updates**: Uses WebSocket connections to push updated plot images to connected browsers
4. **Thread Safety**: Runs the plotting loop in a separate thread while serving the web interface

## Architecture

```
Your System → ZMQ Messages → Web Dashboard → WebSocket → Browser
                ↓
            Matplotlib Plots → PNG Images → Real-time Display
```

## Troubleshooting

### Dashboard shows "No plots available"
- Check that your main system is sending data to the ZMQ socket
- Verify the ZMQ address in your config matches what your system is using
- Check the console output for connection errors

### Connection issues
- Ensure the port (default 5000) is not blocked by firewall
- Check that no other service is using the same port
- Verify your system is sending data to the correct ZMQ address

### Performance issues
- The dashboard updates every 2 seconds by default
- Large plots or many services may impact performance
- Consider reducing update frequency if needed

## Files

- `web_frontend.py`: Main Flask application and plotting adapter
- `web_config.py`: Configuration loader for existing YAML configs
- `start_web_dashboard.py`: Simple startup script
- `templates/dashboard.html`: Web interface template
- `web_requirements.txt`: Python dependencies

## Integration with Existing System

This web dashboard is designed to work alongside your existing `util/plotting_main.py` without modification. It:
- Uses the same ZMQ socket for data
- Reuses your existing plot classes
- Doesn't interfere with your current matplotlib display
- Can run simultaneously with your existing plotting process

You can run both the original plotting system and the web dashboard at the same time to have both desktop matplotlib windows and web access.