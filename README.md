# Measure GNU dd block size on Linux

Run a balanced benchmark of `bs=` for sequential zero writes. The defaults
match the original sweep: `256K`, `1M`, `4M`, `16M`, and `64M`, with 8 GiB
per trial, GNU `dd` buffered I/O, a final `fsync`, and `ionice -c 2 -n 0`.
The runner creates and removes a fresh temporary regular file for each trial.

## Run it

Use Linux, Python 3.10 or newer, GNU coreutils, util-linux, and base R for the
analysis. On Arch, `sudo pacman -S --needed python coreutils util-linux r`
installs those tools. Run the benchmark itself as your ordinary user.

```bash
cd dd-bs-benchmark

# Verify the test directory is on the drive you want to measure.
findmnt -T "$HOME" -o TARGET,SOURCE,FSTYPE
df -h -- "$HOME"

# Inspect the schedule, then run the same assignments with the same seed.
python3 run_benchmark.py --directory "$HOME" --seed 2026 --plan-only
python3 run_benchmark.py --directory "$HOME" --seed 2026
```

The default run writes 40 GiB in total and uses 60-second cooldowns between
trials. Leave at least 8 GiB of free space plus room for normal filesystem
activity. The runner prints the unique results directory and the seed.

For a replicated study, set the number of trials per condition to the same
integer for every size. This example measures three independently shuffled
time blocks on one drive and writes 120 GiB in total:

```bash
python3 run_benchmark.py --directory "$HOME" \
  --trials-per-condition 3 --seed 2026
```

For a short pipeline check, use small files and an abbreviated cooldown:

```bash
python3 run_benchmark.py --directory "$HOME" --bytes 64MiB \
  --trials-per-condition 3 --cooldown-seconds 0 --seed 2026
```

For sustained throughput, narrow the candidate sizes, then increase the volume
per trial. For example, `--bs 4M --bs 16M --bs 64M --bytes 64GiB` tests
the three largest candidates at 64 GiB each. A cooldown is a pause, and the
drive's temperature, idle time, and prior write volume help assess whether a
longer pause is warranted.

## Configuration and assignment

Edit `config.json`, pass partial JSON overrides via `--config FILE`, or
override the values directly on the CLI. Repeat `--bs SIZE` to replace the
entire block-size list. `--help` lists the arguments.

| Config key | Default | Meaning |
| --- | --- | --- |
| `block_sizes` | `256K, 1M, 4M, 16M, 64M` | Candidate GNU `dd` block sizes |
| `bytes_per_trial` | `8GiB` | Fixed number of bytes per trial |
| `trials_per_condition` | `1` | Total scheduled trials at each block size |
| `trials_per_subject` | `1` | Consecutive trials on each assigned session or drive |
| `cooldown_seconds` | `60` | Requested pause between trials |
| `baseline_seconds` | `1` | Pre-trial window for CPU and disk counter deltas |
| `sample_interval_seconds` | `1` | Telemetry sampling interval during `dd` |
| `seed` | `null` | Draw a seed and record it, or supply an integer to replay |
| `io_priority` | `0` | Best-effort `ionice` priority, with `null` to omit it |

If \(T\) is the trials per condition and \(m\) the trials per assigned unit,
then \(T/m\) must be an integer. Each randomized complete block has one unit
per block size. Across \(B=T/m\) blocks each size therefore gets \(Bm=T\)
trials. Randomization shuffles assignments before the benchmark begins. The
original assignment schedule remains useful even if an interruption or a
`dd` failure ends the run early.

**One drive.** Each eligible time slot is a session. Block size is assigned
between sessions. With `--trials-per-subject 2`, the two trials in a session
share the same block size. For example, `--trials-per-condition 6
--trials-per-subject 2` gives three time blocks and two trials in each
session. Estimates apply to this drive and the tested workload. The SSD
continues to share its temperature, cache, and background state across
sessions, so carryover can affect the randomization inference.

**Physical drives.** Supply a roster with `--subjects` for between-drive
assignment. Use one directory on each physical drive. Each block in the roster
needs as many drives as there are conditions, and the roster needs \(T/m\)
blocks. You can group similar drives by model or configuration within a
block, then balance block size within that group. The runner checks the
Linux backing-device mapping and rejects duplicate drives it can identify.
Use single-device filesystems with directly traceable backing devices for
this design. Confirm the mapping yourself when virtual disks, RAID, or
multi-device filesystems are involved.

