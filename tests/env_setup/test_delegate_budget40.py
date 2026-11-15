"""Explicit owner ceiling40; synthetic scratch ledgers only."""
import copy
import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('delegate_budget40',ROOT/'.agents/skills/openrouter-delegate/scripts/openrouter_call.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)

def ledger():
    return {'budget_usd':'40','key_id':'synthetic-existing-key','spent_usd':'2.038919100','blocked':False,
            'reservations':{},'reservation_leases':{},'reservation_count':37,'owner_local_cap_only':False,
            'authorization_note':'SYNTHETIC FIXTURE ONLY; real owner receipt lives outside repository'}

def test_explicit40_preserves_existing_identity_spend_counters_leases(tmp_path):
    path=tmp_path/'existing.json';before=ledger();d.write_json(path,before)
    token=d.reserve(path,'40',before['key_id'],'39','.01',profile='cheap_code')
    result=d.load_json(path)
    assert result['budget_usd']=='40' and result['spent_usd']==before['spent_usd'] and result['key_id']==before['key_id']
    assert result['reservation_count']==38 and result['authorization_note']==before['authorization_note']
    assert result['reservations']=={token:'0.01'} and result['reservation_leases'][token]['state']=='ACTIVE'
    d.settle(path,token,'.004')
    after=d.load_json(path)
    assert after['spent_usd']=='2.042919100' and after['reservation_count']==38
    assert not after['reservations'] and not after['reservation_leases'] and after['blocked'] is False

def test_above40_rejected_before_ledger_creation(tmp_path):
    path=tmp_path/'never.json'
    with pytest.raises(d.DelegateError,match='LIVE_BLOCKED_BUDGET'):
        d.reserve(path,'40.000001','synthetic-key','100','.01')
    assert not path.exists()

def test_malformed_stored_budget_above40_rejected():
    value=ledger();value['budget_usd']='40.000001'
    with pytest.raises(d.DelegateError,match='LEDGER_CONTRACT_MISMATCH'):d._ledger_fields(value)

@pytest.mark.parametrize('key,budget',[('different-key','40'),('synthetic-existing-key','20')])
def test_identity_or_budget_mismatch_preserves_existing_bytes(tmp_path,key,budget):
    path=tmp_path/'existing.json';d.write_json(path,ledger());before=path.read_bytes()
    with pytest.raises(d.DelegateError,match='LEDGER_CONTRACT_MISMATCH'):
        d.reserve(path,budget,key,'40','.01')
    assert path.read_bytes()==before

def test_existing20_not_migrated_by_explicit40_request(tmp_path):
    path=tmp_path/'old.json';value=ledger();value['budget_usd']='20';d.write_json(path,value);before=path.read_bytes()
    with pytest.raises(d.DelegateError,match='LEDGER_CONTRACT_MISMATCH'):
        d.reserve(path,'40',value['key_id'],'40','.01')
    assert path.read_bytes()==before

def test_active_existing_lease_not_removed(tmp_path):
    path=tmp_path/'existing.json';value=ledger();d.write_json(path,value)
    first=d.reserve(path,'40',value['key_id'],'40','.01');saved=d.load_json(path)
    second=d.reserve(path,'40',value['key_id'],'40','.02');after=d.load_json(path)
    assert after['reservation_leases'][first]==saved['reservation_leases'][first]
    assert after['reservations'][first]=='0.01' and after['reservations'][second]=='0.02'
    assert after['reservation_count']==39 and after['spent_usd']==value['spent_usd']

def test_per_call_cap_unchanged(tmp_path):
    with pytest.raises(d.DelegateError,match='PER_CALL_CAP_EXCEEDED'):
        d.reserve(tmp_path/'never.json','40','synthetic-key','100','.100001',profile='cheap_code')
