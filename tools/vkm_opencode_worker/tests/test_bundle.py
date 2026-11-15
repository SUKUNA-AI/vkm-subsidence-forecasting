"""Checks for the actual deliverable, not installed external runtimes."""
import ast
import json
from pathlib import Path
import re
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import vkm_worker_core as c

class BundleTests(unittest.TestCase):
    def test_all_python_sources_parse(self):
        for p in ROOT.rglob('*.py'):
            with self.subTest(path=p.relative_to(ROOT)):ast.parse(p.read_text(encoding='utf-8'),filename=str(p))
    def test_coordinator_skill_frontmatter(self):
        skill=ROOT.parents[1]/'.agents/skills/vkm-opencode-worker'
        s=(skill/'SKILL.md').read_text(encoding='utf-8')
        self.assertTrue(s.startswith('---\n'));self.assertIn('name: vkm-opencode-worker\n',s)
        self.assertTrue((skill/'references/OPERATIONS.md').is_file())
    def test_all_worker_skills_exist(self):
        for name in c.SCENARIOS.values():
            p=ROOT/'worker_skills'/name/'SKILL.md';s=p.read_text(encoding="utf-8")
            self.assertTrue(s.startswith('---\n'));self.assertIn('name: '+name+'\n',s)
    def test_host_template_cannot_be_mistaken_for_live(self):
        h=json.loads((ROOT/'templates/host.template.json').read_text(encoding="utf-8"))
        with self.assertRaises(c.WorkerError):c.validate_host(h,{'profile':'deepseek','mcp':[],'mode':'review'},evidence=False)
    def test_job_template_requires_actual_base(self):
        j=json.loads((ROOT/'examples/coverage-investigation.template.json').read_text(encoding="utf-8"))
        with self.assertRaises(c.WorkerError):c.validate_job(j)
    def test_native_status_is_honest(self):
        n=json.loads((ROOT/'validation/native-status.json').read_text(encoding="utf-8"))
        self.assertFalse(n['checked_here']);self.assertEqual(set(n['checks'].values()),{'NOT_RUN'})
    def test_required_documents_present(self):
        for name in ['README.md','CODEX_TASK_RU.md','docs/REVIEW_RU.md','docs/DEPLOYMENT_RU.md','docs/CONTRACT_RU.md','docs/MCP_AND_MEMORY_RU.md','docs/SOURCES.md']:
            self.assertGreater((ROOT/name).stat().st_size,100)
    def test_input_archives_identified(self):
        e=json.loads((ROOT/'validation/input-packages.json').read_text(encoding="utf-8"));self.assertEqual(len(e['inputs']),2)
        for item in e['inputs']:self.assertRegex(item['sha256'],r'^[a-f0-9]{64}$')
    def test_v1_counterexamples_preserved(self):
        e=json.loads((ROOT/'validation/v1-counterexamples.json').read_text(encoding="utf-8"));self.assertEqual(len(e),4)
        self.assertTrue(all(r['reproduced'] for r in e))

if __name__=='__main__':unittest.main(verbosity=2)
