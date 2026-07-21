# Linux/Wine Setup for Result Mapping

The `ras2cng map` command uses **RasStoreMapHelper.exe** (bundled with
ras-commander) to generate result rasters. Terrain HDF creation uses
**RasProcess.exe** from HEC-RAS. Both Windows components run under
[Wine](https://www.winehq.org/) on Linux.

Do not substitute `RasProcess.exe StoreAllMaps` for the bundled map helper.
RasProcess does not preserve the required stored-map interpolation/render mode.
The helper sets that mode through RASMapper before generating maps.

This guide covers setting up Wine + RasProcess.exe on Ubuntu Linux.

## Tested Configuration

| Component | Version |
|-----------|---------|
| Linux | Debian 13 (Trixie), isolated Proxmox LXC |
| Wine | 11.0 (winehq-stable) |
| .NET Framework | 4.8 (via winetricks) |
| Python | 3.12 |
| Windows HEC-RAS payload | 7.0.1 |
| Qualification fixture | Muncie p03, EPSG:2965 |

The qualified Muncie WSE, depth, and velocity rasters matched the Windows
HEC-RAS 7.0.1 golden pixel-for-pixel, including dimensions, CRS, transform,
nodata, and source pixel hashes.

## Prerequisites

- Ubuntu 22.04+ or Debian 12+
- x86_64 architecture
- Access to the complete Windows HEC-RAS installation for the exact version
  being qualified
- One writable Wine prefix and one writable project copy per task

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

Build a read-only template prefix once, then copy it to node-local storage for
every task. Never initialize or share one writable prefix concurrently.

Wine needs a one-time initialization to create its prefix. On headless servers,
suppress GUI dialogs:

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

From a Windows machine with HEC-RAS installed, copy the **complete** installation
directory. Do not mix DLLs or executables from different versions. For HEC-RAS
7.0.1, the source is normally:

```
C:\Program Files (x86)\HEC\HEC-RAS\7.0.1\
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
sudo mkdir -p /opt/ras2cng-data/ras701

# Copy the contents, not a nested 7.0.1 directory
scp -r user@windows-host:"C:/Program Files (x86)/HEC/HEC-RAS/7.0.1/." /opt/ras2cng-data/ras701/

# Verify that GDAL, x64, bin64, and bin32 were preserved when present.
```

### Expected directory layout

```
/opt/ras2cng-data/ras701/
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

## Step 5: Reject Unsafe CPU Topology

Run this check inside the scheduler/container namespace that will launch Wine:

```python
import os

reported = int(os.sysconf("SC_NPROCESSORS_ONLN"))
allowed = sorted(os.sched_getaffinity(0))
invalid = [cpu for cpu in allowed if cpu >= reported]
if invalid:
    raise SystemExit(
        f"Unsafe Wine CPU namespace: reported={reported}, "
        f"allowed={allowed}, invalid={invalid}"
    )
```

Wine can report a processor count while returning raw Linux CPU IDs. A sparse
cpuset such as `2,5-7` with a reported count of four can therefore produce CLR
`0x80131506`, access violations, or non-returning RASMapper calls.

Prefer a coherent zero-based visible CPU namespace and apply a CPU-time quota.
If the scheduler cannot provide that topology, pin the complete Wine process
tree to one allowed CPU whose ID is lower than `reported`. `taskset` cannot
renumber CPU IDs. Do not widen the scheduler allocation.

The ras-commander source distribution includes the full JSON preflight at
`.claude/skills/hecras-setup-linux-wine-ras2cng/scripts/headless_wine_preflight.py`.

## Step 6: Verify the Wine Runtime

```bash
# Basic test — should print a usage message:
DISPLAY= WINEDEBUG=-all wine /opt/ras2cng-data/ras701/RasProcess.exe
# Expected: "We really need a usage dialogue once this gets to be more solid."

# CreateTerrain — shows usage for terrain HDF creation:
DISPLAY= WINEDEBUG=-all wine /opt/ras2cng-data/ras701/RasProcess.exe CreateTerrain
# Expected: Usage text showing CreateTerrain arguments

# Verify ras-commander sees the prefix, .NET, RasProcess, and HDF libraries:
python - <<'PY'
from ras_commander import RasProcess

RasProcess.configure_wine(
    wine_prefix="/opt/hecras-prefix-template",
    ras_install_dir="/opt/ras2cng-data/ras701",
)
print(RasProcess.check_wine_environment())
PY
```

## Step 7: Provision TCU State Safely

Never let a generic dialog watchdog click the first button on an unknown modal.
`RasTcu.status()` is read-only. If the operator already accepted the same
installed HEC-RAS version and authorizes reuse, run Windows Python inside the
target Wine prefix and use the donor-based `RasTcu.accept()` flow, or initialize
with `accept_tcu=True`. If no accepted donor state exists, stop and report it.

## Step 8: Install ras2cng

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
  --rasprocess /opt/ras2cng-data/ras701 \
  --map-workers 1 --depth --wse --velocity --fail-fast

# Generate only depth rasters for a specific plan
ras2cng map /path/to/project /output/maps \
  --rasprocess /opt/ras2cng-data/ras701 \
  --map-workers 1 \
  --depth --no-wse --no-velocity \
  --plans p01

# Specify render mode (horizontal, sloping, or slopingPretty)
ras2cng map /path/to/project /output/maps \
  --rasprocess /opt/ras2cng-data/ras701 \
  --map-workers 1 \
  --render-mode sloping

# Custom timeout (default: 3 hours)
ras2cng map /path/to/project /output/maps \
  --rasprocess /opt/ras2cng-data/ras701 \
  --map-workers 1 \
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
  --rasprocess /opt/ras2cng-data/ras701 \
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
    rasprocess_path="/opt/ras2cng-data/ras701",
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

## Prefix and Project Isolation

Keep one active RASMapper helper per Wine prefix. A controlled same-prefix
parallel test stalled, while separate prefixes completed concurrently with
exact golden raster hashes. `ras2cng` therefore uses `--map-workers 1` under
Wine. Scale with scheduler arrays that each receive:

- one copied writable prefix;
- one node-local writable project copy;
- one output directory;
- no shared active HDF files.

## Qualification Before Production

Do not qualify a runner from process exit codes alone. Compare a representative
fixture to the same HEC-RAS version on Windows and record raster CRS,
transform, dimensions, nodata, overlap, values, and pixel hashes. For geometry
work, also record exact cell/face counts, boundary assignments, property-table
completeness, and geometry/terrain fingerprints. Critical integration tests may
not be skipped.

See ras-commander notebook
`examples/511_headless_linux_wine_ras2cng.ipynb` for the complete operational
workflow.

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
| CLR `0x80131506`, `0xc0000005`, or nondeterministic hang | Run the CPU-topology preflight; repair the visible CPU namespace or use the safe single-CPU fallback |
| One of two helpers stalls | They share a writable prefix; use one prefix and project copy per task |
| Map command exits but raster differs | Use RasStoreMapHelper, not `RasProcess.exe StoreAllMaps`; match HEC-RAS version and render mode |
| Mapping times out | Keep `--map-workers 1`, verify the plan HDF, then increase `--timeout` |
| CRS mismatch in terrain merge | ras2cng auto-reprojects mismatched TIFs. Install `pyproj` for best CRS comparison |
| HEC-RAS version warnings | Non-fatal warnings from ras-commander version detection. The pipeline works if `--rasprocess` points to a valid RasProcess.exe |
