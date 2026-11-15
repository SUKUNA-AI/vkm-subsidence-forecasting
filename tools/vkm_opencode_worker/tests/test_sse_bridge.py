"""Run the real JS SSE cases in both unittest and repository CPU CI.

A temporary ESM package preserves JS/test bytes and works without relying on
host-specific .js autodetection. No plugin setup, network or model is started.
"""
import json,os,shutil,subprocess,tempfile,unittest
from pathlib import Path
class SSEPortableBridgeTests(unittest.TestCase):
 def test_actual_nine_sse_cases(self):
  node=shutil.which('node');self.assertIsNotNone(node,'Node.js is required for the portable SSE checks; missing dependency is not a PASS')
  package=Path(__file__).resolve().parents[1];script=package/'tests/test_sse.mjs';guard=package/'tools/budget_guard.js'
  with tempfile.TemporaryDirectory(prefix='vkm-sse-cpu-') as tmp:
   root=Path(tmp);(root/'tests').mkdir();(root/'tools').mkdir();(root/'package.json').write_text('{"private":true,"type":"module"}',encoding='utf-8')
   for src,dst in [(script,root/'tests/test_sse.mjs'),(guard,root/'tools/budget_guard.js')]:shutil.copyfile(src,dst);self.assertEqual(src.read_bytes(),dst.read_bytes())
   env={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','HOME','USERPROFILE','APPDATA','LOCALAPPDATA','COMSPEC','PATHEXT'}}
   result=subprocess.run([node,str(root/'tests/test_sse.mjs')],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=30,shell=False)
   self.assertEqual(result.returncode,0,result.stderr)
   report=json.loads(result.stdout.strip());self.assertEqual(report,{'status':'PASS','cases':9,'model_calls':0})
if __name__=='__main__':unittest.main()
