# Copyright (c) 2015-2020 by the parties listed in the AUTHORS file.
# All rights reserved.  Use of this source code is governed by
# a BSD-style license that can be found in the LICENSE file.

import os
import re
from time import time
import warnings

from astropy import units as u
import numpy as np
import traitlets

from ..mpi import MPI, MPI_Comm, use_mpi, Comm

from .operator import Operator
from .. import qarray as qa
from ..timing import function_timer
from ..traits import trait_docs, Int, Unicode, Bool, Dict, Quantity, Instance
from ..utils import Logger, Environment, Timer, GlobalTimers, dtype_to_aligned


from .._libtoast import filter_polynomial


XAXIS, YAXIS, ZAXIS = np.eye(3)


@trait_docs
class PolyFilter2D(Operator):
    """Operator to regress out 2D polynomials across the focal plane."""

    API = Int(0, help="Internal interface version for this operator")

    det_data = Unicode("signal", help="Observation detdata key apply filtering to")

    pattern = Unicode(
        f".*",
        allow_none=True,
        help="Regex pattern to match against detector names. Only detectors that "
        "match the pattern are filtered.",
    )

    order = Int(1, allow_none=False, help="Polynomial order")

    det_flags = Unicode(
        None, allow_none=True, help="Observation detdata key for flags to use"
    )

    det_flag_mask = Int(1, help="Bit mask value for optional detector flagging")

    poly_flag_mask = Int(1, help="Bit mask value for intervals that fail to filter")

    shared_flags = Unicode(
        None, allow_none=True, help="Observation shared key for telescope flags to use"
    )

    shared_flag_mask = Int(1, help="Bit mask value for optional shared flagging")

    view = Unicode(
        None, allow_none=True, help="Use this view of the data in all observations"
    )

    focalplane_key = Unicode(
        None, allow_none=True, help="Which focalplane key to match"
    )

    @traitlets.validate("shared_flag_mask")
    def _check_shared_flag_mask(self, proposal):
        check = proposal["value"]
        if check < 0:
            raise traitlets.TraitError("Shared flag mask should be a positive integer")
        return check

    @traitlets.validate("det_flag_mask")
    def _check_det_flag_mask(self, proposal):
        check = proposal["value"]
        if check < 0:
            raise traitlets.TraitError("Det flag mask should be a positive integer")
        return check

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        return

    @function_timer
    def _exec(self, data, detectors=None, **kwargs):
        """Apply the 2D polynomial filter to the signal.

        Args:
            data (toast.Data): The distributed data.

        """
        if detectors is not None:
            raise RuntimeError("PolyFilter2D cannot be run on subsets of detectors")
        norder = self.order + 1
        nmode = norder ** 2
        pat = re.compile(self.pattern)

        for obs in data.obs:
            t0 = time()
            t_template = 0
            t_get_norm = 0
            t_apply_norm = 0
            t_solve = 0
            t_clean = 0

            # communicator for processes with the same sample range
            comm = obs.comm_col

            # Detectors to process

            detectors = []
            for det in obs.all_detectors:
                if pat.match(det) is None:
                    continue
                detectors.append(det)
            ndet = len(detectors)
            if ndet == 0:
                continue

            # Detector positions

            detector_position = {}
            for det in detectors:
                det_quat = obs.telescope.focalplane[det]["quat"]
                x, y, z = qa.rotate(det_quat, ZAXIS)
                theta, phi = np.arcsin([x, y])
                detector_position[det] = [theta, phi]

            # Enumerate detector groups (e.g. wafers) to filter

            group_index = {}
            groups = {}
            group_ids = {}
            if self.focalplane_key is None:
                groups[None] = []
                group_ids[None] = 0
                ngroup = 1
                for det in detectors:
                    group_index[det] = 0
                    groups[None].append(det)
            else:
                for det in detectors:
                    value = obs.telescope.focalplane[det][self.focalplane_key]
                    if value not in groups:
                        groups[value] = []
                    groups[value].append(det)
                ngroup = len(groups)
                for igroup, group in enumerate(sorted(groups)):
                    group_ids[group] = igroup
                    for det in groups[group]:
                        group_index[det] = igroup

            # Enumerate detectors to process

            detector_index = {}
            group_det = np.zeros(ndet)
            for idet, det in enumerate(detectors):
                detector_index[det] = idet
                group_det[idet] = group_index[det]

            # Measure offset for each group, translate and scale
            # detector positions to [-1, 1]

            group_offset = {}
            all_positions = []
            for group, detectors_group in groups.items():
                ndet_group = len(detectors_group)
                theta_offset, phi_offset = 0, 0
                for det in detectors_group:
                    theta, phi = detector_position[det]
                    theta_offset += theta
                    phi_offset += phi
                theta_offset /= ndet_group
                phi_offset /= ndet_group
                for det in detectors_group:
                    theta, phi = detector_position[det]
                    detector_position[det] = [theta - theta_offset, phi - phi_offset]
                    all_positions.append(detector_position[det])

            thetavec, phivec = np.vstack(all_positions).T
            thetamax = np.amax(np.abs(thetavec))
            phimax = np.amax(np.abs(phivec))
            scale = 0.999 / max(thetamax, phimax)

            for det in detector_position:
                theta, phi = detector_position[det]
                detector_position[det] = [theta * scale, phi * scale]

            # Now evaluate the polynomial templates at the sites of
            # each detector

            orders = np.arange(norder)
            xorders, yorders = np.meshgrid(orders, orders, indexing="ij")
            xorders = xorders.ravel()
            yorders = yorders.ravel()

            detector_templates = np.zeros([ndet, nmode])
            for det in obs.local_detectors:
                if det not in detector_index:
                    continue
                idet = detector_index[det]
                theta, phi = detector_position[det]
                detector_templates[idet] = theta ** xorders * phi ** yorders

            # Iterate over each interval

            views = obs.view[self.view]
            for iview, view in enumerate(views):
                if view.start is None:
                    # This is a view of the whole obs
                    nsample = obs.n_local_samples
                else:
                    nsample = view.stop - view.start

                # Accumulate the linear regression templates

                # templates = np.zeros([ndet, nmode, nsample])
                templates = np.zeros([ndet, nmode])
                masks = np.zeros([ndet, nsample], dtype=bool)
                signals = np.zeros([ndet, nsample])
                # proj = np.zeros([ngroup, nmode, nsample])

                t1 = time()

                if self.shared_flags is not None:
                    shared_flags = views.shared[self.shared_flags][iview]
                    shared_mask = (shared_flags & self.shared_flag_mask) == 0
                else:
                    shared_mask = np.ones(nsample, dtype=bool)

                for idet, det in enumerate(obs.local_detectors):
                    if det not in detector_index:
                        continue
                    ind_det = detector_index[det]
                    ind_group = group_index[det]

                    signal = views.detdata[self.det_data][iview][idet]
                    if self.det_flags is not None:
                        det_flags = views.detdata[self.det_flags][iview][idet]
                        det_mask = (det_flags & self.det_flag_mask) == 0
                        mask = np.logical_and(shared_mask, det_mask)
                    else:
                        mask = shared_mask

                    template = detector_templates[ind_det]
                    templates[ind_det] = template
                    masks[ind_det] = mask
                    signals[ind_det] = signal * mask

                t_template += time() - t1

                t1 = time()
                if comm is not None:
                    comm.allreduce(templates)
                    comm.allreduce(masks)
                    comm.allreduce(signals)
                t_get_norm += time() - t1

                # Solve the linear regression amplitudes.  Each task
                # inverts different template matrices

                t1 = time()
                coeff = np.zeros([nsample, ngroup, nmode])
                masks = masks.T.copy()  # nsample x ndet
                for isample in range(nsample):
                    if comm is not None and isample % comm.size != comm.rank:
                        continue
                    for group, igroup in group_ids.items():
                        good = group_det == igroup
                        mask = masks[isample, good]
                        t = templates[good].T.copy() * mask
                        proj = np.dot(t, signals[good, isample] * mask)
                        ccinv = np.dot(t, t.T)
                        coeff[isample, igroup] = np.linalg.lstsq(ccinv, proj)[0]
                if comm is not None:
                    comm.allreduce(coeff)
                t_solve += time() - t1

                t1 = time()

                for igroup in range(ngroup):
                    local_dets = obs.local_detectors
                    good = np.zeros(len(local_dets), dtype=np.bool)
                    for idet, det in enumerate(local_dets):
                        if group_index[det] == igroup:
                            good[idet] = True
                    if not np.any(good):
                        continue
                    if self.det_flags is not None:
                        for isample in range(nsample):
                            if np.all(coeff[isample, igroup] == 0):
                                views.detdata[self.det_flags][iview][:, isample] |= (
                                    good * self.poly_flag_mask
                                ).astype(views.detdata[self.det_flags][0].dtype)

                coeff = np.transpose(
                    coeff, [1, 0, 2]
                ).copy()  # ngroup x nsample x nmode
                masks = masks.T.copy()  # ndet x nsample
                for idet, det in enumerate(obs.local_detectors):
                    if det not in detector_index:
                        continue
                    igroup = group_index[det]
                    ind = detector_index[det]
                    signal = views.detdata[self.det_data][iview][idet]
                    mask = masks[idet]
                    signal -= np.sum(coeff[igroup] * templates[ind], 1) * mask

                t_clean += time() - t1

            """
            print(
                "Time per observation: {:.1f} s\n"
                "   templates : {:6.1f} s\n"
                "    get_norm : {:6.1f} s\n"
                "  apply_norm : {:6.1f} s\n"
                "       solve : {:6.1f} s\n"
                "       clean : {:6.1f} s".format(
                    time() - t0, t_template, t_get_norm, t_apply_norm, t_solve, t_clean
                ),
                flush=True,
            )
            """

        return

    def _finalize(self, data, **kwargs):
        return

    def _requires(self):
        req = {
            "meta": list(),
            "shared": list(),
            "detdata": [self.det_data],
            "intervals": list(),
        }
        if self.shared_flags is not None:
            req["shared"].append(self.shared_flags)
        if self.det_flags is not None:
            req["detdata"].append(self.det_flags)
        if self.view is not None:
            req["intervals"].append(self.view)
        return req

    def _provides(self):
        prov = {
            "meta": list(),
            "shared": list(),
            "detdata": list(),
        }
        return prov

    def _accelerators(self):
        return list()


