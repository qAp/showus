# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/showus.ipynb (unless otherwise specified).

__all__ = ['load_train_meta', 'load_papers', 'load_sample_text', 'clean_training_text', 'shorten_sentences',
           'find_sublist', 'get_ner_classlabel', 'tag_sentence', 'extract_sentences', 'get_paper_ner_data',
           'get_ner_data', 'write_ner_json', 'load_ner_datasets', 'batched_write_ner_json', 'create_tokenizer',
           'tokenize_and_align_labels', 'jaccard_similarity', 'remove_nonoriginal_outputs', 'compute_metrics',
           'get_ner_inference_data', 'ner_predict', 'batched_ner_predict', 'get_paper_dataset_labels',
           'create_knowledge_bank', 'literal_match', 'combine_matching_and_model', 'filter_dataset_labels']

# Cell
import os, shutil, time
from tqdm import tqdm
from pathlib import Path
import itertools
from functools import partial
import re
import json
import random
import numpy as np
import pandas as pd
import torch
import transformers, seqeval
from transformers import AutoTokenizer, DataCollatorForTokenClassification
from transformers import AutoModelForTokenClassification
from transformers import TrainingArguments, Trainer
from datasets import load_dataset, ClassLabel, load_metric

import matplotlib.pyplot as plt

# Cell
Path.ls = lambda pth: list(pth.iterdir())

# Cell
def load_train_meta(pth, group_id=True):
    df = pd.read_csv(pth)
    if group_id:
        df = df.groupby('Id').agg({'pub_title': 'first', 'dataset_title': '|'.join,
                                   'dataset_label': '|'.join, 'cleaned_label': '|'.join}).reset_index()
    return df

# Cell
def load_papers(dir_json, paper_ids):
    '''
    Load papers into a dictionary.

    `papers`:
        {''}
    '''

    papers = {}
    for paper_id in paper_ids:
        with open(f'{dir_json}/{paper_id}.json', 'r') as f:
            paper = json.load(f)
            papers[paper_id] = paper
    return papers

# Cell
def load_sample_text(jpth):
    sections = json.loads(jpth.read_text())
    text = '\n'.join(section['text'] for section in sections)
    return text

# Cell
def clean_training_text(txt, lower=False, total_clean=False):
    """
    similar to the default clean_text function but without lowercasing.
    """
    txt = str(txt).lower() if lower else str(txt)
    txt = re.sub('[^A-Za-z0-9]+', ' ', txt).strip()
    if total_clean:
        txt = re.sub(' +', ' ', txt)
    return txt

# Cell
def shorten_sentences(sentences, max_length=64, overlap=20):
    '''
    Args:
        sentences (list): List of sentences.
        max_length (int): Maximum number of words allowed for each sentence.
        overlap (int): If a sentence exceeds `max_length`, we split it to multiple sentences with
            this amount of overlapping.
    '''
    short_sentences = []
    for sentence in sentences:
        words = sentence.split()
        if len(words) > max_length:
            for p in range(0, len(words), max_length - overlap):
                short_sentences.append(' '.join(words[p:p+max_length]))
        else:
            short_sentences.append(sentence)
    return short_sentences

# Cell
def find_sublist(big_list, small_list):
    all_positions = []
    for i in range(len(big_list) - len(small_list) + 1):
        if small_list == big_list[i:i+len(small_list)]:
            all_positions.append(i)

    return all_positions

# Cell
def get_ner_classlabel():
    '''
    Labels for named entity recognition.
        'O': Token not part of a phrase that mentions a dataset.
        'I': Intermediate token of a phrase mentioning a dataset.
        'B': First token of a phrase mentioning a dataset.
    '''
    return ClassLabel(names=['O', 'I', 'B'])

# Cell
def tag_sentence(sentence, labels, classlabel=None):
    '''
    requirement: both sentence and labels are already cleaned
    '''
    sentence_words = sentence.split()

    if labels is not None and any(re.findall(f'\\b{label}\\b', sentence)
                                  for label in labels): # positive sample
        nes = [classlabel.str2int('O')] * len(sentence_words)
        for label in labels:
            label_words = label.split()

            all_pos = find_sublist(sentence_words, label_words)
            for pos in all_pos:
                nes[pos] = classlabel.str2int('B')
                for i in range(pos+1, pos+len(label_words)):
                    nes[i] = classlabel.str2int('I')

        return True, list(zip(sentence_words, nes))

    else: # negative sample
        nes = [classlabel.str2int('O')] * len(sentence_words)
        return False, list(zip(sentence_words, nes))

