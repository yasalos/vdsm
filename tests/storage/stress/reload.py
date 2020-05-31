#!/usr/bin/python3
"""
Reproducer for bug 1837199

Create multiple large vgs and simulate reloading pvs, vgs, and lvs while vgs is
being modified concurrently.


Requiremets
-----------

- python3 - on RHEL 7.8 you can install python36 package.
- About 14 GiB of free space on the host for the default configuration.
- More CPUs will reproduce faster - 8 CPUs seems to be good enough.


Setting up storage
------------------

Create a new directory and run "setup":

    # mkdir testdir
    # cd testdir
    # /path/to/reload.py setup

The comnand creates this structure:

    # tree .
    .
    ├── backing_00
    ├── backing_01
    ├── backing_02
    ├── backing_03
    ├── backing_04
    ├── backing_05
    ├── backing_06
    ├── backing_07
    ├── backing_08
    ├── backing_09
    ├── delay_00 -> /dev/mapper/delay0000000000000000000000000000
    ├── delay_01 -> /dev/mapper/delay0000000000000000000000000001
    ├── delay_02 -> /dev/mapper/delay0000000000000000000000000002
    ├── delay_03 -> /dev/mapper/delay0000000000000000000000000003
    ├── delay_04 -> /dev/mapper/delay0000000000000000000000000004
    ├── delay_05 -> /dev/mapper/delay0000000000000000000000000005
    ├── delay_06 -> /dev/mapper/delay0000000000000000000000000006
    ├── delay_07 -> /dev/mapper/delay0000000000000000000000000007
    ├── delay_08 -> /dev/mapper/delay0000000000000000000000000008
    ├── delay_09 -> /dev/mapper/delay0000000000000000000000000009
    ├── loop_00 -> /dev/loop0
    ├── loop_01 -> /dev/loop1
    ├── loop_02 -> /dev/loop2
    ├── loop_03 -> /dev/loop3
    ├── loop_04 -> /dev/loop4
    ├── loop_05 -> /dev/loop5
    ├── loop_06 -> /dev/loop6
    ├── loop_07 -> /dev/loop7
    ├── loop_08 -> /dev/loop8
    ├── loop_09 -> /dev/loop9

And 10 vgs created from the delay devices:

    # vgs --config 'devices {filter=["a|/dev/mapper/delay|", "r|.*|"]}'
      VG                                   #PV #LV #SN Attr   VSize  VFree
      bz1837199-000000000000000000000-0000   1   0   0 wz--n- <2.00t <2.00t
      bz1837199-000000000000000000000-0001   1   0   0 wz--n- <2.00t <2.00t
      bz1837199-000000000000000000000-0002   1   0   0 wz--n- <2.00t <2.00t
      bz1837199-000000000000000000000-0003   1   0   0 wz--n- <2.00t <2.00t
      bz1837199-000000000000000000000-0004   1   0   0 wz--n- <2.00t <2.00t
      bz1837199-000000000000000000000-0005   1   0   0 wz--n- <2.00t <2.00t
      bz1837199-000000000000000000000-0006   1   0   0 wz--n- <2.00t <2.00t
      bz1837199-000000000000000000000-0007   1   0   0 wz--n- <2.00t <2.00t
      bz1837199-000000000000000000000-0008   1   0   0 wz--n- <2.00t <2.00t
      bz1837199-000000000000000000000-0009   1   0   0 wz--n- <2.00t <2.00t


Running
-------

To run reload tests run:

    # /path/to/reload.py run 2>run.log


This runs one trial, which takes 80-90 minutes. Check the "Stats" logs to get
reloads timings and errors stats.

Here are (reformatted) results from CentOS 7.8 VM:

    2020-05-30 01:26:25,346 INFO    (reload/vg) Stats:
    reloads=3455 errors=170 error_rate=4.92% avg_time=1.369 med_time=1.216
    min_time=0.148 max_time=9.041

    2020-05-30 01:26:25,583 INFO    (reload/lv) Stats:
    reloads=4155 errors=198 error_rate=4.77% avg_time=1.140 med_time=1.092
    min_time=0.147 max_time=6.240

    2020-05-30 01:26:25,622 INFO    (reload/pv) Stats:
    reloads=4756 errors=205 error_rate=4.31% avg_time=0.990 med_time=0.925
    min_time=0.147 max_time=5.961

Here results from Fedora 31:

    2020-05-30 01:33:25,981 INFO    (reload/pv) Stats:
    reloads=3540 errors=0 error_rate=0.00% avg_time=1.558 med_time=1.510
    min_time=0.312 max_time=7.323

    2020-05-30 01:33:25,981 INFO    (reload/lv) Stats:
    reloads=3319 errors=0 error_rate=0.00% avg_time=1.660 med_time=1.722
    min_time=0.304 max_time=7.375

    2020-05-30 01:33:25,998 INFO    (reload/vg) Stats: reloads=2833 errors=0
    error_rate=0.00% avg_time=1.947 med_time=1.904 min_time=0.328
    max_time=10.210


Cleanup
-------

To remove the storage run:

    # /path/to/reload.py teardown

"""

