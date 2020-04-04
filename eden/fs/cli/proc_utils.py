#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

# pyre-strict

import abc
import logging
import os
import platform
import subprocess
import sys
import time
import typing
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Tuple


log: logging.Logger = logging.getLogger("eden.fs.cli.proc_utils")
ProcessID = int


class BuildInfo(NamedTuple):
    package_name: str = ""
    package_version: str = ""
    package_release: str = ""
    revision: str = ""
    upstream_revision: str = ""
    build_time: int = 0


class EdenFSProcess(NamedTuple):
    pid: ProcessID
    uid: int
    cmdline: List[bytes]
    eden_dir: Optional[Path]

    # holding_lock indicates if this EdenFS process is currently holding the lock
    # on the state directory.  This is set to True or False if we could tell if the
    # process was holding the lock, or None if we could not tell.
    #
    # Normally this should only ever be False if the lock file (or the entire Eden state
    # directory) is deleted out from under a running EdenFS process.  Current releases
    # of EdenFS will detect this and exit if their lock file is deleted, but older
    # versions of EdenFS would continue to run in this state.
    holding_lock: Optional[bool] = None

    def get_build_info(self) -> BuildInfo:
        """
        Get build information for this EdenFS process.

        Note that the various build info fields may not be populated: development builds
        that are not part of a release will not have build info set, and in some cases
        we may not be able to determine build information.  (We could return an
        Optional[BuildInfo] here, but there doesn't seem to be much value in
        distinguishing failure to get build info vs dev builds that have an empty
        BuildInfo.)
        """
        info = get_build_info_from_pid(self.pid)
        if info is None:
            return BuildInfo()
        return info


try:
    from common.base.pid_info.py import build_info_lib  # @manual

    def get_build_info_from_pid(pid: int) -> Optional[BuildInfo]:
        build_info_dict = build_info_lib.get_build_info_from_pid(pid)
        return BuildInfo(
            package_name=typing.cast(str, build_info_dict.get("package_name", "")),
            package_version=typing.cast(
                str, build_info_dict.get("package_version", "")
            ),
            package_release=typing.cast(
                str, build_info_dict.get("package_release", "")
            ),
            revision=typing.cast(str, build_info_dict.get("revision", "")),
            upstream_revision=typing.cast(
                str, build_info_dict.get("upstream_revision", "")
            ),
            build_time=typing.cast(int, build_info_dict.get("time", 0)),
        )


except ImportError:

    def get_build_info_from_pid(pid: int) -> Optional[BuildInfo]:
        # TODO: We could potentially try making a getExportedValues() thrift call to the
        # process if get_build_info_from_pid() is unavailable.
        return None


