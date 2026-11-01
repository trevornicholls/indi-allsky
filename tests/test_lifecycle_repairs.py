"""Focused regressions for the four real-Dell lifecycle failures. No devices."""
import ast
import importlib.util
from pathlib import Path
from queue import Queue
from types import SimpleNamespace, ModuleType
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from tests.test_qhy5lii_c import CONFIG, frame
from indi_allsky.exposure import qhy5lii_c as C

ROOT = Path(__file__).resolve().parents[1]

class Lifecycle(unittest.TestCase):
    def operation(self):
        shared = [0, 0]; q = Queue(); g = C.FrameGate(CONFIG, q, shared)
        g.set_mode('night'); c = g.start(.01, 1, 1, 'normal')
        g.property('CCD_EXPOSURE', {'CCD_EXPOSURE_VALUE': .01}); g.frame(frame(c))
        return shared, q, g, c

    def test_shutdown_drains_late_correlated_ack_and_replacement(self):
        shared, q, g, c = self.operation()
        def finish():
            time.sleep(.1)
            q.put({'command': c, 'ok': True, 'proposal': (.02, 1, 1)})
        thread = threading.Thread(target=finish); thread.start()
        self.assertEqual(shared[0], 1)
        self.assertTrue(g.drain_shutdown(timeout=1)); thread.join()
        self.assertEqual(g.completed, c); self.assertEqual(shared[0], 0)
        replacement = C.FrameGate(CONFIG, Queue(), shared)
        self.assertNotEqual(replacement.session, g.session)
        self.assertIsNone(replacement.pending)

    def test_shutdown_timeout_retains_fault_for_replacement(self):
        shared, q, g, c = self.operation()
        self.assertFalse(g.drain_shutdown(timeout=0))
        self.assertEqual(shared[0], 2)
        with self.assertRaises(C.ControlError): C.FrameGate(CONFIG, Queue(), shared)

    def test_shutdown_does_not_accept_foreign_or_wrong_settings(self):
        for change in ({'session': 'foreign'}, {'exposure': .1}):
            shared, q, g, c = self.operation()
            q.put({'command': dict(c, **change), 'ok': True, 'proposal': (.02,1,1)})
            self.assertFalse(g.drain_shutdown(timeout=0)); self.assertEqual(shared[0],2)

    def test_capture_drains_before_disconnect(self):
        s = (ROOT/'indi_allsky/capture.py').read_text()
        block = s[s.index('# shutdown here to ensure camera'):]
        self.assertLess(block.index('self.c_gate.drain_shutdown()'), block.index('self.indiclient.disconnectServer()'))

    def test_actual_sensor_broadcast_block(self):
        tree = ast.parse((ROOT/'indi_allsky/flask/miscDb.py').read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name=='addImage')
        block = next(n for n in method.body if isinstance(n, ast.Try) and 'sensor_update' in ast.unparse(n))
        events = ModuleType('indi_allsky.events'); events.event_manager = Mock()
        sensors = ModuleType('indi_allsky.sensors_mapping'); sensors.get_latest_sensors_payload = Mock(return_value={'ok':True})
        image = SimpleNamespace(**{n:1 for n in ['id','filename','camera_id','createDate','dayDate','exposure','gain','binmode','night','temp','sqm','stars']})
        config = {'test':True}; logger=Mock()
        with patch.dict(sys.modules, {'indi_allsky.events':events,'indi_allsky.sensors_mapping':sensors}):
            exec(compile(ast.Module(body=[block],type_ignores=[]),'<actual broadcast>','exec'), {'__package__':'indi_allsky.flask','self':SimpleNamespace(config=config),'image':image,'logger':logger})
        sensors.get_latest_sensors_payload.assert_called_once_with(config)
        logger.error.assert_not_called()
        self.assertEqual(events.event_manager.broadcast.call_args_list[-1].args, ('sensor_update',{'ok':True}))

    def test_legacy_sudoers_migration_and_unknown_policy_refusal(self):
        script = (ROOT/'debian/indi-allsky.preinst').read_text().split("<<'PY'\n",1)[1].split('\nPY',1)[0]
        for unexpected in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp); policy=base/'policy'; policy.mkdir()
                (policy/'sudoers').write_text('# commands necessary for unattended upgrade\nindi-allsky ALL=(root) NOPASSWD: /usr/bin/apt-get update,/usr/bin/apt-get -y install *\n')
                (policy/'sudoers').chmod(0o440)
                if unexpected: (policy/'local-policy').write_text('do not remove')
                code=script.replace("'/etc/sudoers.d/indi-allsky'",repr(str(policy))).replace("'/var/lib/indi-allsky/legacy-sudoers'",repr(str(base/'backup')))
                real_stat=Path.stat
                def root_stat(path,*a,**kw):
                    st=real_stat(path,*a,**kw)
                    # Model the real root-owned 0440 legacy dir without root.
                    return SimpleNamespace(st_uid=0,st_gid=0,st_mode=(st.st_mode if path != policy else 0o40440))
                with patch.object(Path,'stat',root_stat),patch('subprocess.run') as visudo:
                    if unexpected:
                        with self.assertRaises(SystemExit): exec(code,{})
                        self.assertTrue((policy/'local-policy').exists()); visudo.assert_not_called()
                    else:
                        exec(code,{})
                        self.assertFalse(policy.exists());self.assertEqual(len(list((base/'backup').glob('*/sudoers'))),1)
                        visudo.assert_called_once()

    def test_venv_replacement_preserves_old_local_state(self):
        spec=importlib.util.spec_from_file_location('install_venv',ROOT/'misc/install_venv.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            parent=Path(tmp);target=parent/'venv';target.mkdir();(target/'obsolete.dist-info').mkdir();(target/'local-note').write_text('keep')
            bundle=parent/'bundle';bundle.write_bytes(b'archive')
            def extract(args,check): (Path(args[args.index('-C')+1])/'current.dist-info').mkdir()
            with patch.object(mod.subprocess,'run',side_effect=extract),patch.object(mod,'validate') as validated:mod.install(bundle,target)
            validated.assert_called_once();self.assertTrue((target/'current.dist-info').is_dir());self.assertFalse((target/'obsolete.dist-info').exists())
            old=list(parent.glob('venv.previous-*'));self.assertEqual(len(old),1);self.assertEqual((old[0]/'local-note').read_text(),'keep')

    def test_invalid_bundle_preserves_current_environment(self):
        spec=importlib.util.spec_from_file_location('install_venv',ROOT/'misc/install_venv.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'venv';target.mkdir();(target/'good').write_text('original')
            with patch.object(mod.subprocess,'run'),patch.object(mod,'validate',side_effect=RuntimeError('bad dependencies')):
                with self.assertRaises(RuntimeError):mod.install(Path(tmp)/'bundle',target)
            self.assertEqual((target/'good').read_text(),'original');self.assertFalse(list(Path(tmp).glob('venv.previous-*')))

if __name__=='__main__':unittest.main()
