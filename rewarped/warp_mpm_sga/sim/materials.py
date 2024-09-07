import warp as wp


MATL_PLASTICINE = wp.constant(0)
MATL_WATER = wp.constant(1)


@wp.struct
class MPMMaterial:
    name: int = 0
    E: float = None  # Young's modulus
    nu: float = None  # Poisson's ratio
    yield_stress: float = None

    mu: float = None
    lam: float = None


def get_material(
        name: str,
        E: float = None,
        nu: float = None,
        yield_stress: float = None) -> MPMMaterial:
    material = MPMMaterial()
    if name == 'plasticine':
        material.name = MATL_PLASTICINE
        material.E = E
        material.nu = nu
        material.yield_stress = yield_stress
        material.mu, material.lam = get_lame(E, nu)
    elif name == 'water':
        material.name = MATL_WATER
        material.E = E
        material.nu = nu
        material.mu, material.lam = get_lame(E, nu)
    else:
        raise ValueError(type)
    return material


def get_lame(E, nu):
    mu = E / (2.0 * (1.0 + nu))
    lam = E * nu / ((1.0 + nu) * (1.0 - 2.0 * nu))
    return mu, lam


@wp.func
def svd(F: wp.mat33):
    U = wp.mat33(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    V = wp.mat33(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    sigma = wp.vec3(0.0)
    wp.svd3(F, U, sigma, V)

    U_det = wp.determinant(U)
    V_det = wp.determinant(V)

    if U_det < 0.0:
        U = wp.mat33(
            U[0, 0], U[0, 1], -U[0, 2],
            U[1, 0], U[1, 1], -U[1, 2],
            U[2, 0], U[2, 1], -U[2, 2],
        )
        sigma = wp.vec3(sigma[0], sigma[1], -sigma[2])
    if V_det < 0.0:
        V = wp.mat33(
            V[0, 0], V[0, 1], -V[0, 2],
            V[1, 0], V[1, 1], -V[1, 2],
            V[2, 0], V[2, 1], -V[2, 2],
        )
        sigma = wp.vec3(sigma[0], sigma[1], -sigma[2])

    Vh = wp.transpose(V)
    return U, sigma, Vh


@wp.func
def plasticine_deformation(F_trial: wp.mat33, material: MPMMaterial):  # von_mises
    U, sigma, Vh = svd(F_trial)

    threshold = 0.01
    sigma = wp.vec3(wp.max(sigma[0], threshold), wp.max(sigma[1], threshold), wp.max(sigma[2], threshold))

    epsilon = wp.vec3(wp.log(sigma[0]), wp.log(sigma[1]), wp.log(sigma[2]))
    epsilon_trace = epsilon[0] + epsilon[1] + epsilon[2]
    epsilon_bar = epsilon - wp.vec3(epsilon_trace / 3.0, epsilon_trace / 3.0, epsilon_trace / 3.0)
    epsilon_bar_norm = wp.length(epsilon_bar) + 1e-5

    delta_gamma = epsilon_bar_norm - material.yield_stress / (2.0 * material.mu)

    if delta_gamma > 0.0:
        yield_epsilon = epsilon - (delta_gamma / epsilon_bar_norm) * epsilon_bar
        yield_sigma = wp.mat33(
            wp.exp(yield_epsilon[0]), 0.0, 0.0,
            0.0, wp.exp(yield_epsilon[1]), 0.0,
            0.0, 0.0, wp.exp(yield_epsilon[2]),
        )
        F_corrected = U * yield_sigma * Vh
        return F_corrected
    else:
        return F_trial


@wp.func
def water_deformation(F_trial: wp.mat33, material: MPMMaterial):
    J = wp.determinant(F_trial)
    Je_1_3 = wp.pow(J, 1.0 / 3.0)
    F_corrected = wp.diag(wp.vec3(Je_1_3, Je_1_3, Je_1_3))
    return F_corrected


@wp.func
def sigma_elasticity(F: wp.mat33, material: MPMMaterial):
    U, sigma, Vh = svd(F)

    threshold = 1e-5
    sigma = wp.vec3(wp.max(sigma[0], threshold), wp.max(sigma[1], threshold), wp.max(sigma[2], threshold))

    epsilon = wp.vec3(wp.log(sigma[0]), wp.log(sigma[1]), wp.log(sigma[2]))
    trace = epsilon[0] + epsilon[1] + epsilon[2]
    tau = 2.0 * material.mu * epsilon + wp.vec3(material.lam * trace)

    stress = U * wp.diag(tau) * wp.transpose(U)
    return stress


@wp.func
def volume_elasticity(F: wp.mat33, material: MPMMaterial):
    J = wp.determinant(F)
    I = wp.identity(n=3, dtype=float)
    stress = material.lam * J * (J - 1.0) * I
    return stress
