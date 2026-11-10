#!/usr/bin/env python3

import subprocess

PACKAGES = [
    "build-essential",
    "git",
    "curl",
    "python3-pip",
    "python3-dev",
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
REPOS = {
    "https://github.com/canonical/yarf.git": "/root/yarf",
}
COMMANDS = [
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