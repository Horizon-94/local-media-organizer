# Host Resource Monitor

`tools/pipeline_benchmark/host_resource_monitor.py` is intended for an ordinary
macOS Terminal. It samples approximately once per second and writes both a
human-readable terminal stream and machine-readable CSV/JSONL. The wrapper is:

```bash
tools/pipeline_benchmark/run_host_monitor.sh /path/to/monitor-output
```

The monitor uses macOS `ps`, `vm_stat`, `memory_pressure -Q`, `sysctl`,
`top`, and `iostat`. If a command is denied or unavailable it prints `N/A`;
it does not infer GPU memory. On Apple Silicon, `GPU_MEMORY_PROCESS=N/A` is
intentional because unified memory is not NVIDIA-style per-process VRAM.

## Markers

Startup markers can be supplied with `--mark`:

```bash
tools/pipeline_benchmark/run_host_monitor.sh /tmp/monitor --mark MODEL_LOAD_START
```

For markers while the monitor is running, provide a newline-delimited file:

```bash
tools/pipeline_benchmark/run_host_monitor.sh /tmp/monitor --marker-file /tmp/pipeline-events
echo MODEL_LOAD_DONE >> /tmp/pipeline-events
```

Process start/exit markers are inferred from PID visibility and are labeled
`source=inferred`. Model-load, inference, and idle boundaries are not claimed
unless the worker emits a marker or the operator writes one.

## What is not included

`powermetrics` is not started automatically because it may require `sudo` and
can change power/thermal behavior. A future optional mode may collect it after
explicit host authorization. No Qwen/Whisper workload is started by the
monitor itself; run the existing host benchmark separately while this monitor
is active.

The current Codex sandbox is not a valid host measurement environment. A real
Metal allocation, Qwen/Whisper cold run, and persistent-worker run must be
performed from ordinary macOS Terminal before their numbers enter the resource
matrix.
