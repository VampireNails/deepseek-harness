"""Root-only Linux reset supervisor, independent of the managed DSH service cgroup."""
import argparse
import hmac
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socketserver
import ssl
import subprocess
import threading
import time
from worker import perform_reset, validate_data, validate_runtime

SCOPE = 'host-and-client-first-deployment-no-backup'
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
PHASES = {'prepared', 'running', 'completed', 'failed', 'cancelled', 'acknowledged'}


def atomic(path, value):
    temporary = path.with_suffix('.tmp')
    with open(temporary, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as out:
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def secure(path, directory=False):
    info = path.lstat()
    if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077 or (directory and not path.is_dir()):
        raise ValueError('HOST_RESET_UNSAFE_CONTROL')


class Conflict(ValueError):
    pass


class Manager:
    def __init__(self, config, executor=perform_reset):
        self.config = config
        self.executor = executor
        self.root = Path(config['stateRoot'])
        self.lock = threading.RLock()
        self.servers = {}
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.operations = self.root / 'operations'
        self.operations.mkdir(mode=0o700, exist_ok=True)
        for directory in self.operations.iterdir():
            if not UUID.fullmatch(directory.name) or directory.is_symlink():
                raise ValueError('HOST_RESET_UNSAFE_CONTROL')
            operation = self.load(directory.name)
            if operation['phase'] == 'running':
                operation.update(phase='failed', code='HOST_RESET_INTERRUPTED')
                self.save(operation)
            if operation['phase'] != 'acknowledged':
                self.listen(operation)
            else:
                for name in ['key.pem', 'cert.pem']:
                    (directory / name).unlink(missing_ok=True)

    def load(self, identity):
        if not UUID.fullmatch(identity):
            raise ValueError('HOST_RESET_INVALID_OPERATION')
        directory = self.operations / identity
        if directory.is_symlink():
            raise ValueError('HOST_RESET_UNSAFE_CONTROL')
        value = json.loads((directory / 'operation.json').read_text())
        if value.get('operationId') != identity or value.get('deploymentId') != self.config['deploymentId'] or value.get('phase') not in PHASES:
            raise ValueError('HOST_RESET_INVALID_OPERATION')
        return value

    def save(self, operation):
        atomic(self.operations / operation['operationId'] / 'operation.json', operation)

    def status(self, operation):
        return {key: operation[key] for key in ['version', 'deploymentId', 'operationId', 'phase', 'step', 'code', 'hostVerified', 'backup']}

    def prepare(self, identity):
        if not isinstance(identity, str) or not UUID.fullmatch(identity):
            raise ValueError('HOST_RESET_INVALID_OPERATION')
        with self.lock:
            directory = self.operations / identity
            if directory.exists():
                operation = self.load(identity)
                if operation['phase'] != 'prepared':
                    raise Conflict('HOST_RESET_OPERATION_ALREADY_COMMITTED')
            else:
                for path in self.operations.iterdir():
                    previous = self.load(path.name)
                    if previous['phase'] == 'cancelled':
                        previous.update(phase='acknowledged', step='acknowledged')
                        previous.pop('token', None)
                        self.save(previous)
                        self.retire(path.name, delay=0)
                    elif previous['phase'] != 'acknowledged':
                        raise Conflict('HOST_RESET_OPERATION_ACTIVE')
                validate_data(self.config)
                directory.mkdir(mode=0o700)
                token = secrets.token_urlsafe(32)
                address = self.config['progressHost']
                try:
                    ipaddress.ip_address(address)
                    san = 'IP:' + address
                except ValueError:
                    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,250}', address):
                        raise ValueError('HOST_RESET_INVALID_BIND') from None
                    san = 'DNS:' + address
                operation = {'version': 1, 'deploymentId': self.config['deploymentId'], 'operationId': identity,
                             'phase': 'prepared', 'step': 'prepared', 'code': None, 'hostVerified': False,
                             'backup': False, 'token': token, 'preparedAt': int(time.time())}
                try:
                    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '3650',
                                    '-subj', '/CN=hmc-reset-progress', '-addext', 'subjectAltName=' + san,
                                    '-keyout', str(directory / 'key.pem'), '-out', str(directory / 'cert.pem')],
                                   check=True, capture_output=True, timeout=15)
                    for name in ['key.pem', 'cert.pem']:
                        os.chmod(directory / name, 0o600)
                        with open(directory / name, 'rb') as persisted:
                            os.fsync(persisted.fileno())
                    self.save(operation)
                    self.listen(operation)
                except Exception:
                    # Only a newly allocated control directory; no business data exists here.
                    shutil.rmtree(directory)
                    raise
            return {'version': 1, 'scope': SCOPE, 'deploymentId': operation['deploymentId'],
                    'operationId': identity, 'phase': operation['phase'], 'backup': False,
                    'progress': {'baseUrl': 'https://' + self.config['progressHost'] + ':' + str(self.servers[identity].server_address[1]),
                                 'token': operation['token'], 'certificate': (directory / 'cert.pem').read_text()}}

    def command(self, identity, endpoint, args):
        if not isinstance(args, dict) or args.get('operationId') != identity:
            raise ValueError('HOST_RESET_INVALID_OPERATION')
        with self.lock:
            operation = self.load(identity)
            if endpoint == 'host/reset/status' and set(args) == {'operationId'}:
                return self.status(operation)
            if endpoint == 'host/reset/commit' and set(args) == {'operationId', 'confirmation'} and args['confirmation'] == 'RESET_ALL_NO_BACKUP':
                if operation['phase'] in ['running', 'completed']:
                    return self.status(operation)
                if operation['phase'] != 'prepared':
                    raise Conflict('HOST_RESET_OPERATION_FAILED')
                if time.time() - operation['preparedAt'] > 900:
                    raise Conflict('HOST_RESET_PREPARATION_EXPIRED')
                # Durable admission precedes execution. Repeated/late commits never spawn another reset.
                operation.update(phase='running', step='accepted')
                self.save(operation)
                threading.Thread(target=self.execute, args=(identity,), daemon=True).start()
                return self.status(operation)
            if endpoint == 'host/reset/cancel' and set(args) == {'operationId'}:
                if operation['phase'] not in ['prepared', 'cancelled']:
                    raise Conflict('HOST_RESET_ALREADY_STARTED')
                operation.update(phase='cancelled', step='cancelled')
                self.save(operation)
                return self.status(operation)
            if endpoint == 'host/reset/ack' and set(args) == {'operationId'}:
                if operation['phase'] not in ['completed', 'cancelled', 'acknowledged'] or (operation['phase'] == 'completed' and not operation['hostVerified']):
                    raise Conflict('HOST_RESET_NOT_COMPLETED')
                result = self.status(operation)
                operation.update(phase='acknowledged', step='acknowledged')
                operation.pop('token', None)
                self.save(operation)
                threading.Thread(target=self.retire, args=(identity,), daemon=True).start()
                return result
        raise ValueError('HOST_RESET_INVALID_REQUEST')

    def execute(self, identity):
        def report(step, code):
            with self.lock:
                operation = self.load(identity)
                operation.update(step=step, code=code)
                if step == 'completed':
                    operation.update(phase='completed', hostVerified=True)
                self.save(operation)
        try:
            import fcntl
            with open(self.root / 'execution.lock', 'a') as fence:
                os.chmod(self.root / 'execution.lock', 0o600)
                fcntl.flock(fence, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.executor(self.config, report)
        except Exception as error:
            code = str(error)
            if not re.fullmatch(r'HOST_RESET_[A-Z_]{1,60}', code):
                code = 'HOST_RESET_EXECUTION_FAILED'
            with self.lock:
                operation = self.load(identity)
                operation.update(phase='failed', step=operation.get('step', 'accepted'), code=code, hostVerified=False)
                self.save(operation)

    def retire(self, identity, delay=2):
        time.sleep(delay)
        with self.lock:
            server = self.servers.pop(identity, None)
            if server:
                server.shutdown()
                server.server_close()
            for name in ['key.pem', 'cert.pem']:
                (self.operations / identity / name).unlink(missing_ok=True)

    def listen(self, operation):
        manager = self
        identity = operation['operationId']
        token = operation['token']
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def reply(self, status, value):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def allowed(self):
                actual = self.headers.get('Authorization', '')
                return not self.headers.get('Origin') and not self.headers.get('Sec-Fetch-Site') and hmac.compare_digest(actual.encode(), ('Bearer ' + token).encode())
            def do_GET(self):
                if not self.allowed():
                    self.reply(401, {'error': {'code': 'UNAUTHORIZED'}})
                elif self.path == '/v1/capabilities':
                    self.reply(200, {'protocol': 1, 'unary': ['host/reset/status', 'host/reset/commit', 'host/reset/cancel', 'host/reset/ack'], 'streams': [], 'features': {'factoryResetProgress': True}})
                else:
                    self.reply(404, {'error': {'code': 'NOT_FOUND'}})
            def do_POST(self):
                if not self.allowed():
                    self.reply(401, {'error': {'code': 'UNAUTHORIZED'}})
                    return
                try:
                    if self.path != '/v1/rpc' or self.headers.get('Transfer-Encoding') or not 0 < int(self.headers.get('Content-Length', '0')) <= 4096:
                        raise ValueError('HOST_RESET_INVALID_REQUEST')
                    value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if not isinstance(value, dict) or set(value) != {'endpoint', 'args'}:
                        raise ValueError('HOST_RESET_INVALID_REQUEST')
                    result = manager.command(identity, value['endpoint'], value['args'])
                    self.reply(200, {'ok': True, 'value': result})
                except Conflict as error:
                    self.reply(409, {'error': {'code': str(error)}})
                except (ValueError, KeyError, TypeError):
                    self.reply(400, {'error': {'code': 'HOST_RESET_INVALID_REQUEST'}})
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.operations / identity / 'cert.pem', self.operations / identity / 'key.pem')
        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            slots = threading.BoundedSemaphore(16)
            def process_request(self, request, address):
                if not self.slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, address)
                except Exception:
                    self.slots.release()
                    raise
            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    self.slots.release()
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(5)
                # Handshake in the request thread, keeping accepts available during disconnects.
                return context.wrap_socket(connection, server_side=True, do_handshake_on_connect=False), address
        server = Server((self.config['progressHost'], self.config['progressPort']), Handler)
        self.servers[identity] = server
        threading.Thread(target=server.serve_forever, daemon=True).start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        raise ValueError('HOST_RESET_REQUIRES_ROOT')
    file = Path(args.config)
    secure(file)
    config = json.loads(file.read_text())
    validate_runtime(config)
    if not UUID.fullmatch(config['deploymentId']) or not isinstance(config['progressPort'], int) or not 1 <= config['progressPort'] <= 65535:
        raise ValueError('HOST_RESET_INVALID_CONTROL')
    ipaddress.IPv4Address(config['progressHost'])
    state = Path(config['stateRoot'])
    if not state.is_absolute() or any(p.is_symlink() for p in [state, *state.parents]) or any(p == state or p in state.parents for p in [Path(config['home']), Path(config['deploymentRoot'])]):
        raise ValueError('HOST_RESET_UNSAFE_CONTROL')
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    secure(state, True)
    import fcntl
    singleton = open(state / 'supervisor.lock', 'a')
    os.chmod(state / 'supervisor.lock', 0o600)
    fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manager = Manager(config)
    path = Path(config['socket'])
    path.parent.mkdir(mode=0o700, exist_ok=True)
    secure(path.parent, True)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_socket():
            raise ValueError('HOST_RESET_UNSAFE_CONTROL')
        path.unlink()
    class Request(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(10)
            try:
                raw = self.rfile.readline(4097)
                if len(raw) > 4096 or not raw.endswith(b'\n'):
                    raise ValueError('HOST_RESET_INVALID_REQUEST')
                value = json.loads(raw)
                if value == {'action': 'capability'}:
                    result = {'available': True, 'scope': SCOPE}
                elif isinstance(value, dict) and set(value) == {'action', 'requestId'} and value['action'] == 'prepare':
                    result = manager.prepare(value['requestId'])
                else:
                    raise ValueError('HOST_RESET_INVALID_REQUEST')
            except Conflict as error:
                result = {'status': 409, 'error': str(error)}
            except Exception:
                result = {'status': 503, 'error': 'HOST_RESET_UNAVAILABLE'}
            self.wfile.write(json.dumps(result).encode() + b'\n')
    with socketserver.ThreadingUnixStreamServer(str(path), Request) as server:
        os.chmod(path, 0o600)
        server.serve_forever()


if __name__ == '__main__':
    main()
