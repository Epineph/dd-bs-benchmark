#!/usr/bin/env python3
"""Run balanced GNU dd block-size trials against temporary regular files."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import random
import re
import resource
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from telemetry import (
  METRICS, MIB, PROC, Monitor, Snapshot, controller_identity, inspect_mount,
  interval_metrics, read_text, utc_now,
)

VERSION = "1.0.1"
SIZES = {
  "": 1, "B": 1, "K": 1024, "M": MIB, "G": 1024**3, "KiB": 1024,
  "MiB": MIB, "GiB": 1024**3, "kB": 1000, "MB": 1000**2, "GB": 1000**3,
}
PLAN_FIELDS = (
  "run_id", "trial_id", "planned_order", "design", "seed", "block_id",
  "block_index", "subject_id", "position_in_block", "trial_in_subject",
  "block_size", "block_size_bytes", "bytes_requested",
  "planned_trials_per_condition", "planned_trials_per_subject", "directory",
  "mount_source", "mount_major_minor", "filesystem", "disk", "disk_mapping",
  "disk_model", "io_scheduler", "max_sectors_kib",
)
TRIAL_FIELDS = PLAN_FIELDS + (
  "actual_order", "linux_kernel", "attempted_utc", "started_utc", "ended_utc",
  "status",
  "error", "dd_exit_code", "bytes_written", "wall_seconds", "throughput_mib_s",
  "dd_reported_seconds", "records_in_full", "records_in_partial",
  "records_out_full", "records_out_partial", "dd_user_cpu_seconds",
  "dd_system_cpu_seconds", "dd_cpu_pct_one_core", "requested_cooldown_seconds",
  "actual_cooldown_seconds", "seconds_since_previous_device_trial",
  "previous_device_block_size", "device_payload_mib_before",
  "baseline_window_seconds",
) + tuple(f"pre_{field}" for field in METRICS) + (
  "post_cpu_temp_c", "post_ssd_temp_c", "max_cpu_temp_c", "max_ssd_temp_c",
  "dd_argv_json", "dd_stderr_path",
)
SAMPLE_FIELDS = (
  "run_id", "trial_id", "sample_index", "phase", "observed_utc",
  "elapsed_trial_seconds", "interval_seconds",
) + METRICS
PROGRESS_FIELDS = (
  "run_id", "trial_id", "report_index", "is_final", "bytes_reported",
  "dd_reported_seconds",
)
TRANSFER = re.compile(r"^(\d+) bytes?\b.*?\bcopied, ([\d.eE+-]+) s(?:,|$)")
RECORDS = re.compile(r"^(\d+)\+(\d+) records (in|out)$")


# -- Configuration and randomization -----------------------------------------

def size_bytes(value: str) -> int:
  match = re.fullmatch(r"([0-9]+)([A-Za-z]*)", value)
  if not match or match[2] not in SIZES or int(match[1]) <= 0:
    raise ValueError(f"invalid size {value!r}; examples: 256K, 4M, 8GiB")
  return int(match[1]) * SIZES[match[2]]


def argument_parser() -> argparse.ArgumentParser:
  cli = argparse.ArgumentParser(
    description=__doc__,
    epilog="Run as your normal Linux user. "
    "--plan-only generates the CSV schedule without running write trials.",
  )
  location = cli.add_mutually_exclusive_group()
  location.add_argument(
    "--directory", type=Path, help="benchmark directory (default: current)",
  )
  location.add_argument(
    "--subjects", type=Path, help="physical-drive roster CSV",
  )
  cli.add_argument("--disk", help="whole-disk sysfs name for session telemetry")
  cli.add_argument("--results-dir", type=Path, default=Path("results"))
  cli.add_argument("--config", type=Path, help="JSON overrides to config.json")
  cli.add_argument(
    "--bs", dest="block_sizes", action="append", metavar="SIZE",
    help="repeat to replace the default list of block sizes",
  )
  cli.add_argument(
    "--bytes", dest="bytes_per_trial", metavar="SIZE",
    help="data written per trial, e.g. 8GiB",
  )
  cli.add_argument("--trials-per-condition", type=int, metavar="N")
  cli.add_argument("--trials-per-subject", type=int, metavar="N")
  cli.add_argument("--seed", type=int, help="recorded seed for the schedule")
  cli.add_argument("--cooldown-seconds", type=float)
  cli.add_argument("--baseline-seconds", type=float)
  cli.add_argument("--sample-interval-seconds", type=float)
  priority = cli.add_mutually_exclusive_group()
  priority.add_argument("--io-priority", type=int, metavar="N")
  priority.add_argument("--no-ionice", action="store_true")
  cli.add_argument("--plan-only", "--dry-run", action="store_true")
  return cli


def load_config(args: argparse.Namespace) -> dict:
  default_file = Path(__file__).with_name("config.json")
  config = json.loads(default_file.read_text(encoding="utf-8"))
  if args.config:
    overrides = json.loads(args.config.read_text(encoding="utf-8"))
    if not isinstance(overrides, dict) or overrides.keys() - config.keys():
      raise ValueError("config overrides must use keys in config.json")
    config.update(overrides)
  for key in config:
    if (value := getattr(args, key, None)) is not None:
      config[key] = value
  if args.no_ionice:
    config["io_priority"] = None

  sizes = config["block_sizes"]
  if not isinstance(sizes, list) or not all(isinstance(s, str) for s in sizes):
    raise ValueError("block_sizes must be a list of size strings")
  parsed = {s: size_bytes(s) for s in sizes}
  if len(sizes) < 2 or len(set(parsed.values())) != len(sizes):
    raise ValueError("provide at least two distinct block sizes")
  if not isinstance(config["bytes_per_trial"], str):
    raise ValueError("bytes_per_trial must be a size string")
  config["bytes_requested"] = size_bytes(config["bytes_per_trial"])
  config["block_size_bytes"] = parsed

  for key in ("trials_per_condition", "trials_per_subject"):
    if type(config[key]) is not int or config[key] <= 0:
      raise ValueError(f"{key} must be a positive integer")
  if config["trials_per_condition"] % config["trials_per_subject"]:
    raise ValueError(
      "trials_per_condition must be divisible by trials_per_subject",
    )
  config["blocks"] = (
    config["trials_per_condition"] // config["trials_per_subject"]
  )
  for key in ("cooldown_seconds", "baseline_seconds", "sample_interval_seconds"):
    value = config[key]
    minimum = 0 if key == "cooldown_seconds" else 0.01
    if (
      type(value) not in (int, float)
      or not math.isfinite(value)
      or value < minimum
    ):
      raise ValueError(f"{key} must be at least {minimum} seconds")

  seed = config["seed"]
  if seed is not None and (type(seed) is not int or not 0 <= seed < 2**32):
    raise ValueError("seed must be an integer from 0 through 4294967295")
  config["seed"] = secrets.randbits(32) if seed is None else seed
  priority = config["io_priority"]
  if priority is not None and (
    type(priority) is not int or not 0 <= priority <= 7
  ):
    raise ValueError("io_priority must be between 0 and 7, or null")
  return config


@dataclass
class Target:
  directory: Path
  metadata: dict
  monitor: Monitor

  @classmethod
  def prepare(cls, directory: Path, disk: str | None = None) -> Target:
    directory = directory.expanduser().resolve(strict=True)
    if not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK):
      raise ValueError(f"benchmark directory is not writable: {directory}")
    metadata = inspect_mount(directory, disk)
    return cls(directory, metadata, Monitor(directory, metadata["disk"]))

  @property
  def device_key(self) -> tuple[str, str]:
    if self.metadata["disk"]:
      return controller_identity(self.metadata["disk"])
    return ("mount", self.metadata["mount_major_minor"])


def make_subjects(args: argparse.Namespace, config: dict) -> dict:
  """Build each block's roster of units before assigning their conditions."""
  width = len(config["block_sizes"])
  if not args.subjects:
    target = Target.prepare(args.directory or Path.cwd(), args.disk)
    return {
      f"block-{block:03d}": {
        f"session-{block:03d}-{slot:02d}": target
        for slot in range(1, width + 1)
      }
      for block in range(1, config["blocks"] + 1)
    }

  if args.disk:
    raise ValueError("--disk is available with --directory")
  blocks = {}
  ids = set()
  devices = set()
  with args.subjects.open(newline="", encoding="utf-8-sig") as file:
    reader = csv.DictReader(file)
    if reader.fieldnames != ["subject_id", "block_id", "directory"]:
      raise ValueError("roster header must be: subject_id,block_id,directory")
    for row in reader:
      if None in row or any(
        not value or not value.strip() for value in row.values()
      ):
        raise ValueError(f"incomplete roster row {reader.line_num}")
      subject = row["subject_id"].strip()
      block = row["block_id"].strip()
      if subject in ids:
        raise ValueError(f"duplicate subject_id {subject!r}")
      directory = args.subjects.parent / Path(row["directory"]).expanduser()
      target = Target.prepare(directory)
      if not target.metadata["disk"]:
        raise ValueError(f"{subject!r} must map to exactly one backing disk")
      if target.device_key in devices:
        raise ValueError(f"{subject!r} shares a disk with another subject")
      blocks.setdefault(block, {})[subject] = target
      ids.add(subject)
      devices.add(target.device_key)

  if len(blocks) != config["blocks"]:
    raise ValueError(f"roster needs {config['blocks']} complete blocks")
  for block, group in blocks.items():
    if len(group) != width:
      raise ValueError(f"block {block!r} needs {width} distinct drives")
  return blocks


