import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path
import matplotlib.gridspec as gridspec
import os
import shutil
import warnings
from bsccm import BSCCM
from tqdm import tqdm
from scipy import ndimage

def load_data_from_config(config, data_dir):
    """
    Using the data fields in the config file, open important functions and metadata for accessing BSCCM
        markers: list of marker names
        image_target generator:
        dataset_size:
        display_range: dict with min and max to display for marker
        image_from_index_function: for reading the image that will be produced at the kth position in the image_target_generator
    """
    bsccm = BSCCM(data_dir + '/{}/'.format(config['data']['dataset_name']), cache_index=True)
    markers, image_target_generator, dataset_size, display_range, indices = get_bsccm_image_marker_generator(bsccm, **config["data"] )
    
    def image_from_index_function(index):
        """
        index is relative to the data that was loaded, not globabl in BSCCM
        """
        return get_bsccm_image(bsccm, config["data"]["channels"], indices[index])
    
    return markers, image_target_generator, dataset_size, display_range, image_from_index_function

def get_display_channel_names(channel_or_channels):
    conversion = {
        'LED119': 'Single LED off-axis',
        'DF_50': 'Darkfield (NA=0.5)',
        'DPC_Right': 'Differential Phase Contrast',
        'Brightfield': 'Brightfield',
    }
    if type(channel_or_channels) == str:
        return conversion[channel_or_channels]
    else:
        return [conversion[c] for c in channel_or_channels]

def get_targets_and_display_range(bsccm, use_two_spectrum_unmixing=False, batch=0, antibodies=(
                                'CD123', 'CD3', 'CD19', 'CD56', 'HLA-DR', 'CD45', 'CD14', 'CD16'), shuffle=True, 
                                 shuffle_seed=123456, **kwargs):
    """
    Load prediction targets (i.e. marker levels), along with reasonable limits to display each of their histograms
    Convert raw targets to log
    """

    indices = bsccm.get_indices(batch=batch, antibodies=antibodies)
    two_spectra_model_names, two_spectra_data, four_spectra_model_names, four_spectra_data = bsccm.get_surface_marker_data(indices)
    if use_two_spectrum_unmixing:
        #remove autofluor from targets
        markers = [s.split('_')[0] for s in two_spectra_model_names if 'autofluor' not in s]
        non_autofluor_mask = [np.any([m in name for m in markers]) for name in two_spectra_model_names]
        targets = two_spectra_data[:, non_autofluor_mask]
    else:
        markers = [s.split('_')[0] for s in four_spectra_model_names if 'autofluor' not in s]
        targets = four_spectra_data
        if antibodies is None:
            #TODO: whats this about?
            raise Exception('what is this')
            #using single stain data. 
            #set to nan all channels corresponding to antibody it wasn't stained with
            marker_indices = [np.flatnonzero([marker in name for name in markers])[0]
                          for marker in bsccm.index_dataframe.loc[indices, 'antibodies']]
            nan_mask = np.ones_like(targets)
            nan_mask *= np.nan
            nan_mask[np.arange(targets.shape[0]), np.array(marker_indices)] = 1
            targets *= nan_mask
        
    # Reorder the columns to be consistent with supplied order of markers
    columns = []
    for marker in markers:
        col_index = np.flatnonzero([marker in name for name in markers])[0]
        columns.append(targets[:, col_index])
    targets = np.stack(columns, axis=1)  
    
    columns = []
    display_range = {}
    
    targets = np.log(targets)
    for marker_index in range(targets.shape[1]):
        range_min = np.nanmin(targets[:, marker_index])
        range_max = np.nanmax(targets[:, marker_index])
        pad = 0.05 * (range_max - range_min)
        display_range[markers[marker_index]] = (range_min - pad, range_max + pad)

    # Shuffle
    if shuffle:
        shuffle_indices = np.arange(indices.size)
        np.random.seed(shuffle_seed)
        np.random.shuffle(shuffle_indices)
        targets = targets[shuffle_indices]
        indices = indices[shuffle_indices]
    
    dataset_size = indices.size
        
    return markers, targets, indices, display_range, dataset_size

 

