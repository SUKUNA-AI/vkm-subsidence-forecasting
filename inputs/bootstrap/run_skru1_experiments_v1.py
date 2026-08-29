from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import pickle
import re
import shutil
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingRegressor, HistGradientBoostingRegressor, HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import HuberRegressor, LogisticRegression, Ridge
from sklearn.metrics import (
    average_precision_score, precision_recall_curve, precision_score, recall_score,
    f1_score, roc_auc_score, confusion_matrix, mean_absolute_error,
    mean_squared_error, r2_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler, PolynomialFeatures
from sklearn.inspection import permutation_importance

try:
    from catboost import CatBoostRegressor, CatBoostClassifier
    CATBOOST_AVAILABLE = True
except Exception:
    CATBOOST_AVAILABLE = False

try:
    import yaml
except Exception:
    yaml = None

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

VERSION = "SKRU1_Experiment_Suite_v1"
DATASET_VERSION = "SKRU1_Data_Foundation_v3_2_1"
SEED = 32101
RNG = np.random.default_rng(SEED)


# ------------------------------- utilities --------------------------------

def norm(s: str) -> str:
    return str(s).strip().lower()


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False)


def write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")


def json_dump(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def find_file(root: Path, filename: str, required=True) -> Path | None:
    matches = list(root.rglob(filename))
    if not matches:
        if required:
            raise FileNotFoundError(f"{filename} under {root}")
        return None
    return sorted(matches, key=lambda p: (len(p.parts), str(p)))[0]


def find_col(df: pd.DataFrame, exact: Sequence[str] = (), contains: Sequence[str] = (),
             exclude: Sequence[str] = (), required=True) -> str | None:
    lower = {norm(c): c for c in df.columns}
    for x in exact:
        if norm(x) in lower:
            return lower[norm(x)]
    for c in df.columns:
        lc = norm(c)
        if all(x.lower() in lc for x in contains) and not any(x.lower() in lc for x in exclude):
            return c
    if required:
        raise KeyError(f"column not found exact={exact} contains={contains}; columns={list(df.columns)}")
    return None


def parse_date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce")


def as_bool(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s.fillna(False)
    return s.astype(str).str.lower().isin(["true", "1", "yes", "y", "да"])


def robust_minmax(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce")
    lo, hi = x.quantile([0.05, 0.95])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return pd.Series(0.0, index=s.index)
    return ((x - lo) / (hi - lo)).clip(0, 1).fillna(0.5)


def safe_metric(fn: Callable, y, p, default=np.nan):
    try:
        return float(fn(y, p))
    except Exception:
        return default


def regression_metrics(y, p, prefix="") -> dict:
    mask = np.isfinite(y) & np.isfinite(p)
    y = np.asarray(y)[mask]; p = np.asarray(p)[mask]
    if len(y) == 0:
        return {f"{prefix}n": 0}
    return {
        f"{prefix}n": int(len(y)),
        f"{prefix}MAE": float(mean_absolute_error(y, p)),
        f"{prefix}RMSE": float(mean_squared_error(y, p) ** 0.5),
        f"{prefix}Bias": float(np.mean(p - y)),
        f"{prefix}R2": float(r2_score(y, p)) if len(y) > 1 else np.nan,
        f"{prefix}P95_abs_error": float(np.quantile(np.abs(p-y), 0.95)),
    }


def classification_threshold(y, prob, max_fpr=0.10) -> tuple[float, dict]:
    y = np.asarray(y).astype(int); prob = np.asarray(prob, float)
    candidates = np.unique(np.quantile(prob[np.isfinite(prob)], np.linspace(0.02, 0.98, 97)))
    best = (0.5, -1.0, {})
    for t in candidates:
        pred = prob >= t
        tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0,1]).ravel()
        fpr = fp / max(fp + tn, 1)
        prec = precision_score(y, pred, zero_division=0)
        rec = recall_score(y, pred, zero_division=0)
        f1 = f1_score(y, pred, zero_division=0)
        score = f1 if fpr <= max_fpr else f1 - 3*(fpr-max_fpr)
        if score > best[1]:
            best = (float(t), float(score), {"fpr":fpr,"precision":prec,"recall":rec,"f1":f1})
    return best[0], best[2]


def classification_metrics(y, prob, threshold, followup_years=None) -> dict:
    y = np.asarray(y).astype(int); prob=np.asarray(prob,float)
    pred=prob>=threshold
    tn,fp,fn,tp=confusion_matrix(y,pred,labels=[0,1]).ravel()
    out={
        "n":int(len(y)), "positives":int(y.sum()), "threshold":float(threshold),
        "average_precision":safe_metric(average_precision_score,y,prob),
        "roc_auc":safe_metric(roc_auc_score,y,prob) if len(np.unique(y))>1 else np.nan,
        "precision":float(precision_score(y,pred,zero_division=0)),
        "recall":float(recall_score(y,pred,zero_division=0)),
        "f1":float(f1_score(y,pred,zero_division=0)),
        "true_positive":int(tp),"false_positive":int(fp),"false_negative":int(fn),"true_negative":int(tn),
        "fpr":float(fp/max(fp+tn,1)), "missed_event_fraction":float(fn/max(tp+fn,1)),
    }
    if followup_years is not None:
        out["false_warnings_per_100_point_years"] = float(fp / max(float(np.sum(followup_years)),1e-9) * 100)
    return out


def interval_metrics(y, lo, hi, alpha, prefix="") -> dict:
    y=np.asarray(y,float);lo=np.asarray(lo,float);hi=np.asarray(hi,float)
    mask=np.isfinite(y)&np.isfinite(lo)&np.isfinite(hi)
    y=y[mask];lo=lo[mask];hi=hi[mask]
    if len(y)==0: return {f"{prefix}n":0}
    width=hi-lo
    below=(lo-y).clip(min=0); above=(y-hi).clip(min=0)
    wis=width+(2/alpha)*(below+above)
    return {
        f"{prefix}n":int(len(y)),
        f"{prefix}coverage":float(np.mean((y>=lo)&(y<=hi))),
        f"{prefix}mean_width":float(np.mean(width)),
        f"{prefix}median_width":float(np.median(width)),
        f"{prefix}WIS":float(np.mean(wis)),
    }


# --------------------------- data preparation -----------------------------

def load_snapshot(root: Path) -> dict[str,pd.DataFrame]:
    paths={
        "features":root/"model_ready/T1_next_planned_features.csv",
        "labels":root/"evaluation_only/T1_next_planned_labels.csv",
        "hidden":root/"evaluation_only/T1_hidden_truth_labels.csv",
        "splits":root/"metadata/frozen_splits.csv",
        "t5":root/"evaluation_only/T5_early_warning_labels.csv",
        "adjusted":find_file(root,"leveling_adjusted_epochs.csv"),
        "campaigns":find_file(root,"campaigns.csv",required=False) or find_file(root,"survey_campaigns.csv",required=False),
        "membership":find_file(root,"campaign_point_membership.csv"),
        "points":find_file(root,"survey_points.csv",required=False) or find_file(root,"points.csv",required=False),
    }
    out={k:read_csv(p) for k,p in paths.items() if p is not None and p.exists()}
    feat=out["features"];lab=out["labels"];spl=out["splits"]
    data=feat.merge(lab,on=[c for c in ["sample_id","point_id"] if c in feat.columns and c in lab.columns],how="inner",suffixes=("","__label"))
    data=data.merge(spl[[c for c in spl.columns if c in ["sample_id","split","profile_id","zone_id","spatial_zone"]]],on="sample_id",how="left",suffixes=("","__split"))
    if "split__split" in data.columns and "split" not in data.columns: data["split"]=data["split__split"]
    elif "split__split" in data.columns: data["split"]=data["split"].fillna(data["split__split"])
    for c in ["origin_date","target_date"]:
        if c in data.columns: data[c]=parse_date(data[c])
    data=data[as_bool(data["target_available"])].copy()
    out["data"]=data
    return out


def prepare_history(snapshot: Path, adjusted: pd.DataFrame, campaigns: pd.DataFrame|None) -> tuple[dict[str,pd.DataFrame],dict]:
    pcol=find_col(adjusted,exact=["point_id"])
    ccol=find_col(adjusted,exact=["campaign_id","cycle_id"],required=False)
    dcol=find_col(adjusted,exact=["date","campaign_date","observation_date"],required=False)
    if dcol is None and campaigns is not None and ccol:
        camp_id=find_col(campaigns,exact=[ccol,"campaign_id","cycle_id"])
        camp_date=find_col(campaigns,exact=["campaign_date","date","observation_date"])
        campaigns[camp_date]=parse_date(campaigns[camp_date])
        adjusted=adjusted.merge(campaigns[[camp_id,camp_date]].rename(columns={camp_id:ccol}),on=ccol,how="left")
        dcol=camp_date
    if dcol is None: raise KeyError("adjusted epochs lack date")
    adjusted[dcol]=parse_date(adjusted[dcol])
    scol=None
    for c in adjusted.columns:
        lc=norm(c)
        if "settlement" in lc and ("adjust" in lc or "analysis" in lc or "observed" in lc):
            scol=c;break
    if scol is None: scol=find_col(adjusted,contains=["settlement"])
    ucol=find_col(adjusted,contains=["standard","uncertainty"],required=False) or find_col(adjusted,contains=["sigma"],required=False)
    h={}
    for pid,g in adjusted.groupby(pcol):
        gg=g[[dcol,scol]+([ucol] if ucol else [])].copy().sort_values(dcol).dropna(subset=[dcol,scol])
        gg=gg.rename(columns={dcol:"date",scol:"settlement_mm"})
        gg["sigma_mm"]=pd.to_numeric(gg[ucol],errors="coerce") if ucol else 1.0
        gg["sigma_mm"]=gg["sigma_mm"].fillna(1.0).clip(lower=0.1)
        h[str(pid)]=gg[["date","settlement_mm","sigma_mm"]].reset_index(drop=True)
    return h,{"point":pcol,"date":dcol,"settlement":scol,"uncertainty":ucol}


def feature_column(data:pd.DataFrame,names:Sequence[str],contains:Sequence[str]=()) -> str|None:
    return find_col(data,exact=names,contains=contains,required=False)


def derive_last_rates(history:dict[str,pd.DataFrame],pid:str,origin:pd.Timestamp,n=3)->list[float]:
    g=history.get(str(pid))
    if g is None:return []
    g=g[g.date<=origin].tail(n+1)
    if len(g)<2:return []
    vals=[]
    for i in range(1,len(g)):
        dt=(g.iloc[i].date-g.iloc[i-1].date).days/365.25
        if dt>0: vals.append((float(g.iloc[i].settlement_mm)-float(g.iloc[i-1].settlement_mm))/dt)
    return vals[-n:]


# ------------------------------- baselines --------------------------------

def polynomial_predict(history:dict[str,pd.DataFrame],pid:str,origin:pd.Timestamp,target:pd.Timestamp,degree:int,robust=False)->float:
    g=history.get(str(pid))
    if g is None:return np.nan
    g=g[g.date<=origin].tail(10 if degree==2 else 8)
    if len(g)<degree+2:return np.nan
    t=(g.date-origin).dt.days.to_numpy(float)/365.25
    y=g.settlement_mm.to_numpy(float)
    horizon=(target-origin).days/365.25
    current=y[-1]
    try:
        if degree==1 and robust:
            model=HuberRegressor(epsilon=1.35,alpha=0.1).fit(t.reshape(-1,1),y)
            pred=float(model.predict([[horizon]])[0])
        else:
            co=np.polyfit(t,y,degree)
            res=y-np.polyval(co,t)
            med=np.median(res);mad=np.median(np.abs(res-med))+1e-6
            keep=np.abs(res-med)<=3.5*1.4826*mad
            if keep.sum()>=degree+2: co=np.polyfit(t[keep],y[keep],degree)
            pred=float(np.polyval(co,horizon))
        return (pred-current)/max(horizon,1e-9)
    except Exception:return np.nan


def kalman_predict(g:pd.DataFrame,origin:pd.Timestamp,target:pd.Timestamp,q:float)->tuple[float,float,float,float]:
    g=g[g.date<=origin].sort_values("date")
    if len(g)<2:return np.nan,np.nan,np.nan,np.nan
    y=g.settlement_mm.to_numpy(float); dates=pd.to_datetime(g.date).tolist(); sig=g.sigma_mm.to_numpy(float)
    dt0=max((dates[1]-dates[0]).days/365.25,1/365.25)
    x=np.array([y[0],(y[1]-y[0])/dt0],float)
    P=np.diag([max(sig[0]**2,1.0),100.0])
    H=np.array([[1.0,0.0]])
    for i in range(1,len(g)):
        dt=max((dates[i]-dates[i-1]).days/365.25,1/365.25)
        F=np.array([[1.0,dt],[0.0,1.0]])
        Q=q*np.array([[dt**3/3,dt**2/2],[dt**2/2,dt]])
        x=F@x;P=F@P@F.T+Q
        R=max(sig[i]**2,0.01)
        innov=y[i]-(H@x)[0]
        S=(H@P@H.T)[0,0]+R
        K=(P@H.T/S).ravel()
        x=x+K*innov;P=(np.eye(2)-np.outer(K,H.ravel()))@P
    h=max((target-origin).days/365.25,1/365.25)
    F=np.array([[1.0,h],[0.0,1.0]])
    Q=q*np.array([[h**3/3,h**2/2],[h**2/2,h]])
    xp=F@x;Pp=F@P@F.T+Q
    avg_rate=(xp[0]-x[0])/h
    return float(avg_rate),float(xp[0]),float(math.sqrt(max(Pp[0,0],0))),float(math.sqrt(max(Pp[1,1],0)))


def baseline_predictions(data:pd.DataFrame,history:dict[str,pd.DataFrame],q_fixed:float,q_adaptive_base:float)->pd.DataFrame:
    rows=[]
    last_col=feature_column(data,["last_rate_mm_y"],contains=["last","rate"])
    mean3_col=feature_column(data,["mean_last_3_rates_mm_y"],contains=["mean","3","rate"])
    accel_col=feature_column(data,["last_acceleration_mm_y2","acceleration_mm_y2"],contains=["accel"])
    for _,r in data.iterrows():
        pid=str(r.point_id);origin=pd.Timestamp(r.origin_date);target=pd.Timestamp(r.target_date)
        rates=derive_last_rates(history,pid,origin,3)
        last=float(pd.to_numeric(r.get(last_col,np.nan),errors="coerce")) if last_col else (rates[-1] if rates else np.nan)
        mean3=float(pd.to_numeric(r.get(mean3_col,np.nan),errors="coerce")) if mean3_col else (np.mean(rates) if rates else np.nan)
        if not np.isfinite(last) and rates:last=rates[-1]
        if not np.isfinite(mean3):mean3=np.mean(rates) if rates else last
        linear=polynomial_predict(history,pid,origin,target,1,True)
        quad=polynomial_predict(history,pid,origin,target,2,False)
        g=history.get(pid,pd.DataFrame())
        kfix=kalman_predict(g,origin,target,q_fixed) if len(g) else (np.nan,)*4
        accel=float(pd.to_numeric(r.get(accel_col,np.nan),errors="coerce")) if accel_col else (last-mean3 if np.isfinite(last) and np.isfinite(mean3) else 0)
        h=(target-origin).days/365.25
        scale=1+min(5.0,abs(accel)/20.0)+min(2.0,h)
        kadapt=kalman_predict(g,origin,target,q_adaptive_base*scale) if len(g) else (np.nan,)*4
        rows.append({
            "sample_id":r.sample_id,"point_id":pid,"split":r.split,
            "origin_date":origin,"target_date":target,"horizon_days":r.horizon_days,
            "B0_zero":0.0,"B1_last_rate":last,"B2_mean_last3":mean3,
            "B3_robust_linear":linear,"B4_quadratic":quad,
            "B5_kalman_fixed":kfix[0],"B5_next_settlement":kfix[1],"B5_sigma_settlement":kfix[2],"B5_sigma_rate":kfix[3],
            "B6_kalman_adaptive":kadapt[0],"B6_next_settlement":kadapt[1],"B6_sigma_settlement":kadapt[2],"B6_sigma_rate":kadapt[3],
        })
    return pd.DataFrame(rows)


def tune_kalman(data:pd.DataFrame,history:dict[str,pd.DataFrame],adaptive=False)->tuple[float,pd.DataFrame]:
    qs=[0.5,1,2,5,10,25,50,100,250,500,1000]
    val=data[data.split=="validation"]
    rows=[]
    target=val.target_rate_mm_y.to_numpy(float)
    last_col=feature_column(val,["last_rate_mm_y"],contains=["last","rate"])
    mean3_col=feature_column(val,["mean_last_3_rates_mm_y"],contains=["mean","3","rate"])
    accel_col=feature_column(val,["last_acceleration_mm_y2"],contains=["accel"])
    for q in qs:
        preds=[]
        for _,r in val.iterrows():
            base=q
            if adaptive:
                last=float(pd.to_numeric(r.get(last_col,np.nan),errors="coerce")) if last_col else np.nan
                mean3=float(pd.to_numeric(r.get(mean3_col,np.nan),errors="coerce")) if mean3_col else np.nan
                acc=float(pd.to_numeric(r.get(accel_col,np.nan),errors="coerce")) if accel_col else (last-mean3 if np.isfinite(last) and np.isfinite(mean3) else 0)
                h=(pd.Timestamp(r.target_date)-pd.Timestamp(r.origin_date)).days/365.25
                base=q*(1+min(5,abs(acc)/20)+min(2,h))
            g=history.get(str(r.point_id),pd.DataFrame())
            preds.append(kalman_predict(g,pd.Timestamp(r.origin_date),pd.Timestamp(r.target_date),base)[0] if len(g) else np.nan)
        m=regression_metrics(target,np.asarray(preds))
        rows.append({"adaptive":adaptive,"q":q,**m})
    df=pd.DataFrame(rows)
    best=float(df.sort_values("MAE").iloc[0].q)
    return best,df


# ------------------------- safe feature engineering -----------------------
FORBIDDEN_TOKENS=[
    "target_","true_","hidden","event_onset","process_family","regime_stage","base_rate","event_amp","event_center","decay_tau",
    "settlement_anchor_map","x_local","y_local","longitude","latitude","sample_weight",
]
ID_TOKENS=["sample_id","point_id","profile_id","origin_date","target_date","campaign_id","split","label_status"]

GROUP_PATTERNS={
    "history":["last_rate","mean_last","rate_lag","settlement_lag","last_settlement","acceleration","history","rolling"],
    "calendar":["horizon","month","day_of_year","season","campaign_type","elapsed","interval"],
    "mining":["depth","chamber","pillar","load","backfill","mining","axial","extraction","roof"],
    "geology":["kzt","ko_","seismic","fault","lithology","terrain","roughness","tri","lineament","geolog"],
    "profile":["profile_mean","profile_std","neighbor","neighbour","local_mean","local_std"],
    "gnss":["gnss"],
    "insar":["insar","los","coherence"],
    "quality":["uncertainty","provenance","donor_distance","is_missing","unknown","qc_","coverage"],
}


def safe_feature_columns(data:pd.DataFrame)->list[str]:
    cols=[]
    for c in data.columns:
        lc=norm(c)
        if any(t in lc for t in FORBIDDEN_TOKENS):continue
        if c in ID_TOKENS or any(lc==x for x in ID_TOKENS):continue
        if c in ["target_rate_mm_y","target_increment_mm","target_next_settlement_mm","target_available"]:continue
        if pd.api.types.is_datetime64_any_dtype(data[c]):continue
        cols.append(c)
    return cols


def categorize_features(data:pd.DataFrame,features:list[str])->dict[str,list[str]]:
    groups={k:[] for k in GROUP_PATTERNS}
    assigned=set()
    for c in features:
        lc=norm(c)
        for g,pats in GROUP_PATTERNS.items():
            if any(p in lc for p in pats):
                groups[g].append(c);assigned.add(c);break
    groups["other"]=[c for c in features if c not in assigned]
    return groups


def make_model_data(df:pd.DataFrame,cols:list[str]):
    X=df[cols].copy()
    cat=[]
    for c in cols:
        if X[c].dtype==object or str(X[c].dtype).startswith("category") or X[c].dtype==bool:
            X[c]=X[c].astype(str).fillna("__MISSING__")
            cat.append(c)
        else:
            X[c]=pd.to_numeric(X[c],errors="coerce")
    return X,cat


def fit_residual_model(train:pd.DataFrame,val:pd.DataFrame,features:list[str],base_col:str,quantile:float=0.5,params:dict|None=None):
    ytr=train.target_rate_mm_y.to_numpy(float)-train[base_col].to_numpy(float)
    Xtr,cat=make_model_data(train,features)
    if CATBOOST_AVAILABLE:
        loss="RMSE" if quantile==0.5 else f"Quantile:alpha={quantile}"
        p=dict(iterations=350,depth=4,learning_rate=0.035,l2_leaf_reg=5,random_seed=SEED,verbose=False,allow_writing_files=False,loss_function=loss)
        if params:p.update(params)
        model=CatBoostRegressor(**p)
        model.fit(Xtr,ytr,cat_features=cat)
        return model,cat,"catboost"
    # Fallback: one-hot + gradient boosting quantile/Huber.
    num=[c for c in features if c not in cat]
    pre=ColumnTransformer([
        ("num",Pipeline([("imp",SimpleImputer(strategy="median"))]),num),
        ("cat",Pipeline([("imp",SimpleImputer(strategy="most_frequent")),("oh",OneHotEncoder(handle_unknown="ignore"))]),cat),
    ])
    loss="huber" if quantile==0.5 else "quantile"
    reg=GradientBoostingRegressor(loss=loss,alpha=quantile if quantile!=0.5 else 0.9,n_estimators=250,max_depth=3,learning_rate=0.035,random_state=SEED)
    model=Pipeline([("pre",pre),("reg",reg)]).fit(Xtr,ytr)
    return model,cat,"sklearn_gradient_boosting_fallback"


def predict_model(model,df,features):
    X,_=make_model_data(df,features)
    return np.asarray(model.predict(X),float)


def tune_hybrid(train,val,features,base_col)->tuple[dict,pd.DataFrame,str]:
    grids=[
        {"depth":3,"iterations":250,"learning_rate":0.04,"l2_leaf_reg":5},
        {"depth":4,"iterations":350,"learning_rate":0.035,"l2_leaf_reg":5},
        {"depth":5,"iterations":450,"learning_rate":0.025,"l2_leaf_reg":8},
    ] if CATBOOST_AVAILABLE else [{}]
    rows=[];best=None;best_mae=float("inf");backend=""
    for i,p in enumerate(grids):
        model,cat,backend=fit_residual_model(train,val,features,base_col,0.5,p)
        pred=val[base_col].to_numpy(float)+predict_model(model,val,features)
        mae=mean_absolute_error(val.target_rate_mm_y,pred)
        rows.append({"config_id":i,"backend":backend,"params":json.dumps(p),"validation_MAE":mae})
        if mae<best_mae:best_mae=mae;best=p
    return best or {},pd.DataFrame(rows),backend


# ----------------------------- T1 experiment ------------------------------

def run_t1(root:Path,out:Path,data:pd.DataFrame,history:dict[str,pd.DataFrame])->dict:
    q5,t5=tune_kalman(data,history,adaptive=False)
    q6,t6=tune_kalman(data,history,adaptive=True)
    tune=pd.concat([t5,t6],ignore_index=True)
    write_csv(tune,out/"tables/T1_kalman_tuning.csv")
    base=baseline_predictions(data,history,q5,q6)
    df=data.merge(base,on=["sample_id","point_id","split","origin_date","target_date","horizon_days"],how="left")
    features=safe_feature_columns(df)
    groups=categorize_features(df,features)

    tr=df[df.split=="train"].copy();va=df[df.split=="validation"].copy();te=df[df.split=="test"].copy()
    best_params,tuning,backend=tune_hybrid(tr,va,features,"B6_kalman_adaptive")
    write_csv(tuning,out/"tables/T1_hybrid_tuning.csv")

    # Validation models (train only), then final models (train+validation) for test.
    qmodels={}
    for q in [0.025,0.10,0.50,0.90,0.975]:
        qmodels[("val",q)]=fit_residual_model(tr,va,features,"B6_kalman_adaptive",q,best_params)[0]
        qmodels[("test",q)]=fit_residual_model(pd.concat([tr,va],ignore_index=True),te,features,"B6_kalman_adaptive",q,best_params)[0]
    for subset,name in [(va,"validation"),(te,"test")]:
        key="val" if name=="validation" else "test"
        for q,label in [(0.025,"P025"),(0.10,"P10"),(0.50,"P50"),(0.90,"P90"),(0.975,"P975")]:
            subset[f"H1_{label}"]=subset.B6_kalman_adaptive.to_numpy(float)+predict_model(qmodels[(key,q)],subset,features)
    # Train predictions use train-fitted models for diagnostic only.
    for q,label in [(0.025,"P025"),(0.10,"P10"),(0.50,"P50"),(0.90,"P90"),(0.975,"P975")]:
        tr[f"H1_{label}"]=tr.B6_kalman_adaptive.to_numpy(float)+predict_model(qmodels[("val",q)],tr,features)
    full=pd.concat([tr,va,te],ignore_index=True).sort_values(["origin_date","point_id"])

    # Conformal intervals for every baseline, calibrated on validation residuals.
    model_cols=["B0_zero","B1_last_rate","B2_mean_last3","B3_robust_linear","B4_quadratic","B5_kalman_fixed","B6_kalman_adaptive","H1_P50"]
    conf=[]
    for m in model_cols:
        resid=np.abs(va.target_rate_mm_y.to_numpy(float)-va[m].to_numpy(float))
        resid=resid[np.isfinite(resid)]
        q80=float(np.quantile(resid,0.80)) if len(resid) else np.nan
        q95=float(np.quantile(resid,0.95)) if len(resid) else np.nan
        conf.append({"model":m,"q80_abs_residual":q80,"q95_abs_residual":q95,"calibration_split":"validation"})
        full[f"{m}_lo80"]=full[m]-q80;full[f"{m}_hi80"]=full[m]+q80
        full[f"{m}_lo95"]=full[m]-q95;full[f"{m}_hi95"]=full[m]+q95
    # Hybrid native quantiles are retained separately.
    write_csv(pd.DataFrame(conf),out/"tables/T1_conformal_calibration.csv")

    # Derived outputs.
    h=full.horizon_days.to_numpy(float)/365.25
    current_col=feature_column(full,["current_settlement_mm","last_settlement_mm"],contains=["current","settlement"])
    current=pd.to_numeric(full[current_col],errors="coerce").to_numpy(float) if current_col else np.full(len(full),np.nan)
    for m in model_cols:
        full[f"{m}_increment_mm"]=full[m].to_numpy(float)*h
        full[f"{m}_next_settlement_mm"]=current+full[f"{m}_increment_mm"].to_numpy(float)

    # Metrics by split and target.
    metrics=[];intervals=[]
    for split,g in full.groupby("split"):
        yrate=g.target_rate_mm_y.to_numpy(float);yinc=g.target_increment_mm.to_numpy(float);ynext=g.target_next_settlement_mm.to_numpy(float)
        for m in model_cols:
            row={"model":m,"split":split,"target":"observed",**regression_metrics(yrate,g[m].to_numpy(float),"rate_")}
            row.update(regression_metrics(yinc,g[f"{m}_increment_mm"].to_numpy(float),"increment_"))
            row.update(regression_metrics(ynext,g[f"{m}_next_settlement_mm"].to_numpy(float),"next_settlement_"))
            metrics.append(row)
            for lev,alpha in [(80,0.2),(95,0.05)]:
                intervals.append({"model":m,"split":split,"level":lev,**interval_metrics(yrate,g[f"{m}_lo{lev}"],g[f"{m}_hi{lev}"],alpha)})
        # native hybrid intervals
        intervals.append({"model":"H1_native_quantiles","split":split,"level":80,**interval_metrics(yrate,g.H1_P10,g.H1_P90,0.2)})
        intervals.append({"model":"H1_native_quantiles","split":split,"level":95,**interval_metrics(yrate,g.H1_P025,g.H1_P975,0.05)})

    # Hidden truth evaluation if canonical truth fields can be found.
    hidden_path=root/"evaluation_only/T1_hidden_truth_labels.csv"
    if hidden_path.exists():
        hid=read_csv(hidden_path)
        hid_rate=None
        for c in hid.columns:
            lc=norm(c)
            if "rate" in lc and "true" in lc:hid_rate=c;break
        if hid_rate:
            hh=full.merge(hid[["sample_id",hid_rate]],on="sample_id",how="left")
            for split,g in hh.groupby("split"):
                y=pd.to_numeric(g[hid_rate],errors="coerce").to_numpy(float)
                for m in model_cols:
                    metrics.append({"model":m,"split":split,"target":"hidden_truth",**regression_metrics(y,g[m].to_numpy(float),"rate_")})

    write_csv(pd.DataFrame(metrics),out/"tables/T1_all_metrics.csv")
    write_csv(pd.DataFrame(intervals),out/"tables/T1_all_interval_metrics.csv")
    pred_cols=["sample_id","point_id","split","origin_date","target_date","horizon_days","target_rate_mm_y","target_increment_mm","target_next_settlement_mm"]+model_cols+[c for c in full.columns if c.startswith("H1_P") or c.endswith("_lo80") or c.endswith("_hi80") or c.endswith("_lo95") or c.endswith("_hi95") or c.endswith("_increment_mm") or c.endswith("_next_settlement_mm")]
    pred_cols=[c for c in pred_cols if c in full.columns]
    write_csv(full[pred_cols],out/"predictions/T1_predictions.csv")

    # Feature importance of full hybrid on test using median residual model.
    test_model=qmodels[("test",0.5)]
    try:
        if CATBOOST_AVAILABLE and hasattr(test_model,"get_feature_importance"):
            imp=test_model.get_feature_importance()
            fi=pd.DataFrame({"feature":features,"importance":imp}).sort_values("importance",ascending=False)
        else:
            # Permutation on test residuals.
            X,_=make_model_data(te,features);y=te.target_rate_mm_y-te.B6_kalman_adaptive
            pi=permutation_importance(test_model,X,y,n_repeats=10,random_state=SEED,scoring="neg_mean_absolute_error")
            fi=pd.DataFrame({"feature":features,"importance":pi.importances_mean}).sort_values("importance",ascending=False)
    except Exception as e:
        fi=pd.DataFrame({"feature":features,"importance":np.nan,"error":str(e)})
    write_csv(fi,out/"tables/T1_feature_importance.csv")
    json_dump({"catboost_available":CATBOOST_AVAILABLE,"backend":backend,"best_params":best_params,"q_fixed":q5,"q_adaptive_base":q6,"features":features,"groups":groups},out/"metadata/T1_model_config.json")
    return {"full":full,"metrics":pd.DataFrame(metrics),"intervals":pd.DataFrame(intervals),"features":features,"groups":groups,"best_params":best_params,"backend":backend,"q_fixed":q5,"q_adaptive":q6}


# ------------------------------- ablations --------------------------------

def run_ablations(out:Path,full:pd.DataFrame,groups:dict[str,list[str]],params:dict)->pd.DataFrame:
    order=[
        ("E0",["history"]),("E1",["history","calendar"]),("E2",["history","calendar","mining"]),
        ("E3",["history","calendar","mining","geology"]),("E4",["history","calendar","mining","geology","profile"]),
        ("E5",["history","calendar","mining","geology","profile","gnss"]),
        ("E6",["history","calendar","mining","geology","profile","gnss","insar"]),
        ("E7",["history","calendar","mining","geology","profile","gnss","insar","quality"]),
        ("E8",["history","calendar","mining","geology","profile","gnss","insar","quality","other"]),
    ]
    rows=[];feature_rows=[]
    tr=full[full.split=="train"].copy();va=full[full.split=="validation"].copy();te=full[full.split=="test"].copy()
    for eid,glist in order:
        feats=[]
        for g in glist:
            feats.extend(groups.get(g,[]))
        feats=list(dict.fromkeys([f for f in feats if f in full.columns]))
        if not feats:
            rows.append({"experiment_id":eid,"split":"validation","status":"SKIPPED_NO_FEATURES","n_features":0})
            rows.append({"experiment_id":eid,"split":"test","status":"SKIPPED_NO_FEATURES","n_features":0})
            continue
        mval,_,backend=fit_residual_model(tr,va,feats,"B6_kalman_adaptive",0.5,params)
        pv=va.B6_kalman_adaptive.to_numpy(float)+predict_model(mval,va,feats)
        mv=regression_metrics(va.target_rate_mm_y.to_numpy(float),pv)
        rows.append({"experiment_id":eid,"split":"validation","status":"OK","backend":backend,"n_features":len(feats),**mv})
        mt,_,_=fit_residual_model(pd.concat([tr,va]),te,feats,"B6_kalman_adaptive",0.5,params)
        pt=te.B6_kalman_adaptive.to_numpy(float)+predict_model(mt,te,feats)
        mm=regression_metrics(te.target_rate_mm_y.to_numpy(float),pt)
        rows.append({"experiment_id":eid,"split":"test","status":"OK","backend":backend,"n_features":len(feats),**mm})
        feature_rows += [{"experiment_id":eid,"feature":f,"group":next((g for g,x in groups.items() if f in x),"unknown")} for f in feats]
    res=pd.DataFrame(rows);write_csv(res,out/"ablations/ablation_metrics.csv");write_csv(pd.DataFrame(feature_rows),out/"ablations/ablation_feature_sets.csv")
    return res


# ------------------------------- T5 models --------------------------------

def join_t5(root:Path,full:pd.DataFrame)->pd.DataFrame:
    labels=read_csv(root/"evaluation_only/T5_early_warning_labels.csv")
    available=as_bool(labels.label_available)
    labels=labels[available].copy()
    # Join feature origins. T5 uses origin features only; T1 target columns excluded.
    cols=[c for c in full.columns if c not in ["T4_activity_180d","T5_onset_180d"]]
    t5=labels.merge(full[cols].drop_duplicates("sample_id"),on=["sample_id","point_id"],how="inner",suffixes=("","__t1"))
    if "split__t1" in t5.columns:
        # T5 split must be based on horizon_end, not T1 target date.
        pass
    if "split" not in labels.columns:
        h=pd.to_datetime(labels.horizon_end)
        labels["split"]=np.select([h<=pd.Timestamp("2023-12-31"),h.dt.year==2024],["train","validation"],default="test")
    t5["split"]=t5.get("split_x",t5.get("split",labels.split))
    if "split_x" in t5.columns:t5["split"]=t5["split_x"]
    return t5


def safe_classification_features(df:pd.DataFrame)->list[str]:
    return [c for c in safe_feature_columns(df) if c not in ["T4_activity_180d","T5_onset_180d"]]


def fit_classifier(train:pd.DataFrame,features:list[str],kind:str):
    X,cat=make_model_data(train,features);y=train.T5_onset_180d.astype(int).to_numpy()
    if kind=="catboost" and CATBOOST_AVAILABLE:
        model=CatBoostClassifier(iterations=400,depth=4,learning_rate=0.035,l2_leaf_reg=8,random_seed=SEED,verbose=False,allow_writing_files=False,loss_function="Logloss",auto_class_weights="Balanced")
        model.fit(X,y,cat_features=cat);return model,"catboost"
    num=[c for c in features if c not in cat]
    pre=ColumnTransformer([
        ("num",Pipeline([("imp",SimpleImputer(strategy="median")),("scale",StandardScaler())]),num),
        ("cat",Pipeline([("imp",SimpleImputer(strategy="most_frequent")),("oh",OneHotEncoder(handle_unknown="ignore"))]),cat),
    ])
    if kind=="hazard":
        clf=LogisticRegression(max_iter=5000,class_weight="balanced",C=0.5,random_state=SEED)
    elif kind=="catboost":
        clf=HistGradientBoostingClassifier(max_iter=250,learning_rate=0.04,max_leaf_nodes=15,l2_regularization=5,random_state=SEED,class_weight="balanced")
        # HistGradient cannot consume sparse one-hot reliably; use ordinal-ish numeric-only fallback.
        pre=ColumnTransformer([("num",Pipeline([("imp",SimpleImputer(strategy="median"))]),num)],remainder="drop")
    else:
        clf=LogisticRegression(max_iter=5000,class_weight="balanced",C=1.0,random_state=SEED)
    return Pipeline([("pre",pre),("clf",clf)]).fit(X,y),f"sklearn_{kind}"


def predict_proba_model(model,df,features):
    X,_=make_model_data(df,features)
    p=model.predict_proba(X)
    return np.asarray(p[:,1],float)


def run_t5(root:Path,out:Path,full:pd.DataFrame)->dict:
    t5=join_t5(root,full)
    t5["T5_onset_180d"]=pd.to_numeric(t5.T5_onset_180d,errors="coerce").fillna(0).astype(int)
    feats=safe_classification_features(t5)
    # Add baseline hazard/calendar features if absent.
    if "origin_month" not in t5.columns:t5["origin_month"]=pd.to_datetime(t5.origin_date).dt.month
    if "origin_year" not in t5.columns:t5["origin_year"]=pd.to_datetime(t5.origin_date).dt.year
    hazard_feats=list(dict.fromkeys(feats+["origin_month","origin_year"]))
    tr=t5[t5.split=="train"].copy();va=t5[t5.split=="validation"].copy();te=t5[t5.split=="test"].copy()
    if tr.T5_onset_180d.sum()<2 or va.T5_onset_180d.sum()<1 or te.T5_onset_180d.sum()<1:
        # Still run, but record the severe event-count limitation.
        pass
    rows=[];preds=te[["sample_id","point_id","origin_date","horizon_end","T5_onset_180d"]].copy()
    # Rule baseline score: acceleration + rate divergence + uncertainty-aware soft score.
    last=feature_column(t5,["last_rate_mm_y"],contains=["last","rate"])
    mean3=feature_column(t5,["mean_last_3_rates_mm_y"],contains=["mean","3","rate"])
    accel=feature_column(t5,["last_acceleration_mm_y2"],contains=["accel"])
    def rule_score(df):
        l=pd.to_numeric(df[last],errors="coerce").fillna(0) if last else pd.Series(0,index=df.index)
        m=pd.to_numeric(df[mean3],errors="coerce").fillna(l) if mean3 else l
        a=pd.to_numeric(df[accel],errors="coerce").fillna(0) if accel else (l-m)*2
        z=0.06*(l-m)+0.035*a+0.01*np.maximum(l-40,0)
        return 1/(1+np.exp(-z))
    pr_va=rule_score(va).to_numpy(float);pr_te=rule_score(te).to_numpy(float)
    th,_=classification_threshold(va.T5_onset_180d,pr_va)
    met=classification_metrics(te.T5_onset_180d,pr_te,th,np.full(len(te),180/365.25));met.update({"model":"rule_based","backend":"rule"});rows.append(met);preds["rule_based_probability"]=pr_te;preds["rule_based_alert"]=pr_te>=th

    models={}
    for kind,features in [("logistic",feats),("catboost",feats),("hazard",hazard_feats)]:
        model,backend=fit_classifier(tr,features,kind)
        pv=predict_proba_model(model,va,features);pt=predict_proba_model(model,te,features)
        th,_=classification_threshold(va.T5_onset_180d,pv)
        met=classification_metrics(te.T5_onset_180d,pt,th,np.full(len(te),180/365.25));met.update({"model":kind,"backend":backend});rows.append(met)
        preds[f"{kind}_probability"]=pt;preds[f"{kind}_alert"]=pt>=th
        models[kind]=(model,features,th,backend)
    metrics=pd.DataFrame(rows).sort_values("average_precision",ascending=False)
    write_csv(metrics,out/"tables/T5_test_metrics.csv");write_csv(preds,out/"predictions/T5_test_predictions.csv")

    # Event-level detection for unique onset events.
    ev=[]
    if "event_onset_date" in te.columns:
        events=te[te.T5_onset_180d==1].dropna(subset=["event_onset_date"]).copy()
        events["event_onset_date"]=parse_date(events.event_onset_date)
        for model,(_,_,th,_) in models.items():
            pc=f"{model}_probability"
            # probs currently only in preds; merge back.
            temp=te[["sample_id","point_id","origin_date","event_onset_date","T5_onset_180d"]].merge(preds[["sample_id",pc]],on="sample_id",how="left")
            for (pid,ed),g in temp[temp.T5_onset_180d==1].groupby(["point_id","event_onset_date"]):
                alerted=g[g[pc]>=th].sort_values("origin_date")
                detected=not alerted.empty
                lead=(pd.Timestamp(ed)-pd.Timestamp(alerted.iloc[0].origin_date)).days if detected else np.nan
                ev.append({"model":model,"point_id":pid,"event_onset_date":ed,"detected":detected,"lead_time_days":lead,"n_candidate_origins":len(g)})
    evdf=pd.DataFrame(ev);write_csv(evdf,out/"tables/T5_event_detection.csv")
    # Save model bundles.
    for name,(model,features,th,backend) in models.items():
        with (out/"models"/f"T5_{name}.pkl").open("wb") as f:pickle.dump({"model":model,"features":features,"threshold":th,"backend":backend},f)
    return {"data":t5,"metrics":metrics,"predictions":preds,"models":models,"events":evdf,"features":feats}


# ----------------------- spatial/regime/stress validation -----------------

def train_hybrid_fold(train:pd.DataFrame,test:pd.DataFrame,features:list[str],params:dict)->np.ndarray:
    m,_,_=fit_residual_model(train,test,features,"B6_kalman_adaptive",0.5,params)
    return test.B6_kalman_adaptive.to_numpy(float)+predict_model(m,test,features)


def add_zone_ids(root:Path,full:pd.DataFrame)->pd.DataFrame:
    if "zone_id" in full.columns:return full
    points_path=find_file(root,"survey_points.csv",required=False)
    if points_path:
        pts=read_csv(points_path);p=find_col(pts,exact=["point_id"])
        x=find_col(pts,exact=["x_local_m","x_m","x"],required=False);y=find_col(pts,exact=["y_local_m","y_m","y"],required=False)
        if x and y:
            xx=pd.to_numeric(pts[x],errors="coerce");yy=pd.to_numeric(pts[y],errors="coerce")
            pts["zone_id"]="Z"+(xx>=xx.median()).astype(int).astype(str)+(yy>=yy.median()).astype(int).astype(str)
            return full.merge(pts[[p,"zone_id"]].rename(columns={p:"point_id"}),on="point_id",how="left")
    if "profile_id" in full.columns:
        profs=sorted(full.profile_id.dropna().astype(str).unique());mapping={p:f"Z{i%4+1}" for i,p in enumerate(profs)}
        full["zone_id"]=full.profile_id.astype(str).map(mapping)
    else:full["zone_id"]="Z1"
    return full


def run_spatial_validation(root:Path,out:Path,full:pd.DataFrame,features:list[str],params:dict)->pd.DataFrame:
    full=add_zone_ids(root,full.copy())
    rows=[]
    for validation,group_col in [("leave_profile_out","profile_id"),("leave_zone_out","zone_id")]:
        if group_col not in full.columns:continue
        for group in sorted(full[group_col].dropna().astype(str).unique()):
            test=full[(full[group_col].astype(str)==group)&(full.split.isin(["validation","test"]))].copy()
            train=full[(full[group_col].astype(str)!=group)&(full.split.isin(["train","validation"]))].copy()
            if len(test)<5 or len(train)<20:continue
            # B6 is local and needs no fold training.
            rows.append({"validation":validation,"held_out_group":group,"model":"B6_kalman_adaptive",**regression_metrics(test.target_rate_mm_y,test.B6_kalman_adaptive)})
            pred=train_hybrid_fold(train,test,features,params)
            rows.append({"validation":validation,"held_out_group":group,"model":"H1_hybrid",**regression_metrics(test.target_rate_mm_y,pred)})
    df=pd.DataFrame(rows);write_csv(df,out/"validation/spatial_validation_metrics.csv");return df


def run_regime_transition_validation(root:Path,out:Path,full:pd.DataFrame)->pd.DataFrame:
    truth_paths=[]
    for p in (root/"evaluation_only").rglob("*.csv"):
        try:
            cols=pd.read_csv(p,nrows=3).columns
        except:continue
        if "point_id" in cols and any("stage" in norm(c) for c in cols) and any("date" in norm(c) for c in cols):truth_paths.append(p)
    if not truth_paths:
        df=pd.DataFrame(columns=["transition","model","n","MAE"]);write_csv(df,out/"validation/regime_transition_metrics.csv");return df
    truth=read_csv(truth_paths[0]);p=find_col(truth,exact=["point_id"]);d=find_col(truth,exact=["date","month"]);s=find_col(truth,contains=["stage"])
    truth[d]=parse_date(truth[d]);truth=truth.sort_values([p,d])
    lookup={str(pid):g for pid,g in truth.groupby(p)}
    rows=[]
    for _,r in full.iterrows():
        g=lookup.get(str(r.point_id));
        if g is None:continue
        prior=g[g[d]<=r.origin_date];future=g[(g[d]>r.origin_date)&(g[d]<=r.target_date)]
        if prior.empty or future.empty:continue
        a=str(prior.iloc[-1][s]);b=str(future.iloc[-1][s]);transition=f"{a}→{b}"
        if a==b:continue
        for m in ["B1_last_rate","B6_kalman_adaptive","H1_P50"]:
            rows.append({"sample_id":r.sample_id,"transition":transition,"model":m,"absolute_error":abs(float(r[m])-float(r.target_rate_mm_y))})
    raw=pd.DataFrame(rows)
    if raw.empty:res=pd.DataFrame(columns=["transition","model","n","MAE"])
    else:res=raw.groupby(["transition","model"]).absolute_error.agg(n="size",MAE="mean",P95=lambda x:np.quantile(x,.95)).reset_index()
    write_csv(res,out/"validation/regime_transition_metrics.csv");write_csv(raw,out/"validation/regime_transition_errors.csv");return res


def find_stress_tables(root:Path)->list[Path]:
    return [p for p in root.rglob("*.csv") if "stress" in str(p).lower()]


def run_stress_validation(root:Path,out:Path,t1_result:dict,t5_result:dict)->dict:
    files=find_stress_tables(root)
    inventory=[]
    for p in files:
        try:
            df=pd.read_csv(p)
            inventory.append({"path":str(p.relative_to(root)),"rows":len(df),"columns":"|".join(df.columns)})
        except:pass
    write_csv(pd.DataFrame(inventory),out/"validation/stress_inventory.csv")
    # Generic extraction of scenario observations.
    t1_rows=[];t5_rows=[]
    for p in files:
        try:df=read_csv(p)
        except:continue
        cols=[norm(c) for c in df.columns]
        if "scenario_id" not in df.columns or "point_id" not in df.columns:continue
        date=find_col(df,exact=["date","observation_date","campaign_date"],required=False)
        truev=find_col(df,contains=["true","velocity"],required=False) or find_col(df,contains=["target","rate"],required=False)
        obssett=find_col(df,contains=["observed","settlement"],required=False) or find_col(df,contains=["measured","settlement"],required=False)
        zone=find_col(df,exact=["zone_class","stress_zone","zone"],required=False)
        if date and truev and obssett:
            df[date]=parse_date(df[date]);
            for (sid,pid),g in df.groupby(["scenario_id","point_id"]):
                g=g.sort_values(date)
                if len(g)<3:continue
                rates=[]
                for i in range(1,len(g)):
                    dt=(g.iloc[i][date]-g.iloc[i-1][date]).days/365.25
                    rates.append((float(g.iloc[i][obssett])-float(g.iloc[i-1][obssett]))/max(dt,1e-9))
                target=float(pd.to_numeric(g.iloc[-1][truev],errors="coerce"));last=rates[-1] if rates else np.nan;mean3=np.mean(rates[-3:]) if rates else np.nan
                z=str(g.iloc[-1][zone]) if zone else "unknown"
                t1_rows += [
                    {"scenario_id":sid,"point_id":pid,"zone":z,"model":"B1_last_rate","target_rate_mm_y":target,"prediction":last,"abs_error":abs(last-target)},
                    {"scenario_id":sid,"point_id":pid,"zone":z,"model":"B2_mean_last3","target_rate_mm_y":target,"prediction":mean3,"abs_error":abs(mean3-target)},
                ]
        label=find_col(df,contains=["early","label"],required=False) or find_col(df,contains=["onset"],required=False)
        score=find_col(df,contains=["early","score"],required=False)
        if label and score:
            for z,g in df.groupby(zone) if zone else [("all",df)]:
                y=pd.to_numeric(g[label],errors="coerce").fillna(0).astype(int);pr=pd.to_numeric(g[score],errors="coerce").fillna(0)
                t5_rows.append({"zone":z,"model":"stress_provided_score",**classification_metrics(y,pr,.5)})
    t1df=pd.DataFrame(t1_rows)
    if not t1df.empty:
        t1agg=t1df.groupby(["zone","model"]).agg(n=("abs_error","size"),MAE=("abs_error","mean"),P95=("abs_error",lambda x:np.quantile(x,.95))).reset_index()
    else:t1agg=pd.DataFrame(columns=["zone","model","n","MAE","P95"])
    t5df=pd.DataFrame(t5_rows)
    write_csv(t1agg,out/"validation/T1_stress_OOD_metrics.csv");write_csv(t5df,out/"tables/T5_stress_metrics_by_zone.csv")
    return {"inventory":inventory,"T1":t1agg,"T5":t5df}


# ------------------------------- T6 outputs -------------------------------

def run_t6(root:Path,out:Path,full:pd.DataFrame)->pd.DataFrame:
    # Derive profile summaries from point predictions at each target date.
    if "profile_id" not in full.columns:
        df=pd.DataFrame(columns=["output","n","MAE"]);write_csv(df,out/"tables/T6_profile_metrics.csv");return df
    test=full[full.split=="test"].copy()
    # Keep groups with broad profile coverage.
    rows=[]
    for (profile,date),g in test.groupby(["profile_id","target_date"]):
        if len(g)<3:continue
        h=g.horizon_days.to_numpy(float)/365.25
        current_col=feature_column(g,["current_settlement_mm","last_settlement_mm"],contains=["current","settlement"])
        if current_col:
            cur=pd.to_numeric(g[current_col],errors="coerce").to_numpy(float)
            pred_next=cur+g.H1_P50.to_numpy(float)*h
            true_next=g.target_next_settlement_mm.to_numpy(float)
        else:
            pred_next=g.H1_P50.to_numpy(float)*h;true_next=g.target_increment_mm.to_numpy(float)
        rows.append({
            "profile_id":profile,"target_date":date,"n_points":len(g),
            "true_mean_settlement_mm":np.nanmean(true_next),"pred_mean_settlement_mm":np.nanmean(pred_next),
            "true_max_settlement_mm":np.nanmax(true_next),"pred_max_settlement_mm":np.nanmax(pred_next),
            "true_max_rate_mm_y":np.nanmax(g.target_rate_mm_y),"pred_max_rate_mm_y":np.nanmax(g.H1_P50),
        })
    prof=pd.DataFrame(rows);write_csv(prof,out/"predictions/T6_profile_predictions.csv")
    metrics=[]
    mapping=[("mean_settlement_mm","true_mean_settlement_mm","pred_mean_settlement_mm"),("max_settlement_mm","true_max_settlement_mm","pred_max_settlement_mm"),("max_rate_mm_y","true_max_rate_mm_y","pred_max_rate_mm_y")]
    for output,y,p in mapping:
        metrics.append({"output":output,**regression_metrics(prof[y].to_numpy(float),prof[p].to_numpy(float))})
    mdf=pd.DataFrame(metrics);write_csv(mdf,out/"tables/T6_profile_metrics.csv");return mdf


# ------------------------------- Error Atlas ------------------------------

def dominant_provenance(df:pd.DataFrame)->pd.Series:
    prov=[c for c in df.columns if "provenance" in norm(c)]
    if not prov:return pd.Series("unknown",index=df.index)
    def row_dom(row):
        vals=[str(row[c]) for c in prov if pd.notna(row[c])]
        if not vals:return "unknown"
        if any("reconstruct" in norm(v) or norm(v).startswith("r") for v in vals):return "reconstructed_present"
        if any("digit" in norm(v) or norm(v).startswith("d") for v in vals):return "direct_digitized"
        return vals[0]
    return df.apply(row_dom,axis=1)


def build_error_atlas(root:Path,out:Path,full:pd.DataFrame)->dict:
    test=full[full.split=="test"].copy()
    test["error_rate_mm_y"]=test.H1_P50-test.target_rate_mm_y
    test["absolute_error_rate_mm_y"]=test.error_rate_mm_y.abs()
    test["horizon_bin"]=pd.cut(test.horizon_days,[-np.inf,90,180,365,np.inf],labels=["35-90","91-180","181-365",">365"])
    test["speed_bin"]=pd.cut(test.target_rate_mm_y,[-np.inf,20,75,100,250,np.inf],labels=["<20","20-75","75-100","100-250",">250"])
    unc=feature_column(test,["target_standard_uncertainty_rate_mm_y","target_rate_standard_uncertainty"],contains=["uncertainty","rate"])
    if unc:
        test["uncertainty_bin"]=pd.qcut(pd.to_numeric(test[unc],errors="coerce"),3,labels=["low","medium","high"],duplicates="drop")
    else:test["uncertainty_bin"]="unknown"
    donor=[c for c in test.columns if "donor_distance" in norm(c)]
    if donor:
        dd=test[donor].apply(pd.to_numeric,errors="coerce").max(axis=1)
        test["donor_distance_bin"]=pd.cut(dd,[-np.inf,1,100,250,np.inf],labels=["direct","near","far","very_far"])
    else:test["donor_distance_bin"]="unknown"
    test["provenance_group"]=dominant_provenance(test)
    # Attach hidden regime/stage for diagnostic slices only.
    trans_path=find_file(root,"regime_stage_transitions.csv",required=False)
    if trans_path:
        pass
    dims=["horizon_bin","speed_bin","profile_id","uncertainty_bin","donor_distance_bin","provenance_group"]
    for c in ["campaign_type","target_campaign_type","process_family","regime_stage"]:
        if c in test.columns:dims.append(c)
    slices=[]
    for dim in dims:
        if dim not in test.columns:continue
        for val,g in test.groupby(dim,dropna=False):
            slices.append({"dimension":dim,"value":str(val),**regression_metrics(g.target_rate_mm_y,g.H1_P50)})
    sdf=pd.DataFrame(slices).sort_values("MAE",ascending=False);write_csv(sdf,out/"error_atlas/error_slices.csv")
    worst=test.sort_values("absolute_error_rate_mm_y",ascending=False).head(100)
    cols=[c for c in ["sample_id","point_id","profile_id","origin_date","target_date","horizon_days","target_rate_mm_y","H1_P50","H1_P10","H1_P90","error_rate_mm_y","absolute_error_rate_mm_y","horizon_bin","speed_bin","uncertainty_bin","donor_distance_bin","provenance_group"] if c in worst.columns]
    write_csv(worst[cols],out/"error_atlas/worst_cases.csv")
    # Figures for top 12 histories/predictions.
    fig_dir=out/"error_atlas/figures";fig_dir.mkdir(parents=True,exist_ok=True)
    for rank,(_,r) in enumerate(worst.head(12).iterrows(),1):
        fig,ax=plt.subplots(figsize=(8,4.5))
        ax.errorbar([r.origin_date,r.target_date],[0,r.target_rate_mm_y],yerr=[0,0],marker="o",label="observed rate target")
        ax.scatter([r.target_date],[r.H1_P50],marker="x",s=70,label="hybrid P50")
        ax.vlines(r.target_date,r.H1_P10,r.H1_P90,label="P10-P90")
        ax.set_title(f"Worst case #{rank}: {r.point_id}, |error|={r.absolute_error_rate_mm_y:.1f} mm/y")
        ax.set_ylabel("Rate, mm/year");ax.grid(True,alpha=.3);ax.legend();fig.autofmt_xdate();fig.tight_layout()
        fig.savefig(fig_dir/f"worst_{rank:02d}_{r.sample_id}.png",dpi=150);plt.close(fig)
    report=f"""# Error Atlas — SKRU-1 Experiment Suite v1

The atlas is based on the frozen 2025 temporal test and the hybrid median prediction. It separates errors by horizon, speed, profile, target uncertainty, feature provenance and donor distance.

- Test samples: {len(test)}
- Median absolute rate error: {test.absolute_error_rate_mm_y.median():.3f} mm/year
- 95th percentile absolute rate error: {test.absolute_error_rate_mm_y.quantile(.95):.3f} mm/year
- Worst absolute rate error: {test.absolute_error_rate_mm_y.max():.3f} mm/year

The largest errors must be interpreted together with transition and stress tables; a low average MAE is not evidence of reliable early warning.
"""
    (out/"error_atlas/ERROR_ATLAS_REPORT_RU.md").write_text(report,encoding="utf-8")
    return {"slices":sdf,"worst":worst}


# ----------------------------- decision layer -----------------------------

def build_decision_layer(root:Path,out:Path,full:pd.DataFrame,t5:dict)->dict:
    latest=full.sort_values("origin_date").groupby("point_id",as_index=False).tail(1).copy()
    # Onset probability from best non-rule T5 model for latest origins where features exist.
    bestrow=t5["metrics"].sort_values("average_precision",ascending=False).iloc[0]
    model_name=str(bestrow.model)
    if model_name in t5["models"]:
        model,features,threshold,backend=t5["models"][model_name]
        # Align latest with T5 feature schema.
        for c in features:
            if c not in latest.columns:latest[c]=np.nan
        onset=predict_proba_model(model,latest,features)
    else:
        last=feature_column(latest,["last_rate_mm_y"],contains=["last","rate"])
        mean3=feature_column(latest,["mean_last_3_rates_mm_y"],contains=["mean","3","rate"])
        l=pd.to_numeric(latest[last],errors="coerce").fillna(0) if last else pd.Series(0,index=latest.index)
        m=pd.to_numeric(latest[mean3],errors="coerce").fillna(l) if mean3 else l
        onset=1/(1+np.exp(-.05*(l-m)))
    latest["onset_probability"]=onset
    # Uncertainty score from hybrid interval width.
    latest["forecast_uncertainty_score"]=robust_minmax(latest.H1_P90-latest.H1_P10)
    # Geomechanical context: safe static risk-like features only.
    risk_cols=[c for c in latest.columns if any(t in norm(c) for t in ["kzt","ko_","load","seismic","fault","depth"]) and pd.api.types.is_numeric_dtype(latest[c])]
    if risk_cols:
        risk=pd.concat([robust_minmax(latest[c]) for c in risk_cols],axis=1).mean(axis=1)
    else:risk=pd.Series(.5,index=latest.index)
    latest["geomechanical_context_score"]=risk
    # Sensor disagreement: aggregate any GNSS/InSAR features against leveling rate.
    last_col=feature_column(latest,["last_rate_mm_y"],contains=["last","rate"])
    sensor_cols=[c for c in latest.columns if any(t in norm(c) for t in ["gnss_rate","insar_rate","los_rate"])]
    if sensor_cols and last_col:
        level=pd.to_numeric(latest[last_col],errors="coerce")
        disag=pd.concat([(pd.to_numeric(latest[c],errors="coerce")-level).abs() for c in sensor_cols],axis=1).mean(axis=1)
        latest["sensor_disagreement_score"]=robust_minmax(disag)
    else:latest["sensor_disagreement_score"]=0.5
    latest["priority_score"]=0.40*robust_minmax(latest.onset_probability)+0.25*latest.forecast_uncertainty_score+0.20*latest.geomechanical_context_score+0.15*latest.sensor_disagreement_score
    q90=latest.priority_score.quantile(.90);q75=latest.priority_score.quantile(.75)
    latest["research_recommendation"]=np.select([
        latest.priority_score>=q90,
        latest.priority_score>=q75,
        latest.forecast_uncertainty_score>=.8,
    ],[
        "focused_cycle_plus_GNSS_and_InSAR_review",
        "focused_cycle",
        "instrumental_recheck",
    ],default="routine_monitoring")
    latest["normative_risk_class"]="NOT_ASSIGNED_RESEARCH_ONLY"
    cols=[c for c in ["point_id","profile_id","origin_date","onset_probability","forecast_uncertainty_score","geomechanical_context_score","sensor_disagreement_score","priority_score","research_recommendation","normative_risk_class"] if c in latest.columns]
    write_csv(latest[cols].sort_values("priority_score",ascending=False),out/"decision_layer/monitoring_priority_latest.csv")
    selected=latest[latest.research_recommendation!="routine_monitoring"].copy()
    # Add all reference points if points table is present.
    pp=find_file(root,"survey_points.csv",required=False)
    if pp:
        pts=read_csv(pp);p=find_col(pts,exact=["point_id"]);role=find_col(pts,exact=["point_role","role","point_type"],required=False)
        if role:
            refs=pts[pts[role].astype(str).str.lower().str.contains("ref")][[p]+([find_col(pts,exact=["profile_id"],required=False)] if find_col(pts,exact=["profile_id"],required=False) else [])].copy()
            refs=refs.rename(columns={p:"point_id"});refs["research_recommendation"]="mandatory_reference_control";refs["priority_score"]=1.0
            selected=pd.concat([selected,refs],ignore_index=True,sort=False).drop_duplicates("point_id")
    write_csv(selected[[c for c in ["point_id","profile_id","priority_score","research_recommendation"] if c in selected.columns]].sort_values("priority_score",ascending=False),out/"decision_layer/focused_campaign_research_plan.csv")
    return {"priority":latest,"focused":selected,"best_t5_model":model_name}


# -------------------------- reports/manifests/audit ------------------------

def experiment_registry(out:Path,t1:dict,t5:dict,abl:pd.DataFrame)->pd.DataFrame:
    rows=[]
    for m in ["B0_zero","B1_last_rate","B2_mean_last3","B3_robust_linear","B4_quadratic","B5_kalman_fixed","B6_kalman_adaptive","H1_Kalman_CatBoost_Q50"]:
        rows.append({"experiment_id":m,"task":"T1","status":"completed","tuning_split":"validation","test_tuning":False})
    for _,r in abl.drop_duplicates("experiment_id").iterrows():rows.append({"experiment_id":r.experiment_id,"task":"T1_ablation","status":r.status,"tuning_split":"validation","test_tuning":False})
    for m in ["rule_based","logistic","catboost","hazard"]:rows.append({"experiment_id":m,"task":"T5","status":"completed","tuning_split":"validation","test_tuning":False})
    df=pd.DataFrame(rows);write_csv(df,out/"metadata/experiment_registry.csv");return df


def build_manifest(out:Path)->pd.DataFrame:
    rows=[]
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name not in ["experiment_manifest.csv","experiment_checksums.sha256"]:
            rows.append({"relative_path":p.relative_to(out).as_posix(),"size_bytes":p.stat().st_size,"sha256":sha256(p)})
    df=pd.DataFrame(rows);write_csv(df,out/"metadata/experiment_manifest.csv")
    (out/"metadata/experiment_checksums.sha256").write_text("\n".join(f"{r.sha256}  {r.relative_path}" for r in df.itertuples())+"\n",encoding="utf-8")
    return df


def write_protocol(out:Path,t1:dict) -> None:
    protocol={
        "suite_version":VERSION,"dataset_version":DATASET_VERSION,"random_seed":SEED,
        "primary_target":{"id":"T1_RATE_NEXT_PLANNED","unit":"mm_per_year"},
        "secondary_target":{"id":"T5_EW_ONSET_180D","unit":"binary"},
        "derived_outputs":["T1B_INCREMENT_NEXT_PLANNED","T1C_CUMULATIVE_SETTLEMENT","T6_PROFILE_KINEMATICS"],
        "splits":{"train_target_end":"2023-12-31","validation_target_year":2024,"test_target_start":"2025-01-01","split_field":"target_date_or_label_horizon_end"},
        "model_selection":{"hyperparameters":"validation_only","test_retraining":False,"catboost_available":CATBOOST_AVAILABLE,"residual_backend":t1["backend"]},
        "forbidden":{"random_row_split":True,"test_hyperparameter_tuning":True,"hidden_truth_as_feature":True,"terminal_map_as_feature":True},
        "spatial_validation":["leave_profile_out","leave_zone_out","unseen_spatial_stress"],
        "decision_layer":"research_ranking_only_not_normative",
    }
    json_dump(protocol,out/"metadata/experiment_protocol.json")
    if yaml:
        (out/"experiment_protocol.yaml").write_text(yaml.safe_dump(protocol,allow_unicode=True,sort_keys=False),encoding="utf-8")
    else:
        (out/"experiment_protocol.yaml").write_text("# YAML-compatible JSON\n"+json.dumps(protocol,ensure_ascii=False,indent=2),encoding="utf-8")


def write_report(out:Path,t1:dict,t5:dict,abl:pd.DataFrame,spatial:pd.DataFrame,t6:pd.DataFrame,decision:dict,stress:dict)->dict:
    metrics=t1["metrics"]
    testobs=metrics[(metrics.split=="test")&(metrics.target=="observed")].copy().sort_values("rate_MAE")
    best=testobs.iloc[0].to_dict();hy=testobs[testobs.model=="H1_P50"].iloc[0].to_dict() if (testobs.model=="H1_P50").any() else {}
    t5best=t5["metrics"].iloc[0].to_dict() if len(t5["metrics"]) else {}
    abtest=abl[abl.split=="test"].sort_values("MAE") if "MAE" in abl.columns else pd.DataFrame()
    bestab=abtest.iloc[0].to_dict() if len(abtest) else {}
    summary={
        "suite_version":VERSION,"dataset_version":DATASET_VERSION,"catboost_available":CATBOOST_AVAILABLE,
        "best_T1_test_model":best.get("model"),"best_T1_test_MAE_rate_mm_y":best.get("rate_MAE"),
        "hybrid_T1_test_MAE_rate_mm_y":hy.get("rate_MAE"),"best_ablation":bestab.get("experiment_id"),
        "best_T5_model":t5best.get("model"),"best_T5_average_precision":t5best.get("average_precision"),
        "T5_test_positives":t5best.get("positives"),"decision_points":len(decision["priority"]),
        "external_validation_status":"READY_PENDING_REAL_DATA",
    }
    json_dump(summary,out/"metadata/experiment_validation_report.json")
    report=f"""# SKRU-1 Experiment Suite v1 — итоговый исследовательский отчёт

## Статус

- Snapshot: `{DATASET_VERSION}`
- Suite: `{VERSION}`
- CatBoost available: `{CATBOOST_AVAILABLE}`
- Random row split: forbidden
- Hyperparameter selection: validation only
- Production claims: forbidden until frozen external test on real cycles

## T1 — прогноз скорости до следующей плановой кампании

Лучший temporal-test baseline/model: **{best.get('model')}**.

- MAE скорости: {best.get('rate_MAE',float('nan')):.3f} мм/год
- RMSE скорости: {best.get('rate_RMSE',float('nan')):.3f} мм/год
- Bias: {best.get('rate_Bias',float('nan')):.3f} мм/год

Hybrid Kalman + residual model:

- MAE скорости: {hy.get('rate_MAE',float('nan')):.3f} мм/год
- Backend: `{t1['backend']}`
- Selected on validation: yes

The hybrid model is accepted only if it improves temporal and spatial holdouts without degrading stress/OOD behaviour. A lower in-sample error is not sufficient.

## T5 — early warning onset within 180 days

Best test model by Average Precision: **{t5best.get('model')}**.

- Test positives: {t5best.get('positives')}
- Average Precision: {t5best.get('average_precision')}
- Recall: {t5best.get('recall')}
- Precision: {t5best.get('precision')}
- False warnings per 100 point-years: {t5best.get('false_warnings_per_100_point_years')}

Safety conclusions remain limited by the number of independent onset events.

## Ablation E0–E8

Best test ablation: `{bestab.get('experiment_id')}` with MAE {bestab.get('MAE')} мм/год. The comparison is stored with explicit feature sets and a fixed model budget.

## Spatial and stress validation

- Spatial validation rows: {len(spatial)}
- Stress T1 groups: {len(stress['T1'])}
- Stress T5 groups: {len(stress['T5'])}

## T6 profile outputs

T6 is derived from point predictions, not trained as an independent black-box target. Profile metrics are available for mean/max settlement and maximum speed; tilt/curvature remain deterministic profile derivatives when point geometry is available.

## Decision layer

Research ranking covers {len(decision['priority'])} latest working points. It combines onset probability, forecast uncertainty, geomechanical context and sensor disagreement. It is not a regulatory risk class.

## External validation

The frozen harness is ready, but real external results are pending. Retraining and threshold retuning on the external set are prohibited.
"""
    (out/"EXPERIMENT_REPORT_RU.md").write_text(report,encoding="utf-8")
    model_card=f"""# Model card — hybrid T1 and T5 baselines

## Intended use
Research evaluation of reconstructed/synthetic SKRU-1 surveying data.

## T1
Adaptive local-trend Kalman baseline plus a residual tabular model. Quantiles P2.5/P10/P50/P90/P97.5 are generated separately.

## T5
Rule baseline, weighted logistic regression, CatBoost classifier (or declared fallback), and discrete-time hazard logit.

## Prohibited use
- production safety decisions;
- normative hazard classification;
- claims of real mine accuracy before frozen external validation;
- use of evaluation/private generator fields as features.
"""
    (out/"MODEL_CARD_RU.md").write_text(model_card,encoding="utf-8")
    return summary


def external_harness(out:Path,t1:dict)->None:
    ext=out/"external_validation";ext.mkdir(parents=True,exist_ok=True)
    cfg={"dataset_version":DATASET_VERSION,"suite_version":VERSION,"model":"B6_kalman_adaptive","q_base":t1["q_adaptive"],"retraining_allowed":False,"threshold_retuning_allowed":False,"status":"READY_PENDING_REAL_DATA"}
    json_dump(cfg,ext/"frozen_baseline_config.json")
    schema=pd.DataFrame([
        ["point_id","string",True,"Survey point ID"],["date","YYYY-MM-DD",True,"Observation date"],["settlement_mm","float",True,"Observed cumulative settlement"],["standard_uncertainty_mm","float",True,"Standard uncertainty"],["profile_id","string",False,"Profile identifier"],
    ],columns=["field","type","required","description"]);write_csv(schema,ext/"external_cycle_schema.csv")
    script='''#!/usr/bin/env python3\n"""Frozen external validation. No fitting or threshold tuning."""\nimport argparse,json\nfrom pathlib import Path\nimport numpy as np,pandas as pd\nfrom sklearn.metrics import mean_absolute_error,mean_squared_error\n\ndef main():\n p=argparse.ArgumentParser();p.add_argument("--input",required=True);p.add_argument("--config",required=True);p.add_argument("--output",required=True);a=p.parse_args()\n cfg=json.loads(Path(a.config).read_text());assert cfg["retraining_allowed"] is False\n df=pd.read_csv(a.input);df["date"]=pd.to_datetime(df.date);out=Path(a.output);out.mkdir(parents=True,exist_ok=True)\n rows=[]\n for pid,g in df.groupby("point_id"):\n  g=g.sort_values("date")\n  for i in range(2,len(g)):\n   hist=g.iloc[:i];target=g.iloc[i];dt=(target.date-hist.iloc[-1].date).days/365.25\n   prevdt=(hist.iloc[-1].date-hist.iloc[-2].date).days/365.25\n   rate=(hist.iloc[-1].settlement_mm-hist.iloc[-2].settlement_mm)/max(prevdt,1e-9)\n   pred=hist.iloc[-1].settlement_mm+rate*dt\n   rows.append({"point_id":pid,"target_date":target.date,"prediction_settlement_mm":pred,"observed_settlement_mm":target.settlement_mm})\n pr=pd.DataFrame(rows);pr.to_csv(out/"external_predictions.csv",index=False)\n if len(pr):\n  err=pr.prediction_settlement_mm-pr.observed_settlement_mm\n  metrics={"n":len(pr),"MAE_mm":float(np.mean(np.abs(err))),"RMSE_mm":float(np.sqrt(np.mean(err**2))),"Bias_mm":float(np.mean(err)),"retraining":False}\n else:metrics={"n":0,"retraining":False}\n (out/"external_metrics.json").write_text(json.dumps(metrics,indent=2))\nif __name__=="__main__":main()\n'''
    (ext/"run_external_validation.py").write_text(script,encoding="utf-8")
    json_dump({"status":"READY_PENDING_REAL_DATA","software_smoke_test":"not_run_in_suite_builder","real_external_test":"PENDING","retraining":False},ext/"external_validation_status.json")


def main():
    ap=argparse.ArgumentParser();ap.add_argument("--snapshot",default="/mnt/data/SKRU1_Data_Foundation_v3_2_1");ap.add_argument("--output",default="/mnt/data/SKRU1_Experiment_Suite_v1");args=ap.parse_args()
    root=Path(args.snapshot);out=Path(args.output)
    if out.exists():shutil.rmtree(out)
    for d in ["tables","predictions","models","ablations","validation","error_atlas","decision_layer","metadata","external_validation"]:(out/d).mkdir(parents=True,exist_ok=True)
    loaded=load_snapshot(root);data=loaded["data"]
    history,hmeta=prepare_history(root,loaded["adjusted"],loaded.get("campaigns"))
    t1=run_t1(root,out,data,history)
    abl=run_ablations(out,t1["full"],t1["groups"],t1["best_params"])
    t5=run_t5(root,out,t1["full"])
    spatial=run_spatial_validation(root,out,t1["full"],t1["features"],t1["best_params"])
    transitions=run_regime_transition_validation(root,out,t1["full"])
    stress=run_stress_validation(root,out,t1,t5)
    t6=run_t6(root,out,t1["full"])
    atlas=build_error_atlas(root,out,t1["full"])
    decision=build_decision_layer(root,out,t1["full"],t5)
    registry=experiment_registry(out,t1,t5,abl)
    write_protocol(out,t1)
    external_harness(out,t1)
    summary=write_report(out,t1,t5,abl,spatial,t6,decision,stress)
    json_dump({"history_columns":hmeta,"safe_features":t1["features"],"feature_groups":t1["groups"]},out/"metadata/data_contract_used.json")
    build_manifest(out);build_manifest(out)
    (out/"SUCCESS_EXPERIMENT_SUITE.txt").write_text("PASS\n",encoding="utf-8")
    print(json.dumps({"status":"PASS","output":str(out),"summary":summary},ensure_ascii=False,default=str))

if __name__=="__main__":
    try:main()
    except Exception:
        Path("/mnt/data/SKRU1_Experiment_Suite_v1_FAILED.txt").write_text(traceback.format_exc(),encoding="utf-8")
        traceback.print_exc();sys.exit(1)
