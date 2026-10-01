"""Synthetic regressions for dossier qualifications and NAV file/origin identity."""
import json
import pytest
duckdb = pytest.importorskip('duckdb')
pa = pytest.importorskip('pyarrow')
pq = pytest.importorskip('pyarrow.parquet')
from vkm_corpus.api import topic
from vkm_corpus.navigation import cli, store
from vkm_corpus.navigation.manifest import UNVERIFIED, VERIFIED
from nav_manifest_fixture import write_nav_manifest

@pytest.mark.parametrize('snapshot_kind', ['same', 'other', 'missing'])
def test_neo4j_source_id_alone_does_not_verify_canonical_identity(tmp_path, snapshot_kind):
    from vkm_corpus.api.fixtures import synthetic_service
    service, _, _ = synthetic_service(tmp_path / 'canon')
    state = {'state': 'READY', 'run_id': 'SYNTHETIC',
             'snapshot_id': {'same': service.canon.snapshot_id(), 'other': 'other-synthetic-snapshot',
                             'missing': None}[snapshot_kind]}
    result = service._nav_graph_result('NAV_GRAPH_PATHS', 'SYNTHETIC', {'items': []}, state)
    assert result.item.envelope.projection.matches_canonical_snapshot is (False if snapshot_kind == 'other' else None)
    assert result.item.record['canonical_identity_status'] == 'NOT_CHECKED'
    assert result.item.record['navigation_only'] is True
    assert result.item.record['scientific_decision'] == 'NOT_CHECKED'

@pytest.mark.parametrize('scope,scale,status,quote_check', [
    ('SKRU1', 'LAB', 'FACT', 'EXACT'),
    ('SKRU1', 'MASSIF', 'UNKNOWN', 'EXACT'),
    ('SKRU1_SKRU2_PILLAR', 'FIELD', 'FACT', 'EXACT'),
    ('SKRU1', 'FIELD', 'FACT', 'NOT_FOUND'),
])
def test_link_qualification_does_not_disappear_from_gap_diagnostics(scope, scale, status, quote_check):
    st = topic._State(topic.TopicRequest(query='modulus'), [])
    p = {'process_id':'PC-SYN', 'required_parameters':'модуль деформации'}
    link = {'vn_id':'EV-SYN', 'kind':'parameter', 'scope':scope, 'scale':scale,
            'status':status, 'quote_check':quote_check, 'source_id':'VKM-SRC-999', 'locator':'synthetic p.1'}
    topic.DossierBuilder(None)._gaps(st, p, [link], {'EV-SYN': {'deformation_modulus'}})
    assert st.gaps, 'Linked parameter must retain scale/status/attribution/QA qualification in gap diagnostics'
    gap = st.gaps[0]
    assert gap['status'] == 'UNKNOWN' and gap['scientific_decision'] == 'NOT_CHECKED'
    assert gap['binding_status'] == 'NOT_CHECKED' and gap['field_coverage'] == 'NOT_ESTABLISHED'
    assert gap['navigation_only'] is True
    assert gap['linked_records'][0]['scope'] == scope and gap['linked_records'][0]['scale'] == scale
    assert gap['linked_records'][0]['status'] == status and gap['linked_records'][0]['quote_check'] == quote_check


def test_causal_projection_keeps_scale_and_source_diagnostics():
    edge = {'edge_id':'CE-SYN', 'from_node':'A', 'to_node':'B', 'status':'FACT', 'scope':'SKRU1',
            'scale':'LAB', 'source_ids':'VKM-SRC-999', 'locator':'synthetic p.1', 'vn_ids':'EV-SYN',
            'quote_check':'NOT_FOUND', 'process_ids':'PC-SYN'}
    class Cat:
        def rows(self, name):
            return ([{'node_id':'A','label_ru':'A','process_ids':'PC-SYN','status':'UNKNOWN'},
                     {'node_id':'B','label_ru':'B'}] if name == 'causal_graph_nodes' else [edge])
    st = topic._State(topic.TopicRequest(query='synthetic'), [])
    topic.DossierBuilder(None)._causal(st, Cat(), {'PC-SYN'})
    assert st.causal[0].get('scale') == 'LAB'
    assert st.causal[0].get('locator') == 'synthetic p.1'
    assert st.causal[0].get('quote_check') == 'NOT_FOUND'
    assert st.causal[0]['from_metadata']['status'] == 'UNKNOWN'
    assert st.causal[0]['source_ids'] == ['VKM-SRC-999'] and st.causal[0]['vn_ids'] == ['EV-SYN']
    assert st.causal[0]['navigation_only'] and st.causal[0]['scientific_decision'] == 'NOT_CHECKED'
    rendered = topic.DossierBuilder(None)._entries(st)[0].lines[0]
    assert 'CE-SYN' in rendered and 'SKRU1/LAB' in rendered and 'NOT_FOUND' in rendered


