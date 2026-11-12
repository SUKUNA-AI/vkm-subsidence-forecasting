"""Matrix-free two-stage Gauss4 in mass-scaled acceleration coordinates."""
import numpy as np
from scipy.sparse.linalg import LinearOperator,gmres
A2=np.array([[.25,.25-np.sqrt(3)/6],[.25+np.sqrt(3)/6,.25]])
C2=np.array([.5-np.sqrt(3)/6,.5+np.sqrt(3)/6]);AA=A2@A2
def ramp(t):
    if t<=0:return 0.,0.
    if t>=1:return 1.,0.
    return t**3*(10+t*(-15+6*t)),30*t*t*(1-t)**2
def step(t,h,x,v,M,dm,A,dK,C,r0,release,*,constant_post=False,warm=None):
    n=len(M);s=1/np.sqrt(M-dm);alph=np.array([1. if constant_post else ramp(t+c*h)[0] for c in C2]);ms=(M[None,:]-alph[:,None]*dm)*s*s
    def kmul(i,y):return A@y+(1-alph[i])*(dK@y)
    rhs=np.stack([s*(r0+alph[i]*release-kmul(i,x+C2[i]*h*v)-C@v) for i in range(2)])
    def action(y):
        y=y.reshape(2,n);cy=A2@y;ky=AA@y
        return np.stack([ms[i]*y[i]+h*s*(C@(s*cy[i]))+h*h*s*kmul(i,s*ky[i]) for i in range(2)]).ravel()
    diagK=np.stack([A.diagonal()+(1-alph[i])*dK.diagonal() for i in range(2)])*s*s;diagC=C.diagonal()*s*s
    block=np.empty((2,2,n))
    for i in range(2):
        for j in range(2):block[i,j]=(ms[i] if i==j else 0)+h*A2[i,j]*diagC+h*h*AA[i,j]*diagK[i]
    det=block[0,0]*block[1,1]-block[0,1]*block[1,0]
    if np.any(det<=0):raise ValueError('invalid stage block preconditioner')
    def pre(y):
        y=y.reshape(2,n)
        return np.stack([(block[1,1]*y[0]-block[0,1]*y[1])/det,(-block[1,0]*y[0]+block[0,0]*y[1])/det]).ravel()
    calls=[];operator=LinearOperator((2*n,2*n),matvec=action,dtype=float);P=LinearOperator(operator.shape,matvec=pre,dtype=float)
    answer,info=gmres(operator,rhs.ravel(),x0=warm,M=P,rtol=1e-11,atol=1e-10,restart=40,maxiter=20,callback=lambda r:calls.append(float(r)),callback_type='pr_norm')
    residual=action(answer)-rhs.ravel();physical=residual.reshape(2,n)/s
    relative=float(np.linalg.norm(physical)/max(np.linalg.norm(rhs/s),1.))
    if info or relative>1e-8:raise RuntimeError(('stage failed',info,relative,len(calls)))
    acc=answer.reshape(2,n)*s;sx=x+C2[:,None]*h*v+h*h*(AA@acc);sv=v+h*(A2@acc)
    xn=x+h*v+h*h*(np.array([.5,.5])@A2)@acc;vn=v+h*acc.mean(axis=0)
    return xn,vn,sx,sv,answer,dict(iterations=len(calls),physical_relative_residual=relative)
