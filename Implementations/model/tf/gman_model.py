import tensorflow as tf

from model.tf import layers_tf


def placeholder(num_his, num_pred, num_vertex):
    X = tf.compat.v1.placeholder(
        shape = (None, num_his, num_vertex), dtype = tf.float32)
    TE = tf.compat.v1.placeholder(
        shape = (None, num_his + num_pred, 2), dtype = tf.int32)
    label = tf.compat.v1.placeholder(
        shape = (None, num_pred, num_vertex), dtype = tf.float32)
    is_training = tf.compat.v1.placeholder(shape = (), dtype = tf.bool)
    return X, TE, label, is_training

def FC(x, units, activations, bn, bn_decay, is_training, use_bias = True):
    if isinstance(units, int):
        units = [units]
        activations = [activations]
    elif isinstance(units, tuple):
        units = list(units)
        activations = list(activations)
    assert type(units) == list
    for num_unit, activation in zip(units, activations):
        x = layers_tf.conv2d(
            x, output_dims = num_unit, kernel_size = [1, 1], stride = [1, 1],
            padding = 'VALID', use_bias = use_bias, activation = activation,
            bn = bn, bn_decay = bn_decay, is_training = is_training)
    return x

def spatialAttention(X, STE, K, d, bn, bn_decay, is_training):
    '''
    spatial attention mechanism
    X:      [batch_size, num_step, num_vertex, D]
    STE:    [batch_size, num_step, num_vertex, D]
    K:      number of attention heads
    d:      dimension of each attention outputs
    return: [batch_size, num_step, num_vertex, D]
    '''
    D = K * d
    X = tf.concat((X, STE), axis = -1)
    # [batch_size, num_step, num_vertex, K * d]
    query = FC(
        X, units = D, activations = tf.nn.relu,
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    key = FC(
        X, units = D, activations = tf.nn.relu,
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    value = FC(
        X, units = D, activations = tf.nn.relu,
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    # [K * batch_size, num_step, num_vertex, d]
    query = tf.concat(tf.split(query, K, axis = -1), axis = 0)
    key = tf.concat(tf.split(key, K, axis = -1), axis = 0)
    value = tf.concat(tf.split(value, K, axis = -1), axis = 0)
    # [K * batch_size, num_step, num_vertex, num_vertex]
    attention = tf.matmul(query, key, transpose_b = True)
    attention /= (d ** 0.5)
    attention = tf.nn.softmax(attention, axis = -1)
    # [batch_size, num_step, num_vertex, D]
    X = tf.matmul(attention, value)
    X = tf.concat(tf.split(X, K, axis = 0), axis = -1)
    X = FC(
        X, units = [D, D], activations = [tf.nn.relu, None],
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    return X

def temporalAttention(X, STE, K, d, bn, bn_decay, is_training, mask = True):
    '''
    temporal attention mechanism
    X:      [batch_size, num_step, num_vertex, D]
    STE:    [batch_size, num_step, num_vertex, D]
    K:      number of attention heads
    d:      dimension of each attention outputs
    return: [batch_size, num_step, num_vertex, D]
    '''
    D = K * d
    X = tf.concat((X, STE), axis = -1)
    # [batch_size, num_step, num_vertex, K * d]
    query = FC(
        X, units = D, activations = tf.nn.relu,
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    key = FC(
        X, units = D, activations = tf.nn.relu,
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    value = FC(
        X, units = D, activations = tf.nn.relu,
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    # [K * batch_size, num_step, num_vertex, d]
    query = tf.concat(tf.split(query, K, axis = -1), axis = 0)
    key = tf.concat(tf.split(key, K, axis = -1), axis = 0)
    value = tf.concat(tf.split(value, K, axis = -1), axis = 0)
    # query: [K * batch_size, num_vertex, num_step, d]
    # key:   [K * batch_size, num_vertex, d, num_step]
    # value: [K * batch_size, num_vertex, num_step, d]
    query = tf.transpose(query, perm = (0, 2, 1, 3))
    key = tf.transpose(key, perm = (0, 2, 3, 1))
    value = tf.transpose(value, perm = (0, 2, 1, 3))
    # [K * batch_size, num_vertex, num_step, num_step]
    attention = tf.matmul(query, key)
    attention /= (d ** 0.5)
    # mask attention score
    if mask:
        batch_size = tf.shape(X)[0]
        num_step = X.get_shape()[1].value
        num_vertex = X.get_shape()[2].value
        mask = tf.ones(shape = (num_step, num_step))
        mask = tf.linalg.LinearOperatorLowerTriangular(mask).to_dense()
        mask = tf.expand_dims(tf.expand_dims(mask, axis = 0), axis = 0)
        mask = tf.tile(mask, multiples = (K * batch_size, num_vertex, 1, 1))
        mask = tf.cast(mask, dtype = tf.bool)
        attention = tf.compat.v2.where(
            condition = mask, x = attention, y = -2 ** 15 + 1)
    # softmax   
    attention = tf.nn.softmax(attention, axis = -1)
    # [batch_size, num_step, num_vertex, D]
    X = tf.matmul(attention, value)
    X = tf.transpose(X, perm = (0, 2, 1, 3))
    X = tf.concat(tf.split(X, K, axis = 0), axis = -1)
    X = FC(
        X, units = [D, D], activations = [tf.nn.relu, None],
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    return X

def gatedFusion(HS, HT, D, bn, bn_decay, is_training):
    '''
    gated fusion
    HS:     [batch_size, num_step, num_vertex, D]
    HT:     [batch_size, num_step, num_vertex, D]
    D:      output dims
    return: [batch_size, num_step, num_vertex, D]
    '''
    XS = FC(
        HS, units = D, activations = None,
        bn = bn, bn_decay = bn_decay,
        is_training = is_training, use_bias = False)
    XT = FC(
        HT, units = D, activations = None,
        bn = bn, bn_decay = bn_decay,
        is_training = is_training, use_bias = True)
    z = tf.nn.sigmoid(tf.add(XS, XT))
    H = tf.add(tf.multiply(z, HS), tf.multiply(1 - z, HT))
    H = FC(
        H, units = [D, D], activations = [tf.nn.relu, None],
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    return H

def STAttBlock(X, STE, K, d, bn, bn_decay, is_training, mask = False):
    HS = spatialAttention(X, STE, K, d, bn, bn_decay, is_training)
    HT = temporalAttention(X, STE, K, d, bn, bn_decay, is_training, mask = mask)
    H = gatedFusion(HS, HT, K * d, bn, bn_decay, is_training)
    return tf.add(X, H)

def transformAttention(X, STE_his, STE_pred, K, d, bn, bn_decay, is_training):
    '''
    transform attention mechanism
    X:        [batch_size, num_his, num_vertex, D]
    STE_his:  [batch_size, num_his, num_vertex, D]
    STE_pred: [batch_size, num_pred, num_vertex, D]
    K:        number of attention heads
    d:        dimension of each attention outputs
    return:   [batch_size, num_pred, num_vertex, D]
    '''
    D = K * d
    # [batch_size, num_step, num_vertex, K * d]
    query = FC(
        STE_pred, units = D, activations = tf.nn.relu,
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    key = FC(
        STE_his, units = D, activations = tf.nn.relu,
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    value = FC(
        X, units = D, activations = tf.nn.relu,
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    # [K * batch_size, num_step, num_vertex, d]
    query = tf.concat(tf.split(query, K, axis = -1), axis = 0)
    key = tf.concat(tf.split(key, K, axis = -1), axis = 0)
    value = tf.concat(tf.split(value, K, axis = -1), axis = 0)
    # query: [K * batch_size, num_vertex, num_pred, d]
    # key:   [K * batch_size, num_vertex, d, num_his]
    # value: [K * batch_size, num_vertex, num_his, d]
    query = tf.transpose(query, perm = (0, 2, 1, 3))
    key = tf.transpose(key, perm = (0, 2, 3, 1))
    value = tf.transpose(value, perm = (0, 2, 1, 3))    
    # [K * batch_size, num_vertex, num_pred, num_his]
    attention = tf.matmul(query, key)
    attention /= (d ** 0.5)
    attention = tf.nn.softmax(attention, axis = -1)
    # [batch_size, num_pred, num_vertex, D]
    X = tf.matmul(attention, value)
    X = tf.transpose(X, perm = (0, 2, 1, 3))
    X = tf.concat(tf.split(X, K, axis = 0), axis = -1)
    X = FC(
        X, units = [D, D], activations = [tf.nn.relu, None],
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    return X

def STEmbedding(SE, TE, T, D, bn, bn_decay, is_training):
    '''
    spatio-temporal embedding
    SE:     [num_vertex, D]
    TE:     [batch_size, num_his + num_pred, 2] (dayofweek, timeofday)
    T:      num of time steps in one day
    D:      output dims
    retrun: [batch_size, num_his + num_pred, num_vertex, D]
    '''
    batch_size = tf.shape(TE)[0]
    num_frame = TE.get_shape()[1].value
    num_vertex = SE.shape[0]
    # spatial embedding
    SE = tf.expand_dims(tf.expand_dims(SE, axis = 0), axis = 0)
    SE = FC(
        SE, units = [D, D], activations = [tf.nn.relu, None],
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    # temporal embedding
    dayofweek = tf.one_hot(TE[..., 0], depth = 7)
    timeofday = tf.one_hot(TE[..., 1], depth = T)
    TE = tf.concat((dayofweek, timeofday), axis = -1)
    TE = tf.expand_dims(TE, axis = 2)
    TE = FC(
        TE, units = [D, D], activations = [tf.nn.relu, None],
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    return tf.add(SE, TE)

def GMAN(X, TE, SE, num_his, num_pred, T, L, K, d, bn, bn_decay, is_training):
    '''
    GMAN
    X：       [batch_size, num_his, num_vertex]
    TE：      [batch_size, num_his + num_pred, 2] (time-of-day, day-of-week)
    SE：      [num_vertex, K * d]
    num_his： number of history steps
    num_pred：number of prediction steps
    T：       one day is divided into T steps
    L：       number of STAtt blocks in the encoder/decoder
    K：       number of attention heads
    d：       dimension of each attention head outputs
    return：  [batch_size, num_pred, num_vertex]
    '''
    D = K * d
    # input
    X = tf.expand_dims(X, axis = -1)
    X = FC(
        X, units = [D, D], activations = [tf.nn.relu, None],
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    # STE
    STE = STEmbedding(SE, TE, T, D, bn, bn_decay, is_training)
    STE_his = STE[:, :num_his]
    STE_pred = STE[:, num_his:]
    # encoder
    for _ in range(L):
        X = STAttBlock(X, STE_his, K, d, bn, bn_decay, is_training)
    # transAtt
    X = transformAttention(
        X, STE_his, STE_pred, K, d, bn, bn_decay, is_training)
    # decoder
    for _ in range(L):
        X = STAttBlock(X, STE_pred, K, d, bn, bn_decay, is_training)
    # output
    X = FC(
        X, units = [D, 1], activations = [tf.nn.relu, None],
        bn = bn, bn_decay = bn_decay, is_training = is_training)
    return tf.squeeze(X, axis = 3)