def get_bsccm_image_marker_generator(bsccm, channels, 
                                     use_two_spectrum_unmixing=False, batch=0, shuffle=True, 
                                     shuffle_seed=123456,
                                     single_marker=None,
                                     **kwargs):
    """
    Prepare a data generator that yields (image, markers) for BSCCM data
    Also compute some reasonable limits for displaying data on log axes
    """
    markers, targets, indices, display_range, dataset_size = get_targets_and_display_range(bsccm, 
                            use_two_spectrum_unmixing, batch, shuffle=shuffle, shuffle_seed=shuffle_seed)
        
        
    def image_target_generator():
        """
        Generator functions that loads and supplies an image + targets
        """
        image_shape = get_bsccm_image(bsccm, channels, indices[0]).shape
        blank_image = np.zeros(image_shape)
        
        for index, target in zip(indices, targets):
            if single_marker is None:
                multi_channel_img = get_bsccm_image(bsccm, channels, index)
                yield multi_channel_img, target.astype(np.float32)
            else:
                # for speed saving ignore non requested markers, but return None to keep
                # everything in same order for reproducible train/val/test split
                if markers[np.argmax(np.logical_not(np.isnan(target.ravel())).astype(int))] == single_marker:
                    multi_channel_img = get_bsccm_image(bsccm, channels, index)
                    yield multi_channel_img, target.astype(np.float32)
                else:
                    yield blank_image, target.astype(np.float32)
                
            
            
    return markers, image_target_generator, dataset_size, display_range, indices

def get_bsccm_image(bsccm, channels, index):
    return np.stack([bsccm.read_image(index, channel=ch).astype(np.float32)
                     for ch in  channels], axis=-1)
    

def generate_synthetic_multi_led_images(bsccm_coherent, led_indices, edge_crop=0, **kwargs):
    """
    Generate synthetic images with the given led indices on, assuming that all LEDs produce the same number of photons
    on the resultant image
    """
    indices = bsccm_coherent.get_indices(**kwargs)
    min_photons = np.inf
    photons_per_contrast_modality = {}
    single_led_images = {}
    for led_index in led_indices:
        single_led_images[led_index] = load_bsccm_images(bsccm_coherent, 'led_{}'.format(led_index),
                                                          num_images=100, edge_crop=edge_crop, convert_units_to_photons=True)
        photons_per_pixel = np.mean(single_led_images[led_index])
        min_photons = min(min_photons, photons_per_pixel)
        photons_per_contrast_modality[led_index] = photons_per_pixel
        

    photon_fractions = {led_index: min_photons / photons_per_contrast_modality[led_index] for led_index in led_indices}
    print('Photon fractions: {}'.format(photon_fractions))

    # equalize noise between images and then add them together
    synthetic_images = np.zeros_like(single_led_images[led_indices[0]])
    for led_index in led_indices:
         synthetic_images += add_shot_noise_to_experimenal_data(single_led_images[led_index], photon_fractions[led_index])
    
    return synthetic_images


def load_bsccm_images(dataset, channel, num_images=1000, edge_crop=0, empty_slides=False, indices=None,
                      convert_units_to_photons=True, median_filter=False):
    """
    Load a stack of images from a BSCCM dataset

    dataset: BSCCM dataset
    channel: channel to load
    num_images: number of images to load
    edge_crop: number of pixels to crop from each edge of the image
    empty_slides: if True, then load the background image for the slide in which no cell is present
    indices: if not None, then load images with these indices. Ignore num_images
    convert_units_to_photons: if True, convert raw intensity counts to photons
    median_filter: if True, apply a median filter to the image to simulate noiseless data
    """
    if indices is None:
        indices = dataset.get_indices()[:num_images]
    images = []
    for i in indices:
        if empty_slides:
            images.append(dataset.get_background(i, percentile=50, channel=channel))
        else:
            images.append(dataset.read_image(i, channel=channel))
    images =  np.stack(images)
    if edge_crop > 0:
        images = images[:, edge_crop:-edge_crop, edge_crop:-edge_crop]
    if convert_units_to_photons:
        images = _convert_to_photons(images, **dataset.global_metadata['led_array']['camera'])
    if median_filter:
        images = np.array([ndimage.median_filter(img, size=3) for img in images])
    return images


