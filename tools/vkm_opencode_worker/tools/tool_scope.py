"""Coordinator-side typed tool argument checks, invoked before native dispatch.

Application containment on a trusted workstation; shell executes approved code
with host authority. This is not an operating-system sandbox.
"""
from pathlib import Path, PureWindowsPath
from vkm_worker_core import need, inside, no_reparse, relative_file, SCENARIOS

class ToolScope:
    def __init__(self, tree, job, shell_commands=(), mcp_guards=None):
        self.tree=Path(tree).resolve(); self.job=job
        self.reads=set(job.get('read_paths',[])) | set(job.get('edit_paths',[])) | {'AGENTS.md'}
        self.edits=set(job.get('edit_paths',[]));self.commands=set(shell_commands)
        self.mcp_guards=mcp_guards or {}

    def _path(self, value, *, root_only=False):
        need(isinstance(value,str) and value and '\x00' not in value, 'tool path missing')
        # Do not accept explicit traversal, ADS, UNC or device spellings even if
        # their resolved path happens to point back into this workspace.
        parts=value.replace('\\','/').split('/')
        need('..' not in parts and not value.startswith(('\\\\','//','\\\\?\\')), 'tool traversal/device path denied')
        need(':' not in value[2:] if len(value)>2 and value[1]==':' else ':' not in value, 'tool ADS denied')
        path=Path(value)
        if not path.is_absolute():path=self.tree/path
        inside(path,self.tree)
        resolved=path.resolve()
        need(not root_only or resolved==self.tree,'MCP project root differs from workspace')
        return resolved

    def inspect(self, tool, args):
        need(isinstance(tool,str) and isinstance(args,dict),'tool input shape invalid')
        if tool in {'read','edit','write','patch'}:
            need(tool in {'read','edit','write'},'unqualified mutation tool')
            path=self._path(args.get('path'))
            rel=path.relative_to(self.tree).as_posix()
            relative_file(rel,read_only=tool=='read')
            allowed=self.reads if tool=='read' else self.edits
            need(rel.casefold() in {v.casefold() for v in allowed},'tool file outside exact allowlist')
            return {'tool':tool,'relative_path':rel,'scope':'EXACT_FILE_NO_REPARSE'}
        if tool=='shell':
            need(args.get('command') in self.commands,'shell command outside exact allowlist')
            self._path(args.get('workdir',str(self.tree)),root_only=True)
            need(not args.get('background'),'background shell denied')
            return {'tool':tool,'scope':'EXACT_AUDITED_COMMAND_TRUSTED_WORKSTATION'}
        if tool=='skill':
            need(set(args)=={'id'} and args.get('id')==SCENARIOS[self.job['scenario']],'unapproved scenario skill')
            return {'tool':tool,'scope':'PINNED_SCENARIO'}
        need(tool in self.mcp_guards,'MCP tool lacks typed scope qualification')
        rules=self.mcp_guards[tool]
        need(isinstance(rules,dict),'MCP guard specification missing')
        # Only exact, reviewed argument names. No generic guessing that any
        # arbitrary string containing a path is a filesystem dependency.
        for name,mode in rules.items():
            need(mode in {'workspace','inside','inside_optional','inline'},'unknown MCP scope rule')
            if mode=='inline':continue
            if mode=='inside_optional' and (name not in args or args[name] in (None,'')):continue
            self._path(args.get(name),root_only=mode=='workspace')
        # Stateful language/graph servers may scan the project recursively.
        # Reject all newly created junctions before invoking such a server.
        for item in self.tree.rglob('*'):no_reparse(item)
        return {'tool':tool,'scope':'TYPED_MCP_ARGUMENTS_NO_REPARSE','checked_arguments':sorted(rules)}