@trait_docs
class PolyFilter(Operator):
    """Operator which applies polynomial filtering to the TOD."""

    API = Int(0, help="Internal interface version for this operator")

    det_data = Unicode("signal", help="Observation detdata key apply filtering to")

    pattern = Unicode(
        f".*",
        allow_none=True,
        help="Regex pattern to match against detector names. Only detectors that "
        "match the pattern are filtered.",
    )

    order = Int(1, allow_none=False, help="Polynomial order")

    det_flags = Unicode(
        None, allow_none=True, help="Observation detdata key for flags to use"
    )

    det_flag_mask = Int(0, help="Bit mask value for optional detector flagging")

    poly_flag_mask = Int(0, help="Bit mask value for intervals that fail to filter")

    shared_flags = Unicode(
        None, allow_none=True, help="Observation shared key for telescope flags to use"
    )

    shared_flag_mask = Int(0, help="Bit mask value for optional shared flagging")

    view = Unicode(
        None, allow_none=True, help="Use this view of the data in all observations"
    )

    @traitlets.validate("shared_flag_mask")
    def _check_shared_flag_mask(self, proposal):
        check = proposal["value"]
        if check < 0:
            raise traitlets.TraitError("Shared flag mask should be a positive integer")
        return check

    @traitlets.validate("det_flag_mask")
    def _check_det_flag_mask(self, proposal):
        check = proposal["value"]
        if check < 0:
            raise traitlets.TraitError("Det flag mask should be a positive integer")
        return check

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        return

    @function_timer
    def _exec(self, data, detectors=None, **kwargs):
        log = Logger.get()

        if self.pattern is None:
            pat = None
        else:
            pat = re.compile(self.pattern)

        for obs in data.obs:
            # Get the detectors we are using for this observation
            dets = obs.select_local_detectors(detectors)
            if len(dets) == 0:
                # Nothing to do for this observation
                continue

            if self.view is not None:
                local_starts = []
                local_stops = []
                for interval in obs.intervals[self.view]:
                    local_starts.append(interval.first)
                    local_stops.append(intervallast)
            else:
                local_starts = [0]
                local_stops = [obs.n_local_samples - 1]

            local_starts = np.array(local_starts)
            local_stops = np.array(local_stops)

            if self.shared_flags is not None:
                shared_flags = (
                    obs.shared[self.shared_flags].data & self.shared_flag_mask
                )
            else:
                shared_flags = np.zeros(obs.n_local_samples, dtype=np.uint8)

            for idet, det in enumerate(dets):
                # Test the detector pattern
                if pat.match(det) is None:
                    continue

                signal = obs.detdata[self.det_data][idet]
                if self.det_flags is not None:
                    det_flags = obs.detdata[self.det_flags][idet] & self.det_flag_mask
                    flags = shared_flags | det_flags
                else:
                    flags = shared_flags

                filter_polynomial(
                    self.order, flags, [signal], local_starts, local_stops
                )

                if self.det_flags is not None:
                    obs.detdata[self.det_flags][idet][flags != 0] |= self.poly_flag_mask

        return

    def _finalize(self, data, **kwargs):
        return

    def _requires(self):
        req = {
            "meta": list(),
            "shared": list(),
            "detdata": [self.det_data],
            "intervals": [self.view],
        }
        if self.shared_flags is not None:
            req["shared"].append(self.shared_flags)
        if self.det_flags is not None:
            req["detdata"].append(self.det_flags)
        return req

    def _provides(self):
        prov = {
            "meta": list(),
            "shared": list(),
            "detdata": list(),
        }
        return prov

    def _accelerators(self):
        return list()


