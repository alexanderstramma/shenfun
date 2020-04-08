"""
Module for implementation of the :class:`.TensorProductSpace` class and
related methods.
"""
from numbers import Number
import warnings
import sympy as sp
import numpy as np
from shenfun.fourier.bases import R2CBasis, C2CBasis
from shenfun.utilities import apply_mask, get_measures, split
from shenfun.forms.arguments import Function, Array
from shenfun.optimization.cython import evaluate
from shenfun.spectralbase import slicedict, islicedict, SpectralBase
from mpi4py_fft.mpifft import Transform, PFFT
from mpi4py_fft.pencil import Subcomm, Pencil

__all__ = ('TensorProductSpace', 'VectorTensorProductSpace',
           'MixedTensorProductSpace', 'Convolve')

# Default sympy symbols. Note that order is important
x, y, z, r, s = psi = sp.symbols('x,y,z,r,s', real=True)

class TensorProductSpace(PFFT):
    """Class for multidimensional tensorproductspaces.

    The tensorproductspaces are created as Cartesian products from a set of 1D
    bases. The 1D bases are subclassed instances of the :class:`.SpectralBase`
    class.

    Parameters
    ----------
    comm : MPI communicator
    bases : list
        List of 1D bases
    axes : tuple of ints, optional
        A tuple containing the order of which to perform transforms.
        Last item is transformed first. Defaults to range(len(bases))
    dtype : data-type, optional
        Type of input data in real physical space. If not provided it
        will be inferred from the bases.
    slab : bool, optional
        Use 1D slab decomposition.
    collapse_fourier : bool, optional
        Collapse axes for Fourier bases if possible
    backward_from_pencil : False or Pencil
        In case of Pencil configure the transform by starting from the
        Pencil distribution in spectral space. This is primarily intended
        for a padded space, where the spectral distribution must be
        equal to the non-padded space.
    kw : dict, optional
        Dictionary that can be used to plan transforms. Input to method
        `plan` for the bases.

    """
    def __init__(self, comm, bases, axes=None, dtype=None, slab=False,
                 collapse_fourier=False, backward_from_pencil=False,
                 measures=None, **kw):
        # Note do not call __init__ of super
        self.comm = comm
        self.bases = bases
        self.measures = measures if measures is not None else (psi[:len(bases)],)*2
        self.hi = get_measures(*self.measures)
        if not self.hi.prod() == 1:
            for key, val in split(self.hi).items():
                k = 'xyzrs'.index(key)
                self.bases[k]._dx *= val

        shape = list(self.global_shape())
        assert shape
        assert min(shape) > 0

        if axes is not None:
            axes = list(axes) if np.ndim(axes) else [axes]
        else:
            axes = list(range(len(shape)))

        for i, ax in enumerate(axes):
            if isinstance(ax, (int, np.integer)):
                if ax < 0:
                    ax += len(shape)
                axes[i] = (ax,)
            else:
                assert isinstance(ax, (tuple, list))
                ax = list(ax)
                for j, a in enumerate(ax):
                    assert isinstance(a, int)
                    if a < 0:
                        a += len(shape)
                        ax[j] = a
                axes[i] = ax
            assert min(axes[i]) >= 0
            assert max(axes[i]) < len(shape)
            assert 0 < len(axes[i]) <= len(shape)
            assert sorted(axes[i]) == sorted(set(axes[i]))

        if dtype is None:
            dtype = np.complex if isinstance(bases[axes[-1][-1]], C2CBasis) else np.float

        dtype = np.dtype(dtype)
        assert dtype.char in 'fdgFDG'

        self.axes = axes
        self.xfftn = []
        self.transfer = []
        self.pencil = [None, None]
        for axis, base in enumerate(self.bases):
            base.tensorproductspace = self
            base.axis = axis
            base.sl = slicedict(axis=axis, dimensions=len(self.bases))
            base.si = islicedict(axis=axis, dimensions=len(self.bases))

        if not backward_from_pencil:
            if isinstance(bases[axes[-1][-1]], C2CBasis):
                assert np.dtype(dtype).char in 'FDG'
            elif isinstance(bases[axes[-1][-1]], R2CBasis):
                assert np.dtype(dtype).char in 'fdg'

            if isinstance(comm, Subcomm):
                assert slab is False
                assert len(comm) == len(shape)
                assert comm[axes[-1][-1]].Get_size() == 1
                self.subcomm = comm
            else:
                if slab:
                    axis = (axes[-1][-1] + 1) % len(shape)
                    dims = [1] * len(shape)
                    dims[axis] = comm.Get_size()
                else:
                    dims = [0] * len(shape)
                    for ax in axes[-1]:
                        dims[ax] = 1
                self.subcomm = Subcomm(comm, dims)

            # At this points axes is a tuple of tuples of length one.
            # Try to collapse some Fourier transforms into one.
            if np.any([abs(base.padding_factor - 1.0) > 1e-6 for base in bases]):
                collapse_fourier = False
            if collapse_fourier:
                F = lambda ax: bases[ax].family() == 'fourier' and self.subcomm[ax].Get_size() == 1
                axis = self.axes[-1][-1]
                groups = [list(self.axes[-1])]
                F0 = F(axis)
                for ax in reversed(self.axes[:-1]):
                    axis = ax[-1]
                    if F0 and F(axis):
                        groups[0].insert(0, axis)
                    else:
                        groups.insert(0, list(ax))
                    F0 = F(axis)
                self.axes = groups
            self.axes = tuple(map(tuple, self.axes))

            # Configure all transforms
            axes = self.axes[-1]
            pencil = Pencil(self.subcomm, shape, axes[-1])
            self.xfftn.append(self.bases[axes[-1]])
            self.xfftn[-1].plan(pencil.subshape, axes, dtype, kw)
            self.pencil[0] = pencilA = pencil
            if not (shape[axes[-1]] == self.xfftn[-1].forward.output_array.shape[axes[-1]] and
                    self.xfftn[-1].forward.input_array.dtype == self.xfftn[-1].forward.output_array.dtype):
                dtype = self.xfftn[-1].forward.output_array.dtype
                shape[axes[-1]] = self.xfftn[-1].forward.output_array.shape[axes[-1]]
                pencilA = Pencil(self.subcomm, shape, axes[-1])

            for i, axes in enumerate(reversed(self.axes[:-1])):
                pencilB = pencilA.pencil(axes[-1])
                transAB = pencilA.transfer(pencilB, dtype)
                xfftn = self.bases[axes[-1]]
                xfftn.plan(pencilB.subshape, axes, dtype, kw)
                self.xfftn.append(xfftn)
                self.transfer.append(transAB)
                pencilA = pencilB
                if not (shape[axes[-1]] == xfftn.forward.output_array.shape[axes[-1]] and
                        xfftn.forward.input_array.dtype == xfftn.forward.output_array.dtype):
                    dtype = xfftn.forward.output_array.dtype
                    shape[axes[-1]] = xfftn.forward.output_array.shape[axes[-1]]
                    pencilA = Pencil(pencilB.subcomm, shape, axes[-1])

            self.pencil[1] = pencilA

            self.forward = Transform(
                [o.forward for o in self.xfftn],
                [o.forward for o in self.transfer],
                self.pencil)
            self.backward = Transform(
                [o.backward for o in self.xfftn[::-1]],
                [o.backward for o in self.transfer[::-1]],
                self.pencil[::-1])
            self.scalar_product = Transform(
                [o.scalar_product for o in self.xfftn],
                [o.forward for o in self.transfer],
                self.pencil)
            self.backward_uniform = Transform(
                [o.backward_uniform for o in self.xfftn[::-1]],
                [o.backward for o in self.transfer[::-1]],
                self.pencil[::-1])

        else:
            self.configure_backwards(backward_from_pencil, dtype, kw)

        for i, base in enumerate(bases):
            base.axis = i
            if base.has_nonhomogeneous_bcs:
                base.bc.set_tensor_bcs(base, self)

    def configure_backwards(self, pencil, dtype, kw):
        """Configure transforms starting from spectral space

        Parameters
        ----------
            pencil : Pencil
                The distribution in spectral space
            dtype : Numpy dtype
                The type of data in spectral space
            kw : dict
                Any parameters for planning transforms

        Note
        ----
        To ensure the same distribution in spectral space, the padded
        space must be configured by moving from the spectral space towards the
        physical space. The distribution does not have to agree in physical
        space, because the padding is done only in the spectral.
        """
        shape = list(self.global_shape(True))
        axes = self.axes[0]
        xfftn = self.bases[axes[-1]]
        self.xfftn.append(xfftn)
        self.subcomm = pencil.subcomm
        subshape = list(pencil.subshape)
        if isinstance(xfftn, R2CBasis):
            subshape[axes[-1]] = int(np.floor(xfftn.N*xfftn.padding_factor))
            dtype = np.float
        else:
            subshape[axes[-1]] = int(np.floor(subshape[axes[-1]]*xfftn.padding_factor))
        self.xfftn[-1].plan(subshape, axes, dtype, kw)
        if not (shape[axes[-1]] == self.xfftn[-1].forward.input_array.shape[axes[-1]] and
                self.xfftn[-1].forward.input_array.dtype == self.xfftn[-1].forward.output_array.dtype):
            dtype = self.xfftn[-1].forward.input_array.dtype
            shape[axes[-1]] = self.xfftn[-1].forward.input_array.shape[axes[-1]]
        pencilA = Pencil(pencil.subcomm, shape, axes[0])
        self.pencil[0] = pencilA
        for axes in self.axes[1:]:
            pencilB = pencilA.pencil(axes[-1])
            transBA = pencilA.transfer(pencilB, dtype)
            xfftn = self.bases[axes[-1]]
            subshape = list(pencilB.subshape)
            if isinstance(xfftn, R2CBasis):
                subshape[axes[-1]] = int(np.floor(xfftn.N*xfftn.padding_factor))
                dtype = np.float
            else:
                subshape[axes[-1]] = int(np.floor(subshape[axes[-1]]*xfftn.padding_factor))
            xfftn.plan(subshape, axes, dtype, kw)
            self.xfftn.append(xfftn)
            self.transfer.append(transBA)
            pencilA = pencilB
            if not (shape[axes[-1]] == xfftn.forward.input_array.shape[axes[-1]] and
                    xfftn.forward.input_array.dtype == xfftn.forward.output_array.dtype):
                dtype = xfftn.forward.input_array.dtype
                shape[axes[-1]] = xfftn.forward.input_array.shape[axes[-1]]
                pencilA = Pencil(pencilB.subcomm, shape, axes[-1])

        self.pencil[1] = pencilA

        self.backward = Transform(
            [o.backward for o in self.xfftn],
            [o.forward for o in self.transfer],
            self.pencil)
        self.forward = Transform(
            [o.forward for o in self.xfftn[::-1]],
            [o.backward for o in self.transfer[::-1]],
            self.pencil[::-1])
        self.scalar_product = Transform(
            [o.scalar_product for o in self.xfftn[::-1]],
            [o.forward for o in self.transfer[::-1]],
            self.pencil[::-1])
        self.backward_uniform = Transform(
            [o.backward_uniform for o in self.xfftn],
            [o.forward for o in self.transfer],
            self.pencil)

    def get_dealiased(self, padding_factor=1.5, dealias_direct=False):
        if isinstance(padding_factor, Number):
            padding_factor = (padding_factor,)*len(self)
        elif isinstance(padding_factor, (tuple, list, np.ndarray)):
            assert len(padding_factor) == len(self)
        padded_bases = [base.get_dealiased(padding_factor=padding_factor[axis],
                                           dealias_direct=dealias_direct)
                        for axis, base in enumerate(self.bases)]
        return TensorProductSpace(self.comm, padded_bases,
                                  dtype=self.forward.output_array.dtype,
                                  backward_from_pencil=self.forward.output_pencil)

    def get_refined(self, N):
        if isinstance(N, Number):
            N = (N,)*len(self)
        elif isinstance(N, (tuple, list, np.ndarray)):
            assert len(N) == len(self)
        refined_bases = [base.get_refined(N[axis])
                         for axis, base in enumerate(self.bases)]
        return TensorProductSpace(self.comm, refined_bases, axes=self.axes)

    def convolve(self, a_hat, b_hat, ab_hat):
        """Convolution of a_hat and b_hat

        Parameters
        ----------
        a_hat : array
            Input array of shape and type as output array from
            self.forward, or instance of :class:`.Function`
        b_hat : array
            Input array of shape and type as output array from
            self.forward, or instance of :class:`.Function`
        ab_hat : array
            Return array of same type and shape as a_hat and b_hat

        Note
        ----
        The return array ab_hat is truncated to the shape of a_hat and b_hat.

        Also note that self should have bases with padding for this method to give
        a convolution without aliasing. The padding is specified when creating
        instances of bases for the :class:`.TensorProductSpace`.

        FIXME Efficiency due to allocation
        """
        a = self.backward.output_array.copy()
        b = self.backward.output_array.copy()
        a = self.backward(a_hat, a)
        b = self.backward(b_hat, b)
        ab_hat = self.forward(a*b, ab_hat)
        return ab_hat

    def eval(self, points, coefficients, output_array=None, method=2):
        """Evaluate Function at points, given expansion coefficients

        Parameters
        ----------
        points : float or array of floats
            Array must be of shape (D, N), for  N points in D dimensions
        coefficients : array
            Expansion coefficients, or instance of :class:`.Function`
        output_array : array, optional
            Return array, function values at points
        method : int, optional
            Chooses implementation. The default 0 is a low-memory cython
            version. Using method = 1 leads to a faster cython
            implementation that, on the downside, uses more memory.
            The final, method = 2, is a python implementation.
        """
        if output_array is None:
            output_array = np.zeros(points.shape[1], dtype=self.forward.input_array.dtype)
        else:
            output_array[:] = 0
        if len(self.get_nonperiodic_axes()) > 1:
            method = 2
        if method == 0:
            return self._eval_lm_cython(points, coefficients, output_array)
        elif method == 1:
            return self._eval_cython(points, coefficients, output_array)
        else:
            return self._eval_python(points, coefficients, output_array)

    def _eval_python(self, points, coefficients, output_array):
        """Evaluate Function at points, given expansion coefficients

        Parameters
        ----------
        points : float or array of floats
        coefficients : array
            Expansion coefficients
        output_array : array
            Return array, function values at points
        """
        P = []
        last_conj_index = -1
        sl = -1
        out = []
        previous_axes = []
        flataxes = []
        for ax in self.axes:
            for a in ax:
                flataxes.append(a)
        for axis in flataxes:
            base = self.bases[axis]
            x = base.map_reference_domain(points[axis])
            D = base.evaluate_basis_all(x=x, argument=1)
            P = D[..., self.local_slice()[axis]]
            if isinstance(base, R2CBasis):
                M = base.N//2+1
                if base.N % 2 == 0:
                    last_conj_index = M-1
                else:
                    last_conj_index = M
                offset = self.local_slice()[axis].start
                sl = offset
                st = self.local_slice()[axis].stop
                if sl == 0:
                    sl = 1
                st = min(last_conj_index, st) - offset
                sl -= offset
                sp = [slice(None), slice(sl, st)]

            if len(out) == 0:
                out = np.tensordot(P, coefficients, (1, axis))
                if isinstance(base, R2CBasis):
                    ss = [slice(None)]*len(self)
                    ss[axis] = slice(sl, st)
                    out += np.conj(np.tensordot(P[tuple(sp)], coefficients[tuple(ss)], (1, axis)))
                    out = out.real

            else:
                k = np.count_nonzero([m < axis for m in previous_axes])
                if len(self) == 2:
                    if not isinstance(base, R2CBasis):
                        out2 = np.sum(P*out, axis=-1)
                    else:
                        sp = tuple(sp)
                        out2 = np.sum(P.real*out.real - P.imag*out.imag, axis=-1)
                        out2 += np.sum(np.conj(P[sp].real*out[sp].real - P[sp].imag*out[sp].imag), axis=-1)

                elif len(self) == 3:
                    sx = [slice(None)]*out.ndim
                    sd = [slice(None)]*out.ndim
                    if len(out.shape) == 3:
                        kk = 1 if axis-k == 1 else 2
                        sx[kk] = np.newaxis
                    if not isinstance(base, R2CBasis):
                        out2 = np.sum(P[tuple(sx)]*out, axis=1+axis-k)
                    else:
                        out2 = np.sum(P[tuple(sx)].real*out.real - P[tuple(sx)].imag*out.imag, axis=1+axis-k)
                        sx[1+axis-k] = slice(sl, st)
                        sd[1+axis-k] = slice(sl, st)
                        sx = tuple(sx)
                        sd = tuple(sd)
                        out2 += np.sum(np.conj(P[sx].real*out[sd].real - P[sx].imag*out[sd].imag), axis=1+axis-k)
                out = out2
            previous_axes.append(axis)
        output_array[:] = out
        output_array = self.comm.allreduce(output_array)
        return output_array


    def _eval_lm_cython(self, points, coefficients, output_array):
        """Evaluate Function at points, given expansion coefficients

        Parameters
        ----------
        points : float or array of floats
        coefficients : array
            Expansion coefficients
        output_array : array
            Return array, function values at points
        """
        r2c = -1
        last_conj_index = -1
        sl = -1
        x = []
        w = []
        for base in self:
            axis = base.axis
            if isinstance(base, R2CBasis):
                r2c = axis
                M = base.N//2+1
                if base.N % 2 == 0:
                    last_conj_index = M-1
                else:
                    last_conj_index = M
                sl = self.local_slice()[axis].start
            x.append(base.map_reference_domain(points[axis]))
            w.append(base.wavenumbers(bcast=False)[self.local_slice()[axis]].astype(np.float))

        if len(self) == 2:
            output_array = evaluate.evaluate_lm_2D(list(self.bases), output_array, coefficients, x[0], x[1], w[0], w[1], r2c, last_conj_index, sl)

        elif len(self) == 3:
            output_array = evaluate.evaluate_lm_3D(list(self.bases), output_array, coefficients, x[0], x[1], x[2], w[0], w[1], w[2], r2c, last_conj_index, sl)

        output_array = np.atleast_1d(output_array)
        output_array = self.comm.allreduce(output_array)
        return output_array

    def _eval_cython(self, points, coefficients, output_array):
        """Evaluate Function at points, given expansion coefficients

        Parameters
        ----------
        points : float or array of floats
        coefficients : array
            Expansion coefficients
        output_array : array
            Return array, function values at points
        """
        P = []
        r2c = -1
        last_conj_index = -1
        sl = -1
        for base in self:
            axis = base.axis
            x = base.map_reference_domain(points[axis])
            D = base.evaluate_basis_all(x=x)
            P.append(D[..., self.local_slice()[axis]])
            if isinstance(base, R2CBasis):
                r2c = axis
                M = base.N//2+1
                if base.N % 2 == 0:
                    last_conj_index = M-1
                else:
                    last_conj_index = M
                sl = self.local_slice()[axis].start
        if len(self) == 2:
            output_array = evaluate.evaluate_2D(output_array, coefficients, P, r2c, last_conj_index, sl)

        elif len(self) == 3:
            output_array = evaluate.evaluate_3D(output_array, coefficients, P, r2c, last_conj_index, sl)

        output_array = np.atleast_1d(output_array)
        output_array = self.comm.allreduce(output_array)
        return output_array

    def wavenumbers(self, scaled=False, eliminate_highest_freq=False):
        """Return list of wavenumbers of TensorProductSpace

        Parameters
        ----------
        scaled : bool, optional
            Scale wavenumbers with size of box
        eliminate_highest_freq : bool, optional
            Set Nyquist frequency to zero for evenly shaped axes of Fourier
            bases.

        """
        K = []
        for base in self:
            K.append(base.wavenumbers(scaled=scaled,
                                      eliminate_highest_freq=eliminate_highest_freq))
        return K

    def local_wavenumbers(self, broadcast=False, scaled=False,
                          eliminate_highest_freq=False):
        """Return list of local wavenumbers of TensorProductSpace

        Parameters
        ----------
        broadcast : bool, optional
            Broadcast returned wavenumber arrays to actual
            dimensions of TensorProductSpace
        scaled : bool, optional
            Scale wavenumbers with size of box
        eliminate_highest_freq : bool, optional
            Set Nyquist frequency to zero for evenly shaped axes of Fourier
            bases

        """
        k = self.wavenumbers(scaled=scaled, eliminate_highest_freq=eliminate_highest_freq)
        lk = []
        for axis, (n, s) in enumerate(zip(k, self.local_slice(True))):
            ss = [slice(None)]*len(k)
            ss[axis] = s
            lk.append(n[tuple(ss)])
        if broadcast is True:
            return [np.broadcast_to(m, self.shape()) for m in lk]
        return lk

    def mesh(self, uniform=False):
        """Return list of 1D physical meshes for each dimension of
        TensorProductSpace

        Parameters
        ----------
        uniform : bool, optional
            Use uniform mesh for non-periodic bases
        """
        X = []
        for base in self:
            X.append(base.mesh(uniform=uniform))
        return X

    def local_mesh(self, broadcast=False):
        """Return list of local 1D physical meshes for each dimension of
        TensorProductSpace

        Parameters
        ----------
        broadcast : bool, optional
            Broadcast each 1D mesh to real shape of
            :class:`.TensorProductSpace`
        """
        mesh = self.mesh()
        lm = []
        for axis, (n, s) in enumerate(zip(mesh, self.local_slice(False))):
            ss = [slice(None)]*len(mesh)
            ss[axis] = s
            lm.append(n[tuple(ss)])
        if broadcast is True:
            return [np.broadcast_to(m, self.shape(False)) for m in lm]
        return lm

    def dim(self):
        """Return dimension of ``self`` (degrees of freedom)"""
        return np.prod(self.dims())

    def dims(self):
        """Return dimensions (degrees of freedom) of all bases in ``self``"""
        return tuple([base.dim() for base in self])

    def size(self, forward_output=False):
        """Return number of elements in :class:`.TensorProductSpace`"""
        return np.prod(self.shape(forward_output))

    def global_shape(self, forward_output=False):
        """Return global shape of arrays for TensorProductSpace

        Parameters
        ----------
        forward_output : bool, optional
            If True then return shape of an array that is the result of a
            forward transform. If False then return shape of physical
            space, i.e., the input to a forward transform.

        """
        #if not forward_output:
        #    return tuple([int(np.round(base.shape(forward_output)*base.padding_factor)) for base in self])
        return tuple([base.shape(forward_output) for base in self])
        #return self.shape(forward_output)

    def mask_nyquist(self, u_hat, mask=None):
        """Return array `u_hat` with zero Nyquist coefficients

        Parameters
        ----------
        u_hat : array
            Array to be masked
        mask : array or None, optional
            mask array, if not provided then get the mask by calling
            :func:`get_mask_nyquist`
        """
        if mask is None:
            mask = self.get_mask_nyquist()
        u_hat = apply_mask(u_hat, mask)
        return u_hat

    def get_mask_nyquist(self):
        """Return an array with zeros for Nyquist coefficients and one otherwise"""
        k = []
        do_mask = False
        for base in self:
            if base.family() == 'fourier':
                k.append(base.get_mask_nyquist(bcast=True))
                if base.N % 2 == 0:
                    do_mask = True
            else:
                k.append(None)

        if not do_mask:
            return None

        lk = []
        for axis, (n, s) in enumerate(zip(k, self.local_slice(True))):
            if n is None:
                lk.append(1)
            else:
                ss = [slice(None)]*len(k)
                ss[axis] = s
                lk.append(n[tuple(ss)])

        mask = 1
        for ll in lk:
            mask = mask * ll
        return mask

    def __iter__(self):
        return iter(self.bases)

    @property
    def rank(self):
        """Return tensor rank of TensorProductSpace"""
        return 0

    def __len__(self):
        """Return dimension of TensorProductSpace"""
        return len(self.bases)

    def num_components(self):
        """Return number of scalar spaces in TensorProductSpace"""
        return 1

    @property
    def dimensions(self):
        """Return dimension of TensorProductSpace"""
        return self.__len__()

    def get_nonperiodic_axes(self):
        """Return list of axes that are not periodic"""
        axes = []
        for axis, base in enumerate(self):
            if not base.family() == 'fourier':
                axes.append(axis)
        return axes

    @property
    def is_orthogonal(self):
        ortho = True
        for base in self.bases:
            ortho *= base.is_orthogonal
        return ortho

    def get_orthogonal(self):
        ortho = []
        for base in self.bases:
            ortho.append(base.get_orthogonal())
        return TensorProductSpace(self.subcomm, ortho, axes=self.axes)

    def __getitem__(self, i):
        """Return instance of base i

        Parameters
        ----------
        i : int
        """
        return self.bases[i]

    def compatible_base(self, space):
        """Return whether space is compatible with self.

        Parameters
        ----------

        space : TensorProductSpace
            The space compared to

        Note
        ----
        Two spaces are deemed compatible if the underlying bases, along each
        direction, belong to the same family, has the same number of quadrature
        points, and the same quadrature scheme.
        """
        compatible = True
        for base0, base1 in zip(self.bases, space.bases):
            if not hash(base0) == hash(base1):
                compatible = False
                break
        return compatible