# Cell
def extract_sentences(paper, sentence_definition='sentence'):
    if sentence_definition == 'sentence':
        sentences = set(clean_training_text(sentence)
                        for sec in paper for sentence in sec['text'].split('.') if sec['text'])
    elif sentence_definition == 'section':
        sentences = set(clean_training_text(sec['section_title'] + '\n' + sec['text'])
                        for sec in paper if sec['text'])
    return sentences

# Cell
def get_paper_ner_data(paper, labels, classlabel=None,
                       sentence_definition='sentence', max_length=64, overlap=20,
                       neg_keywords=['data', 'study']):
    '''
    Get NER data for a single paper.
    '''
    labels = [clean_training_text(label) for label in labels]
    sentences = extract_sentences(paper, sentence_definition=sentence_definition)
    sentences = shorten_sentences(sentences, max_length=max_length, overlap=overlap)
    sentences = [sentence for sentence in sentences if len(sentence) > 10] # only accept sentences with length > 10 chars

    cnt_pos, cnt_neg, ner_data = 0, 0, []
    for sentence in sentences:
        is_positive, tags = tag_sentence(sentence, labels, classlabel=classlabel)
        if is_positive:
            cnt_pos += 1
            ner_data.append(tags)
        elif neg_keywords:
            if any(word in sentence.lower() for word in neg_keywords):
                ner_data.append(tags)
                cnt_neg += 1
        else:
            ner_data.append(tags)
            cnt_neg += 1

    return cnt_pos, cnt_neg, ner_data

# Cell
def get_ner_data(papers, df=None, classlabel=None, shuffle=True,
                 sentence_definition='sentence', max_length=64, overlap=20,
                 neg_keywords=['study', 'data']):
    '''
    Get NER data for a list of papers.

    Args:
        papers (dict): Like that returned by `load_papers`.
        df (pd.DataFrame): Competition's train.csv or a subset of it.
    Returns:
        cnt_pos (int): Number of samples (or 'sentences') that are tagged or partly
            tagged as datasets.
        cnt_neg (int): Number of samples (or 'sentences') that are not tagged
            or partly tagged as datasets.
        ner_data (list): List of samples, or 'sentences'. Each element is of the form:
            [('There', 0), ('has', 0), ('been', 0), ...]
    '''
    cnt_pos, cnt_neg = 0, 0
    ner_data = []

    tqdm._instances.clear()
    pbar = tqdm(total=len(df))
    for i, id, dataset_label in df[['Id', 'dataset_label']].itertuples():
        paper = papers[id]
        labels = dataset_label.split('|')

        cnt_pos_, cnt_neg_, ner_data_ = get_paper_ner_data(
            paper, labels, classlabel=classlabel,
            sentence_definition=sentence_definition, max_length=max_length, overlap=overlap,
            neg_keywords=neg_keywords)
        cnt_pos += cnt_pos_
        cnt_neg += cnt_neg_
        ner_data.extend(ner_data_)

        pbar.update(1)
        pbar.set_description(f"Training data size: {cnt_pos} positives + {cnt_neg} negatives")

    if shuffle:
        random.shuffle(ner_data)
    return cnt_pos, cnt_neg, ner_data

# Cell
def write_ner_json(ner_data, pth=Path('train_ner.json'), mode='w'):
    '''
    Save NER data to json file.
    '''
    with open(pth, mode=mode) as f:
        for row in ner_data:
            words, nes = list(zip(*row))
            row_json = {'tokens' : words, 'ner_tags' : nes}
            json.dump(row_json, f)
            f.write('\n')

# Cell
def load_ner_datasets(data_files=None):
    '''
    Load NER data in json files to a `datasets` object.  In addition,
    Append the NER ClassLabel for the `ner_tags` feature.
    '''
    datasets = load_dataset('json', data_files=data_files)
    classlabel = get_ner_classlabel()
    for split, dataset in datasets.items():
        dataset.features['ner_tags'].feature = classlabel
    return datasets

# Cell

