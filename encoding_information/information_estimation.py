"""
Functions for estimating entropy and mutual information
"""
from encoding_information.image_utils import *
from jax import jit
from jax.scipy.special import digamma, gammaln

from functools import partial
import jax.numpy as np
import warnings


def analytic_multivariate_gaussian_entropy(cov_matrix):
    """
    Numerically stable computation of the analytics entropy of a multivariate gaussian
    """
    d = cov_matrix.shape[0]
    entropy = 0.5 * d * np.log(2 * np.pi * np.e) + 0.5 * np.sum(np.log(np.linalg.eigvalsh(cov_matrix)))
    return entropy / d
 
def nearest_neighbors_entropy_estimate(X, k=3):
    """
    Estimate the entropy (in nats) of a dsitribution from samples usiing the KL 
    nearest neighbors estimator. 
    
    X : ndarray, shape (n_samples, n_dimensions)
    k : int The k in k-nearest neighbors
    base : base for the logarithms
    """
    return _do_nearest_neighbors_entropy_estimate(X, X.shape[0], X.shape[1], k)

@partial(jit, static_argnums=(1, 2, 3))
def _do_nearest_neighbors_entropy_estimate(X, N, d, k=3):
    """
    Just-in-time compiled helper function for nearest_neighbors_entropy_estimate.
    """
    nn = nearest_neighbors_distance(X, k)

    # compute the log volume of the d-dimensional ball with raidus of the nearest neighbor distance
    log_vd = d * np.log(nn) + d/2 * np.log(np.pi) - gammaln(d/2 + 1)
    # h = np.mean(log_vd + np.log(N - 1) - digamma(k))
    h = np.log(k) - digamma(k) + np.mean(log_vd + np.log(N) - np.log(k))
    return h 


@partial(jit, static_argnums=1)
def nearest_neighbors_distance(X, k):
    """
    Compute the distance to the kth nearest neighbor for each point in X by
    exhaustively searching all points in X.
    
    X : ndarray, shape (n_samples, W, H) or (n_samples, num_features)
    k : int
    """
    X = X.reshape(X.shape[0], -1)
    distance_matrix = np.sum((X[:, None, :] - X[None, :, :]) ** 2, axis=-1)
    kth_nn_index = np.argsort(distance_matrix, axis=-1)[:, k]
    kth_nn = X[kth_nn_index, :]
    kth_nn_dist = np.sqrt(np.sum((X - kth_nn)**2, axis=-1))
    return kth_nn_dist


def gaussian_entropy_estimate(X, stationary=True, optimize=False, eigenvalue_floor=1e-4,  return_cov_mat_and_mean=False,
                               patience=250, num_validation=100, batch_size=12,
                           gradient_clip=1, learning_rate=1e2, momentum=0.9, max_iters=1500,
                              verbose=False):
    """
    Estimate the entropy in "nats" (differential entropy doesn't really have units) per pixel
      of samples from a distribution of images by approximating 
    the distribution as a Gaussian.  i.e. Taking its covariance matrix and 
    computing the entropy of the Gaussian distribution with that same covariance matrix.

    X : ndarray, shape (n_samples, W, H) or (n_samples, num_features)
    stationary : bool, whether to assume the distribution is stationary
    iterative_estimator : bool, whether to optimize the estimate with iterative optimization
    eigenvalue_floor : float, make the eigenvalues of the covariance matrix at least this large
    return_cov_mat_and_mean : bool, whether to return the estimated covariance matrix and mean
    """
    X = X.reshape(X.shape[0], -1)
    D = X.shape[1]
    # np.cov takes D x N shaped data but compute stationary cov mat takes N x D
    if not stationary:
        try:
            cov_mat = estimate_cov_mat(X)
            if eigenvalue_floor is not None:
                eigvals, eigvecs = np.linalg.eigh(cov_mat)
                eigvals = np.where(eigvals < eigenvalue_floor, eigenvalue_floor, eigvals)
                cov_mat = eigvecs @ np.diag(eigvals) @ eigvecs.T
        except:
            raise Exception("Couldn't compute covariance matrix")
            
        evs = np.linalg.eigvalsh(cov_mat)
        if np.any(evs < 0):
            warnings.warn("Covariance matrix is not positive definite. This indicates numerical error.")
        sum_log_evs = np.sum(np.log(np.where(evs < 0, 1e-15, evs)))         
        mean_vec = np.mean(X, axis=0)               
    else:
        mean_vec, cov_mat = estimate_stationary_cov_mat(X, eigenvalue_floor=eigenvalue_floor, verbose=verbose, optimize=optimize, 
                                               patience=patience, num_validation=num_validation, batch_size=batch_size, return_mean=True,
                                               gradient_clip=gradient_clip, learning_rate=learning_rate, momentum=momentum, max_iters=max_iters)       
        sum_log_evs = np.sum(np.log(np.linalg.eigvalsh(cov_mat)))
    gaussian_entropy = 0.5 *(sum_log_evs + D * np.log(2* np.pi * np.e)) / D
    if return_cov_mat_and_mean:
        return gaussian_entropy, cov_mat, mean_vec
    else:
        return gaussian_entropy