class MixedTensorProductSpace(object):
    """Class for composite tensorproductspaces.

    Parameters
    ----------
    spaces : list
        List of TensorProductSpaces
    """

    def __init__(self, spaces):
        self.spaces = spaces
        self.forward = VectorTransform([space.forward for space in spaces])
        self.backward = VectorTransform([space.backward for space in spaces])
        self.backward_uniform = VectorTransform([space.backward_uniform for space in spaces])
        self.scalar_product = VectorTransform([space.scalar_product for space in spaces])

    def eval(self, points, coefficients, output_array=None, method=0):
        """Evaluate Function at points, given expansion coefficients

        Parameters
        ----------
        points : float or array of floats
        coefficients : array
            Expansion coefficients
        output_array : array, optional
            Return array, function values at points
        method : int, optional
            Chooses implementation. The default 0 is a low-memory cython
            version. Using method = 1 leads to a faster cython
            implementation that, on the downside, uses more memory.
            The final, method = 2, is a python implementation used only
            for verification.
        """

        if output_array is None:
            output_array = np.zeros((len(self.flatten()), points.shape[-1]), dtype=self.forward.input_array.dtype)
        for i, space in enumerate(self.flatten()):
            output_array.__array__()[i] = space.eval(points, coefficients.__array__()[i], output_array.__array__()[i], method)
        return output_array

    def convolve(self, a_hat, b_hat, ab_hat):
        """Convolution of a_hat and b_hat

        Parameters
        ----------
        a_hat : array
            Input array of shape and type as output array from
            self.forward, or instance of :class:`.Function`
        b_hat : array
            Input array of shape and type as output array from
            self.forward, or instance of :class:`.Function`
        ab_hat : array
            Return array of same type and shape as a_hat and b_hat

        Note
        ----
        Note that self should have bases with padding for this method to give
        a convolution without aliasing. The padding is specified when creating
        instances of bases for the TensorProductSpace.

        FIXME Efficiency due to allocation
        """
        N = list(self.backward.output_array.shape)
        a = np.zeros([self.dimensions]+N, dtype=self.backward.output_array.dtype)
        b = np.zeros([self.dimensions]+N, dtype=self.backward.output_array.dtype)
        a = self.backward(a_hat, a)
        b = self.backward(b_hat, b)
        ab_hat = self.forward(a*b, ab_hat)
        return ab_hat

    @property
    def dimensions(self):
        """Return dimension of scalar space"""
        return self.flatten()[0].dimensions

    @property
    def rank(self):
        """Return rank of space"""
        return 1

    def dim(self):
        """Return dimension of ``self`` (degrees of freedom)"""
        s = 0
        for space in self.flatten():
            s += space.dim()
        return s

    def dims(self):
        """Return dimensions (degrees of freedom) of all bases in ``self``"""
        s = []
        for space in self.flatten():
            s.append(space.dim())
        return s

    def size(self, forward_output=False):
        """Return number of elements in :class:`.MixedTensorProductSpace`"""
        N = self.shape(forward_output)
        if forward_output:
            return np.sum([np.prod(s) for s in N])
        else:
            return np.prod(N)

    def shape(self, forward_output=False):
        """Return shape of arrays for MixedTensorProductSpace

        Parameters
        ----------
        forward_output : bool, optional
            If True then return shape of an array that is the result of a
            forward transform. If False then return shape of physical
            space, i.e., the input to a forward transform.

        Note
        ----
        A :class:`.MixedTensorProductSpace` may contain tensor product spaces
        of different shape in spectral space. Hence this function returns a
        list of shapes and not one single tuple.
        """
        if forward_output:
            s = []
            for space in self.flatten():
                s.append(space.shape(forward_output))
        else:
            s = self.flatten()[0].shape(forward_output)
            s = (self.num_components(),) + s
        return s

    def global_shape(self, forward_output=False):
        """Return global shape for MixedTensorProductSpace

        Parameters
        ----------
        forward_output : bool, optional
            If True then return shape of an array that is the result of a
            forward transform. If False then return shape of physical
            space, i.e., the input to a forward transform.

        """
        s = self.flatten()[0].global_shape(forward_output)
        return (self.num_components(),) + s

    def local_slice(self, forward_output=False):
        """The local view into the global data

        Parameters
        ----------
        forward_output : bool, optional
            Return local slices of output array (spectral space) if True, else
            return local slices of input array (physical space)

        """
        s = self.flatten()[0].local_slice(forward_output)
        return (slice(None),) + s

    def num_components(self):
        """Return number of spaces in mixed space"""
        f = self.flatten()
        return len(f)

    def flatten(self):
        s = []
        def _recursiveflatten(l, s):
            if hasattr(l, 'spaces'):
                for i in l.spaces:
                    _recursiveflatten(i, s)
            else:
                s.append(l)
        _recursiveflatten(self, s)
        return s

    def compatible_base(self, space):
        assert self.rank == space.rank
        compatible = True
        for space0, space1 in zip(self.flatten(), space.flatten()):
            if space0.compatible_base(space1) is False:
                compatible = False
                break
        return compatible

    def get_refined(self, N):
        raise NotImplementedError

    def __getitem__(self, i):
        return self.spaces[i]

    def __getattr__(self, name):
        obj = object.__getattribute__(self, 'spaces')
        assert name not in ('bases',)
        return getattr(obj[0], name)

    def __len__(self):
        return len(self.spaces)


