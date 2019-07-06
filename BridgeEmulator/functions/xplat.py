import subprocess
import platform
import logging
import shutil
import sys

def check_output(*args, **kwargs):
    cmd = args[0]

    if (platform.system() == 'Windows'):
        cmd = [cmd[0:cmd.index(' ')], cmd[1+cmd.index(' '):]] if isinstance(cmd, str) else cmd
        cmd = ["wsl", *cmd]

    return subprocess.check_output(cmd, *args[1:], **kwargs)

def Popen(*args, **kwargs):
    cmd = args[0]

    if (platform.system() == 'Windows'):
        cmd = [cmd[0:cmd.index(' ')], cmd[1+cmd.index(' '):]] if isinstance(cmd, str) else cmd
        cmd = ["wsl", *cmd]

    return subprocess.Popen(cmd, *args[1:], **kwargs)

def call(*args, **kwargs):
    cmd = args[0]

    if (platform.system() == 'Windows'):
        cmd = [cmd[0:cmd.index(' ')], cmd[1+cmd.index(' '):]] if isinstance(cmd, str) else cmd
        cmd = ["wsl", *cmd]

    return subprocess.call(cmd, *args[1:], **kwargs)

global linux_compatible
linux_compatible = True

def checkEnvironment():
    logging.info('Running on %s', platform.system())
    if (platform.system() == 'Windows'):
        wsl = shutil.which("wsl.exe")
        linux_compatible = wsl is not None
        has_specified_args = len(sys.argv) >= 2

        logging.info("WSL path: %s", wsl)

        if not linux_compatible and not has_specified_args:
            raise Exception('''Running on Windows requires either that;
            a) the Windows Subsystem for Linux (WSL, aka Bash on Ubuntu on Windows) is installed,
            b) host MAC and IP addresses are passed as command-line arguments in the order shown respectively.

            To install WSL, visit https://docs.microsoft.com/en-us/windows/wsl/install-win10 ''')