def test_inputs_without_manifest_do_not_claim_verified_snapshot(tmp_path):
    pq.write_table(pa.table({'section_id':['SEC-SYN']}), tmp_path/'sections.parquet')
    try:
        tables, ref = cli.load_inputs(tmp_path, 'snap-production')
    except ValueError:
        return
    assert ref.get('identity_status') in {'AD_HOC_UNVERIFIED', 'UNVERIFIED'}, 'No manifest must be explicit unverified'


def test_inputs_manifest_hash_and_counts_are_checked(tmp_path):
    pq.write_table(pa.table({'section_id':['SEC-SYN']}), tmp_path/'sections.parquet')
    (tmp_path/'manifest.json').write_text(json.dumps({'format': cli.MANIFEST_FORMAT,
        'snapshot':{'snapshot_id':'snap-production'}, 'datasets':{
            'sections':{'path':'sections.parquet','rows':999,'sha256':'0'*64}}}))
    with pytest.raises(ValueError):
        cli.load_inputs(tmp_path, 'snap-production')


def test_unlisted_parquet_is_never_a_verified_manifest_input(tmp_path):
    pq.write_table(pa.table({'x':['injected']}), tmp_path/'unlisted.parquet')
    (tmp_path/'manifest.json').write_text(json.dumps({'format': cli.MANIFEST_FORMAT,
        'snapshot':{'snapshot_id':'snap-production'}, 'datasets':{}}))
    try:
        tables, ref = cli.load_inputs(tmp_path, 'snap-production')
    except ValueError:
        return
    assert 'unlisted' not in tables


def test_pack_without_manifest_cannot_be_published_as_production_current(tmp_path):
    nav_dir = tmp_path/'derived'/'navigation'/'snap-production'
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({'section_id':['SEC-SYN']}), nav_dir/'sections.parquet')
    try:
        store.pack(nav_dir)
    except (ValueError, store.NavUnavailable):
        return
    with pytest.raises((ValueError, store.NavUnavailable)):
        store.publish(tmp_path,'snap-production')


def test_field_fact_link_still_requires_a_consumer_binding():
    st = topic._State(topic.TopicRequest(query='modulus'), [])
    link = {'vn_id':'EV-SYN', 'kind':'parameter', 'scope':'SKRU1', 'scale':'FIELD', 'status':'FACT',
            'quote_check':'EXACT', 'source_id':'VKM-SRC-999', 'locator':'synthetic p.1', 'evidence_type':'MEASURED'}
    topic.DossierBuilder(None)._gaps(st, {'process_id':'PC-SYN','required_parameters':'модуль деформации'},
                                    [link], {'EV-SYN': {'deformation_modulus'}})
    assert st.gaps[0]['scientific_decision'] == 'NOT_CHECKED'
    assert st.gaps[0]['status'] == 'UNKNOWN' and st.gaps[0]['coverage'] == 'LINKED_RECORDS_REQUIRE_BINDING'


def test_verified_subset_uses_manifest_whitelist_and_preserves_capabilities(tmp_path):
    pq.write_table(pa.table({'section_id':['SEC-SYN']}), tmp_path/'sections.parquet')
    write_nav_manifest(tmp_path, 'snap-production')
    pq.write_table(pa.table({'x':['injected']}), tmp_path/'unlisted.parquet')
    tables, ref = cli.load_inputs(tmp_path, 'snap-production', manifest_sha256='ab'*32, require_verified=True)
    assert set(tables) == {'sections'} and ref['identity_status'] == VERIFIED
    assert ref['capabilities'] == ['sections'] and ref['excluded_unmanifested'] == ['unlisted.parquet']
    assert ref['navigation_only'] and ref['scientific_decision'] == 'NOT_CHECKED'
    packed = store.pack(tmp_path)
    assert packed['snapshot_id'] == 'snap-production' and packed['tables'] == {'sections':1}


