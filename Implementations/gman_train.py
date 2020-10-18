import math
import argparse
import time, datetime
import numpy as np
import tensorflow as tf
from tqdm import tqdm

from lib.gman_utils import loadData
from lib.metrics.metrics_tf import masked_mae_loss
from lib.metrics.metrics_np import calculate_metrics, masked_mae_np
from model.tf import gman_model

parser = argparse.ArgumentParser()
parser.add_argument('--time_slot', type=int, default=5,
                    help='a time step is 5 mins')
parser.add_argument('--num_his', type=int, default=12,
                    help='history steps')
parser.add_argument('--num_pred', type=int, default=12,
                    help='prediction steps')
parser.add_argument('--L', type=int, default=1,
                    help='number of STAtt Blocks')
parser.add_argument('--K', type=int, default=8,
                    help='number of attention heads')
parser.add_argument('--d', type=int, default=8,
                    help='dims of each head attention outputs')
parser.add_argument('--train_ratio', type=float, default=0.7,
                    help='training set [default : 0.7]')
parser.add_argument('--val_ratio', type=float, default=0.1,
                    help='validation set [default : 0.1]')
parser.add_argument('--test_ratio', type=float, default=0.2,
                    help='testing set [default : 0.2]')
parser.add_argument('--batch_size', type=int, default=32,
                    help='batch size')
parser.add_argument('--max_epoch', type=int, default=1000,
                    help='epoch to run')
parser.add_argument('--patience', type=int, default=10,
                    help='patience for early stop')
parser.add_argument('--learning_rate', type=float, default=0.001,
                    help='initial learning rate')
parser.add_argument('--decay_epoch', type=int, default=5,
                    help='decay epoch')
parser.add_argument('--traffic_file', default='data/metr-la/metr-la.h5',
                    help='traffic file')
parser.add_argument('--SE_file', default='data/metr-la/SE.txt',
                    help='spatial emebdding file')
parser.add_argument('--model_file', default='data/metr-la/pretrained/GMAN_latest',
                    help='save the model to disk')
# parser.add_argument('--log_file', default='data/metr-la/logs/gman_log',
#                     help='log file')
args = parser.parse_args()

start = time.time()

print(str(args)[10: -1])

# load data
print('loading data...')
(trainX, trainTE, trainY, valX, valTE, valY, testX, testTE, testY, SE,
 mean, std, ds) = loadData(args)
data = ds.data
print('trainX: %s\ttrainY: %s' % (trainX.shape, trainY.shape))
print('valX:   %s\t\tvalY:   %s' % (valX.shape, valY.shape))
print('testX:  %s\t\ttestY:  %s' % (testX.shape, testY.shape))
print('data loaded!')

# train model
print('compiling model...')
T = 24 * 60 // args.time_slot
num_train, _, num_vertex = trainX.shape
X, TE, label, is_training = gman_model.placeholder(
    args.num_his, args.num_pred, num_vertex)
global_step = tf.Variable(0, trainable=False)
bn_momentum = tf.train.exponential_decay(
    0.5, global_step,
    decay_steps=args.decay_epoch * num_train // args.batch_size,
    decay_rate=0.5, staircase=True)
bn_decay = tf.minimum(0.99, 1 - bn_momentum)
pred = gman_model.GMAN(
    X, TE, SE, args.num_his, args.num_pred, T, args.L, args.K, args.d,
    bn=True, bn_decay=bn_decay, is_training=is_training)
# ADDED inverse scaling after model output
null_val = 0.
loss_fn = masked_mae_loss(ds.scaler, null_val)
loss = loss_fn(preds=pred, labels=label)
# loss = gman_mae_loss(pred, label)
tf.compat.v1.add_to_collection('pred', pred)
tf.compat.v1.add_to_collection('loss', loss)
learning_rate = tf.train.exponential_decay(
    args.learning_rate, global_step,
    decay_steps=args.decay_epoch * num_train // args.batch_size,
    decay_rate=0.7, staircase=True)
learning_rate = tf.maximum(learning_rate, 1e-5)
optimizer = tf.compat.v1.train.AdamOptimizer(learning_rate)
train_op = optimizer.minimize(loss, global_step=global_step)
parameters = 0
for variable in tf.compat.v1.trainable_variables():
    parameters += np.product([x.value for x in variable.get_shape()])
print('trainable parameters: {:,}'.format(parameters))
print('model compiled!')
saver = tf.compat.v1.train.Saver()
config = tf.compat.v1.ConfigProto()
config.gpu_options.allow_growth = True
sess = tf.compat.v1.Session(config=config)
sess.run(tf.compat.v1.global_variables_initializer())
print('**** training model ****')
num_val = valX.shape[0]
wait = 0
val_loss_min = np.inf
for epoch in range(args.max_epoch):
    if wait >= args.patience:
        print('early stop at epoch: %04d' % (epoch))
        break

    # shuffle
    permutation = np.random.permutation(num_train)
    trainX = trainX[permutation]
    trainTE = trainTE[permutation]
    trainY = trainY[permutation]
    # train loss
    start_train = time.time()
    train_loss = 0
    num_batch = math.ceil(num_train / args.batch_size)
    for batch_idx in tqdm(range(num_batch)):
        start_idx = batch_idx * args.batch_size
        end_idx = min(num_train, (batch_idx + 1) * args.batch_size)
        feed_dict = {
            X: trainX[start_idx: end_idx],
            TE: trainTE[start_idx: end_idx],
            label: trainY[start_idx: end_idx],
            is_training: True}
        _, loss_batch = sess.run([train_op, loss], feed_dict=feed_dict)
        train_loss += loss_batch * (end_idx - start_idx)
    train_loss /= num_train
    end_train = time.time()
    # val loss
    start_val = time.time()
    val_loss = 0
    num_batch = math.ceil(num_val / args.batch_size)
    for batch_idx in range(num_batch):
        start_idx = batch_idx * args.batch_size
        end_idx = min(num_val, (batch_idx + 1) * args.batch_size)
        feed_dict = {
            X: valX[start_idx: end_idx],
            TE: valTE[start_idx: end_idx],
            label: valY[start_idx: end_idx],
            is_training: False}
        loss_batch = sess.run(loss, feed_dict=feed_dict)
        val_loss += loss_batch * (end_idx - start_idx)
    val_loss /= num_val
    end_val = time.time()
    print('%s | epoch: %04d/%d, training time: %.1fs, inference time: %.1fs' %
          (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), epoch + 1,
           args.max_epoch, end_train - start_train, end_val - start_val))
    print('train loss: %.4f, val_loss: %.4f' % (train_loss, val_loss))
    if val_loss <= val_loss_min:
        print('val loss decrease from %.4f to %.4f, saving model to %s' %
              (val_loss_min, val_loss, args.model_file))
        wait = 0
        val_loss_min = val_loss
        saver.save(sess, args.model_file)
    else:
        wait += 1

