"""Elastic-only MAPDL prism with six nodes and three displacement DOFs per node.

The eight integration points are inherited from the collapsed HEX8 parent.
This is a separately named representation, not a change to default WEDGE6.
Do not use a six-point material-memory checkpoint with this eight-point rule.
Successful CPU assembly does not imply admission by native element-shape checks.
"""
import numpy as np
from vkm_world.representations.world0.multiseam import ElasticMesh,_quadrature,strain_matrix

COLLAPSE=np.array([0,1,2,2,3,4,5,5])

def native_prism_quadrature():
    """Merged HEX8 derivatives and shape values in parent coordinates; 8 points."""
    for grad8,shape8,weight in _quadrature(8):
        grad=np.zeros((6,3));shape=np.zeros(6)
        np.add.at(grad,COLLAPSE,grad8);np.add.at(shape,COLLAPSE,shape8)
        yield grad,shape,weight

def native_prism_element_data(coordinates,material):
    coordinates=np.asarray(coordinates,float)
    if coordinates.ndim!=3 or coordinates.shape[1:]!=(6,3):raise ValueError('six-node prism coordinates required')
    k=np.zeros((len(coordinates),18,18));ib=np.zeros((len(coordinates),6,18));vol=np.zeros(len(coordinates))
    for grad,shape,weight in native_prism_quadrature():
        b,det=strain_matrix(coordinates,grad);dv=det*weight
        k+=(b.swapaxes(1,2)@material@b)*dv[:,None,None];ib+=b*dv[:,None,None];vol+=dv
    return (k+k.swapaxes(1,2))/2,vol,ib/vol[:,None,None],(ib.swapaxes(1,2)@material[:,:,2,None])[:,:,0]

class NativePrismElasticMesh(ElasticMesh):
    """Independent CPU elastic assembly matching the tested native prism rule.

    Existing default prism validation/assembly is reused before replacing the
    elastic matrices. No material history API is provided by this subclass.
    """
    representation_rule='collapsed_HEX8_full_8GP_prism6_ELASTIC_ONLY'
    def __init__(self,xyz_m,connectivity,young_pa,poisson,fixed_nodes):
        if np.asarray(connectivity).shape[1]!=6:raise ValueError('prism6 required')
        super().__init__(xyz_m,connectivity,young_pa,poisson,fixed_nodes,volumetric_average=False)
        for first in range(0,len(self.cells),1024):
            take=slice(first,first+1024)
            self.ke[take],self.volume_m3[take],self.average_b[take],self.unit_force[take]=native_prism_element_data(self.xyz[self.cells[take]],self.D[take])
        self.matrix=self.assemble(self.ke);self._factor=None

    def body_force_rhs(self,density_kg_m3,acceleration_m_s2):
        rho=np.broadcast_to(np.asarray(density_kg_m3,float),(len(self.cells),));a=np.asarray(acceleration_m_s2,float)
        if a.shape!=(3,) or not np.isfinite(a).all() or not np.isfinite(rho).all() or (rho<=0).any():raise ValueError('invalid density or acceleration')
        mass=np.zeros(len(self.xyz))
        for first in range(0,len(self.cells),2048):
            take=slice(first,first+2048);cells=self.cells[take];coords=self.xyz[cells]
            for grad,shape,weight in native_prism_quadrature():
                det=np.linalg.det(coords.swapaxes(1,2)@grad)
                if (det<=0).any():raise ValueError('nonpositive Jacobian')
                np.add.at(mass,cells.ravel(),(rho[take,None]*det[:,None]*weight*shape).ravel())
        return (mass[:,None]*a).ravel()
