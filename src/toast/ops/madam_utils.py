# Copyright (c) 2015-2020 by the parties listed in the AUTHORS file.
# All rights reserved.  Use of this source code is governed by
# a BSD-style license that can be found in the LICENSE file.

import os

import numpy as np

import healpy as hp

from ..utils import Logger, Timer, GlobalTimers, dtype_to_aligned, memreport

from ..timing import function_timer

from .memory_counter import MemoryCounter


def log_time_memory(
    data, timer=None, timer_msg=None, mem_msg=None, full_mem=False, prefix=""
):
    log = Logger.get()
    data.comm.comm_world.barrier()
    restart = False

    if timer is not None:
        if timer.is_running():
            timer.stop()
            restart = True

        if data.comm.world_rank == 0:
            msg = "{} {}: {:0.1f} s".format(prefix, timer_msg, timer.seconds())
            log.debug(msg)

    if mem_msg is not None:
        # Dump toast memory use
        mem_count = MemoryCounter(silent=True)
        mem_count.total_bytes = 0
        toast_bytes = mem_count.apply(data)

        if data.comm.group_rank == 0:
            msg = "{} {} Group {} memory = {:0.2f} GB".format(
                prefix, mem_msg, data.comm.group, toast_bytes / 1024 ** 2
            )
            log.debug(msg)
        if full_mem:
            _ = memreport(msg="{} {}".format(prefix, mem_msg), comm=comm)
    if restart:
        timer.start()


def stage_local(
    data,
    nsamp,
    view,
    dets,
    detdata_name,
    madam_buffer,
    interval_starts,
    nnz,
    nnz_stride,
    shared_flags,
    shared_mask,
    det_flags,
    det_mask,
    do_purge=False,
):
    """Helper function to fill a madam buffer from a local detdata key."""
    n_det = len(dets)
    interval = 0
    do_flags = False
    if shared_flags is not None or det_flags is not None:
        do_flags = True
        # Flagging should only be enabled when we are processing the pixel indices
        # (which is how madam effectively implements flagging).  So we will set
        # all flagged samples to "-1" below.
        if nnz != 1:
            raise RuntimeError(
                "Internal error on madam copy.  Only pixel indices should be flagged."
            )
    for ob in data.obs:
        # Loop over views
        views = ob.view[view]
        for ivw, vw in enumerate(views):
            view_samples = None
            if vw.start is None:
                # This is a view of the whole obs
                view_samples = ob.n_local_samples
            else:
                view_samples = vw.stop - vw.start
            offset = interval_starts[interval]
            flags = None
            if do_flags:
                # Using flags
                flags = np.zeros(view_samples, dtype=np.uint8)
            if shared_flags is not None:
                flags |= views.shared[shared_flags][ivw] & shared_mask

            ldet = 0
            for idet, det in enumerate(dets):
                if det not in ob.local_detectors:
                    continue
                slc = slice(
                    (idet * nsamp + offset) * nnz,
                    (idet * nsamp + offset + view_samples) * nnz,
                    1,
                )
                if nnz > 1:
                    madam_buffer[slc] = views.detdata[detdata_name][ivw][
                        ldet
                    ].flatten()[::nnz_stride]
                else:
                    madam_buffer[slc] = views.detdata[detdata_name][ivw][ldet].flatten()
                detflags = None
                if do_flags:
                    if det_flags is None:
                        detflags = flags
                    else:
                        detflags = np.copy(flags)
                        detflags |= views.detdata[det_flags][ivw][ldet] & det_mask
                    madam_buffer[slc][detflags != 0] = -1
                ldet += 1
            interval += 1
        if do_purge:
            del ob.detdata[detdata_name]
    return


def stage_in_turns(
    data,
    nodecomm,
    n_copy_groups,
    nsamp,
    view,
    dets,
    detdata_name,
    madam_dtype,
    interval_starts,
    nnz,
    nnz_stride,
    shared_flags,
    shared_mask,
    det_flags,
    det_mask,
):
    """When purging data, take turns staging it."""
    raw = None
    wrapped = None
    for copying in range(n_copy_groups):
        if nodecomm.rank % n_copy_groups == copying:
            # Our turn to copy data
            storage, _ = dtype_to_aligned(madam_dtype)
            raw = storage.zeros(nsamp * len(dets) * nnz)
            wrapped = raw.array()
            stage_local(
                data,
                nsamp,
                view,
                dets,
                detdata_name,
                wrapped,
                interval_starts,
                nnz,
                nnz_stride,
                shared_flags,
                shared_mask,
                det_flags,
                det_mask,
                do_purge=True,
            )
        nodecomm.barrier()
    return raw, wrapped


def restore_local(
    data,
    nsamp,
    view,
    dets,
    detdata_name,
    detdata_dtype,
    madam_buffer,
    interval_starts,
    nnz,
    nside,
    nest,
):
    """Helper function to create a detdata buffer from madam data."""
    n_det = len(dets)
    interval = 0
    for ob in data.obs:
        # Create the detector data
        if nnz == 1:
            ob.detdata.create(detdata_name, dtype=detdata_dtype)
        else:
            ob.detdata.create(detdata_name, dtype=detdata_dtype, sample_shape=(nnz,))
        # Loop over views
        views = ob.view[view]
        for ivw, vw in enumerate(views):
            view_samples = None
            if vw.start is None:
                # This is a view of the whole obs
                view_samples = ob.n_local_samples
            else:
                view_samples = vw.stop - vw.start
            offset = interval_starts[interval]
            ldet = 0
            for idet, det in enumerate(dets):
                if det not in ob.local_detectors:
                    continue
                slc = slice(
                    (idet * nsamp + offset) * nnz,
                    (idet * nsamp + offset + view_samples) * nnz,
                    1,
                )
                if nnz > 1:
                    views.detdata[detdata_name][ivw][ldet] = madam_buffer[slc].reshape(
                        (-1, nnz)
                    )
                else:
                    # If this is the pointing pixel indices, AND if the original was
                    # in RING ordering, then make a temporary array to do the conversion
                    if nside > 0 and not nest:
                        temp_pixels = -1 * np.ones(view_samples, dtype=detdata_dtype)
                        npix = 12 * nside ** 2
                        good = np.logical_and(
                            madam_buffer[slc] >= 0, madam_buffer[slc] < npix
                        )
                        temp_pixels[good] = madam_buffer[slc][good]
                        temp_pixels[good] = hp.nest2ring(nside, temp_pixels[good])
                        views.detdata[detdata_name][ivw][ldet] = temp_pixels
                    else:
                        views.detdata[detdata_name][ivw][ldet] = madam_buffer[slc]
                ldet += 1
            interval += 1
    return


def restore_in_turns(
    data,
    nodecomm,
    n_copy_groups,
    nsamp,
    view,
    dets,
    detdata_name,
    detdata_dtype,
    madam_buffer,
    madam_buffer_raw,
    interval_starts,
    nnz,
    nside,
    nest,
):
    """When restoring data, take turns copying it."""
    for copying in range(n_copy_groups):
        if nodecomm.rank % n_copy_groups == copying:
            # Our turn to copy data
            restore_local(
                data,
                nsamp,
                view,
                dets,
                detdata_name,
                detdata_dtype,
                madam_buffer,
                interval_starts,
                nnz,
                nside,
                nest,
            )
            madam_buffer_raw.clear()
        nodecomm.barrier()
    return