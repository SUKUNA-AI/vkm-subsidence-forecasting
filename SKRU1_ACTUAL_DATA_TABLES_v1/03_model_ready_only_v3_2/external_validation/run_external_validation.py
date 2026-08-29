#!/usr/bin/env python3
import argparse, json, math
from pathlib import Path
import numpy as np, pandas as pd

def kalman_predict(dates, values, sigmas, target_date, q):
    x=np.array([values[0],0.0],float); P=np.diag([max(sigmas[0]**2,1.0),400.0]); last=pd.Timestamp(dates[0])
    for d,z,s in zip(dates[1:],values[1:],sigmas[1:]):
        d=pd.Timestamp(d); dt=(d-last).days/365.25; F=np.array([[1,dt],[0,1]],float); Q=q*np.array([[dt**3/3,dt**2/2],[dt**2/2,dt]],float)
        x=F@x; P=F@P@F.T+Q; H=np.array([[1.,0.]]); R=max(float(s)**2,.25); y=float(z-H@x); S=float(H@P@H.T+R); K=(P@H.T)/S; x=x+K[:,0]*y; P=(np.eye(2)-K@H)@P; last=d
    dt=(pd.Timestamp(target_date)-last).days/365.25; return float((np.array([[1,dt],[0,1]])@x)[0])

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--config',required=True); ap.add_argument('--output',required=True); ap.add_argument('--allow-synthetic-smoke',action='store_true'); a=ap.parse_args()
    df=pd.read_csv(a.input); required=['point_id','date','observed_settlement_mm','standard_uncertainty_mm']; missing=[c for c in required if c not in df.columns]
    if missing: raise SystemExit(f'Missing columns: {missing}')
    df['date']=pd.to_datetime(df['date']); df=df.sort_values(['point_id','date']); cfg=json.loads(Path(a.config).read_text())
    rows=[]
    for pid,g in df.groupby('point_id'):
        g=g.reset_index(drop=True)
        for i in range(3,len(g)):
            hist=g.iloc[:i]; target=g.iloc[i]
            pred=kalman_predict(hist.date.tolist(),hist.observed_settlement_mm.tolist(),hist.standard_uncertainty_mm.tolist(),target.date,cfg['q'])
            rows.append({'point_id':pid,'target_date':target.date.date().isoformat(),'observed_settlement_mm':target.observed_settlement_mm,'predicted_settlement_mm':pred,'residual_mm':pred-target.observed_settlement_mm,'n_history':i})
    out=Path(a.output); out.mkdir(parents=True,exist_ok=True); pred=pd.DataFrame(rows); pred.to_csv(out/'external_predictions.csv',index=False)
    if len(pred):
        r=pred.residual_mm.to_numpy(float); metrics={'n':len(r),'MAE_mm':float(np.mean(np.abs(r))),'RMSE_mm':float(np.sqrt(np.mean(r*r))),'Bias_mm':float(np.mean(r)),'config':cfg,'retrained':False}
    else: metrics={'n':0,'status':'insufficient_history','config':cfg,'retrained':False}
    (out/'external_metrics.json').write_text(json.dumps(metrics,indent=2,ensure_ascii=False),encoding='utf-8')
if __name__=='__main__': main()
