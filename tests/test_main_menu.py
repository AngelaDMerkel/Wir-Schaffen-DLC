import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('main_menu_renderer', ROOT / 'scripts/render_main_menu.py')
MENU = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MENU)


class MainMenuVersionTests(unittest.TestCase):
    def text(self, svg):
        return ' '.join(node.text or '' for node in ET.fromstring(svg).iter('{http://www.w3.org/2000/svg}text'))

    def test_readme_image_matches_current_installer_and_version(self):
        rendered = MENU.render()
        self.assertEqual((ROOT / 'assets/wir-schaffen-dlc-main.svg').read_text(), rendered)
        self.assertIn('v' + MENU.installer.packer.VERSION, self.text(rendered))

    def test_rendered_version_follows_the_installer_version(self):
        with mock.patch.object(MENU.installer.packer, 'VERSION', '12.34.5678'):
            self.assertIn('v12.34.5678', self.text(MENU.render()))

    def test_check_reports_stale_image_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / 'stale.svg'
            image.write_text('<svg>v0.0.0</svg>')
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/render_main_menu.py'),
                                     '--check', '--output', str(image)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('image is stale', result.stderr)
            self.assertEqual(image.read_text(), '<svg>v0.0.0</svg>')