@pytest.mark.parametrize('change', ['snapshot_id','manifest_sha256','rows','columns','sha256'])
def test_declared_identity_contradictions_fail_even_for_exploration(tmp_path, change):
    pq.write_table(pa.table({'section_id':['SEC-SYN']}), tmp_path/'sections.parquet')
    manifest = write_nav_manifest(tmp_path, 'snap-production')
    if change in {'snapshot_id','manifest_sha256'}:
        manifest['snapshot'][change] = 'different'
    else:
        manifest['datasets']['sections'][change] = {'rows':2,'columns':['other'],'sha256':'0'*64}[change]
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        cli.load_inputs(tmp_path,'snap-production',manifest_sha256='ab'*32)
    if change not in {'snapshot_id','manifest_sha256'}:
        with pytest.raises(ValueError):
            store.pack(tmp_path)


def test_unverified_inputs_taint_a_build_with_a_named_canonical_snapshot(tmp_path, monkeypatch):
    con = duckdb.connect()
    con.execute('CREATE SCHEMA meta')
    con.execute("CREATE TABLE meta.snapshot AS SELECT 'snap-production' snapshot_id, ? manifest_sha256, "
                "'synthetic' pipeline_version", ['ab'*32])
    inputs = tmp_path/'inputs'
    inputs.mkdir()
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),inputs/'sections.parquet')
    tables, ref = cli.load_inputs(inputs,'snap-production')
    monkeypatch.setattr(cli,'resolve_part',lambda part: lambda con, **kwargs: {
        'parameter_candidates':pa.table({'candidate_id':['PRM-SYN']})})
    manifest = cli.build_parts(con,tmp_path/'out',['parameters'],inputs=tables,inputs_ref=ref)
    con.close()
    assert manifest['identity_status'] == UNVERIFIED
    assert manifest['capabilities'] == ['parameter_candidates']
    assert store.pack(tmp_path/'out')['identity_status'] == UNVERIFIED


def test_verified_partial_build_can_publish_and_current_cannot_invent_origin(tmp_path):
    nav_dir = tmp_path/'derived'/'navigation'/'snap-production'
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),nav_dir/'sections.parquet')
    write_nav_manifest(nav_dir,'snap-production')
    store.pack(nav_dir)
    store.publish(tmp_path,'snap-production')
    (tmp_path/'duckdb').mkdir()
    con = duckdb.connect(str(tmp_path/'duckdb'/'vkm_corpus.duckdb'))
    con.execute('CREATE SCHEMA canonical')
    con.close()
    nav = store.NavStore(tmp_path)
    assert nav.snapshot_id() == 'snap-production' and nav.meta()['identity_status'] == VERIFIED
    assert nav.datasets() == frozenset({'sections'})
    nav._con.close()
    nav._con = None
    # An explicitly exploratory directory carries no canonical origin. CURRENT is only a selector.
    ad_hoc = tmp_path/'derived'/'navigation'/'ad-hoc'
    ad_hoc.mkdir()
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),ad_hoc/'sections.parquet')
    store.pack(ad_hoc)
    store.publish(tmp_path,'ad-hoc',require_verified=False)
    assert nav.snapshot_id() is None and nav.meta()['identity_status'] == UNVERIFIED


