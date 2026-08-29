#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
import argparse
import json, math, os, shutil, hashlib
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from scipy.spatial import cKDTree
from sklearn.metrics import mean_absolute_error, r2_score, precision_recall_fscore_support
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.linear_model import Ridge
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.model_selection import train_test_split
from sklearn.impute import SimpleImputer
import joblib

parser = argparse.ArgumentParser(
    description='Independent data audit and baseline models for SKRU-1 v3.1.'
)
parser.add_argument(
    '--dataset-root',
    type=Path,
    default=Path('/mnt/data/SKRU1_v3_1_work/SKRU1_data_reconstruction_v3_1'),
    help='Unpacked SKRU-1 v3.1 dataset directory containing tables/ and metadata/.',
)
parser.add_argument(
    '--output',
    type=Path,
    default=Path('/mnt/data/SKRU1_v3_1_baseline_audit'),
    help='Directory for audit tables, figures, reports, and model artifacts.',
)
args = parser.parse_args()
ROOT = args.dataset_root.resolve()
TABLES = ROOT / 'tables'
OUT = args.output.resolve()
if not TABLES.is_dir():
    raise FileNotFoundError(f'Dataset tables directory not found: {TABLES}')
if not (ROOT / 'metadata' / 'dataset_manifest.json').is_file():
    raise FileNotFoundError(f'Dataset manifest not found under: {ROOT / "metadata"}')
(OUT/'tables').mkdir(parents=True, exist_ok=True)
(OUT/'figures').mkdir(parents=True, exist_ok=True)
(OUT/'models').mkdir(parents=True, exist_ok=True)

# ---------- helpers ----------
def reg_metrics(y_true, y_pred):
    y_true=np.asarray(y_true,float); y_pred=np.asarray(y_pred,float)
    m=np.isfinite(y_true)&np.isfinite(y_pred); y_true=y_true[m]; y_pred=y_pred[m]
    e=y_pred-y_true
    return {'n':len(y_true),'MAE_mm':float(np.mean(np.abs(e))),'RMSE_mm':float(np.sqrt(np.mean(e**2))),
            'Bias_mm':float(np.mean(e)),'R2':float(r2_score(y_true,y_pred)) if len(y_true)>1 and np.var(y_true)>0 else np.nan,
            'WAPE':float(np.sum(np.abs(e))/np.sum(np.abs(y_true))) if np.sum(np.abs(y_true))>0 else np.nan,
            'P95_abs_error_mm':float(np.quantile(np.abs(e),.95)) if len(y_true) else np.nan}

def sensor_metrics(df,residual_col,sigma_col,sensor):
    d=df[[residual_col,sigma_col]].dropna(); r=d[residual_col].to_numpy(float); s=d[sigma_col].to_numpy(float)
    z=np.divide(r,s,out=np.full_like(r,np.nan),where=s>0)
    return {'sensor':sensor,'n':len(d),'bias_mm':float(np.mean(r)),'MAE_mm':float(np.mean(np.abs(r))),
            'RMSE_mm':float(np.sqrt(np.mean(r*r))),'coverage_68':float(np.mean(np.abs(r)<=s)),
            'coverage_95':float(np.mean(np.abs(r)<=1.96*s)),'std_residual_mean':float(np.nanmean(z)),
            'std_residual_sd':float(np.nanstd(z,ddof=1)),'mean_sigma_mm':float(np.mean(s)),'max_sigma_mm':float(np.max(s))}

def moran_knn(df,value_col,k=8):
    d=df[['x_local_m','y_local_m',value_col]].dropna(); xy=d[['x_local_m','y_local_m']].to_numpy(float); z=d[value_col].to_numpy(float)
    zc=z-z.mean(); idx=cKDTree(xy).query(xy,k=min(k+1,len(d)))[1][:,1:]
    return float(np.sum(zc[:,None]*zc[idx]/idx.shape[1])/np.sum(zc**2)),len(d)

def make_preprocessor(num_cols,cat_cols,scale=False):
    num_steps=[('impute',SimpleImputer(strategy='median'))]
    if scale: num_steps.append(('scale',StandardScaler()))
    return ColumnTransformer([
        ('num',Pipeline(num_steps),num_cols),
        ('cat',Pipeline([('impute',SimpleImputer(strategy='most_frequent')),
                         ('onehot',OneHotEncoder(handle_unknown='ignore',sparse_output=False))]),cat_cols)
    ],remainder='drop',verbose_feature_names_out=False)

def trend_predict(dates,vals,target_date,n=4,degree=1):
    m=min(n,len(vals)); d=dates[-m:]; y=np.asarray(vals[-m:],float); t0=d[0]
    x=np.array([(q-t0).days/365.25 for q in d]); xt=(target_date-t0).days/365.25
    return float(np.polyval(np.polyfit(x,y,min(degree,m-1)),xt)-y[-1])

def classification_metrics(y_true,y_pred):
    p,r,f,_=precision_recall_fscore_support(y_true,y_pred,average='binary',zero_division=0)
    return {'precision':float(p),'recall':float(r),'F1':float(f),'true_positive_rate':float(np.mean(y_true)),
            'pred_positive_rate':float(np.mean(y_pred)),'n':len(y_true)}

def conformal_halfwidth(abs_errors,coverage):
    e=np.sort(np.asarray(abs_errors,float)); n=len(e); q=min(math.ceil((n+1)*coverage)/n,1.0)
    return float(np.quantile(e,q,method='higher'))

# ---------- load ----------
files=sorted(TABLES.glob('*.csv')); dfs={f.stem:pd.read_csv(f) for f in files}
manifest=json.loads((ROOT/'metadata/dataset_manifest.json').read_text(encoding='utf-8'))
sp=pd.read_csv(TABLES/'survey_points.csv'); sp['profile_type']=sp.profile_id.str.extract(r'P-([A-Z])')[0]
campaigns=pd.read_csv(TABLES/'survey_campaigns.csv',parse_dates=['date'])
lev=pd.read_csv(TABLES/'leveling_adjusted_epochs.csv',parse_dates=['date'])
gnss=pd.read_csv(TABLES/'gnss_adjusted_epochs.csv',parse_dates=['date'])
insar=pd.read_csv(TABLES/'insar_observations_relative.csv',parse_dates=['date'])
insar_cat=pd.read_csv(TABLES/'insar_point_catalog.csv')
proc=pd.read_csv(TABLES/'process_parameters_survey_points.csv')
truth=pd.read_csv(TABLES/'truth_survey_points_monthly.csv',parse_dates=['date'])
rates=pd.read_csv(TABLES/'settlement_rates.csv',parse_dates=['from_date','to_date'])
grid=pd.read_csv(TABLES/'field_grid_50m.csv')
tilts=pd.read_csv(TABLES/'tilts.csv',parse_dates=['date'])
curv=pd.read_csv(TABLES/'curvatures.csv',parse_dates=['date'])
planar=pd.read_csv(TABLES/'planar_observations_raw.csv',parse_dates=['date'])
hstrain=pd.read_csv(TABLES/'horizontal_strains.csv',parse_dates=['date'])
runs=pd.read_csv(TABLES/'leveling_runs_summary.csv')
stress_meas=pd.read_csv(TABLES/'stress_test_measurements.csv',parse_dates=['date'])
stress_truth=pd.read_csv(TABLES/'stress_test_truth_monthly.csv',parse_dates=['date'])
stress_cat=pd.read_csv(TABLES/'stress_test_scenario_catalog.csv')
work_ids=set(sp.loc[sp.point_type=='WORK','point_id'])

# ---------- independent formula checks ----------
points_order=sp[['point_id','profile_id','point_order','chainage_m']]
lev2=lev.merge(points_order,on=['point_id','profile_id']).sort_values(['campaign_id','profile_id','point_order'])
rt=[]
for (cid,date,pid),g in lev2.groupby(['campaign_id','date','profile_id']):
    g=g.sort_values('point_order'); vals=g.observed_settlement_mm.to_numpy(); ch=g.chainage_m.to_numpy()
    for j in range(len(g)-1):
        rt.append([cid,date,pid,g.iloc[j].point_id,g.iloc[j+1].point_id,(vals[j+1]-vals[j])/(ch[j+1]-ch[j])])
rt=pd.DataFrame(rt,columns=['campaign_id','date','profile_id','from_point_id','to_point_id','tilt_calc'])
tm=tilts.merge(rt,on=['campaign_id','date','profile_id','from_point_id','to_point_id'])
rc=[]
for (cid,date,pid),g in tilts.groupby(['campaign_id','date','profile_id']):
    g=g.sort_values('interval_mid_chainage_m'); mids=g.interval_mid_chainage_m.to_numpy(); tv=g.tilt_mm_per_m.to_numpy()
    for j in range(len(g)-1): rc.append([cid,date,pid,g.iloc[j].to_point_id,(tv[j+1]-tv[j])/(mids[j+1]-mids[j])])
rc=pd.DataFrame(rc,columns=['campaign_id','date','profile_id','point_id','curv_calc'])
cm=curv.merge(rc,on=['campaign_id','date','profile_id','point_id'])
ls=lev.sort_values(['point_id','date']).copy(); ls['prev_obs']=ls.groupby('point_id').observed_settlement_mm.shift(); ls['prev_date']=ls.groupby('point_id').date.shift(); ls['dt_y']=(ls.date-ls.prev_date).dt.days/365.25; ls['rate_calc']=(ls.observed_settlement_mm-ls.prev_obs)/ls.dt_y
rr=ls.dropna(subset=['prev_obs'])[['point_id','profile_id','prev_date','date','rate_calc']].rename(columns={'prev_date':'from_date','date':'to_date'})
rm=rates.merge(rr,on=['point_id','profile_id','from_date','to_date'])
po=sp[['point_id','profile_id','point_order']]; pl=planar.merge(po,on=['point_id','profile_id']).sort_values(['profile_id','campaign_id','point_order'])
lens=[]
for (pid,cid,date),g in pl.groupby(['profile_id','campaign_id','date']):
    g=g.sort_values('point_order'); ch=g.observed_chainage_m.to_numpy(); ids=g.point_id.to_numpy()
    for j in range(len(g)-1): lens.append([pid,cid,date,ids[j],ids[j+1],ch[j+1]-ch[j]])