class VectorTensorProductSpace(MixedTensorProductSpace):
    """A special :class:`.MixedTensorProductSpace` where the number of spaces
    must equal the geometrical dimension of the problem.

    For example, a TensorProductSpace created by a Cartesian product of 2 1D
    bases, will have vectors of length 2. A TensorProductSpace created from 3
    1D bases will have vectors of length 3.

    Parameters
    ----------
    space : :class:`.TensorProductSpace`
        Space to create vector from

    """

    def __init__(self, space):
        if isinstance(space, list):
            warnings.warn("Use only the TensorProductSpace as argument", DeprecationWarning)
            spaces = space
        else:
            spaces = [space]*space.dimensions
        MixedTensorProductSpace.__init__(self, spaces)

    def num_components(self):
        """Return number of spaces in mixed space"""
        assert len(self.spaces) == self.dimensions
        return self.dimensions

    @property
    def rank(self):
        """Return tensor rank of space"""
        return 1

    def shape(self, forward_output=False):
        """Return shape of arrays for VectorTensorProductSpace

        Parameters
        ----------
        forward_output : bool, optional
            If True then return shape of an array that is the result of a
            forward transform. If False then return shape of physical
            space, i.e., the input to a forward transform.

        """
        s = self.flatten()[0].shape(forward_output)
        s = (self.num_components(),) + s
        return s

    def get_refined(self, N):
        return VectorTensorProductSpace(self.spaces[0].get_refined(N))

    def get_dealiased(self, padding_factor=1.5, dealias_direct=False):
        return VectorTensorProductSpace(self.spaces[0].get_dealiased(padding_factor, dealias_direct))