def test_api_exposes_unverified_identity_without_snapshot_match(tmp_path):
    from vkm_corpus.api.fixtures import synthetic_service
    service, canon, _fakes = synthetic_service(tmp_path/'canon')
    root = tmp_path/'data'
    snapshot_id = canon.snapshot_id
    nav_dir = root/'derived'/'navigation'/snapshot_id
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),nav_dir/'sections.parquet')
    # A legacy manifest contains a claim, but neither canonical digest nor file identity.
    (nav_dir/'manifest.json').write_text(json.dumps({'snapshot_id':snapshot_id}))
    store.pack(nav_dir)
    store.publish(root,snapshot_id,require_verified=False)
    service.deps.nav = store.NavStore(root,canonical_db=canon.duckdb_path,
                                    functions={'outline':lambda con, sid: [{'section_id':'SEC-SYN'}]})
    result = service.nav_outline('VKM-SRC-001')
    assert result.item.record['identity_status'] == UNVERIFIED
    assert result.item.record['scientific_decision'] == 'NOT_CHECKED'
    assert result.item.envelope.projection.matches_canonical_snapshot is None
    assert result.item.envelope.projection.built_from_snapshot_id is None
    assert 'NAV_IDENTITY_UNVERIFIED' in {w.code for w in result.warnings}


def _canon_with_identity():
    con = duckdb.connect()
    con.execute('CREATE SCHEMA meta')
    con.execute("CREATE TABLE meta.snapshot AS SELECT 'snap-production' snapshot_id, ? manifest_sha256, "
                "'synthetic' pipeline_version", ['ab'*32])
    return con


def test_input_changed_during_builder_never_gets_a_verified_output_manifest(tmp_path, monkeypatch):
    inputs = tmp_path/'inputs'
    inputs.mkdir()
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),inputs/'sections.parquet')
    write_nav_manifest(inputs,'snap-production')
    tables, ref = cli.load_inputs(inputs,'snap-production',require_verified=True)
    def builder(con, **kwargs):
        pq.write_table(pa.table({'section_id':['CHANGED']}),inputs/'sections.parquet')
        return {'parameter_candidates':pa.table({'candidate_id':['PRM-SYN']})}
    monkeypatch.setattr(cli,'resolve_part',lambda part: builder)
    con = _canon_with_identity()
    with pytest.raises(ValueError,match='changed after loading'):
        cli.build_parts(con,tmp_path/'out',['parameters'],inputs=tables,inputs_ref=ref)
    con.close()
    assert not (tmp_path/'out'/'manifest.json').exists()


def test_replaced_in_memory_tables_cannot_borrow_checked_file_identity(tmp_path, monkeypatch):
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),tmp_path/'sections.parquet')
    write_nav_manifest(tmp_path,'snap-production')
    tables, ref = cli.load_inputs(tmp_path,'snap-production',require_verified=True)
    tables['sections'] = pa.table({'section_id':['CHANGED']})
    con = _canon_with_identity()
    with pytest.raises(ValueError,match='consumed tables differ'):
        cli.build_parts(con,tmp_path/'out',['parameters'],inputs=tables,inputs_ref=ref)
    con.close()


def test_manifest_changed_during_pack_preserves_previous_database(tmp_path, monkeypatch):
    from vkm_corpus.navigation import manifest as M
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),tmp_path/'sections.parquet')
    write_nav_manifest(tmp_path,'snap-production')
    store.pack(tmp_path)
    old_digest = M.sha256_file(tmp_path/store.NAV_DB)
    original = M.DatasetReference.verify_unchanged
    def changed(ref):
        (tmp_path/'manifest.json').write_text('{}')
        original(ref)
    monkeypatch.setattr(M.DatasetReference,'verify_unchanged',changed)
    with pytest.raises(ValueError,match='manifest changed'):
        store.pack(tmp_path)
    assert M.sha256_file(tmp_path/store.NAV_DB) == old_digest


def test_changed_manifest_after_pack_blocks_current_switch(tmp_path):
    nav_dir = tmp_path/'derived'/'navigation'/'snap-production'
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),nav_dir/'sections.parquet')
    write_nav_manifest(nav_dir,'snap-production')
    store.pack(nav_dir)
    pq.write_table(pa.table({'section_id':['CHANGED']}),nav_dir/'sections.parquet')
    write_nav_manifest(nav_dir,'snap-production')
    with pytest.raises(store.NavUnavailable,match='current manifest'):
        store.publish(tmp_path,'snap-production')
    assert not (tmp_path/'derived'/'navigation'/'CURRENT').exists()


