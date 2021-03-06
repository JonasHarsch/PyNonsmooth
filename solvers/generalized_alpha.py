#-----------------------------------------------------------------------
# A nonsmooth generalized-alpha method for mechanical systems
# with frictional contact
# 
# Giuseppe Capobianco, Jonas Harsch, Simon R. Eugster, Remco I. Leine
#-----------------------------------------------------------------------
# Int J Numer Methods Eng. 2021; 1– 30. https://doi.org/10.1002/nme.6801
#-----------------------------------------------------------------------
# 
# This file implements the generalized-alpha method as described in our
# paper. All equation numbers found in the comments refer to the paper.
# For this implementation, we chose readability over performance, i.e,
# we aimed at an implementation that is as close as possible to the 
# equations found in our paper. 
#
# Stuttgart, September 2021                      G.Capobianco, J. Harsch

import numpy as np
from numpy.linalg import norm, solve
from tqdm import tqdm

from .numerical_derivative import approx_fprime

# eqn. (25): proximal point function to the set of negative numbers including zero
def prox_Rn0(x):
    return np.minimum(x, 0)

# eqn. (25): proximal point function to a sphere 
def prox_sphere(x, radius):
    nx = norm(x)
    if nx > 0:
        return x if nx <= radius else radius * x / nx
    else: 
        return x if nx <= radius else radius * x

