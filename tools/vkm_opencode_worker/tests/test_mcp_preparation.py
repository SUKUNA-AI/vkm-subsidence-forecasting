import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import mcp_preparation as prep
import vkm_opencode_worker as worker
from vkm_worker_core import WorkerError

def record(path):return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.tree=self.root/'tree';self.tree.mkdir();self.run=self.root/'run';self.run.mkdir();self.owned={}
    def tearDown(self):self.tmp.cleanup()
    def test_serena_control_files_owned_and_exact(self):
        settings={}
        for name,text in [('serena_project_template','read_only: true\n'),('serena_home_template','projects: []\n'),('serena_gitignore_template','cache/\n')]:
            p=self.root/(name+'.txt');p.write_text(text);settings[name]=record(p)
        result=prep.prepare_mcp({'mcp':['serena']},{'mcp_preparation':settings},self.tree,self.run,worker.write_owned,self.owned)
        self.assertEqual((self.tree/'.serena/project.yml').read_text(),'read_only: true\n')
        self.assertEqual(len(self.owned),2);self.assertEqual(result[0]['project_control'],'COORDINATOR_OWNED_NOT_MODEL_EDIT')
    def test_preexisting_serena_project_not_overwritten(self):
        p=self.root/'template';p.write_text('read_only: true\n');settings={n:record(p) for n in ['serena_project_template','serena_home_template','serena_gitignore_template']}
        target=self.tree/'.serena/project.yml';target.parent.mkdir();target.write_text('KEEP')
        with self.assertRaises(FileExistsError):prep.prepare_mcp({'mcp':['serena']},{'mcp_preparation':settings},self.tree,self.run,worker.write_owned,self.owned)
        self.assertEqual(target.read_text(),'KEEP')
    def test_graphify_parser_clean_environment_and_owned_outputs(self):
        exe=self.root/'python.exe';exe.write_bytes(b'fixture')
        def fake_supervise(argv,**kwargs):
            self.assertNotIn('OPENROUTER_API_KEY',kwargs['env']);self.assertNotIn('VKM_WORKER_AUTH',kwargs['env']);self.assertEqual(kwargs['env']['GRAPHIFY_BACKEND'],'none')
            self.assertIn('--code-only',argv);self.assertEqual(argv[argv.index('--out')+1],str(self.tree))
            out=self.tree/'graphify-out';out.mkdir();(out/'graph.json').write_text('{}');return {'lifecycle':'EXITED','returncode':0}
        with patch.object(prep,'supervise',side_effect=fake_supervise),patch.dict(os.environ,{'OPENROUTER_API_KEY':'synthetic','VKM_WORKER_AUTH':'synthetic'}):
            prep.prepare_mcp({'mcp':['graphify']},{'mcp_preparation':{'graphify_python':record(exe)}},self.tree,self.run,worker.write_owned,self.owned)
        self.assertIn(self.tree/'graphify-out/graph.json',self.owned);self.assertTrue((self.run/'mcp/graphify-base/graph.json').is_file())
    def test_existing_graph_preserved(self):
        exe=self.root/'python.exe';exe.write_bytes(b'fixture');(self.tree/'graphify-out').mkdir()
        with self.assertRaises(WorkerError):prep.prepare_mcp({'mcp':['graphify']},{'mcp_preparation':{'graphify_python':record(exe)}},self.tree,self.run,worker.write_owned,self.owned)
    def test_implementation_hash_binds_js_and_powershell(self):
        before=worker.implementation_hash();original=worker.sha256_file
        for suffix in ['.js','.ps1']:
            with self.subTest(suffix=suffix),patch.object(worker,'sha256_file',side_effect=lambda p,s=suffix:'0'*64 if p.suffix==s else original(p)):
                self.assertNotEqual(before,worker.implementation_hash())

if __name__=='__main__':unittest.main()
