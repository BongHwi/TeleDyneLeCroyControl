# Teledyne LeCroy Oscilloscope Library

Python library for controlling Teledyne LeCroy oscilloscopes via VISA/TCP (LXI or VICP).

**Supported Models:** WP804HD, WR8208HD series

**Author:** Bong-Hwi Lim (UTokyo)

## Installation

### Dependencies

```bash
pip install pyvisa pyvisa-py pyvicp numpy matplotlib
```

Transport/runtime notes:
- `pyvisa-py`: LXI/TCP backend
- `pyvicp` (PyVICP): required when using `protocol="vicp"`

For NI-VISA backend (recommended for production):
- Install [NI-VISA](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html)

### Setup

```bash
git clone <this-repo>
cd TeleDyneLeCroyControl
```

## Quick Start

```python
from teledyne_lecroy import (
    WP804HD,
    ChannelConfig,
    ChannelTrigger,
    AcquisitionConfig,
    TriggerConfig,
    TriggerState,
)

# Connect to oscilloscope (default: LXI, optional: protocol="vicp")
with WP804HD("192.168.0.10", protocol="lxi") as scope:
    # Configure channels
    channels = {
        1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True),
        2: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True),
    }

    # Configure acquisition
    acquisition = AcquisitionConfig(tdiv=1e-3, sampling_period=1e-6)

    # Apply configuration
    scope.configure(channels=channels, acquisition=acquisition)

    # Set trigger
    scope.set_trigger(TriggerConfig(
        channels={
            1: ChannelTrigger(state=TriggerState.HIGH, level=0.02),
        },
        mode="SINGLE",
    ))

    # Capture waveform
    scope.arm()
    scope.wait_for_trigger(timeout=10.0)

    # Read data
    data = scope.readout()
    for ch, waveform in data.items():
        voltage = waveform.to_voltage()
        time = waveform.to_time()
        print(f"CH{ch}: {len(voltage)} points")
```

## API Reference

### Oscilloscope Classes

| Class | Description |
|-------|-------------|
| `WP804HD` | WP804HD series (up to 80 GS/s, 8 GHz bandwidth) |
| `WR8208HD` | WR8208HD series (up to 40 GS/s, 4 GHz bandwidth) |

Both classes share the same API. Use the one matching your hardware.

### Configuration Dataclasses

#### `ChannelConfig`

```python
ChannelConfig(
    vdiv: float = 0.020,           # V/div
    offset: float = 0.0,           # V
    coupling: Coupling = Coupling.DC50,  # DC50, DC1M, AC1M, GND
    enabled: bool = True,
)
```

#### `AcquisitionConfig`

```python
AcquisitionConfig(
    tdiv: float = 5e-9,            # s/div (time per division)
    sampling_period: float = 25e-12,  # s (sample interval)
    trigger_delay: float = 0.0,    # s
    window_delay: float = 10e-9,   # s
)
```

#### `TriggerConfig`

```python
TriggerConfig(
    channels: dict[int, ChannelTrigger] = {},
    mode: Literal["AUTO", "NORM", "SINGLE", "STOP"] = "SINGLE",
    logic: Literal["OR", "AND"] = "OR",
    external: bool = False,
    external_level: float = 1.25,
)
```

`ChannelTrigger` values:

```python
ChannelTrigger(
    state: TriggerState = TriggerState.DONT_CARE,  # HIGH, LOW, DONT_CARE
    level: float | None = None,                    # absolute level (V)
    level_offset: float = 0.0,                     # relative level (V)
)
```

#### `SequenceConfig`

```python
SequenceConfig(
    enabled: bool = False,
    num_segments: int = 1,
    timeout_enabled: bool = False,
    timeout_seconds: float = 2.5e6,
)
```

### Data Classes

#### `WaveformData`

Immutable waveform data container.

```python
waveform = data[1]  # Get channel 1 waveform

# Convert to numpy arrays
voltage = waveform.to_voltage()  # NDArray[np.float64]
time = waveform.to_time()        # NDArray[np.float64]

# Access metadata
waveform.channel      # Channel number
waveform.segment      # Segment index (for sequence mode)
waveform.dx           # Time step (s)
waveform.dy           # Voltage scale (V/ADC)
waveform.trigger_time # Trigger timestamp
```

#### `SequenceData`

Container for sequence mode segments.

```python
seq = sequence_data[1]  # Get channel 1 sequence

len(seq)              # Number of segments
seq[0]                # First segment (WaveformData)
seq.to_voltage_array()  # 2D array (n_segments, n_points)
```

### Enums

#### `TriggerSlope`

```python
TriggerSlope.RISING   # Rising edge
TriggerSlope.FALLING  # Falling edge
TriggerSlope.EITHER   # Either edge
```

#### `Coupling`

```python
Coupling.DC50  # 50Ω DC coupling
Coupling.DC1M  # 1MΩ DC coupling
Coupling.AC1M  # 1MΩ AC coupling
Coupling.GND   # Ground coupling
```

### Exceptions

| Exception | Description |
|-----------|-------------|
| `ScopeError` | Base exception |
| `ScopeConnectionError` | Connection failure |
| `ScopeTimeoutError` | Response timeout |
| `ScopeConfigurationError` | Invalid configuration |
| `ScopeTriggerError` | Trigger-related error |

### Core Methods

#### Connection

```python
scope.connect()      # Establish connection
scope.disconnect()   # Close connection
# Or use context manager:
with WP804HD("192.168.0.10") as scope:
    ...
```

Transport selection:
- `protocol="lxi"` (default): `TCPIP0::<ip>::inst0::INSTR`
- `protocol="vicp"`: `VICP::<ip>::INSTR`

#### Configuration

```python
scope.configure(channels, acquisition, sequence=None)
scope.set_trigger(config)
```

#### Trigger Control

```python
scope.arm(force=False)           # Arm trigger (force=True for immediate)
scope.is_triggered()             # Check trigger status
scope.wait_for_trigger(timeout)  # Wait with timeout
scope.set_trigger_mode(mode)     # Set sweep mode
```

#### Data Readout

```python
scope.readout(channels=None)           # Single capture
scope.readout_sequence(channels=None)  # Sequence mode
```

#### Low-Level SCPI

```python
scope.write("SCPI command")    # Send command
scope.query("SCPI query?")     # Send query, get response
scope.read_raw()               # Read binary data
scope.wait_opc()               # Wait for operation complete
scope.clear()                  # Clear registers
```

## Examples

See the `examples/` directory:

| Example | Description |
|---------|-------------|
| `01_connect` | Basic connection and identity query (LXI/VICP) |
| `02_manual_settings` | Configure scope from JSON file |
| `03_single_capture` | Single waveform capture with plotting |
| `04_sequence_capture` | Sequence mode multi-segment capture |

Run examples:

```bash
python -m examples.01_connect.connect --address 192.168.0.10
python -m examples.01_connect.connect --address 192.168.0.10 --protocol vicp
python -m examples.02_manual_settings.manual_settings --config settings.json
python -m examples.03_single_capture.single_capture --outdir ./captures
python -m examples.04_sequence_capture.sequence_capture --segments 100
```

## Hardware Specifications

| Spec | WP804HD | WR8208HD |
|------|---------|------------|
| Max Sampling Rate | 80 GS/s | 40 GS/s |
| Max Bandwidth | 8 GHz | 4 GHz |
| ADC Resolution | 8 bit | 8 bit |
| Channels | 4 | 4 |
| Time Divisions | 10 | 10 |
| Voltage Divisions | 8 | 8 |

## License

MIT License
