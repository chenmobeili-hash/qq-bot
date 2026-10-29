from pathlib import Path
import tempfile
import unittest
import yaml
import configure_fish_audio as setup


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.path=self.root/'config.yaml'
        self.original=b'# private comment\r\nadmin_qq: [123]\r\nimage_download:\r\n  proxy_only: true\r\n'
        self.path.write_bytes(self.original)

    def test_preview_leaves_private_configuration_untouched(self):
        self.assertEqual(setup.configure(self.root)['status'],'preview')
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertFalse(list(self.root.glob('backup-*')))

    def test_append_has_blank_key_voice_free_model_and_preserves_all_existing_fields(self):
        setup.configure(self.root,apply=True)
        self.assertTrue(self.path.read_bytes().startswith(self.original))
        result=yaml.safe_load(self.path.read_text(encoding='utf-8'))
        self.assertEqual(result['image_download'],{'proxy_only':True})
        self.assertEqual(result['fish_audio']['api_key'],'')
        self.assertEqual(result['fish_audio']['voice_id'],'')
        self.assertEqual(result['fish_audio']['model'],'s2.1-pro-free')
        backup=next(self.root.glob('backup-*'))/'config.yaml'
        self.assertEqual(backup.read_bytes(),self.original)

    def test_existing_private_fish_section_is_preserved_exactly(self):
        value=self.original+b'fish_audio:\n  api_key: PRIVATE_EXAMPLE\n'
        self.path.write_bytes(value)
        self.assertEqual(setup.configure(self.root,apply=True)['status'],'preserved')
        self.assertEqual(self.path.read_bytes(),value)