@partial(jit, static_argnums=(1,))
def estimate_conditional_entropy(images, gaussian_noise_sigma=None):
    """
    Compute the conditional entropy H(Y | X) in "nats" 
    (differential entropy doesn't really have units...) per pixel,
    where Y is a random noisy realization of a random clean image X

    images : ndarray clean image HxW or images NxHxW
    gaussian_noise_sigma : float, if not None, assume gaussian noise with this sigma.
            otherwise assume poisson noise.
    """
    # vectorize
    images = images.reshape(-1, images.shape[-2] * images.shape[-1])
    n_pixels = images.shape[-1]
         
    # if np.any(images < 0):
    #     warnings.warn(f"{np.sum(images < 0) / images.size:.2%} of pixels are negative.")
    # images = np.where(images <= 0, 0, images) #always at least fraction of photon

    if gaussian_noise_sigma is None:
        # conditional entropy H(Y | x) for Poisson noise 
        gaussian_approx = 0.5 * (np.log(2 * np.pi * np.e) + np.log(images))
        gaussian_approx = np.where(images <= 0, 0, gaussian_approx)
        per_image_entropies = np.sum(gaussian_approx, axis=1) / n_pixels
        h_y_given_x = np.mean(per_image_entropies)

        # add small amount of gaussian noise (e.g. read noise)
        # read_noise_sigma = 1
        # h_y_given_x += 0.5 * np.log(2 * np.pi * np.e * read_noise_sigma**2)
        return h_y_given_x
    else:
        # conditional entropy H(Y | x) for Gaussian noise
        # only depends on the gaussian sigma
        return  0.5 * np.log(2 * np.pi * np.e * gaussian_noise_sigma**2)
    

def run_bootstrap(data, estimation_fn, num_bootstrap_samples=200, confidence_interval=90, seed=1234, verbose=False):
    """
    Runs a bootstrap estimation procedure on the given data using the provided estimation function.

    Parameters:
    -----------
    data : ndarray, shape (n_samples, ...) or a dictionary of ndarrays
        The data to be used for the bootstrap estimation. If a dictionary is provided, each value in the dictionary
        should be an ndarray with the same number of samples.
    estimation_fn : function
        The function to be used for estimating the desired quantity from the data. This function should take a single
        argument, which is the data to be used for the estimation.
    num_bootstrap_samples : int, optional (default=1000)
        The number of bootstrap samples to generate.
    confidence_interval : float, optional (default=90)
        The confidence interval to use for the estimation, expressed as a percentage.
    seed : int, optional (default=1234)
        The random seed to use for generating the bootstrap samples.
    verbose : bool, optional (default=False)
        Print progress bar

    Returns:
    --------
    mean : float
        The mean estimate of the desired quantity across all bootstrap samples.
    conf_int : list of floats
        The lower and upper bounds of the confidence interval for the estimate, expressed as percentiles of the
        bootstrap sample distribution.
    """
    key = jax.random.PRNGKey(onp.random.randint(0, 1000000))
    N = data.shape[0] if not isinstance(data, dict) else data[list(data.keys())[0]].shape[0]
    results = []
    if verbose:
        iterator = tqdm(range(num_bootstrap_samples), desc="Running bootstraps")
    else:
        iterator = range(num_bootstrap_samples)
    for i in iterator:
        key, subkey = jax.random.split(key)
        if not isinstance(data, dict):
            data_sample = jax.random.choice(subkey, data, shape=(N,), replace=True)
            results.append(estimation_fn(data_sample))
        else:
            data_samples = {}
            for k, v in data.items():
                key, subkey = jax.random.split(key)
                data_samples[k] = jax.random.choice(subkey, v, shape=(N,), replace=True)
            results.append(estimation_fn(**data_samples))
        
    results = np.array(results)
    mean = np.mean(results)
    conf_int = [np.percentile(results, 50 - confidence_interval/2),
                np.percentile(results, 50 + confidence_interval/2)]
    return mean, conf_int
        
    