def make_plan(
  config: dict, blocks: dict, run_id: str, design: str,
) -> list[dict]:
  """Randomize at the subject level, preserving equal allocation per block."""
  rng = random.Random(config["seed"])
  plan = []
  for block_index, (block, subjects) in enumerate(blocks.items(), 1):
    conditions = config["block_sizes"].copy()
    rng.shuffle(conditions)
    assignments = list(zip(subjects, conditions))
    # On one SSD the session slots are chronological. Physical drives can also
    # be shuffled in execution order, independently of assignment.
    if design == "physical_drives":
      rng.shuffle(assignments)
    for position, (subject, condition) in enumerate(assignments, 1):
      for repeat in range(1, config["trials_per_subject"] + 1):
        order = len(plan) + 1
        target = subjects[subject].metadata
        plan.append({
          **{name: target.get(name) for name in PLAN_FIELDS if name in target},
          "run_id": run_id,
          "trial_id": f"trial-{order:05d}",
          "planned_order": order,
          "design": design,
          "seed": config["seed"],
          "block_id": block,
          "block_index": block_index,
          "subject_id": subject,
          "position_in_block": position,
          "trial_in_subject": repeat,
          "block_size": condition,
          "block_size_bytes": config["block_size_bytes"][condition],
          "bytes_requested": config["bytes_requested"],
          "planned_trials_per_condition": config["trials_per_condition"],
          "planned_trials_per_subject": config["trials_per_subject"],
        })
  return plan