```bash
cp subjects.example.csv subjects.csv
# Edit subjects.csv to set the four mounted test directories first.
python3 run_benchmark.py --subjects subjects.csv \
  --bs 4M --bs 64M --trials-per-condition 2 --seed 2026 --plan-only

# After inspecting the assignments, repeat without --plan-only to run them.
```

The `directory` column accepts absolute paths or paths relative to the
roster. All trials of a subject stay together. Within a physical-drive
block, execution order is shuffled independently after assignment. The order
of roster blocks follows their first appearance in the CSV. In one-drive
mode, `--disk nvme0n1` explicitly selects the disk for telemetry when the
mount is hard to trace.

## Data and covariates

The CSV files have UTF-8 headers, comma separators, standard quoting, simple
column names, decimal points, and empty cells for unavailable values. The
runner writes progress, telemetry, and trial rows after each attempt. The
pair `run_id,trial_id` joins trial, telemetry, and progress records. A failed
or interrupted trial retains available output, and unstarted trials appear
in the plan.

| File | Rows | Contents |
| --- | --- | --- |
| `plan.csv` | Planned trials | Seed, block, assignment unit, setting, exact planned order, counts and target |
| `trials.csv` | Attempted trials | Actual order and UTC timestamps, Linux kernel, result, requested and observed cooldown, prior device activity, baseline covariates, `dd` metrics and command |
| `telemetry.csv` | Measured intervals | Pre-trial, in-trial and post-trial CPU, memory, temperature, power and disk data |
| `dd_progress.csv` | Reports from GNU `dd` | Transfer reports with byte totals, reported elapsed seconds and final-report marker |
| `dd/trial-*.stderr.log` | One file per launch | Raw GNU `dd` stderr including progress and diagnostics |
| `run.json` | One document | Effective configuration, versions, CPU model, affinity size, device and filesystem settings, probe sources and run status |

`throughput_mib_s` divides actual bytes by \(2^{20}\) and by the monotonic
wall-clock time from launch to exit, including the final flush.
`dd_reported_seconds` preserves the tool's own reported interval.
`dd_user_cpu_seconds` and `dd_system_cpu_seconds` track task CPU time.
`dd_cpu_pct_one_core` expresses their sum as a percentage of one core's wall
time. The progress CSV preserves GNU `dd` reports as printed, including the
lower time precision of interim reports.

| Candidate covariates | Interpretation |
| --- | --- |
| `pre_cpu_busy_pct`, `pre_cpu_iowait_pct`, `pre_cpu_steal_pct` | System-wide percentages over the baseline window |
| `pre_cpu_freq_mhz`, `pre_load1` | Mean reported scaling frequency across visible policies and 1-minute system load |
| `pre_cpu_temp_c`, `pre_ssd_temp_c` | CPU package and disk temperature at the end of baseline, in °C |
| `pre_mem_available_mib`, `pre_dirty_mib`, `pre_writeback_mib` | Available RAM and Linux dirty/writeback pages in MiB |
| `pre_disk_read_mib_s`, `pre_disk_write_mib_s`, `pre_disk_write_iops` | Background traffic across the whole monitored disk during baseline |
| `pre_disk_avg_write_request_kib`, `pre_disk_write_await_ms`, `pre_disk_active_pct`, `pre_disk_in_flight` | Request size, time per completed write, active time percentage and pending I/O count |
| `pre_free_gib`, `pre_ac_online` | Available filesystem space and whether an exposed AC or USB power supply reports online |
| `device_payload_mib_before`, `position_in_block` | Prior benchmark payload on the same known drive and subject position within the block |

Disk percentages and averages use kernel counters. Temperature readings use
hwmon or CPU thermal zones exposed to the user. On AMD, `Tdie` is preferred
when available and `Tctl` is a fallback. `run.json` identifies the sensor
paths. Idle counters can validly be zero, while unavailable sensors and
undefined rates are left empty. A very short trial can finish before the
first in-trial sample. The post-trial interval still captures the remaining
counter delta.

