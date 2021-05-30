# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/showus.ipynb (unless otherwise specified).

__all__ = ['load_train_meta', 'load_papers', 'load_sample_text', 'clean_training_text', 'shorten_sentences',
           'find_sublist', 'get_ner_classlabel', 'tag_sentence', 'extract_sentences', 'get_paper_ner_data',
           'get_ner_data', 'write_ner_json', 'load_ner_datasets', 'create_tokenizer', 'tokenize_and_align_labels',
           'jaccard_similarity', 'compute_metrics', 'get_ner_inference_data', 'ner_predict', 'get_bert_dataset_labels',
           'filter_bert_labels', 'create_knowledge_bank', 'literal_match', 'combine_matching_and_bert']

# Cell
import os, shutil
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
                       sentence_definition='sentence', max_length=64, overlap=20):
    '''
    Get NER data for a paper.
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
        elif any(word in sentence.lower() for word in ['data', 'study']):
            ner_data.append(tags)
            cnt_neg += 1
    return cnt_pos, cnt_neg, ner_data

# Cell
def get_ner_data(papers, df=None, classlabel=None, shuffle=True,
                 sentence_definition='sentence', max_length=64, overlap=20):
    '''
    Args:
        papers (dict): Like that returned by `load_papers`.
        df (pd.DataFrame): Competition's train.csv or a subset of it.
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
            sentence_definition=sentence_definition, max_length=max_length, overlap=overlap)
        cnt_pos += cnt_pos_
        cnt_neg += cnt_neg_
        ner_data.extend(ner_data_)

        pbar.update(1)
        pbar.set_description(f"Training data size: {cnt_pos} positives + {cnt_neg} negatives")

    if shuffle:
        random.shuffle(ner_data)
    return cnt_pos, cnt_neg, ner_data

# Cell
def write_ner_json(ner_data, pth=Path('train_ner.json')):
    with open(pth, 'w') as f:
        for row in ner_data:
            words, nes = list(zip(*row))
            row_json = {'tokens' : words, 'ner_tags' : nes}
            json.dump(row_json, f)
            f.write('\n')

# Cell
def load_ner_datasets(data_files=None):
    datasets = load_dataset('json', data_files=data_files)
    classlabel = get_ner_classlabel()
    for split, dataset in datasets.items():
        dataset.features['ner_tags'].feature = classlabel
    return datasets

# Cell
def create_tokenizer(model_checkpoint='bert-base-cased'):
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

    tokenized_inputs["labels"] = labels
    return tokenized_inputs

# Cell
def jaccard_similarity(s1, s2):
    l1 = set(s1.split(" "))
    l2 = set(s2.split(" "))
    intersection = len(list(l1.intersection(l2)))
    union = (len(l1) + len(l2)) - intersection
    return float(intersection) / union

# Cell
def compute_metrics(p, metric=None, label_list=None):
    predictions, labels = p
    predictions = np.argmax(predictions, axis=2)

    # Remove ignored index (special tokens)
    true_predictions = [
        [label_list[p] for (p, l) in zip(prediction, label) if l != -100]
        for prediction, label in zip(predictions, labels)
    ]
    true_labels = [
        [label_list[l] for (p, l) in zip(prediction, label) if l != -100]
        for prediction, label in zip(predictions, labels)
    ]

    results = metric.compute(predictions=true_predictions, references=true_labels)
    return {
        "precision": results["overall_precision"],
        "recall": results["overall_recall"],
        "f1": results["overall_f1"],
        "accuracy": results["overall_accuracy"],
    }

# Cell
def get_ner_inference_data(papers, sample_submission, classlabel=None,
                           sentence_definition='sentence', max_length=64, overlap=20):
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
        sentences = [sentence for sentence in sentences if len(sentence) > 10]
        sentences = [sentence for sentence in sentences if any(word in sentence.lower() for word in ['data', 'study'])]

        for sentence in sentences:
            sentence_words = sentence.split()
            dummy_tags = [classlabel.str2int('O')]*len(sentence_words)
            test_rows.append(list(zip(sentence_words, dummy_tags)))

        paper_length.append(len(sentences))

    print(f'total number of sentences: {len(test_rows)}')
    return test_rows, paper_length

