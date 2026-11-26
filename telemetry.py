"""Read benchmark covariates from Linux procfs and sysfs."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROC = Path("/proc")
SYS = Path("/sys")
MIB = 1024**2
GIB = 1024**3

INTERVAL_FIELDS = (
  "cpu_busy_pct",
  "cpu_iowait_pct",
  "cpu_steal_pct",
  "disk_read_mib_s",
  "disk_write_mib_s",
  "disk_write_iops",
  "disk_avg_write_request_kib",
  "disk_write_await_ms",
  "disk_active_pct",
)
INSTANT_FIELDS = (
  "cpu_freq_mhz",
  "cpu_temp_c",
  "ssd_temp_c",
  "load1",
  "mem_available_mib",
  "dirty_mib",
  "writeback_mib",
  "disk_in_flight",
  "ac_online",
  "free_gib",
)
METRICS = INTERVAL_FIELDS + INSTANT_FIELDS


def utc_now() -> str:
  """Return an ISO 8601 timestamp in UTC."""
  return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def read_text(path: Path) -> str | None:
  try:
    return path.read_text(encoding="utf-8", errors="replace").strip() or None
  except OSError:
    return None


def read_int(path: Path) -> int | None:
  value = read_text(path)
  return int(value) if value is not None and value.isdecimal() else None


def backing_disks(node: Path) -> set[str]:
  """Walk from a kernel block device to the disks beneath it."""
  if not node.exists():
    return set()
  node = node.resolve()
  if (node / "partition").exists():
    return backing_disks(node.parent)
  slaves = list((node / "slaves").glob("*"))
  if slaves:
    return set().union(*(backing_disks(slave) for slave in slaves))
  if re.fullmatch(r"(dm-|md|loop|ram|zram)\d+", node.name):
    return set()
  return {node.name}


def inspect_mount(directory: Path, disk_override: str | None) -> dict:
  """Resolve the target filesystem and the device used for disk metrics."""
  result = subprocess.run(
    [
      "findmnt", "--json", "--target", str(directory),
      "--output", "TARGET,SOURCE,FSTYPE,MAJ:MIN,OPTIONS",
    ],
    capture_output=True,
    text=True,
    check=True,
  )
  mount = json.loads(result.stdout)["filesystems"][0]
  source = (mount["source"] or "").split("[", 1)[0]
  devices = backing_disks(SYS / "dev/block" / mount["maj:min"])

  # Some filesystems expose an anonymous mount device, including Btrfs.
  if not devices and source.startswith("/dev/"):
    try:
      info = Path(source).stat()
    except OSError:
      info = None
    if info is not None and stat.S_ISBLK(info.st_mode):
      pair = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
      devices = backing_disks(SYS / "dev/block" / pair)

  disk = next(iter(devices)) if len(devices) == 1 else None
  origin = "mount" if disk else "unresolved"
  if disk_override:
    disk = disk_override.removeprefix("/dev/")
    if not re.fullmatch(r"[A-Za-z0-9_.!+-]+", disk) or disk in {".", ".."}:
      raise ValueError("--disk expects a whole-disk kernel name")
    node = SYS / "block" / disk
    if not node.exists() or backing_disks(node) != {disk}:
      raise ValueError(f"{disk!r} is unavailable as a whole disk in sysfs")
    if devices and disk not in devices:
      raise ValueError(f"{disk!r} is outside the detected backing disks")
    origin = "manual"

  node = SYS / "block" / disk if disk else None
  queue = node / "queue" if node else None
  return {
    "directory": str(directory),
    "mount_target": mount["target"],
    "mount_source": mount["source"],
    "mount_major_minor": mount["maj:min"],
    "mount_options": mount["options"],
    "filesystem": mount["fstype"],
    "backing_disks": sorted(devices),
    "disk": disk,
    "disk_mapping": origin,
    "disk_model": read_text(node / "device/model") if node else None,
    "disk_firmware": (
      read_text(node / "device/firmware_rev") or read_text(node / "device/rev")
    ) if node else None,
    "io_scheduler": read_text(queue / "scheduler") if queue else None,
    "max_sectors_kib": read_int(queue / "max_sectors_kb") if queue else None,
    "logical_block_bytes": (
      read_int(queue / "logical_block_size") if queue else None
    ),
    "physical_block_bytes": (
      read_int(queue / "physical_block_size") if queue else None
    ),
    "optimal_io_bytes": read_int(queue / "optimal_io_size") if queue else None,
  }


def controller_identity(disk: str) -> tuple[str, str]:
  """Reject NVMe namespaces that share an identifiable physical SSD."""
  match = re.fullmatch(r"(nvme\d+)(?:c\d+)?n\d+", disk)
  if match:
    controller = match[1]
    serial = read_text(SYS / "class/nvme" / controller / "serial")
    return ("nvme", serial or controller)
  node = SYS / "block" / disk
  serial = read_text(node / "device/serial")
  return ("block", serial or disk)


def cpu_sensor() -> Path | None:
  """Select a CPU package sensor, preferring an actual die temperature."""
  candidates = []
  for hwmon in sorted((SYS / "class/hwmon").glob("hwmon*")):
    name = read_text(hwmon / "name")
    if name not in {"k10temp", "coretemp", "zenpower", "cpu_thermal"}:
      continue
    for path in sorted(hwmon.glob("temp*_input")):
      label_path = path.with_name(path.name.replace("_input", "_label"))
      label = read_text(label_path) or ""
      if label.startswith(("Tdie", "Package id")):
        candidates.append((0, path))
      elif label == "Tctl" or path.name == "temp1_input":
        candidates.append((1, path))
  if candidates:
    return min(candidates)[1]
  types = {"x86_pkg_temp", "cpu-thermal", "cpu_thermal"}
  for zone in sorted((SYS / "class/thermal").glob("thermal_zone*")):
    if read_text(zone / "type") in types:
      return zone / "temp"
  return None


def disk_sensor(disk: str | None) -> Path | None:
  """Find the hwmon sensor associated with the selected storage device."""
  if not disk:
    return None
  node = SYS / "block" / disk
  local = [
    *node.glob("device/hwmon/hwmon*/temp1_input"),
    *node.glob("device/hwmon*/temp1_input"),
  ]
  match = re.fullmatch(r"(nvme\d+)(?:c\d+)?n\d+", disk)
  if match:
    controller = SYS / "class/nvme" / match[1]
    local += list(controller.glob("hwmon*/temp1_input"))
    local += list(controller.glob("device/hwmon/hwmon*/temp1_input"))
  for path in dict.fromkeys(local):
    chip = read_text(path.parent / "name")
    if chip in {"nvme", "drivetemp"} and path.exists():
      return path
  if match:
    for hwmon in sorted((SYS / "class/hwmon").glob("hwmon*")):
      device = hwmon / "device"
      if (
        read_text(hwmon / "name") == "nvme"
        and match[1] in device.resolve().parts
        and (hwmon / "temp1_input").exists()
      ):
        return hwmon / "temp1_input"
  return None


def read_temperature(sensor: Path | None) -> float | None:
  value = read_text(sensor) if sensor else None
  if value is None:
    return None
  try:
    return int(value) / 1000
  except ValueError:
    return None


def cpu_counters() -> tuple[int, ...] | None:
  raw = read_text(PROC / "stat")
  if raw and raw.startswith("cpu "):
    return tuple(map(int, raw.splitlines()[0].split()[1:9]))
  return None


def disk_counters(disk: str | None) -> tuple[int, ...] | None:
  if not disk:
    return None
  raw = read_text(SYS / "block" / disk / "stat")
  if raw:
    return tuple(map(int, raw.split()[:11]))
  return None


def memory_metrics() -> dict[str, float]:
  names = {
    "MemAvailable": "mem_available_mib",
    "Dirty": "dirty_mib",
    "Writeback": "writeback_mib",
  }
  memory = {}
  text = read_text(PROC / "meminfo") or ""
  for line in text.splitlines():
    key, _, value = line.partition(":")
    if key in names:
      memory[names[key]] = int(value.split()[0]) / 1024
  return memory


@dataclass
class Snapshot:
  time: float
  utc: str
  cpu: tuple[int, ...] | None
  disk: tuple[int, ...] | None
  instant: dict


class Monitor:
  """Sample one target. Missing sensors propagate to empty CSV fields."""

  def __init__(self, directory: Path, disk: str | None):
    self.directory = directory
    self.disk = disk
    self.cpu_sensor = cpu_sensor()
    self.disk_sensor = disk_sensor(disk)
    self.policies = sorted((SYS / "devices/system/cpu/cpufreq").glob("policy*"))
    self.ac_paths = [
      supply / "online"
      for supply in sorted((SYS / "class/power_supply").glob("*"))
      if read_text(supply / "type") in {"Mains", "USB", "USB_C", "USB_PD"}
    ]

  def describe(self) -> dict:
    governors = {
      value
      for policy in self.policies
      if (value := read_text(policy / "scaling_governor")) is not None
    }
    return {
      "cpu_temperature_sensor": (
        str(self.cpu_sensor) if self.cpu_sensor else None
      ),
      "ssd_temperature_sensor": (
        str(self.disk_sensor) if self.disk_sensor else None
      ),
      "cpu_scaling_governors": sorted(governors),
      "ac_online_paths": list(map(str, self.ac_paths)),
    }

  def sample(self) -> Snapshot:
    now = time.monotonic()
    timestamp = utc_now()
    cpu = cpu_counters()
    disk = disk_counters(self.disk)
    frequencies = [
      value
      for policy in self.policies
      if (value := read_int(policy / "scaling_cur_freq")) is not None
    ]
    power = [
      value for path in self.ac_paths if (value := read_int(path)) is not None
    ]
    try:
      fs = os.statvfs(self.directory)
      free_gib = fs.f_bavail * fs.f_frsize / GIB
    except OSError:
      free_gib = None
    values = {
      "cpu_freq_mhz": (
        sum(frequencies) / len(frequencies) / 1000 if frequencies else None
      ),
      "cpu_temp_c": read_temperature(self.cpu_sensor),
      "ssd_temp_c": read_temperature(self.disk_sensor),
      "load1": os.getloadavg()[0],
      "disk_in_flight": disk[8] if disk and len(disk) >= 11 else None,
      "ac_online": max(power) if power else None,
      "free_gib": free_gib,
      **memory_metrics(),
    }
    return Snapshot(now, timestamp, cpu, disk, values)


def interval_metrics(before: Snapshot, after: Snapshot) -> dict:
  """Compute interval rates. Linux disk sectors in these counters are 512 B."""
  values = dict(after.instant)
  seconds = after.time - before.time
  values["interval_seconds"] = seconds
  if seconds <= 0:
    return values
  if before.cpu and after.cpu and len(before.cpu) == len(after.cpu) == 8:
    cpu = [max(0, b - a) for a, b in zip(before.cpu, after.cpu)]
    if total := sum(cpu):
      values["cpu_busy_pct"] = 100 * (total - sum(cpu[3:5]) - cpu[7]) / total
      values["cpu_iowait_pct"] = 100 * cpu[4] / total
      values["cpu_steal_pct"] = 100 * cpu[7] / total
  if before.disk and after.disk and len(before.disk) == len(after.disk) == 11:
    disk = [b - a for a, b in zip(before.disk, after.disk)]
    # The in-flight field is a gauge and may decrease. The others here are
    # counters. A negative delta indicates a reset.
    if min(disk[i] for i in (2, 4, 6, 7, 9)) >= 0:
      values.update(
        disk_read_mib_s=disk[2] * 512 / MIB / seconds,
        disk_write_mib_s=disk[6] * 512 / MIB / seconds,
        disk_write_iops=disk[4] / seconds,
        disk_avg_write_request_kib=disk[6] / (2 * disk[4]) if disk[4] else None,
        disk_write_await_ms=disk[7] / disk[4] if disk[4] else None,
        disk_active_pct=100 * disk[9] / (seconds * 1000),
      )
  return values