# -- Output and execution -----------------------------------------------------

def csv_writer(file, fields: tuple[str, ...]) -> csv.DictWriter:
  writer = csv.DictWriter(file, fieldnames=fields)
  writer.writeheader()
  return writer


def write_json(path: Path, contents: dict) -> None:
  writing = path.with_suffix(".tmp")
  writing.write_text(json.dumps(contents, indent=2) + "\n", encoding="utf-8")
  writing.replace(path)


def write_row(writer: csv.DictWriter, row: dict) -> None:
  writer.writerow({key: row.get(key) for key in writer.fieldnames})


def require_command(name: str) -> str:
  executable = shutil.which(name)
  if executable is None:
    raise ValueError(f"{name} is required on PATH")
  return executable


def command_prefix(config: dict) -> tuple[list[str], str]:
  dd = require_command("dd")
  version = subprocess.run(
    [dd, "--version"], check=True, capture_output=True, text=True,
    env={**os.environ, "LC_ALL": "C"},
  ).stdout.splitlines()[0]
  if "coreutils" not in version.lower():
    raise ValueError(f"GNU coreutils dd is required; found {version!r}")
  if config["io_priority"] is None:
    return [dd], version
  ionice = require_command("ionice")
  return [ionice, "-c", "2", "-n", str(config["io_priority"]), dd], version


