from __future__ import annotations
import argparse,csv,hashlib,json,subprocess,sys,traceback,shutil
from pathlib import Path
import numpy as np,pandas as pd


def read_csv(p):return pd.read_csv(p,low_memory=False)
def write_csv(df,p):p.parent.mkdir(parents=True,exist_ok=True);df.to_csv(p,index=False,encoding='utf-8-sig',lineterminator='\n')
def sha(p):
 h=hashlib.sha256();
 with open(p,'rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
def jload(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def jdump(x,p):Path(p).parent.mkdir(parents=True,exist_ok=True);Path(p).write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
def bools(s):return s.astype(str).str.lower().isin(['true','1','yes','y','да'])

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--snapshot',default='/mnt/data/SKRU1_Data_Foundation_v3_2_1');ap.add_argument('--suite',default='/mnt/data/SKRU1_Experiment_Suite_v1');ap.add_argument('--output',default='/mnt/data/SKRU1_Experiment_Suite_v1_independent_audit');a=ap.parse_args()
 root=Path(a.snapshot);suite=Path(a.suite);out=Path(a.output)
 if out.exists():shutil.rmtree(out)
 (out/'tables').mkdir(parents=True);(out/'figures').mkdir()
 checks=[]
 def add(cid,category,desc,observed,expected,ok,severity='critical'):
  checks.append({'check_id':cid,'category':category,'description':desc,'observed':observed,'expected':expected,'status':'PASS' if bool(ok) else 'FAIL','severity':severity})
 # existence and success markers
 add('A001','snapshot','snapshot success marker',(root/'SUCCESS_V3_2_1.txt').exists(),True,(root/'SUCCESS_V3_2_1.txt').exists())
 add('A002','suite','suite success marker',(suite/'SUCCESS_EXPERIMENT_SUITE.txt').exists(),True,(suite/'SUCCESS_EXPERIMENT_SUITE.txt').exists())
 # Hotfix checks
 hot=read_csv(root/'metadata/hotfix_validation_checks.csv')
 add('A003','hotfix','all hotfix checks pass',int((hot.status=='PASS').sum()),len(hot),(hot.status=='PASS').all())
 # membership exact
 m=read_csv(next(root.rglob('campaign_point_membership.csv')));e=read_csv(next(root.rglob('leveling_adjusted_epochs.csv')))
 p='point_id';cm='campaign_id' if 'campaign_id' in m.columns else 'cycle_id';ce='campaign_id' if 'campaign_id' in e.columns else 'cycle_id';obs=bools(m['observed'])
 mk=m[cm].astype(str)+'|'+m[p].astype(str);ek=set(e[ce].astype(str)+'|'+e[p].astype(str))
 add('A004','hotfix','observed iff adjusted epoch exists',int((obs!=mk.isin(ek)).sum()),0,(obs==mk.isin(ek)).all())
 # truth endpoint from validation report
 hrep=jload(root/'metadata/hotfix_validation_report.json');endpoint=hrep['T5']['truth_endpoint']
 add('A005','hotfix','truth endpoint',endpoint,'>=2026-06-30',pd.Timestamp(endpoint)>=pd.Timestamp('2026-06-30'))
 # missing flags
 f=read_csv(root/'model_ready/T1_next_planned_features.csv');flags=['terrain_TRI_relative_is_missing','terrain_roughness_relative_is_missing','lithology_uncertainty_is_unknown']
 add('A006','hotfix','missingness indicators',sum(x in f.columns for x in flags),3,all(x in f.columns for x in flags))
 # no leakage
 forb=['true_','hidden','base_rate','event_amp','decay_tau','settlement_anchor_map']
 leaks=[]
 for pth in (root/'model_ready').rglob('*.csv'):
  for c in pd.read_csv(pth,nrows=2).columns:
   if any(t in c.lower() for t in forb):leaks.append(f'{pth.name}:{c}')
 add('A007','leakage','no forbidden columns in model-ready',len(leaks),0,len(leaks)==0)
 # frozen artifacts
 req=[root/'metadata/data_manifest.csv',root/'metadata/data_checksums.sha256',root/'metadata/formal_feature_contract.csv',root/'metadata/target_contract.json',root/'metadata/frozen_splits.csv',root/'metadata/experiment_protocol.yaml']
 add('A008','freeze','snapshot artifacts',sum(x.exists() for x in req),len(req),all(x.exists() for x in req))
 # split rules
 spl=read_csv(root/'metadata/frozen_splits.csv');td=pd.to_datetime(spl.target_date)
 ok=((spl.split=='train')==(td<=pd.Timestamp('2023-12-31'))).all() and ((spl.split=='validation')==(td.dt.year==2024)).all()
 add('A009','split','frozen temporal split rule',int(ok),1,ok)
 # T1 predictions/metrics
 pred=read_csv(suite/'predictions/T1_predictions.csv');met=read_csv(suite/'tables/T1_all_metrics.csv');inte=read_csv(suite/'tables/T1_all_interval_metrics.csv')
 add('A010','T1','one prediction per sample',int(pred.sample_id.duplicated().sum()),0,not pred.sample_id.duplicated().any())
 add('A011','T1','test predictions exist',int((pred.split=='test').sum()),'>0',(pred.split=='test').sum()>0)
 required_models=['B0_zero','B1_last_rate','B2_mean_last3','B3_robust_linear','B4_quadratic','B5_kalman_fixed','B6_kalman_adaptive','H1_P50']
 add('A012','T1','required model predictions',sum(x in pred.columns for x in required_models),len(required_models),all(x in pred.columns for x in required_models))
 add('A013','T1','required test metric rows',int(((met.split=='test')&(met.target=='observed')).sum()),f'>={len(required_models)}',((met.split=='test')&(met.target=='observed')).sum()>=len(required_models))
 add('A014','intervals','80 and 95 interval tables',sorted(inte.level.dropna().unique().tolist()),'[80,95]',set(inte.level.dropna().unique())>={80,95})
 # interval ordering for hybrid native
 order=((pred.H1_P025<=pred.H1_P10)&(pred.H1_P10<=pred.H1_P50)&(pred.H1_P50<=pred.H1_P90)&(pred.H1_P90<=pred.H1_P975)).mean()
 add('A015','intervals','quantile ordering',float(order),1.0,order==1.0)
 # config and no test tuning
 proto=jload(suite/'metadata/experiment_protocol.json')
 add('A016','protocol','random split forbidden',proto['forbidden']['random_row_split'],True,proto['forbidden']['random_row_split'] is True)
 add('A017','protocol','test tuning forbidden',proto['forbidden']['test_hyperparameter_tuning'],True,proto['forbidden']['test_hyperparameter_tuning'] is True)
 reg=read_csv(suite/'metadata/experiment_registry.csv')
 add('A018','protocol','registry reports no test tuning',int(reg.test_tuning.astype(bool).sum()),0,reg.test_tuning.astype(bool).sum()==0)
 # Ablations
 abl=read_csv(suite/'ablations/ablation_metrics.csv');aset=read_csv(suite/'ablations/ablation_feature_sets.csv')
 add('A019','ablation','E0-E8 present',len(set(abl.experiment_id)&{f'E{i}' for i in range(9)}),9,{f'E{i}' for i in range(9)}.issubset(set(abl.experiment_id)))
 add('A020','ablation','feature sets explicit',len(aset),'>0',len(aset)>0)
 # T5
 t5m=read_csv(suite/'tables/T5_test_metrics.csv');t5p=read_csv(suite/'predictions/T5_test_predictions.csv')
 add('A021','T5','four classifier families',len(set(t5m.model)&{'rule_based','logistic','catboost','hazard'}),4,{'rule_based','logistic','catboost','hazard'}.issubset(set(t5m.model)))
 add('A022','T5','test contains onset events',int(t5p.T5_onset_180d.sum()),'>=2',t5p.T5_onset_180d.sum()>=2)
 add('A023','T5','probabilities in [0,1]',int(((t5p.filter(like='_probability')>=0)&(t5p.filter(like='_probability')<=1)).all().all()),1,((t5p.filter(like='_probability')>=0)&(t5p.filter(like='_probability')<=1)).all().all())
 # spatial, stress, transitions, T6
 spatial=read_csv(suite/'validation/spatial_validation_metrics.csv');trans=read_csv(suite/'validation/regime_transition_metrics.csv');stress=read_csv(suite/'validation/T1_stress_OOD_metrics.csv');t6=read_csv(suite/'tables/T6_profile_metrics.csv')
 add('A024','validation','leave-profile-out present',int((spatial.validation=='leave_profile_out').sum()),'>0',(spatial.validation=='leave_profile_out').sum()>0)
 add('A025','validation','leave-zone-out present',int((spatial.validation=='leave_zone_out').sum()),'>0',(spatial.validation=='leave_zone_out').sum()>0)
 add('A026','validation','regime transition table generated',len(trans),'>=0',trans is not None)
 add('A027','validation','stress/OOD inventory and metrics',(suite/'validation/stress_inventory.csv').exists(),True,(suite/'validation/stress_inventory.csv').exists())
 add('A028','T6','profile derived outputs',len(t6),'>=3',len(t6)>=3)
 # error atlas / decision
 slices=read_csv(suite/'error_atlas/error_slices.csv');worst=read_csv(suite/'error_atlas/worst_cases.csv');prio=read_csv(suite/'decision_layer/monitoring_priority_latest.csv');plan=read_csv(suite/'decision_layer/focused_campaign_research_plan.csv')
 add('A029','error_atlas','error slices generated',len(slices),'>0',len(slices)>0)
 add('A030','error_atlas','worst cases generated',len(worst),'>0',len(worst)>0)
 add('A031','decision','priority score bounded',int(((prio.priority_score>=0)&(prio.priority_score<=1)).all()),1,((prio.priority_score>=0)&(prio.priority_score<=1)).all())
 add('A032','decision','research-only risk marker',int((prio.normative_risk_class=='NOT_ASSIGNED_RESEARCH_ONLY').all()),1,(prio.normative_risk_class=='NOT_ASSIGNED_RESEARCH_ONLY').all())
 # external smoke test: build fixture from first 4 points and 6 epochs
 adj=e.copy();datecol='date' if 'date' in adj.columns else ('campaign_date' if 'campaign_date' in adj.columns else None);settle=next(c for c in adj.columns if 'settlement' in c.lower())
 if datecol is None:
  camps=read_csv(next(root.rglob('campaigns.csv')));adj=adj.merge(camps[['campaign_id','date']],on='campaign_id',how='left');datecol='date'
 fixture=adj.sort_values([p,datecol]).groupby(p).head(6)[[p,datecol,settle]].rename(columns={p:'point_id',datecol:'date',settle:'settlement_mm'});fixture['standard_uncertainty_mm']=1.0
 extdir=suite/'external_validation/smoke_result';extdir.mkdir(exist_ok=True)
 fixture.to_csv(suite/'external_validation/synthetic_smoke_fixture.csv',index=False)
 cmd=[sys.executable,str(suite/'external_validation/run_external_validation.py'),'--input',str(suite/'external_validation/synthetic_smoke_fixture.csv'),'--config',str(suite/'external_validation/frozen_baseline_config.json'),'--output',str(extdir)]
 proc=subprocess.run(cmd,capture_output=True,text=True)
 add('A033','external','frozen external harness smoke test',proc.returncode,0,proc.returncode==0)
 status={'status':'PASS' if proc.returncode==0 else 'FAIL','software_smoke_test':'synthetic_fixture','real_external_test':'PENDING','retraining':False,'stdout':proc.stdout,'stderr':proc.stderr}
 jdump(status,suite/'external_validation/external_validation_status.json')
 df=pd.DataFrame(checks);write_csv(df,out/'tables/independent_checks.csv')
 passed=int((df.status=='PASS').sum());total=len(df);failed=df[df.status!='PASS']
 summary={'status':'PASS' if failed.empty else 'FAIL','checks_total':total,'passed':passed,'failed':len(failed),'T5_test_positive_events':int(t5p.T5_onset_180d.sum()),'best_T1_test_MAE_rate_mm_y':float(met[(met.split=='test')&(met.target=='observed')].rate_MAE.min()),'external_smoke_test':status['status']}
 jdump(summary,out/'audit_summary.json')
 report=f'''# Независимый аудит SKRU-1 Data Foundation v3.2.1 и Experiment Suite v1\n\n- Статус: **{summary['status']}**\n- Пройдено: **{passed}/{total}**\n- Ошибок: **{len(failed)}**\n- T5 positive events in test: **{summary['T5_test_positive_events']}**\n- Frozen external smoke test: **{summary['external_smoke_test']}**\n\nАудит не вызывает функции генератора или обучения. Проверяются экспортированные таблицы, контракты, split-правила, отсутствие leakage, квантильные интервалы, spatial/stress validation, Error Atlas, research-only decision layer и frozen external harness.\n\n## Непройденные проверки\n\n{failed.to_markdown(index=False) if len(failed) else 'Нет.'}\n'''
 (out/'INDEPENDENT_AUDIT_REPORT_RU.md').write_text(report,encoding='utf-8')
 # manifest
 rows=[]
 for f in sorted(out.rglob('*')):
  if f.is_file() and f.name!='audit_manifest.csv':rows.append({'relative_path':f.relative_to(out).as_posix(),'size_bytes':f.stat().st_size,'sha256':sha(f)})
 write_csv(pd.DataFrame(rows),out/'audit_manifest.csv')
 if failed.empty:(out/'SUCCESS_AUDIT.txt').write_text('PASS\n')
 else:raise AssertionError(f'{len(failed)} independent checks failed')
 print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':
 try:main()
 except Exception:
  Path('/mnt/data/SKRU1_Experiment_Suite_v1_independent_audit_FAILED.txt').write_text(traceback.format_exc(),encoding='utf-8');traceback.print_exc();sys.exit(1)