def batched_write_ner_json(papers, df, pth=Path('train_ner.json'), batch_size=4_000,
                           classlabel=get_ner_classlabel(),
                           sentence_definition='sentence', max_length=64, overlap=20,
                           neg_keywords=['study', 'data']):

    for i in range(0, len(df), batch_size):
        print(f'Batch {i // batch_size}...', end='')
        t0 = time.time()
        cnt_pos, cnt_neg, ner_data = get_ner_data(papers, df.iloc[i:i+batch_size],
                                                  classlabel=classlabel,
                                                  sentence_definition=sentence_definition,
                                                  max_length=max_length, overlap=overlap,
                                                  neg_keywords=neg_keywords)
        write_ner_json(ner_data, pth=pth, mode='w' if i == 0 else 'a')
        print(f'done in {(time.time() - t0) / 60} mins.')

# Cell
def create_tokenizer(model_checkpoint='distilbert-base-cased'):
    tokenizer = AutoTokenizer.from_pretrained(model_checkpoint)
    assert isinstance(tokenizer, transformers.PreTrainedTokenizerFast)
    return tokenizer

# Cell
def tokenize_and_align_labels(examples, tokenizer=None, label_all_tokens=True):
    '''
    Adds a new field called 'labels' that are the NER tags to the tokenized input.

    Args:
        tokenizer (transformers.AutoTokenizer): Tokenizer.
        examples (datasets.arrow_dataset.Dataset): Dataset.
        label_all_tokens (bool): If True, all sub-tokens are given the same tag as the
            first sub-token, otherwise all but the first sub-token are given the tag
            -100.
    '''
    tokenized_inputs = tokenizer(examples["tokens"], truncation=True, is_split_into_words=True)
    labels = []
    word_ids_all = []
    for i, label in enumerate(examples["ner_tags"]):
        word_ids = tokenized_inputs.word_ids(batch_index=i)
        previous_word_idx = None
        label_ids = []
        for word_idx in word_ids:
            if word_idx is None:
                label_ids.append(-100)
            elif word_idx != previous_word_idx:
                label_ids.append(label[word_idx])
            else:
                label_ids.append(label[word_idx] if label_all_tokens else -100)
            previous_word_idx = word_idx

        labels.append(label_ids)
        word_ids_all.append(word_ids)

    tokenized_inputs["labels"] = labels
    tokenized_inputs['word_ids'] = word_ids_all
    return tokenized_inputs

# Cell
def jaccard_similarity(s1, s2):
    l1 = set(s1.split(" "))
    l2 = set(s2.split(" "))
    intersection = len(list(l1.intersection(l2)))
    union = (len(l1) + len(l2)) - intersection
    return float(intersection) / union

# Cell
def remove_nonoriginal_outputs(outputs, word_ids):
    '''
    Remove elements that correspond to special tokens or subtokens,
    retaining only those elements that correspond to a word in original
    text.

    Args:
        outputs (np.array):

    Returns:
        outputs (list)
    '''
    assert len(outputs) == len(word_ids)
    idxs = [[word_id.index(i) for i in set(word_id) if i is not None]
            for word_id in word_ids]
    outputs = [output[idx].tolist() for output, idx in zip(outputs, idxs)]
    for output in outputs:
        assert -100 not in output
    return outputs

# Cell
def compute_metrics(p, metric=None, word_ids=None, label_list=None):
    '''
    1. Remove predicted and ground-truth class ids of special and sub tokens.
    2. Convert class ids to class labels. (int ---> str)
    3. Compute metric.

    Args:
        p (tuple): 2-tuple consisting of model prediction and ground-truth
            labels.  These will contain elements corresponding to special
            tokens and sub-tokens.
        word_ids (list): Word IDs from the tokenizer's output, indicating
            which original word each sub-token belongs to.
    '''
    predictions, label_ids = p
    predictions = predictions.argmax(axis=2)

    true_predictions = remove_nonoriginal_outputs(predictions, word_ids)
    true_label_ids = remove_nonoriginal_outputs(label_ids, word_ids)

    true_predictions = [[label_list[p] for p in pred] for pred in true_predictions]
    true_labels = [[label_list[i] for i in label_id] for label_id in true_label_ids]

    results = metric.compute(predictions=true_predictions, references=true_labels)
    return {
        "precision": results["overall_precision"],
        "recall": results["overall_recall"],
        "f1": results["overall_f1"],
        "accuracy": results["overall_accuracy"],
    }

