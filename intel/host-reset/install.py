"""Install the explicitly configured root management service; never copy deployment data."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from worker import validate_runtime
from supervisor import secure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, help='Root-owned installation manifest; no credentials')
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        raise ValueError('HOST_RESET_REQUIRES_ROOT')
    source = Path(args.config)
    secure(source)
    config = json.loads(source.read_text())
    validate_runtime(config)
    state = Path(config['stateRoot'])
    if not state.is_absolute() or any(p.is_symlink() for p in [state, *state.parents]) or any(p == state or p in state.parents for p in [Path(config['home']), Path(config['deploymentRoot'])]):
        raise ValueError('HOST_RESET_UNSAFE_CONTROL')
    target = Path('/usr/local/lib/hmc-host-reset')
    if any(p.is_symlink() for p in [target, *target.parents]):
        raise ValueError('HOST_RESET_UNSAFE_CONTROL')
    target.mkdir(mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)
    for name in ['worker.py', 'supervisor.py']:
        shutil.copyfile(Path(__file__).parent / name, target / name)
        os.chmod(target / name, 0o600)
    config_path = Path('/etc/hmc-host-reset.json')
    if config_path.is_symlink():
        raise ValueError('HOST_RESET_UNSAFE_CONTROL')
    if config_path.exists():
        secure(config_path)
        config['deploymentId'] = json.loads(config_path.read_text())['deploymentId']
    else:
        config['deploymentId'] = str(uuid.uuid4())
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    os.chmod(config_path, 0o600)
    unit = Path('/etc/systemd/system/hmc-host-reset.service')
    if unit.is_symlink():
        raise ValueError('HOST_RESET_UNSAFE_CONTROL')
    unit.write_text('[Unit]\nDescription=HMC explicitly enabled factory reset supervisor\nAfter=network-online.target\nWants=network-online.target\n\n'
                    '[Service]\nType=simple\nUser=root\nUMask=0077\nRuntimeDirectory=hmc-host-reset\nRuntimeDirectoryMode=0700\n'
                    'ExecStart=/usr/bin/python3 /usr/local/lib/hmc-host-reset/supervisor.py --config /etc/hmc-host-reset.json\n'
                    'Restart=on-failure\nRestartSec=3\nTimeoutStopSec=10\n\n[Install]\nWantedBy=multi-user.target\n')
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=15)
    subprocess.run(['systemctl', 'enable', '--now', 'hmc-host-reset.service'], check=True, timeout=30)
    print('HMC_HOST_RESET_INSTALLED')


if __name__ == '__main__':
    main()
