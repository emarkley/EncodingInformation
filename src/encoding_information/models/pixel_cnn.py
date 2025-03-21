"""
PixelCNN in Jax/Flax. Adapted from :
https://uvadlc-notebooks.readthedocs.io/en/latest/tutorial_notebooks/JAX/tutorial12/Autoregressive_Image_Modeling.html

Mixture density output adapted from:
https://github.com/hardmaru/mdn_jax_tutorial/blob/master/mixture_density_networks_jax.ipynb
"""

## Standard libraries
import os
import numpy as onp
from typing import Any
from tqdm import tqdm
import warnings

## JAX
import jax
import jax.numpy as np
from jax import random
from jax.scipy.special import logsumexp 

from flax import linen as nn
from flax.training.train_state import TrainState
import optax


from .model_base_class import MeasurementModel, MeasurementType, \
            train_model, _evaluate_nll, make_dataset_generators



class PreprocessLayer(nn.Module):
    """
    A layer that normalizes the input images using the provided mean and standard deviation.

    Attributes
    ----------
    mean : np.ndarray
        The mean to subtract from the input images.
    std : np.ndarray
        The standard deviation to divide the input images by.
    """
    mean: np.ndarray
    std: np.ndarray

    def __call__(self, x):
        return (x - self.mean) / (self.std + 1e-5)

class MaskedConvolution(nn.Module):
    """
    A convolutional layer with a mask to ensure autoregressive behavior.
    
    This layer ensures that during the convolution, the current pixel does not 
    have access to any future pixels (either to the right or below in the image).

    Attributes
    ----------
    c_out : int
        The number of output channels.
    mask : np.ndarray
        The mask to apply to the convolution, determining which pixels are visible.
    dilation : int, optional
        The dilation factor for the convolution (default is 1).
    """
    c_out : int
    mask : np.ndarray
    dilation : int = 1

    @nn.compact
    def __call__(self, x):
        # Flax's convolution module already supports masking
        # The mask must be the same size as kernel
        # => extend over input and output feature channels
        if len(self.mask.shape) == 2:
            mask_ext = self.mask[...,None,None]
            mask_ext = np.tile(mask_ext, (1, 1, x.shape[-1], self.c_out))
        else:
            mask_ext = self.mask
        # Convolution with masking
        x = nn.Conv(features=self.c_out,
                    kernel_size=self.mask.shape[:2],
                    kernel_dilation=self.dilation,
                    mask=mask_ext)(x)
        return x
    