# Cell
def get_ner_inference_data(papers, sample_submission, classlabel=None,
                           sentence_definition='sentence', max_length=64, overlap=20,
                           min_length=10, contains_keywords=['data', 'study']):
    '''
    Args:
        papers (dict): Each list in this dictionary consists of the section of a paper.
        sample_submission (pd.DataFrame): Competition 'sample_submission.csv'.
    Returns:
        test_rows (list): Each list in this list is of the form:
             [('goat', 0), ('win', 0), ...] and represents a sentence.
        paper_length (list): Number of sentences in each paper.
    '''
    test_rows = []
    paper_length = []

    for paper_id in sample_submission['Id']:
        paper = papers[paper_id]

        sentences = extract_sentences(paper, sentence_definition=sentence_definition)
        sentences = shorten_sentences(sentences, max_length=max_length, overlap=overlap)

        if min_length > 0:
            sentences = [sentence for sentence in sentences if len(sentence) > min_length]

        if contains_keywords is not None:
            sentences = [sentence for sentence in sentences
                         if any(word in sentence.lower() for word in contains_keywords)]

        for sentence in sentences:
            sentence_words = sentence.split()
            dummy_tags = [classlabel.str2int('O')]*len(sentence_words)
            test_rows.append(list(zip(sentence_words, dummy_tags)))

        paper_length.append(len(sentences))

    print(f'total number of "sentences": {len(test_rows)}')
    return test_rows, paper_length

# Cell
def ner_predict(pth=None, tokenizer=None, model=None, metric=None,
                per_device_train_batch_size=16, per_device_eval_batch_size=16):
    classlabel = get_ner_classlabel()
    datasets = load_ner_datasets(data_files={'test':pth})

    print('Tokenizing testset...', end='')
    t0 = time.time()
    tokenized_datasets = datasets.map(
        partial(tokenize_and_align_labels,tokenizer=tokenizer, label_all_tokens=True),
        batched=True)
    print(f'completed in {(time.time() - t0) / 60:.2f} mins.')

    print('Creating data collator...')
    data_collator = DataCollatorForTokenClassification(tokenizer)

    print('Creating (dummy) training arguments...')
    args = TrainingArguments(output_dir='test_ner', num_train_epochs=3,
                             learning_rate=2e-5, weight_decay=0.01,
                             per_device_train_batch_size=per_device_train_batch_size,
                             per_device_eval_batch_size=per_device_eval_batch_size,
                             evaluation_strategy='epoch', logging_steps=4, report_to='none',
                             save_strategy='epoch', save_total_limit=6)

    print('Creating trainer...')
    word_ids = tokenized_datasets['test']['word_ids']
    compute_metrics_ = partial(compute_metrics, metric=metric, label_list=classlabel.names, word_ids=word_ids)
    trainer = Trainer(model=model, args=args,
                      train_dataset=tokenized_datasets['test'], eval_dataset=tokenized_datasets['test'],
                      data_collator=data_collator, tokenizer=tokenizer, compute_metrics=compute_metrics_)

    print('Predicting on test samples...')
    t0 = time.time()
    predictions, label_ids, _ = trainer.predict(tokenized_datasets['test'])
    print(f'completed in {(time.time() - t0) / 60:.2f} mins.')
    print('Argmaxing...')
    t0 = time.time()
    predictions = predictions.argmax(axis=2)
    print(f'completed in {(time.time() - t0) / 60:.2f} mins.')

    print('Removing non-original outputs...', end='')
    t0 = time.time()
    predictions = remove_nonoriginal_outputs(predictions, word_ids)
    label_ids   = remove_nonoriginal_outputs(label_ids, word_ids)
    print(f'completed in {(time.time() - t0) / 60:.2f} mins.')

    return predictions, label_ids

# Cell

def batched_ner_predict(pth, tokenizer=None, model=None, metric=None,
                        batch_size=64_000,
                        per_device_train_batch_size=16, per_device_eval_batch_size=16):
    '''
    Do inference on dataset in batches.
    '''
    lines = open(pth, mode='r').readlines()

    pth_tmp = 'ner_predict_tmp.json'
    predictions, label_ids = [], []
    for ib in range(0, len(lines), batch_size):
        with open(pth_tmp, mode='w') as f:
            f.writelines(lines[ ib: ib + batch_size ])

        predictions_, label_ids_ = ner_predict(
            pth_tmp, tokenizer=tokenizer, model=model, metric=metric,
            per_device_train_batch_size=per_device_train_batch_size,
            per_device_eval_batch_size=per_device_eval_batch_size)
        predictions.extend(predictions_)
        label_ids.extend(label_ids_)
    return predictions, label_ids