The temporary file benchmark includes the mounted storage stack. Compression,
copy-on-write, encryption, write caching, drive temperature, and other activity
on the drive can affect the results. Choose an uncompressed directory on the
target SSD when benchmarking a zero-filled file, and keep workload and power
settings comparable between runs. Buffered results capture the wrapper's
write path including writeback and the final flush. Filesystem timings give
candidates to test when you next have a raw overwrite to perform.

## Analyze in R

Use the results directory printed by the benchmark:

```bash
Rscript analyze.R results/run-EXAMPLE

# Optional sensitivity model using the first pre-trial sample per unit.
Rscript analyze.R results/run-EXAMPLE \
  --reference 4M \
  --covariates pre_cpu_busy_pct,position_in_block
```

For independent imports in R and Python:

```r
trials <- read.csv("results/run-EXAMPLE/trials.csv", na.strings = "")
telemetry <- read.csv("results/run-EXAMPLE/telemetry.csv", na.strings = "")
observations <- merge(telemetry, trials, by = c("run_id", "trial_id"))
```

```python
import csv

with open("results/run-EXAMPLE/trials.csv", newline="", encoding="utf-8") as file:
  trials = list(csv.DictReader(file))
```

Each invocation prints a new result folder under `analysis/` inside the run.

| Analysis output | Meaning |
| --- | --- |
| `subjects.csv` | One outcome per assigned session or drive, with trial counts and block inclusion |
| `conditions.csv` | Counts, geometric mean rates, median times and the smallest size within 5% of the fastest median time |
| `randomization.csv` | Global within-block permutation test and reference comparisons with Holm-adjusted pairwise p-values |
| `covariate_model.csv` | Optional sensitivity fit status, retained blocks/units and residual degrees of freedom |
| `covariate_coefficients.csv` | Optional log-throughput regression estimates and conventional standard errors |

The primary outcome for trial \(i\) is \(v_i=\mathrm{bytes}_i/(2^{20}t_i)\)
in MiB/s. For each assigned unit, R averages \(\log v_i\) across its trials.
It then averages the unit log-rates for a given condition across complete
blocks and exponentiates. Separately, it takes the median elapsed time
within each unit and the median of those unit medians for each condition.
If \(\tilde t_c\) is that estimate for condition \(c\), the time multiplier
is \(\tilde t_c / \min_j(\tilde t_j)\). The 5% rule is a practical threshold
applied to point estimates.

One complete block yields a descriptive comparison. With at least two, the
global test permutes unit outcomes within each block. The default is 9,999
Monte Carlo draws with a reproducible R seed. Reference comparisons pair unit
log-rates within each block, test differences by sign flips, and report
throughput ratios. They enumerate sign flips through 16 complete blocks and use
Monte Carlo beyond that. The default reference is `4M` when present, or the
smallest included candidate otherwise. Very few blocks yield coarse pairwise
p-values. The study assumes assignment at one unit leaves other units'
potential outcomes unaffected. When failures track settings or elapsed time,
examine the logged incomplete blocks before interpreting the estimates.

Choose optional covariates recorded before the unit's first write. The
sensitivity fit regresses unit log-throughput on block, setting, and the
chosen measured baselines. It uses complete blocks after requiring complete
covariate data. In-trial temperature and CPU activity are useful diagnostics
because they can change in response to the assigned setting. On a single
drive, serial dependence and carryover limit conventional model inference;
interpret the optional regression as a sensitivity comparison.

## References

- GNU `dd` flags, suffixes and synchronization. [Arch manual page](https://man.archlinux.org/man/dd.1.en)
- Randomized complete blocks. [NIST/SEMATECH statistical handbook](https://www.itl.nist.gov/div898/handbook/pri/section3/pri332.htm)
- Linux device counters and their units. [Kernel I/O statistics documentation](https://docs.kernel.org/admin-guide/iostats.html)
- AMD package-temperature labels. [Kernel k10temp documentation](https://docs.kernel.org/hwmon/k10temp.html)
- Monte Carlo permutation p-values. [Phipson and Smyth, 2010](https://pubmed.ncbi.nlm.nih.gov/21044043/)