# NOTE: do not import anything from vdsm to make this test useful for LVM
# developers.

import argparse
import glob
import logging
import os
import random
import signal
import subprocess
import threading
import time

# Based on vdsm configuration, adapted to use device mapper delay devices.
CONFIG_TEMPLATE = """
devices {
 preferred_names=["^/dev/mapper/"]
 ignore_suspended_devices=1
 write_cache_state=0
 disable_after_error_count=3
 filter=["a|^/dev/mapper/delay[0-9]+$|", "r|.*|"]
 %(hints)s
} global {
 locking_type=1
 prioritise_write_locks=1
 wait_for_locks=1
 use_lvmetad=0
} backup {
 retain_min=50
 retain_days=0
}"""

# From lib/vdsm/storage/constants.py
TAG_VOL_UNINIT = "OVIRT_VOL_INITIALIZING"
BLANK_UUID = "00000000-0000-0000-0000-000000000000"
REMOVED_IMAGE_PREFIX = "_remove_me_"

VG_PREFIX = "bz1837199"

terminated = threading.Event()


class Terminated(Exception):
    """ Raised during termination """


class Error(Exception):

    def __init__(self, cmd, rc, out, err):
        self.cmd = cmd
        self.rc = rc
        self.out = out
        self.err = err

    def __str__(self):
        return (
            "Command {self.cmd} failed rc={self.rc} out={self.out!r} "
            "err={self.err!r}"
        ).format(self=self)


def main():
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s (%(threadName)s) %(message)s")

    globals()["cmd_" + args.command](args)


def parse_args():
    p = argparse.ArgumentParser()

    p.add_argument(
        "command",
        choices=("setup", "teardown", "run"))

    p.add_argument(
        "--trials",
        type=int,
        default=1,
        help="Number of trials")

    p.add_argument(
        "--vg-count",
        type=int,
        default=10,
        help="Number of vgs")

    p.add_argument(
        "--lv-count",
        type=int,
        default=500,
        help="Number of lvs per vg")

    p.add_argument(
        "--pv-size",
        type=gib,
        default=gib(2048),
        help="Size of pv in GiB")

    p.add_argument(
        "--delay-msec",
        type=int,
        default=10,
        help="Number of milliseconds to delay I/O")

    p.add_argument(
        "--debug",
        action="store_true",
        help="Show debug logs")

    return p.parse_args()


def cmd_setup(args):
    logging.info("Setting up storage args=%s", args)
    lvm_config = make_lvm_config(args)

    for i in range(args.vg_count):
        # Create backing file.
        backing_file = "backing_{:02}".format(i)
        logging.info("Creating backing file %s", backing_file)
        with open(backing_file, "w") as f:
            f.truncate(args.pv_size)

        # Create loop device.
        loop_device = run(["losetup", "--find", "--show", backing_file])
        logging.info("Created loop device %s", loop_device)

        # Create link to device so we can easily remove it later.
        loop_link = "loop_{:02}".format(i)
        logging.info("Creating symlink %s -> %s", loop_link, loop_device)
        os.symlink(loop_device, loop_link)

        # Create a delay device.
        delay_name = make_delay_name(i)
        logging.info("Creating delay device %s", delay_name)
        sectors = int(run(["blockdev", "--getsize", loop_device]))
        table = "0 {} delay {} 0 {}".format(
            sectors, loop_device, args.delay_msec)
        run(["dmsetup", "create", delay_name], input=table.encode("utf-8"))

        # Create link to device so we can easily remove it later.
        pv_name = make_pv_name(i)
        delay_link = "delay_{:02}".format(i)
        logging.info("Creating symlink %s -> %s", delay_link, pv_name)
        os.symlink(pv_name, delay_link)

        # Create a pv.
        logging.info("Creating pv %s", pv_name)
        run(["pvcreate", "--config", lvm_config, "--metadatasize", "128m",
             "--metadatacopies", "2", pv_name])

        # Create a vg.
        vg_name = make_vg_name(i)
        logging.info("Creating vg %s on pv %s", vg_name, pv_name)
        run(["vgcreate", "--config", lvm_config,
             "--physicalextentsize", "128m", vg_name, pv_name])