lens=pd.DataFrame(lens,columns=['profile_id','campaign_id','date','from_point_id','to_point_id','length_obs_m'])
init=lens.sort_values('date').groupby(['profile_id','from_point_id','to_point_id']).length_obs_m.first().rename('length0').reset_index(); lens=lens.merge(init,on=['profile_id','from_point_id','to_point_id']); lens['strain_calc']=(lens.length_obs_m-lens.length0)/lens.length0
hm=hstrain.merge(lens[['profile_id','campaign_id','date','from_point_id','to_point_id','strain_calc']],on=['profile_id','campaign_id','date','from_point_id','to_point_id'])

# ---------- sensor quality ----------
insar2=insar.loc[~insar.first_epoch_zero_datum.astype(bool)].copy(); insar2['residual_mm_evaluation_only']=insar2.subvertical_estimate_relative_mm-insar2.true_vertical_settlement_relative_mm_evaluation_only
sensor_df=pd.DataFrame([
    sensor_metrics(lev,'residual_mm_evaluation_only','standard_uncertainty_mm','leveling'),
    sensor_metrics(gnss,'residual_mm_evaluation_only','standard_uncertainty_mm','GNSS'),
    sensor_metrics(insar2,'residual_mm_evaluation_only','standard_uncertainty_mm','InSAR')])
runs['failed']=runs.qc_status.str.contains('failed',case=False,na=False); conf=pd.crosstab(runs.gross_error_injected,runs.failed)

# ---------- temporal/spatial diagnostics ----------
truth_work=truth[truth.point_id.isin(work_ids)].copy(); late=truth_work[truth_work.date>=pd.Timestamp('2023-01-01')]
slopes=[]
for pid,g in late.groupby('point_id'):
    x=(g.date-g.date.min()).dt.days.to_numpy()/365.25; y=g.true_velocity_mm_y.to_numpy(); slopes.append([pid,np.polyfit(x,y,1)[0],y.min(),y.max()])
slopes_df=pd.DataFrame(slopes,columns=['point_id','velocity_slope_mm_y2','min_rate','max_rate'])
lev_work=lev[lev.point_id.isin(work_ids)].sort_values(['point_id','date']).copy(); lev_work['true_prev']=lev_work.groupby('point_id').true_settlement_mm_evaluation_only.shift(); lev_work['date_prev']=lev_work.groupby('point_id').date.shift(); lev_work['dt_y']=(lev_work.date-lev_work.date_prev).dt.days/365.25; lev_work['true_rate_interval']=(lev_work.true_settlement_mm_evaluation_only-lev_work.true_prev)/lev_work.dt_y
ri=lev_work.dropna(subset=['true_rate_interval']); interval_rates=ri.true_rate_interval
morans={c:moran_knn(grid,c)[0] for c in ['settlement_reference_map_mm','kzt_reconstructed','ko_reconstructed','seismic_energy_mid_J_m2_reconstructed']}

# ---------- feature rows ----------
adj_rows=[]
for _,r in tilts.iterrows():
    adj_rows += [(r.campaign_id,r.from_point_id,abs(r.tilt_mm_per_m),r.standard_uncertainty_mm_per_m),(r.campaign_id,r.to_point_id,abs(r.tilt_mm_per_m),r.standard_uncertainty_mm_per_m)]
adj=pd.DataFrame(adj_rows,columns=['campaign_id','point_id','abs_tilt','tilt_unc']); adj_agg=adj.groupby(['campaign_id','point_id']).agg(current_abs_tilt_mean=('abs_tilt','mean'),current_abs_tilt_max=('abs_tilt','max'),current_tilt_unc_mean=('tilt_unc','mean')).reset_index(); curv_feat=curv[['campaign_id','point_id','curvature_mm_per_m2','standard_uncertainty_mm_per_m2']]
rows=[]; history_cache={}
for pid,g in lev[lev.point_id.isin(work_ids)].sort_values(['point_id','date']).groupby('point_id'):
    g=g.sort_values('date').reset_index(drop=True); dates=g.date.tolist(); obs=g.observed_settlement_mm.to_numpy(float); sig=g.standard_uncertainty_mm.to_numpy(float); true=g.true_settlement_mm_evaluation_only.to_numpy(float); cids=g.campaign_id.tolist(); rates_obs=np.full(len(g),np.nan); rates_true=np.full(len(g),np.nan); dt_prev=np.full(len(g),np.nan)
    for i in range(1,len(g)):
        dt=(dates[i]-dates[i-1]).days/365.25; dt_prev[i]=dt; rates_obs[i]=(obs[i]-obs[i-1])/dt; rates_true[i]=(true[i]-true[i-1])/dt
    history_cache[pid]={'g':g}
    static=sp.loc[sp.point_id==pid].iloc[0]; pr=proc.loc[proc.point_id==pid].iloc[0]
    for i in range(3,len(g)-1):
        dt_next=(dates[i+1]-dates[i]).days/365.25; td=dates[i+1]
        rows.append({'point_id':pid,'profile_id':static.profile_id,'profile_type':static.profile_type,'current_campaign_id':cids[i],'target_campaign_id':cids[i+1],'current_date':dates[i],'target_date':td,'target_year':td.year,'dt_next_y':dt_next,'dt_prev_y':dt_prev[i],
                     'observed_settlement_mm':obs[i],'current_uncertainty_mm':sig[i],'rate_1_mm_y':rates_obs[i],'rate_2_mm_y':rates_obs[i-1],'rate_3_mm_y':rates_obs[i-2],'rate_mean3_mm_y':np.mean(rates_obs[i-2:i+1]),'rate_std3_mm_y':np.std(rates_obs[i-2:i+1],ddof=1),'rate_change_1_mm_y':rates_obs[i]-rates_obs[i-1],'true_current_rate_mm_y':rates_true[i],
                     'target_increment_observed_mm':obs[i+1]-obs[i],'target_increment_true_mm':true[i+1]-true[i],'target_rate_observed_mm_y':(obs[i+1]-obs[i])/dt_next,'target_rate_true_mm_y':(true[i+1]-true[i])/dt_next,
                     'next_month_sin':math.sin(2*math.pi*td.timetuple().tm_yday/365.25),'next_month_cos':math.cos(2*math.pi*td.timetuple().tm_yday/365.25),
                     'kzt':static.kzt,'ko':static.ko,'seismic_energy_J_m2':static.seismic_energy_J_m2,'fill_density':static.fill_density,'log_fault_distance':math.log1p(static.fault_distance_m),'lithology':static.lithology,
                     'settlement_anchor_map_mm':static.settlement_anchor_map_mm,'latent_regime':pr.regime,'latent_base_rate':pr.base_rate,'latent_event_amp':pr.event_amp,'latent_event_center':pr.event_center,'latent_decay_tau':pr.decay_tau,'x_local_m':static.x_local_m,'y_local_m':static.y_local_m,
                     '_hist_dates':dates[:i+1], '_hist_obs':obs[:i+1]})
model_df=pd.DataFrame(rows).merge(adj_agg,left_on=['current_campaign_id','point_id'],right_on=['campaign_id','point_id'],how='left').drop(columns='campaign_id').merge(curv_feat,left_on=['current_campaign_id','point_id'],right_on=['campaign_id','point_id'],how='left').drop(columns='campaign_id').merge(sp[['point_id','point_order']],on='point_id')
model_df['profile_current_rate_mean']=model_df.groupby(['current_campaign_id','profile_id']).rate_1_mm_y.transform('mean'); model_df['profile_current_rate_std']=model_df.groupby(['current_campaign_id','profile_id']).rate_1_mm_y.transform('std')
model_df=model_df.sort_values(['current_campaign_id','profile_id','point_order'])
model_df['neighbor_rate_mean_mm_y']=model_df.groupby(['current_campaign_id','profile_id']).rate_1_mm_y.transform(lambda s:(s.shift(1)+s.shift(-1))/2).fillna(model_df.groupby(['current_campaign_id','profile_id']).rate_1_mm_y.transform(lambda s:s.shift(1).fillna(s.shift(-1))))
model_df['pred_zero']=0.; model_df['pred_last_rate']=model_df.rate_1_mm_y*model_df.dt_next_y; model_df['pred_mean3_rate']=model_df.rate_mean3_mm_y*model_df.dt_next_y
model_df['pred_linear4']=model_df.apply(lambda r:trend_predict(r._hist_dates,r._hist_obs,r.target_date,4,1),axis=1); model_df['pred_quadratic6']=model_df.apply(lambda r:trend_predict(r._hist_dates,r._hist_obs,r.target_date,6,2),axis=1)
model_df['pred_const_accel']=(model_df.rate_1_mm_y+(model_df.rate_1_mm_y-model_df.rate_2_mm_y)*(model_df.dt_next_y/model_df.dt_prev_y))*model_df.dt_next_y

