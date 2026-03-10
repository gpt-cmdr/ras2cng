# Linux/Wine Setup for Result Mapping

The `ras2cng map` and `ras2cng terrain` commands use **RasProcess.exe** from HEC-RAS to generate result rasters and terrain HDFs. On Linux, RasProcess.exe runs under [Wine](https://www.winehq.org/).

This guide covers setting up Wine + RasProcess.exe on Ubuntu/Debian Linux, including LXC containers (Proxmox).

## Prerequisites

- Ubuntu 22.04+ or Debian 12+ (tested on Ubuntu 24.04 LTS)
- x86_64 architecture
- Access to a Windows HEC-RAS installation (for RasProcess.exe and DLLs)

## Step 1: Install Wine

```bash
# Enable 32-bit architecture (required for Wine)
sudo dpkg --add-architecture i386
sudo apt-get update

# Install dependencies
sudo apt-get install -y wget gnupg2 software-properties-common

# Add WineHQ repository
sudo wget -qO- https://dl.winehq.org/wine-builds/winehq.key | sudo apt-key add -
sudo add-apt-repository 'deb https://dl.winehq.org/wine-builds/ubuntu/ noble main'
sudo apt-get update

# Install Wine stable
sudo apt-get install -y --install-recommends winehq-stable winetricks
```

Verify:
```bash
wine --version
# wine-11.0 (or newer)
```

## Step 2: Initialize Wine Prefix

Wine needs a one-time initialization to create its `~/.wine/` directory:

```bash
DISPLAY= WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" wineboot --init
```

!!! note "Headless environments"
    The `DISPLAY=` (empty) and `WINEDLLOVERRIDES="mscoree,mshtml="` flags are critical for headless servers. Without them, Wine tries to display GUI dialogs and may hang indefinitely.

## Step 3: Install .NET Framework 4.8

RasProcess.exe is a .NET Framework application:

```bash
DISPLAY= WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" winetricks -q dotnet48
```

This downloads and installs .NET Framework 4.8 inside the Wine prefix. It takes several minutes.

## Step 4: Copy RasProcess.exe and Dependencies

From a Windows machine with HEC-RAS installed, copy the contents of the HEC-RAS directory:

```
C:\Program Files (x86)\HEC\HEC-RAS\6.6\
```

You need at minimum:
- All `*.dll` and `*.exe` files in the root directory
- The entire `GDAL/` directory
- The entire `bin32/` directory (32-bit native DLLs including `hdf5.dll`)
- The entire `bin64/` directory (64-bit native DLLs including `hdf5.dll`)
- The entire `x64/` directory (64-bit HDF5 native libraries)

!!! warning "Missing native DLLs"
    If you only copy the managed `.dll` files without the `bin64/` and `x64/` directories, RasProcess will crash with "The type initializer for 'HDF.PInvoke.H5F' threw an exception." These directories contain the native HDF5 C libraries that the .NET wrapper (HDF.PInvoke.dll) loads at runtime.

Place them on your Linux machine, for example at `/opt/ras2cng-data/ras66/`.

### Using scp or rsync

```bash
# From the Windows machine (Git Bash):
scp -r "/c/Program Files (x86)/HEC/HEC-RAS/6.6/"*.{dll,exe} user@linux-host:/opt/ras2cng-data/ras66/
scp -r "/c/Program Files (x86)/HEC/HEC-RAS/6.6/GDAL" user@linux-host:/opt/ras2cng-data/ras66/
```

## Step 5: Verify

```bash
# Should print a usage message:
DISPLAY= WINEDEBUG=-all wine /opt/ras2cng-data/ras66/RasProcess.exe
```

Expected output: `We really need a usage dialogue once this gets to be more solid.`

```bash
# CreateTerrain shows usage:
DISPLAY= WINEDEBUG=-all wine /opt/ras2cng-data/ras66/RasProcess.exe CreateTerrain
```

```bash
# StoreAllMaps (no project, just confirms the command is recognized):
DISPLAY= WINEDEBUG=-all wine /opt/ras2cng-data/ras66/RasProcess.exe StoreAllMaps
```

## Usage with ras2cng

### CLI

```bash
# Generate result maps
ras2cng map /path/to/project /output/dir \
  --rasprocess /opt/ras2cng-data/ras66/RasProcess.exe \
  --depth --wse --velocity

# Consolidate terrain
ras2cng terrain /path/to/project /output/dir \
  --ras-version 6.6
```

### Python API

```python
from ras_commander import RasProcess
from ras2cng.mapping import generate_result_maps

# Configure Wine with explicit RasProcess path
RasProcess.configure_wine("/opt/ras2cng-data/ras66/RasProcess.exe")

# Generate maps
results = generate_result_maps(
    "/path/to/project",
    "/output/dir",
    rasprocess_path="/opt/ras2cng-data/ras66/RasProcess.exe",
    depth=True, wse=True, velocity=True,
)
```

## LXC Container Notes (Proxmox)

If running inside an LXC container, Wine may fail with memory mapping errors. Fix this on the **Proxmox host**:

```bash
# On the Proxmox host (not inside the container):
echo 0 > /proc/sys/vm/mmap_min_addr

# Make persistent:
echo "vm.mmap_min_addr = 0" >> /etc/sysctl.conf
sysctl -p
```

This is required for both privileged and unprivileged containers.

## Troubleshooting

| Symptom | Solution |
|---------|----------|
| `wineboot` hangs | Set `DISPLAY=` and `WINEDLLOVERRIDES="mscoree,mshtml="` |
| "Could not load assembly 'RasMapperLib'" | Copy **all** DLLs from HEC-RAS directory |
| Wine crashes with mmap error | Set `vm.mmap_min_addr=0` on the host |
| .NET install fails | Ensure both `wine-stable-i386` and `wine-stable-amd64` are installed |
| `timeout` during wineboot | Use `timeout 60 wineboot --init` to prevent indefinite hangs |

## Tested Versions

| Component | Version | Status |
|-----------|---------|--------|
| Ubuntu | 24.04 LTS | Working |
| Wine | 11.0 (stable) | Working |
| .NET Framework | 4.8 (via winetricks) | Working |
| RasProcess.exe | HEC-RAS 6.6 | Working |
| Container | Proxmox LXC (unprivileged) | Working (with mmap fix) |