# test model
print('**** testing model ****')
print('loading model from %s' % args.model_file)
saver = tf.compat.v1.train.import_meta_graph(args.model_file + '.meta')
saver.restore(sess, args.model_file)
print('model restored!')
print('evaluating...')
print('                MAE\t\tRMSE\t\tMAPE')


# Evaluation
def compute_preds(sess, setX, setTE, batch_size):
    num_x = setX.shape[0]
    preds = []
    num_batch = math.ceil(num_x / batch_size)
    for batch_idx in tqdm(range(num_batch)):
        start_idx = batch_idx * args.batch_size
        end_idx = min(num_x, (batch_idx + 1) * batch_size)
        feed_dict = {
            X: setX[start_idx: end_idx],
            TE: setTE[start_idx: end_idx],
            is_training: False}
        pred_batch = sess.run(pred, feed_dict=feed_dict)
        preds.append(pred_batch)
    preds = np.concatenate(preds, axis=0)
    preds = ds.scaler.inverse_transform(preds)
    return preds


print('Evaluating...')

# trainPred = compute_preds(trainX, trainTE, args.batch_size)
# train_mae, train_rmse, train_mape = calculate_metrics(trainPred, trainY)
# print('train            %.2f\t\t%.2f\t\t%.2f%%' %
#                  (train_mae, train_rmse, train_mape * 100))

valPred = compute_preds(sess, valX, valTE, batch_size=args.batch_size)
val_mae, val_rmse, val_mape = calculate_metrics(valPred, valY)
print('val              %.2f\t\t%.2f\t\t%.2f%%' %
      (val_mae, val_rmse, val_mape * 100))

testPred = compute_preds(sess, testX, testTE, batch_size=args.batch_size)
test_mae, test_rmse, test_mape = calculate_metrics(testPred, testY)
print('test             %.2f\t\t%.2f\t\t%.2f%%' %
      (test_mae, test_rmse, test_mape * 100))

ds.experiment_save(masked_mae_np(testPred, testY, null_val=0.0), fname='results/gman_predictions')

print('performance in each prediction step')
MAE, RMSE, MAPE = [], [], []
for step in range(args.num_pred):
    mae, rmse, mape = calculate_metrics(testPred[:, step], testY[:, step])
    MAE.append(mae)
    RMSE.append(rmse)
    MAPE.append(mape)
    print('step: %02d         %.2f\t\t%.2f\t\t%.2f%%' %
          (step + 1, mae, rmse, mape * 100))
average_mae = np.mean(MAE)
average_rmse = np.mean(RMSE)
average_mape = np.mean(MAPE)
print('average:         %.2f\t\t%.2f\t\t%.2f%%' %
      (average_mae, average_rmse, average_mape * 100))


print('Evaluating with simulated sensor failure...')

loader = data['val_loader']
all_preds = []
valPred = compute_preds(sess, valX, valTE, batch_size=args.batch_size)
dis_sensors = range(206)  # [189, 200]

for idx, s in tqdm(enumerate(dis_sensors)):
    augmentation_matrix = np.zeros(207)
    augmentation_matrix[s] = 1

    # Generate augmented datasets
    augmented_dataloader = loader.augment(augmentation_matrix)
    # Do inference [3392, 207, 12]
    valX2_it = augmented_dataloader.get_iterator()
    valX2 = np.array([*valX2_it])
    valX2 = valX2[:, 0, ...].squeeze()
    aug_preds = compute_preds(sess, valX2, valTE, batch_size=args.batch_size)
    val_mae, val_rmse, val_mape = calculate_metrics(aug_preds, valY)
    print('val              %.2f\t\t%.2f\t\t%.2f%%' %
          (val_mae, val_rmse, val_mape * 100))
    # relative_err = MAPE(normal) - MAPE(augmented)
    #  mae error per sensor before - error per sensor after
    err_rel = (valY != 0).astype(np.uint8) * (np.abs(valPred - valY) - np.abs(aug_preds - valY))
    # Scale relative_err per 'frame' with softmax, then average over time.
    all_preds.append(err_rel)
    print(err_rel.shape)

pred_mx = np.stack(all_preds)
# Aggregate over time
pred_mx = np.sum(pred_mx, axis=1) / pred_mx.shape[1]
# Switch cols timesteps and sensors
pred_mx = pred_mx.transpose((0, 2, 1))
print(pred_mx.shape)
ds.experiment_save(pred_mx, 'results/gman_preds')


end = time.time()
print('total time: %.1fmin' % ((end - start) / 60))
sess.close()
