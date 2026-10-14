"""Linux deployment data reset. Paths come only from the root-owned installation manifest."""
import json
import os
from pathlib import Path
import shutil
import shlex
import signal
import sqlite3
import subprocess
import time
import re


def validate_runtime(config):
    validate_data(config)
    service = config['service']
    if not re.fullmatch(r'[a-zA-Z0-9_-]+\.service', service):
        raise ValueError('HOST_RESET_INVALID_SERVICE')
    override = Path(config['firstUseOverride'])
    if str(override) != '/etc/systemd/system/' + service + '.d/90-first-use-reset.conf' or any(p.is_symlink() for p in [override, *override.parents]):
        raise ValueError('HOST_RESET_UNSAFE_PATH')
    if not isinstance(config['webPort'], int) or not 1 <= config['webPort'] <= 65535:
        raise ValueError('HOST_RESET_INVALID_PORT')
    if config.get('trustedHost') and not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,250}', config['trustedHost']):
        raise ValueError('HOST_RESET_INVALID_HOST')
    if not isinstance(config['profileManifest'].get('dsh', {}).get('profile', {}).get('bundles'), list):
        raise ValueError('HOST_RESET_INVALID_TEMPLATE')
    if any(not Path(p).is_absolute() for p in config['runnerEntrypoints'] + config['cronCommands']):
        raise ValueError('HOST_RESET_INVALID_RUNNER')