def dd_statistics(log: Path) -> tuple[list[dict], dict]:
  """Preserve every GNU dd transfer report and identify its exit summary."""
  progress = []
  result = {}
  for line in log.read_bytes().decode("utf-8", errors="replace").splitlines():
    if match := RECORDS.match(line):
      full, partial, direction = match.groups()
      result[f"records_{direction}_full"] = int(full)
      result[f"records_{direction}_partial"] = int(partial)
    if match := TRANSFER.match(line):
      progress.append({
        "report_index": len(progress) + 1,
        "is_final": 0,
        "bytes_reported": int(match[1]),
        "dd_reported_seconds": float(match[2]),
      })
  if progress:
    progress[-1]["is_final"] = int("records_out_full" in result)
    result["bytes_written"] = progress[-1]["bytes_reported"]
    if progress[-1]["is_final"]:
      result["dd_reported_seconds"] = progress[-1]["dd_reported_seconds"]
  return progress, result


def sample_while_running(
  stop: threading.Event, monitor: Monitor, seconds: float,
  samples: list[Snapshot],
) -> None:
  while not stop.wait(seconds):
    samples.append(monitor.sample())


def stop_dd(process: subprocess.Popen) -> None:
  process.send_signal(signal.SIGINT)
  try:
    process.wait(timeout=10)
  except subprocess.TimeoutExpired:
    process.kill()
    process.wait()


class StopRequest:
  """Interrupt blocking waits and let trial logging finish after a signal."""

  def __init__(self):
    self.requested = False
    self._waiting = False

  def handle_signal(self, _signum, _frame) -> None:
    self.requested = True
    if self._waiting:
      raise KeyboardInterrupt

  def wait(self, operation: Callable, *args) -> bool:
    # Keep the normal blocking wait so polling cannot inflate short dd timings.
    try:
      self._waiting = True
      if self.requested:
        return False
      operation(*args)
      return True
    except KeyboardInterrupt:
      self.requested = True
      return False
    finally:
      self._waiting = False


def run_dd(
  argv: list[str], config: dict, monitor: Monitor, log: Path, stop: StopRequest,
) -> dict:
  """Time dd including its exit and final flush, with a separate sampler."""
  samples = []
  stopped = threading.Event()
  watcher = threading.Thread(
    target=sample_while_running,
    args=(stopped, monitor, config["sample_interval_seconds"], samples),
    daemon=True,
  )
  interrupted = False
  with log.open("wb") as stderr:
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started_utc = utc_now()
    started = time.monotonic()
    process = subprocess.Popen(
      argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
      env={**os.environ, "LC_ALL": "C"}, start_new_session=True,
    )
    try:
      watcher.start()
      if not stop.wait(process.wait):
        interrupted = True
        stop_dd(process)
    finally:
      ended = time.monotonic()
      ended_utc = utc_now()
      stopped.set()
      if watcher.ident is not None:
        watcher.join()
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)

  wall = ended - started
  user = usage_after.ru_utime - usage_before.ru_utime
  system = usage_after.ru_stime - usage_before.ru_stime
  result = {
    "started_utc": started_utc, "ended_utc": ended_utc,
    "wall_seconds": wall, "dd_exit_code": process.returncode,
    "dd_user_cpu_seconds": user, "dd_system_cpu_seconds": system,
    "dd_cpu_pct_one_core": 100 * (user + system) / wall,
  }
  progress, counts = dd_statistics(log)
  result.update(counts)
  if interrupted:
    result.update(status="interrupted", error="benchmark interrupted")
  elif process.returncode != 0:
    result.update(status="failed", error="dd failed; see the stderr log")
  elif result.get("bytes_written") != config["bytes_requested"]:
    result.update(status="failed", error="dd did not report the expected bytes")
  else:
    throughput = result["bytes_written"] / MIB / wall
    result.update(status="ok", throughput_mib_s=throughput)
  return {
    "result": result, "started": started, "ended": ended,
    "during": [sample for sample in samples if sample.time < ended],
    "progress": progress,
  }


