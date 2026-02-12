# 04_sequence_capture

Capture multiple waveform segments using the oscilloscope's sequence mode.

## What is Sequence Mode?

Sequence mode captures multiple triggered events in rapid succession, storing each as a separate segment. This is useful for:
- Capturing rare or intermittent events
- Recording burst signals with minimal dead time
- Analyzing timing variations across multiple triggers

## Prerequisites

- Python 3.10+
- pyVISA with appropriate backend
- PyVICP (`pyvicp`) if using `--protocol vicp`
- numpy, matplotlib
- Network access to the oscilloscope
- Signal source connected to CH1 and/or CH2

## Running the Example

```bash
cd examples/04_sequence_capture

# Default: WP804HD at 192.168.0.10, 100 segments
python sequence_capture.py

# Testing without a real signal (force trigger)
python sequence_capture.py --force --segments 10

# Specify model, address, segments, and output directory
python sequence_capture.py \
    --model waverunner \
    --address 192.168.1.100 \
    --segments 50 \
    --outdir ./sequences

# Use VICP transport
python sequence_capture.py --protocol vicp --segments 50
```

## Command Line Options

| Option | Default | Description |
|--------|---------|-------------|
| `--model` | `wavepro` | Oscilloscope model (`wavepro` or `waverunner`) |
| `--address` | `192.168.0.10` | IP address of the oscilloscope |
| `--protocol` | `lxi` | Transport protocol (`lxi` or `vicp`) |
| `--segments` | `100` | Number of segments to capture |
| `--outdir` | `.` | Output directory for output files |
| `--channels` | all enabled | Specific channels to capture (e.g., `--channels 1 2`) |
| `--config` | `None` | Path to JSON settings file to apply (omit to read from scope) |
| `--force` | off | Use force trigger for testing without real signals |
| `--wait-timeout` | auto | Trigger wait timeout in seconds (default: `settings.sequence.timeout_seconds`) |

## Expected Output

```
Capturing channels: [1, 2]
Arming for sequence capture (100 segments)...
  [arm] 52.3 ms
  [wait_for_trigger] 156.7 ms
Triggered.
  [readout_sequence] 2345.8 ms

=== Data Size ===
  CH1: 100 segments × 10,000 points = 1,000,000 bytes (1.00 MB)
  CH2: 100 segments × 10,000 points = 1,000,000 bytes (1.00 MB)
  Total: 2,000,000 bytes (2.00 MB)

CH1: 100 segments
  [save_data_ch1] 89.4 ms
Saved data: sequence_ch1.npz
CH1 stats: min=-0.150 V, max=0.152 V, mean=0.001 V, p2p=0.302 V
  [plot_ch1_seg0] 234.5 ms
Saved plot: sequence_ch1_seg0.png
...
```

## Expected Behavior on the Scope

After running:
1. The scope enters sequence mode (visible in acquisition settings)
2. The scope arms and displays segment counter (e.g., "0/100")
3. As triggers occur, the segment counter increments
4. When all segments are captured, acquisition stops
5. The scope displays the last captured segment

The script forces `trigger.mode=NORM` and enables sequence mode with `--segments`.

## Output Files

Data files (all segments):
- `sequence_ch1.npz` - All segment data for Channel 1 (voltages array, time axis)
- `sequence_ch2.npz` - All segment data for Channel 2

Plot files (first 3 segments as sanity check):
- `sequence_ch1_seg0.png`, `sequence_ch1_seg1.png`, `sequence_ch1_seg2.png`
- `sequence_ch2_seg0.png`, `sequence_ch2_seg1.png`, `sequence_ch2_seg2.png`

## Troubleshooting

- **Timeout waiting for trigger**: Use `--force` flag for testing without real signals, or ensure a signal is connected
- **Fewer segments than expected**: Check if the trigger rate is too slow
- **Memory errors**: Reduce `--segments` if the scope runs out of acquisition memory