def test_skipped_rebuild_does_not_reuse_stale_dataset_claims(tmp_path, monkeypatch):
    con = _canon_with_identity()
    monkeypatch.setattr(cli,'resolve_part',lambda part: lambda con: {
        'parameter_candidates':pa.table({'candidate_id':['PRM-SYN']})})
    cli.build_parts(con,tmp_path,['parameters'])
    monkeypatch.setattr(cli,'resolve_part',lambda part: lambda con: None)
    result = cli.build_parts(con,tmp_path,['parameters'])
    con.close()
    assert result['parts']['parameters']['status'] == 'SKIPPED_NO_INPUT'
    assert result['datasets'] == {} and result['capabilities'] == []
    # The old file can exist on disk, but it cannot be packed via the new manifest.
    with pytest.raises(store.NavUnavailable,match='no NAV datasets'):
        store.pack(tmp_path)


@pytest.mark.parametrize('mutation', ['value','schema','extra_table'])
def test_changed_packed_content_cannot_borrow_original_meta_identity(tmp_path, mutation):
    nav_dir = tmp_path/'derived'/'navigation'/'snap-production'
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),nav_dir/'sections.parquet')
    write_nav_manifest(nav_dir,'snap-production')
    store.pack(nav_dir)
    con = duckdb.connect(str(nav_dir/store.NAV_DB))
    if mutation == 'value':
        con.execute("UPDATE sections SET section_id = 'CHANGED'")
    elif mutation == 'schema':
        con.execute('ALTER TABLE sections ADD COLUMN injected VARCHAR')
    else:
        con.execute('CREATE TABLE injected AS SELECT 1 AS injected_value')
    con.close()
    with pytest.raises(store.NavUnavailable,match='packed (tables|dataset)'):
        store.publish(tmp_path,'snap-production')
    assert not (tmp_path/'derived'/'navigation'/'CURRENT').exists()


def test_mislabelled_current_does_not_override_packed_origin(tmp_path):
    nav_dir = tmp_path/'derived'/'navigation'/'wrong-name'
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),nav_dir/'sections.parquet')
    write_nav_manifest(nav_dir,'snap-production')
    store.pack(nav_dir)
    with pytest.raises(store.NavUnavailable,match='name differs'):
        store.publish(tmp_path,'wrong-name',require_verified=False)
    (tmp_path/'derived'/'navigation'/'CURRENT').write_text('wrong-name\n')
    (tmp_path/'duckdb').mkdir()
    duckdb.connect(str(tmp_path/'duckdb'/'vkm_corpus.duckdb')).close()
    with pytest.raises(store.NavUnavailable,match='CURRENT name differs'):
        store.NavStore(tmp_path).snapshot_id()


def test_file_changed_during_read_cannot_receive_a_verified_receipt(tmp_path, monkeypatch):
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),tmp_path/'sections.parquet')
    write_nav_manifest(tmp_path,'snap-production')
    original = pq.read_table
    def changed(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        pq.write_table(pa.table({'section_id':['CHANGED']}),path)
        return result
    monkeypatch.setattr(pq,'read_table',changed)
    with pytest.raises(ValueError,match='changed while reading'):
        cli.load_inputs(tmp_path,'snap-production',require_verified=True)


def test_verified_publish_preserves_duplicates_null_nan_and_nested_values(tmp_path):
    nav_dir = tmp_path/'derived'/'navigation'/'snap-production'
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({'candidate':['A','A','B','C'], 'value':[1.,1.,None,float('nan')],
                             'nested':[[1,2],[1,2],None,[]]}),nav_dir/'parameter_candidates.parquet')
    write_nav_manifest(nav_dir,'snap-production')
    store.pack(nav_dir)
    store.publish(tmp_path,'snap-production')
    assert (tmp_path/'derived'/'navigation'/'CURRENT').read_text().strip() == 'snap-production'


