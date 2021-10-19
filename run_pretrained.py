#!/usr/bin/env python
# coding=utf-8
# Copyright 2020 The HuggingFace Team All rights reserved.
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
"""
Fine-tuning the library models for masked language modeling (BERT, ALBERT, RoBERTa...) on a text file or a dataset.

Here is the full list of checkpoints on the hub that can be fine-tuned by this script:
https://huggingface.co/models?filter=masked-lm
"""
# You can also adapt this script on your own masked language modeling task. Pointers for this are left as comments.
import torch
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

from extra.dataset_dict import DatasetDict as newDatasetDict
from extra.tokenizer import PreTrainedTokenizerBase as newPreTrainedTokenizerBase

from datasets import load_dataset, concatenate_datasets

import numpy as np

import transformers
from transformers import (
    CONFIG_MAPPING,
    MODEL_FOR_MASKED_LM_MAPPING,
    AutoConfig,
    EvalPrediction,
    AutoTokenizer,
    DataCollatorForLanguageModeling,
    HfArgumentParser,
    TrainingArguments,
    set_seed,
)
from extra.classifier_trainer import SM_Trainer as Trainer
from transformers.trainer_utils import get_last_checkpoint, is_main_process
from transformers.utils import check_min_version
from models.modeling_ponet import PoNetForPreTraining
import random

# Will error if the minimal version of Transformers is not installed. Remove at your own risks.
check_min_version("4.6.0")

logger = logging.getLogger(__name__)
MODEL_CONFIG_CLASSES = list(MODEL_FOR_MASKED_LM_MAPPING.keys())
MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)


