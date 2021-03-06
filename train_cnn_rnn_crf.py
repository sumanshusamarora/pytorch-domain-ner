"""
Training code
"""
import os
import ast
import argparse
import numpy as np
import matplotlib.pyplot as plt
from sklearn.metrics import (
    precision_score,
    accuracy_score,
    recall_score,
    f1_score,
    classification_report,
)
from sklearn.utils.class_weight import compute_class_weight
import torch
import torch.nn.functional as F
import torch.nn as nn
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader
from torchcrf import CRF
from torchnlp.datasets.dataset import Dataset
from torchnlp.encoders import LabelEncoder
from torchnlp.encoders.text import pad_tensor
from torchnlp.encoders.text import StaticTokenizerEncoder, CharacterEncoder
from torchnlp.word_to_vector import GloVe
import nltk
import random
import pickle
import dill
import mlflow.pytorch
import subprocess
from datetime import datetime
from pathlib import Path
import yaml
import warnings
warnings.filterwarnings('ignore')

home = str(Path.home())
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

with open("config.yml", "r") as fh:
    config = yaml.safe_load(fh)

with open("environment.yml", "r") as fh:
    conda_environment = yaml.safe_load(fh)

def load_data(fpath="data/data_ready_list.pkl"):
    """
    Loads pickle list of list as per suggested format
    :param fpath: filepath
    :return: Returns X_list (as is), X_list (lower) and y_list
    """
    with open(fpath, "rb") as in_file:
        dataset_ready = pickle.load(in_file)

    X_text_list_as_is = [[word[0] for word in tup] for tup in dataset_ready]
    X_text_list = [[word[0].lower() for word in tup] for tup in dataset_ready]
    y_ner_list = [[word[1] for word in tup] for tup in dataset_ready]

    return X_text_list_as_is, X_text_list, y_ner_list

def get_POS_tags(X_text_list):
    """
    Generates pos tags from NLTK
    :param X_text_list:
    :return: X_tags, tag_to_index dictionary
    """
    X_tags = []
    all_tags = ["<pad>"]
    for lst in X_text_list:
        postag = nltk.pos_tag([word if word.strip() != "" else "<OOS>" for word in lst])
        X_tags.append([tag[1] for tag in postag])

    _ = [
        [all_tags.append(tag) for tag in sent if tag not in all_tags] for sent in X_tags
    ]
    all_tags.append("<UNK>")
    tag_to_index = {tag: i for i, tag in enumerate(all_tags)}
    X_tags = [[tag_to_index[tag] for tag in sent] for sent in X_tags]
    return X_tags, tag_to_index


def split_test_train(
    X_text_list,
    X_text_list_as_is,
    X_tags,
    x_enriched_features,
    y_ner_list,
    split_size=0.3,
):
    """
    Splits x_text, x_tags, x_enriched and y to test and train
    :param X_text_list:
    :param X_text_list_as_is:
    :param X_tags:
    :param x_enriched_features:
    :param y_ner_list:
    :param split_size: defaults to .3
    :return: Tuples for test and train for each input list
    """
    test_index = random.choices(
        range(len(X_text_list)), k=int(split_size * len(X_text_list))
    )
    train_index = [ind for ind in range(len(X_text_list)) if ind not in test_index]

    X_text_list_train = [X_text_list[ind] for ind in train_index]
    X_text_list_test = [X_text_list[ind] for ind in test_index]

    X_text_list_as_is_train = [X_text_list_as_is[ind] for ind in train_index]
    X_text_list_as_is_test = [X_text_list_as_is[ind] for ind in test_index]

    X_tags_train = [X_tags[ind] for ind in train_index]
    X_tags_test = [X_tags[ind] for ind in test_index]

    y_ner_list_train = [y_ner_list[ind] for ind in train_index]
    y_ner_list_test = [y_ner_list[ind] for ind in test_index]

    x_enriched_features_train = x_enriched_features[train_index]
    x_enriched_features_test = x_enriched_features[test_index]

    return (
        (X_text_list_train, X_text_list_test),
        (X_text_list_as_is_train, X_text_list_as_is_test),
        (X_tags_train, X_tags_test),
        (x_enriched_features_train, x_enriched_features_test),
        (y_ner_list_train, y_ner_list_test),
        (train_index, test_index),
    )


def tokenize_sentence(X_text_list_train, X_text_list_test, max_sent_len=800):
    """
    Tokenized sentences with train data list and fits on both train + test
    :param X_text_list_train:
    :param X_text_list_test:
    :param max_sent_len: Max sentence len to pad to, defaults to 800
    :return: x_encoder, x_padded_train, x_padded_test
    """
    x_encoder = StaticTokenizerEncoder(
        sample=X_text_list_train, append_eos=False, tokenize=lambda x: x,
    )
    x_encoded_train = [x_encoder.encode(text) for text in X_text_list_train]
    x_padded_train = torch.LongTensor(
        pad_sequence(x_encoded_train, max_sent_len + 1)
    )

    x_encoded_test = [x_encoder.encode(text) for text in X_text_list_test]
    x_padded_test = torch.LongTensor(pad_sequence(x_encoded_test, max_sent_len + 1))

    if x_padded_train.shape[1] > x_padded_test.shape[1]:
        x_padded_test = torch.cat(
            (
                x_padded_test,
                torch.zeros(
                    x_padded_test.shape[0],
                    x_padded_train.shape[1] - x_padded_test.shape[1],
                ),
            ),
            dim=1,
        ).type(torch.long)

    return x_encoder, x_padded_train, x_padded_test


