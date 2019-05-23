import math
from functools import partial

from scipy.stats import norm
from scipy.special import expit as sigmoid

import numpy as np
import craystack.vectorans as vrans
import craystack.util as util


def NonUniform(enc_statfun, dec_statfun, precision):
    """
    Codec for symbols which are not uniformly distributed. The statfuns specify
    the following mappings:

        enc_statfun: symbol |-> start, freq
        dec_statfun: cf |-> symbol

    The interval [0, 1) is modelled by the range of integers
    [0, 2 ** precision). The operation performed by enc_statfun is used for
    compressing data and is visualised below for a distribution over a set of
    symbols {a, b, c, d}.

    0                                                         2 ** precision
    |    a              !b!              c              d         |
    |----------|--------------------|---------|-------------------|
               |------ freq --------|
             start

    Calling enc_statfun(b) must return the pair (start, freq), where start is
    the start of the interval representing the symbol b and freq is its width.
    Start and freq must satisfy the following constraints:

        0 <  freq
        0 <= start        <  2 ** precision
        0 <  start + freq <= 2 ** precision

    The value of start is analagous to the cdf of the distribution, evaluated
    at b, while freq is analagous to the pmf evaluated at b.

    The function dec_statfun essentially inverts enc_statfun. It is
    necessary for decompressing data, to recover the original symbol.

    0                                                         2 ** precision
    |    a               b               c              d         |
    |----------|-----+--------------|---------|-------------------|
                     ↑
                     cf

    For a number cf in the range [0, 2 ** precision), dec_statfun must return
    the symbol whose range cf lies in, which in the picture above is b.
    """
    def push(message, symbol):
        start, freq = enc_statfun(symbol)
        return vrans.push(message, start, freq, precision)

    def pop(message):
        cf, pop_fun = vrans.pop(message, precision)
        symbol = dec_statfun(cf)
        start, freq = enc_statfun(symbol)
        assert np.all(start <= cf) and np.all(cf < start + freq)
        return pop_fun(start, freq), symbol
    return push, pop

def repeat(codec, n):
    """
    Repeat codec n times.

    Assumes that symbols is a Numpy array with symbols.shape[0] == n. Assume
    that the codec doesn't change the shape of the ANS stack head.
    """
    push_, pop_ = codec
    def push(message, symbols):
        assert np.shape(symbols)[0] == n
        for symbol in reversed(symbols):
            message = push_(message, symbol)
        return message

    def pop(message):
        symbols = []
        for i in range(n):
            message, symbol = pop_(message)
            symbols.append(symbol)
        return message, np.asarray(symbols)
    return push, pop

def serial(codecs):
    """
    Applies given codecs in series.

    Codecs and symbols can be any iterable.
    Codecs are allowed to change the shape of the ANS stack head.
    """
    def push(message, symbols):
        for (push, _), symbol in reversed(list(zip(codecs, symbols))):
            message = push(message, symbol)
        return message

    def pop(message):
        symbols = []
        for _, pop in codecs:
            message, symbol = pop(message)
            symbols.append(symbol)
        return message, symbols

    return push, pop

def substack(codec, view_fun):
    """
    Apply a codec on a subset of a message head.

    view_fun should be a function: head -> subhead, for example
    view_fun = lambda head: head[0]
    to run the codec on only the first element of the head
    """
    push_, pop_ = codec
    def push(message, data, *args, **kwargs):
        head, tail = message
        subhead, update = util.view_update(head, view_fun)
        subhead, tail = push_((subhead, tail), data, *args, **kwargs)
        return update(subhead), tail
    def pop(message, *args, **kwargs):
        head, tail = message
        subhead, update = util.view_update(head, view_fun)
        (subhead, tail), data = pop_((subhead, tail), *args, **kwargs)
        return (update(subhead), tail), data
    return push, pop

def parallel(codecs, view_funs):
    """
    Run a number of independent codecs on different substacks. This could be
    executed in parallel, but when running in the Python interpreter will run
    in series.

    Assumes data is a list, arranged the same way as codecs and view_funs.
    """
    codecs = [substack(codec, view_fun)
              for codec, view_fun in zip(codecs, view_funs)]
    def push(message, symbols):
        assert len(symbols) == len(codecs)
        for (push, _), symbol in reversed(list(zip(codecs, symbols))):
            message = push(message, symbol)
        return message
    def pop(message):
        symbols = []
        for _, pop in codecs:
            message, symbol = pop(message)
            symbols.append(symbol)
        assert len(symbols) == len(codecs)
        return message, symbols
    return push, pop

