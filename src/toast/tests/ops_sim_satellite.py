# Copyright (c) 2015-2020 by the parties listed in the AUTHORS file.
# All rights reserved.  Use of this source code is governed by
# a BSD-style license that can be found in the LICENSE file.

from .mpi import MPITestCase

import os

from datetime import datetime

import numpy as np
import numpy.testing as nt

from astropy import units as u

import healpy as hp

from ..mpi import Comm, MPI

from ..data import Data

from ..observation import default_names as obs_names

from ..instrument import Focalplane, Telescope, SpaceSite

from ..instrument_sim import fake_hexagon_focalplane

from ..schedule_sim_satellite import create_satellite_schedule

from ..pixels_io import write_healpix_fits

from ..vis import set_matplotlib_backend

from .. import qarray as qa

from .. import ops as ops

from ._helpers import create_outdir, create_comm


class SimSatelliteTest(MPITestCase):
    def setUp(self):
        fixture_name = os.path.splitext(os.path.basename(__file__))[0]
        self.outdir = create_outdir(self.comm, fixture_name)

        self.toastcomm = create_comm(self.comm)

        npix = 1
        ring = 1
        while 2 * npix <= self.toastcomm.group_size:
            npix += 6 * ring
            ring += 1
        self.npix = npix
        self.fp = fake_hexagon_focalplane(n_pix=npix)

    def test_exec(self):
        # Slow sampling
        fp = fake_hexagon_focalplane(
            n_pix=self.npix,
            sample_rate=(1.0 / 60.0) * u.Hz,
        )
        site = SpaceSite("L2")

        sch = create_satellite_schedule(
            prefix="test_",
            mission_start=datetime(2023, 2, 23),
            observation_time=24 * u.hour,
            gap_time=0 * u.second,
            num_observations=30,
            prec_period=90 * u.minute,
            spin_period=10 * u.minute,
        )

        tele = Telescope("test", focalplane=fp, site=site)

        data = Data(self.toastcomm)

        # Scan fast enough to cover some sky in a short amount of time.  Reduce the
        # angles to achieve a more compact hit map.
        sim_sat = ops.SimSatellite(
            name="sim_sat",
            telescope=tele,
            schedule=sch,
            hwp_angle=obs_names.hwp_angle,
            hwp_rpm=1.0,
            spin_angle=30.0 * u.degree,
            prec_angle=65.0 * u.degree,
        )
        sim_sat.apply(data)

        # Expand pointing and make a hit map.
        detpointing = ops.PointingDetectorSimple()
        pointing = ops.PointingHealpix(
            nest=True,
            mode="IQU",
            hwp_angle=sim_sat.hwp_angle,
            create_dist="pixel_dist",
            detector_pointing=detpointing,
        )
        pointing.nside_submap = 2
        pointing.nside = 8
        pointing.apply(data)

        build_hits = ops.BuildHitMap(pixel_dist="pixel_dist", pixels=pointing.pixels)
        build_hits.apply(data)

        # Plot the hits

        hit_path = os.path.join(self.outdir, "hits.fits")
        write_healpix_fits(data[build_hits.hits], hit_path, nest=pointing.nest)

        if data.comm.world_rank == 0:
            set_matplotlib_backend()
            import matplotlib.pyplot as plt

            hits = hp.read_map(hit_path, field=None, nest=pointing.nest)
            outfile = os.path.join(self.outdir, "hits.png")
            hp.mollview(hits, xsize=1600, nest=True)
            plt.savefig(outfile)
            plt.close()

    def test_precession(self):
        # Test that the precession axis computed for a SpaceSite (anti-sun direction)
        # returns to its starting point after a year.
        zaxis = np.array([0, 0, 1], dtype=np.float64)

        site = SpaceSite("Earth")

        stamps = np.linspace(
            datetime(2021, 1, 1).timestamp(),
            datetime(2022, 1, 1).timestamp(),
            num=1000,
            endpoint=True,
        )

        position = site.position(stamps)

        pos_norm = np.sqrt((position * position).sum(axis=1)).reshape(-1, 1)
        pos_norm = 1.0 / pos_norm
        prec_axis = pos_norm * position
        q_prec = qa.from_vectors(np.tile(zaxis, len(stamps)).reshape(-1, 3), prec_axis)

        check = qa.rotate(q_prec, zaxis)

        np.testing.assert_almost_equal(np.dot(check[0], check[-1]), 1.0, decimal=5)