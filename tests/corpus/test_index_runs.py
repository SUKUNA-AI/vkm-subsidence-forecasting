import copy
import random
import unittest

from vkm_corpus.figures.index_runs import ast_review, expand, index_runs


class ModelIndexRunsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_runs=staticmethod(index_runs)

    def test_empty(self):
        self.assertEqual(self.index_runs([]),[])

    def test_exact_synthetic_prompt_example(self):
        self.assertEqual(self.index_runs([6,2,1,0,5,5]),[[0,2],[5,6]])

    def test_no_bridge_over_missing_index(self):
        self.assertEqual(self.index_runs([1,3]),[[1,1],[3,3]])

    def test_boundaries_and_singletons(self):
        self.assertEqual(self.index_runs([0,5,6,10]),[[0,0],[5,6],[10,10]])

    def test_duplicates_and_unsorted(self):
        self.assertEqual(self.index_runs([8,7,8,0,7]),[[0,0],[7,8]])

    def test_does_not_mutate_input(self):
        values=[5,1,2,2,9]
        before=copy.deepcopy(values)
        self.index_runs(values)
        self.assertEqual(values,before)

    def test_invalid_indices_rejected(self):
        for bad in (True,False,-1,1.0,"1",None,complex(1),float("nan")):
            with self.subTest(value=repr(bad)),self.assertRaises(ValueError):
                self.index_runs([0,bad,2])

    def test_large_integer_no_float_cast(self):
        value=2**63+1
        self.assertEqual(self.index_runs([value+1,value]),[[value,value+1]])

    def test_seeded_property_exact_membership(self):
        rng=random.Random(417)
        for _ in range(200):
            values=[rng.randrange(500) for _ in range(rng.randrange(100))]
            self.assertEqual(expand(self.index_runs(values)),sorted(set(values)))

    def test_rejects_import_or_extra_top_level_execution(self):
        for code in ("import os\ndef index_runs(indices): return []", "print(1)\ndef index_runs(indices): return []"):
            with self.subTest(code=code),self.assertRaises(ValueError):
                ast_review(code)

    def test_rejects_io_global_and_dynamic_call_candidates(self):
        for code in ("def index_runs(indices): return open('x')", "def index_runs(indices):\n global runs\n return []",
                     "def index_runs(indices): return indices.clear()", "def index_runs(indices): return __import__('os')"):
            with self.subTest(code=code),self.assertRaises(ValueError):
                ast_review(code)


if __name__=="__main__":
    unittest.main(verbosity=2)