def shape(message):
    """Get the shape of the message head(s)"""
    head, _ = message
    def _shape(head):
        if type(head) is tuple:
            return tuple(_shape(h) for h in head)
        else:
            return np.shape(head)
    return _shape(head)

_uniform_enc_statfun = lambda s: (s, 1)
_uniform_dec_statfun = lambda cf: cf

def _cdf_to_enc_statfun(cdf):
    def enc_statfun(s):
        lower = cdf(s)
        return lower, cdf(s + 1) - lower
    return enc_statfun

# VAE observation codecs
def _nearest_int(arr):
    return np.uint64(np.ceil(arr - 0.5))

def Uniform(precision):
    """
    Codec for symbols uniformly distributed over range(1 << precision).
    """
    # TODO: special case this in vectorans.py
    return NonUniform(_uniform_enc_statfun, _uniform_dec_statfun, precision)

def Benford64():
    """
    Simple self-delimiting code for numbers x with

        2 ** 31 <= x < 2 ** 63

    with log(x) approximately uniformly distributed. Useful for coding
    vectorans stack heads.
    """
    length_push, length_pop = Uniform(5)
    x_lower_push, x_lower_pop = Uniform(31)
    def push(message, x):
        message = x_lower_push(message, x & ((1 << 31) - 1))
        x_len = np.uint64(np.log2(x))
        x = x & ((1 << x_len) - 1)  # Rm leading 1
        x_higher_push, _ = Uniform(x_len - 31)
        message = x_higher_push(message, x >> 31)
        message = length_push(message, x_len - 31)
        return message

    def pop(message):
        message, x_len = length_pop(message)
        x_len = x_len + 31
        _, x_higher_pop = Uniform(x_len - 31)
        message, x_higher = x_higher_pop(message)
        message, x_lower = x_lower_pop(message)
        return message, (1 << x_len) | (x_higher << 31) | x_lower
    return push, pop
Benford64 = Benford64()

def flatten(message):
    """
    Flatten a message head and tail into a 1d array. Use this when finished
    coding to map to a message representation which can easily be saved to
    disk.

    If the message head is non-scalar it will be efficiently flattened by
    coding elements as if they were data.
    """
    return vrans.flatten(reshape_head(message, (1,)))

def unflatten(arr, shape):
    """
    Unflatten a 1d array, into a vrans message with desired shape. This is the
    inverse of flatten.
    """
    return reshape_head(vrans.unflatten(arr, (1,)), shape)

def _fold_sizes(small, big):
    sizes = [small]
    while small != big:
        small = 2 * small if 2 * small <= big else big
        sizes.append(small)
    return sizes

_fold_codec = lambda diff: substack(Benford64, lambda head: head[:diff])

def _fold_codecs(sizes):
    return [_fold_codec(diff) for diff in np.subtract(sizes[1:], sizes[:-1])]

def _resize_head_1d(message, size):
    head, tail = message
    sizes = _fold_sizes(*sorted((size, np.size(head))))
    codecs = _fold_codecs(sizes)
    if size < np.size(head):
        for (push, _), new_size in reversed(list(zip(codecs, sizes[:-1]))):
            head, tail = push((head[:new_size], tail), head[new_size:])
    elif size > np.size(head):
        for _, pop in codecs:
            (head, tail), head_ex = pop((head, tail))
            head = np.concatenate([head, head_ex])
    return head, tail

def reshape_head(message, shape):
    """
    Reshape the head of a message. Note that growing the head uses up
    information from the message and will fail if the message is empty.
    """
    head, tail = message
    message = (np.ravel(head), tail)
    head, tail = _resize_head_1d(message, size=np.prod(shape))
    return np.reshape(head, shape), tail

def random_stack(flat_len, shape, rng=np.random):
    """Generate a random vrans stack"""
    arr = rng.randint(1 << 32, size=flat_len, dtype='uint32')
    return unflatten(arr, shape)

def _ensure_nonzero_freq_bernoulli(p, precision):
    p[p == 0] += 1
    p[p == (1 << precision)] -=1
    return p

def _bernoulli_cdf(p, precision, safe=True):
    def cdf(s):
        ret = np.zeros(np.shape(s), "uint64")
        onemp = _nearest_int((1 - p[s==1]) * (1 << precision))
        onemp = (_ensure_nonzero_freq_bernoulli(onemp, precision) if safe
                 else onemp)
        ret[s == 1] += onemp
        ret[s == 2] = 1 << precision
        return ret
    return cdf