class ProcUtils(abc.ABC):
    """ProcUtils provides APIs for querying running processes on the system.

    This API helps abstract out platform-specific logic that varies across Linux, Mac,
    and Windows.  These APIs are grouped together in class (instead of just standalone
    functions) primarily to make it easier to stub out this logic during unit tests.
    """

    @abc.abstractmethod
    def get_edenfs_processes(self) -> Iterable[EdenFSProcess]:
        """Returns a list of running EdenFS processes on the system."""
        raise NotImplementedError()

    @abc.abstractmethod
    def get_process_start_time(self, pid: int) -> float:
        """Get the start time of the process, in seconds since the Unix epoch."""
        raise NotImplementedError()

    @abc.abstractmethod
    def is_process_alive(self, pid: int) -> bool:
        """Return true if a process is currently running with the specified
        process ID.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def is_edenfs_process(self, pid: int) -> bool:
        """Heuristically check if the specified process ID looks like a running
        EdenFS process.  This is primarily used by the health checking code
        if we find an existing EdenFS pid but cannot communicate with it over thrift.

        This should return False if no process exists with this process ID.
        If the process ID exists it should ideally attempt to determine if it looks like
        an EdenFS process or not, and return True only if the process appears to be an
        EdenFS instance.  However, the output is primarily used just for diagnostic
        reporting, so false positives are acceptable.
        """
        raise NotImplementedError()

    def read_lock_file(self, path: Path) -> bytes:
        """Read an EdenFS lock file.
        This method exists primarily to allow it to be overridden in test cases.
        """
        return path.read_bytes()


class UnixProcUtils(ProcUtils):
    def is_process_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            # Still running
            return True
        except OSError as ex:
            import errno

            if ex.errno == errno.ESRCH:
                # The process has exited
                return False
            elif ex.errno == errno.EPERM:
                # The process is still running but we don't have permissions
                # to send signals to it
                return True
            # Any other error else is unexpected
            raise

    def is_edenfs_process(self, pid: int) -> bool:
        comm = self._get_process_command(pid)
        if comm is None:
            return False

        # Note that the command may be just "edenfs" rather than a path, but it
        # works out fine either way.
        return os.path.basename(comm) in ("edenfs", "fake_edenfs")

    def _get_process_command(self, pid: int) -> Optional[str]:
        try:
            stdout = subprocess.check_output(["ps", "-p", str(pid), "-o", "comm="])
        except subprocess.CalledProcessError:
            return None

        return stdout.rstrip().decode("utf8")


class MacProcUtils(UnixProcUtils):
    def get_edenfs_processes(self) -> Iterable[EdenFSProcess]:
        return []

    def get_process_start_time(self, pid: int) -> float:
        raise NotImplementedError(
            "NopProcUtils does not currently implement get_process_start_time()"
        )


class LinuxProcUtils(UnixProcUtils):
    proc_path = Path("/proc")
    _system_boot_time: Optional[float] = None
    _jiffies_per_sec: Optional[int] = None

    def get_edenfs_processes(self) -> Iterable[EdenFSProcess]:
        """Return information about all running EdenFS processes.

        This returns information about processes owned by all users.  The returned
        `EdenFSProcess` objects indicate the UID of the user running each process.
        You can filter the results based on this if you only care about processes owned
        by a specific user.
        """
        for entry in os.listdir(self.proc_path):
            # Ignore entries that do not look like integer process IDs
            try:
                pid = int(entry)
            except ValueError:
                continue

            pid_path = self.proc_path / entry
            try:
                # Ignore processes owned by root, to avoid matching privhelper processes
                # D20199409 changes the privhelper to report its name as
                # "edenfs_privhelp", but in older versions of EdenFS the privhelper
                # process also showed up with a command name of "edenfs".  Once we are
                # sure no old privhelper processes from older versions of EdenFS remain
                # we can drop this check.
                st = self.stat_process_dir(pid_path)
                if st.st_uid == 0:
                    continue

                # Ignore processes that aren't edenfs
                comm = (pid_path / "comm").read_bytes()
                if comm != b"edenfs\n":
                    continue

                cmdline_bytes = (pid_path / "cmdline").read_bytes()
            except OSError:
                # Ignore any errors we encounter reading from the /proc files.
                # For instance, this could happen if the process exits while we are
                # trying to read its data.
                continue

            cmdline = cmdline_bytes.split(b"\x00")
            eden_dir, holding_lock = self.get_eden_dir(pid)
            yield self.make_edenfs_process(
                pid=pid,
                uid=st.st_uid,
                cmdline=cmdline,
                eden_dir=eden_dir,
                holding_lock=holding_lock,
            )

    def stat_process_dir(self, path: Path) -> os.stat_result:
        """Call lstat() on a /proc/PID directory.
        This exists as a separate method solely to allow it to be overridden in unit
        tests.
        """
        return path.lstat()

    def get_eden_dir(self, pid: ProcessID) -> Tuple[Optional[Path], Optional[bool]]:
        # In case the state directory was not specified on the command line we can
        # look at the open FDs to find the state directory
        fd_dir = self.proc_path / str(pid) / "fd"
        try:
            for entry in fd_dir.iterdir():
                try:
                    dest = os.readlink(entry)
                except OSError:
                    continue
                if dest.endswith("/lock"):
                    return Path(dest).parent, True
                if dest.endswith("/lock (deleted)"):
                    return Path(dest).parent, False
        except OSError:
            # We may not have permission to read the fd directory
            pass

        log.debug(f"could not determine edenDir for edenfs process {pid}")
        return None, None

    def make_edenfs_process(
        self,
        pid: int,
        uid: int,
        cmdline: List[bytes],
        eden_dir: Optional[Path],
        holding_lock: Optional[bool],
    ) -> EdenFSProcess:
        return EdenFSProcess(
            pid=pid,
            cmdline=cmdline,
            eden_dir=eden_dir,
            uid=uid,
            holding_lock=holding_lock,
        )

    def get_process_start_time(self, pid: int) -> float:
        stat_path = self.proc_path / str(pid) / "stat"
        stat_data = stat_path.read_bytes()
        pid_and_cmd, partition, fields_str = stat_data.rpartition(b") ")
        if not partition:
            raise ValueError("unexpected data in {stat_path}: {stat_data!r}")
        try:
            fields = fields_str.split(b" ")
            jiffies_after_boot = int(fields[19])
        except (ValueError, IndexError):
            raise ValueError("unexpected data in {stat_path}: {stat_data!r}")

        seconds_after_boot = jiffies_after_boot / self.get_jiffies_per_sec()
        return self.get_system_boot_time() + seconds_after_boot

    def get_system_boot_time(self) -> float:
        boot_time = self._system_boot_time
        if boot_time is None:
            uptime_seconds = self._read_system_uptime()
            boot_time = time.time() - uptime_seconds
            self._system_boot_time = boot_time
        return boot_time

    def _read_system_uptime(self) -> float:
        uptime_line = (self.proc_path / "uptime").read_text()
        return float(uptime_line.split(" ", 1)[0])

    def get_jiffies_per_sec(self) -> int:
        jps = self._jiffies_per_sec
        if jps is None:
            jps = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            self._jiffies_per_sec = jps
        return jps


def new() -> ProcUtils:
    if sys.platform.startswith("linux"):
        return LinuxProcUtils()
    elif sys.platform == "darwin":
        return MacProcUtils()
    elif sys.platform == "win32":
        from . import proc_utils_win

        return proc_utils_win.WinProcUtils()
    raise Exception("unsupported platform: {sys.platform!r}")
