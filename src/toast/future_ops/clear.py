# Copyright (c) 2015-2020 by the parties listed in the AUTHORS file.
# All rights reserved.  Use of this source code is governed by
# a BSD-style license that can be found in the LICENSE file.

import traitlets

from ..utils import Logger

from ..traits import trait_docs, Int, Unicode, List

from ..operator import Operator


@trait_docs
class Clear(Operator):
    """Class to purge data from observations.

    This operator takes lists of shared, detdata, and meta keys to delete from
    observations.

    """

    # Class traits

    API = Int(0, help="Internal interface version for this operator")

    meta = List(
        None, allow_none=True, help="List of Observation dictionary keys to delete"
    )

    detdata = List(
        None, allow_none=True, help="List of Observation detdata keys to delete"
    )

    shared = List(
        None, allow_none=True, help="List of Observation shared keys to delete"
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _exec(self, data, detectors=None, **kwargs):
        log = Logger.get()
        for ob in data.obs:
            if self.detdata is not None:
                for key in self.detdata:
                    # This ignores non-existant keys
                    del ob.detdata[key]
            if self.shared is not None:
                for key in self.shared:
                    # This ignores non-existant keys
                    del ob.shared[key]
            if self.meta is not None:
                for key in self.meta:
                    try:
                        del ob[key]
                    except KeyError:
                        pass
        return

    def _finalize(self, data, **kwargs):
        return None

    def _requires(self):
        # Although we could require nothing, since we are deleting keys only if they
        # exist, providing these as requirements allows us to catch dependency issues
        # in pipelines.
        req = dict()
        req["meta"] = list(self.meta)
        req["detdata"] = list(self.detdata)
        req["shared"] = list(self.shared)
        return req

    def _provides(self):
        return dict()

    def _accelerators(self):
        # Eventually we can delete memory objects on devices...
        return list()
