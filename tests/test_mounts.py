"""Exercise mount attribution with a temporary Linux sysfs tree."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import telemetry


class TestMountDisks(unittest.TestCase):
  def setUp(self):
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.sys = Path(temporary.name)
    sys_patch = patch.object(telemetry, "SYS", self.sys)
    sys_patch.start()
    self.addCleanup(sys_patch.stop)

    self.fsid = "a05e070d-4bef-4967-b986-326ebfe128c8"
    self.fs = self.sys / "fs/btrfs" / self.fsid
    (self.fs / "devices").mkdir(parents=True)
    (self.fs / "devinfo").mkdir()
    (self.sys / "block").mkdir()
    (self.sys / "dev/block").mkdir(parents=True)
    self.mount = {
      "target": "/benchmark", "source": "/dev/bench-first[/@data]",
      "fstype": "btrfs", "uuid": self.fsid, "maj:min": "0:42", "options": "rw",
    }

  def device(self, name, number, parent=None):
    node = (parent or self.sys / "devices") / name
    node.mkdir(parents=True)
    (node / "dev").write_text(number)
    (self.sys / "dev/block" / number).symlink_to(node)
    if parent:
      (node / "partition").write_text("1")
    else:
      (self.sys / "block" / name).symlink_to(node)
    return node

  def member(self, devid, node=None, missing=0):
    info = self.fs / "devinfo" / str(devid)
    info.mkdir()
    (info / "missing").write_text(str(missing))
    if node:
      (self.fs / "devices" / node.name).symlink_to(node)

  def inspect(self, disk=None):
    with patch.object(telemetry.subprocess, "run") as findmnt:
      findmnt.return_value.stdout = json.dumps({"filesystems": [self.mount]})
      return telemetry.inspect_mount(Path(self.mount["target"]), disk)

  def test_two_drives_require_explicit_telemetry_disk(self):
    first = self.device("bench-a", "8:0")
    second = self.device("bench-b", "8:16")
    self.device("bench-c", "8:32")
    self.member(1, self.device("bench-a1", "8:1", first))
    self.member(2, second)
    mount = self.inspect()
    self.assertEqual(mount["backing_disks"], ["bench-a", "bench-b"])
    self.assertIsNone(mount["disk"])
    self.assertEqual(mount["disk_mapping"], "unresolved")
    manual = self.inspect("/dev/bench-b")
    self.assertEqual((manual["disk"], manual["disk_mapping"]), (
      "bench-b", "manual",
    ))
    with self.assertRaisesRegex(ValueError, "outside the detected backing"):
      self.inspect("bench-c")

  def test_members_on_one_drive_through_a_mapper(self):
    disk = self.device("bench-a", "8:0")
    first = self.device("bench-a1", "8:1", disk)
    second = self.device("bench-a2", "8:2", disk)
    mapper = self.device("dm-0", "253:0")
    (mapper / "slaves").mkdir()
    (mapper / "slaves" / first.name).symlink_to(first)
    self.member(1, mapper)
    self.member(2, second)
    self.mount["uuid"] = None
    with patch.object(telemetry, "source_device_number", return_value="253:0"):
      mount = self.inspect()
    self.assertEqual(mount["backing_disks"], ["bench-a"])
    self.assertEqual((mount["disk"], mount["disk_mapping"]),
                     ("bench-a", "mount"))

  def test_degraded_or_unverified_membership(self):
    disk = self.device("bench-a", "8:0")
    self.member(1, disk)
    self.assertEqual(self.inspect()["disk"], "bench-a")
    self.member(2, missing=1)
    self.assertIsNone(self.inspect()["disk"])
    self.assertEqual(self.inspect("bench-a")["disk_mapping"], "manual")
    # Even matching counts cannot establish that every member is present.
    second = self.device("bench-b", "8:16")
    (self.fs / "devices" / second.name).symlink_to(second)
    self.assertEqual(self.inspect()["backing_disks"], [])
    (self.fs / "devinfo/2/missing").unlink()
    self.assertEqual(self.inspect()["backing_disks"], [])

  def test_untraceable_mapper_branch(self):
    disk = self.device("bench-a", "8:0")
    loop = self.device("loop0", "7:0")
    mapper = self.device("dm-0", "253:0")
    (mapper / "slaves").mkdir()
    for slave in (disk, loop):
      (mapper / "slaves" / slave.name).symlink_to(slave)
    self.member(1, mapper)
    self.assertIsNone(self.inspect()["disk"])

  def test_other_filesystem_uses_mount_device_number(self):
    disk = self.device("bench-a", "8:0")
    self.device("bench-a1", "8:1", disk)
    self.mount.update({"fstype": "ext4", "maj:min": "8:1", "uuid": None})
    self.assertEqual(self.inspect()["disk"], "bench-a")