def test_unchanged_nan_input_table_keeps_its_checked_identity(tmp_path, monkeypatch):
    pq.write_table(pa.table({'value':[float('nan')]}),tmp_path/'parameter_candidates.parquet')
    write_nav_manifest(tmp_path,'snap-production')
    tables, ref = cli.load_inputs(tmp_path,'snap-production',require_verified=True)
    monkeypatch.setattr(cli,'resolve_part',lambda part: lambda con, **kwargs: {'sections':pa.table({'id':['SYN']})})
    con = _canon_with_identity()
    result = cli.build_parts(con,tmp_path/'out',['sections'],inputs=tables,inputs_ref=ref)
    con.close()
    assert result['identity_status'] == VERIFIED


def _figure_bundle(directory, *, verified):
    from vkm_corpus.navigation import figure_series as FS
    directory.mkdir(parents=True)
    for dataset in FS.DATASETS:
        pq.write_table(pa.table({'id':['SYN']}),directory/f'{dataset}.parquet')
    manifest = write_nav_manifest(directory,'snap-production')
    for entry in manifest['datasets'].values():
        entry['part'] = FS.PART
    manifest['parts'] = {FS.PART:{'status':'BUILT','rule_version':FS.RULE_VERSION}}
    manifest['identity_status'] = VERIFIED if verified else UNVERIFIED
    (directory/'manifest.json').write_text(json.dumps(manifest))
    return directory


@pytest.mark.parametrize('verified', [False, True])
def test_figure_bundle_merge_preserves_source_identity_taint(tmp_path, verified):
    from vkm_corpus.navigation import figure_series as FS
    bundle = _figure_bundle(tmp_path/'bundle',verified=verified)
    nav_dir = tmp_path/'data'/'derived'/'navigation'/'snap-production'
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),nav_dir/'sections.parquet')
    manifest = write_nav_manifest(nav_dir,'snap-production')
    manifest['identity_status'] = VERIFIED
    (nav_dir/'manifest.json').write_text(json.dumps(manifest))
    FS.import_bundle(bundle,nav_dir)
    merged = json.loads((nav_dir/'manifest.json').read_text())
    assert merged['identity_status'] == (VERIFIED if verified else UNVERIFIED)
    assert store.pack(nav_dir)['identity_status'] == (VERIFIED if verified else UNVERIFIED)
    if verified:
        store.publish(tmp_path/'data','snap-production')
    else:
        with pytest.raises(ValueError,match='AD_HOC_UNVERIFIED'):
            store.publish(tmp_path/'data','snap-production')
        store.publish(tmp_path/'data','snap-production',require_verified=False)


def test_figure_bundle_canonical_digest_conflict_is_rejected_before_copy(tmp_path):
    from vkm_corpus.navigation import figure_series as FS
    bundle = _figure_bundle(tmp_path/'bundle',verified=True)
    nav_dir = tmp_path/'nav'
    nav_dir.mkdir()
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),nav_dir/'sections.parquet')
    manifest = write_nav_manifest(nav_dir,'snap-production')
    manifest['snapshot']['manifest_sha256'] = 'cd'*32
    (nav_dir/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(FS.ImportRefused,match='canonical manifest'):
        FS.import_bundle(bundle,nav_dir)
    assert not (nav_dir/'figure_series.parquet').exists()


def test_null_declared_hash_cannot_receive_verified_identity(tmp_path):
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),tmp_path/'sections.parquet')
    manifest = write_nav_manifest(tmp_path,'snap-production')
    manifest['datasets']['sections']['sha256'] = None
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='sha256'):
        cli.load_inputs(tmp_path,'snap-production',require_verified=True)


@pytest.mark.parametrize('origin', ['inputs','imported'])
def test_legacy_dependency_claims_without_identity_stay_unverified(tmp_path, origin):
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),tmp_path/'sections.parquet')
    manifest = write_nav_manifest(tmp_path,'snap-production')
    if origin == 'inputs':
        manifest['inputs'] = {'snapshot_id':'snap-production','datasets':{'old':{'rows':1}}}
    else:
        manifest['parts'] = {'figure_series':{'imported':{'bundle_manifest_sha256':'ab'*32}}}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    _tables, ref = cli.load_inputs(tmp_path,'snap-production')
    assert ref['identity_status'] == UNVERIFIED


