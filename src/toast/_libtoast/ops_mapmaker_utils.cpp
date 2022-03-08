// Copyright (c) 2015-2021 by the parties listed in the AUTHORS file.
// All rights reserved.  Use of this source code is governed by
// a BSD-style license that can be found in the LICENSE file.

#include <module.hpp>

#include <accelerator.hpp>

#include <intervals.hpp>


void init_ops_mapmaker_utils(py::module & m) {
    m.def(
        "build_noise_weighted", [](
            py::buffer global2local,
            py::buffer zmap,
            py::buffer pixel_index,
            py::buffer pixels,
            py::buffer weight_index,
            py::buffer weights,
            py::buffer data_index,
            py::buffer det_data,
            py::buffer flag_index,
            py::buffer det_flags,
            py::buffer det_scale,
            uint8_t det_flag_mask,
            py::buffer intervals,
            py::buffer shared_flags,
            uint8_t shared_flag_mask,
            bool use_accel
        ) {
            // This is used to return the actual shape of each buffer
            std::vector <int64_t> temp_shape(3);

            int32_t * raw_pixel_index = extract_buffer <int32_t> (
                pixel_index, "pixel_index", 1, temp_shape, {-1}
            );
            int64_t n_det = temp_shape[0];

            int64_t * raw_pixels = extract_buffer <int64_t> (
                pixels, "pixels", 2, temp_shape, {n_det, -1}
            );
            int64_t n_samp = temp_shape[1];

            int32_t * raw_weight_index = extract_buffer <int32_t> (
                weight_index, "weight_index", 1, temp_shape, {n_det}
            );

            double * raw_weights = extract_buffer <double> (
                weights, "weights", 3, temp_shape, {n_det, n_samp, -1}
            );
            int64_t nnz = temp_shape[2];

            int32_t * raw_data_index = extract_buffer <int32_t> (
                data_index, "data_index", 1, temp_shape, {n_det}
            );
            double * raw_det_data = extract_buffer <double> (
                det_data, "det_data", 2, temp_shape, {n_det, n_samp}
            );
            int32_t * raw_flag_index = extract_buffer <int32_t> (
                flag_index, "flag_index", 1, temp_shape, {n_det}
            );
            uint8_t * raw_det_flags = extract_buffer <uint8_t> (
                det_flags, "det_flags", 2, temp_shape, {n_det, n_samp}
            );

            double * raw_det_scale = extract_buffer <double> (
                det_scale, "det_scale", 1, temp_shape, {n_det}
            );

            uint8_t * raw_shared_flags = extract_buffer <uint8_t> (
                shared_flags, "flags", 1, temp_shape, {n_samp}
            );

            Interval * raw_intervals = extract_buffer <Interval> (
                intervals, "intervals", 1, temp_shape, {-1}
            );
            int64_t n_view = temp_shape[0];

            int64_t * raw_global2local = extract_buffer <int64_t> (
                global2local, "global2local", 1, temp_shape, {-1}
            );
            int64_t n_global_submap = temp_shape[0];

            double * raw_zmap = extract_buffer <double> (
                zmap, "zmap", 3, temp_shape, {-1, -1, nnz}
            );
            int64_t n_local_submap = temp_shape[0];
            int64_t n_pix_submap = temp_shape[1];

            auto & omgr = OmpManager::get();
            int dev = omgr.get_device();
            bool offload = (! omgr.device_is_host()) && use_accel;

            int64_t * dev_pixels = raw_pixels;
            double * dev_weights = raw_weights;
            double * dev_det_data = raw_det_data;
            uint8_t * dev_det_flags = raw_det_flags;
            Interval * dev_intervals = raw_intervals;
            uint8_t * dev_shared_flags = raw_shared_flags;
            double * dev_zmap = raw_zmap;
            int64_t * dev_global2local = raw_global2local;

            if (offload) {
                dev_pixels = (int64_t*)omgr.device_ptr((void*)raw_pixels);
                dev_weights = (double*)omgr.device_ptr((void*)raw_weights);
                dev_det_data = (double*)omgr.device_ptr((void*)raw_det_data);
                dev_det_flags = (uint8_t*)omgr.device_ptr((void*)raw_det_flags);
                dev_intervals = (Interval*)omgr.device_ptr(
                    (void*)raw_intervals
                );
                dev_shared_flags = (uint8_t*)omgr.device_ptr((void*)raw_shared_flags);
                dev_zmap = (double*)omgr.device_ptr((void*)raw_zmap);
                dev_global2local = (int64_t*)omgr.device_ptr((void*)raw_global2local);
            }

            double npix_submap_inv = 1.0 / (double)(n_pix_submap);

            #pragma omp target data \
                device(dev) \
                map(to: \
                    raw_weight_index[0:n_det], \
                    raw_pixel_index[0:n_det], \
                    raw_flag_index[0:n_det], \
                    raw_data_index[0:n_det], \
                    raw_det_scale[0:n_det], \
                    raw_global2local[0:n_global_submap], \
                    n_view, \
                    n_det, \
                    n_samp, \
                    nnz, \
                    n_pix_submap, \
                    det_flag_mask, \
                    npix_submap_inv, \
                    shared_flag_mask \
                ) \
                use_device_ptr( \
                    dev_pixels, \
                    dev_weights, \
                    dev_det_data, \
                    dev_det_flags, \
                    dev_intervals, \
                    dev_shared_flags, \
                    dev_zmap \
                ) \
                if(offload)
            {
                #pragma omp target teams distribute collapse(2) if(offload)
                for (int64_t idet = 0; idet < n_det; idet++) {
                    for (int64_t iview = 0; iview < n_view; iview++) {
                        #pragma omp parallel for
                        for (
                            int64_t isamp = dev_intervals[iview].first;
                            isamp <= dev_intervals[iview].last;
                            isamp++
                        ) {
                            int32_t w_indx = raw_weight_index[idet];
                            int32_t p_indx = raw_pixel_index[idet];
                            int32_t f_indx = raw_flag_index[idet];
                            int32_t d_indx = raw_data_index[idet];

                            int64_t off_p = p_indx * n_samp + isamp;
                            int64_t off_w = w_indx * n_samp + isamp;
                            int64_t off_d = d_indx * n_samp + isamp;
                            int64_t off_f = f_indx * n_samp + isamp;
                            int64_t isubpix;
                            int64_t zoff;
                            int64_t off_wt;
                            double scaled_data;
                            int64_t local_submap;
                            int64_t global_submap;

                            if (
                                (dev_pixels[off_p] >= 0) &&
                                ((dev_det_flags[off_f] & det_flag_mask) == 0) &&
                                ((dev_shared_flags[off_p] & shared_flag_mask) == 0)
                            ) {
                                // Good data, accumulate
                                global_submap = (int64_t)(
                                    dev_pixels[off_p] * npix_submap_inv
                                );

                                local_submap = dev_global2local[global_submap];

                                isubpix = dev_pixels[off_p] - global_submap * n_pix_submap;
                                zoff = nnz * (local_submap * n_pix_submap + isubpix);

                                off_wt = nnz * off_w;

                                scaled_data = dev_det_data[off_d] * raw_det_scale[idet];

                                for (
                                    int64_t iweight = 0; iweight < nnz; iweight++
                                ) {
                                    dev_zmap[zoff + iweight] += scaled_data *
                                                                dev_weights[off_wt + iweight];
                                }
                            }
                        }
                    }
                }
            }
            return;
        });

}
