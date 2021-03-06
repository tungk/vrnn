import tensorflow as tf
import numpy as np


def sample(params, eps, dist='gauss'):
    """ utility function for sampling from distributions, given noise """
    if 'bin' in dist:
        logits = params[-1]
        params = params[:-1]
    if 'gauss' in dist:
        mean, cov = params
        s = mean + tf.sqrt(cov) * eps
    elif 'gm' in dist:
        means, covs, pi_logits = params
        choices = tf.multinomial(pi_logits, num_samples=1)
        batch_size = choices.get_shape()[0]
        ids = tf.constant(list(range(batch_size)), dtype=tf.int64, shape=(batch_size, 1))
        idx_tensor = tf.concat([ids, choices], axis=1)
        chosen_means = tf.gather_nd(means, idx_tensor)
        chosen_covs = tf.gather_nd(covs, idx_tensor)
        s = chosen_means + tf.sqrt(chosen_covs) * eps
    else:
        raise NotImplementedError

    if 'bin' in dist:
        sig = tf.sigmoid(logits)
        s = tf.concat([s, sig], axis=1)
    return s


def inference(in_pl, hid_pl, f_state, eps_z, fd):
    """ builds inference model for one time step """
    phi_x = fd['phi_x'](in_pl)
    mean_0, cov_0 = fd['phi_prior'](hid_pl)
    mean_z, cov_z = fd['phi_enc'](phi_x, hid_pl)
    z = sample((mean_z, cov_z), eps_z, 'gauss')
    phi_z = fd['phi_z'](z)
    params_out = fd['phi_dec'](phi_z, hid_pl)
    f_in = tf.concat([phi_x, phi_z], axis=1, name='f_theta_joint_inputs')
    f_out, f_state = fd['f_theta'](f_in, f_state)
    return mean_0, cov_0, mean_z, cov_z, params_out, f_out, f_state


def gaussian_log_p(params_out, x_target, dim):
    """ computes log probability of target in Gaussian with given parameters """
    mean_x, cov_x = params_out
    x_diff = x_target - mean_x
    x_square = tf.reduce_sum((x_diff / cov_x) * x_diff, axis=[1])
    log_x_exp = -0.5 * x_square
    log_cov_x_det = tf.reduce_sum(tf.log(cov_x), axis=[1])
    log_x_norm = -0.5 * (dim * tf.log(2 * np.pi) + log_cov_x_det)
    log_p = log_x_norm + log_x_exp
    return log_p, log_x_norm, log_x_exp, tf.abs(x_diff)


def gm_log_p(params_out, x_target, dim):
    """ computes log probability of target in Gaussian mixture with given parameters """
    mean_x, cov_x, pi_x_logit = params_out
    pi_x = tf.nn.softmax(pi_x_logit)
    mean_x = tf.transpose(mean_x, perm=[1, 0, 2])
    cov_x = tf.transpose(cov_x, perm=[1, 0, 2])
    pi_x = tf.transpose(pi_x, perm=[1, 0])

    x_diff = x_target - mean_x
    x_square = tf.reduce_sum((x_diff / cov_x) * x_diff, axis=[2])
    log_x_exp = -0.5 * x_square
    log_cov_x_det = tf.reduce_sum(tf.log(cov_x), axis=[2])
    log_x_norm = -0.5 * (dim * tf.log(2 * np.pi) + log_cov_x_det) + pi_x
    log_p = tf.reduce_logsumexp(log_x_norm + log_x_exp, axis=[0])
    return log_p, log_x_norm, log_x_exp, tf.abs(x_diff)


def gaussian_kl_div(mean_0, cov_0, mean_1, cov_1, dim):
    """ computes KL divergences between two Gaussians with given parameters"""
    mean_diff = mean_1 - mean_0
    cov_1_inv = tf.reciprocal(cov_1)
    log_cov_1_det = tf.reduce_sum(tf.log(cov_1), axis=[1])
    log_cov_0_det = tf.reduce_sum(tf.log(cov_0), axis=[1])
    log_term = log_cov_1_det - log_cov_0_det
    trace_term = tf.reduce_sum(cov_1_inv * cov_0, axis=[1])
    square_term = tf.reduce_sum(mean_diff * cov_1_inv * mean_diff, axis=[1])
    kl_div = 0.5 * (trace_term + square_term - dim + log_term)
    return kl_div


