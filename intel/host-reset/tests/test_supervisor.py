import importlib.util
import json
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import uuid

sys.path.insert(0, str(Path(__file__).parents[1]))
from supervisor import Manager, Conflict


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / '.dsh'
        (self.home / 'profiles/web/node_modules').mkdir(parents=True)
        deployment = self.root / 'deployment'
        (deployment / 'wrapper').mkdir(parents=True)
        source = self.root / 'source'
        source.mkdir()
        self.release = threading.Event()
        self.calls = []
        self.config = {'stateRoot': str(self.root / 'control'), 'deploymentId': str(uuid.uuid4()),
                       'progressHost': '127.0.0.1', 'progressPort': 0, 'home': str(self.home),
                       'deploymentRoot': str(deployment), 'protectedRoots': [str(source)],
                       'credentialFiles': [], 'profileManifest': {'dsh': {'profile': {'bundles': []}}}}
        def execute(_, report):
            self.calls.append(1)
            self.release.wait(5)
            report('completed', None)
        self.manager = Manager(self.config, execute)
    def tearDown(self):
        self.release.set()
        for server in list(self.manager.servers.values()):
            server.shutdown()
            server.server_close()
        self.temp.cleanup()
    def prepare(self):
        return self.manager.prepare(str(uuid.uuid4()))
    def test_prepare_is_durable_and_excludes_concurrent_operation(self):
        value = self.prepare()
        self.assertEqual(self.manager.prepare(value['operationId'])['progress'], value['progress'])
        with self.assertRaises(Conflict): self.prepare()
        self.assertFalse(value['backup'])
        self.assertEqual(self.calls, [])
    def test_commit_deduplicates_and_completion_requires_worker_verification(self):
        value = self.prepare()
        args = {'operationId': value['operationId'], 'confirmation': 'RESET_ALL_NO_BACKUP'}
        first = self.manager.command(value['operationId'], 'host/reset/commit', args)
        self.assertEqual(first['phase'], 'running')
        self.manager.command(value['operationId'], 'host/reset/commit', args)
        with self.assertRaises(Conflict):
            self.manager.command(value['operationId'], 'host/reset/ack', {'operationId': value['operationId']})
        self.release.set()
        for _ in range(50):
            if self.manager.load(value['operationId'])['phase'] == 'completed': break
            time.sleep(0.02)
        self.assertEqual(self.calls, [1])
        self.assertTrue(self.manager.load(value['operationId'])['hostVerified'])
        self.manager.command(value['operationId'], 'host/reset/commit', args)
        self.assertEqual(self.calls, [1])
    def test_wrong_id_paths_and_extra_scope_are_refused(self):
        value = self.prepare()
        for args in [{'operationId': str(uuid.uuid4())}, {'operationId': value['operationId'], 'path': '/'}]:
            with self.assertRaises(ValueError): self.manager.command(value['operationId'], 'host/reset/status', args)
        with self.assertRaises(ValueError): self.manager.prepare('../escape')
        with self.assertRaises(ValueError):
            self.manager.command(value['operationId'], 'host/reset/commit', {'operationId': value['operationId'], 'confirmation': 'yes'})
    def test_tls_progress_rejects_wrong_credentials_and_does_not_expose_token(self):
        value = self.prepare()
        port = self.manager.servers[value['operationId']].server_address[1]
        context = ssl.create_default_context(cadata=value['progress']['certificate'])
        url = 'https://127.0.0.1:' + str(port) + '/v1/rpc'
        body = json.dumps({'endpoint': 'host/reset/status', 'args': {'operationId': value['operationId']}}).encode()
        bad = urllib.request.Request(url, data=body, headers={'Authorization': 'Bearer wrong'})
        with self.assertRaises(urllib.error.HTTPError) as rejection: urllib.request.urlopen(bad, context=context)
        self.assertEqual(rejection.exception.code, 401)
        request = urllib.request.Request(url, data=body, headers={'Authorization': 'Bearer ' + value['progress']['token']})
        with urllib.request.urlopen(request, context=context) as response: raw = response.read().decode()
        self.assertEqual(json.loads(raw)['value']['phase'], 'prepared')
        self.assertNotIn(value['progress']['token'], raw)
    def test_failure_never_becomes_completed(self):
        def fail(_, report):
            report('clearing', None)
            raise ValueError('HOST_RESET_TEST_PARTIAL_FAILURE')
        self.manager.executor = fail
        value = self.prepare()
        self.manager.command(value['operationId'], 'host/reset/commit', {'operationId': value['operationId'], 'confirmation': 'RESET_ALL_NO_BACKUP'})
        for _ in range(50):
            if self.manager.load(value['operationId'])['phase'] == 'failed': break
            time.sleep(0.02)
        state = self.manager.load(value['operationId'])
        self.assertEqual(state['phase'], 'failed')
        self.assertFalse(state['hostVerified'])
    def test_cancel_cannot_commit_and_ack_removes_credential(self):
        value = self.prepare()
        identity = value['operationId']
        args = {'operationId': identity}
        self.assertEqual(self.manager.command(identity, 'host/reset/cancel', args)['phase'], 'cancelled')
        with self.assertRaises(Conflict):
            self.manager.command(identity, 'host/reset/commit', {**args, 'confirmation': 'RESET_ALL_NO_BACKUP'})
        self.manager.command(identity, 'host/reset/ack', args)
        self.assertNotIn('token', self.manager.load(identity))
        self.assertEqual(self.calls, [])
    def test_restart_recovers_same_tls_proof_without_reexecuting(self):
        value = self.prepare()
        identity = value['operationId']
        for server in self.manager.servers.values():
            server.shutdown()
            server.server_close()
        self.manager = Manager(self.config, lambda *_: self.fail('Restart must not execute'))
        recovered = self.manager.prepare(identity)
        self.assertEqual(recovered['progress']['certificate'], value['progress']['certificate'])
        self.assertEqual(recovered['progress']['token'], value['progress']['token'])
        self.assertEqual(recovered['phase'], 'prepared')
    def test_failed_certificate_creation_leaves_no_active_operation(self):
        from unittest.mock import patch
        with patch('supervisor.subprocess.run', side_effect=OSError('fixture unavailable')):
            with self.assertRaises(OSError): self.prepare()
        self.assertEqual(list(self.manager.operations.iterdir()), [])
        self.prepare()
    def restart(self):
        for server in self.manager.servers.values():
            self.config['progressPort'] = server.server_address[1]
            server.shutdown()
            server.server_close()
        self.manager = Manager(self.config, lambda *_: self.fail('Restart cannot replay deletion'))
    def test_accepted_and_partial_interruption_never_replays_worker(self):
        value = self.prepare()
        for step in ['accepted', 'clearing']:
            operation = self.manager.load(value['operationId'])
            operation.update(phase='running', step=step)
            self.manager.save(operation)
            self.restart()
            recovered = self.manager.load(value['operationId'])
            self.assertEqual(recovered['phase'], 'failed')
            self.assertEqual(recovered['code'], 'HOST_RESET_INTERRUPTED')
            self.assertFalse(recovered['hostVerified'])
            self.assertEqual(self.calls, [])
    def test_completed_restart_keeps_original_port_certificate_and_receipt(self):
        value = self.prepare()
        operation = self.manager.load(value['operationId'])
        operation.update(phase='completed', step='completed', hostVerified=True)
        self.manager.save(operation)
        old_port = self.manager.servers[value['operationId']].server_address[1]
        self.restart()
        self.assertEqual(self.manager.servers[value['operationId']].server_address[1], old_port)
        context = ssl.create_default_context(cadata=value['progress']['certificate'])
        body = json.dumps({'endpoint': 'host/reset/status', 'args': {'operationId': value['operationId']}}).encode()
        request = urllib.request.Request(value['progress']['baseUrl'] + '/v1/rpc', data=body, headers={'Authorization': 'Bearer ' + value['progress']['token']})
        with urllib.request.urlopen(request, context=context) as response:
            state = json.loads(response.read())['value']
        self.assertTrue(state['hostVerified'])
        self.assertEqual(state['phase'], 'completed')
    def test_idle_tls_connections_are_bounded_and_release_their_slots(self):
        import socket
        value = self.prepare()
        port = self.manager.servers[value['operationId']].server_address[1]
        context = ssl.create_default_context(cadata=value['progress']['certificate'])
        connections = []
        try:
            for _ in range(16):
                connections.append(context.wrap_socket(socket.create_connection(('127.0.0.1', port), timeout=2), server_hostname='127.0.0.1'))
            with self.assertRaises((ssl.SSLError, ConnectionError)):
                with socket.create_connection(('127.0.0.1', port), timeout=2) as raw:
                    context.wrap_socket(raw, server_hostname='127.0.0.1')
        finally:
            for connection in connections:
                connection.close()
        for _ in range(50):
            if self.manager.servers[value['operationId']].slots._value == 16:
                break
            time.sleep(0.02)
        self.assertEqual(self.manager.servers[value['operationId']].slots._value, 16)


if __name__ == '__main__': unittest.main()