def tokenize_character(X_text_list_train, X_text_list_test, max_sent_len=800):
    """
    Tokenizes characters at word level
    :param X_text_list_train:
    :param X_text_list_test:
    :param max_sent_len:
    :return: x_char_encoder, x_char_padded_train, x_char_padded_test, max_word_length
    """
    X_text_list_train = [
        lst[:max_sent_len] + (max_sent_len - len(lst)) * ["<end>"]
        for lst in X_text_list_train
    ]
    X_text_list_test = [
        lst[:max_sent_len] + (max_sent_len - len(lst)) * ["<end>"]
        for lst in X_text_list_test
    ]

    x_char_encoder = CharacterEncoder(
        sample=[" ".join(sent) for sent in X_text_list_train], append_eos=False,
    )

    x_char_encoded_train = [
        [x_char_encoder.encode(char) for char in word] for word in X_text_list_train
    ]
    x_char_encoded_test = [
        [x_char_encoder.encode(char) for char in word] for word in X_text_list_test
    ]

    max_word_length = max(
        [
            max([internal.shape[0] for internal in external])
            for external in x_char_encoded_train
        ]
    )

    outer_list = []
    for lst in x_char_encoded_train:
        inner_list = []
        for ten in lst:
            res = torch.zeros(max_word_length, dtype=torch.long)
            res[: ten.shape[0]] = ten[:max_word_length]
            inner_list.append(res)
        outer_list.append(inner_list)

    x_char_padded_train = torch.stack([torch.stack(lst) for lst in outer_list])

    outer_list = []
    for lst in x_char_encoded_test:
        inner_list = []
        for ten in lst:
            res = torch.zeros(max_word_length, dtype=torch.long)
            res[: ten.shape[0]] = ten[:max_word_length]
            inner_list.append(res)
        outer_list.append(inner_list)

    x_char_padded_test = torch.stack([torch.stack(lst) for lst in outer_list])
    return x_char_encoder, x_char_padded_train, x_char_padded_test, max_word_length


def tokenize_pos_tags(X_tags, tag_to_index, max_sen_len=800):
    """
    One hot encodes pos tags
    :param X_tags:
    :param tag_to_index:
    :param max_sen_len:
    :return: One hot encoded vector
    """
    return torch.nn.functional.one_hot(
        torch.stack([pad_tensor(torch.LongTensor(lst), max_sen_len) for lst in X_tags]),
        num_classes=max(tag_to_index.values()) + 1,
    )


def encode_ner_y(y_ner_list_train, y_ner_list_test, class_count_dict, max_sent_len):
    """
    Tokenize y
    :param y_ner_list_train:
    :param y_ner_list_test:
    :param class_count_dict:
    :param max_sent_len:
    :return:
    """
    y_ner_encoder = LabelEncoder(sample=class_count_dict.keys())
    y_ner_encoded_train = [
        [y_ner_encoder.encode(label) for label in label_list]
        for label_list in y_ner_list_train
    ]
    y_ner_encoded_train = [torch.stack(tens) for tens in y_ner_encoded_train]
    y_ner_padded_train = torch.LongTensor(
        pad_sequence(y_ner_encoded_train, max_sent_len + 1)
    )

    y_ner_encoded_test = [
        [y_ner_encoder.encode(label) for label in label_list]
        for label_list in y_ner_list_test
    ]
    y_ner_encoded_test = [torch.stack(tens) for tens in y_ner_encoded_test]
    y_ner_padded_test = torch.LongTensor(
        pad_sequence(y_ner_encoded_test, max_sent_len + 1)
    )

    if y_ner_padded_train.shape[1] > y_ner_padded_test.shape[1]:
        y_ner_padded_test = torch.cat(
            (
                y_ner_padded_test,
                torch.zeros(
                    y_ner_padded_test.shape[0],
                    y_ner_padded_train.shape[1] - y_ner_padded_test.shape[1],
                ),
            ),
            dim=1,
        ).type(torch.long)

    return y_ner_encoder, y_ner_padded_train, y_ner_padded_test


def enrich_data(txt_list: list):
    """
    Generates enrichments features for each word in sequence
    :param txt_list: Text list
    :return: lists like alnum, numeric, alpha, digit, lower, title, ascii
    """
    alnum = []
    numeric = []
    alpha = []
    digit = []
    lower = []
    title = []
    ascii = []

    for document in txt_list:
        alnum.append([int(str(word).isalnum()) for word in document])
        numeric.append([int(str(word).isnumeric()) for word in document])
        alpha.append([int(str(word).isalpha()) for word in document])
        digit.append([int(str(word).isdigit()) for word in document])
        lower.append([int(str(word).islower()) for word in document])
        title.append([int(str(word).istitle()) for word in document])
        ascii.append([int(str(word).isascii()) for word in document])
    return alnum, numeric, alpha, digit, lower, title, ascii


# Sample weights
def calculate_sample_weights(y_ner_padded_train):
    """
    Calculates sample weights for each class
    :param y_ner_padded_train:
    :return: array consisting of sample weight for each class
    """
    ner_class_weights = compute_class_weight(
        "balanced",
        classes=np.unique(torch.flatten(y_ner_padded_train).numpy()),
        y=torch.flatten(y_ner_padded_train).numpy(),
    )

    return ner_class_weights


def pad_and_stack_list_of_list(
    list_of_list: list, max_sentence_len=800, pad_value=0, tensor_type=torch.FloatTensor
):
    """

    :param list_of_list: list of list of sequence
    :param max_sentence_len: defaults to 800
    :param pad_value: defaults to 0
    :param tensor_type: defaults to torch.FloatTensor
    :return: stacked tensor
    """
    padded = [
        pad_tensor(tensor_type(lst), length=max_sentence_len, padding_index=pad_value)
        for lst in list_of_list
    ]
    stacked = torch.stack(padded)
    return stacked