def run_trial(
  config: dict, block_size: str, target: Target, prefix: list[str], log: Path,
  prior: dict | None, stop: StopRequest,
) -> tuple[dict, list[dict], list[dict], float | None]:
  fs = os.statvfs(target.directory)
  if fs.f_bavail * fs.f_frsize < config["bytes_requested"]:
    return {
      "status": "failed",
      "error": f"insufficient free space in {target.directory}",
    }, [], [], None

  with tempfile.TemporaryDirectory(
    prefix=".dd-bs-", dir=target.directory,
  ) as tmp:
    payload = Path(tmp) / "payload.bin"
    argv = prefix + [
      "if=/dev/zero", f"of={payload}", f"bs={block_size}",
      f"count={config['bytes_requested']}", "iflag=count_bytes,fullblock",
      "conv=fsync,excl", "status=progress",
    ]
    start_baseline = target.monitor.sample()
    stop.wait(time.sleep, config["baseline_seconds"])
    end_baseline = target.monitor.sample()
    baseline = interval_metrics(start_baseline, end_baseline)
    result = {
      "baseline_window_seconds": baseline["interval_seconds"],
      **{f"pre_{key}": baseline.get(key) for key in METRICS},
      "device_payload_mib_before": prior["payload_bytes"] / MIB if prior else 0,
    }
    if prior:
      result["previous_device_block_size"] = prior["block_size"]
    samples = [{
      **baseline, "phase": "pre", "sample_index": 1,
      "elapsed_trial_seconds": None, "observed_utc": end_baseline.utc,
    }]
    if stop.requested:
      result.update(status="interrupted", error="interrupted before dd launch")
      return result, samples, [], None
    dd = run_dd(argv, config, target.monitor, log, stop)
    post = target.monitor.sample()

  result.update(dd["result"])
  result.update(dd_argv_json=json.dumps(argv), dd_stderr_path=f"dd/{log.name}")
  if prior:
    result["seconds_since_previous_device_trial"] = (
      dd["started"] - prior["ended"]
    )

  samples[0]["elapsed_trial_seconds"] = end_baseline.time - dd["started"]
  previous = end_baseline
  phased = [("during", sample) for sample in dd["during"]]
  phased.append(("post", post))
  for index, (phase, observation) in enumerate(phased, 2):
    metrics = interval_metrics(previous, observation)
    samples.append({
      **metrics, "phase": phase, "sample_index": index,
      "elapsed_trial_seconds": observation.time - dd["started"],
      "observed_utc": observation.utc,
    })
    previous = observation

  for sensor in ("cpu", "ssd"):
    field = f"{sensor}_temp_c"
    observed = [row[field] for row in samples[1:] if row.get(field) is not None]
    result[f"post_{field}"] = samples[-1].get(field)
    result[f"max_{field}"] = max(observed) if observed else None
  return result, samples, dd["progress"], dd["ended"]


def cpu_model() -> str | None:
  for line in (read_text(PROC / "cpuinfo") or "").splitlines():
    key, _, value = line.partition(":")
    if key.strip() in {"model name", "Hardware"}:
      return value.strip()
  return None