def compute_photon_rescale_fraction(bsccm, channels, images=None, verbose=True, edge_crop=40, empty_slides=False):
    """
    Compute the fraction of photons that should be used to simulate a given contrast modality.
    This is done by taking the average photon count per pixel for each contrast modality, and
    then rescaling all contrast modalities to the lowest photon count.
    if images is not None, then no bsccm is needed
    """
    photons_per_contrast_modality = {}
    min_photons = np.inf
    for channel in channels:
        if bsccm is not None:
            if empty_slides:
                images_at_channel = load_bsccm_images(bsccm, channel, num_images=1000, edge_crop=edge_crop, empty_slides=True, convert_units_to_photons=True)
            else:
                images_at_channel = load_bsccm_images(bsccm, channel, num_images=1000, edge_crop=edge_crop, convert_units_to_photons=True) # take center to avoid counting background
        else:
            images_at_channel = images[channel]
        photons_per_pixel = np.mean(images_at_channel)
        min_photons = min(min_photons, photons_per_pixel)
        photons_per_contrast_modality[channel] = photons_per_pixel

    photon_fractions = {channel: min_photons / photons_per_contrast_modality[channel] for channel in channels}
    if verbose:
        print('phtons per pixel: ', photons_per_contrast_modality)
        print('Rescale to fraction: ', photon_fractions)
    return photon_fractions


def _convert_to_photons(image, gain_db, offset, quantum_efficiency):
    """
    Take an image with raw intensity values, and convert it to photons
    based on the parameters of the camera
    """
    electrons = (image.astype(float) - offset) / (10 ** (gain_db / 10))
    electrons = np.array(electrons)
    electrons[electrons < 0] = 0
    photons = np.array(electrons) / quantum_efficiency
    return photons


def read_images_and_sample_intensities(bsccm, channel, x2_offset, N_images, photon_fraction=1., median_filter=False):
    """
    Read images from a BSCCM dataset and sample intensity coordinates at two points

    bsccm: BSCCM dataset
    channel: channel to load
    x2_offset: offset in pixels to sample intensity coordinates (x1 is at the image center)
    median_filter: if True, apply a median filter to the image before sampling
    """ 
    x1 = []
    x2 = []
    # Choose the index of the image you want to plot
    for index in tqdm(range(N_images)):
        image = load_bsccm_images(bsccm, channel, indices=[index], convert_units_to_photons=True, median_filter=True)[0] * photon_fraction
        x1_val = image[image.shape[0] // 2, image.shape[1] // 2]
        x2_val = image[image.shape[0] // 2 + x2_offset[0], image.shape[1] // 2 + x2_offset[1]]
        x1.append(x1_val)
        x2.append(x2_val)

    x1 = np.array(x1).ravel()
    x2 = np.array(x2).ravel()
    return x1, x2

def add_shot_noise_to_experimenal_data(image_stack, photon_fraction):
    """
    Add synthetic shot noise to an image stack by adding the additional noise 
    that would be expected for the desired photon count
    This also reduces the total number of (average) photons in the image by the photon_fraction
    """
    if photon_fraction > 1 or photon_fraction <= 0:
        raise Exception('photon_fraction must be less than 1 and greater than 0')
    additional_sd = np.sqrt(photon_fraction * image_stack) - photon_fraction * np.sqrt(image_stack)
    simulated_images = image_stack * photon_fraction + additional_sd * np.random.randn(*image_stack.shape)
    positive = np.array(simulated_images)
    positive[positive < 0] = 0 # cant have negative counts
    return np.array(positive)

def load_image_with_synthetic_shot_noise(bsccm, index, channel, photon_fraction):
    """
    Load a BSCCM image and simulate it as if it had been collected with fewer photons,
    by adding the additional noise that would be expected for the desired photon count
    """
    image = load_bsccm_images(bsccm, channel, indices=[index], convert_units_to_photons=True)[0]
    additional_sd = np.sqrt(photon_fraction * image) - photon_fraction * np.sqrt(image)
    simulated_images = image * photon_fraction + additional_sd * np.random.randn(*image.shape)
    positive = np.array(simulated_images)
    positive[positive < 0] = 0 # cant have negative counts
    return np.array(positive)