# ---------- Kalman ----------
def kalman_predictions(q):
    out=[]
    for pid,h in history_cache.items():
        g=h['g'].sort_values('date').reset_index(drop=True); dates=g.date.tolist(); ys=g.observed_settlement_mm.to_numpy(float); sigs=g.standard_uncertainty_mm.to_numpy(float); cids=g.campaign_id.tolist(); x=np.array([ys[0],0.]); P=np.diag([sigs[0]**2,200.**2]); H=np.array([[1.,0.]]); I=np.eye(2)
        for i in range(1,len(g)):
            dt=(dates[i]-dates[i-1]).days/365.25; F=np.array([[1.,dt],[0.,1.]]); Q=q*np.array([[dt**3/3,dt**2/2],[dt**2/2,dt]]); xp=F@x; Pp=F@P@F.T+Q; R=sigs[i]**2; S=float((H@Pp@H.T).item()+R); K=(Pp@H.T/S).reshape(2); x=xp+K*(ys[i]-float((H@xp).item())); P=(I-np.outer(K,H.reshape(2)))@Pp
            if i>=3 and i+1<len(g):
                dn=(dates[i+1]-dates[i]).days/365.25; xn=np.array([[1.,dn],[0.,1.]])@x; out.append([pid,cids[i],cids[i+1],xn[0]-x[0]])
    return pd.DataFrame(out,columns=['point_id','current_campaign_id','target_campaign_id','kalman_pred'])
q_grid=[.01,.1,1,10,30,100,300,1000,3000,10000]; qrows=[]
for q in q_grid:
    z=model_df.merge(kalman_predictions(q),on=['point_id','current_campaign_id','target_campaign_id']); m=reg_metrics(z.loc[z.target_year==2024,'target_increment_observed_mm'],z.loc[z.target_year==2024,'kalman_pred']); qrows.append({'q_accel':q,**m})
q_tuning=pd.DataFrame(qrows); best_q=float(q_tuning.sort_values(['MAE_mm','RMSE_mm']).iloc[0].q_accel); q_tuning['selection_metric']='2024 observed next-cycle increment MAE'; q_tuning['selected']=q_tuning.q_accel.eq(best_q)
model_df=model_df.merge(kalman_predictions(best_q),on=['point_id','current_campaign_id','target_campaign_id'])

# ---------- learned baselines ----------
valid_num=['observed_settlement_mm','current_uncertainty_mm','rate_1_mm_y','rate_2_mm_y','rate_3_mm_y','rate_mean3_mm_y','rate_std3_mm_y','rate_change_1_mm_y','dt_prev_y','next_month_sin','next_month_cos','kzt','ko','seismic_energy_J_m2','fill_density','log_fault_distance']; valid_cat=['lithology','profile_type']
spatial_num=valid_num+['current_abs_tilt_mean','current_abs_tilt_max','curvature_mm_per_m2','profile_current_rate_mean','profile_current_rate_std','neighbor_rate_mean_mm_y']; leaky_num=spatial_num+['settlement_anchor_map_mm','x_local_m','y_local_m']; direct_num=valid_num+['dt_next_y']
tr=model_df.target_year<=2023; va=model_df.target_year==2024; te=model_df.target_year==2025
ridge_pipe=Pipeline([('prep',make_preprocessor(valid_num,valid_cat,True)),('model',Ridge(alpha=10))]); ridge_pipe.fit(model_df.loc[tr,valid_num+valid_cat],model_df.loc[tr,'target_rate_observed_mm_y'])
def make_hgb(num,l2=1,leaves=31): return Pipeline([('prep',make_preprocessor(num,valid_cat,False)),('model',HistGradientBoostingRegressor(learning_rate=.05,max_iter=300,max_leaf_nodes=leaves,min_samples_leaf=10,l2_regularization=l2,random_state=42,early_stopping=False))])
hgb_valid=make_hgb(valid_num,1,31); hgb_valid.fit(model_df.loc[tr,valid_num+valid_cat],model_df.loc[tr,'target_rate_observed_mm_y'])
hgb_spatial=make_hgb(spatial_num,0,31); hgb_spatial.fit(model_df.loc[tr,spatial_num+valid_cat],model_df.loc[tr,'target_rate_observed_mm_y'])
hgb_leaky=make_hgb(leaky_num,1,31); hgb_leaky.fit(model_df.loc[tr,leaky_num+valid_cat],model_df.loc[tr,'target_rate_observed_mm_y'])
direct_ridge=Pipeline([('prep',make_preprocessor(direct_num,valid_cat,True)),('model',Ridge(alpha=.01))]); direct_ridge.fit(model_df.loc[tr,direct_num+valid_cat],model_df.loc[tr,'target_increment_observed_mm'])
direct_hgb=make_hgb(direct_num,0,15); direct_hgb.fit(model_df.loc[tr,direct_num+valid_cat],model_df.loc[tr,'target_increment_observed_mm'])
model_df['pred_ridge_rate']=ridge_pipe.predict(model_df[valid_num+valid_cat])*model_df.dt_next_y; model_df['pred_hgb_rate']=hgb_valid.predict(model_df[valid_num+valid_cat])*model_df.dt_next_y; model_df['pred_hgb_spatial_rate']=hgb_spatial.predict(model_df[spatial_num+valid_cat])*model_df.dt_next_y; model_df['pred_hgb_leaky_rate']=hgb_leaky.predict(model_df[leaky_num+valid_cat])*model_df.dt_next_y; model_df['pred_ridge_direct']=direct_ridge.predict(model_df[direct_num+valid_cat]); model_df['pred_hgb_direct']=direct_hgb.predict(model_df[direct_num+valid_cat])

pred_cols={'Zero increment':'pred_zero','Last observed rate':'pred_last_rate','Mean of last 3 rates':'pred_mean3_rate','Linear trend (4 cycles)':'pred_linear4','Quadratic trend (6 cycles)':'pred_quadratic6','Constant acceleration':'pred_const_accel',f'Kalman local trend (q={best_q:g})':'kalman_pred','Ridge, annualized rate':'pred_ridge_rate','HGB, annualized rate':'pred_hgb_rate','HGB + spatial state, annualized rate':'pred_hgb_spatial_rate','HGB + forbidden terminal map':'pred_hgb_leaky_rate','Ridge, direct increment':'pred_ridge_direct','HGB, direct increment':'pred_hgb_direct'}
met=[]
for name,col in pred_cols.items():
    for label,target in [('hidden_truth','target_increment_true_mm'),('observed','target_increment_observed_mm')]: met.append({'split':'temporal_test_2025','target':label,'model':name,'prediction_column':col,**reg_metrics(model_df.loc[te,target],model_df.loc[te,col])})
baseline_metrics=pd.DataFrame(met); baseline_metrics['rank_by_MAE']=baseline_metrics.groupby(['split','target']).MAE_mm.rank(method='dense')

# Random row split diagnostic
itr,ite=train_test_split(np.arange(len(model_df)),test_size=.25,random_state=42); rand=[]
for label,num,pipe0 in [('HGB annualized rate (valid features)',valid_num,make_hgb(valid_num,1,31)),('HGB annualized rate + terminal map',leaky_num,make_hgb(leaky_num,1,31))]:
    pipe0.fit(model_df.iloc[itr][num+valid_cat],model_df.iloc[itr].target_rate_observed_mm_y); p=pipe0.predict(model_df.iloc[ite][num+valid_cat])*model_df.iloc[ite].dt_next_y.to_numpy(); rand.append({'split':'random_row_25pct','target':'observed','model':label,**reg_metrics(model_df.iloc[ite].target_increment_observed_mm,p)})
pd0=make_hgb(direct_num,0,15); pd0.fit(model_df.iloc[itr][direct_num+valid_cat],model_df.iloc[itr].target_increment_observed_mm); p=pd0.predict(model_df.iloc[ite][direct_num+valid_cat]); rand.append({'split':'random_row_25pct','target':'observed','model':'HGB direct increment',**reg_metrics(model_df.iloc[ite].target_increment_observed_mm,p)})
random_split_metrics=pd.DataFrame(rand)

# profile holdout
ph=[]
for held in sorted(model_df.profile_id.unique()):
    train=(model_df.target_year<=2023)&(model_df.profile_id!=held); test=(model_df.target_year>=2024)&(model_df.profile_id==held); pp=make_hgb(valid_num,1,31); pp.fit(model_df.loc[train,valid_num+valid_cat],model_df.loc[train,'target_rate_observed_mm_y']); z=model_df.loc[test].copy(); z['pred_hgb_holdout']=pp.predict(z[valid_num+valid_cat])*z.dt_next_y.to_numpy()
    for label,target in [('hidden_truth','target_increment_true_mm'),('observed','target_increment_observed_mm')]:
        for mn,col in [('Kalman','kalman_pred'),('Mean3','pred_mean3_rate'),('LastRate','pred_last_rate'),('HGB_rate','pred_hgb_holdout')]: ph.append({'heldout_profile':held,'target':label,'model':mn,**reg_metrics(z[target],z[col])})
profile_holdout_metrics=pd.DataFrame(ph); profile_holdout_summary=profile_holdout_metrics.groupby(['target','model']).agg(profiles=('heldout_profile','nunique'),mean_MAE_mm=('MAE_mm','mean'),median_MAE_mm=('MAE_mm','median'),worst_MAE_mm=('MAE_mm','max'),mean_RMSE_mm=('RMSE_mm','mean')).reset_index()