class Generalized_alpha():
    """Generalized-alpha solver for mechanical systems with frictional contact.
    """
    def __init__(self, model, t0, t1, dt, rho_inf=1, method='newton',
                 newton_tol=1e-6, newton_max_iter=100, error_function=lambda x: np.max(np.abs(x)),
                 fixed_point_tol=1e-6, fixed_point_max_iter=1000):
        
        self.model = model

        # initial time, final time, time step
        self.t0 = t0
        self.t1 = t1 if t1 > t0 else ValueError("t1 must be larger than initial time t0.")
        self.dt = dt

        # eqn. (72): parameters
        self.rho_inf = rho_inf
        self.alpha_m = (2 * rho_inf - 1) / (rho_inf + 1)
        self.alpha_f = rho_inf / (rho_inf + 1)
        self.gamma = 0.5 + self.alpha_f - self.alpha_m
        self.beta = 0.25 * (0.5 + self.gamma)**2

        # newton settings
        self.newton_tol = newton_tol
        self.newton_max_iter = newton_max_iter
        self.newton_error_function = error_function

        # fixed point settings
        self.fixed_point_tol = fixed_point_tol
        self.fixed_point_max_iter = fixed_point_max_iter
        self.fixed_point_error_function = error_function

        # dimensions (nq = number of coordinates q, etc.)
        self.nq = model.nq
        self.nu = model.nu
        self.nla_g = model.nla_g
        self.nla_gamma = model.nla_gamma
        self.nla_N = model.nla_N
        self.nla_F = model.nla_F

        # eqn. (127): dimensions of residual
        self.nR_s = 3 * self.nu + 3 * self.nla_g + 2 * self.nla_gamma
        self.nR_c = 3 * self.nla_N + 2 * self.nla_F
        self.nR = self.nR_s + self.nR_c

        # set initial conditions
        self.ti = t0
        self.qi = model.q0 
        self.ui = model.u0 
        self.kappa_gi = np.zeros_like(model.la_g0)
        self.la_gi = model.la_g0
        self.La_gi = np.zeros_like(model.la_g0)
        self.la_gammai = model.la_gamma0
        self.La_gammai = np.zeros_like(model.la_gamma0)
        self.kappa_Ni = np.zeros_like(model.la_N0)
        self.la_Ni = model.la_N0
        self.La_Ni = np.zeros_like(model.la_N0)
        self.la_Fi = model.la_F0
        self.La_Fi = np.zeros_like(model.la_F0)
        self.Qi = np.zeros(self.nu)
        self.Ui = np.zeros(self.nu)

        # solve for initial accelerations
        self.ai = solve(model.M(t0, model.q0), self.model.h(t0, model.q0, model.u0) + self.model.W_g(t0, model.q0) @ model.la_g0 + self.model.W_gamma(t0, model.q0) @ model.la_gamma0 + self.model.W_N(t0, model.q0) @ model.la_N0 + self.model.W_F(t0, model.q0) @ model.la_F0 )

        # initialize auxilary variables
        self.a_bari = self.ai.copy()
        self.la_Nbari = self.la_Ni.copy()
        self.la_Fbari = self.la_Fi.copy()

        # initialize index sets
        self.Ai1 = np.zeros(self.nla_N, dtype=bool)
        self.Bi1 = np.zeros(self.nla_N, dtype=bool)
        self.Ci1 = np.zeros(self.nla_N, dtype=bool)
        self.Di1_st = np.zeros(self.nla_N, dtype=bool)
        self.Ei1_st = np.zeros(self.nla_N, dtype=bool)

        if method == 'fixed-point':
            self.step = self.step_fixed_point
            self.max_iter = self.fixed_point_max_iter
        elif method == 'newton':
            self.step = self.step_newton
            self.max_iter = self.newton_max_iter

        # function called at the end of each time step. Can for example be 
        # used to norm quaternions at the end of each time step.
        if hasattr(model, 'step_callback'):
            self.step_callback = model.step_callback
        else:
            self.step_callback = self.__step_callback
    
    # identity map as standard callback function 
    def __step_callback(self, q, u):
        return q, u

    def R(self, x, update_index_set=False):
        """Residual R=(R_s, R_c), see eqn. (127)
        """
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma
        nla_N = self.nla_N
        nla_F = self.nla_F
        nR_s = self.nR_s

        mu = self.model.mu
        dt = self.dt
        dt2 = self.dt**2
        ti1 = self.ti + dt

        # eqn. (126): unpack vector x
        ai1 = x[:nu]
        Ui1 = x[nu:2*nu]
        Qi1 = x[2*nu:3*nu]
        kappa_gi1 = x[3*nu:3*nu+nla_g]
        La_gi1 = x[3*nu+nla_g:3*nu+2*nla_g]
        la_gi1 = x[3*nu+2*nla_g:3*nu+3*nla_g]
        La_gammai1 = x[3*nu+3*nla_g:3*nu+3*nla_g+nla_gamma]
        la_gammai1 = x[3*nu+3*nla_g+nla_gamma:3*nu+3*nla_g+2*nla_gamma]
        kappa_Ni1 = x[nR_s:nR_s+nla_N]
        La_Ni1 = x[nR_s+nla_N:nR_s+2*nla_N]
        la_Ni1 = x[nR_s+2*nla_N:nR_s+3*nla_N]
        La_Fi1 = x[nR_s+3*nla_N:nR_s+3*nla_N+nla_F]
        la_Fi1 = x[nR_s+3*nla_N+nla_F:nR_s+3*nla_N+2*nla_F]
        
        # ----- kinematic variables -----
        # eqn. (71): compute auxiliary acceleration variables
        a_bari1 = (self.alpha_f * self.ai + (1 - self.alpha_f) * ai1 - self.alpha_m * self.a_bari) / (1 - self.alpha_m)

        # eqn. (73): velocity update formula
        ui1 = self.ui + dt * ((1 - self.gamma) * self.a_bari + self.gamma * a_bari1) + Ui1

        # eqn. (125): generalized position update formula
        a_beta = (1 - 2 * self.beta) * self.a_bari + 2 * self.beta * a_bari1
        qi1 = self.qi + dt * self.model.q_dot(self.ti, self.qi, self.ui) \
              + dt2 / 2 * self.model.q_ddot(self.ti, self.qi, self.ui, a_beta) \
              + self.model.B(self.ti, self.qi) @ Qi1 

        # ----- normal contact forces -----
        # eqn. (96): compute auxiliary normal contact forces
        la_Nbari1 = (self.alpha_f * self.la_Ni + (1 - self.alpha_f) * la_Ni1 - self.alpha_m * self.la_Nbari) / (1 - self.alpha_m)

        # eqn. (95): compute normal percussions
        P_Ni1 = La_Ni1 + dt * ((1 - self.gamma) * self.la_Nbari + self.gamma * la_Nbari1)

        # eqn. (102): 
        kappa_hatNi1 = kappa_Ni1 + dt2 * ((0.5 - self.beta) * self.la_Nbari + self.beta * la_Nbari1)
        
        # ----- frictions forces -----
        # eqn. (114): compute auxiliary friction forces
        la_Fbari1 = (self.alpha_f * self.la_Fi + (1 - self.alpha_f) * la_Fi1 - self.alpha_m * self.la_Fbari) / (1 - self.alpha_m)

        # eqn. (113): compute frictional percussions
        P_Fi1 = La_Fi1 + dt * ((1 - self.gamma) * self.la_Fbari + self.gamma * la_Fbari1)

        # ----- get quantities from model -----
        # Mass matrix
        Mi1 = self.model.M(ti1, qi1)

        # generalized force directions
        W_gi1 = self.model.W_g(ti1, qi1)
        W_gammai1 = self.model.W_gamma(ti1, qi1)
        W_Ni1 = self.model.W_N(ti1, qi1)
        W_Fi1 = self.model.W_F(ti1, qi1)

        # kinematic quantities of contacts
        g_Ni1 = self.model.g_N(ti1, qi1)
        xi_Ni1 = self.model.xi_N(ti1, qi1, self.ui, ui1)
        xi_Fi1 = self.model.xi_F(ti1, qi1, self.ui, ui1)
        g_N_ddoti1 = self.model.g_N_ddot(ti1, qi1, ui1, ai1)
        gamma_Fi1 = self.model.gamma_F(ti1, qi1, ui1)
        gamma_F_doti1 = self.model.gamma_F_dot(ti1, qi1, ui1, ai1)
        
        # ----- compute residual -----
        R = np.zeros(self.nR)

        # eqn. (127): R_s
        R[:nu] = Mi1 @ ai1 - self.model.h(ti1, qi1, ui1) - W_gi1 @ la_gi1 - W_gammai1 @ la_gammai1 - W_Ni1 @ la_Ni1 - W_Fi1 @ la_Fi1
        R[nu:2*nu] = Mi1 @ Ui1 - W_gi1 @ La_gi1 - W_gammai1 @ La_gammai1 - W_Ni1 @ La_Ni1 - W_Fi1 @ La_Fi1
        R[2*nu:3*nu] = Mi1 @ Qi1 - W_gi1 @ kappa_gi1 - W_Ni1 @ kappa_Ni1 - 0.5 * dt * (W_gammai1 @ La_gammai1 + W_Fi1 @ La_Fi1)
        R[3*nu:3*nu+nla_g] = self.model.g(ti1, qi1)
        R[3*nu+nla_g:3*nu+2*nla_g] = self.model.g_dot(ti1, qi1, ui1)
        R[3*nu+2*nla_g:3*nu+3*nla_g] = self.model.g_ddot(ti1, qi1, ui1, ai1)
        R[3*nu+3*nla_g:3*nu+3*nla_g+nla_gamma] = self.model.gamma(ti1, qi1, ui1)
        R[3*nu+3*nla_g+nla_gamma:3*nu+3*nla_g+2*nla_gamma] = self.model.gamma_dot(ti1, qi1, ui1, ai1)

        # update index sets
        if update_index_set:
            # eqn. (130):
            self.Ai1 = (self.model.prox_r_N * g_Ni1 - kappa_hatNi1 <= 0)
            # eqn. (133):
            self.Bi1 = self.Ai1 * ( (self.model.prox_r_N * xi_Ni1 - P_Ni1) <= 0 )
            # eqn. (136):
            self.Ci1 = self.Bi1 * ( (self.model.prox_r_N * g_N_ddoti1 - la_Ni1) <= 0 )

            for i_N, i_F in enumerate(self.model.NF_connectivity):
                i_F = np.array(i_F)
                # eqn. (139):
                self.Di1_st[i_N] = self.Ai1[i_N] and ( norm(self.model.prox_r_F[i_N] * xi_Fi1[i_F] - P_Fi1[i_F]) <= mu[i_N] * P_Ni1[i_N] )
                # eqn. (141):
                self.Ei1_st[i_N] = self.Di1_st[i_N] and ( norm(self.model.prox_r_F[i_N] * gamma_F_doti1[i_F] - la_Fi1[i_F]) <= mu[i_N] * la_Ni1[i_N] )
        
        # eqn. (129):
        Ai1 = self.Ai1
        Ai1_ind = np.where(Ai1)[0]
        _Ai1_ind = np.where(~Ai1)[0]
        R[nR_s+Ai1_ind] = g_Ni1[Ai1]
        R[nR_s+_Ai1_ind] = kappa_hatNi1[~Ai1]

        # eqn. (132):
        Bi1 = self.Bi1
        Bi1_ind = np.where(Bi1)[0]
        _Bi1_ind = np.where(~Bi1)[0]
        R[nR_s+nla_N+Bi1_ind] = xi_Ni1[Bi1]
        R[nR_s+nla_N+_Bi1_ind] = P_Ni1[~Bi1]

        # eqn. (135):
        Ci1 = self.Ci1
        Ci1_ind = np.where(Ci1)[0]
        _Ci1_ind = np.where(~Ci1)[0]
        R[nR_s+2*nla_N+Ci1_ind] = g_N_ddoti1[Ci1]
        R[nR_s+2*nla_N+_Ci1_ind] = la_Ni1[~Ci1]

        # eqn. (138) and (142):
        Di1_st = self.Di1_st
        Ei1_st = self.Ei1_st

        for i_N, i_F in enumerate(self.model.NF_connectivity):
            i_F = np.array(i_F)
            if Ai1[i_N]:                    
                if Di1_st[i_N]:
                    # eqn. (138a)
                    R[nR_s+3*nla_N+i_F] = xi_Fi1[i_F]
                    
                    if Ei1_st[i_N]:
                        # eqn. (142a)
                        R[nR_s+3*nla_N+nla_F+i_F] = gamma_F_doti1[i_F]
                    else:
                        # eqn. (142b)
                        norm_gamma_Fdoti1 = norm(gamma_F_doti1[i_F])
                        if norm_gamma_Fdoti1 > 0:
                            R[nR_s+3*nla_N+nla_F+i_F] = la_Fi1[i_F] + mu[i_N] * la_Ni1[i_N] * gamma_F_doti1[i_F] / norm_gamma_Fdoti1
                        else:
                            R[nR_s+3*nla_N+nla_F+i_F] = la_Fi1[i_F] + mu[i_N] * la_Ni1[i_N] * gamma_F_doti1[i_F]
                else:
                    # eqn. (138b)
                    norm_xi_Fi1 = norm(xi_Fi1[i_F])
                    if norm_xi_Fi1 > 0:
                        R[nR_s+3*nla_N+i_F] = P_Fi1[i_F] + mu[i_N] * P_Ni1[i_N] * xi_Fi1[i_F] / norm_xi_Fi1
                    else:
                        R[nR_s+3*nla_N+i_F] = P_Fi1[i_F] + mu[i_N] * P_Ni1[i_N] * xi_Fi1[i_F]

                    # eqn. (142c)
                    norm_gamma_Fi1 = norm(gamma_Fi1[i_F])
                    if norm_gamma_Fi1 > 0:
                        R[nR_s+3*nla_N+nla_F+i_F] = la_Fi1[i_F] + mu[i_N] * la_Ni1[i_N] * gamma_Fi1[i_F] / norm_gamma_Fi1
                    else:
                        R[nR_s+3*nla_N+nla_F+i_F] = la_Fi1[i_F] + mu[i_N] * la_Ni1[i_N] * gamma_Fi1[i_F]
            else:
                # eqn. (138c)
                R[nR_s+3*nla_N+i_F] = P_Fi1[i_F]
                # eqn. (142d)
                R[nR_s+3*nla_N+nla_F+i_F] = la_Fi1[i_F]
                
        return R
    
    def R_s(self, y, z):
        """Residual R_s, see eqn. (127), as function of y and z given by (144).
        """
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma
        nla_N = self.nla_N
        nla_F = self.nla_F

        dt = self.dt
        dt2 = self.dt**2
        ti1 = self.ti + dt

        # eqn. (144): unpack vectors y and z
        ai1 = y[:nu]
        Ui1 = y[nu:2*nu]
        Qi1 = y[2*nu:3*nu]
        kappa_gi1 = y[3*nu:3*nu+nla_g]
        La_gi1 = y[3*nu+nla_g:3*nu+2*nla_g]
        la_gi1 = y[3*nu+2*nla_g:3*nu+3*nla_g]
        La_gammai1 = y[3*nu+3*nla_g:3*nu+3*nla_g+nla_gamma]
        la_gammai1 = y[3*nu+3*nla_g+nla_gamma:3*nu+3*nla_g+2*nla_gamma]
        kappa_Ni1 = z[:nla_N]
        La_Ni1 = z[nla_N:2*nla_N]
        la_Ni1 = z[2*nla_N:3*nla_N]
        La_Fi1 = z[3*nla_N:3*nla_N+nla_F]
        la_Fi1 = z[3*nla_N+nla_F:3*nla_N+2*nla_F]

        # ----- kinematic variables -----
        # eqn. (71): compute auxiliary acceleration variables
        a_bari1 = (self.alpha_f * self.ai + (1 - self.alpha_f) * ai1 - self.alpha_m * self.a_bari) / (1 - self.alpha_m)

        # eqn. (73): velocity update formula
        ui1 = self.ui + dt * ((1 - self.gamma) * self.a_bari + self.gamma * a_bari1) + Ui1

        # eqn. (125): generalized position update formula
        a_beta = (1 - 2 * self.beta) * self.a_bari + 2 * self.beta * a_bari1
        qi1 = self.qi + dt * self.model.q_dot(self.ti, self.qi, self.ui) \
              + dt2 / 2 * self.model.q_ddot(self.ti, self.qi, self.ui, a_beta) \
              + self.model.B(self.ti, self.qi) @ Qi1 

        # ----- get quantities from model -----
        # Mass matrix
        Mi1 = self.model.M(ti1, qi1)

        # generalized force directions
        W_gi1 = self.model.W_g(ti1, qi1)
        W_gammai1 = self.model.W_gamma(ti1, qi1)
        W_Ni1 = self.model.W_N(ti1, qi1)
        W_Fi1 = self.model.W_F(ti1, qi1)
        
        # ----- compute residual -----
        R_s = np.zeros(self.nR_s)
        R_s[:nu] = Mi1 @ ai1 - self.model.h(ti1, qi1, ui1) - W_gi1 @ la_gi1 - W_gammai1 @ la_gammai1 - W_Ni1 @ la_Ni1 - W_Fi1 @ la_Fi1
        R_s[nu:2*nu] = Mi1 @ Ui1 - W_gi1 @ La_gi1 - W_gammai1 @ La_gammai1 - W_Ni1 @ La_Ni1 - W_Fi1 @ La_Fi1
        R_s[2*nu:3*nu] = Mi1 @ Qi1 - W_gi1 @ kappa_gi1 - W_Ni1 @ kappa_Ni1 - 0.5 * dt * (W_gammai1 @ La_gammai1 + W_Fi1 @ La_Fi1)
        R_s[3*nu:3*nu+nla_g] = self.model.g(ti1, qi1)
        R_s[3*nu+nla_g:3*nu+2*nla_g] = self.model.g_dot(ti1, qi1, ui1)
        R_s[3*nu+2*nla_g:3*nu+3*nla_g] = self.model.g_ddot(ti1, qi1, ui1, ai1)
        R_s[3*nu+3*nla_g:3*nu+3*nla_g+nla_gamma] = self.model.gamma(ti1, qi1, ui1)
        R_s[3*nu+3*nla_g+nla_gamma:3*nu+3*nla_g+2*nla_gamma] = self.model.gamma_dot(ti1, qi1, ui1, ai1)

        return R_s

    def p(self, y, z):
        '''map p(y,z) used in (146).'''
        nu = self.nu
        nla_N = self.nla_N
        nla_F = self.nla_F

        mu = self.model.mu

        dt = self.dt
        dt2 = self.dt**2
        ti1 = self.ti + dt

        # eqn. (144): read kinematic variables from y 
        ai1 = y[:nu]
        Ui1 = y[nu:2*nu]
        Qi1 = y[2*nu:3*nu]

        # ----- kinematic variables -----
        # eqn. (71): compute auxiliary acceleration variables
        a_bari1 = (self.alpha_f * self.ai + (1 - self.alpha_f) * ai1 - self.alpha_m * self.a_bari) / (1 - self.alpha_m)

        # eqn. (73): velocity update formula
        ui1 = self.ui + dt * ((1 - self.gamma) * self.a_bari + self.gamma * a_bari1) + Ui1

        # eqn. (125): generalized position update formula
        a_beta = (1 - 2 * self.beta) * self.a_bari + 2 * self.beta * a_bari1
        qi1 = self.qi + dt * self.model.q_dot(self.ti, self.qi, self.ui) \
              + dt2 / 2 * self.model.q_ddot(self.ti, self.qi, self.ui, a_beta) \
              + self.model.B(self.ti, self.qi) @ Qi1 

        # ----- get quantities from model -----
        # kinematic quantities of contacts
        g_N = self.model.g_N(ti1, qi1)
        xi_N = self.model.xi_N(ti1, qi1, self.ui, ui1)
        g_N_ddot = self.model.g_N_ddot(ti1, qi1, ui1, ai1)
        gamma_F = self.model.gamma_F(ti1, qi1, ui1)
        xi_F = self.model.xi_F(ti1, qi1, self.ui, ui1)
        gamma_F_dot = self.model.gamma_F_dot(ti1, qi1, ui1, ai1)

        # ----- eqn. (146): fixed point update -----
        # For convenience, we call the iteration index j instead of mu.

        # eqn. (144): unpack vector z
        kappa_Ni1_j = z[:nla_N]
        La_Ni1_j = z[nla_N:2*nla_N]
        la_Ni1_j = z[2*nla_N:3*nla_N]
        La_Fi1_j = z[3*nla_N:3*nla_N+nla_F]
        la_Fi1_j = z[3*nla_N+nla_F:3*nla_N+2*nla_F]

        # eqn. (96): compute auxiliary normal contact forces
        la_Nbari1 = (self.alpha_f * self.la_Ni + (1 - self.alpha_f) * la_Ni1_j - self.alpha_m * self.la_Nbari) / (1 - self.alpha_m)
        # eqn. (95): compute normal percussions
        P_N_j = La_Ni1_j + dt * ((1 - self.gamma) * self.la_Nbari + self.gamma * la_Nbari1)
        # eqn. (102):
        kappa_hatN_j = kappa_Ni1_j + dt2 * ((0.5 - self.beta) * self.la_Nbari + self.beta * la_Nbari1)
        
        # eqn. (114): compute auxiliary friction forces
        la_Fbari1 = (self.alpha_f * self.la_Fi + (1 - self.alpha_f) * la_Fi1_j - self.alpha_m * self.la_Fbari) / (1 - self.alpha_m)
        # eqn. (113): compute frictional percussions
        P_F_j = La_Fi1_j + dt * ((1 - self.gamma) * self.la_Fbari + self.gamma * la_Fbari1)
        
        # -- prox normal direction --
        P_N_j1 = np.zeros(self.nla_N)
        la_Ni1_j1 = np.zeros(self.nla_N)
        
        # eqn. (128):
        prox_arg = self.model.prox_r_N * g_N - kappa_hatN_j
        kappa_hatN_j1 = - prox_Rn0(prox_arg)
        # eqn. (130):
        Ai1 = (prox_arg <= 0 )

        # eqn. (131):
        prox_arg =  self.model.prox_r_N * xi_N - P_N_j
        P_N_j1[Ai1] = - prox_Rn0(prox_arg[Ai1])
        # eqn. (133):
        Bi1 = (prox_arg <= 0) * Ai1

        # eqn. (134):
        la_Ni1_j1[Bi1] = - prox_Rn0(self.model.prox_r_N[Bi1] * g_N_ddot[Bi1] - la_Ni1_j[Bi1])

        # -- prox friction --
        P_F_j1 = np.zeros(self.nla_F)
        la_Fi1_j1 = np.zeros(self.nla_F)

        for i_N, i_F in enumerate(self.model.NF_connectivity):
            i_F = np.array(i_F)
            if Ai1[i_N]:
                # eqn. (137):
                prox_arg = self.model.prox_r_F[i_N] * xi_F[i_F] - P_F_j[i_F]
                radius = mu[i_N] * P_N_j1[i_N]
                P_F_j1[i_F] = - prox_sphere(prox_arg, radius)
                
                # eqn. (139): if contact index is in D_st
                if norm(prox_arg) <= radius:
                    # eqn. (140a):
                    prox_arg_acc = self.model.prox_r_F[i_N] * gamma_F_dot[i_F] - la_Fi1_j[i_F]
                    radius_acc = mu[i_N] * la_Ni1_j1[i_N]
                    la_Fi1_j1[i_F] = - prox_sphere(prox_arg_acc, radius_acc)
                else:
                    # eqn. (140b):
                    norm_gamma_F = norm(gamma_F[i_F])
                    if norm_gamma_F > 0:
                        la_Fi1_j1[i_F] = - mu[i_N] * la_Ni1_j1[i_N] * gamma_F[i_F] / norm_gamma_F
                    else:
                        la_Fi1_j1[i_F] = - mu[i_N] * la_Ni1_j1[i_N] * gamma_F[i_F]

        # -- update contact forces --
        # eqn. (96): compute auxiliary normal contact forces
        la_Nbari1 = (self.alpha_f * self.la_Ni + (1 - self.alpha_f) * la_Ni1_j1 - self.alpha_m * self.la_Nbari) / (1 - self.alpha_m)
        # eqn. (95): compute normal percussions
        La_Ni1_j1 = P_N_j1 - dt * ((1 - self.gamma) * self.la_Nbari + self.gamma * la_Nbari1)
        # eqn. (102):
        kappa_Ni1_j1 = kappa_hatN_j1 - dt2 * ((0.5 - self.beta) * self.la_Nbari + self.beta * la_Nbari1)
        
        # eqn. (114): compute auxiliary friction forces
        la_Fbari1 = (self.alpha_f * self.la_Fi + (1 - self.alpha_f) * la_Fi1_j1 - self.alpha_m * self.la_Fbari) / (1 - self.alpha_m)
        # eqn. (113): compute frictional percussions
        La_Fi1_j1 = P_F_j1 - dt * ((1 - self.gamma) * self.la_Fbari + self.gamma * la_Fbari1)

        # eqn. (144): pack z vector
        z = np.zeros(self.nR_c)
        z[:nla_N] = kappa_Ni1_j1
        z[nla_N:2*nla_N] = La_Ni1_j1
        z[2*nla_N:3*nla_N] = la_Ni1_j1
        z[3*nla_N:3*nla_N+nla_F] = La_Fi1_j1
        z[3*nla_N+nla_F:3*nla_N+2*nla_F] = la_Fi1_j1

        return z

    def step_newton(self):
        '''Time step with semismooth Newton method'''
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma
        nla_N = self.nla_N
        nla_F = self.nla_F
        nR_s = self.nR_s
        dt = self.dt
        ti1 = self.ti + dt

        # eqn. (126): initialize x vector with quanitites from previous time step
        x = np.zeros(self.nR)
        x[:nu] = self.ai
        x[nu:2*nu] = self.Ui
        x[2*nu:3*nu] = self.Qi
        x[3*nu:3*nu+nla_g] = self.kappa_gi
        x[3*nu+nla_g:3*nu+2*nla_g] = self.La_gi
        x[3*nu+2*nla_g:3*nu+3*nla_g] = self.la_gi
        x[3*nu+3*nla_g:3*nu+3*nla_g+nla_gamma] = self.La_gammai
        x[3*nu+3*nla_g+nla_gamma:3*nu+3*nla_g+2*nla_gamma] = self.la_gammai
        x[nR_s:nR_s+nla_N] = self.kappa_Ni
        x[nR_s+nla_N:nR_s+2*nla_N] = self.La_Ni
        x[nR_s+2*nla_N:nR_s+3*nla_N] = self.la_Ni
        x[nR_s+3*nla_N:nR_s+3*nla_N+nla_F] = self.La_Fi
        x[nR_s+3*nla_N+nla_F:nR_s+3*nla_N+2*nla_F] = self.la_Fi

        # initial residual and error
        R = self.R(x, update_index_set=True)
        error = self.newton_error_function(R)
        converged = error < self.newton_tol
        j = 0
        # iterate Newton update until converged or max_iter reached
        if not converged:
            for j in range(self.newton_max_iter):
                # jacobian
                R_x = approx_fprime(x, self.R, method='2-point', eps=1.0e-6)
                
                # eqn. (143): Newton update
                try:
                    x -= solve(R_x, R)
                except:
                    print(f'Failed to invert R at time t = {ti1}.')
                    converged = False
                    break
                
                R = self.R(x, update_index_set=True)
                error = self.newton_error_function(R)
                converged = error < self.newton_tol
                if converged:
                    break
        
        # eqn. (126): unpack converged vector x
        ai1 = x[:nu]
        Ui1 = x[nu:2*nu]
        Qi1 = x[2*nu:3*nu]
        kappa_gi1 = x[3*nu:3*nu+nla_g]
        La_gi1 = x[3*nu+nla_g:3*nu+2*nla_g]
        la_gi1 = x[3*nu+2*nla_g:3*nu+3*nla_g]
        La_gammai1 = x[3*nu+3*nla_g:3*nu+3*nla_g+nla_gamma]
        la_gammai1 = x[3*nu+3*nla_g+nla_gamma:3*nu+3*nla_g+2*nla_gamma]
        kappa_Ni1 = x[nR_s:nR_s+nla_N]
        La_Ni1 = x[nR_s+nla_N:nR_s+2*nla_N]
        la_Ni1 = x[nR_s+2*nla_N:nR_s+3*nla_N]
        La_Fi1 = x[nR_s+3*nla_N:nR_s+3*nla_N+nla_F]
        la_Fi1 = x[nR_s+3*nla_N+nla_F:nR_s+3*nla_N+2*nla_F]
            
        return (converged, j, error), ti1, ai1, Ui1, Qi1, kappa_gi1, La_gi1, la_gi1, La_gammai1, la_gammai1, kappa_Ni1, La_Ni1, la_Ni1, La_Fi1, la_Fi1

    def step_fixed_point(self):
        '''Time step with fixed point iterations'''
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma
        nla_N = self.nla_N
        nla_F = self.nla_F
        nR_s = self.nR_s
        dt = self.dt
        ti1 = self.ti + dt

        # eqn. (126): initialize x vector with quanitites from previous time step
        x = np.zeros(self.nR)
        x[:nu] = self.ai
        x[nu:2*nu] = self.Ui
        x[2*nu:3*nu] = self.Qi
        x[3*nu:3*nu+nla_g] = self.kappa_gi
        x[3*nu+nla_g:3*nu+2*nla_g] = self.La_gi
        x[3*nu+2*nla_g:3*nu+3*nla_g] = self.la_gi
        x[3*nu+3*nla_g:3*nu+3*nla_g+nla_gamma] = self.La_gammai
        x[3*nu+3*nla_g+nla_gamma:3*nu+3*nla_g+2*nla_gamma] = self.la_gammai
        x[nR_s:nR_s+nla_N] = self.kappa_Ni
        x[nR_s+nla_N:nR_s+2*nla_N] = self.La_Ni
        x[nR_s+2*nla_N:nR_s+3*nla_N] = self.la_Ni
        x[nR_s+3*nla_N:nR_s+3*nla_N+nla_F] = self.La_Fi
        x[nR_s+3*nla_N+nla_F:nR_s+3*nla_N+2*nla_F] = self.la_Fi

        # eqn. (144): split variables
        y = x[:nR_s]
        z = x[nR_s:]

        # eqn. (145): Newton iterations for update of non-contact variables
        fixed_point_error = None
        fixed_point_converged = False
        j = 0
        for j in range(self.fixed_point_max_iter):
            R_s = self.R_s(y, z)
            newton_error = self.newton_error_function(R_s)
            newton_converged = newton_error < self.newton_tol
            if not newton_converged:
                for _ in range(self.newton_max_iter):
                    # jacobian
                    R_s_y = approx_fprime(y, lambda y: self.R_s(y, z), method='2-point', eps=1.0e-6)
                    
                    # Newton update
                    y -= solve(R_s_y, R_s)

                    R_s = self.R_s(y, z)
                    newton_error = self.newton_error_function(R_s)
                    newton_converged = newton_error < self.newton_tol
                    if newton_converged:
                        break
                if not newton_converged:
                    raise RuntimeError(f'Newton method in {j}-th fixed-point iteration not converged.')
            
            # eqn. (146): fixed point update
            z1 = self.p(y, z)
            fixed_point_error = self.fixed_point_error_function(z1 - z)
            fixed_point_converged = fixed_point_error < self.fixed_point_tol
            z = z1.copy()

            if fixed_point_converged:
                break

        # eqn. (144): unpack converged y and z vectors
        ai1 = y[:nu]
        Ui1 = y[nu:2*nu]
        Qi1 = y[2*nu:3*nu]
        kappa_gi1 = y[3*nu:3*nu+nla_g]
        La_gi1 = y[3*nu+nla_g:3*nu+2*nla_g]
        la_gi1 = y[3*nu+2*nla_g:3*nu+3*nla_g]
        La_gammai1 = y[3*nu+3*nla_g:3*nu+3*nla_g+nla_gamma]
        la_gammai1 = y[3*nu+3*nla_g+nla_gamma:3*nu+3*nla_g+2*nla_gamma]

        kappa_Ni1 = z[:nla_N]
        La_Ni1 = z[nla_N:2*nla_N]
        la_Ni1 = z[2*nla_N:3*nla_N]
        La_Fi1 = z[3*nla_N:3*nla_N+nla_F]
        la_Fi1 = z[3*nla_N+nla_F:3*nla_N+2*nla_F]
            
        return (fixed_point_converged, j, fixed_point_error), ti1, ai1, Ui1, Qi1, kappa_gi1, La_gi1, la_gi1, La_gammai1, la_gammai1, kappa_Ni1, La_Ni1, la_Ni1, La_Fi1, la_Fi1

    def solve(self): 
        '''Method that runs the solver'''
        dt = self.dt
        dt2 = self.dt**2

        # lists storing output variables
        t = [self.ti]
        q = [self.qi]
        u = [self.ui]
        a = [self.ai]
        kappa_g = [self.kappa_gi]
        La_g = [self.La_gi]
        la_g = [self.la_gi]
        La_gamma = [self.La_gammai]
        la_gamma = [self.la_gammai]
        kappa_N = [self.kappa_Ni]
        La_N = [self.La_Ni]
        la_N = [self.la_Ni]
        La_F = [self.La_Fi]
        la_F = [self.la_Fi]
        P_N = [self.La_Ni + dt * self.la_Ni]
        P_F = [self.La_Fi + dt * self.la_Fi]

        # initialize progress bar
        pbar = tqdm(np.arange(self.t0, self.t1, self.dt))

        iter = []
        fixpt_iter = []
        # for-loop over all time steps
        for _ in pbar:
            # try to solve time step with user-defined method (method='newton' if unspecified)
            try:
                (converged, n_iter, error), ti1, ai1, Ui1, Qi1, kappa_gi1, La_gi1, la_gi1, La_gammai1, la_gammai1, kappa_Ni1, La_Ni1, la_Ni1, La_Fi1, la_Fi1 = self.step()
                pbar.set_description(f't: {ti1:0.2e}s < {self.t1:0.2e}s; {n_iter}/{self.max_iter} iterations; error: {error:0.2e}')
                if not converged:
                    raise RuntimeError(f'step not converged after {n_iter} steps with error: {error:.5e}')
                iter.append(n_iter+1)
            except RuntimeError: # if method specified does not converge, use fixed-point iterations in time step.
                print('\nSwitched to fixed-point step.\n')
                (converged, n_iter, error), ti1, ai1, Ui1, Qi1, kappa_gi1, La_gi1, la_gi1, La_gammai1, la_gammai1, kappa_Ni1, La_Ni1, la_Ni1, La_Fi1, la_Fi1 = self.step_fixed_point()
                pbar.set_description(f't: {ti1:0.2e}s < {self.t1:0.2e}s; {n_iter}/{self.max_iter} iterations; error: {error:0.2e}')
                if not converged:
                    raise RuntimeError(f'fixed-point step not converged after {n_iter} steps with error: {error:.5e}')
                fixpt_iter.append(n_iter+1)

            # ----- compute variables for output -----
            # eqn. (121): get matrix B from model
            Bi = self.model.B(self.ti, self.qi)

            # eqn. (71): compute auxiliary acceleration variables
            a_bari1 = (self.alpha_f * self.ai + (1 - self.alpha_f) * ai1  - self.alpha_m * self.a_bari) / (1 - self.alpha_m)

            # eqn. (73): velocity update formula
            ui1 = self.ui + dt * ((1 - self.gamma) * self.a_bari + self.gamma * a_bari1) + Ui1

            # eqn. (125): generalized position update formula
            a_beta = (1 - 2 * self.beta) * self.a_bari + 2 * self.beta * a_bari1
            qi1 = self.qi + dt * self.model.q_dot(self.ti, self.qi, self.ui) \
                + dt2 / 2 * self.model.q_ddot(self.ti, self.qi, self.ui, a_beta) \
                + self.model.B(self.ti, self.qi) @ Qi1 
            
            # eqn. (96): compute auxiliary normal contact forces
            la_Nbari1 = (self.alpha_f * self.la_Ni + (1 - self.alpha_f) * la_Ni1  - self.alpha_m * self.la_Nbari) / (1 - self.alpha_m)

            # eqn. (95): compute normal percussions
            P_Ni1 = La_Ni1 + dt * ((1 - self.gamma) * self.la_Nbari + self.gamma * la_Nbari1)

            # eqn. (114): compute auxiliary friction forces
            la_Fbari1 = (self.alpha_f * self.la_Fi + (1 - self.alpha_f) * la_Fi1  - self.alpha_m * self.la_Fbari) / (1 - self.alpha_m)

            # eqn. (113): compute frictional percussions
            P_Fi1 = La_Fi1 + dt * ((1 - self.gamma) * self.la_Fbari + self.gamma * la_Fbari1)

            # function called at the end of each time step. Can for example be used to norm quaternions at the end of each time step.
            qi1, ui1 = self.step_callback(qi1, ui1)

            # append solution of time step to global output vectors
            t.append(ti1)
            q.append(qi1)
            u.append(ui1)
            a.append(ai1)
            kappa_g.append(kappa_gi1)
            La_g.append(La_gi1)
            la_g.append(la_gi1)
            La_gamma.append(La_gammai1)
            la_gamma.append(la_gammai1)
            kappa_N.append(kappa_Ni1)
            La_N.append(La_Ni1)
            la_N.append(la_Ni1)
            La_F.append(La_Fi1)
            la_F.append(la_Fi1)
            P_N.append(P_Ni1)
            P_F.append(P_Fi1)

            # update local variables for accepted time step
            self.ti = ti1
            self.qi = qi1
            self.ui = ui1
            self.ai = ai1
            self.Qi = Qi1
            self.kappa_gi = kappa_gi1
            self.La_gi = La_gi1
            self.la_gi = la_gi1
            self.La_gammai = La_gammai1
            self.la_gammai = la_gammai1
            self.kappa_Ni = kappa_Ni1
            self.La_Ni = La_Ni1
            self.la_Ni = la_Ni1
            self.La_Fi = La_Fi1
            self.la_Fi = la_Fi1
            self.a_bari = a_bari1
            self.la_Nbari = la_Nbari1
            self.la_Fbari = la_Fbari1

        # print statistics
        print('-----------------')
        print(f'Iterations per time step: max = {max(iter)}, avg={sum(iter) / float(len(iter))}')
        if len(fixpt_iter)>0:
            print('-----------------')
            print('For the time steps, where primary method did not converge:')
            print(f'Number of such time steps: {len(fixpt_iter)}')
            print(f'Fixed-point iterations: max = {max(fixpt_iter)}, avg={sum(fixpt_iter) / float(len(fixpt_iter))}')
        print('-----------------')
        # return solution
        return np.array(t), np.array(q), np.array(u), np.array(a), np.array(kappa_g), np.array(La_g), \
               np.array(la_g), np.array(La_gamma), np.array(la_gamma), np.array(kappa_N), np.array(La_N), \
               np.array(la_N), np.array(La_F), np.array(la_F), np.array(P_N), np.array(P_F)