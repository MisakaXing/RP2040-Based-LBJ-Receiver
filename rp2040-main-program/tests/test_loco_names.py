import ast
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {'260':'FXD1C','344':'轨道探伤车','361':'起重轨道车',
            '400':'起重轨道车','401':'起重轨道车','403':'轨道探伤车',
            '411':'起重轨道车','413':'起重轨道车','415':'轨道打磨车','422':'轨道探伤车'}


class LocoNameTests(unittest.TestCase):
    def test_mapping_encoding_and_width(self):
        mapping=json.loads((ROOT/'locos.json').read_text())
        source=ast.parse((ROOT/'main.py').read_text())
        nodes=[n for n in source.body if (
            isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in
            ('LOCO_NAME_GBK','UNKNOWN_LOCO_GBK') for t in n.targets)) or
            (isinstance(n,ast.FunctionDef) and n.name=='encode_loco_gbk')]
        ns={}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'loco_display','exec'),ns)
        for code,name in EXPECTED.items():
            with self.subTest(code=code):
                self.assertEqual(mapping[code],name)
                text=name+'-1234A'
                encoded=ns['encode_loco_gbk'](text)
                self.assertEqual(encoded,text.encode('gb2312'))
                self.assertLessEqual(53+len(encoded)*16,320)
        # All existing names must still have the correct device encoding.
        for name in mapping.values():
            self.assertEqual(ns['encode_loco_gbk'](name+'-1234B'),(name+'-1234B').encode('gb2312'))