def _bernoulli_ppf(p, precision, safe=True):
    onemp = _nearest_int((1 - p) * (1 << precision))
    onemp = _ensure_nonzero_freq_bernoulli(onemp, precision) if safe else onemp
    return lambda cf: np.uint64((cf + 0.5) > onemp)

def Bernoulli(p, prec):
    """Codec for Bernoulli distributed data"""
    enc_statfun = _cdf_to_enc_statfun(_bernoulli_cdf(p, prec))
    dec_statfun = _bernoulli_ppf(p, prec)
    return NonUniform(enc_statfun, dec_statfun, prec)

def _cumulative_buckets_from_probs(probs, precision):
    """Ensure each bucket has at least frequency 1"""
    probs = np.rint(probs * (1 << precision)).astype('int64')
    probs[probs == 0] = 1
    # TODO: look at simplifying this
    # Normalize the probabilities by decreasing the maxes
    argmax_idxs = np.argmax(probs, axis=-1)[..., np.newaxis]
    max_value = np.take_along_axis(probs, argmax_idxs, axis=-1)
    diffs = (1 << precision) - np.sum(probs, axis=-1, keepdims=True)
    assert not np.any(max_value + diffs <= 0), \
        "cannot rebalance buckets, consider increasing precision"
    lowered_maxes = (max_value + diffs)
    np.put_along_axis(probs, argmax_idxs, lowered_maxes, axis=-1)
    return np.concatenate((np.zeros(np.shape(probs)[:-1] + (1,), dtype='uint64'),
                           np.cumsum(probs, axis=-1)), axis=-1).astype('uint64')

def _cdf_from_cumulative_buckets(c_buckets):
    def cdf(s):
        ret = np.take_along_axis(c_buckets, s[..., np.newaxis],
                                 axis=-1)
        return ret[..., 0]
    return cdf

def _ppf_from_cumulative_buckets(c_buckets):
    *shape, n = np.shape(c_buckets)
    cumulative_buckets = np.reshape(c_buckets, (-1, n))
    def ppf(cfs):
        cfs = np.ravel(cfs)
        ret = np.array(
            [np.searchsorted(bucket, cf, 'right') - 1 for bucket, cf in
             zip(cumulative_buckets, cfs)])
        return np.reshape(ret, shape)
    return ppf

def Categorical(p, prec):
    """
    Codec for categorical distributed data.
    Assume that the last dim of p contains the probability vectors,
    i.e. np.sum(p, axis=-1) == ones
    """
    cumulative_buckets = _cumulative_buckets_from_probs(p, prec)
    enc_statfun = _cdf_to_enc_statfun(_cdf_from_cumulative_buckets(cumulative_buckets))
    dec_statfun = _ppf_from_cumulative_buckets(cumulative_buckets)
    return NonUniform(enc_statfun, dec_statfun, prec)

def _create_logistic_buckets(means, log_scale, coding_prec, bin_prec, bin_lb, bin_ub):
    buckets = np.linspace(bin_lb, bin_ub, (1 << bin_prec)+1)
    buckets = np.broadcast_to(buckets, means.shape + ((1 << bin_prec)+1,))
    inv_stdv = np.exp(-log_scale)
    cdfs = inv_stdv * (buckets - means[..., np.newaxis])
    cdfs[..., 0] = -np.inf
    cdfs[..., -1] = np.inf
    cdfs = sigmoid(cdfs)
    probs = cdfs[..., 1:] - cdfs[..., :-1]
    return _cumulative_buckets_from_probs(probs, coding_prec)

def _logistic_cdf(means, log_scale, coding_prec, bin_prec):
    inv_stdv = np.exp(-log_scale)
    def cdf(idx):
        # can reduce mem footprint
        buckets = np.linspace(-0.5, 0.5, (1 << bin_prec)+1)
        buckets = np.append(buckets, np.inf)
        bucket_ub = buckets[idx+1]
        scaled = inv_stdv * (bucket_ub - means)
        cdf = sigmoid(scaled)
        return _nearest_int(cdf * (1 << coding_prec))
    return cdf

def _logistic_ppf(means, log_scale, coding_prec, bin_prec):
    stdv = np.exp(log_scale)
    def ppf(cf):
        x = (cf + 0.5) / (1 << coding_prec)
        logit = np.log(x) - np.log(1-x)
        x = logit * stdv + means
        bins = np.linspace(-0.5, 0.5, (1 << bin_prec)+1)[1:]
        return np.uint64(np.digitize(x, bins) - 1)
    return ppf

