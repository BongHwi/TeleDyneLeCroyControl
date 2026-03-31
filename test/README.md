# README — LeCroy Remote Control (apply_config.py / scope_snapshot.py)

This tooling lets you **apply** a human‑friendly JSON configuration to a Teledyne LeCroy oscilloscope and **snapshot** the current scope state in a readable and reproducible way.

It is designed for **WavePro (WP)** and **WaveRunner/WaveSurfer (WR/WS)** scopes and handles model differences internally via `*IDN?`.

---

## Features

- Apply scope settings from a readable `config.json`
- Snapshot scope state:
  - Human‑readable console output
  - JSON dump for logging, diff, and reproducibility
- Trigger‑aware display (Edge vs Logic/Pattern)
- WP / WR channel‑count compatibility (C1–C4 / C1–C8)
- Safe handling of unsupported properties

---

## Requirements

- Ubuntu
- Python 3.10+
- Packages:
  ```bash
  pip install pyvisa pyvisa-py
  ```

The connection uses **VXI‑11 (LXI)**:
```
TCPIP0::<SCOPE_IP>::inst0::INSTR
```

---

## File Overview

- **lecroy_visa.py**
  - VISA connection and VBS/SCPI helpers
  - Maps human‑friendly keys to LeCroy VBS properties
  - Handles WP vs WR differences (e.g. AUX OUT)

- **apply_config.py**
  - Reads `config.json`
  - Applies channel, trigger, horizontal, AUX OUT, and save settings

- **scope_snapshot.py**
  - Reads back scope state using the same key mapping
  - Prints readable summaries
  - Writes JSON snapshots
  - Supports diff against older snapshots

---

## config.json Structure

### Channels

```json
"channels": {
  "C1": {
    "view": -1,
    "coupling": "DC50",
    "scale": 0.2,
    "offset": 0.0,
    "invert": 0,
    "bandwidth_limit": "Full"
  }
}
```

**Official keys**
- `scale` : vertical scale (V/div)
- `offset`: vertical offset (V)

Legacy `ver_scale` / `ver_offset` are **not supported**.

---

### Trigger

```json
"trigger": {
  "type": "Edge",
  "source": "C1",
  "levels": { "C1": -0.05, "Ext": 0.30 },
  "slopes": { "C1": "Negative", "Ext": "Positive" },
  "mode_scpi": "NORMAL"
}
```

- `type`: `Edge` or `Logic`
- `source`: `C1`, `C2`, … or `Ext`
- `levels`, `slopes`: per‑source thresholds
- Logic/Pattern triggers additionally support:
  - `pattern_type`
  - `pattern_states.{Cx}`

---

### AUX OUT

```json
"aux_out": {
  "mode": "TriggerEnabled"
}
```

Handled internally for WP vs WR models.

---

## apply_config.py

Apply configuration to the scope.

```bash
python3 apply_config.py --ip 192.168.0.100 --config config.json
```

### Options
- `--ip` scope IP address
- `--config` configuration file
- `--backend` pyvisa backend (default `@py`)
- `--timeout` VISA timeout (seconds)

### Safety Flags (in config.json)

```json
"dry_run": true,
"stop_on_error": false
```

---

## scope_snapshot.py

Snapshot current scope state.

### Default (print + JSON dump)

```bash
python3 scope_snapshot.py --ip 192.168.0.100 --config config.json
```

### Limit scope

```bash
--only trigger
--only channels
--only horizontal
```

### Trigger display modes

```bash
--trigger-view smart   # default
--trigger-view full
```

- **Edge**: show only active source level/slope
- **Logic/Pattern**: show all pattern states (including Don’t Care)

### Diff

```bash
python3 scope_snapshot.py --ip 192.168.0.100 --config config.json --diff old_snapshot.json
```

### Strict mode

```bash
--strict   # non‑zero exit if unsupported keys exist
```

---

## Recommended Workflow

1. Apply settings:
   ```bash
   python3 apply_config.py --ip <scope-ip> --config config.json
   ```

2. Snapshot and verify:
   ```bash
   python3 scope_snapshot.py --ip <scope-ip> --config config.json
   ```

3. Diff after changes:
   ```bash
   python3 scope_snapshot.py --ip <scope-ip> --config config.json --diff previous.json
   ```

---

## Notes

- Unsupported properties are normal across models/firmware
- All unsupported keys are listed under `[Missing]` in snapshots
- JSON snapshots are intended for **logging and reproducibility**, not manual editing

---

This design intentionally separates:
- **Human‑readable configuration**
- **Instrument‑specific control logic**
- **Reproducible state capture**

---

## Scope NAME mapping (IP → NAME)

`apply_config.py` と `scope_snapshot.py` の両方で、**IP アドレスに紐づいたスコープ名 (NAME)** を使えます。  
スコープ名はファイル名やログ表示に使われ、運用上の取り違え防止に効果があります。

### config.json に IP→NAME を書く

```json
"scope_profile": {
  "channels_max": 8,
  "scope_names": {
    "192.168.0.100": "WP804HD_MAIN",
    "192.168.0.101": "WR8204HD_1"
  }
}
```

- `scope_names` に IP が存在する場合: その NAME を使用
- 存在しない場合: `IP_192_168_0_100` のようにフォールバック

**NAME の推奨文字:** 英数字 + `_` `-` `.`（ファイル名安全のため）

---

## Run number injection (--runnumber)

`apply_config.py` に `--runnumber` を渡すと、`save_waveforms.trace_title_prefix` の run 部分が **必ず 6 桁 (ゼロ埋め)** で置換されます。

### 推奨: プレースホルダ方式

config.json:

```json
"save_waveforms": {
  "enabled": true,
  "trace_title_prefix": "runXXXXXX_",
  "sources": ["C1","C2","C3","C4"]
}
```

実行:

```bash
python3 apply_config.py --ip 192.168.0.100 --config config.json --runnumber 23
```

解決結果:

- `run23` → `run000023`
- `trace_title_prefix` は `run000023_` になります

### 既存の run000123 を置換する場合

`trace_title_prefix` に `run` + 6 桁が既に含まれている場合も、最初の一致を置換します。

例: `run001234_` → `run000023_`

---

## Snapshot filename convention (scope_snapshot.py)

`scope_snapshot.py` の JSON 出力ファイル名は、可能なら **MODEL + NAME + runTag** で自動命名されます。

- `runTag` は `readback_human["save.trace_title"]` から `run\d{6}` を抽出します

### 既定の出力名

```
snapshots/<MODEL>_<NAME>_<runXXXXXX>.json
```

例:

```
snapshots/WP804HD_WP804HD_MAIN_run000023.json
```

### runTag が取得できない場合（フォールバック）

`save.trace_title` から `run\d{6}` が取れない場合は、従来どおり timestamp にフォールバックします:

```
snapshots/<MODEL>_<NAME>_<YYYYmmdd_HHMMSS>.json
```

---

## Updated Usage examples

### Apply settings with run number (and NAME auto from config)

```bash
python3 apply_config.py --ip 192.168.0.100 --config config.json --runnumber 23
```

### Snapshot (print + JSON dump)

```bash
python3 scope_snapshot.py --ip 192.168.0.100 --config config.json
```

### Snapshot trigger only, smart trigger view

```bash
python3 scope_snapshot.py --ip 192.168.0.100 --config config.json --only trigger --trigger-view smart
```

