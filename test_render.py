"""Run with: python3 -m unittest test_render -v"""
import base64
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
KEY = 'ssh-ed25519 ' + base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'x' * 32).decode()


class RenderTest(unittest.TestCase):
    def test_render_deployment_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp) / 'rootfs'
            command = [sys.executable, str(ROOT / 'render.py'), '--host', '203.0.113.10',
                       '--ssh-host-key', KEY, '--output', str(output)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            nginx = (output / 'etc/nginx/sites-available/backup-ip-http').read_text()
            self.assertIn('server_name 203.0.113.10;', nginx)
            agent = (output / 'opt/backup-api/agent.sh').read_text()
            self.assertIn('VPS_IP="203.0.113.10"', agent)
            self.assertIn('203.0.113.10 ' + KEY, agent)
            self.assertNotIn('@@', agent)
            again = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual(agent, (output / 'opt/backup-api/agent.sh').read_text())

    def test_reject_invalid_inputs_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            for host, key in [('example.com; id', KEY), ('example.com', KEY),
                              ('203.0.113.10', 'ssh-ed25519 invalid'),
                              ('203.0.113.10', KEY + '\ncommand=evil')]:
                output = pathlib.Path(tmp) / 'rootfs'
                result = subprocess.run([sys.executable, str(ROOT / 'render.py'), '--host', host,
                                         '--ssh-host-key', key, '--output', str(output)],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