class CommonModeFilter(Operator):
    """Operator to regress out common mode at each time stamp."""

    API = Int(0, help="Internal interface version for this operator")

    det_data = Unicode("signal", help="Observation detdata key apply filtering to")

    pattern = Unicode(
        f".*",
        allow_none=True,
        help="Regex pattern to match against detector names. Only detectors that "
        "match the pattern are filtered.",
    )

    order = Int(1, allow_none=False, help="Polynomial order")

    det_flags = Unicode(
        None, allow_none=True, help="Observation detdata key for flags to use"
    )

    det_flag_mask = Int(0, help="Bit mask value for optional detector flagging")

    poly_flag_mask = Int(0, help="Bit mask value for intervals that fail to filter")

    shared_flags = Unicode(
        None, allow_none=True, help="Observation shared key for telescope flags to use"
    )

    shared_flag_mask = Int(0, help="Bit mask value for optional shared flagging")

    focalplane_key = Unicode(
        None, allow_none=True, help="Which focalplane key to match"
    )

    @traitlets.validate("shared_flag_mask")
    def _check_shared_flag_mask(self, proposal):
        check = proposal["value"]
        if check < 0:
            raise traitlets.TraitError("Shared flag mask should be a positive integer")
        return check

    @traitlets.validate("det_flag_mask")
    def _check_det_flag_mask(self, proposal):
        check = proposal["value"]
        if check < 0:
            raise traitlets.TraitError("Det flag mask should be a positive integer")
        return check

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        return

    @function_timer
    def _exec(self, data, detectors=None, **kwargs):
        """Apply the common mode filter to the signal.

        Args:
            data (toast.Data): The distributed data.

        """
        if detectors is not None:
            raise RuntimeError("CommonModeFilter cannot be run in batch mode")

        pat = re.compile(self.pattern)

        for obs in data.obs:
            focalplane = obs.telescope.focalplane
            # communicator for processes with the same sample range
            comm = obs.comm_col

            detectors = obs.all_detectors
            if self.focalplane_key is None:
                values = [None]
            else:
                values = set()
                for det in detectors:
                    if pat.match(det) is None:
                        continue
                    values.add(focalplane[det][self.focalplane_key])
                values = sorted(values)

            nsample = obs.n_local_samples

            for value in values:
                local_dets = []
                for idet, det in enumerate(obs.local_detectors):
                    if pat.match(det) is None:
                        continue
                    if (
                        value is not None
                        and focalplane[det][self.focalplane_key] != value
                    ):
                        continue
                    local_dets.append((idet, det))

                template = np.zeros(nsample)
                hits = np.zeros(nsample)
                if self.shared_flags is not None:
                    shared_flags = obs.shared[self.shared_flags].data
                    shared_mask = (shared_flags & self.shared_flag_mask) == 0
                else:
                    shared_mask = np.ones(nsample, dtype=bool)
                for idet, det in local_dets:
                    signal = obs.detdata[self.det_data][idet]
                    if self.det_flags is not None:
                        det_flags = obs.detdata[self.det_flags][idet]
                        det_mask = (det_flags & self.det_flag_mask) == 0
                        mask = np.logical_and(shared_mask, det_mask)
                    else:
                        mask = shared_mask
                    template[mask] += signal[mask]
                    hits[mask] += 1

                if comm is not None:
                    comm.Barrier()
                    comm.Allreduce(MPI.IN_PLACE, template, op=MPI.SUM)
                    comm.Allreduce(MPI.IN_PLACE, hits, op=MPI.SUM)

                good = hits != 0
                template[good] /= hits[good]

                for idet, det in local_dets:
                    obs.detdata[self.det_data][idet] -= template
        return

    def _finalize(self, data, **kwargs):
        return

    def _requires(self):
        req = {
            "meta": list(),
            "shared": list(),
            "detdata": [self.det_data],
            "intervals": [self.view],
        }
        if self.shared_flags is not None:
            req["shared"].append(self.shared_flags)
        if self.det_flags is not None:
            req["detdata"].append(self.det_flags)
        return req

    def _provides(self):
        prov = {
            "meta": list(),
            "shared": list(),
            "detdata": list(),
        }
        return prov

    def _accelerators(self):
        return list()
