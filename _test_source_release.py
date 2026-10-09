"""Release plans must preserve customer code, data and recover from failure."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _deploy_server as client
from tools import source_release as release


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'server'
        self.cp = Path(self.tmp.name)/'checkpoint'
        self.local = Path(self.tmp.name)/'local'
        self.instance = 'instances/example_bot'
        self.write(self.root, 'bots.json', json.dumps({'bots':[
            {'id':'one','username':'example_bot','instance_folder':'example_bot'}]}))
        self.write(self.root, self.instance+'/INSTANCE.json', json.dumps(
            {'id':'one','username':'example_bot'}))
        for directory in ('.', self.instance):
            self.write(self.root/directory, 'core.py', 'old source')
            self.write(self.root/directory, 'config.json', 'customer configuration')
            self.write(self.root/directory, 'data/ledger.sqlite3', 'customer bills')
        self.write(self.root, 'README.md', 'old guide')
        self.write(self.local, 'core.py', 'new source')
        self.write(self.local, 'README.md', 'new guide')

    def write(self, root, name, content):
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def stage(self):
        hashes = release.sources(self.local)
        plan = release.plan(hashes, release.inventory(self.root, hashes))
        for name in hashes:
            release.install(self.local/name, self.cp/'candidate'/name)
        release.backup(self.root, self.cp, plan)
        self.write(self.cp,'ready.json',json.dumps(hashes))
        return plan

    def test_whitelist_excludes_secrets_artifacts_and_instance_copies(self):
        for name in ('config.json','bots.json','data/a.py','instances/example_bot/core.py',
                     'output/a.py','_test_a.py','runners/_probe.py','runners/__pycache__/a.py',
                     'static/test.html','solo/config.json','../core.py','/core.py',
                     'runners/../core.py',r'runners\ledger.py','runners//test.py',''):
            self.assertFalse(release.allowed(name), name)
        for name in ('core.py','runners/ledger/commands.py','runners/ledger/__init__.py',
                     'static/icon-192.png','solo/config.example.json','tools/source_release.py'):
            self.assertTrue(release.allowed(name), name)

    def test_identity_uses_current_registered_bots_and_checks_telegram_id(self):
        self.write(self.root, 'bots.json', json.dumps({'bots':[
            {'username':'renamed_bot', 'token':'123:FAKE_IDENTITY'}]}))
        service = ('ActiveState=active\nWorkingDirectory=%s\nExecStart=%s\n'
                   'KillMode=control-group\n') % (self.root, self.root/'主程序.py')
        def response(telegram_id):
            return io.BytesIO(json.dumps({'ok':True, 'result':{
                'username':'renamed_bot', 'id':telegram_id}}).encode())
        with patch.object(release.subprocess, 'check_output', return_value=service), \
                patch.object(release, 'urlopen', side_effect=lambda *a, **kw: response(123)):
            self.assertEqual(release.identity(self.root), ['renamed_bot'])
            with patch.object(release, 'urlopen', return_value=response(456)):
                with self.assertRaisesRegex(RuntimeError, 'identity verification failed'):
                    release.identity(self.root)
            self.write(self.root, 'bots.json', json.dumps({'bots':[]}))
            with self.assertRaisesRegex(ValueError, 'No registered bots'):
                release.identity(self.root)

    def test_global_release_syncs_registered_instances_only(self):
        self.write(self.local,'output/local-only.py','artifact')
        plan = self.stage()
        self.assertEqual(set(plan['changes']),{'core.py','README.md',self.instance+'/core.py'})
        before = release.protected(self.root)
        release.apply(self.root,self.cp,plan)
        self.assertEqual((self.root/self.instance/'core.py').read_text(),'new source')
        self.assertEqual(release.protected(self.root),before)
        self.assertFalse((self.root/self.instance/'README.md').exists())

    def test_rollback_restores_code_and_removes_only_new_source(self):
        self.write(self.local,'customer_ui.py','new file')
        plan = self.stage()
        before = release.protected(self.root)
        release.apply(self.root,self.cp,plan)
        release.restore(self.root,self.cp,plan)
        self.assertEqual(release.inventory(self.root,plan['sources']),plan['baseline'])
        self.assertEqual(release.protected(self.root),before)

    def test_custom_instance_source_is_not_overwritten(self):
        self.write(self.root,self.instance+'/core.py','customer custom version')
        with self.assertRaisesRegex(ValueError,'custom code'):
            self.stage()

    def test_already_updated_instance_is_accepted(self):
        self.write(self.root,self.instance+'/core.py','new source')
        self.assertNotIn(self.instance+'/core.py',self.stage()['changes'])

    def test_source_change_after_plan_blocks_before_any_write(self):
        plan = self.stage()
        self.write(self.root,'core.py','concurrent developer edit')
        with self.assertRaisesRegex(ValueError,'fresh plan'):
            release.apply(self.root,self.cp,plan)
        self.assertEqual((self.root/'README.md').read_text(),'old guide')

    def test_instance_membership_change_blocks(self):
        plan = self.stage()
        self.write(self.root,'bots.json',json.dumps({'bots':[]}))
        with self.assertRaisesRegex(ValueError,'fresh plan'):
            release.apply(self.root,self.cp,plan)

    def test_corrupt_candidate_blocks_before_any_write(self):
        plan = self.stage()
        self.write(self.cp,'candidate/core.py','damaged upload')
        with self.assertRaisesRegex(ValueError,'Candidate'):
            release.apply(self.root,self.cp,plan)
        self.assertEqual(release.inventory(self.root,plan['sources']),plan['baseline'])

    def test_corrupt_checkpoint_blocks_before_partial_rollback(self):
        plan = self.stage()
        release.apply(self.root,self.cp,plan)
        before = release.inventory(self.root,plan['sources'])
        self.write(self.cp,'backup/'+self.instance+'/core.py','damaged backup')
        with self.assertRaisesRegex(ValueError,'checkpoint corrupted'):
            release.restore(self.root,self.cp,plan)
        self.assertEqual(release.inventory(self.root,plan['sources']),before)

    def test_plan_cannot_inject_data_target(self):
        plan = self.stage()
        plan['changes']['config.json'] = plan['changes']['core.py']
        with self.assertRaisesRegex(ValueError,'Invalid release plan'):
            release.apply(self.root,self.cp,plan)

    def test_symlink_source_rejected(self):
        target = self.write(self.local,'outside.py','outside')
        (self.local/'core.py').unlink()
        try:
            (self.local/'core.py').symlink_to(target)
        except OSError:
            self.skipTest('Symlinks require Windows Developer Mode')
        with self.assertRaisesRegex(ValueError,'Symlink'):
            release.sources(self.local)

    def test_failed_health_check_rolls_back(self):
        plan = self.stage()
        self.write(self.root,self.instance+'/runtime.json',json.dumps({'status':'stopped'}))
        before = release.protected(self.root)
        with patch.object(release,'identity'), patch.object(release,'service') as service, \
             patch.object(release,'verify',side_effect=RuntimeError('health failed')):
            with self.assertRaisesRegex(RuntimeError,'health failed'):
                release.deploy(self.root,self.cp,plan)
        self.assertEqual([c.args[0] for c in service.call_args_list],['stop','start','stop','start'])
        self.assertEqual(release.inventory(self.root,plan['sources']),plan['baseline'])
        self.assertEqual(release.protected(self.root),before)

    def test_failure_before_install_does_not_revert_newer_edit(self):
        plan = self.stage()
        self.write(self.root,self.instance+'/runtime.json',json.dumps({'status':'stopped'}))
        def service(action):
            if action == 'stop':
                self.write(self.root,'core.py','external edit')
        with patch.object(release,'identity'), patch.object(release,'service',side_effect=service):
            with self.assertRaisesRegex(ValueError,'fresh plan'):
                release.deploy(self.root,self.cp,plan)
        self.assertEqual((self.root/'core.py').read_text(),'external edit')

    def test_partial_copy_failure_restores_installed_files(self):
        plan = self.stage()
        self.write(self.root,self.instance+'/runtime.json',json.dumps({'status':'stopped'}))
        install = release.install
        count = 0
        def fail_once(src,dst):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('disk error')
            install(src,dst)
        with patch.object(release,'identity'), patch.object(release,'service'), \
             patch.object(release,'install',side_effect=fail_once):
            with self.assertRaisesRegex(OSError,'disk error'):
                release.deploy(self.root,self.cp,plan)
        self.assertEqual(release.inventory(self.root,plan['sources']),plan['baseline'])

    def test_document_update_does_not_restart_service(self):
        (self.local/'core.py').unlink()
        plan = self.stage()
        self.assertFalse(plan['restart'])
        with patch.object(release,'identity'), patch.object(release,'verify'), \
             patch.object(release,'service') as service:
            release.deploy(self.root,self.cp,plan)
        service.assert_not_called()

    def test_dry_run_never_uploads_or_runs_tests(self):
        hashes = release.sources(self.local)
        with patch.object(client,'HERE',self.local), patch.object(client,'inspect',return_value={
            'bots':['example_bot'],'inventory':release.inventory(self.root,hashes)}), \
            patch.object(client,'command') as cmd, patch.object(client,'upload') as upload, \
            patch.object(client.subprocess,'run') as tests, contextlib.redirect_stdout(io.StringIO()):
            client.run(object(), SimpleNamespace(dry_run=True))
        cmd.assert_not_called()
        upload.assert_not_called()
        tests.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'Production release lock uses Linux flock')
    def test_concurrent_release_lock_blocks_second_action(self):
        import fcntl
        cp = self.root.parent/'tgpanel_checkpoints/release_20261009_1'
        cp.mkdir(parents=True)
        with (cp.parent/'release.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(release,'ROOT',self.root), \
                 patch.object(release.sys,'argv',['release.py','deploy',str(cp)]), \
                 patch.object(release,'run_action') as action:
                with self.assertRaises(BlockingIOError):
                    release.main()
                action.assert_not_called()


if __name__ == '__main__':
    unittest.main()
