# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Tests for baseline.py."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import shutil
import tempfile

import numpy as np
import six
import tensorflow as tf
from tensorflow.core.example import example_pb2
from tensorflow.core.example import feature_pb2
from tensorflow.python.framework import ops
from tensorflow_estimator.python.estimator.canned import baseline
from tensorflow_estimator.python.estimator.canned import metric_keys
from tensorflow_estimator.python.estimator.export import export
from tensorflow_estimator.python.estimator.inputs import numpy_io
from tensorflow_estimator.python.estimator.inputs import pandas_io

try:
  # pylint: disable=g-import-not-at-top
  import pandas as pd
  HAS_PANDAS = True
except IOError:
  # Pandas writes a temporary file during import. If it fails, don't use pandas.
  HAS_PANDAS = False
except ImportError:
  HAS_PANDAS = False

# pylint rules which are disabled by default for test files.
# pylint: disable=invalid-name,protected-access,missing-docstring

# Names of variables created by model.
BIAS_NAME = 'baseline/bias'


def assert_close(expected, actual, rtol=1e-04, name='assert_close'):
  with ops.name_scope(name, 'assert_close', (expected, actual, rtol)) as scope:
    expected = ops.convert_to_tensor(expected, name='expected')
    actual = ops.convert_to_tensor(actual, name='actual')
    rdiff = tf.math.abs(expected - actual, 'diff') / tf.math.abs(expected)
    rtol = ops.convert_to_tensor(rtol, name='rtol')
    return tf.compat.v1.debugging.assert_less(
        rdiff,
        rtol,
        data=('Condition expected =~ actual did not hold element-wise:'
              'expected = ', expected, 'actual = ', actual, 'rdiff = ', rdiff,
              'rtol = ', rtol,),
        name=scope)


def save_variables_to_ckpt(model_dir):
  init_all_op = [tf.compat.v1.initializers.global_variables()]
  with tf.compat.v1.Session() as sess:
    sess.run(init_all_op)
    tf.compat.v1.train.Saver().save(sess, os.path.join(model_dir, 'model.ckpt'))


def queue_parsed_features(feature_map):
  tensors_to_enqueue = []
  keys = []
  for key, tensor in six.iteritems(feature_map):
    keys.append(key)
    tensors_to_enqueue.append(tensor)
  queue_dtypes = [x.dtype for x in tensors_to_enqueue]
  input_queue = tf.queue.FIFOQueue(capacity=100, dtypes=queue_dtypes)
  tf.compat.v1.train.queue_runner.add_queue_runner(
      tf.compat.v1.train.queue_runner.QueueRunner(
          input_queue, [input_queue.enqueue(tensors_to_enqueue)]))
  dequeued_tensors = input_queue.dequeue()
  return {keys[i]: dequeued_tensors[i] for i in range(len(dequeued_tensors))}


def sorted_key_dict(unsorted_dict):
  return {k: unsorted_dict[k] for k in sorted(unsorted_dict)}


def sigmoid(x):
  return 1 / (1 + np.exp(-1.0 * x))


def _baseline_regressor_fn(*args, **kwargs):
  return baseline.BaselineRegressorV2(*args, **kwargs)


def _baseline_classifier_fn(*args, **kwargs):
  return baseline.BaselineClassifierV2(*args, **kwargs)


def mock_optimizer_v2(testcase, expected_loss=None):
  """Creates a mock optimizer to test the train method.

  Args:
    testcase: A TestCase instance.
    expected_loss: If given, will assert the loss value.

  Returns:
    A mock Optimizer.
  """
  expected_var_names = ['%s:0' % BIAS_NAME]

  class _Optimizer(tf.keras.optimizers.legacy.Optimizer):

    def get_updates(self, loss, params):
      trainable_vars = params
      testcase.assertItemsEqual(expected_var_names,
                                [var.name for var in trainable_vars])

      # Verify loss. We can't check the value directly, so we add an assert op.
      testcase.assertEquals(0, loss.shape.ndims)
      if expected_loss is None:
        if self.iterations is not None:
          return [self.iterations.assign_add(1).op]
        return [tf.no_op()]
      assert_loss = assert_close(
          tf.cast(expected_loss, name='expected', dtype=tf.dtypes.float32),
          loss,
          name='assert_loss')
      with tf.control_dependencies((assert_loss,)):
        if self.iterations is not None:
          return [self.iterations.assign_add(1).op]
        return [tf.no_op()]

    def get_config(self):
      config = super(_Optimizer, self).get_config()
      return config

  optimizer = _Optimizer(name='my_optimizer')

  return optimizer


# Tests for Baseline Regressor.

# TODO(b/36813849): Add tests with dynamic shape inputs using placeholders.