# Model defintion
# Build Model
class EntityExtraction(nn.Module):
    """

    """
    def __init__(
        self,
        num_classes,
        rnn_hidden_size=512,
        rnn_stack_size=2,
        rnn_bidirectional=True,
        word_embed_dim=256,
        tag_embed_dim=36,
        char_embed_dim=124,
        rnn_type="LSTM",
        rnn_embed_dim=512,
        enrich_dim=7,
        char_embedding=True,
        char_cnn_out_dim=32,
        dropout_ratio=0.3,
        class_weights=None,
        word_embedding_weights=None,
        word_embedding_freeze=True,
    ):
        """

        :param num_classes:
        :param rnn_hidden_size:
        :param rnn_stack_size:
        :param rnn_bidirectional:
        :param word_embed_dim:
        :param tag_embed_dim:
        :param char_embed_dim:
        :param rnn_type:
        :param rnn_embed_dim:
        :param enrich_dim:
        :param char_embedding:
        :param char_cnn_out_dim:
        :param dropout_ratio:
        :param class_weights:
        :param word_embedding_weights:
        :param word_embedding_freeze:
        """
        super().__init__()
        # self variables
        self.num_classes = num_classes
        self.char_embed_dim = char_embed_dim
        self.char_cnn_out_dim = char_cnn_out_dim
        self.rnn_embed_dim = rnn_embed_dim
        self.enrich_dim = enrich_dim
        self.dropout_ratio = dropout_ratio
        self.rnn_hidden_size = rnn_hidden_size
        self.rnn_stack_size = rnn_stack_size
        self.rnn_bidirectional = rnn_bidirectional
        self.class_weights = torch.FloatTensor(class_weights).to(device)
        self.tag_embed_dim = tag_embed_dim
        self.word_embedding_weights = word_embedding_weights
        self.word_embedding_freeze = word_embedding_freeze
        if self.word_embedding_weights is None:
            self.word_embed_dim = word_embed_dim
        else:
            self.word_embed_dim = word_embedding_weights.size(-1)
        # Embedding Layers
        self.word_embed = nn.Embedding(
            num_embeddings=x_encoder.vocab_size, embedding_dim=self.word_embed_dim
        )
        if self.word_embedding_weights is not None:
            self.word_embed = self.word_embed.from_pretrained(
                embeddings=self.word_embedding_weights,
                freeze=self.word_embedding_freeze,
            )

        self.word_embed_drop = nn.Dropout(self.dropout_ratio)

        self.char_embed = nn.Embedding(
            num_embeddings=x_char_encoder.vocab_size, embedding_dim=self.char_embed_dim
        )
        self.char_embed_drop = nn.Dropout(self.dropout_ratio)

        # CNN for character input
        self.char_cnn = nn.Conv1d(
            in_channels=self.char_embed_dim,
            out_channels=self.char_cnn_out_dim,
            kernel_size=5,
        )

        # LSTM for concatenated input
        if rnn_type == "GRU":
            rnn_layer = nn.GRU
        else:
            rnn_layer = nn.LSTM

        self.lstm_ner = rnn_layer(
            input_size=self.word_embed_dim
            + self.tag_embed_dim
            + self.char_cnn_out_dim
            + self.enrich_dim,
            hidden_size=self.rnn_hidden_size,
            num_layers=self.rnn_stack_size,
            batch_first=True,
            dropout=self.dropout_ratio,
            bidirectional=self.rnn_bidirectional,
        )
        self.lstm_ner_drop = nn.Dropout(self.dropout_ratio)

        self.linear_in_size = (
            self.rnn_hidden_size * 2 if self.rnn_bidirectional else self.rnn_hidden_size
        )

        # Linear layers
        self.linear1 = nn.Linear(in_features=self.linear_in_size, out_features=128)
        self.linear_drop = nn.Dropout(self.dropout_ratio)
        self.linear_ner = nn.Linear(
            in_features=128, out_features=self.num_classes + 1
        )  # +1 for padding 0
        self.crf = CRF(self.num_classes + 1, batch_first=True)

    def forward(self, x_word, x_pos, x_char, x_enrich, mask, y_word=None, train=True):
        """

        :param x_word: Padded word sequence
        :param x_pos: Padded pos tag features
        :param x_char: One hot encoded character features for each word
        :param x_enrich: Binary enriched features for each word
        :param mask: mask for padded values
        :param y_word: y only for training step
        :param train: True is training step
        :return: emmission matrix, decoded sequence, crf loss
        """
        x_char_shape = x_char.shape
        batch_size = x_char_shape[0]

        word_out = self.word_embed(x_word)
        word_out = self.word_embed_drop(word_out)

        char_out = self.char_embed(x_char)
        char_out = self.char_embed_drop(
            char_out
        )  # Shape - N, Max Sen Len, Max Char Len, Embedding dim
        char_out = char_out.contiguous().view(
            char_out.size(0) * char_out.size(1), char_out.size(3), char_out.size(2)
        )  # Shape - N*Max Sen Len, Embedding dim, Max Char Len,
        char_out = self.char_cnn(
            char_out
        )  # Shape - N*Max Sen Len, CNN out dim, Max Char Len
        char_out_shape = char_out.shape
        char_out = F.max_pool1d(char_out, kernel_size=char_out_shape[-1]).squeeze(
            -1
        )  # Shape - N*Max Sen Len, Max Char Len
        char_out = char_out.contiguous().view(
            batch_size, -1, char_out.size(-1)
        )  # Shape - N, Max Sen Len, Max Char Len

        # concat = torch.cat((word_out, char_out, tag_out), dim=2)
        concat = torch.cat((word_out, x_pos, char_out, x_enrich), dim=2)
        concat = F.relu(concat)
        # NER LSTM
        ner_lstm_out, _ = self.lstm_ner(concat)
        ner_lstm_out = self.lstm_ner_drop(ner_lstm_out)

        # Linear
        ner_out = self.linear1(ner_lstm_out)
        ner_out = self.linear_drop(ner_out)

        # Final Linear
        ner_out = self.linear_ner(ner_out)

        # if self.class_weights is not None:
        #    ner_out = ner_out * self.class_weights

        crf_out_decoded = self.crf.decode(ner_out)

        if train:
            crf_out = -1 * self.crf(
                emissions=ner_out, tags=y_word, mask=mask, reduction="token_mean"
            )
        else:
            crf_out = -1 * self.crf(emissions=ner_out, tags=torch.LongTensor(crf_out_decoded), mask=mask, reduction="token_mean"
                               )
        return ner_out, crf_out_decoded, crf_out

    def predict(self, x_word, x_pos, x_char, x_enrich, mask):
        self.eval()
        return self(x_word, x_pos, x_char, x_enrich, mask, train=False)