def cmd_teardown(args):
    logging.info("Tearing down storage args=%s", args)
    lvm_config = make_lvm_config(args)

    # Deactivate lvs.
    out = run(["vgs", "--config", lvm_config, "--noheadings", "-o", "vg_name",
               "--select", "vg_name =~ ^{}-[0-9]+".format(VG_PREFIX)])
    for line in out.splitlines():
        vg_name = line.strip()

        logging.info("Deactivating lvs in vg %s", vg_name)
        run(["vgchange", "--config", lvm_config, "--activate", "n", vg_name])

    # Wipe and remove the devices.
    for delay_link in glob.glob("delay_*"):
        delay_device = os.readlink(delay_link)

        if os.path.exists(delay_device):
            logging.info("Wiping delay device %s", delay_device)
            run(["wipefs", "--all", delay_device])

            delay_name = os.path.basename(delay_device)
            logging.info("Removing delay device %s", delay_name)
            run(["dmsetup", "remove", "--force", delay_name])

        os.unlink(delay_link)

    # Remove the loop devices.
    for loop_link in glob.glob("loop_*"):
        loop_device = os.readlink(loop_link)

        logging.info("Removing loop device %s", loop_device)
        run(["losetup", "--detach", loop_device])

        os.unlink(loop_link)

    # Remove the backing files.
    for backing_file in glob.glob("backing_*"):
        logging.info("Removing backing file %s", backing_file)
        os.unlink(backing_file)


def cmd_run(args):
    logging.info("Running trials args=%s", args)

    register_termination_signals()

    lvm_config = make_lvm_config(args)

    reloaders = []

    logging.info("Starting pv reloader")
    r = threading.Thread(
        target=pv_reloader,
        args=(lvm_config, args),
        daemon=True,
        name="reload/pv",
    )
    r.start()
    reloaders.append(r)

    logging.info("Starting vg reloader")
    r = threading.Thread(
        target=vg_reloader,
        args=(lvm_config, args),
        daemon=True,
        name="reload/vg",
    )
    r.start()
    reloaders.append(r)

    logging.info("Starting lv reloader")
    r = threading.Thread(
        target=lv_reloader,
        args=(lvm_config, args),
        daemon=True,
        name="reload/lv",
    )
    r.start()
    reloaders.append(r)

    workers = []

    for i in range(args.vg_count):
        vg_name = make_vg_name(i)

        logging.info("Starting worker for vg %s", vg_name)
        w = threading.Thread(
            target=worker,
            args=(lvm_config, args, vg_name),
            daemon=True,
            name="worker/{:02}".format(i),
        )
        w.start()
        workers.append(w)

        # Mix workers flows by starting them with a delay.
        time.sleep(1)

    while workers:
        workers[0].join(1.0)
        if not workers[0].is_alive():
            workers.pop(0)

    logging.info("Workers stopped")

    terminated.set()

    while reloaders:
        reloaders[0].join(1.0)
        if not reloaders[0].is_alive():
            reloaders.pop(0)

    logging.info("Reloaders stopped")


def register_termination_signals():
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)


def terminate(signo, frame):
    logging.info("terminating after signal %d", signo)
    terminated.set()


def worker(lvm_config, args, vg_name):
    logging.info("Worker started")

    for trial in range(1, args.trials + 1):
        logging.info("Starting trial %s/%s", trial, args.trials)
        try:
            run_trial(lvm_config, args, vg_name)
        except Terminated:
            logging.info("Trial %s terminated", trial)
            break
        except Exception:
            logging.exception("Trial %s failed", trial)
            break
        else:
            logging.info("Trial %s finished", trial)

    logging.info("Worker finished")