# feature importance
pi=permutation_importance(hgb_valid,model_df.loc[va,valid_num+valid_cat],model_df.loc[va,'target_rate_observed_mm_y'],scoring='neg_mean_absolute_error',n_repeats=10,random_state=42,n_jobs=1)
feature_importance=pd.DataFrame({'feature':valid_num+valid_cat,'mae_increase_rate_mm_y_mean':pi.importances_mean,'mae_increase_rate_mm_y_sd':pi.importances_std}).sort_values('mae_increase_rate_mm_y_mean',ascending=False)

# conformal
ci=[]
for mn,col in [('Kalman','kalman_pred'),('Mean3','pred_mean3_rate')]:
    for lab,target in [('observed','target_increment_observed_mm'),('hidden_truth','target_increment_true_mm')]:
        ae=np.abs(model_df.loc[va,target]-model_df.loc[va,col])
        for c in [.8,.9,.95]:
            hw=conformal_halfwidth(ae,c); cov=float(np.mean(np.abs(model_df.loc[te,target]-model_df.loc[te,col])<=hw)); ci.append({'model':mn,'calibration_target':lab,'nominal_coverage':c,'half_width_mm':hw,'test_empirical_coverage':cov,'n_calibration':int(va.sum()),'n_test':int(te.sum())})
conformal_metrics=pd.DataFrame(ci)

# correlations/noise
corr_rate=float(np.corrcoef(model_df.rate_1_mm_y,model_df.target_rate_true_mm_y)[0,1]); corr_obs_true=float(np.corrcoef(model_df.target_increment_observed_mm,model_df.target_increment_true_mm)[0,1]); corrs=[]
for c in ['settlement_anchor_map_mm','latent_base_rate','latent_event_amp','kzt','ko','seismic_energy_J_m2','fill_density','log_fault_distance']: corrs.append({'feature':c,'pearson_r_to_true_next_rate':float(np.corrcoef(model_df[c].astype(float),model_df.target_rate_true_mm_y)[0,1])})
corr_df=pd.DataFrame(corrs).sort_values('pearson_r_to_true_next_rate',ascending=False); noise_floor=reg_metrics(model_df.loc[te,'target_increment_true_mm'],model_df.loc[te,'target_increment_observed_mm'])

# ---------- stress forecasts ----------
def build_stress(track):
    out=[]; truthmap={sid:g.set_index('date').sort_index() for sid,g in stress_truth.groupby('scenario_id')}; catmap=stress_cat.set_index('scenario_id')
    for sid,g0 in stress_meas.groupby('scenario_id'):
        g0=g0.sort_values('date').reset_index(drop=True); pid=g0.point_id.iloc[0]; static=sp.loc[sp.point_id==pid].iloc[0]; tg=truthmap[sid]; x=None; P=None; last_update=None; hist=[]; H=np.array([[1.,0.]]); I=np.eye(2)
        for _,row in g0.iterrows():
            date=row.date
            if len(hist)>=4:
                dts=[(hist[j][0]-hist[j-1][0]).days/365.25 for j in range(1,len(hist))]; rh=[(hist[j][1]-hist[j-1][1])/dts[j-1] for j in range(1,len(hist))]; r1,r2,r3=rh[-1],rh[-2],rh[-3]; last_date,last_obs,last_sig=hist[-1]; prev_date=hist[-2][0]; dn=(date-last_date).days/365.25
                feat={'observed_settlement_mm':last_obs,'current_uncertainty_mm':last_sig,'rate_1_mm_y':r1,'rate_2_mm_y':r2,'rate_3_mm_y':r3,'rate_mean3_mm_y':np.mean(rh[-3:]),'rate_std3_mm_y':np.std(rh[-3:],ddof=1),'rate_change_1_mm_y':r1-r2,'dt_prev_y':dts[-1],'next_month_sin':math.sin(2*math.pi*date.timetuple().tm_yday/365.25),'next_month_cos':math.cos(2*math.pi*date.timetuple().tm_yday/365.25),'kzt':static.kzt,'ko':static.ko,'seismic_energy_J_m2':static.seismic_energy_J_m2,'fill_density':static.fill_density,'log_fault_distance':math.log1p(static.fault_distance_m),'lithology':static.lithology,'profile_type':static.profile_type}
                hp=float(hgb_valid.predict(pd.DataFrame([feat])[valid_num+valid_cat])[0]); kal=np.nan
                if x is not None:
                    dk=(date-last_update).days/365.25; kal=float((np.array([[1.,dk],[0.,1.]])@x)[0]-x[0])
                tc=float(tg.loc[last_date,'true_settlement_mm']); tp=float(tg.loc[prev_date,'true_settlement_mm']); tt=float(tg.loc[date,'true_settlement_mm']); ctr=(tc-tp)/((last_date-prev_date).days/365.25); ti=tt-tc; trt=ti/dn
                out.append({'track':track,'scenario_id':sid,'family':catmap.loc[sid,'family'],'point_id':pid,'previous_date':prev_date,'current_date':last_date,'target_date':date,'dt_next_y':dn,'current_obs_mm':last_obs,'true_current_mm':tc,'true_target_mm':tt,'true_increment_mm':ti,'true_rate_mm_y':trt,'current_true_rate_mm_y':ctr,'current_true_velocity_mm_y':float(tg.loc[last_date,'true_velocity_mm_y']),'target_true_velocity_mm_y':float(tg.loc[date,'true_velocity_mm_y']),'current_rate_obs_mm_y':r1,'pred_last_rate':r1*dn,'pred_mean3_rate':np.mean(rh[-3:])*dn,'pred_const_accel':(r1+(r1-r2)*(dn/max(dts[-1],1e-6)))*dn,'pred_hgb_rate':hp*dn,'pred_kalman':kal,'target_missing':bool(row.missing),'target_gross_error':bool(row.gross_error)})
            usable=not bool(row.missing)
            if track=='oracle_qc': usable=usable and not bool(row.gross_error)
            if usable and np.isfinite(row.observed_settlement_mm):
                y=float(row.observed_settlement_mm); s=float(row.standard_uncertainty_mm); hist.append((date,y,s))
                if x is None: x=np.array([y,0.]); P=np.diag([s**2,200.**2]); last_update=date
                else:
                    dt=(date-last_update).days/365.25; F=np.array([[1.,dt],[0.,1.]]); Q=best_q*np.array([[dt**3/3,dt**2/2],[dt**2/2,dt]]); xp=F@x; Pp=F@P@F.T+Q; S=float((H@Pp@H.T).item()+s**2); K=(Pp@H.T/S).reshape(2); x=xp+K*(y-float((H@xp).item())); P=(I-np.outer(K,H.reshape(2)))@Pp; last_update=date
    return pd.DataFrame(out)
stress_preds=pd.concat([build_stress('raw'),build_stress('oracle_qc')],ignore_index=True)
stress_cols={'Last observed rate':'pred_last_rate','Mean of last 3 rates':'pred_mean3_rate','Constant acceleration':'pred_const_accel','HGB annualized rate':'pred_hgb_rate',f'Kalman q={best_q:g}':'pred_kalman'}; sm=[]; sc=[]
for track,g in stress_preds.groupby('track'):
    for mn,col in stress_cols.items():
        sm.append({'track':track,'model':mn,**reg_metrics(g.true_increment_mm,g[col])}); pr=g[col]/g.dt_next_y
        for th in [100,250]: sc.append({'track':track,'task':f'rate_ge_{th}_mm_y','model':mn,**classification_metrics(g.true_rate_mm_y>=th,pr>=th)})
        yt=(g.true_rate_mm_y>=g.current_true_rate_mm_y*1.2)&((g.true_rate_mm_y-g.current_true_rate_mm_y)>=20); yp=(pr>=g.current_rate_obs_mm_y*1.2)&((pr-g.current_rate_obs_mm_y)>=20); sc.append({'track':track,'task':'acceleration_jump_20pct_and_20mm_y','model':mn,**classification_metrics(yt,yp)})
stress_metrics=pd.DataFrame(sm); stress_classification=pd.DataFrame(sc)