def test_packed_numeric_manifest_hash_cannot_claim_verified_identity(tmp_path):
    nav_dir = tmp_path/'derived'/'navigation'/'snap-production'
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),nav_dir/'sections.parquet')
    write_nav_manifest(nav_dir,'snap-production')
    store.pack(nav_dir)
    con = duckdb.connect(str(nav_dir/store.NAV_DB))
    meta = json.loads(con.execute('SELECT meta_json FROM nav_meta').fetchone()[0])
    meta['manifest_sha256'] = int('1'*64)
    con.execute('UPDATE nav_meta SET meta_json = ?', [json.dumps(meta)])
    con.close()
    (nav_dir.parent/'CURRENT').write_text('snap-production\n')
    (tmp_path/'duckdb').mkdir()
    con = duckdb.connect(str(tmp_path/'duckdb'/'vkm_corpus.duckdb'))
    con.execute('CREATE SCHEMA canonical')
    con.close()
    nav = store.NavStore(tmp_path)
    assert nav.meta()['identity_status'] == UNVERIFIED


@pytest.mark.parametrize('claim', ['part', 'capabilities'])
def test_manifest_cannot_drop_a_still_declared_dataset(tmp_path, claim):
    for name in ('sections', 'section_pages'):
        pq.write_table(pa.table({'id':[name]}),tmp_path/f'{name}.parquet')
    manifest = write_nav_manifest(tmp_path,'snap-production')
    if claim == 'part':
        manifest['parts'] = {'sections':{'status':'BUILT','datasets':['sections','section_pages']}}
    else:
        manifest['capabilities'] = ['sections','section_pages']
    del manifest['datasets']['section_pages']
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='dataset|capabilities'):
        cli.load_inputs(tmp_path,'snap-production',require_verified=True)
    with pytest.raises(ValueError,match='dataset|capabilities'):
        store.pack(tmp_path)


def test_part_dataset_list_must_cover_explicitly_owned_datasets(tmp_path):
    for name in ('sections', 'section_pages'):
        pq.write_table(pa.table({'id':[name]}),tmp_path/f'{name}.parquet')
    manifest = write_nav_manifest(tmp_path,'snap-production')
    manifest['parts'] = {'sections':{'status':'BUILT','datasets':['sections']}}
    for entry in manifest['datasets'].values():
        entry['part'] = 'sections'
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='part.*dataset|dataset.*part'):
        cli.load_inputs(tmp_path,'snap-production',require_verified=True)


def test_genuine_declared_subset_and_skipped_rebuild_are_valid(tmp_path):
    pq.write_table(pa.table({'id':['SYN']}),tmp_path/'sections.parquet')
    manifest = write_nav_manifest(tmp_path,'snap-production')
    manifest['parts'] = {'sections':{'status':'BUILT','datasets':['sections']}}
    manifest['capabilities'] = ['sections']
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    tables, ref = cli.load_inputs(tmp_path,'snap-production',require_verified=True)
    assert set(tables) == {'sections'} and ref['identity_status'] == VERIFIED
    manifest['parts']['sections']['datasets'].append('section_pages')
    manifest['capabilities'].append('section_pages')
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    tables, ref = cli.load_inputs(tmp_path,'snap-production',skip=('section_pages',),require_verified=True)
    assert set(tables) == {'sections'} and ref['identity_status'] == VERIFIED


@pytest.mark.parametrize('claimed_status', ['FAILED', 'UNKNOWN', None])
def test_explicit_unsupported_identity_status_never_upgrades_to_verified(tmp_path, claimed_status):
    pq.write_table(pa.table({'id':['SYN']}),tmp_path/'sections.parquet')
    manifest = write_nav_manifest(tmp_path,'snap-production')
    manifest['identity_status'] = claimed_status
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    _tables, ref = cli.load_inputs(tmp_path,'snap-production')
    assert ref['identity_status'] == UNVERIFIED
    with pytest.raises(ValueError,match='AD_HOC_UNVERIFIED'):
        cli.load_inputs(tmp_path,'snap-production',require_verified=True)