def Logistic_UnifBins(mean, log_scale, coding_prec, bin_prec, bin_lb, bin_ub,
             no_zero_freqs=True, log_scale_min=-6):
    """
    Codec for logistic distributed data.

    The discretization is assumed to be uniform between bin_lb and bin_ub.
    no_zero_freqs=True will rebalance buckets, but is slower.
    """
    if no_zero_freqs:
        cumulative_buckets = _create_logistic_buckets(mean, log_scale, coding_prec, bin_prec,
                                                      bin_lb, bin_ub)
        enc_statfun = _cdf_to_enc_statfun(_cdf_from_cumulative_buckets(cumulative_buckets))
        dec_statfun = _ppf_from_cumulative_buckets(cumulative_buckets)
    else:
        log_scale = max(log_scale, log_scale_min)
        enc_statfun = _cdf_to_enc_statfun(_logistic_cdf(mean, log_scale, coding_prec, bin_prec))
        dec_statfun = _logistic_ppf(mean, log_scale, coding_prec, bin_prec)
    return NonUniform(enc_statfun, dec_statfun, coding_prec)

def _create_logistic_mixture_buckets(means, log_scales, logit_probs, coding_prec, bin_prec,
                                     bin_lb, bin_ub):
    inv_stdv = np.exp(-log_scales)
    buckets = np.linspace(bin_lb, bin_ub, (1 << bin_prec)+1)
    buckets = np.broadcast_to(buckets, means.shape + ((1 << bin_prec)+1,))
    cdfs = inv_stdv[..., np.newaxis] * (buckets - means[..., np.newaxis])
    cdfs[..., 0] = -np.inf
    cdfs[..., -1] = np.inf
    cdfs = sigmoid(cdfs)
    prob_cpts = cdfs[..., 1:] - cdfs[..., :-1]
    mixture_probs = util.softmax(logit_probs, axis=1)
    probs = np.sum(prob_cpts * mixture_probs[..., np.newaxis], axis=1)
    return _cumulative_buckets_from_probs(probs, coding_prec)

def LogisticMixture_UnifBins(means, log_scales, logit_probs, coding_prec, bin_prec, bin_lb, bin_ub):
    """
    Codec for data from a mixture of logistic distributions.

    The discretization is assumed to be uniform between bin_lb and bin_ub.
    logit_probs are the mixture weights as logits.
    """
    cumulative_buckets = _create_logistic_mixture_buckets(means, log_scales, logit_probs,
                                                          coding_prec, bin_prec, bin_lb, bin_ub)
    enc_statfun = _cdf_to_enc_statfun(_cdf_from_cumulative_buckets(cumulative_buckets))
    dec_statfun = _ppf_from_cumulative_buckets(cumulative_buckets)
    return NonUniform(enc_statfun, dec_statfun, coding_prec)

std_gaussian_bucket_cache = {}  # Stores bucket endpoints
std_gaussian_centres_cache = {}  # Stores bucket centres

def std_gaussian_buckets(precision):
    """
    Return the endpoints of buckets partitioning the domain of the prior. Each
    bucket has mass 1 / (1 << precision) under the prior.
    """
    if precision in std_gaussian_bucket_cache:
        return std_gaussian_bucket_cache[precision]
    else:
        buckets = norm.ppf(np.linspace(0, 1, (1 << precision) + 1))
        std_gaussian_bucket_cache[precision] = buckets
        return buckets

def std_gaussian_centres(precision):
    """
    Return the centres of mass of buckets partitioning the domain of the prior.
    Each bucket has mass 1 / (1 << precision) under the prior.
    """
    if precision in std_gaussian_centres_cache:
        return std_gaussian_centres_cache[precision]
    else:
        centres = np.float32(
            norm.ppf((np.arange(1 << precision) + 0.5) / (1 << precision)))
        std_gaussian_centres_cache[precision] = centres
        return centres

def _gaussian_cdf(mean, stdd, prior_prec, post_prec):
    def cdf(idx):
        x = std_gaussian_buckets(prior_prec)[idx]
        return _nearest_int(norm.cdf(x, mean, stdd) * (1 << post_prec))
    return cdf

def _gaussian_ppf(mean, stdd, prior_prec, post_prec):
    def ppf(cf):
        x = norm.ppf((cf + 0.5) / (1 << post_prec), mean, stdd)
        # Binary search is faster than using the actual gaussian cdf for the
        # precisions we typically use, however the cdf is O(1) whereas search
        # is O(precision), so for high precision cdf will be faster.
        return np.uint64(np.digitize(x, std_gaussian_buckets(prior_prec)) - 1)
    return ppf