def  estimate_mutual_information(noisy_images, clean_images=None, use_stationary_model=True, use_iterative_optimization=False,                                 
                                  eigenvalue_floor=1e-3, gaussian_noise_sigma=None, estimate_conditional_from_model_samples=False,
                                 patience=25, num_validation=100, batch_size=12,
                           gradient_clip=1, learning_rate=1e2, momentum=0.9, max_iters=100, return_cov_mat_and_mean=False,  verbose=False,):
    """
    Estimate the mutual information (in bits per pixel) of a stack of noisy images, by making a Gaussian approximation
    to the distribution of noisy images, and subtracting the conditional entropy of the clean images
    If clean_images is not provided, instead compute the conditional entropy of the noisy images.

    noisy : ndarray NxHxW array of images or image patches
    clean_images : ndarray NxHxW array of images or image patches
    use_stationary_model : bool, whether to assume the distribution is stationary
    use_iterative_optimization : bool, whether to use iterative optimization to estimate the covariance matrix
    eigenvalue_floor : float, make the eigenvalues of the covariance matrix at least this large in the stationary model
    gaussian_noise_sigma : float, if not None, assume gaussian noise with this sigma.
            otherwise assume poisson noise.
    estimate_conditional_from_model_samples : bool, whether to estimate the conditional entropy from a model fit to them
        rather than from the the data iteself.  
    patience : int, (if use_iterative_optimization=True) how many iterations to wait for validation loss to improve
    num_validation : int, (if use_iterative_optimization=True) how many samples to use for validation
    gradient_clip : float, (if use_iterative_optimization=True) maximum gradient norm
    learning_rate : float, (if use_iterative_optimization=True) learning rate for gradient descent
    momentum : float, (if use_iterative_optimization=True) momentum for gradient descent
    max_iters : int, (if use_iterative_optimization=True) maximum number of iterations for gradient descent
    return_cov_mat_and_mean : bool, whether to return the estimated covariance matrix and mean
    verbose : bool, whether to print out the estimated values
    """
    clean_images_if_available = clean_images if clean_images is not None else noisy_images
    if np.any(clean_images_if_available < 0):   
        warnings.warn(f"{np.sum(clean_images_if_available < 0) / clean_images_if_available.size:.2%} of pixels are negative.")
    if np.mean(clean_images_if_available) < 20 and not estimate_conditional_from_model_samples:
        warnings.warn(f"Mean pixel value is {np.mean(clean_images_if_available):.2f}. More accurate results can probably be obtained"
                        "by setting estimate_conditional_from_model_samples=True")

    if estimate_conditional_from_model_samples:
        if not use_stationary_model:
            raise NotImplementedError("Conditional entropy from marginal samples only implemented for stationary model")
        vecotrized_images = clean_images_if_available.reshape(clean_images_if_available.shape[0], -1)
        mean_vec = np.ones(vecotrized_images.shape[1]) * np.mean(vecotrized_images)
        stationary_cov_mat = estimate_stationary_cov_mat(vecotrized_images, eigenvalue_floor=eigenvalue_floor, 
                                                         optimize=use_iterative_optimization, verbose=verbose, 
                                                         patience=patience, num_validation=num_validation, batch_size=batch_size,
                                                         gradient_clip=gradient_clip, learning_rate=learning_rate, momentum=momentum, max_iters=max_iters)        
        samples = generate_stationary_gaussian_process_samples(mean_vec, stationary_cov_mat, 
                                                               num_samples=clean_images_if_available.shape[0], ensure_nonnegative=True)
        clean_images_if_available = samples.reshape(clean_images_if_available.shape)

    h_y_given_x = estimate_conditional_entropy(clean_images_if_available, gaussian_noise_sigma=gaussian_noise_sigma,)
    h_y_given_x_per_pixel_bits = h_y_given_x / np.log(2)
    h_y_gaussian, cov_mat, mean_vec = gaussian_entropy_estimate(noisy_images, stationary=use_stationary_model, optimize=use_iterative_optimization,
                                            eigenvalue_floor=eigenvalue_floor,
                                                patience=patience, num_validation=num_validation, batch_size=batch_size,
                                                  gradient_clip=gradient_clip, learning_rate=learning_rate, momentum=momentum, max_iters=max_iters,                                            
                                            verbose=verbose, return_cov_mat_and_mean=True)
    h_y_gaussian_per_pixel_bits = h_y_gaussian / np.log(2)
    mutual_info = h_y_gaussian_per_pixel_bits - h_y_given_x_per_pixel_bits
    if verbose:
        print(f"Estimated H(Y|X) = {h_y_given_x_per_pixel_bits:.3f} bits/pixel")
        print(f"Estimated H(Y) = {h_y_gaussian_per_pixel_bits:.3f} bits/pixel")
        print(f"Estimated I(Y;X) = {mutual_info:.3f} bits/pixel")
    if return_cov_mat_and_mean:
        return mutual_info, cov_mat, mean_vec
    return mutual_info 