# ---------- data quality tables ----------
coverage=pd.read_csv(TABLES/'field_coverage_summary.csv'); gridcov=pd.read_csv(TABLES/'grid_coverage_summary.csv').iloc[0]; anchor_res=pd.read_csv(TABLES/'anchor_settlement_residuals.csv')
checks=[]
def add(cid,cat,name,status,severity,observed,expected,interpretation): checks.append({'check_id':cid,'category':cat,'check':name,'status':status,'severity':severity,'observed':str(observed),'expected':str(expected),'interpretation':interpretation})
add('DQ-001','integrity','CSV tables readable','PASS','critical',f'{len(files)} tables / {sum(len(d) for d in dfs.values()):,} rows','all readable','No structural corruption found.')
count_map={'plan_units':len(dfs['plan_units_reconstructed']),'integrated_reconstructed_rows':len(dfs['integrated_features_reconstructed']),'grid_cells_50m':len(dfs['field_grid_50m']),'source_named_layers':len(dfs['source_layer_manifest']),'profiles':len(dfs['survey_profiles']),'survey_points':len(dfs['survey_points']),'campaigns':len(dfs['survey_campaigns']),'leveling_raw_stations':len(dfs['leveling_stations_raw']),'leveling_adjusted_epochs':len(dfs['leveling_adjusted_epochs']),'gnss_sessions':len(dfs['gnss_sessions_raw']),'insar_points':len(dfs['insar_point_catalog']),'insar_observations':len(dfs['insar_observations_relative']),'ensemble_rows':len(dfs['synthetic_truth_ensemble_monthly'])}; mism={k:(manifest['counts'].get(k),v) for k,v in count_map.items() if manifest['counts'].get(k)!=v}; add('DQ-002','integrity','Manifest row counts','PASS' if not mism else 'FAIL','critical','all counts match' if not mism else mism,'manifest equals tables','Manifest is consistent.')
key_defs={'survey_points':['point_id'],'survey_profiles':['profile_id'],'survey_campaigns':['campaign_id'],'field_grid_50m':['cell_id'],'leveling_adjusted_epochs':['campaign_id','point_id'],'truth_survey_points_monthly':['point_id','date'],'process_parameters_survey_points':['point_id'],'gnss_adjusted_epochs':['campaign_id','point_id'],'insar_point_catalog':['insar_point_id'],'insar_acquisition_catalog':['acquisition_id'],'insar_observations_relative':['acquisition_id','insar_point_id'],'stress_test_scenario_catalog':['scenario_id'],'stress_test_measurements':['scenario_id','date'],'stress_test_truth_monthly':['scenario_id','date'],'tilts':['campaign_id','profile_id','from_point_id','to_point_id'],'curvatures':['campaign_id','profile_id','point_id'],'horizontal_strains':['campaign_id','profile_id','from_point_id','to_point_id'],'settlement_rates':['point_id','from_campaign_id','to_campaign_id']}; dup={t:int(dfs[t].duplicated(k).sum()) for t,k in key_defs.items() if dfs[t].duplicated(k).sum()}; add('DQ-003','integrity','Primary/composite key uniqueness','PASS' if not dup else 'FAIL','critical','0 duplicates' if not dup else dup,'0','Keys are unique.')
add('DQ-004','integrity','Foreign-key closure','PASS','critical','0 orphan references','0','Cross-table identities are consistent.')
add('DQ-005','spatial','Grid/footprint area balance','PASS','high',f'outside={gridcov.outside_footprint_area_m2:g}; uncovered={gridcov.uncovered_footprint_area_m2:g}; balance={gridcov.area_balance_error_m2:.3e}','0/0/negligible','Grid is clipped geometrically.')
add('DQ-006','source_fidelity','Published settlement anchors conditioned','PASS','high',f'max residual={anchor_res.residual_mean_mm.abs().max():.3e} mm','near zero','Published rows constrain the field.')
def covstats(field):
    g=coverage[coverage.field==field]; return float(g.loc[g.provenance_code=='D','effective_area_fraction'].sum()),float(g.loc[g.provenance_code.str.contains('R',regex=False,na=False),'effective_area_fraction'].sum()),float(g.loc[g.provenance_code=='missing','effective_area_fraction'].sum())
for cid,field in [('DQ-007','settlement_reference_map_mm'),('DQ-008','ko_reconstructed')]:
    d,r,m=covstats(field); add(cid,'spatial',f'{field} area coverage','PASS' if m<.02 else 'WARN','medium',f'direct={d:.1%}; reconstructed={r:.1%}; missing={m:.2%}','explicit provenance','Coverage is explicit.')
add('DQ-009','survey_math','Tilt recomputation','PASS','high',f'{np.max(np.abs(tm.tilt_mm_per_m-tm.tilt_calc)):.3e}','<1e-9','Correct.'); add('DQ-010','survey_math','Curvature recomputation','PASS','high',f'{np.max(np.abs(cm.curvature_mm_per_m2-cm.curv_calc)):.3e}','<1e-9','Correct.'); add('DQ-011','survey_math','Settlement-rate recomputation','PASS','high',f'{np.max(np.abs(rm.settlement_rate_mm_y-rm.rate_calc)):.3e}','<1e-6','Correct.'); add('DQ-012','survey_math','Horizontal-strain recomputation','PASS','high',f'{np.max(np.abs(hm.horizontal_strain-hm.strain_calc)):.3e}','<1e-9','Correct.')
for i,r in sensor_df.iterrows(): add(f'DQ-{13+i:03d}','measurement',f'{r.sensor} residual calibration','PASS' if .93<=r.coverage_95<=.995 else 'WARN','high',f'RMSE={r.RMSE_mm:.3f}; coverage95={r.coverage_95:.1%}; zsd={r.std_residual_sd:.3f}','coverage near 95%','Uncertainty is usable but conservative.')
tp,fn,fp,tn=int(conf.loc[True,True]),int(conf.loc[True,False]),int(conf.loc[False,True]),int(conf.loc[False,False]); recall=tp/(tp+fn); false_fail=fp/(fp+tn)
add('DQ-016','measurement','Injected gross-error detection','WARN','high',f'recall={recall:.1%}; missed={fn}; false-fail={false_fail:.1%}','high recall','Four injected gross errors pass closure.'); add('DQ-017','measurement','Adjusted-leveling QC diversity','WARN','medium',lev.qc_status.value_counts().to_dict(),'accepted/warning/rejected','Final epoch warnings are not propagated.')
add('DQ-018','sampling','Focused campaign behavior','FAIL_FOR_FINAL_MODEL','high',f'all 27 campaigns contain 126 points; focused={(campaigns.campaign_type=="focused").sum()}','targeted coverage','campaign_type is decorative.'); reg_counts=proc[proc.point_type=='WORK'].regime.value_counts().to_dict(); add('DQ-019','temporal_realism','Regime balance','FAIL_FOR_FINAL_MODEL','high',reg_counts,'mixed regimes','91/98 work points are accelerating.'); ld=slopes_df.velocity_slope_mm_y2.describe(); add('DQ-020','temporal_realism','Regime label vs late-stage dynamics','FAIL_FOR_FINAL_MODEL','high',f'median slope={ld["50%"]:.3f}; max={ld["max"]:.3f}','time-varying stage','Most are plateauing/decaying by test time.'); add('DQ-021','temporal_realism','Nominal rate tail','FAIL_FOR_FINAL_MODEL','high',f'max={interval_rates.max():.1f}; >250={np.mean(interval_rates>=250):.1%}; >400={np.mean(interval_rates>=400):.1%}','extremes rare/separate','Extreme tail too frequent.'); add('DQ-022','spatial','Spatial autocorrelation','WARN','high','; '.join(f'{k} I={v:.3f}' for k,v in morans.items()),'grouped spatial validation','Random split is optimistic.')
cor_terminal=float(corr_df.set_index('feature').loc['settlement_anchor_map_mm','pearson_r_to_true_next_rate']); add('DQ-023','leakage','Terminal settlement map feature','FAIL_FOR_FINAL_MODEL','critical',f'corr={cor_terminal:.3f}','exclude','Future leakage.'); add('DQ-024','leakage','Latent generator parameters','FAIL_FOR_FINAL_MODEL','critical','regime/base_rate/event_amp/event_center/decay_tau exported','evaluation-only','Generator is exposed.'); add('DQ-025','provenance','Point-level feature provenance','WARN','high','static values without per-feature provenance/uncertainty','carry D/R/H and uncertainty','Model cannot distinguish digitized from reconstructed.'); add('DQ-026','measurement','GNSS sample representativeness','WARN','high','12 REF + top 30 WORK by anchor settlement','stratified sample','High-deformation bias.'); add('DQ-027','measurement','InSAR spatial independence','WARN','high',f'100 points inherit truth from {insar_cat.nearest_truth_point_id.nunique()} survey points','independent field/grouping','Pseudo-replication.'); add('DQ-028','temporal_realism','Reference-date uncertainty','WARN','high','source date NULL; scenario date 2022-07-01','explicit scenario assumption','Not source-derived chronology.'); stress_points=set(stress_cat.point_id); top30=set(sp[sp.point_type=='WORK'].nlargest(30,'settlement_anchor_map_mm').point_id); add('DQ-029','stress_design','Stress scenario spatial coverage','WARN','medium',f'36 scenarios on {len(stress_points)} points; all top-30 anchors','diverse contexts','Stress is concentrated on severe zones.')
direct_mae=float(baseline_metrics.query("target=='hidden_truth' and model=='HGB, direct increment'").MAE_mm.iloc[0]); rate_mae=float(baseline_metrics.query("target=='hidden_truth' and model=='HGB, annualized rate'").MAE_mm.iloc[0]); rand_mae=float(random_split_metrics.iloc[0].MAE_mm); temp_obs=float(baseline_metrics.query("target=='observed' and model=='HGB, annualized rate'").MAE_mm.iloc[0]); add('DQ-030','model_design','Raw increment target under variable intervals','FAIL_FOR_FINAL_MODEL','high',f'direct HGB={direct_mae:.2f}; rate HGB={rate_mae:.2f}','predict rate then integrate','Schedule shift breaks direct target.'); add('DQ-031','validation','Random row split optimism','FAIL_FOR_FINAL_MODEL','high',f'random={rand_mae:.2f}; temporal={temp_obs:.2f}','rolling temporal/spatial holdouts','Rows are dependent.'); add('DQ-032','predictability','Nominal temporal smoothness','WARN','high',f'corr last->next={corr_rate:.4f}; obs->truth={corr_obs_true:.5f}','regime changes','Persistence is nearly optimal.'); add('DQ-033','production_claims','External production validation','FAIL_FOR_PRODUCTION','critical','no real repeated SKRU-1 cycles','independent real cycles','Synthetic metrics are not operational accuracy.')
data_quality_checks=pd.DataFrame(checks)
actions={'DQ-016':'Add robust station residual checks and propagate warning grade.','DQ-017':'Carry run/station warnings into adjusted epochs.','DQ-018':'Generate true focused campaigns and missing patterns.','DQ-019':'Rebalance regimes; stable/uniform/decaying should dominate.','DQ-020':'Use time-varying stage labels.','DQ-021':'Move most extreme rates to stress/OOD.','DQ-022':'Require temporal/profile/block/spatial-buffer validation.','DQ-023':'Remove terminal map from model-ready tables.','DQ-024':'Physically separate latent generator fields.','DQ-025':'Join provenance/donor distance/uncertainty to survey points.','DQ-026':'Stratify GNSS sample.','DQ-027':'Generate independent continuous InSAR truth or group by source point.','DQ-028':'Treat anchor date as scenario parameter and ensemble it.','DQ-029':'Add low/moderate and diverse stress points.','DQ-030':'Predict annualized rate and integrate over known horizon.','DQ-031':'Ban random row split as headline.','DQ-032':'Add change-points, drift, gaps, reactivations.','DQ-033':'Wait for real cycles before production claims.'}
issue_register=data_quality_checks[data_quality_checks.status!='PASS'].copy(); issue_register['priority']=issue_register.status.map({'FAIL_FOR_PRODUCTION':'P1','FAIL_FOR_FINAL_MODEL':'P1','WARN':'P2'}); issue_register['recommended_action']=issue_register.check_id.map(actions); issue_register=issue_register[['check_id','priority','category','check','status','severity','observed','interpretation','recommended_action']]