def write_trials(
  output: Path, config: dict, plan: list[dict], roster: dict, prefix: list[str],
  metadata: dict, plan_only: bool, stop: StopRequest,
) -> int:
  states = {}
  status = 0
  with ExitStack() as stack:
    files = {}
    writers = {}
    for name, fields in (
      ("trials", TRIAL_FIELDS), ("telemetry", SAMPLE_FIELDS),
      ("dd_progress", PROGRESS_FIELDS),
    ):
      files[name] = stack.enter_context(
        (output / f"{name}.csv").open("w", newline="", encoding="utf-8"),
      )
      writers[name] = csv_writer(files[name], fields)
    if plan_only:
      return 0

    for assignment in plan:
      order = assignment["planned_order"]
      subject = assignment["subject_id"]
      target = roster[assignment["block_id"]][subject]
      block_size = assignment["block_size"]
      requested = config["cooldown_seconds"] if order > 1 else 0
      before_sleep = time.monotonic()
      if requested:
        print(f"Cooling down for {requested:g} s", flush=True)
        stop.wait(time.sleep, requested)
      if stop.requested:
        return 130
      actual = time.monotonic() - before_sleep if requested else 0
      print(f"[{order}/{len(plan)}] {subject}  bs={block_size}", flush=True)

      trial_id = assignment["trial_id"]
      key = target.device_key
      prior = states.get(key)
      attempted_utc = utc_now()
      try:
        result, samples, progress, end_clock = run_trial(
          config, block_size, target, prefix,
          output / "dd" / f"{trial_id}.stderr.log", prior, stop,
        )
      except OSError as exc:
        result = {"status": "failed", "error": str(exc)}
        samples, progress, end_clock = [], [], None

      ids = {"run_id": assignment["run_id"], "trial_id": trial_id}
      for sample in samples:
        write_row(writers["telemetry"], {**ids, **sample})
      for report in progress:
        write_row(writers["dd_progress"], {**ids, **report})
      write_row(writers["trials"], {
        **assignment, **result, "actual_order": order,
        "linux_kernel": metadata["linux_kernel"],
        "attempted_utc": attempted_utc,
        "requested_cooldown_seconds": requested,
        "actual_cooldown_seconds": actual,
      })
      for file in files.values():
        file.flush()

      metadata["attempted_trials"] += 1
      if end_clock is not None:
        states[key] = {
          "ended": end_clock, "block_size": block_size,
          "payload_bytes": (
            (prior["payload_bytes"] if prior else 0)
            + (result.get("bytes_written") or 0)
          ),
        }
      if result["status"] == "ok":
        metadata["successful_trials"] += 1
        print(f"  {result['throughput_mib_s']:.1f} MiB/s", flush=True)
      else:
        print(f"  {result['error']}", file=sys.stderr, flush=True)
        status = 130 if result["status"] == "interrupted" else 1
        break
  return status


def main() -> int:
  cli = argument_parser()
  args = cli.parse_args()
  if platform.system() != "Linux":
    cli.error("this benchmark requires Linux")
  stop = StopRequest()
  signal.signal(signal.SIGTERM, stop.handle_signal)
  signal.signal(signal.SIGINT, stop.handle_signal)
  try:
    config = load_config(args)
    roster = make_subjects(args, config)
    prefix, dd_version = command_prefix(config)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(
      prefix=time.strftime("run-%Y%m%dT%H%M%SZ-", time.gmtime()),
      dir=args.results_dir.resolve(),
    ))
  except (OSError, ValueError, subprocess.CalledProcessError) as exc:
    cli.error(str(exc))

  design = "physical_drives" if args.subjects else "single_drive_sessions"
  plan = make_plan(config, roster, output.name, design)
  targets = {}
  for group in roster.values():
    for target in group.values():
      directory = str(target.directory)
      if directory not in targets:
        targets[directory] = {**target.metadata, **target.monitor.describe()}
  host = platform.uname()
  metadata = {
    "run_id": output.name, "runner_version": VERSION, "design": design,
    "created_utc": utc_now(), "finished_utc": None,
    "linux_kernel": host.release, "kernel_version": host.version,
    "machine": host.machine, "python_version": platform.python_version(),
    "cpu_model": cpu_model(), "logical_cpus": os.cpu_count(),
    "allowed_cpu_count": len(os.sched_getaffinity(0)),
    "dd_version": dd_version, "dd_command_prefix": prefix,
    "argv": sys.argv, "settings": config, "targets": targets,
    "planned_trials": len(plan), "attempted_trials": 0, "successful_trials": 0,
    "status": "plan_only" if args.plan_only else "running",
  }
  (output / "dd").mkdir()
  with (output / "plan.csv").open("w", newline="", encoding="utf-8") as file:
    writer = csv_writer(file, PLAN_FIELDS)
    for trial in plan:
      write_row(writer, trial)
  write_json(output / "run.json", metadata)
  print(f"Results: {output}", flush=True)
  print(f"Seed: {config['seed']} | Planned trials: {len(plan)}", flush=True)

  status = 1
  try:
    status = write_trials(
      output, config, plan, roster, prefix, metadata, args.plan_only, stop,
    )
    if stop.requested:
      status = 130
  finally:
    metadata["finished_utc"] = utc_now()
    success = "plan_only" if args.plan_only else "complete"
    metadata["status"] = {0: success, 130: "interrupted"}.get(status, "failed")
    write_json(output / "run.json", metadata)
  return status


if __name__ == "__main__":
  sys.exit(main())
