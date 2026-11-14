"""Coordinator-owned, hash-pinned per-run MCP inputs; no installers or models."""
import os
from pathlib import Path
from process_tree import supervise
from vkm_worker_core import need, verify_file_record, sha256_file, write_json, no_reparse

def prepare_mcp(job, host, tree, run_dir, write_owned, owned):
    records=[]; settings=host.get('mcp_preparation',{})
    if 'serena' in job['mcp']:
        project=verify_file_record(settings.get('serena_project_template'))
        home=verify_file_record(settings.get('serena_home_template'))
        write_owned(tree/'.serena/project.yml',project.read_bytes(),owned)
        ignore=verify_file_record(settings.get('serena_gitignore_template'))
        write_owned(tree/'.serena/.gitignore',ignore.read_bytes(),owned)
        destination=run_dir/'mcp/serena-home/serena_config.yml'
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open('xb') as f:f.write(home.read_bytes())
        records.append({'component':'serena','project_template_sha256':sha256_file(project),'home_template_sha256':sha256_file(home),
                        'project_control':'COORDINATOR_OWNED_NOT_MODEL_EDIT','server_access':'READ_ONLY','upstream_path_scope':'NOT_ENFORCED_BY_RAW_SERVER'})
    if 'graphify' in job['mcp']:
        python=verify_file_record(settings.get('graphify_python'))
        output=tree/'graphify-out';need(not output.exists(),'preexisting Graphify index must not be overwritten')
        clean_names={'PATH','PATHEXT','SYSTEMROOT','WINDIR','COMSPEC','PROGRAMDATA','PROGRAMFILES','PROGRAMFILES(X86)','NUMBER_OF_PROCESSORS'}
        env={k:v for k,v in os.environ.items() if k.upper() in clean_names}
        isolated=run_dir/'mcp/graphify-home';isolated.mkdir(parents=True)
        env.update(HOME=str(isolated),USERPROFILE=str(isolated),TEMP=str(isolated),TMP=str(isolated),
                   PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',GRAPHIFY_BACKEND='none',GRAPHIFY_QUERY_LOG_DISABLE='1')
        log=run_dir/'mcp/graphify-extract.log';lines=[]
        # Upstream --out denotes the project/output BASE and appends its own
        # graphify-out child. Passing output here would nest it twice.
        result=supervise([str(python),'-B','-X','utf8','-m','graphify','extract',str(tree),'--out',str(tree),'--code-only','--max-workers','1'],
                         cwd=tree,env=env,timeout=120,stdout_line=lines.append,stderr_line=lines.append,max_bytes=4*1024*1024)
        log.write_text(''.join(lines),encoding='utf-8')
        need(result['lifecycle']=='EXITED' and result['returncode']==0 and (output/'graph.json').is_file(),'Graphify local parser preparation failed')
        files=[]
        for path in sorted(output.rglob('*')):
            no_reparse(path)
            if path.is_file():
                sha=sha256_file(path);owned[path]=sha
                backup=run_dir/'mcp/graphify-base'/path.relative_to(output)
                backup.parent.mkdir(parents=True,exist_ok=True)
                with backup.open('xb') as f:f.write(path.read_bytes())
                files.append({'path':path.relative_to(tree).as_posix(),'sha256':sha})
        records.append({'component':'graphify','process':result,'files':files,'mode':'LOCAL_CODE_ONLY_NO_MODELS_NO_SCIENTIFIC_GRAPH'})
    write_json(run_dir/'mcp-preparation.json',{'schema':'vkm.opencode.mcp-preparation/1','components':records},exclusive=True)
    return records
