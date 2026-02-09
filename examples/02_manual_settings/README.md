# 02_manual_settings

Configure oscilloscope channel, acquisition, and trigger settings from a JSON file, or save current scope settings to a file.

## Prerequisites

- Python 3.10+
- pyVISA with appropriate backend
- PyVICP (`pyvicp`) if using `--protocol vicp`
- Network access to the oscilloscope

## Configuration File

Edit `settings.json` to customize the oscilloscope configuration:

```json
{
  "channels": {
    "1": { "vdiv": 0.2, "offset": 0.0, "coupling": "DC50", "enabled": true },
    "2": { "vdiv": 0.2, "offset": 0.0, "coupling": "DC50", "enabled": true }
  },
  "acquisition": {
    "tdiv": 1e-3,
    "sampling_period": 1e-6,
    "trigger_delay": 0.0,
    "window_delay": 10e-9,
    "memory_size": 10000,
    "sample_rate": 1000000.0
  },
  "trigger": {
    "channels": {
      "1": { "state": "HIGH", "level": 0.1 },
      "2": { "state": "LOW", "level": -0.05 }
    },
    "mode": "SINGLE",
    "logic": "OR",
    "external": false,
    "external_level": 1.25
  },
  "instrument": {
    "display": "OFF",
    "grid": "QUATTRO",
    "bandwidth_limit": "OFF"
  },
  "sequence": {
    "enabled": false,
    "num_segments": 1,
    "timeout_enabled": false,
    "timeout_seconds": 2500000.0
  },
  "auxiliary_output": "TRIGGER_OUT"
}
```

### Channel Settings

| Field | Description |
|-------|-------------|
| `channels.<n>.vdiv` | Vertical scale (V/div) |
| `channels.<n>.offset` | Vertical offset (V) |
| `channels.<n>.coupling` | Coupling mode: `DC50`, `DC1M`, `AC1M`, or `GND` |
| `channels.<n>.enabled` | Enable/disable channel |

**Coupling Modes:**
- `DC50` - 50Ω DC coupling (default)
- `DC1M` - 1MΩ DC coupling
- `AC1M` - 1MΩ AC coupling
- `GND` - Ground

### Acquisition Settings

| Field | Description |
|-------|-------------|
| `acquisition.tdiv` | Horizontal scale (s/div) |
| `acquisition.sampling_period` | Sample interval (s) |
| `acquisition.trigger_delay` | Trigger delay time (s) |
| `acquisition.window_delay` | Window delay time (s) |
| `acquisition.memory_size` | Acquisition memory size (points, optional) |
| `acquisition.sample_rate` | Sample rate (Hz, optional) |

### Trigger Settings

| Field | Description |
|-------|-------------|
| `trigger.channels.<n>.state` | `HIGH`, `LOW`, or `DONT_CARE` |
| `trigger.channels.<n>.level` | Absolute trigger level (V) |
| `trigger.channels.<n>.level_offset` | Relative to baseline (V), used if `level` is not set |
| `trigger.mode` | `SINGLE`, `NORM`, or `AUTO` |
| `trigger.logic` | Pattern logic: `OR` or `AND` |
| `trigger.external` | Enable external trigger input (bool) |
| `trigger.external_level` | External trigger level (V) |

**Trigger States:**
- `HIGH` - Trigger on rising edge (high level)
- `LOW` - Trigger on falling edge (low level)
- `DONT_CARE` - Don't use this channel for triggering

**External Trigger:**
When `external` is `true`, the scope will use the external trigger input (EX) in addition to any configured channel triggers. Set `external_level` to specify the threshold voltage.

### Sequence Settings

| Field | Description |
|-------|-------------|
| `sequence.enabled` | Enable sequence mode (bool) |
| `sequence.num_segments` | Number of segments to capture |
| `sequence.timeout_enabled` | Enable timeout between segments (bool) |
| `sequence.timeout_seconds` | Timeout duration (s) |

**Sequence Mode:**
When enabled, the oscilloscope captures multiple waveform segments in a single acquisition. Each segment is triggered independently and stored separately. This is useful for capturing rare events or analyzing signal variations over time.

### Instrument Settings

| Field | Description |
|-------|-------------|
| `instrument.display` | Scope display: `ON` or `OFF` |
| `instrument.grid` | Grid mode (example: `QUATTRO`) |
| `instrument.bandwidth_limit` | Bandwidth limit: `ON` or `OFF` |

### Auxiliary Output Settings

| Field | Description |
|-------|-------------|
| `auxiliary_output` | Auxiliary output mode: `TRIGGER_OUT` or `TRIGGER_ENABLED` |

**Auxiliary Output Modes:**
- `TRIGGER_OUT` - Output a pulse when trigger occurs
- `TRIGGER_ENABLED` - Output high when trigger is armed

## Running the Example

```bash
cd examples/02_manual_settings
```

### Save Current Settings (Read Mode)

Without `--config`, the script reads current scope settings and saves to a file:

```bash
# Save to settings.json (default), prompts if file exists
python manual_settings.py

# Force overwrite without confirmation
python manual_settings.py -f

# Save to custom output path
python manual_settings.py --output my_settings.json

# Specify model and address
python manual_settings.py --model waverunner --address 192.168.1.100

# Use VICP transport
python manual_settings.py --protocol vicp
```

### Apply Settings from File (Apply Mode)

With `--config`, the script applies settings from the specified file:

```bash
# Apply from settings.json
python manual_settings.py --config settings.json

# Apply from custom file
python manual_settings.py --config my_settings.json

# Partial config - only apply trigger settings
python manual_settings.py --config trigger_only.json

# Apply settings over VICP transport
python manual_settings.py --protocol vicp --config settings.json
```

### Partial Configuration Files

The script supports partial config files. Only sections present in the JSON file are applied; missing sections are left unchanged on the scope. For example, to change only the trigger settings:

```json
{
  "trigger": {
    "channels": {
      "1": { "state": "HIGH", "level": 0.5 }
    },
    "mode": "SINGLE"
  }
}
```

## Expected Output

### When Saving Settings

```
Settings saved to: settings.json
```

If the output file already exists:
```
'settings.json' exists. Overwrite? [y/N]: y
Settings saved to: settings.json
```

### When Applying Settings

```
Loading settings from: settings.json
Applying sections: instrument, channels, acquisition, trigger, sequence, auxiliary_output
Settings applied. Verify changes on the scope front panel.
```

For partial configs:
```
Loading settings from: trigger_only.json
Applying sections: trigger
Settings applied. Verify changes on the scope front panel.
```

## Expected Behavior on the Scope

After running this script, verify on the oscilloscope front panel:

- **Channels 1 and 2**: Enabled with 200 mV/div vertical scale
- **Timebase**: 1 ms/div horizontal scale
- **Trigger**: Pattern trigger with CH1 HIGH (0.1V), CH2 LOW (-0.05V), single-shot mode

## Troubleshooting

- **JSON parse error**: Validate your settings.json syntax
- **Configuration errors**: Ensure the scope supports the requested settings
- **Trigger not working**: Check trigger levels are within signal range