def ce_loss(logits_out, bin_target):
    """ computes binary cross entropy loss given logit estimate and target"""
    l = tf.nn.sigmoid_cross_entropy_with_logits(logits=logits_out, labels=bin_target, name='ce_loss')
    return tf.squeeze(l)


def loss(x_target, mean_0, cov_0, mean_z, cov_z, params_out, param_dict):
    """ 
    computes variational upper bound, depending on output distribution and optionally adds binary CE loss 
    employs masking, if enabled. also returns partial losses in most setups (inaccurate with masking)
    """
    maybe_ce = []

    kl_div = gaussian_kl_div(mean_z, cov_z, mean_0, cov_0, param_dict['z_dim'])
    if param_dict['model'] == 'gauss_out':
        log_p, log_x_norm, log_x_exp, abs_diff = gaussian_log_p(params_out, x_target, param_dict['x_dim'])
    elif param_dict['model'] == 'gm_out':
        log_p, log_x_norm, log_x_exp, abs_diff = gm_log_p(params_out, x_target, param_dict['x_dim'])
    elif param_dict['model'] == 'gauss_out_bin':
        dist_target = tf.slice(x_target, [0, 0], [-1, param_dict['x_dim']])
        bin_target = tf.slice(x_target, [0, param_dict['x_dim']], [-1, 1])
        log_p, log_x_norm, log_x_exp, abs_diff = gaussian_log_p(params_out[:-1], dist_target, param_dict['x_dim'])
        maybe_ce = [ce_loss(params_out[-1], bin_target)]
    elif param_dict['model'] == 'gm_out_bin':
        dist_target = tf.slice(x_target, [0, 0], [-1, param_dict['x_dim']])
        bin_target = tf.slice(x_target, [0, param_dict['x_dim']], [-1, 1])
        log_p, log_x_norm, log_x_exp, abs_diff = gm_log_p(params_out[:-1], dist_target, param_dict['x_dim'])
        maybe_ce = [ce_loss(params_out[-1], bin_target)]
    else:
        raise NotImplementedError

    if param_dict['masking']:
        zero_vals = tf.abs(x_target - tf.constant(param_dict['mask_value'], dtype=tf.float32))
        mask = tf.sign(tf.reduce_max(zero_vals, axis=1))
        num_live_samples = tf.reduce_sum(mask, axis=0)
        log_p = tf.reduce_sum(tf.where(tf.equal(mask, 0.0), mask, log_p), name='log_p_sum') / num_live_samples
        kl_div = tf.reduce_sum(tf.where(tf.equal(mask, 0.0), mask, kl_div), name='kl_div_sum') / num_live_samples
        bound = (param_dict['kl_weight'] * kl_div) - log_p
        if 'bin' in param_dict['model']:
            maybe_ce[0] = tf.where(tf.equal(mask, 0.0), mask, maybe_ce[0])
            maybe_ce[0] = tf.reduce_sum(maybe_ce[0]) / num_live_samples
            bound += maybe_ce[0]
        # norm = tf.reduce_sum(tf.where(tf.equal(mask, 0.0), mask, log_x_norm)) / num_live_samples
        # exp = tf.reduce_sum(tf.where(tf.equal(mask, 0.0), mask, log_x_exp)) / num_live_samples
        # diff = tf.reduce_sum(tf.where(tf.equal(mask, 0.0), mask,
        #                               tf.reduce_mean(abs_diff, axis=[1]))) / num_live_samples

    else:
        kl_div = tf.reduce_mean(kl_div)
        log_p = tf.reduce_mean(log_p)
        bound = (param_dict['kl_weight'] * kl_div) - log_p
        if 'bin' in param_dict['model']:
            bound += tf.reduce_mean(maybe_ce[0])

    norm = tf.reduce_mean(log_x_norm)
    exp = tf.reduce_mean(log_x_exp)
    diff = tf.reduce_mean(abs_diff)
    sub_losses = [kl_div, log_p, norm, exp, diff] + maybe_ce

    return bound, sub_losses


def optimization(err_acc, learning_rate):
    """
    creates train operation using ADAM and gradient clipping
    """
    optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)
    tvars = tf.trainable_variables()
    grads = tf.gradients(err_acc, tvars)
    tg_pairs = [(tf.clip_by_value(k[0], -100, 100), k[1]) for k in zip(grads, tvars) if k[0] is not None]
    train_op = optimizer.apply_gradients(tg_pairs)
    return train_op


