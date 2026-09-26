export const meta = {
  name: 'corpus-sweep',
  description: 'Deep chunked re-sweep of the VKM/SKRU-1 private corpus: per-chunk records.jsonl + coverage.json',
  phases: [{ title: 'Sweep', detail: 'one deep reader per source chunk' }],
}
const RET = {
  type: 'object',
  properties: {
    source_id: { type: 'string' }, chunk_id: { type: 'string' },
    n_records: { type: 'number' }, n_new: { type: 'number' }, n_boreholes: { type: 'number' },
    n_formulas: { type: 'number' }, n_citations: { type: 'number' }, n_visual_checks: { type: 'number' },
    key_findings: { type: 'array', items: { type: 'string' } },
    blockers: { type: 'array', items: { type: 'string' } },
  },
  required: ['source_id', 'chunk_id', 'n_records', 'key_findings', 'blockers'],
}
const chunks = args.chunks
log(`Sweep group ${args.group}: ${chunks.length} chunk readers`)
phase('Sweep')
const results = await parallel(chunks.map(c => () => agent(
  `You are a deep corpus reader in a scientific re-sweep. FIRST read the protocol file /home/user/work/run/SWEEP_PROTOCOL.md completely and follow it exactly.\n\n` +
  `ASSIGNMENT\n- source_id: ${c.sid}\n- source: ${c.title}\n- chunk_id: ${c.cid}\n- pages: ${c.pages}\n- text location: ${c.text}\n- existing evidence digest: /home/user/work/run/existing/${c.sid}.tsv (may not exist for some sources)\n- output dir: /home/user/work/run/sweep/${c.sid}/${c.cid}/ (create it)\n\n` +
  `FOCUS FOR THIS SOURCE: ${c.focus}\n\n` +
  `Remember: read EVERY page of your range (skim only clearly irrelevant pages and list them), render and look at every project-relevant table/figure/map/section/equation, extract every borehole, formula, parameter, dated event, spatial entity, monitoring item and bibliography entry in the range. Do not modify any repository. Final answer = the compact JSON summary only.`,
  { label: `${c.sid}:${c.cid}`, phase: 'Sweep', schema: RET }
)))
const ok = results.filter(Boolean)
log(`Group ${args.group}: ${ok.length}/${chunks.length} chunks returned`)
return { group: args.group, returned: ok.length, expected: chunks.length, results: ok, missing: chunks.filter((c, i) => !results[i]).map(c => `${c.sid}:${c.cid}`) }
