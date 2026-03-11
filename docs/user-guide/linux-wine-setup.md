# Linux/Wine Setup for Result Mapping

The `ras2cng map` and `ras2cng terrain` commands use **RasProcess.exe** from HEC-RAS to generate result rasters and terrain HDFs. On Linux, RasProcess.exe runs under [Wine](https://www.winehq.org/).

This guide covers setting up Wine + RasProcess.exe on Ubuntu/Debian Linux, including LXC containers (Proxmox). All steps have been validated on a production Proxmox VE 9.x environment with Ubuntu 24.04 LTS containers.

## Tested Configuration

| Component | Version | Status |
|-----------|---------|--------|
| Host OS | Proxmox VE 9.0.6 (kernel 6.14.11-1-pve) | Working |
| Container OS | Ubuntu 24.04 LTS (Noble Numbat) | Working |
| Container Type | LXC (unprivileged) | Working (with mmap fix) |
| Wine | 11.0 (winehq-stable) | Working |
| .NET Framework | 4.8 (via winetricks) | Working |
| Python | 3.12 | Working |
| RasProcess.exe | HEC-RAS 6.6 | Working |

## Prerequisites

- Ubuntu 22.04+ or Debian 12+ (tested on Ubuntu 24.04 LTS)
- x86_64 architecture
- Access to a Windows HEC-RAS 6.x installation (for RasProcess.exe and DLLs)

## Step 1: Install Wine

```bash
# Enable 32-bit architecture (required for Wine)
sudo dpkg --add-architecture i386
sudo apt-get update

# Install dependencies
sudo apt-get install -y wget gnupg2 software-properties-common

# Add WineHQ repository (Ubuntu 24.04 / Noble)
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

## Step 2: LXC Container Kernel Fix (Proxmox Only)

If running inside an LXC container, Wine requires a kernel parameter change on the **Proxmox host** (not inside the container):

```bash
# On the Proxmox host:
echo 0 > /proc/sys/vm/mmap_min_addr

# Make persistent across reboots:
echo "vm.mmap_min_addr = 0" >> /etc/sysctl.conf
sysctl -p
```

!!! warning "Required for both privileged and unprivileged containers"
    Without this fix, Wine will crash with memory mapping errors when attempting to run any Windows application. This setting allows Wine's Windows-compatible memory allocator to map pages at low virtual addresses.

## Step 3: Initialize Wine Prefix (Headless)

Wine needs a one-time initialization to create its `~/.wine/` directory. In headless environments (servers, containers), you must suppress GUI dialogs:

```bash
DISPLAY= WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" wineboot --init
```

!!! note "Why these flags matter"
    - `DISPLAY=` (empty) — prevents Wine from trying to open X11 windows
    - `WINEDEBUG=-all` — suppresses verbose debug output
    - `WINEDLLOVERRIDES="mscoree,mshtml="` — skips Mono/Gecko install prompts that would hang

If `wineboot` hangs despite these flags, use a timeout:
```bash
timeout 120 env DISPLAY= WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" wineboot --init
```

## Step 4: Install .NET Framework 4.8

RasProcess.exe is a .NET Framework 4.x application:

```bash
DISPLAY= WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" winetricks -q dotnet48
```

This downloads and installs .NET Framework 4.8 inside the Wine prefix. It takes several minutes and may produce warning messages — these are generally safe to ignore as long as the process completes.

## Step 5: Copy RasProcess.exe and Dependencies

From a Windows machine with HEC-RAS installed, copy the contents of the HEC-RAS directory:

```
C:\Program Files (x86)\HEC\HEC-RAS\6.6\
```

### Required files and directories

| Path | Contents | Why needed |
|------|----------|-----------|
| `*.dll` | Managed .NET assemblies (RasMapperLib.dll, etc.) | Core application dependencies |
| `*.exe` | RasProcess.exe, Ras.exe, etc. | The executables |
| `GDAL/` | GDAL native binaries | Raster I/O and spatial operations |
| `bin32/` | 32-bit native DLLs (hdf5.dll, szip.dll, zlib.dll) | HDF5 file access (32-bit) |
| `bin64/` | 64-bit native DLLs (hdf5.dll, szip.dll, zlib.dll) | HDF5 file access (64-bit) |
| `x64/` | 64-bit HDF5 native libraries | HDF5 PInvoke bindings |

!!! danger "Missing native DLLs cause silent crashes"
    If you only copy the managed `.dll` files without the `bin32/`, `bin64/`, and `x64/` directories, RasProcess will crash with:

    ```
    The type initializer for 'HDF.PInvoke.H5F' threw an exception.
    ```

    These directories contain the native HDF5 C libraries that the .NET wrapper (`HDF.PInvoke.dll`) loads at runtime via P/Invoke.

### Copying via SCP

```bash
# From a machine with SSH access to both Windows and Linux:
# Copy all root-level files
scp user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/6.6/*.dll" /opt/ras2cng-data/ras66/
scp user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/6.6/*.exe" /opt/ras2cng-data/ras66/

# Copy required subdirectories
scp -r user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/6.6/GDAL" /opt/ras2cng-data/ras66/
scp -r user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/6.6/bin32" /opt/ras2cng-data/ras66/
scp -r user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/6.6/bin64" /opt/ras2cng-data/ras66/
scp -r user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/6.6/x64" /opt/ras2cng-data/ras66/
```

### Recommended directory layout

```
/opt/ras2cng-data/
├── ras66/                    # HEC-RAS 6.6 binaries
│   ├── RasProcess.exe
│   ├── Ras.exe
│   ├── RasMapperLib.dll
│   ├── HDF.PInvoke.dll
│   ├── ... (all other .dll/.exe files)
│   ├── GDAL/
│   ├── bin32/
│   ├── bin64/
│   └── x64/
└── projects/                 # HEC-RAS project files
    └── MyProject/
        ├── MyProject.prj
        ├── MyProject.g01.hdf
        ├── MyProject.p01.hdf
        ├── MyProject.rasmap
        └── Terrain/
            ├── Terrain50.hdf
            └── Terrain50.tif
```

## Step 6: Verify RasProcess.exe

```bash
# Basic test — should print a usage message:
DISPLAY= WINEDEBUG=-all wine /opt/ras2cng-data/ras66/RasProcess.exe
# Expected: "We really need a usage dialogue once this gets to be more solid."

# CreateTerrain — shows usage for terrain HDF creation:
DISPLAY= WINEDEBUG=-all wine /opt/ras2cng-data/ras66/RasProcess.exe CreateTerrain
# Expected: Usage text showing CreateTerrain arguments

# StoreAllMaps — confirms the mapping command is recognized:
DISPLAY= WINEDEBUG=-all wine /opt/ras2cng-data/ras66/RasProcess.exe StoreAllMaps
# Expected: "RasMapFilename '' does not exist." (normal — no project provided)
```

## Step 7: Install ras2cng

```bash
# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install ras2cng
git clone https://github.com/gpt-cmdr/ras2cng.git
cd ras2cng
uv sync --all-extras

# Verify
uv run ras2cng --help
uv run pytest tests/ -v
```

## Usage with ras2cng

### CLI: Generate result rasters

```bash
# Generate depth, WSE, and velocity rasters for all plans
ras2cng map /opt/ras2cng-data/projects/MyProject /output/maps \
  --rasprocess /opt/ras2cng-data/ras66/RasProcess.exe \
  --depth --wse --velocity

# Generate only depth rasters for a specific plan
ras2cng map /opt/ras2cng-data/projects/MyProject /output/maps \
  --rasprocess /opt/ras2cng-data/ras66/RasProcess.exe \
  --depth --no-wse --no-velocity \
  --plans p01

# Custom timeout (default: 3 hours)
ras2cng map /opt/ras2cng-data/projects/MyProject /output/maps \
  --rasprocess /opt/ras2cng-data/ras66/RasProcess.exe \
  --timeout 7200
```

### CLI: Consolidate terrain

```bash
# Merge all terrain TIFFs into a single file
ras2cng terrain /opt/ras2cng-data/projects/MyProject /output/terrain \
  --ras-version 6.6 \
  --tiff-only

# With downsampling
ras2cng terrain /opt/ras2cng-data/projects/MyProject /output/terrain \
  --ras-version 6.6 \
  --downsample 2.0 \
  --tiff-only
```

### Python API

```python
from ras2cng.mapping import generate_result_maps
from ras2cng.terrain import consolidate_terrain, discover_terrains

# Generate result rasters
results = generate_result_maps(
    "/opt/ras2cng-data/projects/MyProject",
    "/output/maps",
    rasprocess_path="/opt/ras2cng-data/ras66/RasProcess.exe",
    depth=True,
    wse=True,
    velocity=True,
)

# Discover terrains in a project
terrains = discover_terrains("/opt/ras2cng-data/projects/MyProject")
for t in terrains:
    print(f"{t.name}: {len(t.tif_files)} TIFs, HDF exists: {t.hdf_exists}")

# Consolidate terrain TIFFs
merged = consolidate_terrain(
    "/opt/ras2cng-data/projects/MyProject",
    "/output/terrain",
    terrain_name="Consolidated",
    create_hdf=False,  # TIFF only, skip HDF creation
)
```

### Python API: configure_wine directly

```python
from ras_commander import RasProcess

# Point ras-commander at the Wine-based RasProcess installation
RasProcess.configure_wine(ras_install_dir="/opt/ras2cng-data/ras66")
```

!!! note "configure_wine expects a directory"
    `configure_wine()` takes `ras_install_dir=` (the directory containing `RasProcess.exe`), not the full path to the executable. When you pass `--rasprocess /path/to/RasProcess.exe` on the CLI, ras2cng automatically extracts the parent directory.

## Full-Project Archive with Mapping

The `archive` command can invoke mapping and terrain consolidation as part of a full-project archive:

```bash
ras2cng archive /opt/ras2cng-data/projects/MyProject /output/archive \
  --results \
  --terrain \
  --map \
  --consolidate-terrain \
  --rasprocess /opt/ras2cng-data/ras66/RasProcess.exe
```

## Timeout Considerations

RasProcess.exe mapping operations can be slow under Wine, especially for large models. The default timeout is **3 hours (10800 seconds)** per plan. Adjust with `--timeout`:

```bash
# 6 hours for very large models
ras2cng map /path/to/project /output --timeout 21600

# 30 minutes for quick test runs
ras2cng map /path/to/project /output --timeout 1800
```

## Troubleshooting

| Symptom | Solution |
|---------|----------|
| `wineboot` hangs | Set `DISPLAY=` and `WINEDLLOVERRIDES="mscoree,mshtml="` |
| "Could not load assembly 'RasMapperLib'" | Copy **all** DLLs from HEC-RAS directory, not just RasProcess.exe |
| "HDF.PInvoke.H5F threw an exception" | Copy `bin32/`, `bin64/`, and `x64/` directories from HEC-RAS install |
| Wine crashes with mmap error | Set `vm.mmap_min_addr=0` on the Proxmox host |
| .NET install fails | Ensure both `wine-stable-i386` and `wine-stable-amd64` are installed |
| `timeout` during `wineboot` | Use `timeout 120 env DISPLAY= ... wineboot --init` |
| StoreAllMaps times out | Increase `--timeout` (default: 10800s). Check that the plan HDF has valid results |
| CRS mismatch in terrain merge | ras2cng auto-reprojects mismatched TIFs. Install `pyproj` for best CRS comparison |
| HEC-RAS version errors | These are non-fatal warnings from ras-commander version detection. The pipeline still works if RasProcess.exe is correctly pointed |

## Proxmox LXC Container Setup

For a dedicated ras2cng container on Proxmox:

### Create the container

```bash
# On the Proxmox host:
pct create 101 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname ras2cng-wine \
  --memory 4096 \
  --swap 512 \
  --cores 4 \
  --rootfs local-lvm:32 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1

# Set mmap_min_addr on host
echo 0 > /proc/sys/vm/mmap_min_addr
echo "vm.mmap_min_addr = 0" >> /etc/sysctl.conf

# Start the container
pct start 101
```

### Inside the container

Follow Steps 1-7 above. All commands run as root inside the container (no `sudo` needed).

### Running commands from the host

```bash
# Execute commands inside the container from the Proxmox host:
pct exec 101 -- bash -c 'cd /root/ras2cng && uv run ras2cng map ...'

# Or via SSH (if configured):
ssh root@<container-ip> 'cd /root/ras2cng && uv run ras2cng map ...'
```

!!! tip "Proxmox API note"
    Proxmox 9.x may return HTTP 596 errors for authenticated REST API requests due to internal SSL renegotiation issues. Use `pct exec` via SSH instead of the REST API for container management.
