"""Synthetic cube and explicit conditional bundles; not a mine specimen."""
import numpy as np
from vkm_world.representations.world0.constitutive import Nishihara, Lubby2, AbelSOE
from vkm_world.representations.world0.tl_core import TetMesh

E0 = 1.5e9
NU = .3
RHO0 = 2300.
K0 = E0 / (3 * (1 - 2 * NU))
G0 = E0 / (2 * (1 + NU))
DAY = 86400.


def laws():
    return {
        'Nishihara': Nishihara(K0, G0, .5*K0, .5*G0, .5*K0*31557600.,
                              .5*G0*31557600., 1.262304e15, 3e6),
        'Lubby2': Lubby2(K0, G0, .7*G0, .7*G0*63115200., G0*473364000.,
                        -2e-8, -3e-8, 0.),
        'AbelSOE': AbelSOE(K0, G0, .6, .00004, .6, .0002, DAY, 6311520000.,128)
    }


def cube():
    x = np.array([[0,0,0],[1,0,0],[1,1,0],[0,1,0],
                  [0,0,1],[1,0,1],[1,1,1],[0,1,1]], dtype=float)
    t = np.array([[0,1,2,6],[0,2,3,6],[0,3,7,6],
                  [0,7,4,6],[0,4,5,6],[0,5,1,6]])
    return TetMesh(x, t, RHO0)


def stretch_bc(mesh, stretch):
    fixed = np.r_[3*np.arange(len(mesh.nodes)), 1, 2, 11]
    values = np.r_[(stretch-1)*mesh.nodes[:,0], 0., 0., 0.]
    return fixed, values