class VectorTransform(object):

    __slots__ = ('_transforms',)

    def __init__(self, transforms):
        self._transforms = []
        for transform in transforms:
            if isinstance(transform, VectorTransform):
                self._transforms += transform._transforms
            else:
                self._transforms.append(transform)

    def __getattr__(self, name):
        obj = object.__getattribute__(self, '_transforms')
        if name == '_transforms':
            return obj
        return getattr(obj[0], name)

    def __call__(self, input_array, output_array, **kw):
        for i, transform in enumerate(self._transforms):
            output_array.__array__()[i] = transform(input_array.__array__()[i],
                                                    output_array.__array__()[i], **kw)
        return output_array


class Convolve(object):
    r"""Class for convolving without truncation.

    The convolution of :math:`\hat{a}` and :math:`\hat{b}` is computed by first
    transforming backwards with padding::

        a = Tp.backward(a_hat)
        b = Tp.backward(b_hat)

    and then transforming the product ``a*b`` forward without truncation::

        ab_hat = T.forward(a*b)

    where Tp is a :class:`.TensorProductSpace` for regular padding, and
    T is a TensorProductSpace with no padding, but using the shape
    of the padded a and b arrays.

    For convolve with truncation forward, use just the convolve method
    of the Tp space instead.

    Parameters
    ----------
    padding_space : :class:`.TensorProductSpace`
        Space with regular padding backward and truncation forward.
    """

    def __init__(self, padding_space):
        self.padding_space = padding_space
        shape = padding_space.shape()
        bases = []
        for i, base in enumerate(padding_space.bases):
            newbase = base.__class__(shape[i], padding_factor=1.0)
            bases.append(newbase)
        axes = []
        for axis in padding_space.axes:
            axes.append(axis[0])
        newspace = TensorProductSpace(padding_space.comm, bases, axes=axes)
        self.newspace = newspace

    def __call__(self, a_hat, b_hat, ab_hat=None):
        """Compute convolution of a_hat and b_hat without truncation

        Parameters
        ----------
        a_hat : :class:`.Function`
        b_hat : :class:`.Function`
        ab_hat : :class:`.Function`
        """
        Tp = self.padding_space
        T = self.newspace
        if ab_hat is None:
            ab_hat = Function(T)

        a = Array(Tp)
        b = Array(Tp)
        a = Tp.backward(a_hat, a)
        b = Tp.backward(b_hat, b)
        ab_hat = T.forward(a*b, ab_hat)
        return ab_hat


