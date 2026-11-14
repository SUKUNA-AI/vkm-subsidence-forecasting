"""Real native OpenCode + localhost hook qualification; no real key or API.

This verifies hooks/isolation and cost collection with an explicit fake ledger
object, separately from native SharedSession ledger tests and stock-provider QA.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from vkm_worker_core import json_bytes, write_json, sha256_file, Events
from process_tree import supervise
from request_gate import BudgetThread, HookGate, WirePolicy
from vkm_opencode_worker import child_environment
from tool_scope import ToolScope

class OfflineSession:
    def __init__(self): self.calls = []
    def before_attempt(self, ident, bound, pins, **kwargs):
        assert hashlib.sha256(kwargs['request_body']).hexdigest() == pins['request_sha256']
        self.calls.append({'event':'POSSIBLY_SENT','id':ident,'pins':pins,'bound':bound})
        return {'scope':'LOCAL_MOCK_ONLY','state':'POSSIBLY_SENT'}
    def after_attempt(self, ident, generation, cost, sha, **kwargs):
        self.calls.append({'event':'KNOWN','id':ident,'generation':generation,'cost':cost})
        return {'scope':'SYNTHETIC_COST','state':'KNOWN'}
    def finish(self, **kw): return {'all_known':True,'scope':'LOCAL_MOCK_NOT_REAL_SHARED_LEDGER'}
    def abort(self, *args): return {'all_known':False,'scope':'LOCAL_MOCK_NOT_REAL_SHARED_LEDGER'}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--case',choices=['positive','missing-plugin','over-cap','wrong-route','tool-outside','tool-junction','edit-new','write-new','shell-env','skill-only','http-error'],default='positive')
    parser.add_argument('--ledger-fixture',type=Path,help='Read-only input copied to output; NEVER mutate the supplied ledger')
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=False)
    seen=[]; auth_denials=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_POST(self):
            if self.headers.get('Authorization') != 'Bearer SYNTHETIC_LOCAL_ONLY':
                auth_denials.append({'authorized':False,'actual_secret_absent':True})
                self.send_response(401);self.end_headers();return
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append(body); index=len(seen)
            if args.case=='http-error':
                self.send_response(400);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(b'{"error":{"code":400,"message":"SYNTHETIC native error; key=SYNTHETIC_LOCAL_ONLY"}}');return
            first=index==1
            tool_name='shell' if args.case=='shell-env' else 'skill' if args.case=='skill-only' else 'write' if args.case=='write-new' else 'edit' if args.case=='edit-new' else 'read'
            tool_args=({'command':'python -B -X utf8 env_probe.py'} if args.case=='shell-env' else
                       {'id':'vkm-code-investigation'} if args.case=='skill-only' else
                       {'path':str(workspace/'newdir/generated.py'),'content':'# NATIVE_NEW_FILE\n'} if args.case=='write-new' else
                       {'path':str(workspace/'newdir/generated.py'),'oldString':'','newString':'# NATIVE_NEW_FILE\n'} if args.case=='edit-new' else {'path':str(read_target)})
            delta={'role':'assistant','tool_calls':[{'index':0,'id':'call-local-read','type':'function','function':{'name':tool_name,'arguments':json.dumps(tool_args)}}]} if first else {'role':'assistant','content':'LOCAL_TOOL_HOOK_OK'}
            chunk={'id':'gen-local-'+str(index),'object':'chat.completion.chunk','model':profile['model_id'],'created':1,'choices':[{'index':0,'delta':delta,'finish_reason':None}]}
            end={**chunk,'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls' if first else 'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':3,'total_tokens':13,'cost':0.00001}}
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            self.wfile.write(('data: '+json.dumps(chunk)+'\n\ndata: '+json.dumps(end)+'\n\ndata: [DONE]\n\n').encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    workspace=args.out/'проект с пробелами';workspace.mkdir();(workspace/'fixture.txt').write_text('LOCAL_FIXTURE_ONLY\n',encoding='utf-8')
    if args.case=='shell-env':
        (workspace/'env_probe.py').write_text("import os\nassert not any(k in os.environ for k in ['VKM_WORKER_AUTH','OPENROUTER_API_KEY','VKM_BUDGET_NONCE','VKM_BUDGET_GATE'])\nprint('SHELL_ENV_SAFE')\n",encoding='utf-8')
    read_target=workspace/'fixture.txt'
    if args.case in {'tool-outside','tool-junction'}:
        outside=args.out/'outside';outside.mkdir();(outside/'denied.txt').write_text('FORBIDDEN_NATIVE_SENTINEL_74611',encoding='utf-8')
        read_target=outside/'denied.txt'
        if args.case=='tool-junction':
            import subprocess
            subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(workspace/'link'),str(outside)],check=True,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            read_target=workspace/'link/denied.txt'
    profile={'model_id':'fixture/model','provider_only':['fixture/provider'],'reasoning_effort':'high','max_output_tokens':512}
    if args.ledger_fixture:
        profile.update(model_id='deepseek/deepseek-v4.1-flash-1017',provider_only=['stepfun/fp8'])
    endpoint={'context_length':100000,'max_completion_tokens':512,'pricing':{'prompt':'0.000001','completion':'0.000002'}}
    bound_profile={**profile,'max_output_tokens':64} if args.case=='over-cap' else profile
    target=f'http://127.0.0.1:{server.server_port}/v1/chat/completions'+('/wrong' if args.case=='wrong-route' else '')
    session=OfflineSession()
    factory=lambda:session
    if args.ledger_fixture:
        from shared_budget import SharedSession
        project=Path(__file__).resolve().parents[3]
        client=project/'.agents/skills/openrouter-delegate/scripts/openrouter_call.py'
        policy=project/'.agents/skills/openrouter-delegate/references/DELEGATION_POLICY_V2.json'
        fixture=args.out/'fixture-ledger.json';fixture.write_bytes(args.ledger_fixture.read_bytes())
        record={'client_path':str(client),'client_sha256':sha256_file(client),'policy_path':str(policy),'policy_sha256':sha256_file(policy),
                'requested_reasoning':{'enabled':True,'effort':'high','exclude':True}}
        budget={'ledger_path':str(fixture),'total_budget_usd':'20','key_fingerprint':json.loads(fixture.read_bytes())['key_id'],
                'key_remaining_usd':'18','diagnostic_reserve_usd':'.025'}
        factory=lambda:SharedSession.begin(budget,record,'cheap_code','.10','LOCAL-NATIVE-HOOK-FIXTURE',90,args.out/'shared-session.json')
    scope=ToolScope(workspace,{'read_paths':['fixture.txt'],'edit_paths':['newdir/generated.py'] if args.case in {'edit-new','write-new'} else [],'scenario':'code_investigation'},
                    ['python -B -X utf8 env_probe.py'] if args.case=='shell-env' else [])
    gate=HookGate(BudgetThread(factory),WirePolicy(bound_profile,endpoint,target_url=target),args.out,tool_scope=scope)
    plugin=args.out/'trusted-plugin';plugin.mkdir();shutil.copyfile(Path(__file__).with_name('budget_guard.js'),plugin/'index.js')
    config={'model':'vkm-local/fixture/model','default_agent':'vkm-worker',
      'agents':{'vkm-worker':{'model':'vkm-local/fixture/model','mode':'primary','steps':4}},
      'providers':{'vkm-local':{'package':'@opencode/ai/providers/openai-compatible','settings':{'baseURL':f'http://127.0.0.1:{server.server_port}/v1','apiKey':'VKM_RUNTIME_GATE_REQUIRED_INVALID'},
        'models':{'fixture/model':{'name':'Local test','limit':{'context':100000,'output':512}}},
        'body':{'provider':{'only':profile['provider_only'],'allow_fallbacks':False,'require_parameters':True},'reasoning':{'enabled':True,'effort':'high','exclude':True},'usage':{'include':True}}}},
      'permissions':[{'action':'*','resource':'*','effect':'deny'},{'action':'read','resource':'*','effect':'allow'}],
      'policies':[{'action':'provider.use','resource':'*','effect':'deny'},{'action':'provider.use','resource':'vkm-local','effect':'allow'},
                  {'action':'plugin.use','resource':'*','effect':'deny'},{'action':'plugin.use','resource':'vkm-budget-guard','effect':'allow'},
                  {'action':'plugin.use','resource':str(plugin),'effect':'allow'},
                  {'action':'websearch.use','resource':'*','effect':'deny'}],
      'plugins':[str(plugin)],'compaction':{'auto':False},'snapshots':False,'formatter':False}
    config_path=args.out/'trusted-config.json';write_json(config_path,config,exclusive=True)
    if args.case=='shell-env':
        config['permissions'].append({'action':'shell','resource':'python -B -X utf8 env_probe.py','effect':'allow'});write_json(config_path,config)
    if args.case=='skill-only':
        skillroot=args.out/'trusted-skills';skilldir=skillroot/'vkm-code-investigation';skilldir.mkdir(parents=True)
        original=Path(__file__).resolve().parents[1]/'worker_skills/vkm-code-investigation/SKILL.md'
        (skilldir/'SKILL.md').write_bytes(original.read_bytes())
        config['skills']=[str(skillroot)]
        config['permissions'].append({'action':'skill','resource':'vkm-code-investigation','effect':'allow'})
        config['policies'].append({'action':'skill.use','resource':'vkm-code-investigation','effect':'allow'})
        write_json(config_path,config)
    if args.case in {'edit-new','write-new'}:
        action='edit' if args.case=='edit-new' else 'write'
        config['permissions'].extend([{'action':action,'resource':'newdir/generated.py','effect':'allow'},
                                      {'action':action,'resource':(workspace/'newdir/generated.py').as_posix(),'effect':'allow'}])
        if action=='write':
            config['permissions'].extend([{'action':'edit','resource':'newdir/generated.py','effect':'allow'},
                                         {'action':'edit','resource':(workspace/'newdir/generated.py').as_posix(),'effect':'allow'}])
        write_json(config_path,config)
    if args.ledger_fixture:
        config=json.loads(json.dumps(config).replace('fixture/model',profile['model_id']))
        write_json(config_path,config)
    if args.case=='missing-plugin':
        config['plugins']=[];write_json(config_path,config)
    old=os.environ.get('OPENROUTER_API_KEY');os.environ['OPENROUTER_API_KEY']='SYNTHETIC_LOCAL_ONLY'
    try: env=child_environment({'environment_names':[]},args.out,config_path)
    finally:
        if old is None:os.environ.pop('OPENROUTER_API_KEY',None)
        else:os.environ['OPENROUTER_API_KEY']=old
    env['VKM_BUDGET_GATE']=gate.url;env['VKM_BUDGET_NONCE']=gate.token
    events=Events();stdout=[];stderr=[]
    def line(s): stdout.append(s);events.feed(s)
    try:
        result=supervise([str(args.exe),'run','--standalone','--archived','--format','json','--model','vkm-local/'+profile['model_id'],'--title','local-hook-qualification'],cwd=workspace,env=env,timeout=90,stdout_line=line,stderr_line=stderr.append,stdin_bytes=b'Read fixture.txt with read tool, then report LOCAL_TOOL_HOOK_OK.\n')
    finally: settlement=gate.close(clean=gate.inflight is None);server.shutdown();server.server_close()
    (args.out/'stdout.ndjson').write_text(''.join(stdout),encoding='utf-8');(args.out/'stderr.txt').write_text(''.join(stderr),encoding='utf-8')
    # All inputs here are public synthetic, no actual provider key is read.
    write_json(args.out/'captured-local-requests.json',seen,exclusive=True)
    write_json(args.out/'accounting-events.json',session.calls,exclusive=True)
    attempts=json.loads((args.out/'shared-session.json').read_bytes()).get('attempts',[]) if args.ledger_fixture else []
    accounting_ok=len(attempts)==2 and all(x['state']=='KNOWN' for x in attempts) if args.ledger_fixture else len(session.calls)==4
    expected_tool='shell' if args.case=='shell-env' else 'skill' if args.case=='skill-only' else 'write' if args.case=='write-new' else 'edit' if args.case=='edit-new' else 'read'
    positive=result['returncode']==0 and len(seen)==2 and accounting_ok and events.finished_tools=={expected_tool} and 'LOCAL_TOOL_HOOK_OK' in ''.join(events.text_parts)
    if args.case in {'edit-new','write-new'}:positive=positive and (workspace/'newdir/generated.py').read_text()=='# NATIVE_NEW_FILE\n'
    if args.case=='shell-env':positive=positive and 'SHELL_ENV_SAFE' in json.dumps(seen) and 'SYNTHETIC_LOCAL_ONLY' not in json.dumps(seen)
    if args.case=='http-error':
        ok=result['returncode']!=0 and len(seen)==1 and len(gate.unresolved)==1 and gate.inflight is not None
        if args.ledger_fixture:ok=ok and json.loads((args.out/'fixture-ledger.json').read_bytes())['blocked'] is True
        ok=ok and 'SYNTHETIC_LOCAL_ONLY' not in (args.out/'http-1.unresolved.json').read_text()
    elif args.case in {'tool-outside','tool-junction'}:
        ok=len(seen)==2 and accounting_ok and not events.finished_tools and gate.denied>0 and 'FORBIDDEN_NATIVE_SENTINEL_74611' not in json.dumps(seen)
    else:
        ok=positive if args.case in {'positive','edit-new','write-new','shell-env','skill-only'} else result['returncode']!=0 and len(seen)==0 and len(session.calls)==0 and (bool(auth_denials) if args.case=='missing-plugin' else gate.denied>0)
    receipt={'schema':'vkm.opencode.native-hook-qualification/2','status':'PASS' if ok else 'FAIL','process':result,
      'case':args.case,'real_api_calls':0,'localhost_requests':len(seen),'auth_denied_http_requests':len(auth_denials),'explicit_title':True,'project_config_disabled':True,
      'explicit_plugin_loaded':bool(attempts) if args.ledger_fixture else len(session.calls)>0,'synthetic_accounting':settlement,'events':events.summary(),'denials':gate.denials,
      'plugin_sha256':sha256_file(plugin/'index.js'),'exe_sha256':sha256_file(args.exe),'financial_acceptance':'NOT_IMPLIED'}
    write_json(args.out/'RECEIPT.json',receipt,exclusive=True)
    print(json.dumps({k:receipt[k] for k in ('status','case','real_api_calls','localhost_requests','auth_denied_http_requests','explicit_plugin_loaded','denials')},ensure_ascii=False))
    return 0 if ok else 1
if __name__=='__main__':raise SystemExit(main())