def run_trial(lvm_config, args, vg_name):
    # Create lvs.
    for lv_name in iter_lvs(args):
        create_lv(lvm_config, vg_name, lv_name)
        change_lv_tags(
            lvm_config, vg_name, lv_name,
            rem=[TAG_VOL_UNINIT],
            add=["IU_{}".format(lv_name), "PU_{}".format(BLANK_UUID)])
        deactivate_lv(lvm_config, vg_name, lv_name)

    # Simulate lv usage.
    for lv_name in iter_lvs(args):
        activate_lv(lvm_config, vg_name, lv_name)
        perform_io(vg_name, lv_name)
        extend_lv(lvm_config, vg_name, lv_name)
        deactivate_lv(lvm_config, vg_name, lv_name)

    # Prepare lvs for removal.
    for lv_name in iter_lvs(args):
        change_lv_tags(
            lvm_config, vg_name, lv_name,
            rem=["IU_{}".format(lv_name)],
            add=["IU_{}{}".format(REMOVED_IMAGE_PREFIX, lv_name)])

    # Discard and remove lvs.
    for lv_name in iter_lvs(args):
        activate_lv(lvm_config, vg_name, lv_name)
        discard_lv(vg_name, lv_name)
        deactivate_lv(lvm_config, vg_name, lv_name)
        remove_lv(lvm_config, vg_name, lv_name)


def iter_lvs(args):
    for i in range(args.lv_count):
        if terminated.is_set():
            raise Terminated

        yield make_lv_name(i)


def make_delay_name(i):
    # Generate predictable WWID-like name.
    # 360014053b18095bd13c48158687153a5
    return "delay{:028}".format(i)


def make_pv_name(i):
    return "/dev/mapper/{}".format(make_delay_name(i))


def make_vg_name(i):
    # Generate predictable uuid-like name.
    return "{}-000000000000000000000-{:04}".format(VG_PREFIX, i)


def make_lv_name(i):
    # Generate predictable uuid-like name.
    return "lv-0000000000000000000000000000-{:04}".format(i)


def create_lv(config, vg_name, lv_name):
    logging.info("Creating lv %s/%s", vg_name, lv_name)

    run([
        "lvcreate",
        "--config", config,
        "--autobackup", "n",
        "--contiguous", "n",
        "--size", "1g",
        "--addtag", TAG_VOL_UNINIT,
        "--activate", "y",
        "--name", lv_name,
        vg_name
    ])


def activate_lv(config, vg_name, lv_name):
    logging.info("Activating lv %s/%s", vg_name, lv_name)
    change_lv(config, vg_name, lv_name, activate="y")


def deactivate_lv(config, vg_name, lv_name):
    logging.info("Deactivating lv %s/%s", vg_name, lv_name)
    change_lv(config, vg_name, lv_name, activate="n")


def change_lv_tags(config, vg_name, lv_name, add=(), rem=()):
    logging.info("Changing lv tags %s/%s", vg_name, lv_name)
    change_lv(config, vg_name, lv_name, add_tags=add, del_tags=rem)


def change_lv(config, vg_name, lv_name, activate=None, add_tags=(),
              del_tags=()):
    cmd = [
        "lvchange",
        "--config", config,
        "--autobackup", "n",
    ]

    for tag in add_tags:
        cmd.extend(("--addtag", tag))

    for tag in del_tags:
        cmd.extend(("--deltag", tag))

    if activate:
        cmd.extend(("--activate", activate))

    cmd.append("{}/{}".format(vg_name, lv_name))

    run(cmd)


def extend_lv(config, vg_name, lv_name):
    logging.info("Extending lv %s/%s", vg_name, lv_name)

    run([
        "lvextend",
        "--config", config,
        "--autobackup", "n",
        "--size", "+1g",
        "{}/{}".format(vg_name, lv_name)
    ])


def remove_lv(config, vg_name, lv_name):
    logging.info("Removing %s/%s", vg_name, lv_name)

    run([
        "lvremove",
        "--config", config,
        "--autobackup", "n",
        "--force",
        "{}/{}".format(vg_name, lv_name)
    ])


def discard_lv(vg_name, lv_name):
    lv_device = "/dev/{}/{}".format(vg_name, lv_name)
    logging.info("Discarding lv %s", lv_device)
    run(["blkdiscard", "--step", "32m", lv_device])