class VerticalStackConvolution(nn.Module):
    """
    A vertical convolutional layer that processes the pixels above the current pixel in an image.

    This layer creates a vertical stack by masking the convolution kernel, ensuring that the pixels
    below the current pixel are not visible during the convolution.

    Attributes
    ----------
    c_out : int
        The number of output channels.
    kernel_size : int
        The size of the convolution kernel.
    mask_center : bool, optional
        Whether to mask out the center pixel in the kernel (default is False).
    dilation : int, optional
        The dilation factor for the convolution (default is 1).
    """
    c_out : int
    kernel_size : int
    mask_center : bool = False
    dilation : int = 1

    def setup(self):
        # Mask out all pixels below. For efficiency, we could also reduce the kernel
        # size in height, but for simplicity, we stick with masking here.
        mask = onp.ones((self.kernel_size, self.kernel_size), dtype=onp.float32)
        mask[self.kernel_size//2+1:,:] = 0
        # For the very first convolution, we will also mask the center row
        if self.mask_center:
            mask[self.kernel_size//2,:] = 0
        # Our convolution module
        self.conv = MaskedConvolution(c_out=self.c_out,
                                      mask=mask,
                                      dilation=self.dilation)

    def __call__(self, x):
        return self.conv(x)


class HorizontalStackConvolution(nn.Module):
    """
    A horizontal convolutional layer that processes the pixels to the left of the current pixel in an image.

    This layer creates a horizontal stack by masking the convolution kernel, ensuring that the pixels
    to the right of the current pixel are not visible during the convolution.

    Attributes
    ----------
    c_out : int
        The number of output channels.
    kernel_size : int
        The size of the convolution kernel.
    mask_center : bool, optional
        Whether to mask out the center pixel in the kernel (default is False).
    dilation : int, optional
        The dilation factor for the convolution (default is 1).
    """
    c_out : int
    kernel_size : int
    mask_center : bool = False
    dilation : int = 1

    def setup(self):
        # Mask out all pixels on the left. Note that our kernel has a size of 1
        # in height because we only look at the pixel in the same row.
        mask = onp.ones((1, self.kernel_size), dtype=onp.float32)
        mask[0,self.kernel_size//2+1:] = 0
        # For the very first convolution, we will also mask the center pixel
        if self.mask_center:
            mask[0,self.kernel_size//2] = 0
        # Our convolution module
        self.conv = MaskedConvolution(c_out=self.c_out,
                                      mask=mask,
                                      dilation=self.dilation)

    def __call__(self, x):
        return self.conv(x)


class GatedMaskedConv(nn.Module):
    """
    A gated masked convolution layer used in PixelCNN. This layer uses gated activation functions
    to improve gradient flow during training.

    It combines information from a vertical stack and a horizontal stack, each being passed through
    masked convolutions, and optionally conditioned on an external vector (such as class labels).

    Attributes
    ----------
    dilation : int, optional
        The dilation factor for the convolutions (default is 1).
    id : int, optional
        The layer ID, used for parameter naming.
    condition_vector_size : int, optional
        The size of the condition vector for conditional PixelCNN.
    """
    dilation : int = 1
    id: int = None
    condition_vector_size : int = None

    @nn.compact
    def __call__(self, v_stack, h_stack, condition_vector=None):
        c_in = v_stack.shape[-1]

        # Layers (depend on input shape)
        conv_vert = VerticalStackConvolution(c_out=2*c_in,
                                             kernel_size=3,
                                             mask_center=False,
                                             dilation=self.dilation)
        conv_horiz = HorizontalStackConvolution(c_out=2*c_in,
                                                kernel_size=3,
                                                mask_center=False,
                                                dilation=self.dilation)
        conv_vert_to_horiz = nn.Conv(2*c_in,
                                     kernel_size=(1, 1))
        conv_horiz_1x1 = nn.Conv(c_in,
                                 kernel_size=(1, 1))



        # Vertical stack (left)
        v_stack_feat = conv_vert(v_stack)
        v_val, v_gate = np.split(v_stack_feat, 2, axis=-1)
      
        if condition_vector is not None:
            weights = self.param(f'conditioning_weights_vert_{self.id}', jax.nn.initializers.lecun_normal(), (1, self.condition_vector_size,))
            y = np.dot(weights, condition_vector.T).reshape(-1,1,1,1)
            weights_gate = self.param(f'conditioning_weights_vert_gate{self.id}', jax.nn.initializers.lecun_normal(), (1, self.condition_vector_size,))
            y_gate = np.dot(weights_gate, condition_vector.T).reshape(-1,1,1,1)
            v_stack_out = nn.tanh(v_val + y) * nn.sigmoid(v_gate + y_gate)
        else:
            v_stack_out = nn.tanh(v_val) * nn.sigmoid(v_gate)

        # Horizontal stack (right)
        h_stack_feat = conv_horiz(h_stack)
        h_stack_feat = h_stack_feat + conv_vert_to_horiz(v_stack_feat)
        h_val, h_gate = np.split(h_stack_feat, 2, axis=-1)
        if condition_vector is not None:
            weights = self.param(f'conditioning_weights_horz{self.id}', jax.nn.initializers.lecun_normal(), (1, self.condition_vector_size,))
            y = np.dot(weights, condition_vector.T).reshape(-1,1,1,1)
            weights_gate = self.param(f'conditioning_weights_horz_gate{self.id}', jax.nn.initializers.lecun_normal(), (1, self.condition_vector_size,))
            y_gate = np.dot(weights_gate, condition_vector.T).reshape(-1,1,1,1)
            h_stack_feat = nn.tanh(h_val + y) * nn.sigmoid(h_gate + y_gate)
        else:
            h_stack_feat = nn.tanh(h_val) * nn.sigmoid(h_gate)
        h_stack_out = conv_horiz_1x1(h_stack_feat)
        h_stack_out = h_stack_out + h_stack

        return v_stack_out, h_stack_out
    

class _PixelCNNFlaxImpl(nn.Module):
    """
    The core implementation of the PixelCNN model in Flax.

    This module defines the structure of the PixelCNN, including the vertical and horizontal
    masked convolutions, gated activation functions, and a mixture density output layer.

    Attributes
    ----------
    data_shape : tuple
        The shape of the input data (height, width, channels).
    num_hidden_channels : int, optional
        The number of hidden channels in the model (default is 64).
    num_mixture_components : int, optional
        The number of components in the mixture density output (default is 40).
    train_data_mean : float
        The mean of the training data used for normalization.
    train_data_std : float
        The standard deviation of the training data used for normalization.
    train_data_min : float
        The minimum value of the training data.
    train_data_max : float
        The maximum value of the training data.
    sigma_min : float, optional
        The minimum standard deviation for the mixture density output (default is 1).
    condition_vector_size : int, optional
        The size of the condition vector for conditional PixelCNN.
    use_positional_embedding : bool, optional
        Whether to use learned positional embeddings for each pixel (default is False).
    """
    data_shape : tuple
    num_hidden_channels : int = 64
    num_mixture_components : int = 40
    train_data_mean : float = None
    train_data_std : float = None
    train_data_min : float = None
    train_data_max : float = None
    sigma_min : float = 1
    condition_vector_size : int = None
    use_positional_embedding : bool = False

    def setup(self):
        if None in [self.train_data_mean, self.train_data_std, self.train_data_min, self.train_data_max]:
            raise Exception('Must pass in training data statistics constructor')

        if self.train_data_max.dtype != np.float32 or self.train_data_min.dtype != np.float32 or \
            self.train_data_mean.dtype != np.float32 or self.train_data_std.dtype != np.float32:
            raise Exception('Must pass in training data statistics as float32')

        self.normalize = PreprocessLayer(mean=self.train_data_mean, std=self.train_data_std)

        if not isinstance(self.num_hidden_channels, int):
            raise ValueError("num_hidden_channels must be an integer")
        # Initial convolutions skipping the center pixel
        self.conv_vstack = VerticalStackConvolution(self.num_hidden_channels, kernel_size=3, mask_center=True)
        self.conv_hstack = HorizontalStackConvolution(self.num_hidden_channels, kernel_size=3, mask_center=True)
        # Convolution block of PixelCNN. We use dilation instead of downscaling
        self.conv_layers = [
            GatedMaskedConv(dilation=1, id=0, condition_vector_size=self.condition_vector_size),
            GatedMaskedConv(dilation=2, id=1, condition_vector_size=self.condition_vector_size),
            GatedMaskedConv(dilation=1, id=2, condition_vector_size=self.condition_vector_size),
            GatedMaskedConv(dilation=4, id=3, condition_vector_size=self.condition_vector_size),
            GatedMaskedConv(dilation=1, id=4, condition_vector_size=self.condition_vector_size),
            GatedMaskedConv(dilation=2, id=5, condition_vector_size=self.condition_vector_size),
            GatedMaskedConv(dilation=1, id=6, condition_vector_size=self.condition_vector_size),
        ]
        # Output classification convolution (1x1)
        self.conv_out = nn.Conv(self.num_hidden_channels, kernel_size=(1, 1))
        

        # parameters for mixture density 
        def my_bias_init(rng, shape, dtype):
            return random.uniform(rng, shape, dtype=dtype,
                                  minval=self.train_data_min, maxval=self.train_data_max)
        
        # Parameters for learned positional embedding
        if self.use_positional_embedding:
            self.positional_embedding = nn.Embed(num_embeddings=self.data_shape[0] * self.data_shape[1], features=self.num_hidden_channels)
            # generate unique index for each pixel
            self.position_indices = np.arange(self.data_shape[0] * self.data_shape[1]).reshape(*self.data_shape[:2])

        self.mu_dense = nn.Dense(self.num_mixture_components, bias_init=my_bias_init)
        self.sigma_dense = nn.Dense(self.num_mixture_components)
        self.mix_logit_dense = nn.Dense(self.num_mixture_components)

    def __call__(self, x, condition_vectors=None):
        """
        Do forward pass output the parameters of the gaussian mixture output
        """
        # add trailing channel dimension if necessary
        if x.ndim == 3:
            x = x[..., np.newaxis]

        return self.forward_pass(x, condition_vectors=condition_vectors)
    
    def compute_gaussian_nll(self, mu, sigma, mix_logit, x):
        # numerically efficient implementation of mixture density, slightly modified
        # see https://github.com/hardmaru/mdn_jax_tutorial/blob/master/mixture_density_networks_jax.ipynb
        # compute per-pixel negative log-likelihood
        nll = - logsumexp(mix_logit - logsumexp(mix_logit, axis=-1, keepdims=True) + self.lognormal(x, mu, sigma), axis=-1)
        return nll

    def compute_loss(self, mu, sigma, mix_logit, x):
        """ 
        Compute average negative log likelihood per pixel averaged over batch and pixels
        """
        return self.compute_gaussian_nll(mu, sigma, mix_logit, x).mean()


    def lognormal(self, y, mean, sigma):
        logSqrtTwoPI = np.log(np.sqrt(2.0 * np.pi))
        return -0.5 * ((y - mean) / sigma) ** 2 - np.log(sigma) - logSqrtTwoPI

    def forward_pass(self, x, condition_vectors=None):
        """
        Forward pass of the PixelCNN model.

        The image is passed through the vertical and horizontal masked convolutions, followed by
        gated convolutions, and finally a mixture density output layer. The model outputs the parameters
        of the mixture density for each pixel (mean, standard deviation, and mixture logits).

        Parameters
        ----------
        x : ndarray
            The input image, with shape (batch_size, height, width, channels).
        condition_vectors : ndarray, optional
            A vector to condition the image generation process (e.g., class labels).

        Returns
        -------
        mu : ndarray
            The mean of the Gaussian components for each pixel.
        sigma : ndarray
            The standard deviation of the Gaussian components for each pixel.
        mix_logit : ndarray
            The logits for the mixture components.
        """
        # check shape
        if x.ndim != 4:
            raise ValueError("Input image must have shape BxHxWx1")

        # rescale to 0-1ish
        x = self.normalize(x)
        # Initial convolutions
        v_stack = self.conv_vstack(x)
        h_stack = self.conv_hstack(x)
        # Gated Convolutions
        for layer in self.conv_layers:
            v_stack, h_stack = layer(v_stack, h_stack, condition_vector=condition_vectors)
        # 1x1 classification convolution
        # Apply ELU before 1x1 convolution for non-linearity on residual connection
        out = self.conv_out(nn.elu(h_stack))

        if self.use_positional_embedding:
            # add positional embedding
            indices = self.position_indices            
            # apply positional embedding
            out = out + self.positional_embedding(indices)
    
        # must be positive and within data range
        mu = np.clip(self.mu_dense(out), self.train_data_min, self.train_data_max) 

        sigma = nn.activation.softplus( self.sigma_dense(out) )  # must be positive
        # avoid having tiny components that overly concentrate mass, and don't need components larger than data standard deviation
        sigma = np.clip(sigma, self.sigma_min, self.train_data_std )

        mix_logit = self.mix_logit_dense(out)

        return mu, sigma, mix_logit



class PixelCNN(MeasurementModel):
    """
    The PixelCNN model for autoregressive image modeling.

    This class handles the training and evaluation of the PixelCNN model and wraps the Flax implementation
    in a higher-level interface that conforms to the MeasurementModel class. It provides methods for fitting
    the model to data, computing the negative log-likelihood of images, and generating new images.

    Attributes
    ----------
    num_hidden_channels : int
        The number of hidden channels in the model.
    num_mixture_components : int
        The number of components in the mixture density output.
    """

    def __init__(self, num_hidden_channels=64, num_mixture_components=40):
        """
        Initialize the PixelCNN model with image shape, number of hidden channels, and mixture components.

        Parameters
        ----------
        num_hidden_channels : int
            Number of hidden channels in the convolutional layers.
        num_mixture_components : int
            Number of mixture components for the output layer.
        """

        super().__init__([MeasurementType.HW, MeasurementType.HWC], measurement_dtype=float)
        self.num_hidden_channels = num_hidden_channels
        self.num_mixture_components = num_mixture_components
        self._flax_model = None
        self._fitted = False

    def fit(self, train_images, condition_vectors=None, learning_rate=1e-2, max_epochs=200, steps_per_epoch=100,  patience=40, 
            sigma_min=1, batch_size=64, num_val_samples=None, percent_samples_for_validation=0.1,  do_lr_decay=False, verbose=True,
            add_gaussian_noise=False, add_uniform_noise=True, model_seed=None, data_seed=None, use_positional_embedding=False,
            # deprecated
            seed=None,):
        """
        Train the PixelCNN model on a dataset of images.

        Parameters
        ----------
        train_images : ndarray
            The input dataset, with shape (N, H, W, C).
        condition_vectors : ndarray, optional
            Vectors to condition the image generation process (e.g., class labels).
        learning_rate : float, optional
            The learning rate for optimization (default is 1e-2).
        max_epochs : int, optional
            The maximum number of training epochs (default is 200).
        steps_per_epoch : int, optional
            The number of steps per epoch (default is 100).
        patience : int, optional
            The number of epochs to wait before early stopping (default is 40).
        sigma_min : float, optional
            The minimum standard deviation for the mixture density output (default is 1).
        batch_size : int, optional
            The batch size for training (default is 64).
        num_val_samples : int, optional
            The number of validation samples. If None, a percentage is used (default is None).
        percent_samples_for_validation : float, optional
            The percentage of samples to use for validation (default is 0.1).
        do_lr_decay : bool, optional
            Whether to apply learning rate decay during training (default is False).
        verbose : bool, optional
            Whether to print progress during training (default is True).
        add_gaussian_noise : bool, optional
            Whether to add Gaussian noise to the training images (default is False).
        add_uniform_noise : bool, optional
            Whether to add uniform noise to the training images (default is True).
        model_seed : int, optional
            Seed for model initialization.
        data_seed : int, optional
            Seed for data shuffling.

        Returns
        -------
        val_loss_history : list
            A list of validation loss values for each epoch.
        """
        if seed is not None:
            warnings.warn("seed argument is deprecated. Use model_seed and data_seed instead")
            model_seed = seed
            data_seed = seed

        if model_seed is not None:
            onp.random.seed(model_seed)
        model_key = jax.random.PRNGKey(onp.random.randint(0, 100000))
        
        self._validate_data(train_images)


        train_images = train_images.astype(np.float32)

        # check that only one type of noise is added
        if add_gaussian_noise and add_uniform_noise:
            raise ValueError("Only one type of noise can be added to the training data")

        num_val_samples = int(train_images.shape[0] * percent_samples_for_validation) if num_val_samples is None else num_val_samples

        # add trailing channel dimension if necessary
        if train_images.ndim == 3:
            train_images = train_images[..., np.newaxis]

        self.image_shape = train_images.shape[1:3]

        # Use the make dataset generators function because training data may be modified here during training
        # (i.e. adding small amounts of noise to account for discrete data and continuous model)
        _, dataset_fn = make_dataset_generators(train_images, batch_size=400, num_val_samples=train_images.shape[0],
                                                add_gaussian_noise=add_gaussian_noise, add_uniform_noise=add_uniform_noise, 
                                                seed=data_seed)
        example_images = dataset_fn().next()

        if self._flax_model is None:
            self.add_gaussian_noise = add_gaussian_noise
            self.add_uniform_noise = add_uniform_noise
            self._flax_model = _PixelCNNFlaxImpl(num_hidden_channels=self.num_hidden_channels, num_mixture_components=self.num_mixture_components,
                                    train_data_mean=np.mean(example_images), train_data_std=np.std(example_images),
                                    train_data_min=np.min(example_images), train_data_max=np.max(example_images), sigma_min=sigma_min,
                                    condition_vector_size=None if condition_vectors is None else condition_vectors.shape[-1],
                                    data_shape=train_images.shape[1:], use_positional_embedding=use_positional_embedding)
                                    
            # pass in an intial batch            
            initial_params = self._flax_model.init(model_key, train_images[:3], 
                                condition_vectors[:3] if condition_vectors is not None else None)

            if do_lr_decay:
                lr_schedule = optax.exponential_decay(init_value=learning_rate,
                                                    transition_steps=steps_per_epoch,
                                                    decay_rate=0.99,)

                self._optimizer = optax.adam(lr_schedule)
            else:
                self._optimizer = optax.adam(learning_rate)

            def apply_fn(params, x, condition_vector=None):
                output = self._flax_model.apply(params, x, condition_vector)
                return self._flax_model.compute_loss(*output, x)

            self._state = TrainState.create(apply_fn=apply_fn, params=initial_params, tx=self._optimizer)        
        
        if condition_vectors is None:

            def loss_fn(params, state, imgs):
                return state.apply_fn(params, imgs)
            grad_fn = jax.value_and_grad(loss_fn)

            @jax.jit
            def train_step(state, imgs):
                """
                A standard gradient descent training step
                """
                loss, grads = grad_fn(state.params, state, imgs)
                state = state.apply_gradients(grads=grads)
                return state, loss
        else:

            def loss_fn(params, state, imgs, condition_vecs):
                return state.apply_fn(params, imgs, condition_vecs)
            grad_fn = jax.value_and_grad(loss_fn)

            @jax.jit
            def train_step(state, imgs, condition_vecs):
                """
                A standard gradient descent training step
                """
                loss, grads = grad_fn(state.params, state, imgs, condition_vecs)
                state = state.apply_gradients(grads=grads)
                return state, loss


        best_params, val_loss_history = train_model(train_images=train_images, condition_vectors=condition_vectors, train_step=train_step,
                                                    state=self._state, batch_size=batch_size, num_val_samples=int(num_val_samples),
                                                    add_gaussian_noise=add_gaussian_noise, add_uniform_noise=add_uniform_noise,
                                                    steps_per_epoch=steps_per_epoch, num_epochs=max_epochs, patience=patience, seed=data_seed,
                                                    verbose=verbose)
        self._state = self._state.replace(params=best_params)
        self.val_loss_history = val_loss_history
        self._fitted = True
        return val_loss_history




    def compute_negative_log_likelihood(self, data, conditioning_vecs=None,  data_seed=None, average=True, verbose=True, seed=None):
        """
        Compute the negative log-likelihood (NLL) of images under the trained PixelCNN model.

        Parameters
        ----------
        data : ndarray
            The input images for which to compute the NLL.
        conditioning_vecs : ndarray, optional
            Vectors to condition the image generation process (e.g., class labels).
        data_seed : int, optional
            Seed for data shuffling.
        average : bool, optional
            If True, return the average NLL over all images (default is True).
        verbose : bool, optional
            Whether to print progress (default is True).
        seed : int, optional
            Deprecated. Use data_seed instead.

        Returns
        -------
        nll : float
            The negative log-likelihood of the input images.
        """
        if not self._fitted:
            raise ValueError('must call fit() before computing likelihood')
        # See superclass for docstring
        if seed is not None:
            warnings.warn("seed argument is deprecated. Use data_seed instead")
            data_seed = seed

        if data.ndim == 3:
            # add a trailing channel dimension if necessary
            data = data[..., np.newaxis]
        elif data.ndim == 2:
            # add trailing channel and batch dimensions
            data = data[np.newaxis, ..., np.newaxis]

        # TODO: extend to images sizes different the default
        # check if data shape is different than image shape
        if data.shape[1:3] != self.image_shape:
            raise ValueError("Data shape is different than image shape of trained model. This is not yet supported"
                             "Expected {}, got {}".format(self.image_shape, data.shape[1:3]))

        # get test data generator. Here all data is "validation", because the data passed into this should already be
        # (in the typical case) a test set
        _, dataset_fn = make_dataset_generators(data, batch_size=32 if average else 1, num_val_samples=data.shape[0], 
                                                add_gaussian_noise=self.add_gaussian_noise, add_uniform_noise=self.add_uniform_noise,
                                                condition_vectors=conditioning_vecs, seed=data_seed)
        @jax.jit
        def conditional_eval_step(state, imgs, condition_vecs):
            return state.apply_fn(state.params, imgs, condition_vecs)

        return _evaluate_nll(dataset_fn(), self._state, return_average=average,
                            eval_step=conditional_eval_step if conditioning_vecs is not None else None, verbose=verbose)


    def generate_samples(self, num_samples, conditioning_vecs=None, sample_shape=None, ensure_nonnegative=True, seed=None, verbose=True):
        """
        Generate new images from the trained PixelCNN model by sampling pixel by pixel.

        Parameters
        ----------
        num_samples : int
            Number of images to generate.
        conditioning_vecs : jax.Array, optional
            Optional conditioning vectors. If provided, the shape should match 
            (num_samples, condition_vector_size). Default is None.
        sample_shape : tuple of int or int, optional
            Shape of the images to generate. If None, the model's image_shape is used.
            If a single int is provided, it will be treated as a square shape. Default is None.
        ensure_nonnegative : bool, optional
            If True, ensure that the generated pixel values are non-negative. Default is True.
        seed : int, optional
            Random seed for reproducibility. Default is 123 if not provided.
        verbose : bool, optional
            If True, display progress during the generation process. Default is True.

        Returns
        -------
        jax.Array
            Generated images with the specified shape.
        """
        if seed is None:
            seed = 123
        key = jax.random.PRNGKey(seed)
        if sample_shape is None:
            sample_shape = self.image_shape
        if type(sample_shape) == int:
            sample_shape = (sample_shape, sample_shape)

        if conditioning_vecs is not None:
            assert conditioning_vecs.shape[0] == num_samples
            assert conditioning_vecs.shape[1] == self._flax_model.condition_vector_size

        sampled_images = onp.zeros((num_samples, *sample_shape))
        for i in tqdm(onp.arange(sample_shape[0]), desc='Generating PixelCNN samples') if verbose else np.arange(sample_shape[0]):
            for j in onp.arange(sample_shape[1]):
                i_limits = max(0, i - self.image_shape[0] + 1), max(self.image_shape[0], i+1)
                j_limits = max(0, j - self.image_shape[1] + 1), max(self.image_shape[1], j+1)

                conditioning_images = sampled_images[:, i_limits[0]:i_limits[1], j_limits[0]:j_limits[1]]
                i_in_cropped_image = i - i_limits[0]
                j_in_cropped_image = j - j_limits[0]

                assert conditioning_images.shape[1:] == self.image_shape

                key, key2 = jax.random.split(key)
                if conditioning_vecs is None:
                    mu, sigma, mix_logit = self._flax_model.apply(self._state.params, conditioning_images)
                else:
                    mu, sigma, mix_logit = self._flax_model.apply(self._state.params, conditioning_images, conditioning_vecs)
                # only sampling one pixel at a time
                # make onp arrays for range checking
                mu = onp.array(mu)[:, i_in_cropped_image, j_in_cropped_image, :]
                sigma = onp.array(sigma)[:, i_in_cropped_image, j_in_cropped_image, :]
                mix_logit = onp.array(mix_logit)[:, i_in_cropped_image, j_in_cropped_image, :]

                # mix_probs = np.exp(mix_logit - logsumexp(mix_logit, axis=-1, keepdims=True))
                component_indices = jax.random.categorical(key, mix_logit, axis=-1)
                # draw categorical sample
                sample_mus = mu[np.arange(num_samples), component_indices]
                sample_sigmas = sigma[np.arange(num_samples), component_indices]
                sample = jax.random.normal(key2, shape=sample_mus.shape) * sample_sigmas + sample_mus
                sampled_images[:, i, j] = sample

        if ensure_nonnegative:
            sampled_images = np.where(sampled_images < 0, 0, sampled_images)
        return sampled_images
   
