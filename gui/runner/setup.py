#!/usr/bin/env python3

import subprocess

PACKAGES = [
    "build-essential",
    "git",
    "curl",
    "python3-pip",
    "python3-paramiko",
    "python3-venv",
    "libxkbcommon-dev",
    "libgl1",
    "tesseract-ocr",
    "python3-tk",
    "ffmpeg",
    "sshpass",
    "socat",
    "qemu-system-x86",
    "qemu-utils",
    "swtpm",
    "ovmf",
]
YARF_REVISION = "d4b9a9be8cf42779465a6e27538e1128fd8bf6e0"
REPOS = {
    "https://github.com/canonical/yarf.git": "/root/yarf",
}
COMMANDS = [
    f"git -C /root/yarf checkout {YARF_REVISION}",
    "python3 -m pip install --break-system-packages uv",
    "uv sync --directory /root/yarf",
    "ln -sf /root/yarf/.venv/bin/yarf /usr/local/bin/yarf",
]


def run(cmd):
    subprocess.run(cmd, shell=True, check=True)


def main():
    run("apt-get update")
    for package in PACKAGES:
        run(f"DEBIAN_FRONTEND=noninteractive apt-get install -y {package}")

    for url, dest in REPOS.items():
        run(f"git clone {url} {dest}")

    for cmd in COMMANDS:
        run(cmd)


if __name__ == "__main__":
    main()