def perform_io(vg_name, lv_name):
    lv_device = "/dev/{}/{}".format(vg_name, lv_name)
    logging.info("Doing some I/O with %s", lv_device)

    # Write 2 MiB per lv, total 10 GiB per 5000 lvs.
    run([
        "dd",
        "if=/dev/zero",
        "of=" + lv_device,
        "bs=64k",
        "count=32",
        "oflag=direct",
    ])

    run([
        "dd",
        "if=" + lv_device,
        "of=/dev/null",
        "bs=64k",
        "count=32",
        "iflag=direct",
    ])


def pv_reloader(lvm_config, args):
    logging.info("Reloader started")

    reloads = 0
    errors = 0
    times = []

    while not terminated.is_set():
        pv_number = random.randint(0, args.vg_count - 1)
        pv_name = make_pv_name(pv_number)

        logging.info("Reloading pv %s", pv_name)
        selection = "pv_name = {}".format(pv_name)
        reloads += 1

        start = time.monotonic()
        try:
            run(["pvs", "--config", lvm_config, "--noheadings",
                 "--select", selection])
        except Error as e:
            logging.error("Reloading pv failed: %s", e)
            errors += 1
        finally:
            times.append(time.monotonic() - start)

    log_reload_stats(reloads, errors, times)


def vg_reloader(lvm_config, args):
    logging.info("Reloader started")

    reloads = 0
    errors = 0
    times = []

    while not terminated.is_set():
        vg_number = random.randint(0, args.vg_count - 1)
        vg_name = make_vg_name(vg_number)

        logging.info("Reloading vg %s", vg_name)
        selection = "vg_name = {}".format(vg_name)
        reloads += 1

        start = time.monotonic()
        try:
            run(["vgs", "--config", lvm_config, "--noheadings",
                 "--select", selection])
        except Error as e:
            logging.error("Reloading vg failed: %s", e)
            errors += 1
        finally:
            times.append(time.monotonic() - start)

    log_reload_stats(reloads, errors, times)


def lv_reloader(lvm_config, args):
    logging.info("Reloader started")

    reloads = 0
    errors = 0
    times = []

    while not terminated.is_set():
        vg_number = random.randint(0, args.vg_count - 1)
        vg_name = make_vg_name(vg_number)

        lv_number = random.randint(0, args.lv_count - 1)
        lv_name = make_lv_name(lv_number)

        logging.info("Reloading lv %s/%s", vg_name, lv_name)
        selection = "vg_name = {} && lv_name = {}".format(vg_name, lv_name)
        reloads += 1

        start = time.monotonic()
        try:
            run(["lvs", "--config", lvm_config, "--noheadings",
                 "--select", selection])
        except Error as e:
            logging.error("Reloading lv failed: %s", e)
            errors += 1
        finally:
            times.append(time.monotonic() - start)

    log_reload_stats(reloads, errors, times)


def log_reload_stats(reloads, errors, times):
    times.sort()

    min_time = times[0]
    max_time = times[-1]

    mid = len(times) // 2
    if len(times) % 2:
        med_time = times[mid]
    else:
        med_time = (times[mid - 1] + times[mid]) / 2

    avg_time = sum(times) / len(times)

    logging.info(
        "Stats: reloads=%s errors=%s error_rate=%.2f%% avg_time=%.3f "
        "med_time=%.3f min_time=%.3f max_time=%.3f",
        reloads,
        errors,
        errors / reloads * 100,
        avg_time,
        med_time,
        min_time,
        max_time,
    )


def gib(s):
    return int(s) * 1024**3


def make_lvm_config(args):
    config = CONFIG_TEMPLATE % {
        "hints": 'hints="none"' if lvm_version() == ("2", "03") else "",
    }
    return config.replace("\n", "")


def lvm_version():
    out = run(["lvm", "version"])
    for line in out.splitlines():
        if line.startswith("LVM version:"):
            #  LVM version:     2.03.09(2) (2020-03-26)
            _, _, version, date = line.split(None, 3)
            major, minor, _ = version.split(".")
            return major, minor
    raise RuntimeError("Cannot get LVM version")


def run(args, input=None):
    logging.debug("Running command %s", args)

    p = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if input else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)

    out, err = p.communicate(input=input)

    out = out.decode("utf-8").strip()
    err = err.decode("utf-8").strip()

    logging.debug("Command completed rc=%s out=%r err=%r",
                  p.returncode, out, err)

    if p.returncode != 0:
        raise Error(args, p.returncode, out, err)

    return out


if __name__ == "__main__":
    main()
