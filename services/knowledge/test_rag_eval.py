import unittest

from evals import eval_rag


class RAGEvaluationTests(unittest.TestCase):
    def test_historical_questions_retrieve_right_evidence_without_cross_product_hits(self):
        temp, kb = eval_rag.fixture()
        try:
            summary = eval_rag.evaluate(kb)['summary']
        finally:
            temp.cleanup()
        self.assertEqual(summary['positive'], 26)
        self.assertEqual(summary['negative'], 3)
        self.assertEqual(summary['all_gold_at_5'], 26)
        self.assertEqual(summary['any_gold_at_1'], 26)
        self.assertEqual(summary['forbidden_at_5'], 0)
