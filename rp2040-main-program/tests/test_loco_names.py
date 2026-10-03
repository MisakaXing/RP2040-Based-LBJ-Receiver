import ast
import json
from pathlib import Path
import unittest
import textwrap
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {'260':'FXD1C','263':'FXD1BA','344':'轨道探伤车','361':'起重轨道车',
            '400':'起重轨道车','401':'起重轨道车','403':'轨道探伤车',
            '411':'起重轨道车','413':'起重轨道车','415':'轨道打磨车','422':'轨道探伤车'}


class LocoNameTests(unittest.TestCase):
    def test_five_digit_parser_and_boundary(self):
        tree = ast.parse((ROOT / 'lbj_receiver.py').read_text())
        receiver = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LBJReceiver')
        methods = {'_is_emu_loco_code', '_is_five_digit_loco_code', '_loco_number_raw',
                   '_resolve_loco_code', '_bcd_to_hex', '_decode_class_tag', '_parse_ext',
                   '_score_lbj_candidate', '_find_lbj_block', '_parse_basic',
                   '_has_usable_basic', '_parse_train_data', '_merge_lbj_fragments',
                   '_lbj_fragment_role', '_ric_number', '_ric_addr'}
        receiver.body = [n for n in receiver.body if isinstance(n, ast.Assign)
                         or isinstance(n, ast.FunctionDef) and n.name in methods]
        ns = {}
        exec(compile(ast.Module(body=[receiver], type_ignores=[]), 'loco_parser', 'exec'), ns)
        parser = ns['LBJReceiver']()
        parser.loco_types = json.loads((ROOT / 'locos.json').read_text())
        parser.last_align_log = 0
        raw = '57082 --- ----- | 20204220478230U4[3 7-8 [-[202011633083940166258000'
        basic_raw, ext_raw = raw.split(' | ')
        basic = parser._parse_train_data(basic_raw)
        ext = parser._parse_train_data(ext_raw)
        basic['ric'], ext['ric'] = '1234000-F1', '1234002-F1'
        result = parser._merge_lbj_fragments(basic, ext)
        self.assertEqual(result['extended']['loco_type'], '轨道探伤车-04782')
        self.assertEqual(result['extended']['cab_end'], '30')
        for code in ('344', '361', '422', '999'):
            for cab in ('31', '32'):
                with self.subTest(code=code, cab=cab):
                    ext = parser._parse_ext('2020' + code + '04782' + cab + '0' * 33)
                    self.assertTrue(ext['loco_type'].endswith('-04782'))
                    self.assertEqual(ext['cab_end'], cab)
        self.assertEqual(parser._loco_number_raw('34304782', '343'), '0478')
        self.assertEqual(parser._loco_number_raw('26304782', '263'), '4782')

    def test_five_digit_small_cab_render_and_history(self):
        source = (ROOT / 'main.py').read_text()
        tree = ast.parse(source)
        encoder_nodes = [n for n in tree.body if (
            isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in
            ('LOCO_NAME_GBK', 'UNKNOWN_LOCO_GBK') for t in n.targets)) or
            isinstance(n, ast.FunctionDef) and n.name == 'encode_loco_gbk']
        ns = {}
        exec(compile(ast.Module(body=encoder_nodes, type_ignores=[]), 'loco_encoder', 'exec'), ns)
        start = source.index("        loco = str(ext.get('loco_type'")
        end = source.index('\n    lon =', start)
        render = compile(textwrap.dedent(source[start:end]), 'loco_render', 'exec')
        for ext in (
            {'loco_type': '轨道探伤车-04782', 'loco_raw': '42204782', 'cab_end': '31'},
            {'loco_type': '轨道探伤车-0478', 'loco_raw': '42204782', 'cab_end': '32'},
            {'loco_type': '轨道探伤车-04782', 'cab_end': '31'},
        ):
            draws = []
            ns.update(ext=ext, y3=125, WHITE=1, bg_color=0,
                      tft=SimpleNamespace(draw_gbk=lambda *a, **kw: draws.append((a, kw))))
            exec(render, ns)
            self.assertEqual(draws[0][0][0], '轨道探伤车-04782'.encode('gb2312'))
            self.assertEqual(draws[1][0][0], b'B' if ext['cab_end'] == '32' else b'A')
            self.assertEqual(draws[1][0][1:3], (309, 133))
            self.assertEqual(draws[1][1]['scale'], 1)
            self.assertLessEqual(draws[1][0][1] + 8, 320)
        draws = []
        ns.update(ext={'loco_type': 'FXD1BA-0001', 'loco_raw': '26300001', 'cab_end': '31'})
        exec(render, ns)
        self.assertEqual(len(draws), 1)
        self.assertEqual(draws[0][0][0], b'FXD1BA-0001A')

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

        label = ns['encode_loco_gbk'](mapping['263'] + '-0001A')
        self.assertEqual(label, b'FXD1BA-0001A')
        self.assertEqual(53 + len(label) * 8 * 2, 245)