def test_exploratory_publication_taints_mutated_packed_identity(tmp_path):
    nav_dir = tmp_path/'derived'/'navigation'/'snap-production'
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({'section_id':['SYN']}),nav_dir/'sections.parquet')
    write_nav_manifest(nav_dir,'snap-production')
    store.pack(nav_dir)
    con = duckdb.connect(str(nav_dir/store.NAV_DB))
    con.execute("UPDATE sections SET section_id = 'CHANGED'")
    con.close()
    store.publish(tmp_path,'snap-production',require_verified=False)
    (tmp_path/'duckdb').mkdir()
    con = duckdb.connect(str(tmp_path/'duckdb'/'vkm_corpus.duckdb'))
    con.execute('CREATE SCHEMA canonical')
    con.close()
    nav = store.NavStore(tmp_path)
    assert nav.query('SELECT section_id FROM sections') == [{'section_id':'CHANGED'}]
    assert nav.meta()['identity_status'] == UNVERIFIED


@pytest.mark.parametrize('endpoint', ['outline','topic'])
@pytest.mark.parametrize('digest_matches', [False, True])
def test_api_canonical_match_compares_known_source_digest(tmp_path, endpoint, digest_matches):
    from vkm_corpus.api.fixtures import synthetic_service
    service, canon, _fakes = synthetic_service(tmp_path/'canon')
    root = tmp_path/'data'
    nav_dir = root/'derived'/'navigation'/canon.snapshot_id
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({'section_id':['SEC-SYN']}),nav_dir/'sections.parquet')
    manifest = write_nav_manifest(nav_dir,canon.snapshot_id)
    manifest['snapshot']['manifest_sha256'] = service.canon.status()['manifest_sha256'] if digest_matches else 'cd'*32
    (nav_dir/'manifest.json').write_text(json.dumps(manifest))
    store.pack(nav_dir)
    store.publish(root,canon.snapshot_id)
    service.deps.nav = store.NavStore(root,canonical_db=canon.duckdb_path,
                                    functions={'outline':lambda con, sid: [{'section_id':'SEC-SYN'}]})
    result = service.nav_outline('VKM-SRC-001') if endpoint == 'outline' else service.reconstruct_topic('synthetic')
    assert result.item.record.get('identity_status',result.item.record.get('inputs',{}).get('nav_identity_status')) == VERIFIED
    assert result.item.envelope.projection.matches_canonical_snapshot is digest_matches
    assert ('NAV_CANONICAL_IDENTITY_CONFLICT' in {w.code for w in result.warnings}) is not digest_matches


def test_import_rejects_manifest_changed_between_parse_and_checked_load(tmp_path, monkeypatch):
    from vkm_corpus.navigation import figure_series as FS, manifest as M
    bundle = _figure_bundle(tmp_path/'bundle',verified=True)
    nav_dir = tmp_path/'nav'
    nav_dir.mkdir()
    pq.write_table(pa.table({'id':['SYN']}),nav_dir/'sections.parquet')
    target = write_nav_manifest(nav_dir,'snap-production')
    original = M.load_datasets
    def changed(directory, *args, **kwargs):
        if directory == bundle:
            target['snapshot']['manifest_sha256'] = 'cd'*32
            (nav_dir/'manifest.json').write_text(json.dumps(target))
        return original(directory, *args, **kwargs)
    monkeypatch.setattr(M,'load_datasets',changed)
    with pytest.raises(FS.ImportRefused,match='changed|canonical manifest'):
        FS.import_bundle(bundle,nav_dir)
    assert not (nav_dir/'figure_series.parquet').exists()


def test_packed_origin_cannot_contradict_the_checked_source_manifest(tmp_path):
    nav_dir = tmp_path/'derived'/'navigation'/'snap-production'
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({'id':['SYN']}),nav_dir/'sections.parquet')
    write_nav_manifest(nav_dir,'snap-production')
    store.pack(nav_dir)
    con = duckdb.connect(str(nav_dir/store.NAV_DB))
    meta = json.loads(con.execute('SELECT meta_json FROM nav_meta').fetchone()[0])
    meta['snapshot']['manifest_sha256'] = 'cd'*32
    con.execute('UPDATE nav_meta SET meta_json = ?', [json.dumps(meta)])
    con.close()
    with pytest.raises(store.NavUnavailable,match='current manifest'):
        store.publish(tmp_path,'snap-production')
    assert not (nav_dir.parent/'CURRENT').exists()
