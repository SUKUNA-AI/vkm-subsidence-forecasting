import importlib.util, pathlib, tempfile, json, subprocess, os
from unittest.mock import patch
import argparse
parser=argparse.ArgumentParser(description='Offline regressions against the ORIGINAL v1 launcher')
parser.add_argument('--v1-launcher',type=pathlib.Path,required=True)
P=parser.parse_args().v1_launcher
s=importlib.util.spec_from_file_location('v1',P); m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
def cmd(*a): subprocess.run(a,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
def repo_at(r,collision=False):
 r.mkdir();cmd('git','init',str(r));cmd('git','-C',str(r),'config','user.email','test@example.invalid');cmd('git','-C',str(r),'config','user.name','test')
 (r/'main.py').write_text('x=1\n'); (r/'other.py').write_text('y=1\n')
 if collision:
  (r/'.opencode').mkdir(); (r/'.opencode/opencode.jsonc').write_text('{"existing":true}\n')
 cmd('git','-C',str(r),'add','.');cmd('git','-C',str(r),'commit','-m','fixture')
base={'task_id':'VKM-REPRO-001','mode':'edit','profile':'deepseek','objective':'Edit only main.py and nothing else.','acceptance_criteria':['Only main.py changes'],'mcp':[],'budget_usd':0.1,'edit_paths':['main.py'],'max_runtime_sec':30}
results=[]
with tempfile.TemporaryDirectory() as td:
 t=pathlib.Path(td); cli=t/'fake-opencode'; cli.write_text('#!/usr/bin/env python3\nimport sys,pathlib,json\nif "--version" in sys.argv: print("fixture");sys.exit(0)\npathlib.Path("other.py").write_text("unauthorized=1\\n")\nprint(json.dumps({"type":"end"}))\n');cli.chmod(0o755)
 for collision in [True,False]:
  r=t/('repo_collision' if collision else 'repo_scope');repo_at(r,collision)
  job=t/('collision.json' if collision else 'scope.json');job.write_text(json.dumps(base))
  runs=t/('runs_collision' if collision else 'runs_scope')
  with patch.dict(os.environ,{'OPENROUTER_API_KEY':'FAKE_NOT_SENT'}):
   try: out=m.execute(job,r,runs,exe=str(cli),live=True,owner_confirmed=True)
   except m.WorkerError as e: out={'raised':str(e)}
  wt=runs/base['task_id']/'worktree'
  if collision: results.append({'case':'collision_deletes_preexisting_config','reproduced':not (wt/'.opencode/opencode.jsonc').exists(),'original_preserved':(r/'.opencode/opencode.jsonc').exists(),'result':out})
  else: results.append({'case':'out_of_scope_edit_is_not_rejected','reproduced':out['status']=='CLI_COMPLETE_UNVERIFIED' and 'unauthorized' in (wt/'other.py').read_text(),'result':out['status']})
results.append({'case':'wildcard_scope_accepted','reproduced':m.check_path('src/*.py')=='src/*.py'})
results.append({'case':'windows_reserved_name_accepted','reproduced':m.check_path('CON')=='CON'})
print(json.dumps(results,indent=2))