# Cell
def ner_predict(test_rows, tokenizer=None, model=None, metric=None):
    classlabel = get_ner_classlabel()

    write_ner_json(test_rows, pth='test_ner.json')
    datasets = load_ner_datasets(data_files={'test':'test_ner.json'})
    print('Tokenizing testset...')
    tokenized_datasets = datasets.map(
        partial(tokenize_and_align_labels,tokenizer=tokenizer, label_all_tokens=True),
        batched=True)

    print('Creating data collator...')
    data_collator = DataCollatorForTokenClassification(tokenizer)
    print('Creating (dummy) training arguments...')
    args = TrainingArguments(output_dir='test_ner', num_train_epochs=3,
                             learning_rate=2e-5, weight_decay=0.01,
                             per_device_train_batch_size=16, per_device_eval_batch_size=16,
                             evaluation_strategy='epoch', logging_steps=4, report_to='none',
                             save_strategy='epoch', save_total_limit=6)

    print('Creating trainer...')
    trainer = Trainer(model=model, args=args,
                      train_dataset=tokenized_datasets['test'], eval_dataset=tokenized_datasets['test'],
                      data_collator=data_collator, tokenizer=tokenizer,
                      compute_metrics=partial(compute_metrics, metric=metric, label_list=classlabel.names))

    print('Predicting on test samples...')
    predictions, label_ids, _ = trainer.predict(tokenized_datasets['test'])
    predictions = predictions.argmax(axis=2)
    true_predictions = [
        [p for p, i in zip(prediction, label_id) if i != -100]
        for prediction, label_id in zip(predictions, label_ids)]

    return true_predictions

# Cell
def get_bert_dataset_labels(test_rows, paper_length, bert_outputs, classlabel=None):
    '''
    Returns:
        bert_dataset_labels (list): Each element is a set consisting of labels predicted
            by the model.
    '''
    test_sentences = [list(zip(*row))[0] for row in test_rows]
#     test_sentences = [row['tokens'] for row in test_rows]

    bert_dataset_labels = [] # store all dataset labels for each publication

    for length in paper_length:
        labels = set()
        for sentence, pred in zip(test_sentences[:length], bert_outputs[:length]):
            curr_phrase = ''
            for word, tag in zip(sentence, pred):
                if tag == classlabel.str2int('B'): # start a new phrase
                    if curr_phrase:
                        labels.add(curr_phrase)
                        curr_phrase = ''
                    curr_phrase = word
                elif tag == classlabel.str2int('I') and curr_phrase: # continue the phrase
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
        bert_dataset_labels.append(labels)

        del test_sentences[:length], bert_outputs[:length]

    return bert_dataset_labels

# Cell
def filter_bert_labels(bert_dataset_labels):
    '''
    When several labels for a paper are too similar, keep just one of them.
    '''
    filtered_bert_labels = []

    for labels in bert_dataset_labels:
        filtered = []

        for label in sorted(labels, key=len):
            label = clean_training_text(label, lower=True)
            if len(filtered) == 0 or all(jaccard_similarity(label, got_label) < 0.75 for got_label in filtered):
                filtered.append(label)

        filtered_bert_labels.append('|'.join(filtered))
    return filtered_bert_labels

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
def combine_matching_and_bert(literal_preds, filtererd_bert_labels):
    '''
    For a given sentence, if there's a literal match, use that as the final
    prediction for the sentence.  If there isn't a literal match,
    use what the model predicts.
    '''
    final_predictions = []
    for literal_match, bert_pred in zip(literal_preds, filtered_bert_labels):
        if literal_match:
            final_predictions.append(literal_match)
        else:
            final_predictions.append(bert_pred)
    return final_predictions