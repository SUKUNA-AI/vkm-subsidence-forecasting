"""Same grouped WEDGE assembly and TL Newton; changed material time scheme only."""
import numpy as np
from vkm_world.representations.world0.wedge_adapter import Group,GroupedLaw,ReferenceElastic,FiniteState,ReferentialLubby,DeviatoricMaxwell
from vkm_world.representations.world0.wedge_adapter import WedgeModel
from vkm_world.representations.world0.midpoint import MidpointLubby,MidpointMaxwell,transfer_point

class MidpointWedgeModel(WedgeModel):
 def work_audit(self,old,step,external_force):
  self.validate_state(old);self.validate_state(step.state);f=np.asarray(external_force,float)
  if f.shape!=old.u.shape or not np.isfinite(f).all():raise ValueError('force shape/finite')
  du=step.state.u-old.u;w=self.mesh.measures;F0=self.mesh.deformation_gradient(old.u);dF=step.response.F-F0
  from vkm_world.representations.world0.tl_core import MANDEL
  dS=np.einsum('...a,aij->...ij',step.state.material.stress-old.material.stress,MANDEL)
  G=float(.25*np.sum(w*np.einsum('egij,egki,egkj->eg',dS,dF,dF)))
  delta=float(np.sum(w*(self.law.energy(step.state.material)-self.law.energy(old.material))))
  D=float(np.sum(w*step.response.material_trial.diagnostics['branch_viscous_dissipation']))
  f0,_,_=self.assemble(old,old.u,0.);r0=f0-f;r1=step.reactions
  correction=-.5*float(np.sum((r0+r1)*du));W=float(np.sum(f*du))
  values=dict(external_work_J=W,stored_energy_change_J=delta,physical_branch_dissipation_J=D,geometric_endpoint_defect_J=G,equilibrium_work_residual_J=correction,physical_firstlaw_defect_J=W-delta-D,algebraic_residual_J=W-delta-D-G-correction,elastic_endpoint_defect_J=0.)
  if not all(np.isfinite(v) for v in values.values()):raise ValueError('nonfinite finite work audit')
  values['scope']='fixed activity/deadload; midpoint D quadrature; signed G plus -0.5(r0+r1)du, not exact continuous first law'
  return values

def transfer_grouped(model,state):
 model.validate_state(state);groups=[];parts=[]
 for i,g in enumerate(model.law.groups):
  source=g.law;part=model.law._extract(state.material,i)
  if isinstance(source,ReferentialLubby):target=MidpointLubby(source.inner,source.reference_strain);new=transfer_point(source,target,part)
  elif isinstance(source,DeviatoricMaxwell):target=MidpointMaxwell(source.K0,source.G0,source.eta_shear);new=transfer_point(source,target,part)
  elif isinstance(source,ReferenceElastic):target=source;new=part
  else:raise ValueError('unsupported group')
  groups.append(Group(g.point_ids,target));parts.append(new)
 law=GroupedLaw(tuple(groups),model.law.point_shape);out=MidpointWedgeModel(model.mesh,law);newstate=FiniteState(state.u,law._join(parts),out.signature);out.validate_state(newstate)
 if not np.array_equal(model.law.energy(state.material),law.energy(newstate.material)):raise ValueError('transfer changed stored energy')
 return out,newstate