class BoundaryValues(object):
    """Class for setting nonhomogeneous boundary conditions for a 1D Dirichlet
    base inside a multidimensional TensorProductSpace.

    Parameters
    ----------
    T : TensorProductSpace
    bc : tuple of numbers
        Tuple with physical boundary values at edges of 1D domain
    """
    # pylint: disable=protected-access, redefined-outer-name, dangerous-default-value, unsubscriptable-object

    def __init__(self, T, bc=(0, 0)):
        self.base = T
        self.tensorproductspace = None
        self.bc = bc            # Containing Data, sp.Exprs or np.ndarray
        self.bcs = list((0,)*len(bc))       # Processed bc
        self.bcs_final = list((0,)*len(bc)) # Data. May differ from bcs only for TensorProductSpaces
        self.axis = 0
        self.bc_time = 0
        self.update_bcs(bc=bc)

    def update_bcs(self, bc=None):
        if bc is not None:
            assert isinstance(bc, (list, tuple))
            assert len(bc) in (2, 4)
            self.bc = list(bc)
            for i, bci in enumerate(bc):
                if isinstance(bci, (Number, sp.Expr, np.ndarray)):
                    self.bcs[i] = bci
                else:
                    raise NotImplementedError

            self.bcs_final[:] = self.bcs

    def update_bcs_time(self, time):
        tt = sp.symbols('t')
        update_time = False
        for i, bci in enumerate(self.bc):
            if isinstance(bci, sp.Expr):
                if tt in bci.free_symbols:
                    self.bc_time = time
                    self.bcs[i] = bci.subs({'t': time})
                    update_time = True
        if update_time:
            self.bcs_final[:] = self.bcs
            if self.tensorproductspace is not None:
                self.set_tensor_bcs(self.base, self.tensorproductspace)

    def set_tensor_bcs(self, this_base, T):
        """Set correct boundary values for tensor, using values in self.bc

        To modify boundary conditions on the fly, modify first self.bc and then
        call this function. The boundary condition can then be applied as before.
        """
        self.axis = this_base.axis
        self.base = this_base
        self.tensorproductspace = T

        if isinstance(T, SpectralBase):
            pass
        elif this_base.has_nonhomogeneous_bcs:
            # Setting the Dirichlet boundary condition in a TensorProductSpace
            # is more involved than for a single dimension, and the routine will
            # depend on the order of the bases. If the Dirichlet space is the last
            # one, then the boundary condition is applied directly. If there is
            # one other space to the right, then one transform needs to
            # be performed on the bc data first. For two other spaces to the right,
            # two transforms need to be executed.
            axis = None
            number_of_bases_after_this = 0
            for axes in reversed(T.axes):
                base = T.bases[axes[-1]]
                if base == this_base:
                    axis = base.axis
                else:
                    if axis is None:
                        number_of_bases_after_this += 1

            self.number_of_bases_after_this = number_of_bases_after_this

            if self.has_nonhomogeneous_bcs() is False:
                for i in range(len(self.bc)):
                    self.bcs[i] = self.bcs_final[i] = 0
                return

            # Set boundary values
            # These are values set at the end of a transform in Dirichlet space,
            # but before any other transforms
            # Shape is like real space, since Dirichlet does not alter shape
            b = Array(T)
            s = T.local_slice(False)[self.axis]

            for j, bci in enumerate(self.bc):
                if isinstance(bci, sp.Expr):
                    X = T.local_mesh(True)
                    for sym in bci.free_symbols:
                        if str(sym) == 't':
                            bci = bci.subs({'t': self.bc_time})
                    sym0 = bci.free_symbols
                    lbci = sp.lambdify(sym0, bci, 'numpy')
                    Yi = []
                    for sym in sym0:
                        k = 'xyzrs'.index(str(sym))
                        Yi.append(X[k][this_base.si[j]])
                    f_bci = lbci(*Yi)

                    #x, y, z, tt = sp.symbols("x,y,z,t")
                    #sym0 = [sym for sym in (x, y, z) if sym in bci.free_symbols]
                    #if tt in bci.free_symbols:
                    #    bci = bci.subs({'t': self.bc_time})
                    #lbci = sp.lambdify(sym0, bci, 'numpy')
                    #Yi = []
                    #for i, ax in enumerate((x, y, z)):
                    #    if ax in bci.free_symbols:
                    #        Yi.append(X[i][this_base.si[j]])
                    #f_bci = lbci(*Yi)
                    # Put the Dirichlet value in the position of the bc dofs
                    if s.stop == int(this_base.N*this_base.padding_factor):
                        b[this_base.si[-(len(self.bc))+j]] = f_bci

                elif isinstance(bci, (Number, np.ndarray)):
                    if s.stop == int(this_base.N*this_base.padding_factor):
                        b[this_base.si[-(len(self.bc))+j]] = bci

                else:
                    raise NotImplementedError

            if number_of_bases_after_this == 0:
                # Dirichlet base is the first to be transformed
                b_hat = b

            elif number_of_bases_after_this == 1:
                T.forward._xfftn[0].input_array[...] = b

                T.forward._xfftn[0]()
                arrayA = T.forward._xfftn[0].output_array
                arrayB = T.forward._xfftn[1].input_array
                T.forward._transfer[0](arrayA, arrayB)
                b_hat = arrayB.copy()

            elif number_of_bases_after_this == 2:
                T.forward._xfftn[0].input_array[...] = b

                T.forward._xfftn[0]()
                arrayA = T.forward._xfftn[0].output_array
                arrayB = T.forward._xfftn[1].input_array
                T.forward._transfer[0](arrayA, arrayB)

                T.forward._xfftn[1]()
                arrayA = T.forward._xfftn[1].output_array
                arrayB = T.forward._xfftn[2].input_array
                T.forward._transfer[1](arrayA, arrayB)
                b_hat = arrayB.copy()

            # Now b_hat contains the correct slices in slm1 and slm2
            # These are the values to use on intermediate steps. If for example the Dirichlet space is squeezed between two Fourier spaces
            for i in range(len(self.bc)):
                self.bcs[i] = b_hat[this_base.si[-(len(self.bc))+i]].copy()

            # Final (the values to set on fully transformed functions)
            T.forward._xfftn[0].input_array[...] = b
            for i in range(len(T.forward._transfer)):

                T.forward._xfftn[i]()
                arrayA = T.forward._xfftn[i].output_array
                arrayB = T.forward._xfftn[i+1].input_array
                T.forward._transfer[i](arrayA, arrayB)

            T.forward._xfftn[-1]()
            b_hat = T.forward._xfftn[-1].output_array
            for i in range(len(self.bc)):
                self.bcs_final[i] = b_hat[this_base.si[-(len(self.bc))+i]].copy()

    def add_to_orthogonal(self, u, uh):
        """Add contribution from boundary functions to `u`

        Parameters
        ----------
        u : Function
            Apply boundary values to this Function
        uh : Function
            Containing correct boundary values of Function
        """
        B = self.base.get_bc_basis()
        M = B.coefficient_matrix().T
        sl = B.slice()
        if u.ndim > 1:
            sl = self.base.sl[sl]
        for i, row in enumerate(M):
            u[self.base.si[i]] += np.sum(self.base.broadcast_to_ndims(row)*uh[sl], axis=self.base.axis)

    def add_mass_rhs(self, u):
        """Add contribution of boundary functions to rhs before solving in forward transforms

        Parameters
        ----------
        u : Function
            Add boundary values to this Function

        """
        B = self.base.get_bc_basis()
        M = B.addmass_matrix()
        sl = B.slice()
        if u.ndim > 1:
            sl = self.base.sl[sl]

        for i, row in enumerate(M):
            for j in range(len(self.bc)):
                u[self.base.si[i]] -= row[j]*self.bcs[j]

    def set_boundary_dofs(self, u, final=False):
        """Apply boundary condition after solving, fixing boundary dofs

        Parameters
        ----------
        u : Function
            Apply boundary values to this Function
        final : bool
            Whether the function is fully transformed or not. Not is used in
            forward transforms, where the transform of this base may be in
            between the transforms of other bases. If final is True, then u
            must be a fully transformed Function.

        """
        M = len(self.bc)
        if final is True:
            for i in range(M):
                u[self.base.si[-(M)+i]] = self.bcs_final[i]

        else:
            for i in range(M):
                u[self.base.si[-(M)+i]] = self.bcs[i]

    def has_nonhomogeneous_bcs(self):
        for bc in self.bc:
            if isinstance(bc, Number):
                if not bc == 0:
                    return True
            elif isinstance(bc, np.ndarray):
                if not np.all(bc == 0):
                    return True
            elif isinstance(bc, sp.Expr):
                return True
        return False