class BaselineRegressorEvaluationTest(tf.test.TestCase):

  def setUp(self):
    self._model_dir = tempfile.mkdtemp()

  def tearDown(self):
    if self._model_dir:
      tf.compat.v1.summary.FileWriterCache.clear()
      shutil.rmtree(self._model_dir)

  def test_evaluation_for_simple_data(self):
    with tf.Graph().as_default():
      tf.Variable([13.0], name=BIAS_NAME)
      tf.Variable(
          100, name=tf.compat.v1.GraphKeys.GLOBAL_STEP, dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    baseline_regressor = _baseline_regressor_fn(model_dir=self._model_dir)
    eval_metrics = baseline_regressor.evaluate(
        input_fn=lambda: ({
            'age': ((1,),)
        }, ((10.,),)), steps=1)

    # Logit is bias = 13, while label is 10. Loss is 3**2 = 9.
    self.assertDictEqual(
        {
            metric_keys.MetricKeys.LOSS: 9.,
            metric_keys.MetricKeys.LOSS_MEAN: 9.,
            metric_keys.MetricKeys.PREDICTION_MEAN: 13.,
            metric_keys.MetricKeys.LABEL_MEAN: 10.,
            tf.compat.v1.GraphKeys.GLOBAL_STEP: 100
        }, eval_metrics)

  def test_evaluation_batch(self):
    """Tests evaluation for batch_size==2."""
    with tf.Graph().as_default():
      tf.Variable([13.0], name=BIAS_NAME)
      tf.Variable(
          100, name=tf.compat.v1.GraphKeys.GLOBAL_STEP, dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    baseline_regressor = _baseline_regressor_fn(model_dir=self._model_dir)
    eval_metrics = baseline_regressor.evaluate(
        input_fn=lambda: ({
            'age': ((1,), (1,))
        }, ((10.,), (10.,))), steps=1)

    # Logit is bias = 13, while label is 10.
    # Loss per example is 3**2 = 9.
    # Training loss is the sum over batch size = (9 + 9) / 2 = 9
    # Average loss is the average over batch = 9
    self.assertDictEqual(
        {
            metric_keys.MetricKeys.LOSS: 9.,
            metric_keys.MetricKeys.LOSS_MEAN: 9.,
            metric_keys.MetricKeys.PREDICTION_MEAN: 13.,
            metric_keys.MetricKeys.LABEL_MEAN: 10.,
            tf.compat.v1.GraphKeys.GLOBAL_STEP: 100
        }, eval_metrics)

  def test_evaluation_weights(self):
    """Tests evaluation with weights."""
    with tf.Graph().as_default():
      tf.Variable([13.0], name=BIAS_NAME)
      tf.Variable(
          100, name=tf.compat.v1.GraphKeys.GLOBAL_STEP, dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    def _input_fn():
      features = {'age': ((1,), (1,)), 'weights': ((1.,), (2.,))}
      labels = ((10.,), (10.,))
      return features, labels

    baseline_regressor = _baseline_regressor_fn(
        weight_column='weights', model_dir=self._model_dir)
    eval_metrics = baseline_regressor.evaluate(input_fn=_input_fn, steps=1)

    # Logit is bias = 13, while label is 10.
    # Loss per example is 3**2 = 9.
    # Training loss is the weighted sum over batch size = (9 + 2*9) / 2 = 13.5
    # average loss is the weighted average = 9 + 2*9 / (1 + 2) = 9
    self.assertDictEqual(
        {
            metric_keys.MetricKeys.LOSS: 13.5,
            metric_keys.MetricKeys.LOSS_MEAN: 9.,
            metric_keys.MetricKeys.PREDICTION_MEAN: 13.,
            metric_keys.MetricKeys.LABEL_MEAN: 10.,
            tf.compat.v1.GraphKeys.GLOBAL_STEP: 100
        }, eval_metrics)

  def test_evaluation_for_multi_dimensions(self):
    label_dim = 2
    with tf.Graph().as_default():
      tf.Variable([46.0, 58.0], name=BIAS_NAME)
      tf.Variable(100, name='global_step', dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    baseline_regressor = _baseline_regressor_fn(
        label_dimension=label_dim, model_dir=self._model_dir)
    input_fn = numpy_io.numpy_input_fn(
        x={
            'age': np.array([[2., 4., 5.]]),
        },
        y=np.array([[46., 58.]]),
        batch_size=1,
        num_epochs=None,
        shuffle=False)
    eval_metrics = baseline_regressor.evaluate(input_fn=input_fn, steps=1)

    self.assertItemsEqual(
        (metric_keys.MetricKeys.LOSS, metric_keys.MetricKeys.LOSS_MEAN,
         metric_keys.MetricKeys.PREDICTION_MEAN,
         metric_keys.MetricKeys.LABEL_MEAN, tf.compat.v1.GraphKeys.GLOBAL_STEP),
        eval_metrics.keys())

    # Logit is bias which is [46, 58]
    self.assertAlmostEqual(0, eval_metrics[metric_keys.MetricKeys.LOSS])


class BaselineRegressorPredictTest(tf.test.TestCase):

  def setUp(self):
    self._model_dir = tempfile.mkdtemp()

  def tearDown(self):
    if self._model_dir:
      tf.compat.v1.summary.FileWriterCache.clear()
      shutil.rmtree(self._model_dir)

  def test_1d(self):
    """Tests predict when all variables are one-dimensional."""
    with tf.Graph().as_default():
      tf.Variable([.2], name=BIAS_NAME)
      tf.Variable(100, name='global_step', dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    baseline_regressor = _baseline_regressor_fn(model_dir=self._model_dir)

    predict_input_fn = numpy_io.numpy_input_fn(
        x={'x': np.array([[2.]])},
        y=None,
        batch_size=1,
        num_epochs=1,
        shuffle=False)
    predictions = baseline_regressor.predict(input_fn=predict_input_fn)
    predicted_scores = list([x['predictions'] for x in predictions])
    # x * weight + bias = 2. * 10. + .2 = 20.2
    self.assertAllClose([[.2]], predicted_scores)

  def testMultiDim(self):
    """Tests predict when all variables are multi-dimenstional."""
    batch_size = 2
    label_dimension = 3
    with tf.Graph().as_default():
      tf.Variable(  # shape=[label_dimension]
          [.2, .4, .6], name=BIAS_NAME)
      tf.Variable(100, name='global_step', dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    baseline_regressor = _baseline_regressor_fn(
        label_dimension=label_dimension, model_dir=self._model_dir)

    predict_input_fn = numpy_io.numpy_input_fn(
        # x shape=[batch_size, x_dim]
        x={'x': np.array([[1., 2., 3., 4.], [5., 6., 7., 8.]])},
        y=None,
        batch_size=batch_size,
        num_epochs=1,
        shuffle=False)
    predictions = baseline_regressor.predict(input_fn=predict_input_fn)
    predicted_scores = list([x['predictions'] for x in predictions])
    # score = bias, shape=[batch_size, label_dimension]
    self.assertAllClose([[0.2, 0.4, 0.6], [0.2, 0.4, 0.6]], predicted_scores)


class BaselineRegressorIntegrationTest(tf.test.TestCase):

  def setUp(self):
    self._model_dir = tempfile.mkdtemp()

  def tearDown(self):
    if self._model_dir:
      tf.compat.v1.summary.FileWriterCache.clear()
      shutil.rmtree(self._model_dir)

  def _test_complete_flow(self, train_input_fn, eval_input_fn, predict_input_fn,
                          input_dimension, label_dimension, prediction_length):
    feature_columns = [
        tf.feature_column.numeric_column('x', shape=(input_dimension,))
    ]
    est = _baseline_regressor_fn(
        label_dimension=label_dimension, model_dir=self._model_dir)

    # TRAIN
    # learn y = x
    est.train(train_input_fn, steps=200)

    # EVALUTE
    scores = est.evaluate(eval_input_fn)
    self.assertEqual(200, scores[tf.compat.v1.GraphKeys.GLOBAL_STEP])
    self.assertIn(metric_keys.MetricKeys.LOSS, six.iterkeys(scores))

    # PREDICT
    predictions = np.array(
        [x['predictions'] for x in est.predict(predict_input_fn)])
    self.assertAllEqual((prediction_length, label_dimension), predictions.shape)

    # EXPORT
    feature_spec = tf.feature_column.make_parse_example_spec(feature_columns)
    serving_input_receiver_fn = export.build_parsing_serving_input_receiver_fn(
        feature_spec)
    export_dir = est.export_saved_model(tempfile.mkdtemp(),
                                        serving_input_receiver_fn)
    self.assertTrue(tf.compat.v1.gfile.Exists(export_dir))

  def test_numpy_input_fn(self):
    """Tests complete flow with numpy_input_fn."""
    label_dimension = 2
    input_dimension = label_dimension
    batch_size = 10
    prediction_length = batch_size
    data = np.linspace(0., 2., batch_size * label_dimension, dtype=np.float32)
    data = data.reshape(batch_size, label_dimension)

    train_input_fn = numpy_io.numpy_input_fn(
        x={'x': data},
        y=data,
        batch_size=batch_size,
        num_epochs=None,
        shuffle=True)
    eval_input_fn = numpy_io.numpy_input_fn(
        x={'x': data},
        y=data,
        batch_size=batch_size,
        num_epochs=1,
        shuffle=False)
    predict_input_fn = numpy_io.numpy_input_fn(
        x={'x': data},
        y=None,
        batch_size=batch_size,
        num_epochs=1,
        shuffle=False)

    self._test_complete_flow(
        train_input_fn=train_input_fn,
        eval_input_fn=eval_input_fn,
        predict_input_fn=predict_input_fn,
        input_dimension=input_dimension,
        label_dimension=label_dimension,
        prediction_length=prediction_length)

  def test_pandas_input_fn(self):
    """Tests complete flow with pandas_input_fn."""
    if not HAS_PANDAS:
      return

    # Pandas DataFrame natually supports 1 dim data only.
    label_dimension = 1
    input_dimension = label_dimension
    batch_size = 10
    data = np.array([1., 2., 3., 4.], dtype=np.float32)
    x = pd.DataFrame({'x': data})
    y = pd.Series(data)
    prediction_length = 4

    train_input_fn = pandas_io.pandas_input_fn(
        x=x, y=y, batch_size=batch_size, num_epochs=None, shuffle=True)
    eval_input_fn = pandas_io.pandas_input_fn(
        x=x, y=y, batch_size=batch_size, shuffle=False)
    predict_input_fn = pandas_io.pandas_input_fn(
        x=x, batch_size=batch_size, shuffle=False)

    self._test_complete_flow(
        train_input_fn=train_input_fn,
        eval_input_fn=eval_input_fn,
        predict_input_fn=predict_input_fn,
        input_dimension=input_dimension,
        label_dimension=label_dimension,
        prediction_length=prediction_length)

  def test_input_fn_from_parse_example(self):
    """Tests complete flow with input_fn constructed from parse_example."""
    label_dimension = 2
    input_dimension = label_dimension
    batch_size = 10
    prediction_length = batch_size
    data = np.linspace(0., 2., batch_size * label_dimension, dtype=np.float32)
    data = data.reshape(batch_size, label_dimension)

    serialized_examples = []
    for datum in data:
      example = example_pb2.Example(
          features=feature_pb2.Features(
              feature={
                  'x':
                      feature_pb2.Feature(
                          float_list=feature_pb2.FloatList(value=datum)),
                  'y':
                      feature_pb2.Feature(
                          float_list=feature_pb2.FloatList(
                              value=datum[:label_dimension])),
              }))
      serialized_examples.append(example.SerializeToString())

    feature_spec = {
        'x': tf.io.FixedLenFeature([input_dimension], tf.dtypes.float32),
        'y': tf.io.FixedLenFeature([label_dimension], tf.dtypes.float32),
    }

    def _train_input_fn():
      feature_map = tf.compat.v1.io.parse_example(serialized_examples,
                                                  feature_spec)
      features = queue_parsed_features(feature_map)
      labels = features.pop('y')
      return features, labels

    def _eval_input_fn():
      feature_map = tf.compat.v1.io.parse_example(
          tf.compat.v1.train.limit_epochs(serialized_examples, num_epochs=1),
          feature_spec)
      features = queue_parsed_features(feature_map)
      labels = features.pop('y')
      return features, labels

    def _predict_input_fn():
      feature_map = tf.compat.v1.io.parse_example(
          tf.compat.v1.train.limit_epochs(serialized_examples, num_epochs=1),
          feature_spec)
      features = queue_parsed_features(feature_map)
      features.pop('y')
      return features, None

    self._test_complete_flow(
        train_input_fn=_train_input_fn,
        eval_input_fn=_eval_input_fn,
        predict_input_fn=_predict_input_fn,
        input_dimension=input_dimension,
        label_dimension=label_dimension,
        prediction_length=prediction_length)


class BaselineRegressorTrainingTest(tf.test.TestCase):

  def setUp(self):
    self._model_dir = tempfile.mkdtemp()

  def tearDown(self):
    if self._model_dir:
      tf.compat.v1.summary.FileWriterCache.clear()
      shutil.rmtree(self._model_dir)

  def _assert_checkpoint(self,
                         label_dimension,
                         expected_global_step,
                         expected_bias=None):
    shapes = {
        name: shape
        for (name, shape) in tf.train.list_variables(self._model_dir)
    }

    self.assertEqual([], shapes[tf.compat.v1.GraphKeys.GLOBAL_STEP])
    self.assertEqual(
        expected_global_step,
        tf.train.load_variable(self._model_dir,
                               tf.compat.v1.GraphKeys.GLOBAL_STEP))

    self.assertEqual([label_dimension], shapes[BIAS_NAME])
    if expected_bias is not None:
      self.assertEqual(expected_bias,
                       tf.train.load_variable(self._model_dir, BIAS_NAME))

  def testFromScratchWithDefaultOptimizer(self):
    # Create BaselineRegressor.
    label = 5.
    age = 17
    baseline_regressor = _baseline_regressor_fn(model_dir=self._model_dir)

    # Train for a few steps, and validate final checkpoint.
    num_steps = 10
    baseline_regressor.train(
        input_fn=lambda: ({
            'age': ((age,),)
        }, ((label,),)), steps=num_steps)
    self._assert_checkpoint(label_dimension=1, expected_global_step=num_steps)

  def testTrainWithOneDimLabel(self):
    label_dimension = 1
    batch_size = 20
    est = _baseline_regressor_fn(
        label_dimension=label_dimension, model_dir=self._model_dir)
    data_rank_1 = np.linspace(0., 2., batch_size, dtype=np.float32)
    self.assertEqual((batch_size,), data_rank_1.shape)

    train_input_fn = numpy_io.numpy_input_fn(
        x={'age': data_rank_1},
        y=data_rank_1,
        batch_size=batch_size,
        num_epochs=None,
        shuffle=True)
    est.train(train_input_fn, steps=200)
    self._assert_checkpoint(label_dimension=1, expected_global_step=200)

  def testTrainWithOneDimWeight(self):
    label_dimension = 1
    batch_size = 20
    est = _baseline_regressor_fn(
        label_dimension=label_dimension,
        weight_column='w',
        model_dir=self._model_dir)

    data_rank_1 = np.linspace(0., 2., batch_size, dtype=np.float32)
    self.assertEqual((batch_size,), data_rank_1.shape)

    train_input_fn = numpy_io.numpy_input_fn(
        x={
            'age': data_rank_1,
            'w': data_rank_1
        },
        y=data_rank_1,
        batch_size=batch_size,
        num_epochs=None,
        shuffle=True)
    est.train(train_input_fn, steps=200)
    self._assert_checkpoint(label_dimension=1, expected_global_step=200)

  def testFromScratch(self):
    # Create BaselineRegressor.
    label = 5.
    age = 17
    # loss = (logits - label)^2 = (0 - 5.)^2 = 25.
    mock_optimizer = mock_optimizer_v2(self, expected_loss=25.)
    baseline_regressor = _baseline_regressor_fn(
        model_dir=self._model_dir, optimizer=mock_optimizer)

    # Train for a few steps, and validate optimizer and final checkpoint.
    num_steps = 10
    baseline_regressor.train(
        input_fn=lambda: ({
            'age': ((age,),)
        }, ((label,),)), steps=num_steps)
    self.assertEqual(
        num_steps,
        baseline_regressor.get_variable_value(mock_optimizer.iterations.name))
    self._assert_checkpoint(
        label_dimension=1, expected_global_step=num_steps, expected_bias=[0.])

  def testFromCheckpoint(self):
    # Create initial checkpoint.
    bias = 7.0
    initial_global_step = 100
    with tf.Graph().as_default():
      tf.Variable([bias], name=BIAS_NAME)
      tf.Variable(
          initial_global_step,
          name=tf.compat.v1.GraphKeys.GLOBAL_STEP,
          dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    # logits = bias = 6.
    # loss = (logits - label)^2 = (7 - 5)^2 = 4
    mock_optimizer = mock_optimizer_v2(self, expected_loss=4.)
    baseline_regressor = _baseline_regressor_fn(
        model_dir=self._model_dir, optimizer=mock_optimizer)

    # Train for a few steps, and validate optimizer and final checkpoint.
    num_steps = 10
    baseline_regressor.train(
        input_fn=lambda: ({
            'age': ((17,),)
        }, ((5.,),)), steps=num_steps)
    self.assertEqual(
        initial_global_step + num_steps,
        baseline_regressor.get_variable_value(mock_optimizer.iterations.name))
    self._assert_checkpoint(
        label_dimension=1,
        expected_global_step=initial_global_step + num_steps,
        expected_bias=[bias])

  def testFromCheckpointMultiBatch(self):
    # Create initial checkpoint.
    bias = 5.0
    initial_global_step = 100
    with tf.Graph().as_default():
      tf.Variable([bias], name=BIAS_NAME)
      tf.Variable(
          initial_global_step,
          name=tf.compat.v1.GraphKeys.GLOBAL_STEP,
          dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    # logits = bias
    # logits[0] = 5.
    # logits[1] = 5.
    # loss = (sum(logits - label)^2 = (5 - 5)^2 + (5 - 3)^2) / 2 (batch size)
    # loss = 2
    mock_optimizer = mock_optimizer_v2(self, expected_loss=2.)
    baseline_regressor = _baseline_regressor_fn(
        model_dir=self._model_dir, optimizer=mock_optimizer)

    # Train for a few steps, and validate optimizer and final checkpoint.
    num_steps = 10
    baseline_regressor.train(
        input_fn=lambda: ({
            'age': ((17,), (15,))
        }, ((5.,), (3.,))),
        steps=num_steps)
    self.assertEqual(
        initial_global_step + num_steps,
        baseline_regressor.get_variable_value(mock_optimizer.iterations.name))
    self._assert_checkpoint(
        label_dimension=1,
        expected_global_step=initial_global_step + num_steps,
        expected_bias=bias)


# Tests for Baseline Classifier.


class BaselineClassifierTrainingTest(tf.test.TestCase):

  def setUp(self):
    self._model_dir = tempfile.mkdtemp()

  def tearDown(self):
    if self._model_dir:
      shutil.rmtree(self._model_dir)

  def _assert_checkpoint(self,
                         n_classes,
                         expected_global_step,
                         expected_bias=None):
    logits_dimension = n_classes if n_classes > 2 else 1

    shapes = {
        name: shape
        for (name, shape) in tf.train.list_variables(self._model_dir)
    }

    self.assertEqual([], shapes[tf.compat.v1.GraphKeys.GLOBAL_STEP])
    self.assertEqual(
        expected_global_step,
        tf.train.load_variable(self._model_dir,
                               tf.compat.v1.GraphKeys.GLOBAL_STEP))

    self.assertEqual([logits_dimension], shapes[BIAS_NAME])
    if expected_bias is not None:
      self.assertAllEqual(expected_bias,
                          tf.train.load_variable(self._model_dir, BIAS_NAME))

  def _testFromScratchWithDefaultOptimizer(self, n_classes):
    label = 0
    age = 17
    est = baseline.BaselineClassifierV2(
        n_classes=n_classes, model_dir=self._model_dir)

    # Train for a few steps, and validate final checkpoint.
    num_steps = 10
    est.train(
        input_fn=lambda: ({
            'age': ((age,),)
        }, ((label,),)), steps=num_steps)
    self._assert_checkpoint(n_classes, num_steps)

  def testBinaryClassesFromScratchWithDefaultOptimizer(self):
    self._testFromScratchWithDefaultOptimizer(n_classes=2)

  def testMultiClassesFromScratchWithDefaultOptimizer(self):
    self._testFromScratchWithDefaultOptimizer(n_classes=4)

  def _testTrainWithTwoDimsLabel(self, n_classes):
    batch_size = 20

    est = baseline.BaselineClassifierV2(
        n_classes=n_classes, model_dir=self._model_dir)
    data_rank_1 = np.array([0, 1])
    data_rank_2 = np.array([[0], [1]])
    self.assertEqual((2,), data_rank_1.shape)
    self.assertEqual((2, 1), data_rank_2.shape)

    train_input_fn = numpy_io.numpy_input_fn(
        x={'age': data_rank_1},
        y=data_rank_2,
        batch_size=batch_size,
        num_epochs=None,
        shuffle=True)
    est.train(train_input_fn, steps=200)
    self._assert_checkpoint(n_classes, 200)

  def testBinaryClassesTrainWithTwoDimsLabel(self):
    self._testTrainWithTwoDimsLabel(n_classes=2)

  def testMultiClassesTrainWithTwoDimsLabel(self):
    self._testTrainWithTwoDimsLabel(n_classes=4)

  def _testTrainWithOneDimLabel(self, n_classes):
    batch_size = 20

    est = baseline.BaselineClassifierV2(
        n_classes=n_classes, model_dir=self._model_dir)
    data_rank_1 = np.array([0, 1])
    self.assertEqual((2,), data_rank_1.shape)

    train_input_fn = numpy_io.numpy_input_fn(
        x={'age': data_rank_1},
        y=data_rank_1,
        batch_size=batch_size,
        num_epochs=None,
        shuffle=True)
    est.train(train_input_fn, steps=200)
    self._assert_checkpoint(n_classes, 200)

  def testBinaryClassesTrainWithOneDimLabel(self):
    self._testTrainWithOneDimLabel(n_classes=2)

  def testMultiClassesTrainWithOneDimLabel(self):
    self._testTrainWithOneDimLabel(n_classes=4)

  def _testTrainWithTwoDimsWeight(self, n_classes):
    batch_size = 20

    est = baseline.BaselineClassifierV2(
        weight_column='w', n_classes=n_classes, model_dir=self._model_dir)
    data_rank_1 = np.array([0, 1])
    data_rank_2 = np.array([[0], [1]])
    self.assertEqual((2,), data_rank_1.shape)
    self.assertEqual((2, 1), data_rank_2.shape)

    train_input_fn = numpy_io.numpy_input_fn(
        x={
            'age': data_rank_1,
            'w': data_rank_2
        },
        y=data_rank_1,
        batch_size=batch_size,
        num_epochs=None,
        shuffle=True)
    est.train(train_input_fn, steps=200)
    self._assert_checkpoint(n_classes, 200)

  def testBinaryClassesTrainWithTwoDimsWeight(self):
    self._testTrainWithTwoDimsWeight(n_classes=2)

  def testMultiClassesTrainWithTwoDimsWeight(self):
    self._testTrainWithTwoDimsWeight(n_classes=4)

  def _testTrainWithOneDimWeight(self, n_classes):
    batch_size = 20

    est = baseline.BaselineClassifierV2(
        weight_column='w', n_classes=n_classes, model_dir=self._model_dir)
    data_rank_1 = np.array([0, 1])
    self.assertEqual((2,), data_rank_1.shape)

    train_input_fn = numpy_io.numpy_input_fn(
        x={
            'age': data_rank_1,
            'w': data_rank_1
        },
        y=data_rank_1,
        batch_size=batch_size,
        num_epochs=None,
        shuffle=True)
    est.train(train_input_fn, steps=200)
    self._assert_checkpoint(n_classes, 200)

  def testBinaryClassesTrainWithOneDimWeight(self):
    self._testTrainWithOneDimWeight(n_classes=2)

  def testMultiClassesTrainWithOneDimWeight(self):
    self._testTrainWithOneDimWeight(n_classes=4)

  def _testFromScratch(self, n_classes):
    label = 1
    age = 17
    # For binary classifier:
    #   loss = sigmoid_cross_entropy(logits, label) where logits=0 (weights are
    #   all zero initially) and label = 1 so,
    #      loss = 1 * -log ( sigmoid(logits) ) = 0.69315
    # For multi class classifier:
    #   loss = cross_entropy(logits, label) where logits are all 0s (weights are
    #   all zero initially) and label = 1 so,
    #      loss = 1 * -log ( 1.0 / n_classes )
    # For this particular test case, as logits are same, the formula
    # 1 * -log ( 1.0 / n_classes ) covers both binary and multi class cases.
    mock_optimizer = mock_optimizer_v2(
        self, expected_loss=-1 * math.log(1.0 / n_classes))

    est = baseline.BaselineClassifierV2(
        n_classes=n_classes,
        optimizer=mock_optimizer,
        model_dir=self._model_dir)

    # Train for a few steps, and validate optimizer and final checkpoint.
    num_steps = 10
    est.train(
        input_fn=lambda: ({
            'age': ((age,),)
        }, ((label,),)), steps=num_steps)
    self.assertEqual(num_steps,
                     est.get_variable_value(mock_optimizer.iterations.name))
    self._assert_checkpoint(
        n_classes,
        expected_global_step=num_steps,
        expected_bias=[0.] if n_classes == 2 else [.0] * n_classes)

  def testBinaryClassesFromScratch(self):
    self._testFromScratch(n_classes=2)

  def testMultiClassesFromScratch(self):
    self._testFromScratch(n_classes=4)

  def _testFromCheckpoint(self, n_classes):
    # Create initial checkpoint.
    label = 1
    age = 17
    bias = [-1.0] if n_classes == 2 else [-1.0] * n_classes
    initial_global_step = 100
    with tf.Graph().as_default():
      tf.Variable(bias, name=BIAS_NAME)
      tf.Variable(
          initial_global_step,
          name=tf.compat.v1.GraphKeys.GLOBAL_STEP,
          dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    # For binary classifier:
    #   logits = bias = -1.
    #   loss = sigmoid_cross_entropy(logits, label)
    #   so, loss = 1 * -log ( sigmoid(-1) ) = 1.3133
    # For multi class classifier:
    #   loss = cross_entropy(logits, label)
    #   where logits = bias and label = 1
    #   so, loss = 1 * -log ( softmax(logits)[1] )
    if n_classes == 2:
      expected_loss = 1.3133
    else:
      logits = bias
      logits_exp = np.exp(logits)
      softmax = logits_exp / logits_exp.sum()
      expected_loss = -1 * math.log(softmax[label])

    mock_optimizer = mock_optimizer_v2(self, expected_loss=expected_loss)

    est = baseline.BaselineClassifierV2(
        n_classes=n_classes,
        optimizer=mock_optimizer,
        model_dir=self._model_dir)

    # Train for a few steps, and validate optimizer and final checkpoint.
    num_steps = 10
    est.train(
        input_fn=lambda: ({
            'age': ((age,),)
        }, ((label,),)), steps=num_steps)
    self.assertEqual(initial_global_step + num_steps,
                     est.get_variable_value(mock_optimizer.iterations.name))
    self._assert_checkpoint(
        n_classes,
        expected_global_step=initial_global_step + num_steps,
        expected_bias=bias)

  def testBinaryClassesFromCheckpoint(self):
    self._testFromCheckpoint(n_classes=2)

  def testMultiClassesFromCheckpoint(self):
    self._testFromCheckpoint(n_classes=4)

  def _testFromCheckpointFloatLabels(self, n_classes):
    """Tests float labels for binary classification."""
    # Create initial checkpoint.
    if n_classes > 2:
      return
    label = 0.8
    age = 17
    bias = [-1.0]
    initial_global_step = 100
    with tf.Graph().as_default():
      tf.Variable(bias, name=BIAS_NAME)
      tf.Variable(
          initial_global_step,
          name=tf.compat.v1.GraphKeys.GLOBAL_STEP,
          dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    # logits = bias = -1.
    # loss = sigmoid_cross_entropy(logits, label)
    # => loss = -0.8 * log(sigmoid(-1)) -0.2 * log(sigmoid(+1)) = 1.1132617
    mock_optimizer = mock_optimizer_v2(self, expected_loss=1.1132617)

    est = baseline.BaselineClassifierV2(
        n_classes=n_classes,
        optimizer=mock_optimizer,
        model_dir=self._model_dir)

    # Train for a few steps, and validate optimizer and final checkpoint.
    num_steps = 10
    est.train(
        input_fn=lambda: ({
            'age': ((age,),)
        }, ((label,),)), steps=num_steps)
    self.assertEqual(initial_global_step + num_steps,
                     est.get_variable_value(mock_optimizer.iterations.name))

  def testBinaryClassesFromCheckpointFloatLabels(self):
    self._testFromCheckpointFloatLabels(n_classes=2)

  def testMultiClassesFromCheckpointFloatLabels(self):
    self._testFromCheckpointFloatLabels(n_classes=4)

  def _testFromCheckpointMultiBatch(self, n_classes):
    # Create initial checkpoint.
    label = [1, 0]
    age = [17, 18.5]
    batch_size = 2
    # For binary case, the expected weight has shape (1,1). For multi class
    # case, the shape is (1, n_classes). In order to test the weights, set
    # weights as 2.0 * range(n_classes).
    bias = [-1.0] if n_classes == 2 else [-1.0] * n_classes
    initial_global_step = 100
    with tf.Graph().as_default():
      tf.Variable(bias, name=BIAS_NAME)
      tf.Variable(
          initial_global_step,
          name=tf.compat.v1.GraphKeys.GLOBAL_STEP,
          dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    # For binary classifier:
    #   logits = bias
    #   logits[0] = -1.
    #   logits[1] = -1.
    #   loss = sigmoid_cross_entropy(logits, label)
    #   so, loss[0] = 1 * -log ( sigmoid(-1) ) = 1.3133
    #       loss[1] = (1 - 0) * -log ( 1- sigmoid(-1) ) = 0.3132
    # For multi class classifier:
    #   loss = cross_entropy(logits, label)
    #   where logits = bias and label = [1, 0]
    #   so, loss = 1 * -log ( softmax(logits)[label] )
    if n_classes == 2:
      expected_loss = (1.3133 + 0.3132) / 2
    else:
      # Expand logits since batch_size=2
      logits = bias * np.ones(shape=(2, 1))
      logits_exp = np.exp(logits)
      softmax_row_0 = logits_exp[0] / logits_exp[0].sum()
      softmax_row_1 = logits_exp[1] / logits_exp[1].sum()
      expected_loss_0 = -1 * math.log(softmax_row_0[label[0]])
      expected_loss_1 = -1 * math.log(softmax_row_1[label[1]])
      expected_loss = (expected_loss_0 + expected_loss_1) / 2

    mock_optimizer = mock_optimizer_v2(self, expected_loss=expected_loss)

    est = baseline.BaselineClassifierV2(
        n_classes=n_classes,
        optimizer=mock_optimizer,
        model_dir=self._model_dir)

    # Train for a few steps, and validate optimizer and final checkpoint.
    num_steps = 10
    est.train(input_fn=lambda: ({'age': (age)}, (label)), steps=num_steps)
    self.assertEqual(initial_global_step + num_steps,
                     est.get_variable_value(mock_optimizer.iterations.name))
    self._assert_checkpoint(
        n_classes,
        expected_global_step=initial_global_step + num_steps,
        expected_bias=bias)

  def testBinaryClassesFromCheckpointMultiBatch(self):
    self._testFromCheckpointMultiBatch(n_classes=2)

  def testMultiClassesFromCheckpointMultiBatch(self):
    self._testFromCheckpointMultiBatch(n_classes=4)


class BaselineClassifierEvaluationTest(tf.test.TestCase):

  def setUp(self):
    self._model_dir = tempfile.mkdtemp()

  def tearDown(self):
    if self._model_dir:
      shutil.rmtree(self._model_dir)

  def _test_evaluation_for_simple_data(self, n_classes):
    label = 1
    age = 1.

    bias = [-1.0] if n_classes == 2 else [-1.0] * n_classes

    with tf.Graph().as_default():
      tf.Variable(bias, name=BIAS_NAME)
      tf.Variable(
          100, name=tf.compat.v1.GraphKeys.GLOBAL_STEP, dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    est = _baseline_classifier_fn(
        n_classes=n_classes, model_dir=self._model_dir)
    eval_metrics = est.evaluate(
        input_fn=lambda: ({
            'age': ((age,),)
        }, ((label,),)), steps=1)

    if n_classes == 2:
      # Binary classes: loss = -log(sigmoid(-1)) / batch size = 1.3133
      # Prediction = sigmoid(-1) = 0.2689
      expected_metrics = {
          metric_keys.MetricKeys.LOSS: 1.3133,
          tf.compat.v1.GraphKeys.GLOBAL_STEP: 100,
          metric_keys.MetricKeys.LOSS_MEAN: 1.3133,
          metric_keys.MetricKeys.ACCURACY: 0.,
          metric_keys.MetricKeys.PRECISION: 0.,
          metric_keys.MetricKeys.RECALL: 0.,
          metric_keys.MetricKeys.PREDICTION_MEAN: 0.2689,
          metric_keys.MetricKeys.LABEL_MEAN: 1.,
          metric_keys.MetricKeys.ACCURACY_BASELINE: 1,
          metric_keys.MetricKeys.AUC: 0.,
          metric_keys.MetricKeys.AUC_PR: 1.,
      }
    else:
      # Multi classes: loss = 1 * -log ( softmax(logits)[label] )
      logits = bias
      logits_exp = np.exp(logits)
      softmax = logits_exp / logits_exp.sum()
      expected_loss = -1 * math.log(softmax[label])

      expected_metrics = {
          metric_keys.MetricKeys.LOSS: expected_loss,
          tf.compat.v1.GraphKeys.GLOBAL_STEP: 100,
          metric_keys.MetricKeys.LOSS_MEAN: expected_loss,
          metric_keys.MetricKeys.ACCURACY: 0.,
      }

    self.assertAllClose(
        sorted_key_dict(expected_metrics),
        sorted_key_dict(eval_metrics),
        rtol=1e-3)

  def test_binary_classes_evaluation_for_simple_data(self):
    self._test_evaluation_for_simple_data(n_classes=2)

  def test_multi_classes_evaluation_for_simple_data(self):
    self._test_evaluation_for_simple_data(n_classes=4)

  def _test_evaluation_batch(self, n_classes):
    """Tests evaluation for batch_size==2."""
    label = [1, 0]
    age = [17., 18.]
    bias = [-1.0] if n_classes == 2 else [-1.0] * n_classes
    initial_global_step = 100
    with tf.Graph().as_default():
      tf.Variable(bias, name=BIAS_NAME)
      tf.Variable(
          initial_global_step,
          name=tf.compat.v1.GraphKeys.GLOBAL_STEP,
          dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    est = _baseline_classifier_fn(
        n_classes=n_classes, model_dir=self._model_dir)
    eval_metrics = est.evaluate(
        input_fn=lambda: ({
            'age': (age)
        }, (label)), steps=1)

    if n_classes == 2:
      # Logits are (-1., -1.) labels are (1, 0).
      # Loss is
      #   loss for row 1: 1 * -log(sigmoid(-1)) = 1.3133
      #   loss for row 2: (1 - 0) * -log(1 - sigmoid(-1)) = 0.3132
      # Prediction = sigmoid(-1) = 0.2689
      expected_loss = (1.3133 + 0.3132) / 2  # batch size

      expected_metrics = {
          metric_keys.MetricKeys.LOSS: expected_loss,
          tf.compat.v1.GraphKeys.GLOBAL_STEP: 100,
          metric_keys.MetricKeys.LOSS_MEAN: expected_loss,
          metric_keys.MetricKeys.ACCURACY: 0.5,
          metric_keys.MetricKeys.PRECISION: 0.,
          metric_keys.MetricKeys.RECALL: 0.,
          metric_keys.MetricKeys.PREDICTION_MEAN: 0.2689,
          metric_keys.MetricKeys.LABEL_MEAN: 0.5,
          metric_keys.MetricKeys.ACCURACY_BASELINE: 0.5,
          metric_keys.MetricKeys.AUC: 0.5,
          metric_keys.MetricKeys.AUC_PR: 0.5,
      }
    else:
      # Expand logits since batch_size=2
      logits = bias * np.ones(shape=(2, 1))
      logits_exp = np.exp(logits)
      softmax_row_0 = logits_exp[0] / logits_exp[0].sum()
      softmax_row_1 = logits_exp[1] / logits_exp[1].sum()
      expected_loss_0 = -1 * math.log(softmax_row_0[label[0]])
      expected_loss_1 = -1 * math.log(softmax_row_1[label[1]])
      expected_loss = (expected_loss_0 + expected_loss_1) / 2  # batch size

      expected_metrics = {
          metric_keys.MetricKeys.LOSS: expected_loss,
          tf.compat.v1.GraphKeys.GLOBAL_STEP: 100,
          metric_keys.MetricKeys.LOSS_MEAN: expected_loss,
          metric_keys.MetricKeys.ACCURACY: 0.5,
      }

    self.assertAllClose(
        sorted_key_dict(expected_metrics),
        sorted_key_dict(eval_metrics),
        rtol=1e-3)

  def test_binary_classes_evaluation_batch(self):
    self._test_evaluation_batch(n_classes=2)

  def test_multi_classes_evaluation_batch(self):
    self._test_evaluation_batch(n_classes=4)

  def _test_evaluation_weights(self, n_classes):
    """Tests evaluation with weights."""

    label = [1, 0]
    age = [17., 18.]
    weights = [1., 2.]
    # For binary case, the expected weight has shape (1,1). For multi class
    # case, the shape is (1, n_classes). In order to test the weights, set
    # weights as 2.0 * range(n_classes).
    bias = [-1.0] if n_classes == 2 else [-1.0] * n_classes
    initial_global_step = 100
    with tf.Graph().as_default():
      tf.Variable(bias, name=BIAS_NAME)
      tf.Variable(
          initial_global_step,
          name=tf.compat.v1.GraphKeys.GLOBAL_STEP,
          dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    est = _baseline_classifier_fn(
        n_classes=n_classes, weight_column='w', model_dir=self._model_dir)
    eval_metrics = est.evaluate(
        input_fn=lambda: ({
            'age': (age),
            'w': (weights)
        }, (label)), steps=1)

    if n_classes == 2:
      # Logits are (-1., -1.) labels are (1, 0).
      # Loss is
      #   loss for row 1: 1 * -log(sigmoid(-1)) = 1.3133
      #   loss for row 2: (1 - 0) * -log(1 - sigmoid(-1)) = 0.3132
      #   weights = [1., 2.]
      expected_loss = (1.3133 * 1. + 0.3132 * 2.) / 2  # batch size
      loss_mean = (1.3133 * 1. + 0.3132 * 2.) / (1.0 + 2.0)
      label_mean = np.average(label, weights=weights)
      logits = [-1, -1]
      logistics = sigmoid(np.array(logits))
      predictions_mean = np.average(logistics, weights=weights)

      expected_metrics = {
          metric_keys.MetricKeys.LOSS: expected_loss,
          tf.compat.v1.GraphKeys.GLOBAL_STEP: 100,
          metric_keys.MetricKeys.LOSS_MEAN: loss_mean,
          metric_keys.MetricKeys.ACCURACY: 2. / (1. + 2.),
          metric_keys.MetricKeys.PRECISION: 0.,
          metric_keys.MetricKeys.RECALL: 0.,
          metric_keys.MetricKeys.PREDICTION_MEAN: predictions_mean,
          metric_keys.MetricKeys.LABEL_MEAN: label_mean,
          metric_keys.MetricKeys.ACCURACY_BASELINE:
              (max(label_mean, 1 - label_mean)),
          metric_keys.MetricKeys.AUC: 0.5,
          metric_keys.MetricKeys.AUC_PR: 0.33333,
      }
    else:
      # Multi classes: unweighted_loss = 1 * -log ( soft_max(logits)[label] )
      # Expand logits since batch_size=2
      logits = bias * np.ones(shape=(2, 1))
      logits_exp = np.exp(logits)
      softmax_row_0 = logits_exp[0] / logits_exp[0].sum()
      softmax_row_1 = logits_exp[1] / logits_exp[1].sum()
      expected_loss_0 = -1 * math.log(softmax_row_0[label[0]])
      expected_loss_1 = -1 * math.log(softmax_row_1[label[1]])
      loss_mean = np.average([expected_loss_0, expected_loss_1],
                             weights=weights)
      expected_loss = (loss_mean * np.sum(weights)) / 2  # batch size

      expected_metrics = {
          metric_keys.MetricKeys.LOSS: expected_loss,
          tf.compat.v1.GraphKeys.GLOBAL_STEP: 100,
          metric_keys.MetricKeys.LOSS_MEAN: loss_mean,
          metric_keys.MetricKeys.ACCURACY: 2. / (1. + 2.),
      }

    self.assertAllClose(
        sorted_key_dict(expected_metrics),
        sorted_key_dict(eval_metrics),
        rtol=1e-3)

  def test_binary_classes_evaluation_weights(self):
    self._test_evaluation_weights(n_classes=2)

  def test_multi_classes_evaluation_weights(self):
    self._test_evaluation_weights(n_classes=4)


class BaselineClassifierPredictTest(tf.test.TestCase):

  def setUp(self):
    self._model_dir = tempfile.mkdtemp()

  def tearDown(self):
    if self._model_dir:
      shutil.rmtree(self._model_dir)

  def _testPredictions(self, n_classes, label_vocabulary, label_output_fn):
    """Tests predict when all variables are one-dimensional."""
    age = 1.

    bias = [10.0] if n_classes == 2 else [10.0] * n_classes

    with tf.Graph().as_default():
      tf.Variable(bias, name=BIAS_NAME)
      tf.Variable(100, name='global_step', dtype=tf.dtypes.int64)
      save_variables_to_ckpt(self._model_dir)

    est = _baseline_classifier_fn(
        label_vocabulary=label_vocabulary,
        n_classes=n_classes,
        model_dir=self._model_dir)

    predict_input_fn = numpy_io.numpy_input_fn(
        x={'age': np.array([[age]])},
        y=None,
        batch_size=1,
        num_epochs=1,
        shuffle=False)
    predictions = list(est.predict(input_fn=predict_input_fn))

    if n_classes == 2:
      scalar_logits = bias[0]
      two_classes_logits = [0, scalar_logits]
      two_classes_logits_exp = np.exp(two_classes_logits)
      softmax = two_classes_logits_exp / two_classes_logits_exp.sum()

      expected_predictions = {
          'class_ids': [1],
          'all_class_ids': [0, 1],
          'classes': [label_output_fn(1)],
          'all_classes': [label_output_fn(0),
                          label_output_fn(1)],
          'logistic': [sigmoid(np.array(scalar_logits))],
          'logits': [scalar_logits],
          'probabilities': softmax,
      }
    else:
      onedim_logits = np.array(bias)
      class_ids = onedim_logits.argmax()
      all_class_ids = list(range(len(onedim_logits)))
      logits_exp = np.exp(onedim_logits)
      softmax = logits_exp / logits_exp.sum()
      expected_predictions = {
          'class_ids': [class_ids],
          'all_class_ids': all_class_ids,
          'classes': [label_output_fn(class_ids)],
          'all_classes': [label_output_fn(i) for i in all_class_ids],
          'logits': onedim_logits,
          'probabilities': softmax,
      }

    self.assertEqual(1, len(predictions))
    # assertAllClose cannot handle byte type.
    self.assertEqual(expected_predictions['classes'], predictions[0]['classes'])
    expected_predictions.pop('classes')
    predictions[0].pop('classes')
    self.assertAllEqual(expected_predictions['all_classes'],
                        predictions[0]['all_classes'])
    expected_predictions.pop('all_classes')
    predictions[0].pop('all_classes')
    self.assertAllClose(
        sorted_key_dict(expected_predictions), sorted_key_dict(predictions[0]))

  def testBinaryClassesWithoutLabelVocabulary(self):
    n_classes = 2
    self._testPredictions(
        n_classes,
        label_vocabulary=None,
        label_output_fn=lambda x: ('%s' % x).encode())

  def testBinaryClassesWithLabelVocabulary(self):
    n_classes = 2
    self._testPredictions(
        n_classes,
        label_vocabulary=['class_vocab_{}'.format(i) for i in range(n_classes)],
        label_output_fn=lambda x: ('class_vocab_%s' % x).encode())

  def testMultiClassesWithoutLabelVocabulary(self):
    n_classes = 4
    self._testPredictions(
        n_classes,
        label_vocabulary=None,
        label_output_fn=lambda x: ('%s' % x).encode())

  def testMultiClassesWithLabelVocabulary(self):
    n_classes = 4
    self._testPredictions(
        n_classes,
        label_vocabulary=['class_vocab_{}'.format(i) for i in range(n_classes)],
        label_output_fn=lambda x: ('class_vocab_%s' % x).encode())


class BaselineClassifierIntegrationTest(tf.test.TestCase):

  def setUp(self):
    self._model_dir = tempfile.mkdtemp()

  def tearDown(self):
    if self._model_dir:
      shutil.rmtree(self._model_dir)

  def _test_complete_flow(self, n_classes, train_input_fn, eval_input_fn,
                          predict_input_fn, input_dimension, prediction_length):
    feature_columns = [
        tf.feature_column.numeric_column('x', shape=(input_dimension,))
    ]
    est = _baseline_classifier_fn(
        n_classes=n_classes, model_dir=self._model_dir)

    # TRAIN
    # learn y = x
    est.train(train_input_fn, steps=200)

    # EVALUTE
    scores = est.evaluate(eval_input_fn)
    self.assertEqual(200, scores[tf.compat.v1.GraphKeys.GLOBAL_STEP])
    self.assertIn(metric_keys.MetricKeys.LOSS, six.iterkeys(scores))

    # PREDICT
    predictions = np.array(
        [x['classes'] for x in est.predict(predict_input_fn)])
    self.assertAllEqual((prediction_length, 1), predictions.shape)

    # EXPORT
    feature_spec = tf.feature_column.make_parse_example_spec(feature_columns)
    serving_input_receiver_fn = export.build_parsing_serving_input_receiver_fn(
        feature_spec)
    export_dir = est.export_saved_model(tempfile.mkdtemp(),
                                        serving_input_receiver_fn)
    self.assertTrue(tf.compat.v1.gfile.Exists(export_dir))

  def _test_numpy_input_fn(self, n_classes):
    """Tests complete flow with numpy_input_fn."""
    input_dimension = 4
    batch_size = 10
    prediction_length = batch_size
    data = np.linspace(0., 2., batch_size * input_dimension, dtype=np.float32)
    data = data.reshape(batch_size, input_dimension)
    target = np.array([1] * batch_size)

    train_input_fn = numpy_io.numpy_input_fn(
        x={'x': data},
        y=target,
        batch_size=batch_size,
        num_epochs=None,
        shuffle=True)
    eval_input_fn = numpy_io.numpy_input_fn(
        x={'x': data},
        y=target,
        batch_size=batch_size,
        num_epochs=1,
        shuffle=False)
    predict_input_fn = numpy_io.numpy_input_fn(
        x={'x': data},
        y=None,
        batch_size=batch_size,
        num_epochs=1,
        shuffle=False)

    self._test_complete_flow(
        n_classes=n_classes,
        train_input_fn=train_input_fn,
        eval_input_fn=eval_input_fn,
        predict_input_fn=predict_input_fn,
        input_dimension=input_dimension,
        prediction_length=prediction_length)

  def test_binary_classes_numpy_input_fn(self):
    self._test_numpy_input_fn(n_classes=2)

  def test_multi_classes_numpy_input_fn(self):
    self._test_numpy_input_fn(n_classes=4)

  def _test_pandas_input_fn(self, n_classes):
    """Tests complete flow with pandas_input_fn."""
    if not HAS_PANDAS:
      return

    # Pandas DataFrame natually supports 1 dim data only.
    input_dimension = 1
    batch_size = 10
    data = np.array([1., 2., 3., 4.], dtype=np.float32)
    target = np.array([1, 0, 1, 0], dtype=np.int32)
    x = pd.DataFrame({'x': data})
    y = pd.Series(target)
    prediction_length = 4

    train_input_fn = pandas_io.pandas_input_fn(
        x=x, y=y, batch_size=batch_size, num_epochs=None, shuffle=True)
    eval_input_fn = pandas_io.pandas_input_fn(
        x=x, y=y, batch_size=batch_size, shuffle=False)
    predict_input_fn = pandas_io.pandas_input_fn(
        x=x, batch_size=batch_size, shuffle=False)

    self._test_complete_flow(
        n_classes=n_classes,
        train_input_fn=train_input_fn,
        eval_input_fn=eval_input_fn,
        predict_input_fn=predict_input_fn,
        input_dimension=input_dimension,
        prediction_length=prediction_length)

  def test_binary_classes_pandas_input_fn(self):
    self._test_pandas_input_fn(n_classes=2)

  def test_multi_classes_pandas_input_fn(self):
    self._test_pandas_input_fn(n_classes=4)

  def _test_input_fn_from_parse_example(self, n_classes):
    """Tests complete flow with input_fn constructed from parse_example."""
    input_dimension = 2
    batch_size = 10
    prediction_length = batch_size
    data = np.linspace(0., 2., batch_size * input_dimension, dtype=np.float32)
    data = data.reshape(batch_size, input_dimension)
    target = np.array([1] * batch_size, dtype=np.int64)

    serialized_examples = []
    for x, y in zip(data, target):
      example = example_pb2.Example(
          features=feature_pb2.Features(
              feature={
                  'x':
                      feature_pb2.Feature(
                          float_list=feature_pb2.FloatList(value=x)),
                  'y':
                      feature_pb2.Feature(
                          int64_list=feature_pb2.Int64List(value=[y])),
              }))
      serialized_examples.append(example.SerializeToString())

    feature_spec = {
        'x': tf.io.FixedLenFeature([input_dimension], tf.dtypes.float32),
        'y': tf.io.FixedLenFeature([1], tf.dtypes.int64),
    }

    def _train_input_fn():
      feature_map = tf.compat.v1.io.parse_example(serialized_examples,
                                                  feature_spec)
      features = queue_parsed_features(feature_map)
      labels = features.pop('y')
      return features, labels

    def _eval_input_fn():
      feature_map = tf.compat.v1.io.parse_example(
          tf.compat.v1.train.limit_epochs(serialized_examples, num_epochs=1),
          feature_spec)
      features = queue_parsed_features(feature_map)
      labels = features.pop('y')
      return features, labels

    def _predict_input_fn():
      feature_map = tf.compat.v1.io.parse_example(
          tf.compat.v1.train.limit_epochs(serialized_examples, num_epochs=1),
          feature_spec)
      features = queue_parsed_features(feature_map)
      features.pop('y')
      return features, None

    self._test_complete_flow(
        n_classes=n_classes,
        train_input_fn=_train_input_fn,
        eval_input_fn=_eval_input_fn,
        predict_input_fn=_predict_input_fn,
        input_dimension=input_dimension,
        prediction_length=prediction_length)

  def test_binary_classes_input_fn_from_parse_example(self):
    self._test_input_fn_from_parse_example(n_classes=2)

  def test_multi_classes_input_fn_from_parse_example(self):
    self._test_input_fn_from_parse_example(n_classes=4)


# Tests for Baseline logit_fn.


class BaselineLogitFnTest(tf.test.TestCase):

  def test_basic_logit_correctness(self):
    """baseline_logit_fn simply returns the bias variable."""
    with tf.Graph().as_default():
      bias_var, logits = baseline._baseline_model_fn_builder_v2(
          features={'age': [[23.], [31.]]}, num_outputs=2)
      with tf.compat.v1.Session() as sess:
        sess.run([tf.compat.v1.initializers.global_variables()])
        self.assertAllClose([[0., 0.], [0., 0.]], logits.eval())
        sess.run(bias_var[0].assign([10., 5.]))
        self.assertAllClose([[10., 5.], [10., 5.]], logits.eval())


if __name__ == '__main__':
  tf.test.main()
