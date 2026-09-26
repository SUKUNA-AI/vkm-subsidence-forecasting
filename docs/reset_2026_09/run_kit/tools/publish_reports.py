"""Copy synthesis reports into PUBLIC docs/science and rewrite local links to public catalogue paths.

Links to files that are published (per scripts/public_catalogue_map.json) are rewritten to relative PUBLIC
paths; links to private/build artefacts become plain code spans. Long «…» quotations (> 300 chars) are flagged.
Usage: python publish_reports.py STREAM:REPORT.md [...]
"""
import json, os, pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _roots import root, synth_dir  # noqa: E402

PUB = root('VKM_PUB')
SYN = synth_dir()
sys.path.insert(0, str(PUB / 'src'))
from vkm_world.governance.leakage import sanitize_paths  # noqa: E402
mapping = json.loads((PUB / 'scripts/public_catalogue_map.json').read_text(encoding='utf-8'))
by_name = {}
for src, dst in mapping.items():
    by_name.setdefault(pathlib.Path(src).name, dst)
DEST = PUB / 'docs' / 'science'
DEST.mkdir(parents=True, exist_ok=True)
LINK = re.compile(r'(!?)\[([^\]]*)\]\(([^)\s]+)\)')


def fix(text: str, report_name: str) -> tuple[str, list[str]]:
    notes = []

    def rep(m):
        bang, label, target = m.groups()
        if re.match(r'^(https?:|mailto:|#)', target):
            return m.group(0)
        name = pathlib.Path(target.split('#')[0]).name
        if name in by_name:
            rel = os.path.relpath(PUB / by_name[name], DEST)
            return f'{bang}[{label}]({rel})'
        if name.endswith('.md') and (DEST / name).exists():
            return f'[{label}]({name})'
        notes.append(f'unlinked: {target}')
        return f'{label} (`{target}`, PRIVATE/рабочие материалы)' if label != target else f'`{target}`'
    out = LINK.sub(rep, text)
    for q in re.findall(r'«([^»]{300,})»', out):
        notes.append(f'long quotation {len(q)} chars: {q[:60]}…')
    return out, notes


for arg in sys.argv[1:]:
    stream, rep = arg.split(':')
    src = SYN / stream / rep
    text = src.read_text(encoding='utf-8')
    out, notes = fix(text, rep)
    header = (f'<!-- Опубликовано из PRIVATE 11_evidence_vnext/canonical/{stream}/{rep} (синтез Phase 1). '
              f'Дословные цитаты источников — только в PRIVATE. -->\n')
    (DEST / rep).write_text(sanitize_paths(header + out), encoding='utf-8', newline='\n')
    print(rep, len(notes), notes[:4])
