"""Offline contracts + real Git and fake CLI regressions. NO model inference.

Native Windows/real OpenCode/MCP acceptance is deliberately NOT claimed here.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import vkm_worker_core as c
import vkm_opencode_worker as w
from process_tree import supervise

KEY = 'sk-or-test-FAKE_NEVER_SENT'
FP = hashlib.sha256(KEY.encode()).hexdigest()


def job(**updates):
    d = {'schema':'vkm.opencode.job/2','task_id':'VKM-TEST-001','base_sha':'1'*40,
         'mode':'review','profile':'deepseek','scenario':'code_investigation',
         'objective':'Inspect the source and report actual coverage without edits.',
         'acceptance_criteria':['Give exact file and line references'], 'mcp':[], 'edit_paths':[],
         'max_steps':4,'max_runtime_sec':30,'required_tools':['read']}
    d.update(updates);return d


def host(root=Path('/tmp/vkm-offline')):
    return {'schema':'vkm.opencode.host/2','platform':'windows-native',
            'opencode':{'path':str(root/'opencode.exe'),'sha256':'1'*64,'version':'2.2.6'},
            'run_root':str(root/'runs'),'state_root':str(root/'state'),'protected_roots':[],
            'approved_plugins':[], 'environment_names':[], 'shell_commands':{},
            'profiles':{'deepseek':{'model_id':'fixture/model','provider_only':['fixture/endpoint'],
                       'reasoning_effort':'high','max_output_tokens':1024,'checked_at':c.utc()}},
            'mcp':{},'budget':{'authorized_total_usd':5,'baseline_usage_usd':0,'authorization_id':'offline-test', 'key_fingerprint':FP}}


def keyinfo(**updates):
    d={'limit':5,'limit_remaining':5,'usage':0,'limit_reset':None,'include_byok_in_limit':True};d.update(updates);return d


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value),encoding='utf-8')
    return {'path':str(path),'sha256':c.sha256_file(path)}


def run(*argv):
    return subprocess.run(argv,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout


def make_repo(root, *, collision=False):
    repo=root/'repo';repo.mkdir();run('git','init',str(repo));run('git','-C',str(repo),'config','user.email','offline@example.invalid');run('git','-C',str(repo),'config','user.name','offline')
    (repo/'main.py').write_text('x = 1\n');(repo/'other.py').write_text('y = 1\n');(repo/'AGENTS.md').write_text('Do not fabricate tests.\n')
    if collision:
        (repo/'.opencode').mkdir();(repo/'.opencode/opencode.jsonc').write_text('{"keep":true}\n')
    run('git','-C',str(repo),'add','.');run('git','-C',str(repo),'commit','-m','fixture')
    return repo,run('git','-C',str(repo),'rev-parse','HEAD').decode().strip()


def make_cli(root, body=''):
    cli=root/'fake-opencode'
    cli.write_text('#!/usr/bin/env python3\nimport json,pathlib,os,sys\n'+body+'\n'
        'print(json.dumps({"type":"tool_use","sessionID":"ses_fixture","part":{"tool":"read","state":{"status":"completed"}}}))\n'
        'print(json.dumps({"type":"text","part":{"type":"text","text":"Observed fixture main.py:1; independent tests NOT_RUN."}}))\n',encoding='utf-8')
    cli.chmod(0o755);return cli


def live_fixture(root, *, body='', collision=False, **updates):
    repo,sha=make_repo(root,collision=collision);cli=make_cli(root,body)
    h=host(root);h['opencode']['path']=str(cli);h['opencode']['sha256']=c.sha256_file(cli)
    proof=write(root/'capabilities.json',{'offline_fixture':True})
    h['profiles']['deepseek']['capability_receipt']=proof;h['budget']['authorization_receipt']=proof
    j=job(base_sha=sha,**updates)
    return repo,h,j


class JobTests(unittest.TestCase):
    def test_valid(self): self.assertEqual(c.validate_job(job())['mode'],'review')
    def test_unknown_field(self):
        with self.assertRaises(c.WorkerError):c.validate_job(job(modle='typo'))
    def test_missing_base(self):
        d=job();del d['base_sha']
        with self.assertRaises(c.WorkerError):c.validate_job(d)
    def test_symbolic_base_forbidden(self):
        with self.assertRaises(c.WorkerError):c.validate_job(job(base_sha='HEAD'))
    def test_review_edit_denied(self):
        with self.assertRaises(c.WorkerError):c.validate_job(job(edit_paths=['src/main.py']))
    def test_edit_requires_files(self):
        with self.assertRaises(c.WorkerError):c.validate_job(job(mode='edit'))
    def test_bool_steps_denied(self):
        with self.assertRaises(c.WorkerError):c.validate_job(job(max_steps=True))
    def test_step_bound(self):
        with self.assertRaises(c.WorkerError):c.validate_job(job(max_steps=100000))
    def test_negative_time(self):
        with self.assertRaises(c.WorkerError):c.validate_job(job(max_runtime_sec=-1))
    def test_duplicate_mcps(self):
        with self.assertRaises(c.WorkerError):c.validate_job(job(mcp=['serena','serena']))
    def test_case_colliding_paths(self):
        with self.assertRaises(c.WorkerError):c.validate_job(job(mode='edit',edit_paths=['src/A.py','src/a.py']))
    def test_duplicate_json_keys(self):
        with self.assertRaises(c.WorkerError):c.parse_json('{"mode":"review","mode":"edit"}')
    def test_nan_json(self):
        with self.assertRaises(c.WorkerError):c.parse_json('{"cost":NaN}')
    def test_invalid_json(self):
        with self.assertRaises(c.WorkerError):c.parse_json('{')


def directory_link(link, target):
    """Exercise an actual reparse point without requiring symlink privilege."""
    if os.name == 'nt':
        result=subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(link),str(target)],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:raise RuntimeError('test junction creation failed')
    else:link.symlink_to(target,target_is_directory=True)


class PathTests(unittest.TestCase):
    def test_literal_file(self):self.assertEqual(c.relative_file('src/Модель.py'),'src/Модель.py')
    def test_wildcard_regression(self):
        for path in ['src/*.py','src/?.py','src/[a].py','src/{x}.py']:
            with self.subTest(path=path),self.assertRaises(c.WorkerError):c.relative_file(path)
    def test_windows_device_regression(self):
        for path in ['CON','src/nul.py','COM1.txt','LPT9','PRN','AUX','CONOUT$']:
            with self.subTest(path=path),self.assertRaises(c.WorkerError):c.relative_file(path)
    def test_traversal(self):
        for path in ['../x','/abs','C:/x','src\\file.py','src/../file.py','a//b','a\n.py','file:stream','trailing.','trailing ']:  # host-path-ok: synthetic negative Windows path fixture
            with self.subTest(path=path),self.assertRaises(c.WorkerError):c.relative_file(path)
    def test_instructions_denied(self):
        for path in ['AGENTS.md','src/agents.md','.git/config','.opencode/foo','CLAUDE.md']:
            with self.subTest(path=path),self.assertRaises(c.WorkerError):c.relative_file(path)
    def test_symlink_denied(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/'outside').mkdir();(p/'root').mkdir();directory_link(p/'root/link',p/'outside')
            with self.assertRaises(c.WorkerError):c.inside(p/'root/link/file',p/'root')


class ConfigTests(unittest.TestCase):
    def config(self,j=None,h=None):return c.build_config(j or job(),h or host(),Path('/work/tree'),Path('/work'))
    def test_skills_really_allowed(self):
        cfg=self.config();self.assertIn({'action':'skill','resource':'vkm-code-investigation','effect':'allow'},cfg['permissions'])
    def test_no_auto_shell(self):
        cfg=self.config();self.assertFalse(any(x['action']=='shell' and x['effect']=='allow' for x in cfg['permissions']))
    def test_internal_relative_edit_path(self):
        cfg=self.config(job(mode='edit',edit_paths=['src/a.py']))
        self.assertIn({'action':'edit','resource':'src/a.py','effect':'allow'},cfg['permissions'])
        self.assertFalse(any(x['action']=='edit' and '*' in x['resource'] for x in cfg['permissions']))
    def test_only_openrouter_provider(self):
        cfg=self.config();self.assertEqual(set(cfg['providers']),{'openrouter'})
        self.assertIn({'action':'provider.use','resource':'*','effect':'deny'},cfg['policies'])
    def test_no_endpoint_fallback(self):
        body=self.config()['providers']['openrouter']['body'];self.assertFalse(body['provider']['allow_fallbacks']);self.assertTrue(body['provider']['require_parameters'])
    def test_reasoning_is_provider_body(self):
        cfg=self.config();self.assertEqual(cfg['providers']['openrouter']['body']['reasoning'],{'enabled':True,'effort':'high','exclude':True});self.assertNotIn('request',cfg['agents']['vkm-worker'])
    def test_maintenance_model_is_pinned(self):
        cfg=self.config()
        for name in ['title','summary','compaction']:self.assertEqual(cfg['agents'][name]['model'],cfg['model'])
    def test_steps_bound_is_used(self):self.assertEqual(self.config()['agents']['vkm-worker']['steps'],4)
    def test_code_mode_off_for_every_server(self):
        h=host();h['mcp']['context7']={'config':{'type':'remote','url':'https://mcp.context7.com/mcp'},'allowed_tools':['resolve-library-id','query-docs'],'access':'read'}
        cfg=self.config(job(mcp=['context7']),h);self.assertIs(cfg['mcp']['servers']['context7']['codemode'],False)
    def test_context7_hyphens_preserved(self):self.assertEqual(c.tool_action('context7','resolve-library-id'),'context7_resolve-library-id')
    def test_hosted_graphify_rejected(self):
        h=host();h['mcp']['graphify']={'config':{'type':'remote','url':'https://api.graphify.com/mcp'},'allowed_tools':['query_graph'],'access':'read'}
        with self.assertRaises(c.WorkerError):self.config(job(mcp=['graphify']),h)
    def test_floating_mcp_dependency_rejected(self):
        h=host();h['mcp']['serena']={'config':{'type':'local','command':['/uvx','--from','git+https://example.invalid/repo','server']},'allowed_tools':['find_symbol'],'access':'read'}
        with self.assertRaises(c.WorkerError):self.config(job(mcp=['serena']),h)
    def test_wildcard_tools_rejected(self):
        h=host();h['mcp']['serena']={'config':{'type':'local','command':[sys.executable]},'allowed_tools':['*'],'access':'read'}
        with self.assertRaises(c.WorkerError):self.config(job(mcp=['serena']),h)
    def test_mutating_mcp_review_rejected(self):
        h=host();h['mcp']['serena']={'config':{'type':'local','command':[sys.executable]},'allowed_tools':['rename_symbol'],'access':'scoped-write','scope_enforced':True}
        with self.assertRaises(c.WorkerError):self.config(job(mcp=['serena']),h)
    def test_obsolete_opencode_rejected(self):
        h=host();h['opencode']['version']='2.1.0'
        with self.assertRaises(c.WorkerError):self.config(h=h)
    def test_shell_glob_rejected(self):
        h=host();h['shell_commands']={'structural_change':['python -m pytest *']}
        with self.assertRaises(c.WorkerError):self.config(job(mode='edit',scenario='structural_change',edit_paths=['main.py']),h)
    def test_no_user_key_in_config(self):self.assertNotIn(KEY,json.dumps(self.config()))


class BudgetTests(unittest.TestCase):
    def check(self,info):return c.check_budget(info,host()['budget'],KEY)
    def test_finite_cap(self):self.assertEqual(self.check(keyinfo())['remaining'],'5')
    def test_unlimited_rejected(self):
        with self.assertRaises(c.WorkerError):self.check(keyinfo(limit=None,limit_remaining=None))
    def test_remaining_exceeds_authorization(self):
        with self.assertRaises(c.WorkerError):self.check(keyinfo(limit=100,limit_remaining=100))
    def test_resets_rejected(self):
        with self.assertRaises(c.WorkerError):self.check(keyinfo(limit_reset='daily'))
    def test_key_rotation_rejected(self):
        with self.assertRaises(c.WorkerError):c.check_budget(keyinfo(),host()['budget'],'another-key')
    def test_exhausted_rejected(self):
        with self.assertRaises(c.WorkerError):self.check(keyinfo(limit_remaining=0))
    def test_byok_not_outside_cap(self):
        with self.assertRaises(c.WorkerError):self.check(keyinfo(include_byok_in_limit=False))
    def test_counter_regression_rejected(self):
        h=host();h['budget']['baseline_usage_usd']=1
        with self.assertRaises(c.WorkerError):c.check_budget(keyinfo(),h['budget'],KEY)
    def test_reduced_remaining_valid(self):self.assertEqual(self.check(keyinfo(usage=1,limit_remaining=4))['observed_batch_spend'],'1')
    def test_nonfinite_money_rejected(self):
        for v in [True,float('nan'),float('inf'),'-1']:
            with self.subTest(v=v),self.assertRaises(c.WorkerError):c.finite_money(v)


class EventTests(unittest.TestCase):
    def test_error_event_counted(self):
        e=c.Events();e.feed('{"type":"error"}');self.assertEqual(e.errors,1)
    def test_malformed_line_not_success(self):
        e=c.Events();e.feed('not json');self.assertEqual(e.bad,1)
    def test_no_fictional_cost(self):
        e=c.Events();e.feed('{"type":"end","cost":123}');self.assertIsNone(e.summary()['cost_usd'])
    def test_reasoning_not_persisted(self):
        e=c.Events();meta=e.feed(json.dumps({'type':'reasoning','part':{'type':'reasoning','text':'PRIVATE'}}));self.assertNotIn('PRIVATE',json.dumps(meta));self.assertEqual(e.text_parts,[])
    def test_tool_payload_not_persisted(self):
        e=c.Events();meta=e.feed(json.dumps({'type':'tool_use','part':{'tool':'read','state':{'status':'completed','output':'secret document'}}}));self.assertNotIn('secret document',json.dumps(meta));self.assertEqual(e.finished_tools,{'read'})
    def test_secret_redacted_from_text(self):
        e=c.Events([KEY]);e.feed(json.dumps({'type':'text','part':{'text':'key='+KEY}}));self.assertNotIn(KEY,''.join(e.text_parts))
    def test_truncation_detected(self):
        e=c.Events();e.feed('{"type":"step_finish","part":{"reason":"length"}}');self.assertTrue(e.truncated_completion)
    def test_environment_does_not_inherit_other_tokens(self):
        with patch.dict(os.environ,{'OPENROUTER_API_KEY':KEY,'OPENAI_API_KEY':'forbidden','GITHUB_TOKEN':'forbidden','OPENCODE_CONFIG':'forbidden','DB_PASSWORD':'forbidden'}):
            env=w.child_environment(host())
            self.assertEqual(env['OPENROUTER_API_KEY'],'VKM_RUNTIME_GATE_REQUIRED_INVALID')
            self.assertEqual(env['VKM_WORKER_AUTH'],KEY)
        for k in ['OPENAI_API_KEY','GITHUB_TOKEN','OPENCODE_CONFIG','DB_PASSWORD']:self.assertNotIn(k,env)


class OwnershipTests(unittest.TestCase):
    def test_checkout_byte_preservation_with_inherited_autocrlf(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,_=make_repo(root)
            (repo/'main.py').write_bytes(b'x = 2\n')
            (repo/'literal.dat').write_bytes(b'literal\r\nbytes\r\n')
            (repo/'.gitattributes').write_bytes(b'literal.dat -text\n')
            run('git','-C',str(repo),'-c','core.autocrlf=false','add','.')
            run('git','-C',str(repo),'commit','-m','pin exact input bytes')
            base=run('git','-C',str(repo),'rev-parse','HEAD').decode().strip()
            before=(repo/'.git/config').read_bytes();tree=root/'fresh byte worktree'
            with patch.dict(os.environ,{'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'core.autocrlf','GIT_CONFIG_VALUE_0':'true'}):
                w.checkout_base(repo,tree,base)
            self.assertEqual((tree/'main.py').read_bytes(),b'x = 2\n')
            self.assertEqual((tree/'literal.dat').read_bytes(),b'literal\r\nbytes\r\n')
            self.assertEqual((repo/'.git/config').read_bytes(),before)
    def test_cleanup_only_owned_files(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'existing';p.write_text('keep');owned={}
            with self.assertRaises(FileExistsError):w.write_owned(p,b'new',owned)
            w.remove_owned(owned);self.assertEqual(p.read_text(),'keep')
    def test_control_tampering_preserved_and_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'ours';owned={};w.write_owned(p,b'before',owned);p.write_bytes(b'changed')
            self.assertEqual(w.remove_owned(owned),[str(p)]);self.assertEqual(p.read_bytes(),b'changed')
    def test_key_lock_exclusive(self):
        with tempfile.TemporaryDirectory() as td:
            state=Path(td);w.take_lock(state,FP,'first')
            with self.assertRaises(c.WorkerError):w.take_lock(state,FP,'second')
    def test_pending_blocks_new_task(self):
        with tempfile.TemporaryDirectory() as td:
            state=Path(td);write(state/(FP+'.pending.json'),{'task_id':'old'})
            with self.assertRaises(c.WorkerError):w.take_lock(state,FP,'new')
    def test_manifest_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'proof.json';rec=write(p,{});p.write_text('{"changed":true}')
            with self.assertRaises(c.WorkerError):c.verify_file_record(rec)


class IntegrationTests(unittest.TestCase):
    def execute(self,j,h,repo):
        # Test-only Python fixture adapter. Live launcher still requires an .exe.
        def fake_process(argv, **kwargs):
            return supervise([sys.executable, argv[0], *argv[1:]], **kwargs)
        class OfflineGate:
            url='http://127.0.0.1:1';token='synthetic-offline-only'
            def close(self,**kw):return {'scope':'TEST_DOUBLE_NO_LEDGER_OR_HTTP','all_known':True}
        with patch.object(w,'start_budget_gate',return_value=OfflineGate()),patch.object(w,'require_windows'),patch.object(w,'verify_native'),patch.object(w,'get_key_info',return_value=keyinfo()),patch.object(w,'supervise',side_effect=fake_process),patch.dict(os.environ,{'OPENROUTER_API_KEY':KEY}):
            return w.execute(j,h,repo)
    def test_plan_is_side_effect_free(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root);p=w.plan(j,h,repo)
            self.assertFalse(Path(h['run_root']).exists());self.assertFalse(p['inference_performed'])
    def test_dirty_source_preserved_and_explicit_base(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root);(repo/'main.py').write_text('dirty=1\n')
            result=self.execute(j,h,repo)
            self.assertEqual(result['status'],'CANDIDATE_READY_FOR_CODEX');self.assertTrue(result['source_unchanged'])
            tree=Path(h['run_root'])/j['task_id']/'worktree';self.assertEqual((tree/'main.py').read_text(),'x = 1\n');self.assertEqual((repo/'main.py').read_text(),'dirty=1\n')
            self.assertTrue((Path(h['state_root'])/(FP+'.pending.json')).exists())
    def test_collision_does_not_delete_original_worktree_file(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root,collision=True)
            with self.assertRaises(c.WorkerError):self.execute(j,h,repo)
            tree=Path(h['run_root'])/j['task_id']/'worktree'
            self.assertEqual((tree/'.opencode/opencode.jsonc').read_text(),'{"keep":true}\n')
            self.assertFalse((Path(h['state_root'])/(FP+'.pending.json')).exists())
    def test_out_of_scope_edit_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root,body='pathlib.Path("other.py").write_text("bad=1\\n")',mode='edit',edit_paths=['main.py'])
            result=self.execute(j,h,repo);self.assertEqual(result['status'],'REVIEW_REQUIRED');self.assertIn('SCOPE_VIOLATION',result['blockers'])
    def test_new_ignored_file_is_still_audited(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root,body='pathlib.Path(".hidden-leak").write_text("bad")')
            result=self.execute(j,h,repo);self.assertIn('OUT_OF_SCOPE:.hidden-leak',result['changes']['violations'])
    def test_allowed_new_file_exported(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root,body='pathlib.Path("new.py").write_text("new=1\\n")',mode='edit',edit_paths=['new.py'])
            result=self.execute(j,h,repo);self.assertEqual(result['status'],'CANDIDATE_READY_FOR_CODEX')
            self.assertEqual((Path(h['run_root'])/j['task_id']/'new-files/new.py').read_text(),'new=1\n')
    def test_staged_out_of_scope_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root,body='import subprocess\npathlib.Path("other.py").write_text("bad=1\\n")\nsubprocess.run(["git","add","other.py"],check=True)',mode='edit',edit_paths=['main.py'])
            result=self.execute(j,h,repo);self.assertIn('OUT_OF_SCOPE:other.py',result['changes']['violations'])
    def test_error_event_exit_zero_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root,body='print(json.dumps({"type":"error"}))')
            result=self.execute(j,h,repo);self.assertEqual(result['status'],'REVIEW_REQUIRED')
    def test_required_mcp_tool_not_inferred(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root,required_tools=['grep'])
            result=self.execute(j,h,repo);self.assertIn('REQUIRED_TOOLS_NOT_OBSERVED',result['blockers'])
    def test_duplicate_task_no_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root);self.execute(j,h,repo)
            with self.assertRaises(c.WorkerError):w.plan(j,h,repo)
    def test_second_task_blocked_by_unsettled_run(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root);self.execute(j,h,repo);j['task_id']='VKM-TEST-002'
            with self.assertRaises(c.WorkerError):self.execute(j,h,repo)
    def test_secret_not_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root,body='print(json.dumps({"type":"text","part":{"text":os.environ["OPENROUTER_API_KEY"]}}))')
            self.execute(j,h,repo);rd=Path(h['run_root'])/j['task_id']
            for p in rd.iterdir():
                if p.is_file():self.assertNotIn(KEY,p.read_text(errors='replace'))
    def test_directory_scope_blocked_before_inference(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root,mode='edit',edit_paths=['src'])
            (repo/'src').mkdir();(repo/'src/a.py').write_text('a=1');run('git','-C',str(repo),'add','.');run('git','-C',str(repo),'commit','-m','dir');j['base_sha']=run('git','-C',str(repo),'rev-parse','HEAD').decode().strip()
            with self.assertRaises(c.WorkerError):self.execute(j,h,repo)
            self.assertFalse((Path(h['state_root'])/(FP+'.pending.json')).exists())
    def test_runs_cannot_be_inside_repo(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,h,j=live_fixture(root);h['run_root']=str(repo/'runs')
            with self.assertRaises(c.WorkerError):w.plan(j,h,repo)


class ProcessTests(unittest.TestCase):
    def call(self,script,**kw):
        with tempfile.TemporaryDirectory() as td:
            out=[];err=[]
            r=supervise([sys.executable,'-c',script],cwd=Path(td),env=dict(os.environ),timeout=kw.pop('timeout',5),stdout_line=out.append,stderr_line=err.append,**kw)
            return r,out,err
    def test_both_pipes_drained(self):
        r,out,err=self.call('import sys;print("out");print("err",file=sys.stderr)');self.assertEqual(r['returncode'],0);self.assertTrue(out and err)
    def test_timeout(self):
        r,_,_=self.call('import time;time.sleep(5)',timeout=0.2);self.assertEqual(r['lifecycle'],'TIMED_OUT')
    def test_line_limit(self):
        r,_,_=self.call('print("x"*4096)',max_line=100);self.assertEqual(r['lifecycle'],'OUTPUT_LIMIT')
    def test_total_output_limit(self):
        r,_,_=self.call('print("abc\\n"*1000)',max_bytes=100);self.assertEqual(r['lifecycle'],'OUTPUT_LIMIT')
    def test_invalid_utf8(self):
        r,_,_=self.call('import sys;sys.stdout.buffer.write(b"\\xff\\n")');self.assertEqual(r['lifecycle'],'INVALID_UTF8')
    def test_callback_failure_still_reaps(self):
        with tempfile.TemporaryDirectory() as td:
            def fail(_):raise RuntimeError('test callback')
            with self.assertRaises(RuntimeError):supervise([sys.executable,'-c','import time;print("x",flush=True);time.sleep(5)'],cwd=Path(td),env=dict(os.environ),timeout=5,stdout_line=fail,stderr_line=lambda _:None)


class ReviewRegressionTests(unittest.TestCase):
    def test_windows_device_task_id_rejected(self):
        with self.assertRaises(c.WorkerError):c.validate_job(job(task_id='CON'))
    def test_task_id_trailing_dot_rejected(self):
        with self.assertRaises(c.WorkerError):c.validate_job(job(task_id='VKM-001.'))
    def test_relative_state_path_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            repo,h,j=live_fixture(Path(td));h['run_root']='relative/runs'
            with self.assertRaises(c.WorkerError):w.plan(j,h,repo)

    def test_credential_header_literal_denied(self):
        with self.assertRaises(c.WorkerError):
            c.check_config_secrets({'type':'remote','url':'https://example.invalid/mcp','headers':{'Authorization':'Bearer abc'}})
    def test_credential_header_env_reference_allowed(self):
        c.check_config_secrets({'type':'remote','url':'https://example.invalid/mcp','headers':{'Authorization':'Bearer {env:VKM_MCP_TOKEN}'}})
    def test_key_environment_literal_denied(self):
        with self.assertRaises(c.WorkerError):
            c.check_config_secrets({'type':'local','command':[sys.executable],'environment':{'CONTEXT7_API_KEY':'secret'}})
    def test_secret_in_argv_denied(self):
        with self.assertRaises(c.WorkerError):
            c.check_config_secrets({'type':'local','command':[sys.executable,'--api-key=secret']})
    def test_url_query_secret_denied(self):
        with self.assertRaises(c.WorkerError):
            c.check_config_secrets({'type':'remote','url':'https://example.invalid/mcp?key=secret'})
    def test_disguised_openrouter_key_denied(self):
        with self.assertRaises(c.WorkerError):
            c.check_config_secrets({'type':'local','command':[sys.executable,KEY]})
    def test_event_schema_qualification_required(self):
        self.assertIn('event_schema',c.RUNTIME_CHECKS)
    def test_do_not_rely_on_ignored_project_update_setting(self):
        conf=c.build_config(job(),host(),Path('/tmp/work'),Path('/tmp/run'))
        self.assertNotIn('update',conf)
        self.assertFalse(conf['formatter']);self.assertFalse(conf['snapshots'])
    def test_owned_cleanup_never_follows_changed_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);owned={};parent=root/'control';parent.mkdir()
            path=parent/'owned.json';w.write_owned(path,b'owned',owned)
            saved=root/'moved';parent.rename(saved)
            directory_link(parent,saved)
            altered=w.remove_owned(owned)
            self.assertTrue(altered);self.assertEqual((saved/'owned.json').read_bytes(),b'owned')


if __name__=='__main__':unittest.main(verbosity=2)
