# Linux/Wine Setup for Result Mapping

The `ras2cng map` and `ras2cng terrain` commands use **RasStoreMapHelper.exe** (bundled with ras-commander) to generate result rasters, and **RasProcess.exe** from HEC-RAS for terrain HDFs. Both run under [Wine](https://www.winehq.org/) on Linux. The helper sets the correct water surface render mode via .NET reflection before generating maps, producing pixel-perfect output.

This guide covers setting up Wine + RasProcess.exe on Ubuntu Linux.

## Tested Configuration

| Component | Version |
|-----------|---------|
| Ubuntu | 24.04 LTS (Noble Numbat) |
| Wine | 11.0 (winehq-stable) |
| .NET Framework | 4.8 (via winetricks) |
| Python | 3.12 |
| RasProcess.exe | HEC-RAS 6.6 |

## Prerequisites

- Ubuntu 22.04+ or Debian 12+
- x86_64 architecture
- Access to a Windows HEC-RAS 6.x installation (for RasProcess.exe and its dependencies)

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

## Step 2: Initialize Wine Prefix

Wine needs a one-time initialization to create its `~/.wine/` directory. On headless servers, suppress GUI dialogs:

```bash
DISPLAY= WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" wineboot --init
```

On a desktop Ubuntu installation with a display server, you can simply run:
```bash
wineboot --init
```

!!! note "Headless flags explained"
    - `DISPLAY=` (empty) — prevents Wine from trying to open X11 windows
    - `WINEDEBUG=-all` — suppresses verbose debug output
    - `WINEDLLOVERRIDES="mscoree,mshtml="` — skips Mono/Gecko install prompts that would hang

If `wineboot` hangs, use a timeout:
```bash
timeout 120 env DISPLAY= WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" wineboot --init
```

## Step 3: Install .NET Framework 4.8

RasProcess.exe is a .NET Framework 4.x application:

```bash
DISPLAY= WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" winetricks -q dotnet48
```

This downloads and installs .NET Framework 4.8 inside the Wine prefix. It takes several minutes and may produce warning messages — these are generally safe to ignore as long as the process completes.

## Step 4: Copy HEC-RAS Files

From a Windows machine with HEC-RAS installed, copy the contents of the HEC-RAS installation directory to your Linux machine. For HEC-RAS 6.6, the source directory is:

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
# Create destination directory
sudo mkdir -p /opt/ras2cng-data/ras66

# Copy root-level files
scp user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/6.6/*.dll" /opt/ras2cng-data/ras66/
scp user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/6.6/*.exe" /opt/ras2cng-data/ras66/

# Copy required subdirectories
scp -r user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/6.6/GDAL" /opt/ras2cng-data/ras66/
scp -r user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/6.6/bin32" /opt/ras2cng-data/ras66/
scp -r user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/6.6/bin64" /opt/ras2cng-data/ras66/
scp -r user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/6.6/x64" /opt/ras2cng-data/ras66/
```

### Expected directory layout

```
/opt/ras2cng-data/ras66/
├── RasProcess.exe
├── Ras.exe
├── RasMapperLib.dll
├── HDF.PInvoke.dll
├── ... (all other .dll/.exe files)
├── GDAL/
├── bin32/
├── bin64/
└── x64/
```

## Step 5: Verify RasProcess.exe

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

## Step 6: Install ras2cng

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

## Usage

### Generate result rasters

```bash
# Generate depth, WSE, and velocity rasters for all plans
ras2cng map /path/to/project /output/maps \
  --rasprocess /opt/ras2cng-data/ras66/RasProcess.exe \
  --depth --wse --velocity

# Generate only depth rasters for a specific plan
ras2cng map /path/to/project /output/maps \
  --rasprocess /opt/ras2cng-data/ras66/RasProcess.exe \
  --depth --no-wse --no-velocity \
  --plans p01

# Specify render mode (horizontal, sloping, or slopingPretty)
ras2cng map /path/to/project /output/maps \
  --rasprocess /opt/ras2cng-data/ras66/RasProcess.exe \
  --render-mode sloping

# Custom timeout (default: 3 hours)
ras2cng map /path/to/project /output/maps \
  --rasprocess /opt/ras2cng-data/ras66/RasProcess.exe \
  --timeout 7200
```

### Consolidate terrain

```bash
# Merge all terrain TIFFs into a single file
ras2cng terrain /path/to/project /output/terrain \
  --ras-version 6.6 \
  --tiff-only

# With downsampling (half resolution)
ras2cng terrain /path/to/project /output/terrain \
  --ras-version 6.6 \
  --downsample 2.0 \
  --tiff-only
```

### Full-project archive with mapping

```bash
ras2cng archive /path/to/project /output/archive \
  --results \
  --terrain \
  --map \
  --consolidate-terrain \
  --rasprocess /opt/ras2cng-data/ras66/RasProcess.exe \
  --render-mode horizontal
```

### Python API

```python
from ras2cng.mapping import generate_result_maps
from ras2cng.terrain import consolidate_terrain, discover_terrains

# Generate result rasters
results = generate_result_maps(
    "/path/to/project",
    "/output/maps",
    rasprocess_path="/opt/ras2cng-data/ras66/RasProcess.exe",
    depth=True, wse=True, velocity=True,
    render_mode="horizontal",  # or "sloping", "slopingPretty"
)

# Discover terrains in a project
terrains = discover_terrains("/path/to/project")
for t in terrains:
    print(f"{t.name}: {len(t.tif_files)} TIFs, HDF exists: {t.hdf_exists}")

# Consolidate terrain TIFFs
merged = consolidate_terrain(
    "/path/to/project",
    "/output/terrain",
    terrain_name="Consolidated",
    create_hdf=False,
)
```

!!! note "configure_wine expects a directory"
    When using the Python API directly, `RasProcess.configure_wine()` takes `ras_install_dir=` (the directory containing `RasProcess.exe`), not the full path to the executable. The CLI `--rasprocess` flag accepts either and extracts the parent directory automatically.

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
| Wine crashes with mmap error in LXC | Set `vm.mmap_min_addr=0` on the container host |
| .NET install fails | Ensure both `wine-stable-i386` and `wine-stable-amd64` packages are installed |
| StoreAllMaps times out | Increase `--timeout` (default: 10800s). Verify the plan HDF has valid results |
| CRS mismatch in terrain merge | ras2cng auto-reprojects mismatched TIFs. Install `pyproj` for best CRS comparison |
| HEC-RAS version warnings | Non-fatal warnings from ras-commander version detection. The pipeline works if `--rasprocess` points to a valid RasProcess.exe |
