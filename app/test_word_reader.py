import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import zipfile

import group_file_reader
import paper_reader
import word_reader

NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def docx(body, extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{NS}"><w:body>{body}</w:body></w:document>')
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return buffer.getvalue()


class WordTests(unittest.IsolatedAsyncioTestCase):
    def sample(self):
        return docx('<w:p><w:r><w:t>标题与正文。' + '这是实际的文献正文，不是标题或文件名。' * 5 + '</w:t></w:r></w:p>'
                    '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格甲</w:t></w:r></w:p></w:tc>'
                    '<w:tc><w:p><w:r><w:t>表格乙</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')

    def test_docx_reads_real_body_tables_and_reports_paragraph_not_page(self):
        result = paper_reader.extract_document(self.sample(), '文献.docx')
        text = ''.join(row['text'] for row in result['units'])
        self.assertIn('实际的文献正文', text)
        self.assertIn('表格甲', text)
        self.assertIn('表格乙', text)
        self.assertTrue(all(row['label'].startswith('段') for row in result['units']))
        self.assertIn('未还原Word分页', result['kind'])

    async def test_same_isolated_worker_contract_works_for_word(self):
        result = await paper_reader.extract_in_worker(self.sample(), '文献.docx', 'fixture')
        self.assertIn('Word', result['kind'])
        self.assertEqual(result['source'], 'fixture')

    def test_revisions_hidden_text_and_external_links_are_not_executed(self):
        body = '<w:p><w:del><w:r><w:delText>已删除</w:delText></w:r></w:del>'
        body += '<w:r><w:rPr><w:vanish/></w:rPr><w:t>隐藏文字</w:t></w:r>'
        body += '<w:hyperlink><w:r><w:t>可见链接文字</w:t></w:r></w:hyperlink>'
        body += '<w:ins><w:r><w:t>新增文字</w:t></w:r></w:ins></w:p>'
        text = word_reader.read_docx(docx(body, {'word/_rels/document.xml.rels': 'https://example.com/private'}))
        self.assertIn('可见链接文字', text)
        self.assertIn('新增文字', text)
        self.assertNotIn('隐藏文字', text)
        self.assertNotIn('已删除', text)

    def test_footnotes_preserve_text(self):
        body = docx('<w:p><w:r><w:t>正文</w:t></w:r></w:p>',
                    {'word/footnotes.xml': f'<w:footnotes xmlns:w="{NS}"><w:footnote w:id="1"><w:p><w:r><w:t>脚注内容</w:t></w:r></w:p></w:footnote></w:footnotes>'})
        self.assertIn('脚注内容', word_reader.read_docx(body))

    def test_corrupt_missing_xml_and_xml_entities_are_rejected(self):
        with self.assertRaises(word_reader.WordError):
            word_reader.read_docx(b'not a ZIP')
        for body in (docx('<!DOCTYPE evil>'), docx('broken xml </x>')):
            with self.assertRaises(word_reader.WordError):
                word_reader.read_docx(body)

    def test_archive_bomb_limits_and_duplicate_paths_are_rejected(self):
        with patch.object(word_reader, 'EXPANDED_LIMIT', 10), self.assertRaises(word_reader.WordError):
            word_reader.read_docx(self.sample())
        with patch.object(word_reader, 'XML_LIMIT', 10), self.assertRaises(word_reader.WordError):
            word_reader.read_docx(self.sample())
        with self.assertRaises(word_reader.WordError):
            word_reader.read_docx(docx('<w:p/>', {'../outside.xml': 'bad'}))

    def test_long_word_text_is_marked_partial_without_fake_page_numbers(self):
        result = paper_reader.extract_document(docx('<w:p><w:r><w:t>' + '字' * 241000 + '</w:t></w:r></w:p>'), 'long.docx')
        self.assertTrue(result['truncated'])
        self.assertEqual(sum(len(u['text']) for u in result['units']), paper_reader.MAX_CHARS)

    def test_word_extensions_enter_group_library_and_filename_matching(self):
        self.assertIn('.docx', paper_reader.EXTENSIONS)
        self.assertIn('.doc', paper_reader.EXTENSIONS)
        library = paper_reader.PaperLibrary()
        library.remember_file(('123', '7'), {'name': '研究.docx', 'id': 'fixture'})
        self.assertTrue(library.get(library.files, ('123', '7')))
        self.assertEqual(group_file_reader.normalized('研究.docx'), group_file_reader.normalized('研究'))

    def test_legacy_doc_without_parser_is_actionable(self):
        with patch.object(word_reader.shutil, 'which', return_value=None), self.assertRaisesRegex(word_reader.WordError, 'antiword'):
            word_reader.read_doc(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fixture')
        with self.assertRaisesRegex(word_reader.WordError, '格式无效'):
            word_reader.read_doc(b'not a doc')

    def test_legacy_doc_uses_bounded_converter_without_shell(self):
        def run(args, **kwargs):
            self.assertEqual(args[:3], ['fixture-antiword', '-m', 'UTF-8.txt'])
            self.assertEqual(kwargs['timeout'], 45)
            self.assertNotIn('shell', kwargs)
            kwargs['stdout'].write(('旧版Word实际正文。' * 10).encode())
            return subprocess.CompletedProcess(args, 0)
        with patch.object(word_reader.shutil, 'which', return_value='fixture-antiword'), patch.object(word_reader.subprocess, 'run', side_effect=run):
            result = paper_reader.extract_document(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fixture', 'old.doc')
        self.assertIn('旧版Word实际正文', result['units'][0]['text'])


if __name__ == '__main__':
    unittest.main()
