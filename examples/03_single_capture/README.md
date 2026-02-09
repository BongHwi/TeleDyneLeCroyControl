# 03_single_capture

Capture a single waveform from the oscilloscope and save it as a PNG plot.

## Prerequisites

- Python 3.10+
- pyVISA with appropriate backend
- PyVICP (`pyvicp`) if using `--protocol vicp`
- numpy, matplotlib
- Network access to the oscilloscope
- Signal source connected to CH1 and/or CH2

## Running the Example

```bash
cd examples/03_single_capture

# Default: WavePro at 192.168.0.10, output to current directory
python single_capture.py

# Testing without a real signal (force trigger)
python single_capture.py --force

# Specify model, address, and output directory
python single_capture.py --model waverunner --address 192.168.1.100 --outdir ./captures

# Use VICP transport
python single_capture.py --protocol vicp --outdir ./captures
```

## Command Line Options

| Option | Default | Description |
|--------|---------|-------------|
| `--model` | `wavepro` | Oscilloscope model (`wavepro` or `waverunner`) |
| `--address` | `192.168.0.10` | IP address of the oscilloscope |
| `--protocol` | `lxi` | Transport protocol (`lxi` or `vicp`) |
| `--outdir` | `.` | Output directory for output files |
| `--channels` | all enabled | Specific channels to capture (e.g., `--channels 1 2`) |
| `--config` | `None` | Path to JSON settings file to apply (omit to read from scope) |
| `--force` | off | Use force trigger for testing without real signals |

## Expected Output

```
Capturing channels: [1, 2]
Arming for single capture...
  [arm] 45.2 ms
  [wait_for_trigger] 123.5 ms
Triggered.
  [readout] 890.3 ms

=== Data Size ===
  CH1: 10,000 bytes (0.01 MB), 10,000 points
  CH2: 10,000 bytes (0.01 MB), 10,000 points
  Total: 20,000 bytes (0.02 MB)

  [save_data_ch1] 12.3 ms
Saved data: single_capture_ch1.npz
CH1 stats: min=-0.150 V, max=0.152 V, mean=0.001 V, p2p=0.302 V
  [plot_ch1] 245.6 ms
Saved plot: single_capture_ch1.png
```

## Expected Behavior on the Scope

After running:
1. The scope arms and displays "Armed" or "Ready" status
2. Once triggered, the scope captures and displays the waveform
3. The "STOP" indicator appears after acquisition completes
4. Waveform data is transferred to the PC and saved as PNG plots

## Notes

- If `--config` is omitted, current scope settings are saved to `settings.json`.
- The script forces `trigger.mode=SINGLE` and disables sequence mode for this run.

## Output Files

- `single_capture_ch1.npz` - Waveform data for Channel 1 (time, voltage, raw, metadata)
- `single_capture_ch1.png` - Waveform plot for Channel 1
- `single_capture_ch2.npz` - Waveform data for Channel 2
- `single_capture_ch2.png` - Waveform plot for Channel 2

## Troubleshooting

- **Timeout waiting for trigger**: Use `--force` flag for testing without real signals, or ensure a signal is connected
- **No data returned**: Check that channels are enabled in the configuration
- **Plot looks wrong**: Verify the vertical scale (vdiv) matches your signal amplitude
