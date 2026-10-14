import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('reset_worker', Path(__file__).parents[1] / 'worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)

class EraseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / '.dsh'
        self.profile = self.home / 'profiles/web'
        (self.profile / 'node_modules/pkg').mkdir(parents=True)
        (self.profile / 'node_modules/pkg/code.js').write_text('installed code')
        self.deployment = self.root / 'deployment'
        (self.deployment / 'wrapper').mkdir(parents=True)
        (self.deployment / 'wrapper/code.js').write_text('deployment code')
        self.source = self.root / 'source'
        self.source.mkdir()
        self.config = {'home': str(self.home), 'deploymentRoot': str(self.deployment),
                       'protectedRoots': [str(self.source)], 'credentialFiles': [],
                       'profileManifest': {'name': 'fresh', 'dsh': {'profile': {'bundles': []}}}}
    def tearDown(self):
        self.temp.cleanup()
    def test_erases_business_and_credentials_without_backup_preserving_installation(self):
        (self.home / 'sessions').mkdir()
        (self.home / 'sessions/old').write_text('private history')
        (self.home / '.credentials.yaml').write_text('old authorization')
        (self.profile / 'cordis.patch.yml').write_text('old personal settings')
        (self.deployment / 'token.txt').write_text('old pairing')
        worker.erase_data(self.config)
        self.assertFalse((self.home / 'sessions').exists())
        self.assertFalse((self.home / '.credentials.yaml').exists())
        self.assertFalse((self.deployment / 'token.txt').exists())
        self.assertEqual((self.profile / 'node_modules/pkg/code.js').read_text(), 'installed code')
        self.assertEqual((self.profile / 'cordis.patch.yml').read_text(), '[]\n')
        self.assertEqual(set(p.name for p in self.root.iterdir()), {'.dsh', 'deployment', 'source'})
    def test_symlink_leaf_is_unlinked_never_followed(self):
        (self.source / 'precious').write_text('keep')
        (self.home / 'sessions').symlink_to(self.source, target_is_directory=True)
        worker.erase_data(self.config)
        self.assertEqual((self.source / 'precious').read_text(), 'keep')
    def test_symlinked_preserved_profile_refuses_before_any_erasure(self):
        import shutil
        shutil.rmtree(self.home / 'profiles')
        (self.home / 'profiles').symlink_to(self.source, target_is_directory=True)
        (self.home / '.credentials.yaml').write_text('keep until admission')
        with self.assertRaises(ValueError): worker.erase_data(self.config)
        self.assertTrue((self.home / '.credentials.yaml').exists())
    def test_root_or_source_ancestor_is_never_admitted(self):
        for path in ['/', str(self.root), str(self.source)]:
            with self.assertRaises(ValueError): worker.erase_data({**self.config, 'home': path})
    def test_credential_path_cannot_delete_from_a_protected_source(self):
        credential = self.source / '.env'
        credential.write_text('preserved source-owned fixture')
        with self.assertRaisesRegex(ValueError, 'HOST_RESET_PROTECTED_PATH'):
            worker.erase_data({**self.config, 'credentialFiles': [str(credential)]})
        self.assertTrue(credential.exists())

if __name__ == '__main__': unittest.main()
