# laser-lock

Single-channel laser frequency monitor and lock using a HighFinesse WS6-200 wavemeter.

Collects frequency data at the maximum hardware measurement rate (~1.8 kHz with a single
channel active) and reports it live in the terminal.  A PID feedback controller will be
added in a future phase.

## Requirements

- HighFinesse WS6-200 wavemeter connected via USB (Windows only for live hardware)
- `wlmData.dll` installed (placed at `C:\Windows\System32\wlmData.dll` by the HighFinesse
  software installer)
- Python 3.12 via conda (see setup below)
- **No third-party Python packages required** — pure standard library

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/sophiehhli/laser-lock.git
cd laser-lock
```

### 2. Create the conda environment

Works on both macOS (simulation) and Windows (live hardware):

```bash
conda env create -f environment.yml
conda activate laser-lock
```

To update an existing environment after a `git pull`:

```bash
conda env update -f environment.yml --prune
```

## Usage

### Live (Windows, wavemeter connected)

```bash
python monitor.py --channel 1 --setpoint 384.2300
```

With CSV logging:

```bash
python monitor.py --channel 1 --setpoint 384.2300 --log run_001.csv
```

### Simulation (macOS / Linux / Windows without hardware)

Simulation activates automatically on non-Windows platforms.  Force it on Windows with:

```bash
python monitor.py --debug --setpoint 384.2300
```

### All options

```
python monitor.py --help

  --channel   INT    Wavemeter channel to monitor (default: 1)
  --setpoint  THz    Frequency setpoint in THz — enables deviation display
  --log       FILE   Write timestamped CSV log (columns: time_ns, frequency_THz)
  --debug            Force simulation mode
  --dll       PATH   Custom path to wlmData.dll
```

### Live display

```
WS6-200 monitor  |  channel 1  |  LIVE
Setpoint : 384.230000 THz
Press Ctrl+C to stop.

  Freq: 384.230001 THz  |  Rate: 1799.7 Hz  |  Dev:    +0.001 MHz
```

## File structure

```
laser_lock/
├── wlm_reader.py    # Thin DLL wrapper / simulation source
├── acquisition.py   # Background polling thread and ring buffer
├── monitor.py       # Terminal display and CSV logger
└── environment.yml  # Conda environment specification
```

## How it works

1. **`wlm_reader.py`** loads `wlmData.dll` via `ctypes` and calls
   `SetSwitcherMode(0)` to disable multi-channel switching — this is required to
   reach 1.8 kHz.  On non-Windows hosts it automatically generates a synthetic
   1.8 kHz signal for development and testing.

2. **`acquisition.py`** runs a tight polling loop in a daemon thread with no
   `time.sleep()`.  It detects new measurements by equality comparison (the DLL
   returns the previous value until a new measurement completes), timestamps each
   new sample with `time.perf_counter_ns()`, and stores it in a lock-free
   `collections.deque` ring buffer.

3. **`monitor.py`** reads the latest sample from the buffer, prints a live
   `\r`-updating terminal line, and optionally writes every sample to a
   line-buffered CSV file.

## Roadmap

- [ ] `lock.py` — PID controller computing frequency error and outputting a
      correction signal to a DAC