# Cell
def get_paper_dataset_labels(pth, paper_length, predictions):
    '''
    Args:
        pth (Path, str): Path to json file containing NER data.  Each row is
            of form: {'tokens': ['Studying', 'human'], 'ner_tags': [0, 0, ...]}.

    Returns:
        paper_dataset_labels (list): Each element is a set consisting of labels predicted
            by the model.
    '''
    test_sentences = [json.loads(sample)['tokens'] for sample in open(pth).readlines()]

    paper_dataset_labels = [] # store all dataset labels for each publication
    for ipaper in range(len(paper_length)):
        istart = sum(paper_length[:ipaper])
        iend = istart + paper_length[ipaper]

        labels = set()
        for sentence, pred in zip(test_sentences[istart:iend], predictions[istart:iend]):
            curr_phrase = ''
            for word, tag in zip(sentence, pred):
                if tag == 'B': # start a new phrase
                    if curr_phrase:
                        labels.add(curr_phrase)
                        curr_phrase = ''
                    curr_phrase = word
                elif tag == 'I' and curr_phrase: # continue the phrase
                    curr_phrase += ' ' + word
                else: # end last phrase (if any)
                    if curr_phrase:
                        labels.add(curr_phrase)
                        curr_phrase = ''
            # check if the label is the suffix of the sentence
            if curr_phrase:
                labels.add(curr_phrase)
                curr_phrase = ''

        # record dataset labels for this publication
        paper_dataset_labels.append(labels)

    return paper_dataset_labels

# Cell
def create_knowledge_bank(pth):
    '''
    Args:
        pth (str): Path to meta data like 'train.csv', which
        needs to have columns: 'dataset_title', 'dataset_label', and 'cleaned_label'.

    Returns:
        all_labels (set): All possible strings associated with a dataset from the meta data.
    '''
    df = load_train_meta(pth, group_id=False)
    all_labels = set()
    for label_1, label_2, label_3 in df[['dataset_title', 'dataset_label', 'cleaned_label']].itertuples(index=False):
        all_labels.add(str(label_1).lower())
        all_labels.add(str(label_2).lower())
        all_labels.add(str(label_3).lower())
    return all_labels

# Cell
def literal_match(paper, all_labels):
    '''
    Args:
        paper ()
    '''
    text_1 = '. '.join(section['text'] for section in paper).lower()
    text_2 = clean_training_text(text_1, lower=True, total_clean=True)

    labels = set()
    for label in all_labels:
        if label in text_1 or label in text_2:
            labels.add(clean_training_text(label, lower=True, total_clean=True))
    return labels

# Cell
def combine_matching_and_model(literal_preds, paper_dataset_labels):
    '''
    Args:
        literal_preds (list): Each element is a set, containing predicted labels for a paper
            using literal matching.
        paper_dataset_labels (list): Each element is a set, containing predicted labels for
            a paper using trained model.
    Returns:
        filtered_dataset_labels (list): Each element is a string, containing
            labels seperated by '|'.

    Notes:
        Combine literal matching predictions and model predictions.
        Literal match predictions are appended IN FRONT of the model predictions,
        because literal matches will be kept when removing labels that are too
        similar to each other.
    '''
    all_labels = [list(literal_match) + list(model_pred)
                  for literal_match, model_pred in zip(literal_preds, paper_dataset_labels)]
    return all_labels

# Cell
def filter_dataset_labels(all_labels, max_similarity=0.75):
    '''
    When several labels for a paper are too similar, keep just one of them,
    the one that appears FIRST.

    Args:
        all_labels (list, set): Each element is a list of labels (str).

    Returns:
        filtered_dataset_labels (list): Each element is a string, containing
            labels seperated by '|'.
    '''
    filtered_dataset_labels = []

    for labels in all_labels:
        filtered = []

        for label in labels:
            label = clean_training_text(label, lower=True)
            if len(filtered) == 0 or all(jaccard_similarity(label, got_label) < max_similarity
                                         for got_label in filtered):
                filtered.append(label)

        filtered_dataset_labels.append('|'.join(filtered))
    return filtered_dataset_labels