def canonical_directory(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise ValueError('HOST_RESET_UNSAFE_PATH')
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError('HOST_RESET_UNSAFE_PATH')
    return path


def validate_data(config):
    home = canonical_directory(config['home'])
    deployment = canonical_directory(config['deploymentRoot'])
    if home.name != '.dsh' or len(home.parts) < 3 or len(deployment.parts) < 3:
        raise ValueError('HOST_RESET_UNSAFE_PATH')
    protected = [Path(p).resolve() for p in config['protectedRoots']]
    if any(root == p or root in p.parents or p in root.parents for root in [home, deployment] for p in protected):
        raise ValueError('HOST_RESET_PROTECTED_PATH')
    profiles = canonical_directory(str(home / 'profiles'))
    profile = canonical_directory(str(profiles / 'web'))
    canonical_directory(str(profile / 'node_modules'))
    canonical_directory(str(deployment / 'wrapper'))
    for value in config['credentialFiles']:
        path = Path(value)
        if not path.is_absolute() or path.name != '.env' or any(p.is_symlink() for p in path.parents):
            raise ValueError('HOST_RESET_UNSAFE_CREDENTIAL_PATH')
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ValueError('HOST_RESET_UNSAFE_CREDENTIAL_PATH')
    return home, deployment, profile


def remove_entry(path):
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def erase_data(config):
    """Erase admitted business state without copying it; retain installed web artifacts only."""
    home, deployment, profile = validate_data(config)
    manifest = config['profileManifest']
    if not isinstance(manifest.get('dsh', {}).get('profile', {}).get('bundles'), list):
        raise ValueError('HOST_RESET_INVALID_TEMPLATE')
    for path in home.iterdir():
        if path.name != 'profiles':
            remove_entry(path)
    for path in (home / 'profiles').iterdir():
        if path.name != 'web':
            remove_entry(path)
    for path in profile.iterdir():
        if path.name not in ['node_modules', 'pnpm-lock.yaml', 'pnpm-workspace.yaml']:
            remove_entry(path)
    (profile / 'package.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (profile / 'cordis.yml').write_text('[]\n')
    (profile / 'cordis.patch.yml').write_text('[]\n')
    for path in deployment.iterdir():
        if path.name != 'wrapper':
            remove_entry(path)
    for value in config['credentialFiles']:
        remove_entry(Path(value))


def disable_cron(config):
    """Remove exact installed Muse command tokens; unrelated crontab content is retained."""
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    if result.returncode not in [0, 1]:
        raise ValueError('HOST_RESET_CRON_UNREADABLE')
    retained = []
    for line in result.stdout.splitlines(keepends=True):
        if line.strip() and not line.lstrip().startswith('#'):
            try:
                parts = shlex.split(line)
            except ValueError:
                raise ValueError('HOST_RESET_CRON_INVALID') from None
            if any(command in parts for command in config['cronCommands']):
                continue
        retained.append(line)
    subprocess.run(['crontab', '-'], input=''.join(retained), text=True, check=True, capture_output=True)


def processes():
    rows = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            tail = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if tail[0] == 'Z':
                continue  # Reaped by its parent; a zombie cannot write.
            rows[int(entry.name)] = {'parent': int(tail[1]), 'start': tail[19],
                                    'args': (entry / 'cmdline').read_bytes().split(b'\0')}
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    return rows


def writer_tree(config):
    rows = processes()
    roots = {pid for pid, row in rows.items() if any(str(Path(p)).encode() in row['args'] for p in config['runnerEntrypoints'])}
    for pid in rows:
        try:
            env = dict(item.split(b'=', 1) for item in (Path('/proc') / str(pid) / 'environ').read_bytes().split(b'\0') if b'=' in item)
            effective = env.get(b'DSH_HOME') or env.get(b'HOME', b'/root') + b'/.dsh'
            if env.get(b'INTEL_TASK_ID') and env.get(b'INTEL_RUN_ID') and effective.decode() == config['home']:
                roots.add(pid)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    owned = set(roots)
    while True:
        children = {pid for pid, row in rows.items() if row['parent'] in owned}
        if children <= owned:
            return {pid: rows[pid]['start'] for pid in owned}
        owned |= children


def freeze_writers(config):
    owned = {}
    for _ in range(5):
        current = writer_tree(config)
        added = {pid: start for pid, start in current.items() if pid not in owned}
        for pid, start in added.items():
            row = processes().get(pid)
            if row and row['start'] == start:
                try:
                    os.kill(pid, signal.SIGSTOP)
                except ProcessLookupError:
                    pass
        owned.update(current)
        if not added:
            return owned
    stop_writers(owned)
    raise ValueError('HOST_RESET_WRITER_SET_UNSTABLE')


def stop_writers(owned):
    """Fence PID reuse; terminate captured descendants including detached process groups."""
    for sig in [signal.SIGTERM, signal.SIGKILL]:
        for pid, identity in owned.items():
            current = processes().get(pid)
            if current and current['start'] == identity:
                try:
                    os.kill(pid, sig)
                    if sig == signal.SIGTERM:
                        os.kill(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = processes()
            if not any(pid in rows and rows[pid]['start'] == start for pid, start in owned.items()):
                return
            time.sleep(0.1)
    raise ValueError('HOST_RESET_WRITER_STILL_ACTIVE')


def verify_quiescent(config):
    roots = [Path(config['home']), Path(config['deploymentRoot'])]
    for pid in processes():
        if pid == os.getpid():
            continue
        directory = Path('/proc') / str(pid) / 'fd'
        try:
            entries = list(directory.iterdir())
        except (FileNotFoundError, PermissionError):
            continue
        for entry in entries:
            try:
                target = Path(os.readlink(entry).removesuffix(' (deleted)'))
                flags = next(line.split()[1] for line in (Path('/proc') / str(pid) / 'fdinfo' / entry.name).read_text().splitlines() if line.startswith('flags:'))
                if int(flags, 8) & os.O_ACCMODE and any(target == root or root in target.parents for root in roots):
                    raise ValueError('HOST_RESET_UNCONFIRMED_WRITER')
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass


def verify_initial(config):
    home = Path(config['home'])
    if (home / 'sessions').exists() and list((home / 'sessions').glob('**/session.v*.jsonl*')):
        raise ValueError('HOST_RESET_NONEMPTY_SESSION')
    workspace = home / 'storages/workspace.json'
    if workspace.exists():
        state = json.loads(workspace.read_text())['global']
        if any(state.get(key, []) for key in ['workspaceIds', 'archivedSessionIds', 'pinnedSessionIds']):
            raise ValueError('HOST_RESET_NONEMPTY_WORKSPACE')
    memory = home / 'intel-memory/memory.db'
    if memory.exists():
        with sqlite3.connect('file:' + str(memory) + '?mode=ro', uri=True) as db:
            if db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]:
                raise ValueError('HOST_RESET_NONEMPTY_MEMORY')
    credentials = home / '.credentials.yaml'
    if credentials.exists() and any(name in credentials.read_text() for name in ['deepseek', 'openai-codex', 'access_token', 'refresh_token', 'apiKey']):
        raise ValueError('HOST_RESET_CREDENTIAL_REMAINS')
    if any(Path(p).exists() for p in config['credentialFiles']) or any(p.name != 'wrapper' for p in Path(config['deploymentRoot']).iterdir()):
        raise ValueError('HOST_RESET_CONNECTION_REMAINS')


def perform_reset(config, report):
    """Destructive steps run only after path admission and writer shutdown, never under the DSH cgroup."""
    validate_runtime(config)
    report('stopping', None)
    disable_cron(config)
    owned = freeze_writers(config)
    try:
        subprocess.run(['systemctl', 'stop', config['service']], check=True, capture_output=True, timeout=60)
    finally:
        stop_writers(owned)
    stopped = subprocess.run(['systemctl', 'show', config['service'], '-p', 'MainPID', '--value'], check=True, capture_output=True, text=True, timeout=10)
    if stopped.stdout.strip() != '0':
        raise ValueError('HOST_RESET_SERVICE_STILL_ACTIVE')
    if writer_tree(config):
        raise ValueError('HOST_RESET_WRITER_STILL_ACTIVE')
    verify_quiescent(config)
    report('clearing', None)
    erase_data(config)
    override = Path(config['firstUseOverride'])
    override.parent.mkdir(parents=True, exist_ok=True)
    args = ['/usr/bin/env', '-i', 'HOME=' + str(Path(config['home']).parent), 'USER=root',
            'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'DSH_TELEMETRY_DISABLED=1',
            '/usr/local/bin/dsh', '--profile', 'web', '--port', str(config['webPort'])]
    if config.get('trustedHost'):
        args += ['--trusted-host', config['trustedHost']]
    override.write_text('[Service]\nExecStart=\nExecStart=' + shlex.join(args) + '\n')
    subprocess.run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=15)
    subprocess.run(['systemctl', 'start', config['service']], check=True, capture_output=True, timeout=60)
    report('verifying', None)
    import urllib.request
    import urllib.error
    for _ in range(40):
        active = subprocess.run(['systemctl', 'is-active', config['service']], capture_output=True, text=True, timeout=5).stdout.strip() == 'active'
        reachable = False
        try:
            with urllib.request.urlopen('http://127.0.0.1:' + str(config['webPort']) + '/', timeout=2) as response:
                reachable = response.status == 200
        except urllib.error.HTTPError as error:
            reachable = error.code == 401
        except (OSError, urllib.error.URLError):
            pass
        if active and reachable:
            verify_initial(config)
            # Persist erasures and first-use configuration before publishing a durable receipt.
            os.sync()
            report('completed', None)
            return
        time.sleep(0.5)
    raise ValueError('HOST_RESET_FIRST_USE_UNAVAILABLE')
