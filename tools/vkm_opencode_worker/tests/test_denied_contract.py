"""Static independently specified diagnostic enum contract, not inferred behavior."""
import json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import denied_telemetry as dt
class StaticDenialContract(unittest.TestCase):
 def test_independently_declared_enums_and_worker_map(self):
  contract=json.loads((Path(__file__).parent/'fixtures/denied_contract.json').read_text(encoding='utf-8'))
  self.assertEqual(dt.STAGES,frozenset(contract['stages']))
  self.assertEqual(dt.REASON_CODES,frozenset(contract['reason_codes']))
  self.assertEqual(dt.WORKER_ERROR_CODES,contract['WorkerError_exact_map'])
