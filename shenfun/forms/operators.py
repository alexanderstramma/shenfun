"""
This module contains the implementation of operators acting on arguments.
"""
import numpy as np
import sympy as sp
import copy
from .arguments import Expr, BasisFunction

__all__ = ('div', 'grad', 'Dx', 'curl')

#pylint: disable=protected-access

def div(test):
    """Return div(test)

    Parameters
    ----------
    test:  Expr or BasisFunction
           Must be rank > 0 (cannot take divergence of scalar)

    """
    assert isinstance(test, (Expr, BasisFunction))

    if isinstance(test, BasisFunction):
        test = Expr(test)

    test = copy.copy(test)
    ndim = test.dimensions
    coors = test.function_space().coors

    if coors.is_cartesian:
        # Cartesian
        if ndim == 1:      # 1D
            v = np.array(test.terms())
            v += 1
            test._terms = v.tolist()
            return test

        else:
            d = Dx(test[0], 0, 1)
            for i in range(1, ndim):
                d += Dx(test[i], i, 1)
            return d

    else:
        assert test.expr_rank() < 2, 'Cannot (yet) take divergence of higher order tensor in curvilinear coordinates'

        # Each term in test will lead to at least one, but possibly 2 new terms
        # Collect a list of new terms that are nonzero and ine the end put together
        # new array of terms
        sg = coors.get_sqrt_g()
        d = Dx(test[0]*sg, 0, 1)*(1/sg)
        for i in range(1, ndim):
            d += Dx(test[i]*sg, i, 1)*(1/sg)
        return d

def grad(test):
    """Return grad(test)

    Parameters
    ----------
    test: Expr or BasisFunction
    """
    assert isinstance(test, (Expr, BasisFunction))

    if isinstance(test, BasisFunction):
        test = Expr(test)

    test = copy.copy(test)
    ndim = test.dimensions
    coors = test.function_space().coors

    if coors.is_cartesian:

        d = [Dx(test, 0, 1)]
        for i in range(1, ndim):
            d.append(Dx(test, i, 1))

        terms, scales, indices = [], [], []
        for i in range(ndim):
            terms += d[i]._terms
            scales += d[i]._scales
            indices += d[i]._indices
        test._terms = terms
        test._scales = scales
        test._indices = indices

        return test

    else:
        assert test.expr_rank() < 2, 'Cannot (yet) take gradient of higher order tensor in curvilinear coordinates'

        gt = coors.get_contravariant_metric_tensor()
        d = []
        for i in range(ndim):
            di = Dx(test, 0, 1)*gt[0, i]
            for j in range(1, ndim):
                di += Dx(test, j, 1)*gt[j, i]
            d.append(di)

        terms, scales, indices = [], [], []
        for i in range(ndim):
            terms += d[i]._terms
            scales += d[i]._scales
            indices += d[i]._indices

        test._terms = terms
        test._scales = scales
        test._indices = indices

    return test

def Dx(test, x, k=1):
    """Return k'th order partial derivative in direction x

    Parameters
    ----------
    test: Expr or BasisFunction
    x:  int
        axis to take derivative over
    k:  int
        Number of derivatives
    """
    assert isinstance(test, (Expr, BasisFunction))

    if isinstance(test, BasisFunction):
        test = Expr(test)

    test = copy.copy(test)
    coors = test.function_space().coors

    if coors.is_cartesian:
        v = np.array(test.terms())
        v[..., x] += k
        test._terms = v.tolist()

    else:
        assert test.expr_rank() < 1, 'Cannot (yet) take derivative of tensor in curvilinear coordinates'
        psi = coors.coordinates[0]
        v = copy.deepcopy(test.terms())
        sc = copy.deepcopy(test.scales())
        ind = copy.deepcopy(test.indices())
        num_terms = test.num_terms()
        num_comp = test.num_components()
        for i in range(num_comp):
            for j in range(num_terms[i]):
                sc0 = sp.simplify(sp.diff(sc[i][j], psi[x], 1))
                if not sc0 == 0:
                    v[i].append(copy.deepcopy(v[i][j]))
                    sc[i].append(sc0)
                    ind[i].append(ind[i][j])
                v[i][j][x] += 1
        test._terms = v
        test._scales = sc
        test._indices = ind

    return test


def curl(test):
    """Return curl of test

    Parameters
    ----------
    test: Expr or BasisFunction

    """
    assert isinstance(test, (Expr, BasisFunction))

    if isinstance(test, BasisFunction):
        test = Expr(test)

    test = copy.copy(test)

    assert test.expr_rank() > 0
    assert test.num_components() == test.dimensions  # vector

    coors = test.function_space().coors
    hi = coors.hi

    # Note - need to make curvilinear in terms of covariant vector

    if coors.is_cartesian:
        if test.dimensions == 3:
            w0 = Dx(test[2], 1, 1) - Dx(test[1], 2, 1)
            w1 = Dx(test[0], 2, 1) - Dx(test[2], 0, 1)
            w2 = Dx(test[1], 0, 1) - Dx(test[0], 1, 1)
            test._terms = w0.terms()+w1.terms()+w2.terms()
            test._scales = w0.scales()+w1.scales()+w2.scales()
            test._indices = w0.indices()+w1.indices()+w2.indices()
        else:
            assert test.dimensions == 2
            test = Dx(test[1], 0, 1) - Dx(test[0], 1, 1)

    else:
        assert test.expr_rank() < 2, 'Cannot (yet) take curl of higher order tensor in curvilinear coordinates'
        psi = coors.psi
        sg = coors.get_sqrt_g()
        if coors.is_orthogonal:
            if test.dimensions == 3:
                w0 = (hi[2]**2*Dx(test[2], 1, 1) + test[2]*sp.diff(hi[2]**2, psi[1], 1) - hi[1]**2*Dx(test[1], 2, 1) - test[1]*sp.diff(hi[1]**2, psi[2], 1))/sg
                w1 = (hi[0]**2*Dx(test[0], 2, 1) + test[0]*sp.diff(hi[0]**2, psi[2], 1) - hi[2]**2*Dx(test[2], 0, 1) - test[2]*sp.diff(hi[2]**2, psi[0], 1))/sg
                w2 = (hi[1]**2*Dx(test[1], 0, 1) + test[1]*sp.diff(hi[1]**2, psi[0], 1) - hi[0]**2*Dx(test[0], 1, 1) - test[0]*sp.diff(hi[0]**2, psi[1], 1))/sg
                test._terms = w0.terms()+w1.terms()+w2.terms()
                test._scales = w0.scales()+w1.scales()+w2.scales()
                test._indices = w0.indices()+w1.indices()+w2.indices()

            else:
                assert test.dimensions == 2
                test = (hi[1]**2*Dx(test[1], 0, 1) + test[1]*sp.diff(hi[1]**2, psi[0], 1) - hi[0]**2*Dx(test[0], 1, 1) - test[0]*sp.diff(hi[0]**2, psi[1], 1))/sg
        else:
            raise NotImplementedError

    return test