def git_commit_push(commit_message, add=True, push=False):
    """
    Programatically runs code commit
    Programatically runs code commit
    :param commit_message:
    :param add: Run git add .? Defaults to True
    :param push: Run git push? Defaults to False
    :return: commit id
    """
    if add:
        subprocess.run(["git", "add", "."])

    subprocess.run(["git", "commit", "-m", f"{commit_message}"])

    if push:
        subprocess.run(["git", "push"])

    return subprocess.getoutput('git log --format="%H" -n 1')


def trim_list_of_lists_upto_max_len(lst_of_lst, max_len):
    """
    Trims each nested list to max len
    :param lst_of_lst:
    :param max_len:
    :return: Trimmed list of list
    """
    if isinstance(lst_of_lst, list):
        return [lst[:max_len] for lst in lst_of_lst]
    return None


class ClassificationModelUtils:
    """

    """
    def __init__(
        self,
        dataloader_train,
        dataloader_test,
        ner_class_weights,
        num_classes,
        y_o_index,
        cuda=True,
        dropout=0.3,
        rnn_type="LSTM",
        rnn_stack_size=2,
        rnn_hidden_size=512,
        learning_rate=0.001,
        word_embed_dim=256,
        postag_embed_dim=36,
        char_cnn_out_dim=32,
        enrich_dim=7,
        word_embedding_weights=None,
        word_embedding_freeze=True,
    ):
        """

        :param dataloader_train:
        :param dataloader_test:
        :param ner_class_weights: None if no class weights
        :param num_classes: Num of output classes
        :param y_o_index:
        :param cuda:
        :param dropout:
        :param rnn_type:
        :param rnn_stack_size:
        :param rnn_hidden_size:
        :param learning_rate:
        :param word_embed_dim:
        :param postag_embed_dim:
        :param char_cnn_out_dim:
        :param enrich_dim:
        :param word_embedding_weights:
        :param word_embedding_freeze:
        """
        if cuda:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            torch.cuda.empty_cache()
        else:
            self.device = torch.device("cpu")

        self.learning_rate = learning_rate

        self.dataloader_train = dataloader_train
        self.dataloader_test = dataloader_test

        self.ner_class_weights = ner_class_weights
        self.num_classes = num_classes
        self.y_o_index = y_o_index

        self.model = EntityExtraction(
            num_classes=num_classes,
            dropout_ratio=dropout,
            rnn_type=rnn_type,
            rnn_stack_size=rnn_stack_size,
            rnn_hidden_size=rnn_hidden_size,
            word_embed_dim=word_embed_dim,
            class_weights=self.ner_class_weights,
            tag_embed_dim=postag_embed_dim,
            enrich_dim=enrich_dim,
            char_cnn_out_dim=char_cnn_out_dim,
            word_embedding_weights=word_embedding_weights,
            word_embedding_freeze=word_embedding_freeze,
        )
        self.model = self.model.to(self.device)
        self.criterion_crossentropy = nn.CrossEntropyLoss(
            weight=torch.FloatTensor(self.ner_class_weights).to(device)
        )
        self.optimizer = torch.optim.Adam(
            self.model.parameters(), lr=self.learning_rate
        )

        # Train metric result holders
        self.epoch_losses = []
        self.epoch_ner_accuracy = []
        self.epoch_ner_recall = []
        self.epoch_ner_precision = []
        self.epoch_ner_f1s = []

        # Test metric result holders
        self.test_epoch_loss = []
        self.test_epoch_ner_accuracy = []
        self.test_epoch_ner_recall = []
        self.test_epoch_ner_precision = []
        self.test_epoch_ner_f1s = []

        # CRF
        # self.crf_model = CRF(self.num_classes+1).to(device)


    def evaluate_classification_metrics(self, truth, prediction, type="ner", average='macro'):
        """
        Evaluates classification metrics
        :param truth: truth array
        :param prediction: prediction array
        :param type: ner or binary, defaults to ner
        :param average: Defaulta to macro, micro and average are two other options
        :return: Matrix results - accuracy, precision, recall, f1
        """
        if type != "ner":
            average = None

        precision = precision_score(truth, prediction, average=average, zero_division=0)
        accuracy = accuracy_score(truth, prediction)
        f1 = f1_score(truth, prediction, average=average, zero_division=0)
        recall = recall_score(truth, prediction, average=average, zero_division=0)
        return accuracy, precision, recall, f1

    def plot_graphs(self, figsize=(24, 22), save_fig=True):
        """
        Plots graphs to show loss, accuracy, precision, recall and f1 for test and train
        :param figsize:
        :return:
        """
        fig = plt.figure(figsize=figsize)
        ax = fig.add_subplot(3, 2, 2)
        ax.plot(self.epoch_losses, color="b", label="Train")
        ax.plot(self.test_epoch_loss, color="g", label="Test")
        ax.legend()
        ax.set_title("Loss")

        ax = fig.add_subplot(3, 2, 3)
        ax.plot(self.epoch_ner_accuracy, color="b", label="Train")
        ax.plot(self.test_epoch_ner_accuracy, color="g", label="Test")
        ax.legend()
        ax.set_title("Accuracy")

        ax = fig.add_subplot(3, 2, 4)
        ax.plot(self.epoch_ner_precision, color="b", label="Train")
        ax.plot(self.test_epoch_ner_precision, color="g", label="Test")
        ax.legend()
        ax.set_title("Precision")

        ax = fig.add_subplot(3, 2, 5)
        ax.plot(self.epoch_ner_recall, color="b", label="Train")
        ax.plot(self.test_epoch_ner_recall, color="g", label="Test")
        ax.legend()
        ax.set_title("Recall")

        ax = fig.add_subplot(3, 2, 6)
        ax.plot(self.epoch_ner_f1s, color="b", label="Train")
        ax.plot(self.test_epoch_ner_f1s, color="g", label="Test")
        ax.legend()
        ax.set_title("F1")

        if save_fig:
            plt.savefig("artifacts/graph.png")

        plt.show()


    def validate(self):
        """
        Runs validation step
        :return:
        """
        test_losses = []
        test_ner_accs = []
        test_ner_precisions = []
        test_ner_recalls = []
        test_ner_f1s = []

        self.test_epoch_prediction_all = []
        self.test_epoch_truth_all = []

        self.model.eval()

        print("************Evaluating validation data now***************")
        for k, data_test in enumerate(self.dataloader_test):
            with torch.no_grad():
                data_test["x_padded"] = data_test["x_padded"].to(self.device)
                data_test["x_char_padded"] = data_test["x_char_padded"].to(self.device)
                data_test["x_postag_padded"] = data_test["x_postag_padded"].to(
                    self.device
                )
                data_test["x_enriched_features"] = data_test["x_enriched_features"].to(
                    self.device
                )
                data_test["y_ner_padded"] = data_test["y_ner_padded"].to(self.device)

                mask = torch.where(
                    data_test["x_padded"] > 0,
                    torch.Tensor([1]).type(torch.uint8).to(device),
                    torch.Tensor([0]).type(torch.uint8).to(device),
                )

                test_ner_out, test_crf_out, test_loss = self.model(
                    data_test["x_padded"],
                    data_test["x_postag_padded"],
                    data_test["x_char_padded"],
                    data_test["x_enriched_features"],
                    mask,
                    data_test["y_ner_padded"],
                )
                # Loss
                test_losses.append(test_loss.item())

                # Evaluation Metrics
                test_ner_out_result = np.ravel(np.array(test_crf_out))
                test_ner_truth_result = (
                    torch.flatten(data_test["y_ner_padded"]).to("cpu").numpy()
                )

                _ = [
                    self.test_epoch_prediction_all.append(out)
                    for out in test_ner_out_result
                ]
                _ = [
                    self.test_epoch_truth_all.append(out)
                    for out in np.where(
                        test_ner_truth_result == 0, self.y_o_index, test_ner_truth_result
                    )
                ]

                (
                    test_ner_accuracy,
                    test_ner_precision,
                    test_ner_recall,
                    test_ner_f1,
                ) = self.evaluate_classification_metrics(
                    self.test_epoch_truth_all, self.test_epoch_prediction_all
                )

                test_ner_accs.append(test_ner_accuracy)
                test_ner_precisions.append(test_ner_precision)
                test_ner_recalls.append(test_ner_recall)
                test_ner_f1s.append(test_ner_f1)

        self.test_epoch_loss.append(np.array(test_losses).mean())

        self.test_epoch_ner_accuracy.append(test_ner_accuracy)
        self.test_epoch_ner_precision.append(test_ner_precision)
        self.test_epoch_ner_recall.append(test_ner_recall)
        self.test_epoch_ner_f1s.append(test_ner_f1)

        print(
            f"-->Validation Loss - {self.test_epoch_loss[-1]:.4f}, "
            f"Validation Accuracy - {self.test_epoch_ner_accuracy[-1]:.2f} "
            f"Validation Precision - {self.test_epoch_ner_precision[-1]:.2f}, "
            f"Validation Recall - {self.test_epoch_ner_recall[-1]:.2f} "
            + f"Validation F1 - {self.test_epoch_ner_f1s[-1]:.2f}"
        )

    def train(self, num_epochs=10):
        """
        Runs training step
        :param num_epochs: defaults to 10
        :return:
        """
        index_metric_append = int(len(dataloader_train) / 3)

        for epoch in range(num_epochs):
            self.model.train()
            self.crf_weights = []
            print(
                f"\n\n------------------------- Epoch - {epoch + 1} of {num_epochs} -------------------------"
            )
            batch_losses = []
            batch_ner_accuracy = []
            batch_ner_f1s = []
            batch_ner_recalls = []
            batch_ner_precisions = []

            self.epoch_prediction_all = []
            self.epoch_truth_all = []

            for batch_num, data in enumerate(dataloader_train):
                self.optimizer.zero_grad()
                self.crf_weights.append(
                    self.model.crf.state_dict()["transitions"].to("cpu").numpy()
                )
                data["x_padded"] = data["x_padded"].to(self.device)
                data["x_char_padded"] = data["x_char_padded"].to(self.device)
                data["x_postag_padded"] = data["x_postag_padded"].to(self.device)
                data["x_enriched_features"] = data["x_enriched_features"].to(
                    self.device
                )
                data["y_ner_padded"] = data["y_ner_padded"].to(self.device)

                mask = torch.where(
                    data["x_padded"] > 0,
                    torch.Tensor([1]).type(torch.uint8).to(device),
                    torch.Tensor([0]).type(torch.uint8).to(device),
                )

                ner_out, crf_out, loss = self.model(
                    data["x_padded"],
                    data["x_postag_padded"],
                    data["x_char_padded"],
                    data["x_enriched_features"],
                    mask,
                    data["y_ner_padded"],
                )

                # Loss
                # loss = self.criterion_crossentropy(ner_out.transpose(2, 1), data['y_ner_padded'])
                batch_losses.append(loss.item())

                # Evaluation Metric
                test_ner_out_result = np.ravel(np.array(crf_out))
                # test_ner_out_result = torch.flatten(torch.argmax(ner_out, dim=2)).to('cpu').numpy()
                test_ner_truth_result = (
                    torch.flatten(data["y_ner_padded"]).to("cpu").numpy()
                )

                _ = [
                    self.epoch_prediction_all.append(out) for out in test_ner_out_result
                ]
                _ = [
                    self.epoch_truth_all.append(out)
                    for out in np.where(
                        test_ner_truth_result == 0, self.y_o_index, test_ner_truth_result
                    )
                ]

                (
                    ner_accuracy,
                    ner_precision,
                    ner_recall,
                    ner_f1,
                ) = self.evaluate_classification_metrics(
                    self.epoch_truth_all, self.epoch_prediction_all
                )

                batch_ner_accuracy.append(ner_accuracy)
                batch_ner_precisions.append(ner_precision)
                batch_ner_recalls.append(ner_recall)
                batch_ner_f1s.append(ner_f1)

                if batch_num % index_metric_append == 0 and batch_num != 0:
                    print(
                        f"--> Batch - {batch_num + 1}, "
                        + f"Loss - {np.array(batch_losses).mean():.4f}, "
                        + f"Accuracy - {ner_accuracy:.2f}, "
                        + f"Precision - {ner_precision:.2f}, "
                        + f"Recall - {ner_recall:.2f}, "
                        + f"F1 - {ner_f1:.2f}"
                    )

                loss.backward()
                self.optimizer.step()

            self.epoch_losses.append(np.array(batch_losses).mean())

            self.epoch_ner_accuracy.append(ner_accuracy)
            self.epoch_ner_precision.append(ner_precision)
            self.epoch_ner_recall.append(ner_recall)
            self.epoch_ner_f1s.append(ner_f1)

            self.validate()
            print(
                classification_report(
                    self.test_epoch_truth_all, self.test_epoch_prediction_all
                )
            )
            # self.plot_graphs()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Get Input Values")
    parser.add_argument(
        "--data-path",
        dest="DATA_PATH",
        default=config['data_path'],
        type=str,
        help="Data file path - pickle format",
    )
    parser.add_argument(
        "--comment",
        dest="COMMENT",
        default=f"{config['comment']} : UTC {datetime.utcnow()}",
        type=str,
        help="Any comment about training step",
    )
    parser.add_argument(
        "--exp-name",
        dest="EXPERIMENT_NAME",
        default=config['exp_name'],
        type=str,
        help="MLFLOW Experiment Name",
    )
    parser.add_argument(
        "--fw", dest="FRAMEWORK", default=config['fw'], type=str, help="Framework name"
    )
    parser.add_argument(
        "--epochs", dest="EPOCHS", default=config['epochs'], type=int, help="Number of epochs to run"
    )
    parser.add_argument(
        "--dropout", dest="DROPOUT", default=config['dropout'], type=float, help="Dropout to apply"
    )
    parser.add_argument(
        "--rnn-stack-size",
        dest="RNN_STACK_SIZE",
        default=config['rnn_stack_size'],
        type=int,
        help="Number of LSTM layers to stack",
    )
    parser.add_argument(
        "--max-sen-len",
        dest="MAX_SENTENCE_LEN",
        default=config['max_sen_len'],
        type=int,
        help="Max Senetence Length",
    )
    parser.add_argument(
        "--max-word-len",
        dest="MAX_WORD_LEN",
        default=config['max_word_len'],
        type=int,
        help="Max Word Length",
    )
    parser.add_argument(
        "--lr", dest="LEARNING_RATE", default=config['lr'], type=float, help="Learning Rate"
    )
    parser.add_argument(
        "--split-size",
        dest="TEST_SPLIT",
        default=config['split_size'],
        type=float,
        help="Test Split Size",
    )
    parser.add_argument("--gpu", dest="GPU", default=ast.literal_eval(config['gpu']), type=bool, help="Use GPU")
    parser.add_argument(
        "--batch-size", dest="BATCH_SIZE", default=config['batch_size'], type=int, help="Batch Size"
    )
    parser.add_argument(
        "--word-embed-cache-path",
        dest="WORD_EMBED_CACHE_PATH",
        default=config['word_embed_cache_path'].format(home=home) if '{home}' in config['word_embed_cache_path'] else config['word_embed_cache_path'],
        type=str,
        help="Glove word embedding cache dir path, Defaults to .word_vectors_cache "
             "directory in home dir. Pass '' or None to train embeddings from scratch",
    )
    parser.add_argument(
        "--word-embed-name",
        dest="WORD_EMBED_NAME",
        default=config['word_embed_name'],
        type=str,
        help="Glove word embedding name",
    )
    parser.add_argument(
        "--word-embed-freeze",
        dest="WORD_EMBED_FREEZE",
        default=ast.literal_eval(config['word_embed_freeze']),
        type=bool,
        help="Freeze word embedding weights",
    )

    parser.add_argument(
        "--word-embed-dim",
        dest="WORD_EMBED_DIM",
        default=config['word_embed_dim'],
        type=int,
        help="Word embedding dimension. Ignore if providing a pre-trained word embedding",
    )

    parser.add_argument(
        "--char-cnn-out-dim",
        dest="CHAR_CNN_OUT_DIM",
        default=config['char_cnn_out_dim'],
        type=int,
        help="Character CNN out dimentions",
    )

    parser.add_argument(
        "--rnn-type",
        dest="RNN_TYPE",
        default=config['rnn_type'],
        type=str,
        help="RNN Type - LSTM or GRU",
    )

    parser.add_argument(
        "--rnn-hidden-size",
        dest="RNN_HIDDEN_SIZE",
        default=config['rnn_hidden_size'],
        type=int,
        help="LSTM hidden size",
    )

    args = parser.parse_args()

    mlflow.set_experiment(args.EXPERIMENT_NAME)
    experiment = mlflow.get_experiment_by_name(args.EXPERIMENT_NAME)
    with mlflow.start_run() as run:
        mlflow.set_tags(
            {
                "Framework": args.FRAMEWORK,
                "Embeddings": "Word-POSTAG",
                "Outputs": "NER Only",
                "Loss": "CRF with mask",
            }
        )
        mlflow.log_param("CUDA", args.GPU)
        mlflow.log_param("COMMENT", args.COMMENT)
        mlflow.log_param("EPOCHS", args.EPOCHS)
        mlflow.log_param("DROPOUT", args.DROPOUT)
        mlflow.log_param("CHAR_CNN_OUT_DIM", args.CHAR_CNN_OUT_DIM)
        mlflow.log_param("RNN_STACK_SIZE", args.RNN_STACK_SIZE)
        mlflow.log_param("LEARNING_RATE", args.LEARNING_RATE)
        mlflow.log_param("TEST_SPLIT", args.TEST_SPLIT)
        mlflow.log_param("GPU_AVAILABLE", torch.cuda.is_available())
        mlflow.log_param("RNN_HIDDEN_SIZE", args.RNN_HIDDEN_SIZE)
        mlflow.log_param("BATCH_SIZE", args.BATCH_SIZE)
        mlflow.log_param("DATA_PATH", args.DATA_PATH)

        commit_id = git_commit_push(commit_message=args.COMMENT)
        mlflow.log_param("COMMIT ID", commit_id)
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        ARTIFACTS_DIR = os.path.join(BASE_DIR, "artifacts")
        if not os.path.exists(ARTIFACTS_DIR):
            os.makedirs(ARTIFACTS_DIR)


        if isinstance(args.WORD_EMBED_CACHE_PATH, str) and args.WORD_EMBED_CACHE_PATH.strip() != "":
            vectors = GloVe(name=args.WORD_EMBED_NAME, cache=args.WORD_EMBED_CACHE_PATH)
        else:
            vectors = None

        # Word embedding logging
        mlflow.log_param("WORD_EMBED_CACHE_PATH", args.WORD_EMBED_CACHE_PATH)
        mlflow.log_param("WORD_EMBED_FREEZE", args.WORD_EMBED_FREEZE)
        mlflow.log_param("WORD_EMBED_NAME", args.WORD_EMBED_NAME)

        # Load Data
        X_text_list_as_is, X_text_list, y_ner_list = load_data(args.DATA_PATH)

        # Get POS tags
        X_tags, tag_to_index = get_POS_tags(X_text_list)
        with open(os.path.join(ARTIFACTS_DIR, "tag_to_index"), "wb") as inf:
            dill.dump(tag_to_index, inf)

        POSTAG_EMBED_DIM = max(tag_to_index.values()) + 1
        mlflow.log_param("DATA_SIZE", len(X_text_list))
        mlflow.log_param("POSTAG_EMBED_DIM", POSTAG_EMBED_DIM)

        # SENTENCE_LEN_LIST = [len(sentence) for sentence in X_text_list]

        X_text_list = trim_list_of_lists_upto_max_len(
            X_text_list, args.MAX_SENTENCE_LEN
        )
        X_text_list_as_is = trim_list_of_lists_upto_max_len(
            X_text_list_as_is, args.MAX_SENTENCE_LEN
        )
        y_ner_list = trim_list_of_lists_upto_max_len(y_ner_list, args.MAX_SENTENCE_LEN)
        X_tags = trim_list_of_lists_upto_max_len(X_tags, args.MAX_SENTENCE_LEN)
        print(
            f"Max sentence len after trimming upto {args.MAX_SENTENCE_LEN} words is {max([len(sentence) for sentence in X_text_list])}"
        )

        alnum, numeric, alpha, digit, lower, title, ascii = enrich_data(
            X_text_list_as_is
        )

        alnum = pad_and_stack_list_of_list(
            alnum,
            max_sentence_len=args.MAX_SENTENCE_LEN,
            pad_value=-1,
            tensor_type=torch.FloatTensor,
        )
        numeric = pad_and_stack_list_of_list(
            numeric,
            max_sentence_len=args.MAX_SENTENCE_LEN,
            pad_value=-1,
            tensor_type=torch.FloatTensor,
        )
        alpha = pad_and_stack_list_of_list(
            alpha,
            max_sentence_len=args.MAX_SENTENCE_LEN,
            pad_value=-1,
            tensor_type=torch.FloatTensor,
        )
        digit = pad_and_stack_list_of_list(
            digit,
            max_sentence_len=args.MAX_SENTENCE_LEN,
            pad_value=-1,
            tensor_type=torch.FloatTensor,
        )
        lower = pad_and_stack_list_of_list(
            lower,
            max_sentence_len=args.MAX_SENTENCE_LEN,
            pad_value=-1,
            tensor_type=torch.FloatTensor,
        )
        title = pad_and_stack_list_of_list(
            title,
            max_sentence_len=args.MAX_SENTENCE_LEN,
            pad_value=-1,
            tensor_type=torch.FloatTensor,
        )
        ascii = pad_and_stack_list_of_list(
            ascii,
            max_sentence_len=args.MAX_SENTENCE_LEN,
            pad_value=-1,
            tensor_type=torch.FloatTensor,
        )

        x_enriched_features = torch.stack(
            (alnum, numeric, alpha, digit, lower, title, ascii), dim=2
        )
        ENRICH_FEAT_DIM = x_enriched_features.size(-1)

        # Split data in test and train plus return segregate as input lists

        (
            (X_text_list_train, X_text_list_test),
            (X_text_list_as_is_train, X_text_list_as_is_test),
            (X_tags_train, X_tags_test),
            (x_enriched_features_train, x_enriched_features_test),
            (y_ner_list_train, y_ner_list_test),
            (train_index, test_index),
        ) = split_test_train(
            X_text_list,
            X_text_list_as_is,
            X_tags,
            x_enriched_features,
            y_ner_list,
            split_size=args.TEST_SPLIT,
        )

        # Set some important parameters values
        all_labels = []
        _ = [[all_labels.append(label) for label in lst] for lst in y_ner_list_train]
        class_count_out = np.unique(all_labels, return_counts=True)
        class_count_dict = dict(zip(class_count_out[0], class_count_out[1]))
        num_classes = len([clas for clas in class_count_dict.keys()])
        print(
            f"Max sentence length - {args.MAX_SENTENCE_LEN}, Total Classes = {num_classes}"
        )

        mlflow.log_param("TEST_INDEX", str(test_index))
        mlflow.log_param("MAX_SENTENCE_LEN", args.MAX_SENTENCE_LEN)
        mlflow.log_param("NUM_CLASSES", num_classes)
        mlflow.log_param("ENRICH_FEAT_DIM", ENRICH_FEAT_DIM)

        # Tokenize Sentences
        x_encoder, x_padded_train, x_padded_test = tokenize_sentence(
            X_text_list_train, X_text_list_test, args.MAX_SENTENCE_LEN
        )

        with open(os.path.join(ARTIFACTS_DIR, "x_encoder"), "wb") as inf:
            dill.dump(x_encoder, inf)

        if vectors is not None:
            x_embed_weights = torch.stack([vectors[word] for word in x_encoder.vocab])
            args.WORD_EMBED_DIM = x_embed_weights.size(-1)
            mlflow.log_param("EMBEDDING_WEIGHTS", x_embed_weights.size())
        else:
            x_embed_weights = None
            mlflow.log_param("EMBEDDING_WEIGHTS", x_embed_weights)

        mlflow.log_param("WORD_EMBED_DIM", args.WORD_EMBED_DIM)

        # Tokenize Characters
        (
            x_char_encoder,
            x_char_padded_train,
            x_char_padded_test,
            max_word_length,
        ) = tokenize_character(
            X_text_list_as_is_train, X_text_list_as_is_test, args.MAX_SENTENCE_LEN
        )
        mlflow.log_param("MAX_WORD_LENGTH", max_word_length)

        with open(os.path.join(ARTIFACTS_DIR, "x_char_encoder"), "wb") as inf:
            dill.dump(x_char_encoder, inf)

        # Tokenize Pos tags
        x_postag_padded_train = tokenize_pos_tags(
            X_tags_train, tag_to_index=tag_to_index, max_sen_len=args.MAX_SENTENCE_LEN
        )
        x_postag_padded_test = tokenize_pos_tags(
            X_tags_train, tag_to_index=tag_to_index, max_sen_len=args.MAX_SENTENCE_LEN
        )

        # Encode y NER
        y_ner_encoder, y_ner_padded_train, y_ner_padded_test = encode_ner_y(
            y_ner_list_train, y_ner_list_test, class_count_dict, args.MAX_SENTENCE_LEN
        )
        with open(os.path.join(ARTIFACTS_DIR, "y_ner_encoder"), "wb") as inf:
            dill.dump(y_ner_encoder, inf)

        y_o_index = y_ner_encoder.token_to_index['O']
        mlflow.log_param("Y_O_INDEX", y_o_index)

        mlflow.log_artifacts('artifacts', 'files')

        # Create train dataloader
        dataset_train = Dataset(
            [
                {
                    "x_padded": x_padded_train[i],
                    "x_char_padded": x_char_padded_train[i],
                    "x_postag_padded": x_postag_padded_train[i],
                    "x_enriched_features": x_enriched_features_train[i],
                    "y_ner_padded": y_ner_padded_train[i],
                }
                for i in range(x_padded_train.shape[0])
            ]
        )

        dataloader_train = DataLoader(
            dataset=dataset_train, batch_size=args.BATCH_SIZE, shuffle=True
        )

        # Create test dataloader
        dataset_test = Dataset(
            [
                {
                    "x_padded": x_padded_test[i],
                    "x_char_padded": x_char_padded_test[i],
                    "x_postag_padded": x_postag_padded_test[i],
                    "x_enriched_features": x_enriched_features_test[i],
                    "y_ner_padded": y_ner_padded_test[i],
                }
                for i in range(x_padded_test.shape[0])
            ]
        )

        dataloader_test = DataLoader(
            dataset=dataset_test, batch_size=args.BATCH_SIZE, shuffle=False
        )

        # Build model
        ner_class_weights = calculate_sample_weights(y_ner_padded_train)

        model_utils = ClassificationModelUtils(
            dataloader_train,
            dataloader_test,
            ner_class_weights,
            y_o_index=y_o_index,
            num_classes=num_classes,
            cuda=args.GPU,
            rnn_stack_size=args.RNN_STACK_SIZE,
            word_embed_dim=args.WORD_EMBED_DIM,
            enrich_dim=ENRICH_FEAT_DIM,
            postag_embed_dim=POSTAG_EMBED_DIM,
            learning_rate=args.LEARNING_RATE,
            word_embedding_weights=x_embed_weights,
            word_embedding_freeze=args.WORD_EMBED_FREEZE,
            char_cnn_out_dim=args.CHAR_CNN_OUT_DIM,
            rnn_hidden_size=args.RNN_HIDDEN_SIZE,
            rnn_type=args.RNN_TYPE,
        )
        model_utils.train(args.EPOCHS)

        mlflow.pytorch.log_model(model_utils.model, "model", conda_env=conda_environment)

        mlflow.log_metric("Loss-Test", model_utils.test_epoch_loss[-1])
        mlflow.log_metric("Loss-Train", model_utils.epoch_losses[-1])

        mlflow.log_metric("Accuracy-Test", model_utils.test_epoch_ner_accuracy[-1])
        mlflow.log_metric("Accuracy-Train", model_utils.epoch_ner_accuracy[-1])

        mlflow.log_metric("Precision-Test", model_utils.test_epoch_ner_precision[-1])
        mlflow.log_metric("Precision-Train", model_utils.epoch_ner_precision[-1])

        mlflow.log_metric("Recall-Test", model_utils.test_epoch_ner_recall[-1])
        mlflow.log_metric("Recall-Train", model_utils.epoch_ner_recall[-1])

        mlflow.log_metric("F1-Test", model_utils.test_epoch_ner_f1s[-1])
        mlflow.log_metric("F1-Train", model_utils.epoch_ner_f1s[-1])

        model_utils.plot_graphs()
        mlflow.log_artifact("artifacts/graph.png", 'files')
