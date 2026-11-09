#!/usr/bin/python3
"""
This script spawns a qemu vm, with a vnc backend, and then runs `yarf`
from the command line, running a test suite on said VM.
Requires the xtightvncviewer binary package for viewing the test run.
"""

import argparse
import json
import logging
import os
import platform
import random
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from time import sleep, time
from urllib.parse import urlparse

import paramiko

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO
)

PATH_TO_SCRIPT = os.path.dirname(os.path.realpath(__file__))


def parse_args():
    """
    Parse command line args
    """
    parser = argparse.ArgumentParser(
        description="""Spawns a qemu vm and runs a test suite on it via yarf.
                       Keys with values set to `null` in json config will be
                       replaced if there's a corresponding key/value pair in
                       the `replacers` variable in this script, otherwise, just the
                       key will be added to the qemu command line."""
    )
    parser.add_argument(
        "--test-suite",
        type=str,
        required=False,
        help="Path to test suite directory",
    )
    parser.add_argument(
        "--view",
        "-v",
        action="store_true",
        help=(
            "Spawn a `vncviewer` session to view the test suite run over"
            "vnc (requires xtightvncviewer package)."
        ),
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help=("Delete ISO when test is complete."),
    )
    parser.add_argument(
        "--cleanup-storage",
        action="store_true",
        help=("Delete VM storage when test is complete."),
    )
    parser.add_argument(
        "--tpm",
        action="store_true",
        help="Emulate a TPM chip when starting qemu",
    )
    parser.add_argument(
        "--iso",
        type=str,
        required=True,
        help="ISO to use; pass a file path to use that file, otherwise pass a URL",
    )
    parser.add_argument(
        "--qemu-args-json",
        type=str,
        required=False,
        default=f"{os.path.dirname(os.path.realpath(__file__))}/qemu-args.json",
        help="path to json file containing qemu args",
    )
    parser.add_argument(
        "--memory",
        type=str,
        required=False,
        # default="8192M",
        default="16384M",
        help="-m in `man qemu-system-$(uname -m)`, default=16384M",
    )
    parser.add_argument(
        "--cores",
        type=str,
        required=False,
        default="8",
        help="-smp in `man qemu-system-$(uname -m)`, default=8",
    )
    parser.add_argument(
        "--image-type",
        type=str,
        required=False,
        default="qcow2",
        help=(
            "qemu image file type - see https://qemu-project.gitlab.io/qemu/system/images.html"
            "#disk-image-file-formats - default=qcow2"
        ),
    )
    parser.add_argument(
        "--storage-prefix",
        type=str,
        required=False,
        default="/tmp/",
        help=("directory path to store the VM disk"),
    )
    parser.add_argument(
        "--image-disk-size",
        type=str,
        required=False,
        default="40G",
        help="Size of qcow2 disk, default=40G",
    )
    parser.add_argument(
        "--spawn-only",
        action="store_true",
        help="Don't run a yarf test, just spawn a VM and do nothing",
    )
    parser.add_argument(
        "--stay-alive",
        action="store_true",
        help="Keep VM alive on test completion",
    )
    parser.add_argument(
        "--archive-dir",
        type=str,
        required=False,
        default=None,
        help="Directory to store all VM artifacts e.g. the disk image in the specified directory",
    )
    parser.add_argument(
        "--suite",
        type=str,
        required=False,
        default=None,
        help="Specify specific robot suite",
    )
    parser.add_argument(
        "--disk-path",
        type=str,
        required=False,
        default=None,
        help="Desired path to VM disk (will use existing disk if it exists)",
    )
    return parser.parse_args()