def some_basic_tests():
    import pyfftw             #pylint: disable=import-error
    from mpi4py import MPI

    comm = MPI.COMM_WORLD
    N = 8
    K0 = C2CBasis(N)
    K1 = C2CBasis(N)
    K2 = C2CBasis(N)
    K3 = R2CBasis(N)
    T = TensorProductSpace(comm, (K0, K1, K2, K3))

    # Create data on rank 0 for testing
    if comm.Get_rank() == 0:
        f_g = np.random.random(T.shape())
        f_g_hat = pyfftw.interfaces.numpy_fft.rfftn(f_g, axes=(0, 1, 2, 3))
    else:
        f_g = np.zeros(T.shape())
        f_g_hat = np.zeros(T.shape(), dtype=np.complex)

    # Distribute test data to all ranks
    comm.Bcast(f_g, root=0)
    comm.Bcast(f_g_hat, root=0)

    # Create a function in real space to hold the test data
    fj = Array(T)
    fj[:] = f_g[T.local_slice(False)]

    # Perform forward transformation
    f_hat = T.forward(fj)

    assert np.allclose(f_g_hat[T.local_slice(True)], f_hat*N**4)

    # Perform backward transformation
    fj2 = Array(T)
    fj2 = T.backward(f_hat)

    assert np.allclose(fj, fj2)

    f_hat = T.scalar_product(fj)

    # Padding
    # Needs new instances of bases because arrays have new sizes
    Kp0 = C2CBasis(N, padding_factor=1.5)
    Kp1 = C2CBasis(N, padding_factor=1.5)
    Kp2 = C2CBasis(N, padding_factor=1.5)
    Kp3 = R2CBasis(N, padding_factor=1.5)
    Tp = TensorProductSpace(comm, (Kp0, Kp1, Kp2, Kp3))

    f_g_pad = Tp.backward(f_hat)
    f_hat2 = Tp.forward(f_g_pad)

    assert np.allclose(f_hat2, f_hat)


if __name__ == '__main__':
    some_basic_tests()