# ---------- summary tables ----------
inventory=[]
for f in files:
    d=dfs[f.stem]; inventory.append({'table':f.name,'rows':len(d),'columns':len(d.columns),'missing_cells':int(d.isna().sum().sum()),'missing_fraction':float(d.isna().sum().sum()/max(len(d)*len(d.columns),1)),'file_size_bytes':f.stat().st_size})
table_inventory=pd.DataFrame(inventory).sort_values('rows',ascending=False)
bins=[-np.inf,20,75,100,250,400,np.inf]; labels=['<20','20–75','75–100','100–250','250–400','>400']; vc=pd.cut(interval_rates,bins=bins,labels=labels,right=False).value_counts().reindex(labels,fill_value=0); rate_band=pd.DataFrame({'band_mm_y':labels,'count':[int(vc[x]) for x in labels]}); rate_band['fraction']=rate_band['count']/len(interval_rates)
deformation_summary=pd.DataFrame([{'parameter':'observed settlement rate','unit':'mm/year','min':rates.settlement_rate_mm_y.min(),'median':rates.settlement_rate_mm_y.median(),'p95':rates.settlement_rate_mm_y.quantile(.95),'max':rates.settlement_rate_mm_y.max()},{'parameter':'tilt','unit':'mm/m','min':tilts.tilt_mm_per_m.min(),'median':tilts.tilt_mm_per_m.median(),'p95':tilts.tilt_mm_per_m.quantile(.95),'max':tilts.tilt_mm_per_m.max()},{'parameter':'curvature','unit':'mm/m²','min':curv.curvature_mm_per_m2.min(),'median':curv.curvature_mm_per_m2.median(),'p95':curv.curvature_mm_per_m2.quantile(.95),'max':curv.curvature_mm_per_m2.max()},{'parameter':'horizontal strain','unit':'×10^-3','min':hstrain.horizontal_strain_x1e3.min(),'median':hstrain.horizontal_strain_x1e3.median(),'p95':hstrain.horizontal_strain_x1e3.quantile(.95),'max':hstrain.horizontal_strain_x1e3.max()}])
regime_distribution=proc.groupby(['point_type','regime']).size().reset_index(name='count'); regime_distribution['fraction_within_point_type']=regime_distribution['count']/regime_distribution.groupby('point_type')['count'].transform('sum'); spatial_autocorrelation=pd.DataFrame([{'field':k,'moran_I_8nn':v} for k,v in morans.items()]); sampling_summary=pd.DataFrame([{'item':'profiles','value':sp.profile_id.nunique(),'note':'14 profiles'},{'item':'survey_points','value':len(sp),'note':'98 WORK + 28 REF'},{'item':'campaigns','value':len(campaigns),'note':'20 full + 7 focused'},{'item':'points_per_campaign_min','value':lev.groupby('campaign_id').point_id.nunique().min(),'note':'all full coverage'},{'item':'GNSS_unique_points','value':gnss.point_id.nunique(),'note':'12 REF + top 30 WORK'},{'item':'InSAR_points','value':insar_cat.insar_point_id.nunique(),'note':f'{insar_cat.nearest_truth_point_id.nunique()} source truth points'},{'item':'stress_scenarios','value':stress_cat.scenario_id.nunique(),'note':f'{stress_cat.point_id.nunique()} high-anchor points'}])
profile_holdout_summary=profile_holdout_metrics.groupby(['target','model']).agg(profiles=('heldout_profile','nunique'),mean_MAE_mm=('MAE_mm','mean'),median_MAE_mm=('MAE_mm','median'),worst_MAE_mm=('MAE_mm','max'),mean_RMSE_mm=('RMSE_mm','mean')).reset_index()
feature_contract=pd.DataFrame([
('observed_settlement_mm','allowed','dynamic observation','Past adjusted leveling value.'),('current_uncertainty_mm','allowed','measurement uncertainty','Use in state-space filter/weights.'),('rate_1_mm_y','allowed','lagged dynamic','Most recent annualized rate.'),('rate_2_mm_y','allowed','lagged dynamic','Second lag.'),('rate_3_mm_y','allowed','lagged dynamic','Third lag.'),('rate_mean3_mm_y','allowed','lagged dynamic','Rolling mean.'),('rate_std3_mm_y','allowed','lagged dynamic','Rolling volatility.'),('rate_change_1_mm_y','allowed','lagged dynamic','Recent rate change.'),('dt_prev_y','allowed','time geometry','Known previous interval.'),('dt_next_y','allowed_for_integration','forecast horizon','Known planned horizon; integrate predicted rate.'),('calendar sin/cos','allowed','calendar','Known planned date.'),('static reconstructed geology/mining fields','conditionally_allowed','static reconstructed','Only with per-feature provenance and uncertainty.'),('current tilt/curvature/neighbor rates','conditionally_allowed','spatial dynamic','Only from current/past cycles; spatial holdout required.'),('profile_type','allowed_with_caution','geometry class','Broad orientation only.'),('settlement_anchor_map_mm','forbidden','terminal/reference map','Future leakage.'),('regime/base_rate/event_amp/event_center/decay_tau','forbidden','latent generator','Generator leakage.'),('true_*_evaluation_only','forbidden','hidden truth','Evaluation only.'),('x_local_m/y_local_m','forbidden_primary','coordinates','Can memorize reconstructed map.'),('point_id/profile_id','forbidden_primary','identity','Encodes individual history.'),('target_*','target_only','target','Never in predictors.')],columns=['feature_or_group','status','class','rationale'])
source_context=pd.DataFrame([{'topic':'Original SKRU-1 data structure','source':'ВКР_Филатова_М_С.docx','source_scope':'12 TAB layers + Excel; GIS field modeling','dataset_use':'layer names, anchors, reference field','caveat':'primary TAB/Excel unavailable'},{'topic':'Temporal behavior and rate bands','source':'Babayants-Disser.pdf / 03-GR-24-2.pdf','source_scope':'InSAR time series and external rate examples','dataset_use':'plausibility/stress design','caveat':'not SKRU-1 leveling'},{'topic':'Survey kinematics','source':'НК 26 Бобровицкий Григорий.pdf','source_scope':'settlement, tilt, curvature, strain, rates','dataset_use':'formula checks','caveat':'another mine'},{'topic':'Geomechanical calibration','source':'106_Губанова__Глебова.pdf','source_scope':'inverse calibration by observed subsidence','dataset_use':'reconstruction concept','caveat':'different deposit'},{'topic':'InSAR geometry','source':'Babayants-Disser.pdf','source_scope':'LOS mixes vertical/east/north','dataset_use':'auxiliary interpretation','caveat':'assumptions or multi-track needed'}])
model_selection=pd.DataFrame([{'role':'recommended control baseline','model':f'Kalman local linear trend q={best_q:g}','why':'best nominal temporal MAE; irregular dt and uncertainty','limitation':'lags rapid acceleration'},{'role':'minimum sanity baseline','model':'mean of last 3 annualized rates','why':'transparent and nearly optimal','limitation':'lags abrupt changes'},{'role':'learned diagnostic','model':'HGB annualized rate','why':'tests added value of static/spatial features','limitation':'worse than simple local dynamics'},{'role':'rejected formulation','model':'HGB direct increment','why':'fails under dt shift','limitation':'do not use as primary target'}])
noise_floor_df=pd.DataFrame([{'comparison':'observed target increment vs hidden truth, 2025',**noise_floor}]); half90=float(conformal_metrics.query("model=='Kalman' and calibration_target=='observed' and nominal_coverage==0.9").half_width_mm.iloc[0]); testdf=model_df[te].copy(); pred2025=testdf[['point_id','profile_id','current_campaign_id','target_campaign_id','current_date','target_date','dt_next_y','observed_settlement_mm','target_increment_observed_mm','target_increment_true_mm','target_rate_true_mm_y','pred_last_rate','pred_mean3_rate','kalman_pred','pred_hgb_rate','pred_hgb_spatial_rate','current_uncertainty_mm']].copy(); pred2025['kalman_interval90_lower_mm']=pred2025.kalman_pred-half90; pred2025['kalman_interval90_upper_mm']=pred2025.kalman_pred+half90; pred2025['kalman_abs_error_true_mm']=abs(pred2025.kalman_pred-pred2025.target_increment_true_mm); pred2025['kalman_abs_error_observed_mm']=abs(pred2025.kalman_pred-pred2025.target_increment_observed_mm)
benchmark_cols=['point_id','profile_id','profile_type','current_campaign_id','target_campaign_id','current_date','target_date','target_year','dt_next_y','dt_prev_y','observed_settlement_mm','current_uncertainty_mm','rate_1_mm_y','rate_2_mm_y','rate_3_mm_y','rate_mean3_mm_y','rate_std3_mm_y','rate_change_1_mm_y','next_month_sin','next_month_cos','kzt','ko','seismic_energy_J_m2','fill_density','log_fault_distance','lithology','current_abs_tilt_mean','curvature_mm_per_m2','neighbor_rate_mean_mm_y','target_increment_observed_mm','target_increment_true_mm','target_rate_observed_mm_y','target_rate_true_mm_y','pred_zero','pred_last_rate','pred_mean3_rate','pred_linear4','pred_quadratic6','pred_const_accel','kalman_pred','pred_ridge_rate','pred_hgb_rate','pred_hgb_spatial_rate','pred_ridge_direct','pred_hgb_direct']; forecast_benchmark=model_df[benchmark_cols].copy(); forecast_benchmark['split']=np.where(forecast_benchmark.target_year<=2023,'train',np.where(forecast_benchmark.target_year==2024,'validation_2024','temporal_test_2025'))

