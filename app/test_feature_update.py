import unittest
from feature_update import function_digest, merge_word_parser

OLD = '''EXTENSIONS = {'.pdf', '.txt'}
HELP = 'PDF/TXT/Markdown'
def fetch_bytes(url):
    return 'VM_DOH_CUSTOMIZATION_KEEP'
def extract_document(body, filename, source='群文件'):
    return {'old': True}
def parse_other():
    return 'unchanged'
'''
NEW = OLD.replace("return {'old': True}", "return {'word': True}")


class MergeTests(unittest.TestCase):
    def test_merging_keeps_vm_network_functions_verbatim(self):
        merged = merge_word_parser(OLD, NEW, function_digest(OLD))
        self.assertIn("return 'VM_DOH_CUSTOMIZATION_KEEP'", merged)
        self.assertIn("return 'unchanged'", merged)
        self.assertIn("return {'word': True}", merged)
        self.assertIn("'.docx'", merged)
        self.assertIn('DOCX', merged)

    def test_already_updated_extractor_is_accepted_and_unrecognized_one_is_refused(self):
        once = merge_word_parser(OLD, NEW, function_digest(OLD))
        self.assertEqual(once, merge_word_parser(once, NEW, function_digest(OLD)))
        changed = OLD.replace("{'old': True}", "{'vm_custom_parser': True}")
        with self.assertRaisesRegex(ValueError, 'merge Word parsing manually'):
            merge_word_parser(changed, NEW, function_digest(OLD))


if __name__ == '__main__':
    unittest.main()