def train_loop(x_pl, f_theta, bound_acc, count, f_state, eps_z, param_dict, fun_dict, tracked_tensors):
    """
    executes one timestep of inference and accumulates loss (and partial losses) passes results for next timestep
    """
    x_t = tf.squeeze(tf.slice(x_pl, [tf.to_int32(count), 0, 0], [1, -1, -1]), axis=[0])
    eps_z_t = tf.squeeze(tf.slice(eps_z, [tf.to_int32(count), 0, 0], [1, -1, -1]), axis=[0])

    mean_0, cov_0, mean_z, cov_z, params_out, f_theta, f_state = inference(x_t, f_theta, f_state, eps_z_t, fun_dict)
    bound_step, sub_losses_step = loss(x_t, mean_0, cov_0, mean_z, cov_z, params_out, param_dict)

    bound_acc += bound_step
    sub_losses_acc = tracked_tensors[0]
    sub_losses_acc = [a + s for (a, s) in zip(sub_losses_acc, sub_losses_step)]

    mean_x, cov_x = params_out[:2]
    dist_params = [mean_0, cov_0, mean_z, cov_z, mean_x, cov_x]

    tracked_tensors = [sub_losses_acc, dist_params]
    count += 1
    return x_pl, f_theta, bound_acc, count, f_state, eps_z, tracked_tensors


def get_train_loop_fun(param_dict, fun_dict):
    """ function wrapper to assign the dicts. return value can be looped with tf.while_loop """
    def train_loop_fun(x_pl, hid_pl, err_acc, count, f_state, eps_z, debug_tensors):
        return train_loop(x_pl, hid_pl, err_acc, count, f_state, eps_z, param_dict, fun_dict, debug_tensors)
    return train_loop_fun


def get_train_stop_fun(num_iter):
    """ sequence length counter needed for while loop """
    def train_stop_fun(*args):
        count = args[3]
        return tf.less(count, num_iter)
    return train_stop_fun


def generation(hid_pl, f_state, eps_z, eps_x, pd, fd):
    """ builds generative model for one time step """
    params_prior = fd['phi_prior'](hid_pl)
    z = sample(params_prior, eps_z, 'gauss')
    phi_z = fd['phi_z'](z)
    params_out = fd['phi_dec'](phi_z, hid_pl)
    x = sample(params_out, eps_x, pd['model'])

    phi_x = fd['phi_x'](x)
    f_in = tf.concat([phi_x, phi_z], axis=1, name='f_theta_joint_inputs')
    f_out, f_state = fd['f_theta'](f_in, f_state)
    return x, f_out, f_state


def gen_loop(x_pl, hid_pl, count, f_state, eps_z, eps_x, pd, fun_dict):
    """
    executes one timestep of generation and accumulates results, writing to x_pl. passes results for next timestep
    """
    eps_z_t = tf.squeeze(tf.slice(eps_z, [tf.to_int32(count), 0, 0], [1, -1, -1]), axis=[0])
    eps_x_t = tf.squeeze(tf.slice(eps_x, [tf.to_int32(count), 0, 0], [1, -1, -1]), axis=[0])

    x_t, f_out, f_state = generation(hid_pl, f_state, eps_z_t, eps_x_t, pd, fun_dict)

    x_old = tf.slice(x_pl, [0, 0, 0], [tf.to_int32(count), -1, -1])
    x_empty = tf.slice(x_pl, [tf.to_int32(count) + 1, 0, 0], [-1, -1, -1])
    x_t = tf.reshape(x_t, [1, pd['batch_size'], pd['in_dim']])
    x_pl = tf.concat([x_old, x_t, x_empty], axis=0)
    x_pl.set_shape([pd['seq_length'], pd['batch_size'], pd['in_dim']])

    count += 1
    return x_pl, f_out, count, f_state, eps_z, eps_x


def get_gen_loop_fun(param_dict, fun_dict):
    """ function wrapper to assign the dicts. return value can be looped with tf.while_loop """
    def f(x_pl, hid_pl, count, f_state, eps_z, eps_x):
        return gen_loop(x_pl, hid_pl, count, f_state, eps_z, eps_x, param_dict, fun_dict)
    return f


def get_gen_stop_fun(num_iter):
    """ sequence length counter needed for while loop """
    def gen_stop_fun(*args):
        count = args[2]
        return tf.less(count, num_iter)
    return gen_stop_fun