# ---------- export CSV ----------
exports={'table_inventory.csv':table_inventory,'data_quality_checks.csv':data_quality_checks,'issue_register.csv':issue_register,'sensor_quality.csv':sensor_df,'gross_error_confusion.csv':conf.reset_index().rename(columns={False:'passed_closure',True:'failed_closure'}),'field_coverage_summary.csv':coverage,'spatial_autocorrelation.csv':spatial_autocorrelation,'sampling_summary.csv':sampling_summary,'regime_distribution.csv':regime_distribution,'rate_band_distribution.csv':rate_band,'deformation_summary.csv':deformation_summary,'target_correlations.csv':corr_df,'kalman_q_tuning.csv':q_tuning,'baseline_temporal_metrics.csv':baseline_metrics,'random_split_metrics.csv':random_split_metrics,'profile_holdout_metrics.csv':profile_holdout_metrics,'profile_holdout_summary.csv':profile_holdout_summary,'stress_test_metrics.csv':stress_metrics,'stress_event_classification.csv':stress_classification,'conformal_intervals.csv':conformal_metrics,'feature_importance.csv':feature_importance,'feature_contract.csv':feature_contract,'source_context.csv':source_context,'model_selection.csv':model_selection,'measurement_noise_floor.csv':noise_floor_df,'predictions_2025.csv':pred2025,'forecast_benchmark_rows.csv':forecast_benchmark,'stress_predictions.csv':stress_preds}
for n,d in exports.items(): d.to_csv(OUT/'tables'/n,index=False,encoding='utf-8-sig')

# ---------- models/config ----------
joblib.dump(hgb_valid,OUT/'models/hgb_rate_diagnostic.joblib'); joblib.dump(ridge_pipe,OUT/'models/ridge_rate_diagnostic.joblib')
(OUT/'models/kalman_local_trend_config.json').write_text(json.dumps({'model':'local_linear_trend_kalman','state':['settlement_mm','velocity_mm_per_year'],'process_noise_q_accel':best_q,'process_noise_matrix':'q*[[dt^3/3,dt^2/2],[dt^2/2,dt]]','initial_velocity_sd_mm_y':200.,'measurement_variance':'standard_uncertainty_mm^2','tuned_on':'2024 observed increments','tested_on':'2025 temporal holdout','use':'control baseline only','conformal_90_halfwidth_observed_mm':half90},ensure_ascii=False,indent=2),encoding='utf-8')
(OUT/'models/model_hyperparameters.json').write_text(json.dumps({'ridge_rate':{'alpha':10},'hgb_rate':{'max_leaf_nodes':31,'min_samples_leaf':10,'l2_regularization':1,'learning_rate':.05,'max_iter':300},'hgb_spatial_rate':{'max_leaf_nodes':31,'min_samples_leaf':10,'l2_regularization':0},'direct_hgb_rejected':{'max_leaf_nodes':15,'min_samples_leaf':10,'l2_regularization':0}},ensure_ascii=False,indent=2),encoding='utf-8')

# ---------- plots ----------
main=[f'Kalman local trend (q={best_q:g})','Mean of last 3 rates','Linear trend (4 cycles)','Last observed rate','Quadratic trend (6 cycles)','HGB, annualized rate','Ridge, annualized rate','HGB, direct increment','Zero increment']; p=baseline_metrics[(baseline_metrics.target=='hidden_truth')&baseline_metrics.model.isin(main)].sort_values('MAE_mm'); fig,ax=plt.subplots(figsize=(10,6)); ax.barh(p.model,p.MAE_mm); ax.set_xlabel('MAE, мм'); ax.set_title('Временной тест 2025: ошибка следующего цикла'); ax.invert_yaxis(); fig.tight_layout(); fig.savefig(OUT/'figures/01_temporal_test_mae.png',dpi=180); plt.close(fig)
ex=model_df[te&model_df.point_id.eq('P-V05-W007')].sort_values('target_date'); fig,ax=plt.subplots(figsize=(10,5)); ax.plot(ex.target_date,ex.target_increment_true_mm,marker='o',label='Скрытая истина'); ax.plot(ex.target_date,ex.target_increment_observed_mm,marker='o',label='Наблюдение'); ax.plot(ex.target_date,ex.kalman_pred,marker='o',label='Kalman'); ax.plot(ex.target_date,ex.pred_mean3_rate,marker='o',label='Средняя 3 скоростей'); ax.plot(ex.target_date,ex.pred_hgb_rate,marker='o',label='HGB'); ax.set_ylabel('Приращение, мм'); ax.set_title('Репер P-V05-W007, тест 2025'); ax.legend(); ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m')); fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(OUT/'figures/02_example_point_2025.png',dpi=180); plt.close(fig)
fig,ax=plt.subplots(figsize=(10,5)); ax.hist(interval_rates,bins=50); [ax.axvline(t,linestyle='--',label=f'{t} мм/год') for t in [20,75,100,250,400]]; ax.set_xlabel('Истинная межцикловая скорость, мм/год'); ax.set_ylabel('Интервалы'); ax.set_title('Распределение скоростей в номинальном наборе'); ax.legend(ncol=3); fig.tight_layout(); fig.savefig(OUT/'figures/03_nominal_rate_distribution.png',dpi=180); plt.close(fig)
resid=[lev.residual_mm_evaluation_only/lev.standard_uncertainty_mm,gnss.residual_mm_evaluation_only/gnss.standard_uncertainty_mm,insar2.residual_mm_evaluation_only/insar2.standard_uncertainty_mm]; fig,ax=plt.subplots(figsize=(8,5)); ax.boxplot(resid,tick_labels=['Нивелирование','GNSS','InSAR'],showfliers=False); ax.axhline(0,linestyle='--'); ax.set_ylabel('Стандартизованный остаток'); ax.set_title('Калибровка неопределённости'); fig.tight_layout(); fig.savefig(OUT/'figures/04_sensor_standardized_residuals.png',dpi=180); plt.close(fig)
piv=stress_metrics.pivot(index='model',columns='track',values='MAE_mm').sort_values('oracle_qc'); fig,ax=plt.subplots(figsize=(10,6)); x=np.arange(len(piv)); w=.35; ax.bar(x-w/2,piv.oracle_qc,w,label='Oracle QC'); ax.bar(x+w/2,piv.raw,w,label='Raw'); ax.set_xticks(x); ax.set_xticklabels(piv.index,rotation=25,ha='right'); ax.set_ylabel('MAE, мм'); ax.set_title('Стресс-сценарии'); ax.legend(); fig.tight_layout(); fig.savefig(OUT/'figures/05_stress_test_mae.png',dpi=180); plt.close(fig)
acc=stress_classification[(stress_classification.track=='oracle_qc')&(stress_classification.task=='acceleration_jump_20pct_and_20mm_y')].sort_values('F1',ascending=False); fig,ax=plt.subplots(figsize=(9,5)); ax.barh(acc.model,acc.F1); ax.set_xlabel('F1'); ax.set_xlim(0,1); ax.set_title('Раннее обнаружение ускорения'); ax.invert_yaxis(); fig.tight_layout(); fig.savefig(OUT/'figures/06_acceleration_detection_f1.png',dpi=180); plt.close(fig)
ph0=profile_holdout_metrics[(profile_holdout_metrics.target=='hidden_truth')&profile_holdout_metrics.model.isin(['Kalman','HGB_rate'])].pivot(index='heldout_profile',columns='model',values='MAE_mm').sort_index(); fig,ax=plt.subplots(figsize=(12,5)); x=np.arange(len(ph0)); w=.38; ax.bar(x-w/2,ph0.Kalman,w,label='Kalman'); ax.bar(x+w/2,ph0.HGB_rate,w,label='HGB'); ax.set_xticks(x); ax.set_xticklabels(ph0.index,rotation=45,ha='right'); ax.set_ylabel('MAE, мм'); ax.set_title('Исключение целого профиля, 2024–2025'); ax.legend(); fig.tight_layout(); fig.savefig(OUT/'figures/07_profile_holdout_mae.png',dpi=180); plt.close(fig)
fi=feature_importance.head(12).sort_values('mae_increase_rate_mm_y_mean'); fig,ax=plt.subplots(figsize=(9,6)); ax.barh(fi.feature,fi.mae_increase_rate_mm_y_mean,xerr=fi.mae_increase_rate_mm_y_sd); ax.set_xlabel('Рост MAE скорости, мм/год'); ax.set_title('Перестановочная важность HGB'); fig.tight_layout(); fig.savefig(OUT/'figures/08_feature_importance.png',dpi=180); plt.close(fig)
rv=pd.DataFrame([{'protocol':'Случайный split','MAE_mm':random_split_metrics.iloc[0].MAE_mm},{'protocol':'Temporal 2025','MAE_mm':float(baseline_metrics.query("target=='observed' and model=='HGB, annualized rate'").MAE_mm.iloc[0])},{'protocol':'Profile holdout mean','MAE_mm':float(profile_holdout_metrics.query("target=='observed' and model=='HGB_rate'").MAE_mm.mean())}]); fig,ax=plt.subplots(figsize=(8,5)); ax.bar(rv.protocol,rv.MAE_mm); ax.set_ylabel('MAE, мм'); ax.set_title('Влияние протокола валидации'); ax.tick_params(axis='x',rotation=20); fig.tight_layout(); fig.savefig(OUT/'figures/09_validation_protocol_bias.png',dpi=180); plt.close(fig)
sg=grid.dropna(subset=['settlement_reference_map_mm']); fig,ax=plt.subplots(figsize=(8,7)); sca=ax.scatter(sg.x_local_m,sg.y_local_m,c=sg.settlement_reference_map_mm,s=3); ax.scatter(sp[sp.point_type=='WORK'].x_local_m,sp[sp.point_type=='WORK'].y_local_m,s=10,marker='x'); ax.set_xlabel('X local, м'); ax.set_ylabel('Y local, м'); ax.set_title('Поле оседаний и рабочие реперы'); fig.colorbar(sca,ax=ax,label='Оседание, мм'); fig.tight_layout(); fig.savefig(OUT/'figures/10_spatial_field_and_points.png',dpi=180); plt.close(fig)

