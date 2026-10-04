import ast
from array import array
from pathlib import Path
import random
import unittest


class CompactBchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'lbj_receiver.py'
        tree = ast.parse(source.read_text())
        receiver = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                        and n.name == 'LBJReceiver')
        names = {'_init_syndrome_table', '_calc_syndrome', '_parity_check', '_correct_bch'}
        receiver.body = [n for n in receiver.body if isinstance(n, ast.Assign)
                         or isinstance(n, ast.FunctionDef) and n.name in names]
        namespace = {'array': array}
        exec(compile(ast.Module(body=[receiver], type_ignores=[]), str(source), 'exec'), namespace)
        cls.radio = namespace['LBJReceiver']()
        cls.radio.BCH_POLY = 0x769
        cls.radio._init_syndrome_table()
        cls.reference = {}
        for i in range(1, 32):
            mask = 1 << i
            cls.reference[cls.radio._calc_syndrome(mask)] = (mask, 1)
        for i in range(1, 32):
            for j in range(i + 1, 32):
                mask = (1 << i) | (1 << j)
                cls.reference[cls.radio._calc_syndrome(mask)] = (mask, 2)

    def reference_decode(self, word):
        syndrome = self.radio._calc_syndrome(word)
        if syndrome == 0:
            return (word, 0) if self.radio._parity_check(word) else (word ^ 1, 1)
        match = self.reference.get(syndrome)
        if match is None:
            return word, -1
        mask, count = match
        corrected = word ^ mask
        if not self.radio._parity_check(corrected):
            if count == 2:
                return word, -1
            corrected ^= 1
            count += 1
        return corrected, count

    def test_all_1024_syndromes_preserve_reference_meaning(self):
        self.assertEqual(len(self.radio.syndrome_table) * self.radio.syndrome_table.itemsize, 4096)
        for syndrome in range(1024):
            match = self.reference.get(syndrome)
            expected = 0 if match is None else match[0] | (match[1] - 1)
            self.assertEqual(self.radio.syndrome_table[syndrome], expected)

    def test_random_and_all_one_two_bit_vectors_preserve_decode(self):
        rng = random.Random(20261004)
        for _ in range(100000):
            word = rng.getrandbits(32)
            self.assertEqual(self.radio._correct_bch(word), self.reference_decode(word))
        for word in (0x7cd215d8, 0x7a89c197, 0xa63ca6ae):
            self.assertEqual(self.radio._correct_bch(word), self.reference_decode(word))
            for i in range(32):
                damaged = word ^ (1 << i)
                self.assertEqual(self.radio._correct_bch(damaged), self.reference_decode(damaged))
                for j in range(i + 1, 32):
                    damaged_two = damaged ^ (1 << j)
                    self.assertEqual(self.radio._correct_bch(damaged_two), self.reference_decode(damaged_two))


if __name__ == '__main__':
    unittest.main()