@dataclass
class ModelArguments:
  """
  Arguments pertaining to which model/config/tokenizer we are going to fine-tune, or train from scratch.
  """

  model_name_or_path: Optional[str] = field(
      default=None,
      metadata={
          "help": "The model checkpoint for weights initialization."
          "Don't set if you want to train a model from scratch."
      },
  )
  model_type: Optional[str] = field(
      default=None,
      metadata={"help": "If training from scratch, pass a model type from the list: " + ", ".join(MODEL_TYPES)},
  )
  config_name: Optional[str] = field(
      default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
  )
  tokenizer_name: Optional[str] = field(
      default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
  )
  cache_dir: Optional[str] = field(
      default=None,
      metadata={"help": "Where do you want to store the pretrained models downloaded from huggingface.co"},
  )
  use_fast_tokenizer: bool = field(
      default=True,
      metadata={"help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
  )
  model_revision: str = field(
      default="main",
      metadata={"help": "The specific model version to use (can be a branch name, tag name or commit id)."},
  )
  use_auth_token: bool = field(
      default=False,
      metadata={
          "help": "Will use the token generated when running `transformers-cli login` (necessary to use this script "
          "with private models)."
      },
  )


@dataclass
class DataTrainingArguments:
  """
  Arguments pertaining to what data we are going to input our model for training and eval.
  """

  dataset_name: Optional[str] = field(
      default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
  )
  dataset_config_name: Optional[str] = field(
      default=None, metadata={"help": "The configuration name of the dataset to use (via the datasets library)."}
  )
  dataset2_name: Optional[str] = field(
      default=None, metadata={"help": "The name of the dataset2 to use (via the datasets library)."}
  )
  dataset2_config_name: Optional[str] = field(
      default=None, metadata={"help": "The configuration name of the dataset2 to use (via the datasets library)."}
  )
  train_file: Optional[str] = field(default=None, metadata={"help": "The input training data file (a text file)."})
  validation_file: Optional[str] = field(
      default=None,
      metadata={"help": "An optional input evaluation data file to evaluate the perplexity on (a text file)."},
  )
  overwrite_cache: bool = field(
      default=False, metadata={"help": "Overwrite the cached training and evaluation sets"}
  )
  validation_split_percentage: Optional[int] = field(
      default=5,
      metadata={
          "help": "The percentage of the train set used as validation set in case there's no validation split"
      },
  )
  max_seq_length: Optional[int] = field(
      default=None,
      metadata={
          "help": "The maximum total input sequence length after tokenization. Sequences longer "
          "than this will be truncated."
      },
  )
  preprocessing_num_workers: Optional[int] = field(
      default=None,
      metadata={"help": "The number of processes to use for the preprocessing."},
  )
  mlm_probability: float = field(
      default=0.15, metadata={"help": "Ratio of tokens to mask for masked language modeling loss"}
  )
  line_by_line: bool = field(
      default=False,
      metadata={"help": "Whether distinct lines of text in the dataset are to be handled as distinct sequences."},
  )
  pad_to_max_length: bool = field(
      default=False,
      metadata={
          "help": "Whether to pad all samples to `max_seq_length`. "
          "If False, will pad the samples dynamically when batching to the maximum length in the batch."
      },
  )
  dupe_factor: int = field(
      default=5,
      metadata={
          "help": "Number of times to duplicate the input data (with different masks)."
      },
  )
  max_train_samples: Optional[int] = field(
      default=None,
      metadata={
          "help": "For debugging purposes or quicker training, truncate the number of training examples to this "
          "value if set."
      },
  )
  max_eval_samples: Optional[int] = field(
      default=None,
      metadata={
          "help": "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
          "value if set."
      },
  )

  def __post_init__(self):
    if self.dataset_name is None and self.train_file is None and self.validation_file is None:
      raise ValueError("Need either a dataset name or a training/validation file.")
    else:
      if self.train_file is not None:
        extension = self.train_file.split(".")[-1]
        assert extension in ["csv", "json", "txt"], "`train_file` should be a csv, a json or a txt file."
      if self.validation_file is not None:
        extension = self.validation_file.split(".")[-1]
        assert extension in ["csv", "json", "txt"], "`validation_file` should be a csv, a json or a txt file."


def main():
  # See all possible arguments in src/transformers/training_args.py
  # or by passing the --help flag to this script.
  # We now keep distinct sets of args, for a cleaner separation of concerns.

  parser = HfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments))
  if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
    # If we pass only one argument to the script and it's the path to a json file,
    # let's parse it to get our arguments.
    model_args, data_args, training_args = parser.parse_json_file(json_file=os.path.abspath(sys.argv[1]))
  else:
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()

  # Detecting last checkpoint.
  last_checkpoint = None
  if os.path.isdir(training_args.output_dir) and training_args.do_train and not training_args.overwrite_output_dir:
    last_checkpoint = get_last_checkpoint(training_args.output_dir)
    if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
      raise ValueError(
          f"Output directory ({training_args.output_dir}) already exists and is not empty. "
          "Use --overwrite_output_dir to overcome."
      )
    elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
      logger.info(
          f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
          "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
      )

  # Setup logging
  logging.basicConfig(
      format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
      datefmt="%m/%d/%Y %H:%M:%S",
      handlers=[logging.StreamHandler(sys.stdout)],
  )
  logger.setLevel(logging.INFO if is_main_process(training_args.local_rank) else logging.WARN)

  # Log on each process the small summary:
  logger.warning(
      f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
      + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
  )
  # Set the verbosity to info of the Transformers logger (on main process only):
  if is_main_process(training_args.local_rank):
    transformers.utils.logging.set_verbosity_info()
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()
  logger.info(f"Training/evaluation parameters {training_args}")

  # Set seed before initializing model.
  set_seed(training_args.seed)

  # Get the datasets: you can either provide your own CSV/JSON/TXT training and evaluation files (see below)
  # or just provide the name of one of the public datasets available on the hub at https://huggingface.co/datasets/
  # (the dataset will be downloaded automatically from the datasets Hub
  #
  # For CSV/JSON files, this script will use the column called 'text' or the first column. You can easily tweak this
  # behavior (see below)
  #
  # In distributed training, the load_dataset function guarantee that only one local process can concurrently
  # download the dataset.
  if data_args.dataset_name is not None:
    # Downloading and loading a dataset from the hub.
    datasets = load_dataset(data_args.dataset_name, data_args.dataset_config_name, cache_dir=model_args.cache_dir)
    if "validation" not in datasets.keys():
      datasets["validation"] = load_dataset(
          data_args.dataset_name,
          data_args.dataset_config_name,
          split=f"train[:{data_args.validation_split_percentage}%]",
          cache_dir=model_args.cache_dir,
      )
      datasets["train"] = load_dataset(
          data_args.dataset_name,
          data_args.dataset_config_name,
          split=f"train[{data_args.validation_split_percentage}%:]",
          cache_dir=model_args.cache_dir,
      )
    # datasets_2 = load_dataset("wikitext", "wikitext-103-raw-v1", cache_dir=model_args.cache_dir)
    if data_args.dataset2_name is not None:
      datasets_2 = load_dataset(data_args.dataset2_name, data_args.dataset2_config_name, cache_dir=model_args.cache_dir)
      for k in datasets.keys():
        datasets[k] = concatenate_datasets([datasets[k], datasets_2[k]])
  else:
    data_files = {}
    if data_args.train_file is not None:
      data_files["train"] = data_args.train_file
    if data_args.validation_file is not None:
      data_files["validation"] = data_args.validation_file
    extension = data_args.train_file.split(".")[-1]
    if extension == "txt":
      extension = "text"
    datasets = load_dataset(extension, data_files=data_files, cache_dir=model_args.cache_dir)
  # See more about loading any type of standard or custom dataset (from files, python dict, pandas DataFrame, etc) at
  # https://huggingface.co/docs/datasets/loading_datasets.html.

  # XXX: inject patch, reconstruct later
  if datasets.__class__.__name__ == 'DatasetDict':
    setattr(datasets.__class__, 'map', newDatasetDict.map)

  # Load pretrained model and tokenizer
  #
  # Distributed training:
  # The .from_pretrained methods guarantee that only one local process can concurrently
  # download model & vocab.
  config_kwargs = {
      "cache_dir": model_args.cache_dir,
      "revision": model_args.model_revision,
      "use_auth_token": True if model_args.use_auth_token else None,
  }
  if model_args.config_name:
    config = AutoConfig.from_pretrained(model_args.config_name, **config_kwargs)
  elif model_args.model_name_or_path:
    config = AutoConfig.from_pretrained(model_args.model_name_or_path, **config_kwargs)
  else:
    config = CONFIG_MAPPING[model_args.model_type]()
    logger.warning("You are instantiating a new config instance from scratch.")

  tokenizer_kwargs = {
      "cache_dir": model_args.cache_dir,
      "use_fast": model_args.use_fast_tokenizer,
      "revision": model_args.model_revision,
      "use_auth_token": True if model_args.use_auth_token else None,
      "model_input_names": ['input_ids', 'token_type_ids', 'attention_mask', 'segment_ids']
  }
  if model_args.tokenizer_name:
    tokenizer = AutoTokenizer.from_pretrained(model_args.tokenizer_name, **tokenizer_kwargs)
  elif model_args.model_name_or_path:
    tokenizer = AutoTokenizer.from_pretrained(model_args.model_name_or_path, **tokenizer_kwargs)
  else:
    raise ValueError(
        "You are instantiating a new tokenizer from scratch. This is not supported by this script."
        "You can do it from another script, save it, and load it from here, using --tokenizer_name."
    )

  # XXX: inject patch, reconstruct later
  setattr(tokenizer.__class__, '_pad', newPreTrainedTokenizerBase._pad)

  if model_args.model_name_or_path:
    model = PoNetForPreTraining.from_pretrained(
        model_args.model_name_or_path,
        from_tf=bool(".ckpt" in model_args.model_name_or_path),
        config=config,
        cache_dir=model_args.cache_dir,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )
  else:
    logger.info("Training new model from scratch")
    model = PoNetForPreTraining(config)

  model.resize_token_embeddings(len(tokenizer))

  # Preprocessing the datasets.
  # First we tokenize all the texts.
  if training_args.do_train:
    column_names = datasets["train"].column_names
  else:
    column_names = datasets["validation"].column_names
  text_column_name = "text" if "text" in column_names else column_names[0]

  if data_args.max_seq_length is None:
    max_seq_length = tokenizer.model_max_length
    if max_seq_length > 1024:
      logger.warning(
          f"The tokenizer picked seems to have a very large `model_max_length` ({tokenizer.model_max_length}). "
          "Picking 1024 instead. You can change that default value by passing --max_seq_length xxx."
      )
      max_seq_length = 1024
  else:
    if data_args.max_seq_length > tokenizer.model_max_length:
      logger.warning(
          f"The max_seq_length passed ({data_args.max_seq_length}) is larger than the maximum length for the"
          f"model ({tokenizer.model_max_length}). Using max_seq_length={tokenizer.model_max_length}."
      )
    max_seq_length = min(data_args.max_seq_length, tokenizer.model_max_length)

  if data_args.line_by_line:
    # When using line_by_line, we just tokenize each nonempty line.
    padding = "max_length" if data_args.pad_to_max_length else False

    def tokenize_function(examples):
      # Remove empty lines
      examples["text"] = [line for line in examples["text"] if len(line) > 0 and not line.isspace()]
      return tokenizer(
          examples["text"],
          padding=padding,
          truncation=True,
          max_length=max_seq_length,
          add_special_tokens=False,
          # We use this option because DataCollatorForLanguageModeling (see below) is more efficient when it
          # receives the `special_tokens_mask`.
          return_special_tokens_mask=True,
      )

    tokenized_datasets = datasets.map(
        tokenize_function,
        batched=True,
        num_proc=data_args.preprocessing_num_workers,
        remove_columns=[text_column_name],
        load_from_cache_file=not data_args.overwrite_cache,
    )
  else:
    # Otherwise, we tokenize every text, then concatenate them together before splitting them in smaller parts.
    # We use `return_special_tokens_mask=True` because DataCollatorForLanguageModeling (see below) is more
    # efficient when it receives the `special_tokens_mask`.
    def tokenize_function(examples):
      return tokenizer(examples[text_column_name], return_special_tokens_mask=True, add_special_tokens=False)

    tokenized_datasets = datasets.map(
        tokenize_function,
        batched=True,
        num_proc=data_args.preprocessing_num_workers,
        remove_columns=column_names,
        load_from_cache_file=not data_args.overwrite_cache,
        new_fingerprints={'train': 'f100a6a7741b77ef', 'validation': 'f815a2392c2825cb'}
    )

    # Main data processing function that will concatenate all texts from our dataset and generate chunks of
    # max_seq_length.
    def group_texts(examples):
      """Creates examples for a single document."""
      results = {k:[] for k in examples.keys()}
      results["next_sentence_label"] = []
      results["segment_ids"] = []
      for _ in range(data_args.dupe_factor):
        # Account for special tokens
        max_num_tokens = max_seq_length - tokenizer.num_special_tokens_to_add(pair=True)
        short_seq_prob = 0.1
        fk = 'input_ids'

        # We *usually* want to fill up the entire sequence since we are padding
        # to `block_size` anyways, so short sequences are generally wasted
        # computation. However, we *sometimes*
        # (i.e., short_seq_prob == 0.1 == 10% of the time) want to use shorter
        # sequences to minimize the mismatch between pretraining and fine-tuning.
        # The `target_seq_length` is just a rough target however, whereas
        # `block_size` is a hard limit.
        target_seq_length = max_num_tokens
        if random.random() < short_seq_prob:
          target_seq_length = random.randint(2, max_num_tokens)

        # get_all_chunk
        total_chunk = []
        current_chunk = []  # a buffer stored current working segments
        current_length = 0
        i = 0
        while i < len(examples[fk]):
          segment = examples[fk][i]  # get a segment
          if not segment:
            i += 1
            continue
          current_chunk.append(examples['input_ids'][i])  # add a segment to current chunk
          current_length += len(segment)  # overall token length
          # if current length goes to the target length or reaches the end of file, start building token a and b
          if i == len(examples[fk]) - 1 or current_length >= target_seq_length:
            if current_chunk:
              total_chunk.append(current_chunk)

            current_chunk = []  # clear current chunk
            current_length = 0  # reset current text length
          i += 1  # go to next line

        # We DON'T just concatenate all of the tokens from a document into a long
        # sequence and choose an arbitrary split point because this would make the
        # next sentence prediction task too easy. Instead, we split the input into
        # segments "A" and "B" based on the actual "sentences" provided by the user
        # input.
        current_chunk = []  # a buffer stored current working segments
        current_length = 0
        i = 0
        chunk_id = -1
        while i < len(examples[fk]):
          segment = examples[fk][i]  # get a segment
          if not segment:
            i += 1
            continue
          current_chunk.append(examples['input_ids'][i])  # add a segment to current chunk
          current_length += len(segment)  # overall token length
          # if current length goes to the target length or reaches the end of file, start building token a and b
          if i == len(examples[fk]) - 1 or current_length >= target_seq_length:
            if current_chunk:
              chunk_id += 1
              # `a_end` is how many segments from `current_chunk` go into the `A` (first) sentence.
              a_end = 1
              # if current chunk has more than 2 sentences, pick part of it `A` (first) sentence
              if len(current_chunk) >= 2:
                a_end = random.randint(1, len(current_chunk) - 1)
              # token a
              tokens_a = []
              a_segment_ids = []
              for j in range(a_end):
                tokens_a.extend(current_chunk[j])
                a_segment_ids.extend([j] * len(current_chunk[j]))

              # token b
              tokens_b = []
              b_segment_ids = []
              for j in range(a_end, len(current_chunk)):
                tokens_b.extend(current_chunk[j])
                b_segment_ids.extend([j] * len(current_chunk[j]))

              if len(tokens_a) == 0 or len(tokens_b) == 0:
                continue

              rdn = random.random()
              if rdn < 1/3:
                is_next = 1
                tokens_a, tokens_b = tokens_b, tokens_a
                a_segment_ids, b_segment_ids = b_segment_ids, a_segment_ids
              elif rdn < 2/3 and len(total_chunk) > 1:
                is_next = 2
                while True:
                  rid = random.randint(0, len(total_chunk)-1)
                  if rid != chunk_id:
                    break
                another_chunk = total_chunk[rid]
                tokens_b = sum(another_chunk, [])
                b_segment_ids = sum([[acid]*len(ac) for acid, ac in enumerate(another_chunk)], [])
              else:
                is_next = 0

              def truncate_seq_pair(tokens_a, tokens_b, max_num_tokens, a_segment_ids, b_segment_ids):
                """Truncates a pair of sequences to a maximum sequence length."""
                while True:
                  total_length = len(tokens_a) + len(tokens_b)
                  if total_length <= max_num_tokens:
                    break
                  trunc_tokens = tokens_a if len(tokens_a) > len(tokens_b) else tokens_b
                  trunc_segment_ids = a_segment_ids if len(a_segment_ids) > len(b_segment_ids) else b_segment_ids
                  assert len(trunc_tokens) >= 1
                  # We want to sometimes truncate from the front and sometimes from the
                  # back to add more randomness and avoid biases.
                  if random.random() < 0.5:
                    del trunc_tokens[0]
                    del trunc_segment_ids[0]
                  else:
                    trunc_tokens.pop()
                    trunc_segment_ids.pop()

              truncate_seq_pair(tokens_a, tokens_b, max_num_tokens, a_segment_ids, b_segment_ids)
              # add special tokens
              input_ids = tokenizer.build_inputs_with_special_tokens(tokens_a, tokens_b)
              # add token type ids, 0 for sentence a, 1 for sentence b
              token_type_ids = tokenizer.create_token_type_ids_from_sequences(tokens_a, tokens_b)
              attention_mask = [1] * len(input_ids)
              assert len(tokens_a) >= 1
              assert len(tokens_b) >= 1

              results["input_ids"].append(input_ids)
              results["token_type_ids"].append(token_type_ids)
              results["attention_mask"].append(attention_mask)
              results["next_sentence_label"].append(is_next)
              results["special_tokens_mask"].append([1] + [0] * len(tokens_a) + [1] + [0] * len(tokens_b) + [1])

              a_segment_ids = [asi-a_segment_ids[0]+1 for asi in a_segment_ids]
              b_segment_ids = [bsi-b_segment_ids[0]+a_segment_ids[-1]+2 for bsi in b_segment_ids]
              results["segment_ids"].append([0] + a_segment_ids + [a_segment_ids[-1]+1] + b_segment_ids + [b_segment_ids[-1]+1])
            current_chunk = []  # clear current chunk
            current_length = 0  # reset current text length
          i += 1  # go to next line
      return results

    # Note that with `batched=True`, this map processes 1,000 texts together, so group_texts throws away a
    # remainder for each of those groups of 1,000 texts. You can adjust that batch_size here but a higher value
    # might be slower to preprocess.
    #
    # To speed up this part, we use multiprocessing. See the documentation of the map method for more information:
    # https://huggingface.co/docs/datasets/package_reference/main_classes.html#datasets.Dataset.map
    ori_dupe_factor = data_args.dupe_factor
    data_args.dupe_factor = 1
    tokenized_datasets["validation"] = tokenized_datasets["validation"].map(
        group_texts,
        batched=True,
        num_proc=data_args.preprocessing_num_workers,
        load_from_cache_file=not data_args.overwrite_cache,
        new_fingerprint="g5888790b41eabcb"
    )
    data_args.dupe_factor = ori_dupe_factor
    tokenized_datasets["train"] = tokenized_datasets["train"].map(
        group_texts,
        batched=True,
        num_proc=data_args.preprocessing_num_workers,
        load_from_cache_file=not data_args.overwrite_cache,
        new_fingerprint="ed9f003830c2481d"
    )

  if training_args.do_train:
    if "train" not in tokenized_datasets:
      raise ValueError("--do_train requires a train dataset")
    train_dataset = tokenized_datasets["train"]
    if data_args.max_train_samples is not None:
      train_dataset = train_dataset.select(range(data_args.max_train_samples))

  if training_args.do_eval:
    if "validation" not in tokenized_datasets:
      raise ValueError("--do_eval requires a validation dataset")
    eval_dataset = tokenized_datasets["validation"]
    if data_args.max_eval_samples is not None:
      eval_dataset = eval_dataset.select(range(data_args.max_eval_samples))

  # Data collator
  # This one will take care of randomly masking the tokens.
  pad_to_multiple_of_8 = data_args.line_by_line and training_args.fp16 and not data_args.pad_to_max_length
  data_collator = DataCollatorForLanguageModeling(
      tokenizer=tokenizer,
      mlm_probability=data_args.mlm_probability,
      pad_to_multiple_of=8 if pad_to_multiple_of_8 else None,
  )

  def compute_metrics(p: EvalPrediction):
    p.predictions[2][p.predictions[2]==-100] = -1
    out = {
      "mlm_loss": p.predictions[0].mean().item(),
      "sop_loss": p.predictions[1].mean().item(),
      "mlm_accuracy": ((p.predictions[2] == p.label_ids[0]).astype(np.float32).sum()/(p.label_ids[0] != -100).astype(np.float32).sum()).item(),
      "sop_accuracy": (p.predictions[3] == p.label_ids[1]).astype(np.float32).mean().item(),
    }
    return out

  # Initialize our Trainer
  trainer = Trainer(
      model=model,
      args=training_args,
      train_dataset=train_dataset if training_args.do_train else None,
      eval_dataset=eval_dataset if training_args.do_eval else None,
      compute_metrics=compute_metrics,
      tokenizer=tokenizer,
      data_collator=data_collator,
  )

  # Training
  if training_args.do_train:
    checkpoint = None
    if training_args.resume_from_checkpoint is not None:
      checkpoint = training_args.resume_from_checkpoint
    elif last_checkpoint is not None:
      checkpoint = last_checkpoint
    train_result = trainer.train(resume_from_checkpoint=checkpoint)
    trainer.save_model()  # Saves the tokenizer too for easy upload
    metrics = train_result.metrics

    max_train_samples = (
        data_args.max_train_samples if data_args.max_train_samples is not None else len(train_dataset)
    )
    metrics["train_samples"] = min(max_train_samples, len(train_dataset))

    trainer.log_metrics("train", metrics)
    trainer.save_metrics("train", metrics)
    trainer.save_state()

  # Evaluation
  if training_args.do_eval:
    logger.info("*** Evaluate ***")

    metrics = trainer.evaluate()

    max_eval_samples = data_args.max_eval_samples if data_args.max_eval_samples is not None else len(eval_dataset)
    metrics["eval_samples"] = min(max_eval_samples, len(eval_dataset))
    perplexity = math.exp(metrics["eval_loss"])
    metrics["perplexity"] = perplexity

    trainer.log_metrics("eval", metrics)
    trainer.save_metrics("eval", metrics)

  if training_args.push_to_hub:
    kwargs = {"finetuned_from": model_args.model_name_or_path, "tags": "fill-mask"}
    if data_args.dataset_name is not None:
      kwargs["dataset_tags"] = data_args.dataset_name
      if data_args.dataset_config_name is not None:
        kwargs["dataset_args"] = data_args.dataset_config_name
        kwargs["dataset"] = f"{data_args.dataset_name} {data_args.dataset_config_name}"
      else:
        kwargs["dataset"] = data_args.dataset_name

    trainer.push_to_hub(**kwargs)


def _mp_fn(index):
  # For xla_spawn (TPUs)
  main()


if __name__ == "__main__":
  main()