def DiagGaussian_StdBins(mean, stdd, coding_prec, bin_prec):
    """
    Codec for data from a diagonal Gaussian with bins that have equal mass under
    a standard (0, I) Gaussian
    """
    enc_statfun = _cdf_to_enc_statfun(
        _gaussian_cdf(mean, stdd, bin_prec, coding_prec))
    dec_statfun = _gaussian_ppf(mean, stdd, bin_prec, coding_prec)
    return NonUniform(enc_statfun, dec_statfun, coding_prec)

def DiagGaussian_GaussianBins(mean, stdd, bin_mean, bin_stdd, coding_prec, bin_prec):
    """
    Codec for data from a diagonal Gaussian with bins that have equal mass under
    a different diagonal Gaussian
    """
    def cdf(idx):
        x = norm.ppf(idx / (1 << bin_prec), bin_mean, bin_stdd)  # this gives lb of bin
        return _nearest_int(norm.cdf(x, mean, stdd) * (1 << coding_prec))

    def ppf(cf):
        x_max = norm.ppf((cf + 0.5) / (1 << coding_prec), mean, stdd)
        # if our gaussians have little overlap, then the cdf could be exactly 1
        # therefore cut off at (1<<bin_prec)-1 to make sure we return a valid bin
        return np.uint64(np.minimum((1 << bin_prec) - 1,
                                    norm.cdf(x_max, bin_mean, bin_stdd) * (1 << bin_prec)))

    enc_statfun = _cdf_to_enc_statfun(cdf)
    return NonUniform(enc_statfun, ppf, coding_prec)

def DiagGaussian_UnifBins(mean, stdd, bin_min, bin_max, coding_prec, n_bins, rebalanced=True):
    """
    Codec for data from a diagonal Gaussian with uniform bins.
    rebalanced=True will ensure no zero frequencies, but is slower.
    """
    if rebalanced:
        bins = np.linspace(bin_min, bin_max, n_bins)
        bins = np.broadcast_to(np.moveaxis(bins, 0, -1), mean.shape + (n_bins,))
        cdfs = norm.cdf(bins, mean[..., np.newaxis], stdd[..., np.newaxis])
        cdfs[..., 0] = 0
        cdfs[..., -1] = 1
        pmfs = cdfs[..., 1:] - cdfs[..., :-1]
        buckets = _cumulative_buckets_from_probs(pmfs, coding_prec)
        enc_statfun = _cdf_to_enc_statfun(_cdf_from_cumulative_buckets(buckets))
        dec_statfun = _ppf_from_cumulative_buckets(buckets)
    else:
        bin_width = (bin_max - bin_min)/float(n_bins)
        def cdf(idx):
            bin_ub = bin_min + idx * bin_width
            return _nearest_int(norm.cdf(bin_ub, mean, stdd) * (1 << coding_prec))
        def ppf(cf):
            x_max = norm.ppf((cf + 0.5) / (1 << coding_prec), mean, stdd)
            bin_idx = np.floor((x_max - bin_min) / bin_width)
            return np.uint64(np.minimum(n_bins-1, bin_idx))
        enc_statfun = _cdf_to_enc_statfun(cdf)
        dec_statfun = ppf
    return NonUniform(enc_statfun, dec_statfun, coding_prec)

def AutoRegressive(param_fn, data_shape, params_shape, elem_idxs, elem_codec):
    """
    Codec for data from distributions which are calculated autoregressively.
    That is, the data can be partitioned into n elements such that the
    distribution/codec for an element is only known when all previous
    elements are known. This is does not affect the push step, but does
    affect the pop step, which must be done in sequence (so is slower).

    elem_param_fn maps data to the params for the respective codecs.
    elem_idxs defines the ordering over elements within data.

    We assume that the indices within elem_idxs can also be used to index
    the params from elem_param_fn. These indexed params are then used in
    the elem_codec to actually code each element.
    """
    def push(message, data, all_params=None):
        if not all_params:
            all_params = param_fn(data)
        for idx in reversed(elem_idxs):
            elem_params = all_params[idx]
            elem_push, _ = elem_codec(elem_params, idx)
            message = elem_push(message, data[idx].astype('uint64'))
        return message

    def pop(message):
        data = np.zeros(data_shape, dtype=np.uint64)
        all_params = np.zeros(params_shape, dtype=np.float32)
        for idx in elem_idxs:
            all_params = param_fn(data, all_params, idx)
            elem_params = all_params[idx]
            _, elem_pop = elem_codec(elem_params, idx)
            message, elem = elem_pop(message)
            data[idx] = elem
        return message, data
    return push, pop
