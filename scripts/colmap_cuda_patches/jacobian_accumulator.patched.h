// Copyright (c) 2021, Viktor Larsson
// All rights reserved.
//
// Redistribution and use in source and binary forms, with or without
// modification, are permitted provided that the following conditions are met:
//
//     * Redistributions of source code must retain the above copyright
//       notice, this list of conditions and the following disclaimer.
//
//     * Redistributions in binary form must reproduce the above copyright
//       notice, this list of conditions and the following disclaimer in the
//       documentation and/or other materials provided with the distribution.
//
//     * Neither the name of the copyright holder nor the
//       names of its contributors may be used to endorse or promote products
//       derived from this software without specific prior written permission.
//
// THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
// AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
// IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
// ARE DISCLAIMED. IN NO EVENT SHALL COPYRIGHT HOLDERS OR CONTRIBUTORS BE LIABLE
// FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
// (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
// LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
// ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
// (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
// SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

#ifndef POSELIB_JAC_ACC_H_
#define POSELIB_JAC_ACC_H_

#include "../../types.h"
#include "../robust_loss.h"
#include "optim_utils.h"

#include <memory>
#include <type_traits>

namespace poselib {

/*
Aggregator for jacobians for optimization.
Only store matrices for normal equations (J'*J)x = J'*r
This is separated out from the LM implementation to allow for testing.
TODO: Try something like QRAccumulator that solve J*x = r with QR
*/
class NormalAccumulator {
  public:
    NormalAccumulator() {}
    ~NormalAccumulator() {}

    // This must be called before any other method!
    void initialize(int num_params, std::shared_ptr<RobustLoss> loss = nullptr) {
        JtJ.resize(num_params, num_params);
        Jtr.resize(num_params, 1);
        loss_fcn = loss;
        if (loss_fcn.get() == nullptr) {
            loss_fcn.reset(new TrivialLoss());
        }
    }

    void reset_residual() {
        residual_acc = 0;
        residual_count = 0;
    }
    inline void add_residual(const double res, const double w = 1.0) {
        residual_acc += w * loss_fcn->loss(res * res);
        residual_count++;
    }
    // [YYIL-MSVC-FIX] Same MSVC overload-resolution issue as add_jacobian
    // below -- generalized to any non-arithmetic (Eigen expression) type.
    template <typename ResidualType,
              typename = typename std::enable_if<!std::is_arithmetic<ResidualType>::value>::type>
    inline void add_residual(const ResidualType &res, const double w = 1.0) {
        const double r_squared = res.squaredNorm();
        residual_acc += w * loss_fcn->loss(r_squared);
        residual_count++;
    }
    double get_residual() const { return residual_acc * residual_scale(); }

    void reset_jacobian() {
        residual_count = 0;
        JtJ.setZero();
        Jtr.setZero();
    }
    // [YYIL-MSVC-FIX] Collapsed from 3 separate templated overloads (one for
    // Matrix<ResidualDim,1>+Matrix<ResidualDim,ParamsDim>, one for
    // Matrix<ResidualDim,1>+MatrixXd) into a single generic template. MSVC's
    // template partial ordering could not consistently pick a best match
    // between those overloads for real call sites in this codebase (e.g. a
    // fixed-rows/dynamic-cols Matrix<double,2,Eigen::Dynamic> jac, or a
    // Matrix<double,2,9> jac alongside a Vector2d res), reporting "no
    // matching overloaded function"/deduction-failure errors that GCC/Clang
    // (this project's normal CI compilers) do not hit. The math itself only
    // ever uses generic Eigen expression operations (squaredNorm/cols/col/
    // transpose) against dynamically-sized JtJ/Jtr, so a single template
    // covering any Eigen expression type is both correct and unambiguous.
    // enable_if excludes arithmetic types so this can never compete with the
    // scalar-residual "1-dim" overload below for a plain `double res` call.
    template <typename ResidualType, typename JacType,
              typename = typename std::enable_if<!std::is_arithmetic<ResidualType>::value>::type>
    inline void add_jacobian(const ResidualType &res, const JacType &jac, const double w = 1.0) {
        const double r_squared = res.squaredNorm();
        const double weight = w * loss_fcn->weight(r_squared);
        if (weight == 0) {
            return;
        }
        for (int i = 0; i < jac.cols(); ++i) {
            for (int j = 0; j <= i; ++j) {
                JtJ(i, j) += weight * (jac.col(i).dot(jac.col(j)));
            }
        }
        Jtr += jac.transpose() * (weight * res);
        residual_count++;
    }

    // Residuals that are 1-dim
    template <int ParamsDim, int StorageOrder, int MaxRows, int MaxCols>
    inline void add_jacobian(const double res,
                             const Eigen::Matrix<double, 1, ParamsDim, StorageOrder, MaxRows, MaxCols> &jac,
                             const double w = 1.0) {
        const double r_squared = res * res;
        const double weight = w * loss_fcn->weight(r_squared);
        if (weight == 0) {
            return;
        }
        for (int i = 0; i < jac.cols(); ++i) {
            for (int j = 0; j <= i; ++j) {
                JtJ(i, j) += weight * (jac(i) * jac(j));
            }
        }
        Jtr += (weight * res) * jac.transpose();
        residual_count++;
    }

    double grad_norm() const { return residual_scale() * Jtr.norm(); }

    Eigen::VectorXd solve(double lambda, BundleOptions::DampingType damping = BundleOptions::LEVENBERG) {
        const double scale = residual_scale();
        Eigen::MatrixXd scaled_JtJ = scale * JtJ;
        if (damping == BundleOptions::MARQUARDT) {
            for (int i = 0; i < scaled_JtJ.cols(); ++i) {
                scaled_JtJ(i, i) += std::max(scaled_JtJ(i, i) * lambda, 1e-8);
            }
        } else {
            for (int i = 0; i < scaled_JtJ.cols(); ++i) {
                scaled_JtJ(i, i) += lambda;
            }
        }
        return scaled_JtJ.template selfadjointView<Eigen::Lower>().llt().solve(-(scale * Jtr));
    }

    // Predicted decrease from the quadratic model: -step' * (lambda * step + scaled_Jtr)
    double predicted_decrease(const Eigen::VectorXd &step, double lambda) const {
        const double scale = residual_scale();
        return -step.dot(lambda * step + scale * Jtr);
    }

    double residual_acc;
    std::shared_ptr<RobustLoss> loss_fcn;
    size_t residual_count = 0;
    Eigen::Matrix<double, Eigen::Dynamic, Eigen::Dynamic> JtJ;
    Eigen::Matrix<double, Eigen::Dynamic, 1> Jtr;

  private:
    double residual_scale() const { return 1.0 / std::max(1.0, static_cast<double>(residual_count)); }
};

} // namespace poselib

#endif