class QemuYarfRunner:
    """
    Runs yarf tests on a qemu vm with a vnc backend
    """

    # pylint: disable=too-many-instance-attributes

    vm_process = None
    viewer_process = None
    yarf_process = None
    swtpm_process = None
    cleanup = False
    cleanup_storage = False
    stay_alive = False
    archive_dir = None
    ovmf_dir = None
    orig_ovmf_dir = None

    def __init__(self) -> None:
        self.args = parse_args()
        self.orig_ovmf_dir = f"{PATH_TO_SCRIPT}/../ovmf/"
        # pylint: disable=consider-using-with
        self.ovmf_dir = tempfile.TemporaryDirectory()
        self.copy_ovmf_vars()
        self.vnc_port = self.get_vnc_port()
        self.guest_cid = self.get_guest_cid()
        self.cleanup = self.args.cleanup
        self.cleanup_storage = self.args.cleanup_storage
        self.stay_alive = self.args.stay_alive
        self.vnc_viewer = "vncviewer"
        self.iso_path = self.get_iso(self.args.iso)
        self.archive_dir = self.args.archive_dir
        if self.args.disk_path:
            if self.args.disk_path.endswith(".qcow2"):
                self.storage_path = self.args.disk_path
            else:
                logging.debug(
                    "Invalid image type provided: %s - this script only supports qcow2",
                    self.args.disk_path,
                )
                sys.exit(1)
        else:
            self.storage_path = (
                f"{self.args.storage_prefix}/yarf-vm-{self.vnc_port}.qcow2"
            )
        self.recovery_key_path = self.storage_path.replace(
            ".qcow2", "-recovery-key.txt"
        )
        self.replacers = {
            "-cdrom": self.iso_path,
            "-m": self.args.memory,
            "-smp": self.args.cores,
            "-hda": self.storage_path,
            "-vnc": f":{str(self.vnc_port - 5900)},share=ignore",
        }
        self.tpm_dir = Path(
            f"/tmp/hit-emulated-tpm-{str(self.vnc_port - 5900)}/"
        )
        self.tpm_socket = self.tpm_dir / "swtpm-sock"
        self.host_arch = platform.machine()
        self.qemu_executable = f"qemu-system-{self.host_arch}"
        self.qemu_args_json_file = Path(self.args.qemu_args_json)
        self.qemu_args = self.construct_qemu_args()
        self.qemu_command = " ".join([self.qemu_executable, self.qemu_args])
        self.yarf_command = [
            "yarf",
            "--platform=Vnc",
            self.args.test_suite,
            "--outdir",
            "./artifacts",
            "--",
            "--variable",
            f"CID:{self.guest_cid}",
            # f"RECOVERY_KEY_PATH:{self.recovery_key_path}",
        ]
        if self.args.suite:
            self.yarf_command += [
                "--suite",
                # f'"{self.args.suite}"',
                f"{self.args.suite}",
            ]
        logging.info(f"YARF command:\n{self.yarf_command}")
        self.viewer_command = [
            self.vnc_viewer,
            "-shared",
            "-viewonly",
            f"127.0.0.1:{self.vnc_port}",
        ]
        if not self.checks():
            sys.exit(1)
        self.disk_create_command = " ".join(
            [
                "qemu-img",
                "create",
                "-f",
                self.args.image_type,
                self.storage_path,
                self.args.image_disk_size,
            ]
        )

    def copy_ovmf_vars(self):
        """
        Copy ovmf vars to a temporary directory
        """
        shutil.copytree(
            str(self.orig_ovmf_dir),
            str(self.ovmf_dir.name),
            dirs_exist_ok=True,
        )

    def archive_artifacts(self):
        """
        Archives:
            - disk
            - swtpm
            - ovmf vars
        """
        # recreate artifact dir
        archive_dir = Path(self.archive_dir)
        if archive_dir.exists():
            shutil.rmtree(self.archive_dir)
        archive_dir = archive_dir / "reference"
        archive_dir.mkdir(parents=True)
        # copy over disk image to archive directory
        shutil.copy(self.storage_path, self.archive_dir)
        if self.tpm_dir and self.tpm_dir.exists():
            # copy over tpm stuff
            swtpm_archive_dir = archive_dir / "swtpm"
            swtpm_archive_dir.mkdir(parents=True)
            shutil.copytree(str(self.tpm_dir), str(swtpm_archive_dir))
            # copy over OVMF vars
            ovmf_archive_dir = archive_dir / "ovmf"
            ovmf_archive_dir.mkdir(parents=True)
            shutil.copytree(str(self.ovmf_dir), str(ovmf_archive_dir))
            # copy over recovery key
            shutil.copy(self.recovery_key_path, str(archive_dir))

    def __enter__(self) -> "QemuYarfRunner":
        return self

    def __exit__(self, *args) -> None:
        logging.info("Exiting, killing all processes...")
        # kill viewer
        if os.path.exists("./qemu-debug-log"):
            shutil.move("./qemu-debug-log", "./artifacts/qemu-debug-log")
        if self.viewer_process:
            self.viewer_process.kill()
        # kill VM
        if self.vm_process:
            self.vm_process.kill()
        # kill yarf
        if self.yarf_process:
            self.yarf_process.kill()
        # kill swtpm
        if self.swtpm_process:
            self.swtpm_process.kill()
        # archive artifacts
        if self.archive_dir:
            self.archive_artifacts()
        # remove original swtpm dir
        if self.swtpm_process:
            shutil.rmtree(self.tpm_dir)
        # remove live ISO
        if self.cleanup:
            os.remove(self.iso_path)
        # remove storage
        if self.cleanup_storage:
            os.remove(self.storage_path)
        if self.ovmf_dir:
            self.ovmf_dir.cleanup()

    def checks(self) -> bool:
        """
        Check all dependencies are present.
        """
        passed = True
        logging.debug(
            "Checking to see if necessary pre-requisites are available..."
        )
        for executable in [
            self.qemu_executable,
            "qemu-img",
            "yarf",
            self.vnc_viewer if self.args.view else "which",
            "swtpm" if self.args.tpm else "which",
        ]:
            try:
                subprocess.run(
                    f"which {executable}",
                    check=True,
                    shell=True,
                    stdout=subprocess.DEVNULL,
                )
            except subprocess.CalledProcessError:
                logging.error(f"{executable} doesn't exist! Exiting...")
                passed = False
        return passed

    def get_iso(self, path: str):
        """
        Either returns a path to a file, if a url is passed
        to this script, downloads the iso to the current directory.
        """
        parsed = urlparse(path)
        if all([parsed.scheme, parsed.netloc]):
            wget_command = f"wget -nv -c {path}"
            iso_name = path.split("/")[-1]
            for attempt in range(60):
                try:
                    subprocess.run(
                        wget_command,
                        check=True,
                        shell=True,
                    )
                    iso_path = f"./{iso_name}"
                    logging.info(f"ISO is at {iso_path}")
                    return iso_path
                except subprocess.CalledProcessError as e:
                    logging.error(
                        f"Downloading failed with {e}, retrying in {attempt} seconds"
                    )
                    sleep(attempt)
        else:
            logging.info(f"ISO is at {path}")
            return path
        raise TimeoutError(f"Failed to download {path}!")

    def construct_qemu_args(self) -> str:
        """
        Constructs qemu args from json file and replacers
        """
        list_of_args = []
        for arg, value in json.loads(
            self.qemu_args_json_file.read_text(encoding="utf-8")
        ).items():
            if value is None:
                if arg in self.replacers:
                    this_arg = " ".join([arg, self.replacers[arg]])
                else:
                    this_arg = arg
            else:
                this_arg = " ".join([arg, value])
            list_of_args.append(this_arg)
        if self.args.tpm:
            list_of_args += [
                f"-chardev socket,id=chrtpm,path={self.tpm_socket}",
                "-tpmdev emulator,id=tpm0,chardev=chrtpm",
                "-device tpm-tis,tpmdev=tpm0",
                f"-drive if=pflash,format=raw,unit=0,file={self.ovmf_dir.name}/OVMF_CODE_4M.ms.fd,readonly=on",  # pylint: disable=line-too-long
                f"-drive if=pflash,format=raw,unit=1,file={self.ovmf_dir.name}/OVMF_VARS_4M.ms.fd",
                "-machine q35,smm=on",
                "-netdev user,id=netdev1 -device virtio-net-pci,netdev=netdev1,romfile=",
            ]
        # Add a virtio-vsock device to the VM that provides a direct
        # communication channel (in this case to establish an SSH connection)
        # See https://wiki.qemu.org/Features/VirtioVsock for more details.
        list_of_args.append(
            f"-device vhost-vsock-pci,id=vhost-vsock-pci0,guest-cid={self.guest_cid}"
        )
        logging.info(f"qemu args:\n{' '.join(list_of_args)}")
        return " ".join(list_of_args)

    def start_swtpm(self) -> None:
        """
        Starts software tpm emulation subprocess
        """
        self.tpm_dir.mkdir(parents=True, exist_ok=True)
        # pylint: disable=consider-using-with
        self.swtpm_process = subprocess.Popen(
            [
                "swtpm",
                "socket",
                "--tpmstate",
                f"dir={str(self.tpm_dir)}",
                "--ctrl",
                f"type=unixio,path={str(self.tpm_socket)}",
                "--tpm2",
            ],
        )

    def spawn_vm(self) -> None:
        """
        Spawns vm by starting qemu-system subprocess with specified args
        """
        self.create_disk()
        logging.info("Spinning up qemu VM...")
        logging.debug(f"qemu command line: \n{self.qemu_command}")
        # pylint: disable=consider-using-with
        self.vm_process = subprocess.Popen(
            self.qemu_command.split(" "), cwd=self.ovmf_dir.name
        )

    def spawn_viewer(self) -> None:
        """Spawns vncviewer session to already spawned vm"""
        vnc_server_timeout = 30
        logging.info("Waiting for vnc server...")
        start_time = time()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            while s.connect_ex(("127.0.0.1", self.vnc_port)) != 0:
                if (time() - start_time) > vnc_server_timeout:
                    logging.error(
                        f"Failed to connect to vnc server after {vnc_server_timeout}"
                    )
                    sys.exit(1)
        # pylint: disable=consider-using-with
        self.viewer_process = subprocess.Popen(self.viewer_command)
        logging.info("Connected to vnc server and spawned vncviewer session!")

    def create_disk(self) -> None:
        """
        Creates qemu format disk image
        """
        if not os.path.exists(self.storage_path):
            subprocess.run(self.disk_create_command, check=True, shell=True)

    def get_vnc_port(self) -> int:
        """
        Searches for a free port between 5900 and 5999
        """
        ports_range = list(range(5900, 5999))
        while len(ports_range) > 0:
            port_idx = random.randint(0, len(ports_range)) - 1
            port = ports_range[port_idx]
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(("127.0.0.1", port))
                sock.close()
                return port
            except Exception as _:
                pass
            del ports_range[port_idx]
        raise ValueError("No free ports available for a VNC server!")

    def get_guest_cid(self) -> int:
        """
        Returns a random CID between 1000 and 10000
        """
        return random.randint(1000, 10000)

    def collect_installer_logs(self) -> None:
        """
        Copies a tar archive of /var/log/installer to the yarf artifacts directory
        """

        def save_remote_sudo_cmd_output(
            transport: paramiko.Transport, command: str, filename: str
        ):
            session = transport.open_session()
            session.invoke_shell()

            # Give the server some time to send a MOTD and remove it from the
            # input buffer if present
            sleep(1)
            if session.recv_ready():
                session.recv(len(session.in_buffer))

            session.sendall(f'sudo -p "" -S {command}\n'.encode())
            session.sendall("ubuntu\n".encode())
            while not session.recv_ready():
                sleep(0.1)
            with open(filename, "wb") as f:
                while session.recv_ready():
                    chunk = session.recv(0xFFFF)
                    f.write(chunk)
            session.close()

        try:
            sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            sock.connect((self.guest_cid, 22))
            transport = paramiko.Transport(sock)
            transport.connect(username="ubuntu", password="ubuntu")
            save_remote_sudo_cmd_output(
                transport,
                "tar cz -C /var/log/installer .",
                "./artifacts/installer_logs.tar.gz",
            )
            save_remote_sudo_cmd_output(
                transport,
                "sh -c 'journalctl --no-pager|gzip'",
                "./artifacts/journal.gz",
            )
            transport.close()
            sock.close()
        except Exception as e:
            logging.error(f"could not collect installer logs: {e}")

    def run_test_via_yarf(self) -> None:
        """
        Calls yarf, pointing to a test suite, to run on the already spawned VM
        """
        mod_env = os.environ.copy()
        mod_env["VNC_PORT"] = str(self.vnc_port - 5900)
        # pylint: disable=consider-using-with
        self.yarf_process = subprocess.Popen(self.yarf_command, env=mod_env)
        while self.yarf_process.poll() is None:
            logging.info("yarf process still running!")
            sleep(10)
        logging.info(f"poll result: {self.yarf_process.poll()}")
        logging.info(f"Yarf has exited: {self.yarf_process.returncode}")
        self.collect_installer_logs()
        if self.stay_alive:
            while True:
                sleep(1)
        sys.exit(self.yarf_process.returncode)

    def run(self) -> None:
        """
        Equivalent to a `main` function
        """
        if self.args.tpm:
            self.start_swtpm()
            # need an actual check here to see if the socket is up or something
            sleep(10)
        self.spawn_vm()
        if self.args.view:
            self.spawn_viewer()
        # 2025-08-04: I've seen instances where the first connect call to the vnc server
        # fails, so let's add a needlessly long sleep here to (hopefully) avoid this problem
        sleep(10)
        if not self.args.spawn_only:
            self.run_test_via_yarf()
        else:
            while True:
                sleep(10)


if __name__ == "__main__":
    with QemuYarfRunner() as runner:
        runner.run()
