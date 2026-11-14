import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from tool_scope import ToolScope
from vkm_worker_core import WorkerError,validate_generated_config

class ToolScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.tree=self.root/'tree';self.tree.mkdir()
        (self.tree/'ok.py').write_text('x=1');(self.root/'outside.txt').write_text('sentinel')
        self.scope=ToolScope(self.tree,{'read_paths':['ok.py'],'edit_paths':['new.py'],'scenario':'code_investigation'},['python -m pytest'],
                             {'graphify_query_graph':{'project_path':'workspace'},'serena_read_file':{'relative_path':'inside'}})
    def tearDown(self):self.tmp.cleanup()
    def test_inside_positive(self):self.assertEqual(self.scope.inspect('read',{'path':str(self.tree/'ok.py')})['relative_path'],'ok.py')
    def test_unlisted_file_denied(self):
        (self.tree/'other.txt').write_text('other')
        with self.assertRaises(WorkerError):self.scope.inspect('read',{'path':'other.txt'})
    def test_traversal_abs_and_ads(self):
        for path in ['../outside.txt',str(self.root/'outside.txt'),'ok.py:stream','//host/share/file','C:/CON']:
            with self.subTest(path=path),self.assertRaises(WorkerError):self.scope.inspect('read',{'path':path})
    def test_edit_exact(self):
        self.scope.inspect('edit',{'path':'new.py'})
        self.scope.inspect('write',{'path':'new.py'})
        with self.assertRaises(WorkerError):self.scope.inspect('edit',{'path':'ok.py'})
        with self.assertRaises(WorkerError):self.scope.inspect('write',{'path':'../outside.txt'})
    def test_native_skill_id_shape(self):
        self.scope.inspect('skill',{'id':'vkm-code-investigation'})
        with self.assertRaises(WorkerError):self.scope.inspect('skill',{'name':'vkm-code-investigation'})
    def test_unqualified_tool(self):
        with self.assertRaises(WorkerError):self.scope.inspect('grep',{'pattern':'*'})
    def test_mcp_exact_project(self):
        self.scope.inspect('graphify_query_graph',{'project_path':str(self.tree)})
        with self.assertRaises(WorkerError):self.scope.inspect('graphify_query_graph',{'project_path':str(self.root)})
    def test_mcp_relative_parent(self):
        with self.assertRaises(WorkerError):self.scope.inspect('serena_read_file',{'relative_path':'../outside.txt'})
    def test_shell_cwd_and_background(self):
        self.scope.inspect('shell',{'command':'python -m pytest'})
        for args in [{'command':'python -m pytest','workdir':str(self.root)},{'command':'python -m pytest','background':True},{'command':'python -c dangerous'}]:
            with self.subTest(args=args),self.assertRaises(WorkerError):self.scope.inspect('shell',args)
    def test_native_reparse_before_dispatch(self):
        link=self.tree/'link'
        if os.name=='nt':
            result=subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(link),str(self.root)],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode,0)
        else:link.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(WorkerError):self.scope.inspect('read',{'path':str(link/'outside.txt')})
        with self.assertRaises(WorkerError):self.scope.inspect('graphify_query_graph',{'project_path':str(self.tree)})
    def test_unknown_native_policy(self):
        with self.assertRaises(WorkerError):validate_generated_config({'policies':[{'action':'unknown.foo','resource':'*','effect':'allow'}],'permissions':[]})

if __name__=='__main__':unittest.main()
