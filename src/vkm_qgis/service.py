"""Bounded stdlib host: existing QGIS runtime, isolated worker, logical paths and receipts."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from vkm_qgis import __version__
from vkm_qgis.errors import ToolFailure
from vkm_qgis.policy import Paths

TOOLS = {
    'qgis_status', 'qgis_create_geopackage', 'qgis_list_layers', 'qgis_create_layer', 'qgis_feature',
    'qgis_import_vector', 'qgis_register_control_points', 'qgis_fit_transform', 'qgis_residuals',
    'qgis_apply_transform', 'qgis_overlay', 'qgis_export_layer', 'qgis_import_raster', 'qgis_project', 'qgis_render',
}
LOCK = threading.Lock()

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()

def runtime_path():
    configured=os.environ.get('VKM_QGIS_PYTHON')
    root=os.environ.get('VKM_QGIS_ROOT')
    if configured:
        path=Path(configured)
    elif root:
        path=Path(root)/'bin'/'python-qgis.bat' if os.name=='nt' else Path(root)/'bin'/'python3'
    else:
        found=shutil.which('python-qgis.bat') or shutil.which('python-qgis')
        path=Path(found) if found else None
    if path is None or not path.is_file():
        return None
    path=path.resolve()
    if any(c in str(path) for c in '"&|<>^%\r\n'):
        raise ToolFailure('RUNTIME_CONFIG','QGIS executable path contains shell metacharacters')
    return path

class QgisService:
    def __init__(self, paths=None, timeout=120):
        self.paths=paths or Paths.from_env()
        self.timeout=min(max(float(timeout),1),180)

    def _validate(self, tool, args):
        if tool not in TOOLS:
            raise ToolFailure('UNKNOWN_TOOL','Operation is not exposed')
        if not isinstance(args,dict):
            raise ToolFailure('INVALID_ARGUMENT','Arguments must be an object')
        if len(json.dumps(args,allow_nan=False).encode())>20_000_000:
            raise ToolFailure('BUDGET_EXCEEDED','Request exceeds 20 MB')
        for key in ('path','source_path','output_path','transform_path','left_path','right_path'):
            if key not in args:
                continue
            write=(key=='output_path' or (key=='path' and tool in {
                'qgis_create_geopackage','qgis_create_layer','qgis_import_vector','qgis_register_control_points'
            }) or (key=='path' and tool=='qgis_feature' and args.get('action','get') in {'add','update'})
                   or (key=='path' and tool=='qgis_project' and args.get('action','open')=='create'))
            self.paths.resolve(args[key],write=write)
        if tool=='qgis_project':
            for spec in args.get('layers',[]):
                self.paths.resolve(spec['path'])

    def call(self, tool, args=None):
        # Serialize validation, hashes, worker and receipt together within this host.
        with LOCK:
            return self._call(tool,args)

    def _call(self, tool, args=None):
        args={} if args is None else args
        identifier=uuid.uuid4().hex
        started=time.perf_counter()
        stamp=datetime.now(timezone.utc).isoformat()
        payload={'schema':'vkm-qgis.result/1','server':'vkm-qgis','server_version':__version__,
                 'request_id':identifier,'tool':tool,'ok':False,'result':None,'error':None}
        inputs={}; stderr=''; command=[]; code=None
        try:
            self._validate(tool,args)
            for key in ('path','source_path','transform_path','left_path','right_path'):
                if key in args:
                    p=self.paths.resolve(args[key])
                    if p.is_file(): inputs[args[key]]=digest(p)
            runtime=runtime_path()
            if runtime is None:
                if tool=='qgis_status':
                    payload.update(ok=True,result={'available':False,'status':'UNAVAILABLE','bridge_version':__version__,
                                   'reason':'Configure VKM_QGIS_ROOT or VKM_QGIS_PYTHON for an existing installation',
                                   'gui':False,'gpu':False,'epsg_inference':False})
                else:
                    raise ToolFailure('QGIS_RUNTIME_UNAVAILABLE','Existing QGIS runtime was not found')
            else:
                worker=Path(__file__).with_name('worker.py').resolve()
                request={'request_id':identifier,'tool':tool,'args':args,'paths':self.paths.config()}
                env=os.environ.copy()
                env.update(QT_QPA_PLATFORM='offscreen',PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
                if os.name=='nt' and runtime.suffix.lower() in {'.bat','.cmd'}:
                    # Pass a preformed command line: list2cmdline uses C-runtime escaping,
                    # while cmd.exe /s /c uses different quote rules for batch files.
                    command=f'"{os.environ.get("COMSPEC","cmd.exe")}" /d /s /c ""{runtime}" -u "{worker}""'
                else:
                    command=[str(runtime),'-u',str(worker)]
                process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    text=True,encoding='utf-8',errors='replace',env=env,cwd=self.paths.roots['public'],
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                try:
                    stdout,stderr=process.communicate(json.dumps(request,ensure_ascii=True),timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    if os.name=='nt':
                        # Kill ONLY this owned batch/worker process tree, never a shared service.
                        subprocess.run(['taskkill.exe','/PID',str(process.pid),'/T','/F'],capture_output=True,
                                       timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        process.kill()
                    process.communicate(timeout=10)
                    raise
                code=process.returncode
                response=None
                for line in stdout.splitlines():
                    try: candidate=json.loads(line)
                    except json.JSONDecodeError: continue
                    if isinstance(candidate,dict) and candidate.get('request_id')==identifier:
                        response=candidate
                if process.returncode!=0 or response is None:
                    raise ToolFailure('WORKER_PROTOCOL','QGIS worker failed or returned no matching JSON',
                                      details={'exit_code':process.returncode})
                payload.update(ok=response['ok'],result=response.get('result'),error=response.get('error'))
                if tool=='qgis_status' and not payload['ok'] and payload['error']['code']=='QGIS_RUNTIME_UNAVAILABLE':
                    payload.update(ok=True,result={'available':False,'status':'UNAVAILABLE','reason':payload['error']['message']},error=None)
        except ToolFailure as exc:
            payload['error']=exc.as_dict()
        except subprocess.TimeoutExpired:
            payload['error']=ToolFailure('WORKER_TIMEOUT','Bounded QGIS operation exceeded timeout',retryable=False).as_dict()
        except (KeyError,TypeError,ValueError) as exc:
            payload['error']=ToolFailure('INVALID_ARGUMENT',type(exc).__name__+': '+str(exc)[:300]).as_dict()
        except OSError as exc:
            payload['error']=ToolFailure('QGIS_RUNTIME_UNAVAILABLE',type(exc).__name__+': '+str(exc)[:300]).as_dict()
        outputs={}
        if payload['ok'] and isinstance(payload['result'],dict):
            for ref in payload['result'].get('outputs',[]):
                p=self.paths.resolve(ref)
                if p.is_file(): outputs[ref]=digest(p)
        safe_args=json.loads(json.dumps(args,default=str),parse_constant=lambda value:value)
        receipt={'schema':'vkm.qgis_operation_receipt/1','request_id':identifier,'started_at':stamp,
                 'ended_at':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.perf_counter()-started,6),
                 'tool':tool,'arguments':safe_args,'arguments_sha256':hashlib.sha256(json.dumps(safe_args,sort_keys=True,ensure_ascii=True).encode()).hexdigest(),
                 'inputs':inputs,'outputs':outputs,'command':['existing-qgis-python','-u','public:src/vkm_qgis/worker.py'],
                 'exit_code':code,'result':payload,'epistemic_status':'DERIVATION','verification':'AUTO_EXTRACTED_UNREVIEWED'}
        receipts=self.paths.roots['work']/'qgis_2026-09-30'/'receipts'
        receipts.mkdir(parents=True,exist_ok=True)
        destination=receipts/(identifier+'.json')
        destination.write_text(json.dumps(receipt,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        if stderr:
            destination.with_suffix('.stderr.log').write_text(stderr,encoding='utf-8')
        payload['receipt']='work:'+destination.relative_to(self.paths.roots['work']).as_posix()
        return payload

def main():
    print(json.dumps(QgisService().call('qgis_status'),ensure_ascii=True,indent=2))

if __name__=='__main__':
    main()
