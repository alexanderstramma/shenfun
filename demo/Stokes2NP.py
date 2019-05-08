r"""Solve Stokes equations using a coupled formulation

The Stokes equations are in strong form

.. math::

    -\nabla^2 u - \nabla p &= f \\
    \nabla \cdot u &= h \\
    u(x, y=\pm 1) &= 0 \\
    u(x=\pm 1, y) &= 0

where :math:`f` and :math:`h` are given functions of space.
In addition we require :math:`\int p d\ = 0`, which is achieved by
fixing the coefficient :math:`\hat{p}_{0, 0} = 0`.

We use a tensorproductspace with a composite Legendre for the Dirichlet space
and a regular Legendre for the pressure space.

To remove all nullspaces we use a P_{N} x P_{N-2} basis, with P_{N-2} for the
pressure.

"""
import os
import numpy as np
from mpi4py import MPI
from sympy import symbols, sin, cos, lambdify
from shenfun import *

comm = MPI.COMM_WORLD
x, y = symbols("x,y")

assert comm.Get_size() == 1, "Two non-periodic directions only have solver implemented for serial"

# Some right hand side (manufactured solution)
uex = (cos(4*np.pi*x)+sin(2*np.pi*y))*(1-y**2)*(1-x**2)
uey = (sin(2*np.pi*x)+cos(6*np.pi*y))*(1-y**2)*(1-x**2)
pe = -0.1*sin(2*x)*sin(4*y)
fx = -uex.diff(x, 2) - uex.diff(y, 2) - pe.diff(x, 1)
fy = -uey.diff(x, 2) - uey.diff(y, 2) - pe.diff(y, 1)
h = uex.diff(x, 1) + uey.diff(y, 1)

# Lambdify for faster evaluation
ulx = lambdify((x, y), uex, 'numpy')
uly = lambdify((x, y), uey, 'numpy')
flx = lambdify((x, y), fx, 'numpy')
fly = lambdify((x, y), fy, 'numpy')
hl = lambdify((x, y), h, 'numpy')
pl = lambdify((x, y), pe, 'numpy')

N = (40, 40)
family = 'Chebyshev'
SD0 = Basis(N[0], family, bc=(0, 0))
SD1 = Basis(N[1], family, bc=(0, 0))
ST0 = Basis(N[0], family)
ST1 = Basis(N[1], family)

# To get a P_N x P_{N-2} space, just pick the first N-2 items of the pressure basis
# Note that this effectively sets P_N and P_{N-1} to zero, but still the basis uses
# the same quadrature points as the Dirichlet basis, which is required for the inner
# products.
ST0.slice = lambda: slice(0, ST0.N-2)
ST1.slice = lambda: slice(0, ST1.N-2)

TD = TensorProductSpace(comm, (SD0, SD1))
TT = TensorProductSpace(comm, (ST0, ST1))
VT = VectorTensorProductSpace(TD)
Q = MixedTensorProductSpace([VT, TT])
X = TD.local_mesh(True)

up = TrialFunction(Q)
vq = TestFunction(Q)

u, p = up
v, q = vq

# Assemble blocks of the complete block matrix
if family.lower() == 'legendre':
    A00 = inner(grad(v), grad(u))
    A01 = inner(div(v), p)
else:
    A00 = inner(v, -div(grad(u)))
    A01 = inner(v, -grad(p))

A10 = inner(q, div(u))

# Create Block matrix
M = BlockMatrix(A00+A01+A10)

# Assemble right hand side
fh = Array(Q)
f_, h_ = fh
f_[0] = flx(*X)
f_[1] = fly(*X)
h_[:] = hl(*X)
fh_hat = Function(Q)
f_hat, h_hat = fh_hat
f_hat = inner(v, f_, output_array=f_hat)
h_hat = inner(q, h_, output_array=h_hat)

# Solve problem
uh_hat = Function(Q)
uh_hat = M.solve(fh_hat, u=uh_hat, integral_constraint=(2, 0)) # Constraint for component 2 of mixed space

# Move solution to regular Function
up = uh_hat.backward()
u_, p_ = up

# Exact solution
ux = ulx(*X)
uy = uly(*X)
pe = pl(*X)

# Compute error
error = [comm.reduce(np.linalg.norm(ux-u_[0])),
         comm.reduce(np.linalg.norm(uy-u_[1])),
         comm.reduce(np.linalg.norm(pe-p_))]

if comm.Get_rank() == 0:
    print('Error    u          v          p')
    print('     %2.4e %2.4e %2.4e' %(error[0], error[1], error[2]))
    #assert np.all(abs(np.array(error)) < 1e-7), error

if 'pytest' not in os.environ:
    import matplotlib.pyplot as plt
    plt.figure()
    plt.contourf(X[0], X[1], p_, 100)
    plt.figure()
    plt.quiver(X[0], X[1], u_[0], u_[1])
    plt.figure()
    plt.spy(M.diags())
    plt.figure()
    plt.contourf(X[0], X[1], u_[0], 100)
    plt.show()