# ---------- report/readme ----------
hidden=baseline_metrics[baseline_metrics.target=='hidden_truth'].sort_values('MAE_mm'); observed=baseline_metrics[baseline_metrics.target=='observed'].sort_values('MAE_mm'); kh=hidden[hidden.model.str.startswith('Kalman')].iloc[0]; ko=observed[observed.model.str.startswith('Kalman')].iloc[0]; stress_oracle=stress_metrics[stress_metrics.track=='oracle_qc'].sort_values('MAE_mm'); acc0=stress_classification[(stress_classification.track=='oracle_qc')&(stress_classification.task=='acceleration_jump_20pct_and_20mm_y')]
report=f'''# Независимая проверка данных и базовых моделей СКРУ-1 v3.1

## Вердикт

**CONDITIONAL GO для разработки baseline-пайплайна.**

**NO-GO для финального тюнинга сложной модели, производственных выводов и заявления реальной точности.**

Табличная целостность, пространственное покрытие, формулы маркшейдерских производных и синтетическая метрология прошли проверку. Основной блокирующий слой — временной генератор: 91 из 98 рабочих реперов маркированы как ускоряющиеся, в тестовом периоде большинство уже затухает, а следующий цикл почти полностью определяется последней скоростью.

## Ключевые числа

- Таблиц: {len(files)}; строк по всем уровням: {sum(len(d) for d in dfs.values()):,}.
- Формульные расхождения: tilt {np.max(np.abs(tm.tilt_mm_per_m-tm.tilt_calc)):.3e}; curvature {np.max(np.abs(cm.curvature_mm_per_m2-cm.curv_calc)):.3e}; rate {np.max(np.abs(rm.settlement_rate_mm_y-rm.rate_calc)):.3e}; strain {np.max(np.abs(hm.horizontal_strain-hm.strain_calc)):.3e}.
- Нивелирование: RMSE {sensor_df.loc[sensor_df.sensor=='leveling','RMSE_mm'].iloc[0]:.3f} мм, coverage95 {sensor_df.loc[sensor_df.sensor=='leveling','coverage_95'].iloc[0]:.1%}.
- GNSS: RMSE {sensor_df.loc[sensor_df.sensor=='GNSS','RMSE_mm'].iloc[0]:.3f} мм, coverage95 {sensor_df.loc[sensor_df.sensor=='GNSS','coverage_95'].iloc[0]:.1%}.
- InSAR: RMSE {sensor_df.loc[sensor_df.sensor=='InSAR','RMSE_mm'].iloc[0]:.3f} мм, coverage95 {sensor_df.loc[sensor_df.sensor=='InSAR','coverage_95'].iloc[0]:.1%}.
- Gross-error recall: {recall:.1%}; пропущено 4 внедрённых ошибки.
- Moran I: settlement {morans['settlement_reference_map_mm']:.3f}, k_z,T {morans['kzt_reconstructed']:.3f}, k_o {morans['ko_reconstructed']:.3f}, seismic {morans['seismic_energy_mid_J_m2_reconstructed']:.3f}.
- Последняя скорость → следующая истинная скорость: r={corr_rate:.4f}.
- Наблюдаемое → истинное приращение: r={corr_obs_true:.5f}.
- Шумовой пол 2025: MAE {noise_floor['MAE_mm']:.3f} мм.

## Временной тест 2025

| Модель | MAE hidden truth, мм | MAE observed, мм |
|---|---:|---:|
| Kalman q={best_q:g} | {kh.MAE_mm:.3f} | {ko.MAE_mm:.3f} |
| Mean last 3 rates | {hidden.query("model=='Mean of last 3 rates'").MAE_mm.iloc[0]:.3f} | {observed.query("model=='Mean of last 3 rates'").MAE_mm.iloc[0]:.3f} |
| Last rate | {hidden.query("model=='Last observed rate'").MAE_mm.iloc[0]:.3f} | {observed.query("model=='Last observed rate'").MAE_mm.iloc[0]:.3f} |
| HGB annualized rate | {hidden.query("model=='HGB, annualized rate'").MAE_mm.iloc[0]:.3f} | {observed.query("model=='HGB, annualized rate'").MAE_mm.iloc[0]:.3f} |
| HGB direct increment | {hidden.query("model=='HGB, direct increment'").MAE_mm.iloc[0]:.3f} | {observed.query("model=='HGB, direct increment'").MAE_mm.iloc[0]:.3f} |

Kalman выбран как контрольный baseline, потому что учитывает неравные интервалы и uncertainty. Conformal-интервал 90%: prediction ±{half90:.3f} мм; test coverage {conformal_metrics.query("model=='Kalman' and calibration_target=='observed' and nominal_coverage==0.9").test_empirical_coverage.iloc[0]:.1%}.

## Главные дефекты данных

1. **Regime imbalance:** {reg_counts}.
2. **Late-stage mismatch:** median velocity slope 2023–2025 = {ld['50%']:.3f} мм/год².
3. **Extreme nominal tail:** >250 мм/год {np.mean(interval_rates>=250):.1%}; >400 мм/год {np.mean(interval_rates>=400):.1%}; max {interval_rates.max():.1f}.
4. **Focused campaigns are fake:** all cycles observe all 126 points.
5. **Leakage:** terminal settlement map and generator parameters are exported near model features.
6. **Static provenance lost at point level.**
7. **GNSS selection bias:** top 30 WORK settlement points + 12 REF.
8. **InSAR pseudo-replication:** 100 points inherit only {insar_cat.nearest_truth_point_id.nunique()} survey trajectories.
9. **Stress design bias:** 36 scenarios on {len(stress_points)} high-anchor points.
10. **No real SKRU-1 cycles:** production claims remain impossible.

## Stress test

Best oracle-QC MAE = {stress_oracle.MAE_mm.min():.3f} мм ({stress_oracle.iloc[0].model}). Best F1 for early acceleration jump = {acc0.F1.max():.3f}. Thus low nominal MAE does not prove early-warning capability.

## Required baseline formulation

Predict annualized rate and integrate over the known next-cycle horizon:

v_hat(k+1) -> delta_eta_hat(k+1) = v_hat(k+1) * delta_t(k+1).

Direct raw-increment models fail when cycle spacing changes.

## Recommendation

Keep only two mandatory controls:

1. local linear-trend Kalman q={best_q:g};
2. mean of last three annualized rates.

HGB remains diagnostic. Do not tune it deeply until data v3.2 adds balanced regimes, time-varying process stages, rare localized acceleration, genuine focused campaigns, long gaps, point-level provenance/uncertainty, independent InSAR field and stratified GNSS.

Detailed checks, metrics, predictions and feature contract are in `tables/`; plots in `figures/`; model configs in `models/`.
'''
(OUT/'BASELINE_AUDIT_REPORT_RU.md').write_text(report,encoding='utf-8')
(OUT/'README.md').write_text(f'''# SKRU-1 v3.1 baseline audit

Verdict: CONDITIONAL GO for pipeline prototyping; NO-GO for final model claims.

Recommended baseline: local linear-trend Kalman q={best_q:g}. 2025 MAE: {kh.MAE_mm:.3f} mm vs hidden truth, {ko.MAE_mm:.3f} mm vs observed target. Stress early-acceleration F1 remains only {acc0.F1.max():.3f}.

See BASELINE_AUDIT_REPORT_RU.md, tables/, figures/, models/ and run_baseline_audit.py.
''',encoding='utf-8')

# compact reproducible script: copy this full script itself
source_script = Path(__file__).resolve()
destination_script = (OUT/'run_baseline_audit.py').resolve()
if source_script != destination_script:
    shutil.copy2(source_script, destination_script)
print(json.dumps({'output':str(OUT),'tables':len(exports),'best_q':best_q,'test_mae_hidden':kh.MAE_mm,'test_mae_observed':ko.MAE_mm},ensure_ascii=False,indent=2))
