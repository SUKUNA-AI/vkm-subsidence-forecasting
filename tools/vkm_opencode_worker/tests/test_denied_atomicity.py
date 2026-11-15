"""Native owner controls absent from the frozen9/independent18; no model credit."""
from concurrent.futures import ThreadPoolExecutor
import hashlib,json,sys,tempfile,unittest,urllib.request,urllib.error
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_denied_repro as f

class AdditionalNative80(unittest.TestCase):
    def test_two_concurrent_HTTP_denials_have_unique_records_and_exact_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            g,b=f.setup(td)
            def reject(index):
                p=f.mutate('model','wrong-'+str(index))
                try:f.http(g,'/before',p)
                except urllib.error.HTTPError as exc:self.assertEqual(exc.code,403)
                else:self.fail('denied request unexpectedly permitted')
                return hashlib.sha256(p['body_text'].encode()).hexdigest()
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:hashes=list(pool.map(reject,[1,2]))
                files=f.diagnostics(td);self.assertEqual(len(files),2);self.assertEqual(len(set(p.name for p in files)),2)
                records=[json.loads(p.read_text(encoding='utf-8')) for p in files]
                self.assertEqual({r['body_sha256'] for r in records},set(hashes))
                self.assertTrue(all(r['reason_code']=='MODEL_PIN_MISMATCH' for r in records));self.assertEqual(b.calls,[])
            finally:g.close(clean=True)

    def test_failure_after_durable_before_is_not_false_DENIED_and_holds_inflight(self):
        with tempfile.TemporaryDirectory() as td:
            g,b=f.setup(td);original=f.gate.write_json
            def fail_admission(path,*args,**kwargs):
                if Path(path).name.endswith('.admission.json'):raise OSError('FAKE_ADMISSION_WRITE_FAILURE')
                return original(path,*args,**kwargs)
            try:
                with mock.patch.object(f.gate,'write_json',side_effect=fail_admission):
                    with self.assertRaises(OSError):g.dispatch('/before',f.payload())
                self.assertTrue(b.durable);self.assertEqual(g.inflight,'http-1');self.assertEqual(f.diagnostics(td),[])
            finally:self.assertEqual(g.close(clean=False)['state'],'UNKNOWN')
            self.assertEqual([x[0] for x in b.calls],['before_attempt','abort'])

    def test_failure_after_known_after_does_not_relabel_admitted_as_denied(self):
        with tempfile.TemporaryDirectory() as td:
            g,b=f.setup(td);a=g.dispatch('/before',f.payload());original=f.gate.write_json
            def fail_response(path,*args,**kwargs):
                if Path(path).name.endswith('.response.json'):raise OSError('FAKE_RESPONSE_WRITE_FAILURE')
                return original(path,*args,**kwargs)
            try:
                with mock.patch.object(f.gate,'write_json',side_effect=fail_response):
                    with self.assertRaises(OSError):g.dispatch('/after',{'attempt_id':a['attempt_id'],'generation_id':'gen-fixture','cost':'0.002','response_sha256':'a'*64,'http_status':200})
                self.assertEqual(f.diagnostics(td),[]);self.assertEqual(g.inflight,a['attempt_id'])
            finally:self.assertEqual(g.close(clean=False)['state'],'UNKNOWN')
            self.assertEqual([x[0] for x in b.calls],['before_attempt','after_attempt','abort'])

    def test_unauthenticated_malformed_body_never_hashed_or_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            g,b=f.setup(td)
            try:
                req=urllib.request.Request(g.url+'/before',data=b'{FAKE_PRIVATE_MALFORMED_BODY')
                with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(req,timeout=5)
                self.assertEqual(cm.exception.code,403);r=f.one(td)
                self.assertEqual(r['reason_code'],'HOOK_AUTHENTICATION_FAILED');self.assertIsNone(r['body_sha256']);self.assertIsNone(r['body_utf8_bytes'])
                self.assertEqual(b.calls,[])
            finally:g.close(clean=True)

if __name__=='__main__':unittest.main()
