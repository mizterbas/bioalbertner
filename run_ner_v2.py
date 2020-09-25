# coding=utf-8
# Copyright 2018 The Google AI Team Authors.
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
"""BERT finetuning on classification tasks."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time
#from albert import classifier_utils
from albert import fine_tuning_utils
from albert import modeling
import tensorflow.compat.v1 as tf
from tensorflow.contrib import cluster_resolver as contrib_cluster_resolver
from tensorflow.contrib import tpu as contrib_tpu


import collections
import csv

from albert import optimization
from albert import tokenization

from tensorflow.contrib import data as contrib_data
from tensorflow.contrib import metrics as contrib_metrics

# Added for BioALbert
from albert import tf_metrics

flags = tf.flags

FLAGS = flags.FLAGS

## Required parameters
flags.DEFINE_string(
    "data_dir", None,
    "The input data dir. Should contain the .tsv files (or other data files) "
    "for the task.")

flags.DEFINE_string(
    "albert_config_file", None,
    "The config json file corresponding to the pre-trained ALBERT model. "
    "This specifies the model architecture.")

flags.DEFINE_string("task_name", None, "The name of the task to train.")

flags.DEFINE_string(
    "vocab_file", None,
    "The vocabulary file that the ALBERT model was trained on.")

flags.DEFINE_string("spm_model_file", None,
                    "The model file for sentence piece tokenization.")

flags.DEFINE_string(
    "output_dir", None,
    "The output directory where the model checkpoints will be written.")

flags.DEFINE_string("cached_dir", None,
                    "Path to cached training and dev tfrecord file. "
                    "The file will be generated if not exist.")

## Other parameters

flags.DEFINE_string(
    "init_checkpoint", None,
    "Initial checkpoint (usually from a pre-trained BERT model).")

flags.DEFINE_string(
    "albert_hub_module_handle", None,
    "If set, the ALBERT hub module to use.")

flags.DEFINE_bool(
    "do_lower_case", True,
    "Whether to lower case the input text. Should be True for uncased "
    "models and False for cased models.")

flags.DEFINE_integer(
    "max_seq_length", 512,
    "The maximum total input sequence length after WordPiece tokenization. "
    "Sequences longer than this will be truncated, and sequences shorter "
    "than this will be padded.")

flags.DEFINE_bool("do_train", False, "Whether to run training.")

flags.DEFINE_bool("do_eval", False, "Whether to run eval on the dev set.")

flags.DEFINE_bool(
    "do_predict", False,
    "Whether to run the model in inference mode on the test set.")

flags.DEFINE_integer("train_batch_size", 32, "Total batch size for training.")

flags.DEFINE_integer("eval_batch_size", 8, "Total batch size for eval.")

flags.DEFINE_integer("predict_batch_size", 8, "Total batch size for predict.")

flags.DEFINE_float("learning_rate", 5e-5, "The initial learning rate for Adam.")

flags.DEFINE_integer("train_step", 1000,
                     "Total number of training steps to perform.")

flags.DEFINE_integer(
    "warmup_step", 0,
    "number of steps to perform linear learning rate warmup for.")

flags.DEFINE_integer("save_checkpoints_steps", 1000,
                     "How often to save the model checkpoint.")

flags.DEFINE_integer("keep_checkpoint_max", 5,
                     "How many checkpoints to keep.")

flags.DEFINE_integer("iterations_per_loop", 1000,
                     "How many steps to make in each estimator call.")

flags.DEFINE_bool("use_tpu", False, "Whether to use TPU or GPU/CPU.")

flags.DEFINE_string("optimizer", "adamw", "Optimizer to use")

tf.flags.DEFINE_string(
    "tpu_name", None,
    "The Cloud TPU to use for training. This should be either the name "
    "used when creating the Cloud TPU, or a grpc://ip.address.of.tpu:8470 "
    "url.")

tf.flags.DEFINE_string(
    "tpu_zone", None,
    "[Optional] GCE zone where the Cloud TPU is located in. If not "
    "specified, we will attempt to automatically detect the GCE project from "
    "metadata.")

tf.flags.DEFINE_string(
    "gcp_project", None,
    "[Optional] Project name for the Cloud TPU-enabled project. If not "
    "specified, we will attempt to automatically detect the GCE project from "
    "metadata.")

tf.flags.DEFINE_string("master", None, "[Optional] TensorFlow master URL.")

flags.DEFINE_integer(
    "num_tpu_cores", 8,
    "Only used if `use_tpu` is True. Total number of TPU cores to use.")

class InputExample(object):
  """A single training/test example for simple sequence classification."""

  def __init__(self, guid, text_a, text_b=None, label=None):
    """Constructs a InputExample.
    Args:
      guid: Unique id for the example.
      text_a: string. The untokenized text of the first sequence. For single
        sequence tasks, only this sequence must be specified.
      text_b: (Optional) string. The untokenized text of the second sequence.
        Only must be specified for sequence pair tasks.
      label: (Optional) string. The label of the example. This should be
        specified for train and dev examples, but not for test examples.
    """
    self.guid = guid
    self.text_a = text_a
    self.text_b = text_b
    self.label = label


class PaddingInputExample(object):
  """Fake example so the num input examples is a multiple of the batch size.
  When running eval/predict on the TPU, we need to pad the number of examples
  to be a multiple of the batch size, because the TPU requires a fixed batch
  size. The alternative is to drop the last batch, which is bad because it means
  the entire output data won't be generated.
  We use this class instead of `None` because treating `None` as padding
  battches could cause silent errors.
  """


class InputFeatures(object):
  """A single set of features of data."""

  def __init__(self,
               input_ids,
               input_mask,
               segment_ids,
               label_id,
               guid=None,
               example_id=None,
               is_real_example=True):
    self.input_ids = input_ids
    self.input_mask = input_mask
    self.segment_ids = segment_ids
    self.label_id = label_id
    self.example_id = example_id
    self.guid = guid
    self.is_real_example = is_real_example


class DataProcessor(object):
  """Base class for data converters for sequence classification data sets."""

  def __init__(self, use_spm, do_lower_case):
    super(DataProcessor, self).__init__()
    self.use_spm = use_spm
    self.do_lower_case = do_lower_case

  def get_train_examples(self, data_dir):
    """Gets a collection of `InputExample`s for the train set."""
    raise NotImplementedError()

  def get_dev_examples(self, data_dir):
    """Gets a collection of `InputExample`s for the dev set."""
    raise NotImplementedError()

  def get_test_examples(self, data_dir):
    """Gets a collection of `InputExample`s for prediction."""
    raise NotImplementedError()

  def get_labels(self):
    """Gets the list of labels for this data set."""
    raise NotImplementedError()

  @classmethod
  def _read_tsv(cls, input_file, quotechar=None):
    """Reads a tab separated value file."""
    with tf.gfile.Open(input_file, "r") as f:
      reader = csv.reader(f, delimiter="\t", quotechar=quotechar)
      lines = []
      for line in reader:
        lines.append(line)
      return lines

  def process_text(self, text):
    if self.use_spm:
      return tokenization.preprocess_text(text, lower=self.do_lower_case)
    else:
      return tokenization.convert_to_unicode(text)

class BIOSSES(DataProcessor):
  """Processor for the BIOSSES dataset."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BIOSSES", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BIOSSES", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BIOSSES", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    return ["0", "1", "2", "3", "4"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0:
        continue
      if set_type == "test":
        #guid = line[0]
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        text_b = self.process_test(line[1])
        label = "0"
      else:
        #text_a = self.process_text(line[3])
        #label = self.process_text(line[1])
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        text_b = self.process_test(line[1])
        label = self.process_text(line[-1])
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples

class ColaProcessor(DataProcessor):
  """Processor for the CoLA data set (GLUE version)."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "CoLA", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "CoLA", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "CoLA", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    return ["0", "1"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0:
        continue
      if set_type == "test":
        #guid = line[0]
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        label = "0"
      else:
        #text_a = self.process_text(line[3])
        #label = self.process_text(line[1])
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        label = self.process_text(line[1])
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples

class CovidProcessor(DataProcessor):
  """Processor for the COVID data set (Formulated Task)."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "COVID", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "COVID", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "COVID", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    return ["0", "1"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      # Only the test set has a header
      if i == 0:
        continue
      if set_type == "test":
        #guid = line[0]
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        label = "0"
      else:
        #text_a = self.process_text(line[3])
        #label = self.process_text(line[1])
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        label = self.process_text(line[1])
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples

class Covid2Processor(DataProcessor):
  """Processor for the COVID data set (Formulated Task)."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "COVID-2", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "COVID-2", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "COVID-2", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    return ["0", "1"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      # Only the test set has a header
      if i == 0:
        continue
      if set_type == "test":
        #guid = line[0]
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        label = "0"
      else:
        #text_a = self.process_text(line[3])
        #label = self.process_text(line[1])
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        label = self.process_text(line[1])
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples

class Sst2Processor(DataProcessor):
  """Processor for the SST-2 data set (GLUE version)."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "SST-2", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "SST-2", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "SST-2", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    return ["pubmed", "covid"]
    #return ["b", "i","o","x"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0:
        continue
      if set_type != "test":
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        label = self.process_text(line[1])
      else:
        #guid = self.process_text(line[0])
        #guid = "%s-%s" % (set_type, line[0])
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        label = "pubmed"
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples


class BC2GMProcessor(DataProcessor):
  """Processor for the BC2GM data set (GLUE version)."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BC2GM", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BC2GM", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BC2GM", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    #return ["0", "1"]
    return ["b", "i","o","x"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0:
        continue
      if set_type != "test":
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        label = self.process_text(line[1])
      else:
        #guid = self.process_text(line[0])
        #guid = "%s-%s" % (set_type, line[0])
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        label = "o"
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples

class BC4CHEMDProcessor(DataProcessor):
  """Processor for the BC4CHEMD data set (GLUE version)."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BC4CHEMD", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BC4CHEMD", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BC4CHEMD", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    #return ["0", "1"]
    return ["b", "i","o","x"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0:
        continue
      if set_type != "test":
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        label = self.process_text(line[1])
      else:
        #guid = self.process_text(line[0])
        #guid = "%s-%s" % (set_type, line[0])
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        label = "o"
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples

class BC5CDRChemProcessor(DataProcessor):
  """Processor for the BC5CDR-chem data set (GLUE version)."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BC5CDR-chem", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BC5CDR-chem", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BC5CDR-chem", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    #return ["0", "1"]
    return ["b", "i","o","x"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0:
        continue
      if set_type != "test":
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        label = self.process_text(line[1])
      else:
        #guid = self.process_text(line[0])
        #guid = "%s-%s" % (set_type, line[0])
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        label = "o"
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples

class BC5CDRDiseaseProcessor(DataProcessor):
  """Processor for the BC5CDR-disease data set (GLUE version)."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BC5CDR-disease", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BC5CDR-disease", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "BC5CDR-disease", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    #return ["0", "1"]
    return ["b", "i","o","x"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0:
        continue
      if set_type != "test":
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        label = self.process_text(line[1])
      else:
        #guid = self.process_text(line[0])
        #guid = "%s-%s" % (set_type, line[0])
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        label = "o"
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples

class JNLPBAProcessor(DataProcessor):
  """Processor for the JNLPBA data set (GLUE version)."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "JNLPBA", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "JNLPBA", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "JNLPBA", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    #return ["0", "1"]
    return ["b", "i","o","x"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0:
        continue
      if set_type != "test":
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        label = self.process_text(line[1])
      else:
        #guid = self.process_text(line[0])
        #guid = "%s-%s" % (set_type, line[0])
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        label = "o"
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples

class LinnaeusProcessor(DataProcessor):
  """Processor for the linnaeus data set (GLUE version)."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "linnaeus", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "linnaeus", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "linnaeus", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    #return ["0", "1"]
    return ["b", "i","o","x"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0:
        continue
      if set_type != "test":
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        label = self.process_text(line[1])
      else:
        #guid = self.process_text(line[0])
        #guid = "%s-%s" % (set_type, line[0])
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        label = "o"
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples

class NCBIDiseaseProcessor(DataProcessor):
  """Processor for the NCBI-disease data set (GLUE version)."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "NCBI-disease", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "NCBI-disease", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "NCBI-disease", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    #return ["0", "1"]
    return ["b", "i","o","x"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0:
        continue
      if set_type != "test":
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        label = self.process_text(line[1])
      else:
        #guid = self.process_text(line[0])
        #guid = "%s-%s" % (set_type, line[0])
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        label = "o"
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples

class S800Processor(DataProcessor):
  """Processor for the s800 data set (GLUE version)."""

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "s800", "train.tsv")), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "s800", "dev.tsv")), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, "s800", "test.tsv")), "test")

  def get_labels(self):
    """See base class."""
    #return ["0", "1"]
    return ["b", "i","o","x"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0:
        continue
      if set_type != "test":
        guid = "%s-%s" % (set_type, i)
        text_a = self.process_text(line[0])
        label = self.process_text(line[1])
      else:
        #guid = self.process_text(line[0])
        #guid = "%s-%s" % (set_type, line[0])
        guid = "%s-%s" % (set_type, i)
        #text_a = self.process_text(line[1])
        text_a = self.process_text(line[0])
        label = "o"
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
    return examples
def convert_single_example(ex_index, example, label_list, max_seq_length,
                           tokenizer, task_name):
  """Converts a single `InputExample` into a single `InputFeatures`."""

  if isinstance(example, PaddingInputExample):
    return InputFeatures(
        input_ids=[0] * max_seq_length,
        input_mask=[0] * max_seq_length,
        segment_ids=[0] * max_seq_length,
        label_id=0,
        is_real_example=False)

  if task_name != "sts-b":
    label_map = {}
    for (i, label) in enumerate(label_list):
      label_map[label] = i

  tokens_a = tokenizer.tokenize(example.text_a)
  tokens_b = None
  if example.text_b:
    tokens_b = tokenizer.tokenize(example.text_b)

  if tokens_b:
    # Modifies `tokens_a` and `tokens_b` in place so that the total
    # length is less than the specified length.
    # Account for [CLS], [SEP], [SEP] with "- 3"
    _truncate_seq_pair(tokens_a, tokens_b, max_seq_length - 3)
  else:
    # Account for [CLS] and [SEP] with "- 2"
    if len(tokens_a) > max_seq_length - 2:
      tokens_a = tokens_a[0:(max_seq_length - 2)]

  # The convention in ALBERT is:
  # (a) For sequence pairs:
  #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
  #  type_ids: 0     0  0    0    0     0       0 0     1  1  1  1   1 1
  # (b) For single sequences:
  #  tokens:   [CLS] the dog is hairy . [SEP]
  #  type_ids: 0     0   0   0  0     0 0
  #
  # Where "type_ids" are used to indicate whether this is the first
  # sequence or the second sequence. The embedding vectors for `type=0` and
  # `type=1` were learned during pre-training and are added to the
  # embedding vector (and position vector). This is not *strictly* necessary
  # since the [SEP] token unambiguously separates the sequences, but it makes
  # it easier for the model to learn the concept of sequences.
  #
  # For classification tasks, the first vector (corresponding to [CLS]) is
  # used as the "sentence vector". Note that this only makes sense because
  # the entire model is fine-tuned.
  tokens = []
  segment_ids = []
  tokens.append("[CLS]")
  segment_ids.append(0)
  for token in tokens_a:
    tokens.append(token)
    segment_ids.append(0)
  tokens.append("[SEP]")
  segment_ids.append(0)

  if tokens_b:
    for token in tokens_b:
      tokens.append(token)
      segment_ids.append(1)
    tokens.append("[SEP]")
    segment_ids.append(1)

  input_ids = tokenizer.convert_tokens_to_ids(tokens)

  # The mask has 1 for real tokens and 0 for padding tokens. Only real
  # tokens are attended to.
  input_mask = [1] * len(input_ids)

  # Zero-pad up to the sequence length.
  while len(input_ids) < max_seq_length:
    input_ids.append(0)
    input_mask.append(0)
    segment_ids.append(0)

  assert len(input_ids) == max_seq_length
  assert len(input_mask) == max_seq_length
  assert len(segment_ids) == max_seq_length

  if task_name != "sts-b":
    label_id = label_map[example.label]
  else:
    label_id = example.label

  if ex_index < 5:
    tf.logging.info("*** Example ***")
    tf.logging.info("guid: %s" % (example.guid))
    tf.logging.info("tokens: %s" % " ".join(
        [tokenization.printable_text(x) for x in tokens]))
    tf.logging.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
    tf.logging.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
    tf.logging.info("segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
    tf.logging.info("label: %s (id = %d)" % (example.label, label_id))

  feature = InputFeatures(
      input_ids=input_ids,
      input_mask=input_mask,
      segment_ids=segment_ids,
      label_id=label_id,
      is_real_example=True)
  return feature


def file_based_convert_examples_to_features(
    examples, label_list, max_seq_length, tokenizer, output_file, task_name):
  """Convert a set of `InputExample`s to a TFRecord file."""

  writer = tf.python_io.TFRecordWriter(output_file)

  for (ex_index, example) in enumerate(examples):
    if ex_index % 10000 == 0:
      tf.logging.info("Writing example %d of %d" % (ex_index, len(examples)))

    feature = convert_single_example(ex_index, example, label_list,
                                     max_seq_length, tokenizer, task_name)

    def create_int_feature(values):
      f = tf.train.Feature(int64_list=tf.train.Int64List(value=list(values)))
      return f

    def create_float_feature(values):
      f = tf.train.Feature(float_list=tf.train.FloatList(value=list(values)))
      return f

    features = collections.OrderedDict()
    features["input_ids"] = create_int_feature(feature.input_ids)
    features["input_mask"] = create_int_feature(feature.input_mask)
    features["segment_ids"] = create_int_feature(feature.segment_ids)
    features["label_ids"] = create_float_feature([feature.label_id])\
        if task_name == "sts-b" else create_int_feature([feature.label_id])
    features["is_real_example"] = create_int_feature(
        [int(feature.is_real_example)])

    tf_example = tf.train.Example(features=tf.train.Features(feature=features))
    writer.write(tf_example.SerializeToString())
  writer.close()


def file_based_input_fn_builder(input_file, seq_length, is_training,
                                drop_remainder, task_name, use_tpu, bsz,
                                multiple=1):
  """Creates an `input_fn` closure to be passed to TPUEstimator."""
  labeltype = tf.float32 if task_name == "sts-b" else tf.int64

  name_to_features = {
      "input_ids": tf.FixedLenFeature([seq_length * multiple], tf.int64),
      "input_mask": tf.FixedLenFeature([seq_length * multiple], tf.int64),
      "segment_ids": tf.FixedLenFeature([seq_length * multiple], tf.int64),
      "label_ids": tf.FixedLenFeature([], labeltype),
      "is_real_example": tf.FixedLenFeature([], tf.int64),
  }

  def _decode_record(record, name_to_features):
    """Decodes a record to a TensorFlow example."""
    example = tf.parse_single_example(record, name_to_features)

    # tf.Example only supports tf.int64, but the TPU only supports tf.int32.
    # So cast all int64 to int32.
    for name in list(example.keys()):
      t = example[name]
      if t.dtype == tf.int64:
        t = tf.to_int32(t)
      example[name] = t

    return example

  def input_fn(params):
    """The actual input function."""
    if use_tpu:
      batch_size = params["batch_size"]
    else:
      batch_size = bsz

    # For training, we want a lot of parallel reading and shuffling.
    # For eval, we want no shuffling and parallel reading doesn't matter.
    d = tf.data.TFRecordDataset(input_file)
    if is_training:
      d = d.repeat()
      d = d.shuffle(buffer_size=100)

    d = d.apply(
        contrib_data.map_and_batch(
            lambda record: _decode_record(record, name_to_features),
            batch_size=batch_size,
            drop_remainder=drop_remainder))

    return d

  return input_fn


def _truncate_seq_pair(tokens_a, tokens_b, max_length):
  """Truncates a sequence pair in place to the maximum length."""

  # This is a simple heuristic which will always truncate the longer sequence
  # one token at a time. This makes more sense than truncating an equal percent
  # of tokens from each, since if one sequence is very short then each token
  # that's truncated likely contains more information than a longer sequence.
  while True:
    total_length = len(tokens_a) + len(tokens_b)
    if total_length <= max_length:
      break
    if len(tokens_a) > len(tokens_b):
      tokens_a.pop()
    else:
      tokens_b.pop()


def create_model(albert_config, is_training, input_ids, input_mask, segment_ids,
                 labels, num_labels, use_one_hot_embeddings, task_name,
                 hub_module):
  """Creates a classification model."""
  (output_layer, _) = fine_tuning_utils.create_albert(
      albert_config=albert_config,
      is_training=is_training,
      input_ids=input_ids,
      input_mask=input_mask,
      segment_ids=segment_ids,
      use_one_hot_embeddings=use_one_hot_embeddings,
      use_einsum=True,
      hub_module=hub_module)

  hidden_size = output_layer.shape[-1].value

  output_weights = tf.get_variable(
      "output_weights", [num_labels, hidden_size],
      initializer=tf.truncated_normal_initializer(stddev=0.02))

  output_bias = tf.get_variable(
      "output_bias", [num_labels], initializer=tf.zeros_initializer())

  with tf.variable_scope("loss"):
    if is_training:
      # I.e., 0.1 dropout
      output_layer = tf.nn.dropout(output_layer, keep_prob=0.9)

    logits = tf.matmul(output_layer, output_weights, transpose_b=True)
    logits = tf.nn.bias_add(logits, output_bias)
    if task_name != "sts-b":
      probabilities = tf.nn.softmax(logits, axis=-1)
      predictions = tf.argmax(probabilities, axis=-1, output_type=tf.int32)
      log_probs = tf.nn.log_softmax(logits, axis=-1)
      one_hot_labels = tf.one_hot(labels, depth=num_labels, dtype=tf.float32)

      per_example_loss = -tf.reduce_sum(one_hot_labels * log_probs, axis=-1)
    else:
      probabilities = logits
      logits = tf.squeeze(logits, [-1])
      predictions = logits
      per_example_loss = tf.square(logits - labels)
    loss = tf.reduce_mean(per_example_loss)

    return (loss, per_example_loss, probabilities, logits, predictions)


def model_fn_builder(albert_config, num_labels, init_checkpoint, learning_rate,
                     num_train_steps, num_warmup_steps, use_tpu,
                     use_one_hot_embeddings, task_name, hub_module=None,
                     optimizer="adamw"):
  """Returns `model_fn` closure for TPUEstimator."""

  def model_fn(features, labels, mode, params):  # pylint: disable=unused-argument
    """The `model_fn` for TPUEstimator."""

    tf.logging.info("*** Features ***")
    for name in sorted(features.keys()):
      tf.logging.info("  name = %s, shape = %s" % (name, features[name].shape))

    input_ids = features["input_ids"]
    input_mask = features["input_mask"]
    segment_ids = features["segment_ids"]
    label_ids = features["label_ids"]
    is_real_example = None
    if "is_real_example" in features:
      is_real_example = tf.cast(features["is_real_example"], dtype=tf.float32)
    else:
      is_real_example = tf.ones(tf.shape(label_ids), dtype=tf.float32)

    is_training = (mode == tf.estimator.ModeKeys.TRAIN)

    (total_loss, per_example_loss, probabilities, logits, predictions) = \
        create_model(albert_config, is_training, input_ids, input_mask,
                     segment_ids, label_ids, num_labels, use_one_hot_embeddings,
                     task_name, hub_module)

    tvars = tf.trainable_variables()
    initialized_variable_names = {}
    scaffold_fn = None
    if init_checkpoint:
      (assignment_map, initialized_variable_names
      ) = modeling.get_assignment_map_from_checkpoint(tvars, init_checkpoint)
      if use_tpu:

        def tpu_scaffold():
          tf.train.init_from_checkpoint(init_checkpoint, assignment_map)
          return tf.train.Scaffold()

        scaffold_fn = tpu_scaffold
      else:
        tf.train.init_from_checkpoint(init_checkpoint, assignment_map)

    tf.logging.info("**** Trainable Variables ****")
    for var in tvars:
      init_string = ""
      if var.name in initialized_variable_names:
        init_string = ", *INIT_FROM_CKPT*"
      tf.logging.info("  name = %s, shape = %s%s", var.name, var.shape,
                      init_string)

    output_spec = None
    if mode == tf.estimator.ModeKeys.TRAIN:

      train_op = optimization.create_optimizer(
          total_loss, learning_rate, num_train_steps, num_warmup_steps,
          use_tpu, optimizer)

      output_spec = contrib_tpu.TPUEstimatorSpec(
          mode=mode,
          loss=total_loss,
          train_op=train_op,
          scaffold_fn=scaffold_fn)
    elif mode == tf.estimator.ModeKeys.EVAL:
      if task_name == "sts-b":
        def metric_fn(per_example_loss, label_ids, logits, is_real_example):
          """Compute Pearson correlations for STS-B."""
          # Display labels and predictions
          concat1 = contrib_metrics.streaming_concat(logits)
          concat2 = contrib_metrics.streaming_concat(label_ids)

          # Compute Pearson correlation
          pearson = contrib_metrics.streaming_pearson_correlation(
              logits, label_ids, weights=is_real_example)

          # Compute MSE
          # mse = tf.metrics.mean(per_example_loss)
          mse = tf.metrics.mean_squared_error(
              label_ids, logits, weights=is_real_example)

          loss = tf.metrics.mean(
              values=per_example_loss,
              weights=is_real_example)

          return {"pred": concat1, "label_ids": concat2, "pearson": pearson,
                  "MSE": mse, "eval_loss": loss,}
      else:
        def metric_fn(per_example_loss, label_ids, logits, is_real_example):
          """Compute Matthew's correlations for COLA."""
          predictions = tf.argmax(logits, axis=-1, output_type=tf.int32)
          # https://en.wikipedia.org/wiki/Matthews_correlation_coefficient
          tp, tp_op = tf.metrics.true_positives(
              labels=label_ids, predictions=predictions,
              weights=is_real_example)
          tn, tn_op = tf.metrics.true_negatives(
              labels=label_ids, predictions=predictions,
              weights=is_real_example)
          fp, fp_op = tf.metrics.false_positives(
              labels=label_ids, predictions=predictions,
              weights=is_real_example)
          fn, fn_op = tf.metrics.false_negatives(
              labels=label_ids, predictions=predictions,
              weights=is_real_example)

          # computing precision, recall and f1 score 
          # Added for BioAlbert
          precision = tf_metrics.precision(label_ids,predictions,num_labels,[1,2],average="micro")
          recall = tf_metrics.recall(label_ids,predictions,num_labels,[1,2],average="micro")
          f1 = tf_metrics.f1(label_ids,predictions,num_labels,[1,2],average="micro")
          
          # Compute Matthew's correlation
          mcc = tf.div_no_nan(
              tp * tn - fp * fn,
              tf.pow((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn), 0.5))

          # Compute accuracy
          accuracy = tf.metrics.accuracy(
              labels=label_ids, predictions=predictions,
              weights=is_real_example)

          loss = tf.metrics.mean(
              values=per_example_loss,
              weights=is_real_example)

          return {"matthew_corr": (mcc, tf.group(tp_op, tn_op, fp_op, fn_op)),
                  "accuracy": accuracy, "eval_loss": loss,
                  "precision":precision,
                  "recall":recall,
                  "f1_score": f1,}

      eval_metrics = (metric_fn,
                      [per_example_loss, label_ids, logits, is_real_example])
      output_spec = contrib_tpu.TPUEstimatorSpec(
          mode=mode,
          loss=total_loss,
          eval_metrics=eval_metrics,
          scaffold_fn=scaffold_fn)
    else:
      output_spec = contrib_tpu.TPUEstimatorSpec(
          mode=mode,
          predictions={
              "probabilities": probabilities,
              "predictions": predictions
          },
          scaffold_fn=scaffold_fn)
    return output_spec

  return model_fn


# This function is not used by this file but is still used by the Colab and
# people who depend on it.
def input_fn_builder(features, seq_length, is_training, drop_remainder):
  """Creates an `input_fn` closure to be passed to TPUEstimator."""

  all_input_ids = []
  all_input_mask = []
  all_segment_ids = []
  all_label_ids = []

  for feature in features:
    all_input_ids.append(feature.input_ids)
    all_input_mask.append(feature.input_mask)
    all_segment_ids.append(feature.segment_ids)
    all_label_ids.append(feature.label_id)

  def input_fn(params):
    """The actual input function."""
    batch_size = params["batch_size"]

    num_examples = len(features)

    # This is for demo purposes and does NOT scale to large data sets. We do
    # not use Dataset.from_generator() because that uses tf.py_func which is
    # not TPU compatible. The right way to load data is with TFRecordReader.
    d = tf.data.Dataset.from_tensor_slices({
        "input_ids":
            tf.constant(
                all_input_ids, shape=[num_examples, seq_length],
                dtype=tf.int32),
        "input_mask":
            tf.constant(
                all_input_mask,
                shape=[num_examples, seq_length],
                dtype=tf.int32),
        "segment_ids":
            tf.constant(
                all_segment_ids,
                shape=[num_examples, seq_length],
                dtype=tf.int32),
        "label_ids":
            tf.constant(all_label_ids, shape=[num_examples], dtype=tf.int32),
    })

    if is_training:
      d = d.repeat()
      d = d.shuffle(buffer_size=100)

    d = d.batch(batch_size=batch_size, drop_remainder=drop_remainder)
    return d

  return input_fn


# This function is not used by this file but is still used by the Colab and
# people who depend on it.
def convert_examples_to_features(examples, label_list, max_seq_length,
                                 tokenizer, task_name):
  """Convert a set of `InputExample`s to a list of `InputFeatures`."""

  features = []
  for (ex_index, example) in enumerate(examples):
    if ex_index % 10000 == 0:
      tf.logging.info("Writing example %d of %d" % (ex_index, len(examples)))

    feature = convert_single_example(ex_index, example, label_list,
                                     max_seq_length, tokenizer, task_name)

    features.append(feature)
  return features
  
def main(_):
  tf.logging.set_verbosity(tf.logging.INFO)

  processors = {
      "cola": ColaProcessor,
      "sst-2": Sst2Processor,
      "bc2gm": BC2GMProcessor,
      "bc4chemd": BC4CHEMDProcessor,
      "bc5cdr-chem": BC5CDRChemProcessor,
      "bc5cdr-disease": BC5CDRDiseaseProcessor,
      "jnlpba": JNLPBAProcessor,
      "linnaeus": LinnaeusProcessor,
      "ncbi-disease": NCBIDiseaseProcessor,
      "s800": S800Processor,
      "covid": CovidProcessor,
      "covid-2": Covid2Processor,
  }

  if not FLAGS.do_train and not FLAGS.do_eval and not FLAGS.do_predict:
    raise ValueError(
        "At least one of `do_train`, `do_eval` or `do_predict' must be True.")

  if not FLAGS.albert_config_file and not FLAGS.albert_hub_module_handle:
    raise ValueError("At least one of `--albert_config_file` and "
                     "`--albert_hub_module_handle` must be set")

  if FLAGS.albert_config_file:
    albert_config = modeling.AlbertConfig.from_json_file(
        FLAGS.albert_config_file)
    if FLAGS.max_seq_length > albert_config.max_position_embeddings:
      raise ValueError(
          "Cannot use sequence length %d because the ALBERT model "
          "was only trained up to sequence length %d" %
          (FLAGS.max_seq_length, albert_config.max_position_embeddings))
  else:
    albert_config = None  # Get the config from TF-Hub.

  tf.gfile.MakeDirs(FLAGS.output_dir)

  task_name = FLAGS.task_name.lower()

  if task_name not in processors:
    raise ValueError("Task not found: %s" % (task_name))

  processor = processors[task_name](
      use_spm=True if FLAGS.spm_model_file else False,
      do_lower_case=FLAGS.do_lower_case)

  label_list = processor.get_labels()

  tokenizer = fine_tuning_utils.create_vocab(
      vocab_file=FLAGS.vocab_file,
      do_lower_case=FLAGS.do_lower_case,
      spm_model_file=FLAGS.spm_model_file,
      hub_module=FLAGS.albert_hub_module_handle)

  tpu_cluster_resolver = None
  if FLAGS.use_tpu and FLAGS.tpu_name:
    tpu_cluster_resolver = contrib_cluster_resolver.TPUClusterResolver(
        FLAGS.tpu_name, zone=FLAGS.tpu_zone, project=FLAGS.gcp_project)

  is_per_host = contrib_tpu.InputPipelineConfig.PER_HOST_V2
  if FLAGS.do_train:
    iterations_per_loop = int(min(FLAGS.iterations_per_loop,
                                  FLAGS.save_checkpoints_steps))
  else:
    iterations_per_loop = FLAGS.iterations_per_loop
  run_config = contrib_tpu.RunConfig(
      cluster=tpu_cluster_resolver,
      master=FLAGS.master,
      model_dir=FLAGS.output_dir,
      save_checkpoints_steps=int(FLAGS.save_checkpoints_steps),
      keep_checkpoint_max=0,
      tpu_config=contrib_tpu.TPUConfig(
          iterations_per_loop=iterations_per_loop,
          num_shards=FLAGS.num_tpu_cores,
          per_host_input_for_training=is_per_host))

  train_examples = None
  if FLAGS.do_train:
    train_examples = processor.get_train_examples(FLAGS.data_dir)
  model_fn = model_fn_builder(
      albert_config=albert_config,
      num_labels=len(label_list),
      init_checkpoint=FLAGS.init_checkpoint,
      learning_rate=FLAGS.learning_rate,
      num_train_steps=FLAGS.train_step,
      num_warmup_steps=FLAGS.warmup_step,
      use_tpu=FLAGS.use_tpu,
      use_one_hot_embeddings=FLAGS.use_tpu,
      task_name=task_name,
      hub_module=FLAGS.albert_hub_module_handle,
      optimizer=FLAGS.optimizer)

  # If TPU is not available, this will fall back to normal Estimator on CPU
  # or GPU.
  estimator = contrib_tpu.TPUEstimator(
      use_tpu=FLAGS.use_tpu,
      model_fn=model_fn,
      config=run_config,
      train_batch_size=FLAGS.train_batch_size,
      eval_batch_size=FLAGS.eval_batch_size,
      predict_batch_size=FLAGS.predict_batch_size)

  if FLAGS.do_train:
    cached_dir = FLAGS.cached_dir
    if not cached_dir:
      cached_dir = FLAGS.output_dir
    train_file = os.path.join(cached_dir, task_name + "_train.tf_record")
    if not tf.gfile.Exists(train_file):
      file_based_convert_examples_to_features(
          train_examples, label_list, FLAGS.max_seq_length, tokenizer,
          train_file, task_name)
    tf.logging.info("***** Running training *****")
    tf.logging.info("  Num examples = %d", len(train_examples))
    tf.logging.info("  Batch size = %d", FLAGS.train_batch_size)
    tf.logging.info("  Num steps = %d", FLAGS.train_step)
    train_input_fn = file_based_input_fn_builder(
        input_file=train_file,
        seq_length=FLAGS.max_seq_length,
        is_training=True,
        drop_remainder=True,
        task_name=task_name,
        use_tpu=FLAGS.use_tpu,
        bsz=FLAGS.train_batch_size)
    estimator.train(input_fn=train_input_fn, max_steps=FLAGS.train_step)

  if FLAGS.do_eval:
    eval_examples = processor.get_dev_examples(FLAGS.data_dir)
    num_actual_eval_examples = len(eval_examples)
    if FLAGS.use_tpu:
      # TPU requires a fixed batch size for all batches, therefore the number
      # of examples must be a multiple of the batch size, or else examples
      # will get dropped. So we pad with fake examples which are ignored
      # later on. These do NOT count towards the metric (all tf.metrics
      # support a per-instance weight, and these get a weight of 0.0).
      while len(eval_examples) % FLAGS.eval_batch_size != 0:
        eval_examples.append(PaddingInputExample())

    cached_dir = FLAGS.cached_dir
    if not cached_dir:
      cached_dir = FLAGS.output_dir
    eval_file = os.path.join(cached_dir, task_name + "_eval.tf_record")
    if not tf.gfile.Exists(eval_file):
      file_based_convert_examples_to_features(
          eval_examples, label_list, FLAGS.max_seq_length, tokenizer,
          eval_file, task_name)

    tf.logging.info("***** Running evaluation *****")
    tf.logging.info("  Num examples = %d (%d actual, %d padding)",
                    len(eval_examples), num_actual_eval_examples,
                    len(eval_examples) - num_actual_eval_examples)
    tf.logging.info("  Batch size = %d", FLAGS.eval_batch_size)

    # This tells the estimator to run through the entire set.
    eval_steps = None
    # However, if running eval on the TPU, you will need to specify the
    # number of steps.
    if FLAGS.use_tpu:
      assert len(eval_examples) % FLAGS.eval_batch_size == 0
      eval_steps = int(len(eval_examples) // FLAGS.eval_batch_size)

    eval_drop_remainder = True if FLAGS.use_tpu else False
    eval_input_fn = file_based_input_fn_builder(
        input_file=eval_file,
        seq_length=FLAGS.max_seq_length,
        is_training=False,
        drop_remainder=eval_drop_remainder,
        task_name=task_name,
        use_tpu=FLAGS.use_tpu,
        bsz=FLAGS.eval_batch_size)

    best_trial_info_file = os.path.join(FLAGS.output_dir, "best_trial.txt")

    def _best_trial_info():
      """Returns information about which checkpoints have been evaled so far."""
      if tf.gfile.Exists(best_trial_info_file):
        with tf.gfile.GFile(best_trial_info_file, "r") as best_info:
          global_step, best_metric_global_step, metric_value = (
              best_info.read().split(":"))
          global_step = int(global_step)
          best_metric_global_step = int(best_metric_global_step)
          metric_value = float(metric_value)
      else:
        metric_value = -1
        best_metric_global_step = -1
        global_step = -1
      tf.logging.info(
          "Best trial info: Step: %s, Best Value Step: %s, "
          "Best Value: %s", global_step, best_metric_global_step, metric_value)
      return global_step, best_metric_global_step, metric_value

    def _remove_checkpoint(checkpoint_path):
      for ext in ["meta", "data-00000-of-00001", "index"]:
        src_ckpt = checkpoint_path + ".{}".format(ext)
        tf.logging.info("removing {}".format(src_ckpt))
        tf.gfile.Remove(src_ckpt)

    def _find_valid_cands(curr_step):
      filenames = tf.gfile.ListDirectory(FLAGS.output_dir)
      candidates = []
      for filename in filenames:
        if filename.endswith(".index"):
          ckpt_name = filename[:-6]
          idx = ckpt_name.split("-")[-1]
          if int(idx) > curr_step:
            candidates.append(filename)
      return candidates

    output_eval_file = os.path.join(FLAGS.output_dir, "eval_results.txt")

    if task_name == "sts-b":
      key_name = "pearson"
    elif task_name == "cola":
      #key_name = "matthew_corr"
      key_name = "f1_score"
    elif task_name == "sst-2":
      key_name = "f1_score"
    else:
      key_name = "f1_score"

    global_step, best_perf_global_step, best_perf = _best_trial_info()
    writer = tf.gfile.GFile(output_eval_file, "w")
    while global_step < FLAGS.train_step:
      steps_and_files = {}
      filenames = tf.gfile.ListDirectory(FLAGS.output_dir)
      for filename in filenames:
        if filename.endswith(".index"):
          ckpt_name = filename[:-6]
          cur_filename = os.path.join(FLAGS.output_dir, ckpt_name)
          if cur_filename.split("-")[-1] == "best":
            continue
          gstep = int(cur_filename.split("-")[-1])
          if gstep not in steps_and_files:
            tf.logging.info("Add {} to eval list.".format(cur_filename))
            steps_and_files[gstep] = cur_filename
      tf.logging.info("found {} files.".format(len(steps_and_files)))
      if not steps_and_files:
        tf.logging.info("found 0 file, global step: {}. Sleeping."
                        .format(global_step))
        time.sleep(60)
      else:
        for checkpoint in sorted(steps_and_files.items()):
          step, checkpoint_path = checkpoint
          if global_step >= step:
            if (best_perf_global_step != step and
                len(_find_valid_cands(step)) > 1):
              _remove_checkpoint(checkpoint_path)
            continue
          result = estimator.evaluate(
              input_fn=eval_input_fn,
              steps=eval_steps,
              checkpoint_path=checkpoint_path)
          global_step = result["global_step"]
          tf.logging.info("***** Eval results *****")
          for key in sorted(result.keys()):
            tf.logging.info("  %s = %s", key, str(result[key]))
            writer.write("%s = %s\n" % (key, str(result[key])))
          writer.write("best = {}\n".format(best_perf))
          if result[key_name] > best_perf:
            best_perf = result[key_name]
            best_perf_global_step = global_step
          elif len(_find_valid_cands(global_step)) > 1:
            _remove_checkpoint(checkpoint_path)
          writer.write("=" * 50 + "\n")
          writer.flush()
          with tf.gfile.GFile(best_trial_info_file, "w") as best_info:
            best_info.write("{}:{}:{}".format(
                global_step, best_perf_global_step, best_perf))
    writer.close()

    for ext in ["meta", "data-00000-of-00001", "index"]:
      src_ckpt = "model.ckpt-{}.{}".format(best_perf_global_step, ext)
      tgt_ckpt = "model.ckpt-best.{}".format(ext)
      tf.logging.info("saving {} to {}".format(src_ckpt, tgt_ckpt))
      tf.io.gfile.rename(
          os.path.join(FLAGS.output_dir, src_ckpt),
          os.path.join(FLAGS.output_dir, tgt_ckpt),
          overwrite=True)

  if FLAGS.do_predict:
    predict_examples = processor.get_test_examples(FLAGS.data_dir)
    num_actual_predict_examples = len(predict_examples)

    output_predict_result_file = os.path.join(FLAGS.output_dir, "predict_results.txt")
    predict_result_writer = tf.gfile.GFile(output_predict_result_file, "w")

    if FLAGS.use_tpu:
      # TPU requires a fixed batch size for all batches, therefore the number
      # of examples must be a multiple of the batch size, or else examples
      # will get dropped. So we pad with fake examples which are ignored
      # later on.
      while len(predict_examples) % FLAGS.predict_batch_size != 0:
        predict_examples.append(PaddingInputExample())

    predict_file = os.path.join(FLAGS.output_dir, "predict.tf_record")
    file_based_convert_examples_to_features(
        predict_examples, label_list,
        FLAGS.max_seq_length, tokenizer,
        predict_file, task_name)

    tf.logging.info("***** Running prediction*****")
    tf.logging.info("  Num examples = %d (%d actual, %d padding)",
                    len(predict_examples), num_actual_predict_examples,
                    len(predict_examples) - num_actual_predict_examples)
    tf.logging.info("  Batch size = %d", FLAGS.predict_batch_size)

    predict_drop_remainder = True if FLAGS.use_tpu else False
    predict_input_fn = file_based_input_fn_builder(
        input_file=predict_file,
        seq_length=FLAGS.max_seq_length,
        is_training=False,
        drop_remainder=predict_drop_remainder,
        task_name=task_name,
        use_tpu=FLAGS.use_tpu,
        bsz=FLAGS.predict_batch_size)

    checkpoint_path = os.path.join(FLAGS.output_dir, "model.ckpt-best")
    result = estimator.predict(
        input_fn=predict_input_fn,
        checkpoint_path=checkpoint_path)
        
    if FLAGS.use_tpu:
      assert len(predict_examples) % FLAGS.predict_batch_size == 0
      predict_steps = int(len(predict_examples) // FLAGS.predict_batch_size)
      
    predict_result = estimator.evaluate(
              input_fn=predict_input_fn,
              steps=predict_steps,
              checkpoint_path=checkpoint_path
              )


    output_predict_file = os.path.join(FLAGS.output_dir, "test_results.tsv")
    output_submit_file = os.path.join(FLAGS.output_dir, "submit_results.tsv")

    with tf.gfile.GFile(output_predict_file, "w") as pred_writer,\
        tf.gfile.GFile(output_submit_file, "w") as sub_writer:
      sub_writer.write("index" + "\t" + "prediction\n")
      num_written_lines = 0
      tf.logging.info("***** Predict results *****")
      for (i, (example, prediction)) in\
          enumerate(zip(predict_examples, result)):
        probabilities = prediction["probabilities"]
        if i >= num_actual_predict_examples:
          break
        output_line = "\t".join(
            str(class_probability)
            for class_probability in probabilities) + "\n"
        pred_writer.write(output_line)

        if task_name != "sts-b":
          actual_label = label_list[int(prediction["predictions"])]
        else:
          actual_label = str(prediction["predictions"])
        sub_writer.write(example.guid + "\t" + actual_label + "\n")
        num_written_lines += 1
    assert num_written_lines == num_actual_predict_examples

    tf.logging.info("***** Prediction Results *****")
    for key in sorted(predict_result.keys()):
        tf.logging.info("  %s = %s", key, str(predict_result[key]))
        predict_result_writer.write("%s = %s\n" % (key, str(predict_result[key])))
        predict_result_writer.write("=" * 50 + "\n")
        predict_result_writer.flush()

    predict_result_writer.close()


if __name__ == "__main__":
  flags.mark_flag_as_required("data_dir")
  flags.mark_flag_as_required("task_name")
  flags.mark_flag_as_required("spm_model_file")
  flags.mark_flag_as_required("output_dir")
  tf.app.run()