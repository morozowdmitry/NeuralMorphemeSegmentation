import bisect
import inspect
import json
import os
from collections import defaultdict
from itertools import chain

import numpy as np
from keras import layers as kl, backend as kb, Model
from keras.engine.topology import InputSpec
from keras.callbacks import EarlyStopping, ModelCheckpoint
from keras.optimizers import adam

from embedder import Embedder
from generate import DataGenerator, MultitaskDataGenerator, MultimodelTrainer, SimpleDataGenerator
from read import extract_morpheme_type
from tabled_trie import make_trie


AUXILIARY_CODES = PAD, BEGIN, END, UNKNOWN = 0, 1, 2, 3

AUXILIARY = ['PAD', 'BEGIN', 'END', 'UNKNOWN']


def _make_vocabulary(source):
    """
    Создаёт словарь символов.
    """
    symbols = {a for word in source for a in word}
    symbols = AUXILIARY + sorted(symbols)
    symbol_codes = {a: i for i, a in enumerate(symbols)}
    return symbols, symbol_codes


def make_bucket_lengths(lengths, buckets_number):
    """
    Вычисляет максимальные длины элементов в корзинах. Каждая корзина состоит из элементов примерно одинаковой длины
    """
    m = len(lengths)
    lengths = sorted(lengths)
    last_bucket_length, bucket_lengths = 0, []
    for i in range(buckets_number):
        # могут быть проблемы с выбросами большой длины
        level = (m * (i + 1) // buckets_number) - 1
        curr_length = lengths[level]
        if curr_length > last_bucket_length:
            bucket_lengths.append(curr_length)
            last_bucket_length = curr_length
    return bucket_lengths


def collect_buckets(lengths, buckets_number, max_bucket_size=-1):
    """
    Распределяет элементы по корзинам
    """
    bucket_lengths = make_bucket_lengths(lengths, buckets_number)
    indexes = [[] for _ in bucket_lengths]
    for i, length in enumerate(lengths):
        index = bisect.bisect_left(bucket_lengths, length)
        indexes[index].append(i)
    if max_bucket_size != -1:
        bucket_lengths = list(chain.from_iterable(
            ([L] * ((len(curr_indexes)-1) // max_bucket_size + 1))
            for L, curr_indexes in zip(bucket_lengths, indexes)
            if len(curr_indexes) > 0))
        indexes = [curr_indexes[start:start+max_bucket_size]
                   for curr_indexes in indexes
                   for start in range(0, len(curr_indexes), max_bucket_size)]
    return [(L, curr_indexes) for L, curr_indexes
            in zip(bucket_lengths, indexes) if len(curr_indexes) > 0]

class Partitioner:

    """
    models_number: int, default=1, число моделей
    to_memorize_morphemes: bool, default=False,
        производится ли запоминание морфемных энграмм
    min_morpheme_count: int, default=2,
        минимальное количество раз, которое должна встречаться запоминаемая морфема
    to_memorize_ngram_counts: bool, default=False,
        используются ли частоты энграмм как морфем при вычислении признаков
    min_relative_ngram_count: float, default=0.1,
        минимальное отношение частоты энграммы как морфемы к её общей частоте,
        необходимое для её запоминания
    use_embeddings: bool, default=False,
        используется ли дополнительный слой векторных представлений символов
    embeddings_size: int, default=32, размер символьного представления
    conv_layers: int, default=1, число свёрточных слоёв
    window_size: int or list of ints, список размеров окна в свёрточном слое
    filters_number: int or list of ints or list of list of ints,
        число фильтров в свёрточных слоях,
        filters_number[i,j] --- число фильтров для i-го окна j-го слоя,
        если задан список, то filters_number[j] --- число фильтров в окнах j-го слоя,
        если число --- то одно и то же число фильтров для всех слоёв и окон
    dense_output_units: int, default=0,
        число нейронов на дополнительном слое перед вычислением выходных вероятностей.
        если 0, то этот слой отсутствует
    use_lstm: bool, default=False,
        используется ли дополнительный выходной слой LSTM (ухудшает качество)
    lstm_units: int, default=64, число нейронов в LSTM-слое
    dropout: float, default=0.0
        доля выкидываемых нейронов в dropout-слое, помогает бороться с переобучением
    context_dropout: float, default=0.0,
        вероятность маскировки векторного представления контекста
    buckets_number: int, default=10,
        число корзин, в одну корзину попадают данные примерно одинаковой длины
    nepochs: int, default=10, число эпох в обучении
    validation_split: float, default=0.2, доля элементов в развивающей выборке
    batch_size: int, default=32, число элементов в одном батче
    callbacks: list of keras.callbacks or None, default=None,
        коллбэки для управления процессом обучения,
    early_stopping: int, default=None,
        число эпох, в течение которого не должно улучшаться качество
        на валидационной выборке, чтобы обучение остановилось,
        если None, то в любом случае модель обучается nepochs эпох
    """

    LEFT_MORPHEME_TYPES = ["pref", "root"]
    RIGHT_MORPHEME_TYPES = ["root", "suff", "end", "postfix"]
    BAD_FIELDS = ["callbacks", "models_", "left_morphemes_", "right_morphemes_",
                  "morpheme_trie_", "lm_embedder", "language_models_"]

    def __init__(self, models_number=1, use_inputs=True, use_morpheme_types=True,
                 to_memorize_morphemes=False, min_morpheme_count=2,
                 to_memorize_ngram_counts=False, min_relative_ngram_count=0.1,
                 lm_embedder_file=None, lm_state_size=64, lm_inputs_to_conv=True,
                 use_embeddings=False, embeddings_size=32,
                 conv_layers=1, window_size=5, filters_number=64,
                 dense_output_units=0, use_lstm=False, lstm_units=64,
                 dropout=0.0, context_dropout=0.0,
                 buckets_number=10, nepochs=10, lm_epochs=5,
                 validation_split=0.2, batch_size=32,
                 callbacks=None, early_stopping=None):
        self.models_number = models_number
        self.use_inputs = use_inputs
        self.use_morpheme_types = use_morpheme_types
        self.to_memorize_morphemes = to_memorize_morphemes
        self.min_morpheme_count = min_morpheme_count
        self.to_memorize_ngram_counts = to_memorize_ngram_counts
        self.min_relative_ngram_count = min_relative_ngram_count
        self.lm_embedder_file = lm_embedder_file
        self.lm_state_size = lm_state_size
        self.lm_inputs_to_conv = lm_inputs_to_conv
        self.use_embeddings = use_embeddings
        self.embeddings_size = embeddings_size
        self.conv_layers = conv_layers
        self.window_size = window_size
        self.filters_number = filters_number
        self.dense_output_units = dense_output_units
        self.use_lstm = use_lstm
        self.lstm_units = lstm_units
        self.dropout = dropout
        self.context_dropout = context_dropout
        self.buckets_number = buckets_number
        self.nepochs = nepochs
        self.lm_epochs = lm_epochs
        self.validation_split = validation_split
        self.batch_size = batch_size
        self.callbacks = callbacks
        self.early_stopping = early_stopping
        self.check_params()

    def check_params(self):
        if isinstance(self.window_size, int):
            # если было только одно окно в свёрточных слоях
            self.window_size = [self.window_size]
        # приводим фильтры к двумерному виду
        self.filters_number = np.atleast_2d(self.filters_number)
        if self.filters_number.shape[0] == 1:
            self.filters_number = np.repeat(self.filters_number, len(self.window_size), axis=0)
        if self.filters_number.shape[0] != len(self.window_size):
            raise ValueError("Filters array should have shape (len(window_size), conv_layers)")
        if self.filters_number.shape[1] == 1:
            self.filters_number = np.repeat(self.filters_number, self.conv_layers, axis=1)
        if self.filters_number.shape[1] != self.conv_layers:
            raise ValueError("Filters array should have shape (len(window_size), conv_layers)")
        # переводим в список из int, а не np.int32, чтобы не было проблем при сохранении
        self.filters_number = list([list(map(int, x)) for x in self.filters_number])
        if self.callbacks is None:
            self.callbacks = []
        if (self.early_stopping is not None and
                not any(isinstance(x, EarlyStopping) for x in self.callbacks)):
            self.callbacks.append(EarlyStopping(patience=self.early_stopping, monitor="val_acc"))
        if self.use_morpheme_types:
            self._morpheme_memo_func = self._make_morpheme_data
        else:
            self._morpheme_memo_func = self._make_morpheme_data_simple
        if self.lm_embedder_file is not None:
            with open(self.lm_embedder_file, "r", encoding="utf8") as fin:
                params = json.load(fin)
                self.lm_embedder = Embedder(**params)
        else:
            self.lm_embedder = None

    def to_json(self, outfile, model_file=None):
        info = dict()
        if model_file is None:
            pos = outfile.rfind(".")
            model_file = outfile[:pos] + ("-model.hdf5" if pos != -1 else "-model")
        model_files = [make_model_file(model_file, i+1) for i in range(self.models_number)]
        for i in range(self.models_number):
            # при сохранении нужен абсолютный путь, а не от текущей директории
            model_files[i] = os.path.abspath(model_files[i])
        for (attr, val) in inspect.getmembers(self):
            # перебираем поля класса и сохраняем только задаваемые при инициализации
            if not (attr.startswith("__") or inspect.ismethod(val) or
                    isinstance(getattr(Partitioner, attr, None), property) or
                    attr.isupper() or attr in self.BAD_FIELDS):
                info[attr] = val
            elif attr == "models_":
                # для каждой модели сохраняем веса
                info["model_files"] = model_files
                for model, curr_model_file in zip(self.models_, model_files):
                    model.save_weights(curr_model_file)
        info["cls"] = "Partitioner"
        with open(outfile, "w", encoding="utf8") as fout:
            json.dump(info, fout)

    # property --- функция, прикидывающаяся переменной; декоратор метода (превращает метод класса в атрибут класса)
    @property
    def symbols_number_(self):
        return len(self.symbols_)

    @property
    def target_symbols_number_(self):
        return len(self.target_symbols_)

    @property
    def memory_dim(self):
        return 15 if self.use_morpheme_types else 3

    def _preprocess(self, data, targets=None):
        # к каждому слову добавляются символы начала и конца строки
        lengths = [len(x) + 2 for x in data]
        # разбиваем данные на корзины
        buckets_with_indexes = collect_buckets(lengths, self.buckets_number)
        # преобразуем данные в матрицы в каждой корзине
        data_by_buckets = [self._make_bucket_data(data, length, indexes)
                           for length, indexes in buckets_with_indexes]
        # targets=None --- предсказание, иначе --- обучение
        if targets is not None:
            targets_by_buckets = [self._make_bucket_data(targets, length, indexes, is_target=True)
                                  for length, indexes in buckets_with_indexes]
            return data_by_buckets, targets_by_buckets, buckets_with_indexes
        else:
            return data_by_buckets, buckets_with_indexes

    def _make_bucket_data(self, data, bucket_length, bucket_indexes, is_target=False):
        """
        data: list of lists, исходные данные
        bucket_length: int, максимальная длина элемента в корзине
        bucket_indexes: list of ints, индексы элементов в корзине
        is_target: boolean, default=False,
            являются ли данные исходными или ответами

        answer = [symbols, (classes)],
            symbols: array of shape (len(data), bucket_length)
                элементы data, дополненные символом PAD справа до bucket_length
            classes: array of shape (len(data), classes_number)
        """
        bucket_data = [data[i] for i in bucket_indexes]
        if is_target:
            return self._recode_bucket_data(bucket_data, bucket_length, self.target_symbol_codes_)
        else:
            if self.use_inputs:
                answer = [self._recode_bucket_data(bucket_data, bucket_length, self.symbol_codes_)]
                if self.to_memorize_morphemes:
                    # print("Processing morphemes for bucket length", bucket_length)
                    answer.append(self._morpheme_memo_func(bucket_data, bucket_length))
                    # print("Processing morphemes for bucket length", bucket_length, "finished")
            else:
                answer = []
            if self.lm_embedder is not None:
                answer.append(self._make_lm_embeddings_data(bucket_data, bucket_length))
                # answer = [self._make_lm_embeddings_data(bucket_data, bucket_length)]
            return answer

    def _recode_bucket_data(self, data, bucket_length, encoding):
        answer = np.full(shape=(len(data), bucket_length), fill_value=PAD, dtype=int)
        answer[:,0] = BEGIN
        for j, word in enumerate(data):
            answer[j,1:1+len(word)] = [encoding.get(x, UNKNOWN) for x in word]
            answer[j,1+len(word)] = END
        return answer

    def _make_morpheme_data(self, data, bucket_length):
        """
        строит для каждой позиции во входных словах вектор, кодирующий энграммы в контексте

        data: list of strs, список исходных слов
        bucket_length: int, максимальная длина слова в корзине

        answer: np.array[float] of shape (len(data), bucket_length, 15)
        """
        answer = np.zeros(shape=(len(data), bucket_length, 15), dtype=float)
        for j, word in enumerate(data):
            m = len(word)
            curr_answer = np.zeros(shape=(bucket_length, 15), dtype=int)
            root_starts = [0]
            ending_ends = [m]
            prefixes = self.left_morphemes_["pref"].descend_by_prefixes(word[:-1])
            for end in prefixes:
                score = self._get_ngram_score(word[:end], "pref")
                if end == 1:
                    curr_answer[1,10] = max(score, curr_answer[1,10])
                else:
                    curr_answer[1,0] = max(score, curr_answer[1,0])
                    curr_answer[end, 5] = max(score, curr_answer[end, 5])
            root_starts += prefixes
            postfix_lengths = self.right_morphemes_["postfix"].descend_by_prefixes(word[:0:-1])
            for k in postfix_lengths:
                score = self._get_ngram_score(word[-k:], "postfix")
                if k == 1:
                    curr_answer[m, 14] = max(score, curr_answer[m, 14])
                else:
                    curr_answer[m, 9] = max(score, curr_answer[m, 9])
                    curr_answer[m-k+1,4] = max(score, curr_answer[m-k+1,4])
                ending_ends.append(m-k)
            suffix_ends = set(ending_ends)
            for end in ending_ends[::-1]:
                ending_lengths = self.right_morphemes_["end"].descend_by_prefixes(word[end-1:0:-1])
                for k in ending_lengths:
                    score = self._get_ngram_score(word[end-k:end], "end")
                    if k == 1:
                        curr_answer[end, 13] = max(score, curr_answer[end, 13])
                    else:
                        curr_answer[end-k+1, 3] = max(score, curr_answer[end-k+1, 3])
                        curr_answer[end, 8] = max(score, curr_answer[end, 8])
                    suffix_ends.add(end-k)
            suffixes = self.right_morphemes_["suff"].descend_by_prefixes(
                word[::-1], start_pos=[m-k for k in suffix_ends], max_count=3, return_pairs=True)
            suffix_starts = suffix_ends
            for first, last in suffixes:
                score = self._get_ngram_score(word[m-last:m-first], "suff")
                if last == first + 1:
                    curr_answer[m-first, 12] = max(score, curr_answer[m-first, 12])
                else:
                    curr_answer[m-last+1, 2] = max(score, curr_answer[m-last+1, 2])
                    curr_answer[m-first, 7] = max(score, curr_answer[m-first, 7])
                suffix_starts.add(m-last)
            for start in root_starts:
                root_ends = self.left_morphemes_["root"].descend_by_prefixes(word[start:])
                for end in root_ends:
                    score = self._get_ngram_score(word[start:end], "root")
                    if end == start+1:
                        curr_answer[start + 1, 11] = max(score, curr_answer[start + 1, 11])
                    else:
                        curr_answer[start + 1, 1] = max(score, curr_answer[start + 1, 1])
                        curr_answer[end, 6] = max(score, curr_answer[end, 6])
            for end in suffix_starts:
                root_lengths = self.right_morphemes_["root"].descend_by_prefixes(word[end-1:-1:-1])
                for k in root_lengths:
                    score = self._get_ngram_score(word[end-k:end], 'root')
                    if k == 1:
                        curr_answer[end, 11] = max(curr_answer[end, 11], score)
                    else:
                        curr_answer[end-k+1, 1] = max(curr_answer[end-k+1, 1], score)
                        curr_answer[end, 6] = max(curr_answer[end, 6], score)
            answer[j] = curr_answer
        return answer

    def _make_morpheme_data_simple(self, data, bucket_length):
        answer = np.zeros(shape=(len(data), bucket_length, 3), dtype=float)
        for j, word in enumerate(data):
            m = len(word)
            curr_answer = np.zeros(shape=(bucket_length, 3), dtype=int)
            positions = self.morpheme_trie_.find_substrings(word, return_positions=True)
            for starts, end in positions:
                for start in starts:
                    score = self._get_ngram_score(word[start:end])
                    if end == start+1:
                        curr_answer[start+1, 2] = max(curr_answer[start+1, 2], score)
                    else:
                        curr_answer[start+1, 0] = max(curr_answer[start+0, 2], score)
                        curr_answer[end, 1] = max(curr_answer[end, 1], score)
            answer[j] = curr_answer
        return answer

    def _get_ngram_score(self, ngram, mode="None"):
        if self.to_memorize_ngram_counts:
            return self.morpheme_counts_[mode].get(ngram, 0)
        else:
            return 1.0

    def _make_lm_embeddings_data(self, data, length):
        answer = np.zeros(shape=(len(data), length, self.lm_state_size), dtype=float)
        embeddings = self.lm_embedder.transform([[elem] for elem in data])
        for i, elem in enumerate(embeddings):
            answer[i,:len(elem)] = elem
        return answer

    def train(self, source, targets, dev=None, dev_targets=None, model_file=None, verbose=True):
        """

        source: list of strs, список слов для морфемоделения
        targets: list of strs, метки морфемоделения в формате BMES
        model_file: str or None, default=None, файл для сохранения моделей

        Возвращает:
        -------------
        self, обученный морфемоделитель
        """
        self.symbols_, self.symbol_codes_ = _make_vocabulary(source)
        self.target_symbols_, self.target_symbol_codes_ = _make_vocabulary(targets)
        self.target_types_ = [x.split("-")[1] for x in self.target_symbols_ if "-" in x]
        if self.to_memorize_morphemes:
            self._memorize_morphemes(source, targets)

        data_by_buckets, targets_by_buckets, _ = self._preprocess(source, targets)
        if dev is not None:
            # dev, dev_targets = dev[:100], dev_targets[:100]
            dev_data_by_buckets, dev_targets_by_buckets, _ = self._preprocess(dev, dev_targets)
        else:
            dev_data_by_buckets, dev_targets_by_buckets = None, None
        self.build(verbose=verbose)
        self._train_models(data_by_buckets, targets_by_buckets, dev_data_by_buckets,
                           dev_targets_by_buckets, model_file=model_file, verbose=verbose)
        return self

    def build(self, verbose=True):
        """
        Создаёт нейронные модели
        """
        self.models_ = [self.build_model() for _ in range(self.models_number)]
        if verbose:
            print(self.models_[0].summary())
        return self

    def build_model(self):
        """
        Функция, задающая архитектуру нейронной сети
        """
        # symbol_inputs: array, 1D-массив длины m
        if self.use_inputs:
            symbol_inputs = kl.Input(shape=(None,), dtype='uint8', name="symbol_inputs")
            # symbol_embeddings: array, 2D-массив размера m*self.symbols_number
            if self.use_embeddings:
                symbol_embeddings = kl.Embedding(self.symbols_number_, self.embeddings_size,
                                                 name="symbol_embeddings")(symbol_inputs)
            else:
                symbol_embeddings = kl.Lambda(kb.one_hot, output_shape=(None, self.symbols_number_),
                                              arguments={"num_classes": self.symbols_number_},
                                              name="symbol_embeddings")(symbol_inputs)
            inputs, conv_inputs = [symbol_inputs], [symbol_embeddings]
        else:
            inputs, conv_inputs = [], []
        if self.to_memorize_morphemes:
            # context_inputs: array, 2D-массив размера m*15
            context_inputs = kl.Input(shape=(None, self.memory_dim), dtype='float32', name="context_inputs")
            inputs.append(context_inputs)
            if self.context_dropout > 0.0:
                context_inputs = kl.Dropout(self.context_dropout)(context_inputs)
            # представление контекста подклеивается к представлению символа
            conv_inputs.append(context_inputs)
        if self.lm_embedder is not None:
            lm_inputs = kl.Input(shape=(None, self.lm_state_size), dtype='float32', name="lm_inputs")
            inputs.append(lm_inputs)
            if self.lm_inputs_to_conv:
                conv_inputs.append(lm_inputs)
        if len(conv_inputs) == 1:
            conv_inputs = conv_inputs[0]
        else:
            conv_inputs = kl.Concatenate()(conv_inputs)
        if self.conv_layers > 0:
            conv_outputs = []
            for window_size, curr_filters_numbers in zip(self.window_size, self.filters_number):
                # свёрточный слой отдельно для каждой ширины окна
                curr_conv_input = conv_inputs
                for j, filters_number in enumerate(curr_filters_numbers[:-1]):
                    # все слои свёртки, кроме финального (после них возможен dropout)
                    curr_conv_input = kl.Conv1D(filters_number, window_size,
                                                activation="relu", padding="same")(curr_conv_input)
                    curr_conv_input = kl.BatchNormalization()(curr_conv_input)
                    if self.dropout > 0.0:
                        # между однотипными слоями рекомендуется вставить dropout
                        curr_conv_input = kl.Dropout(self.dropout)(curr_conv_input)
                if not self.use_lstm:
                    curr_conv_output = kl.Conv1D(curr_filters_numbers[-1], window_size,
                                                 activation="relu", padding="same")(curr_conv_input)
                else:
                    curr_conv_output = curr_conv_input
                conv_outputs.append(curr_conv_output)
            # соединяем выходы всех свёрточных слоёв в один вектор
            if len(conv_outputs) == 1:
                conv_output = conv_outputs[0]
            else:
                conv_output = kl.Concatenate(name="conv_output")(conv_outputs)
        else:
            conv_output = conv_inputs
        if self.lm_embedder is not None and not self.lm_inputs_to_conv:
            if conv_output is not None:
                conv_output = kl.Concatenate()([conv_output, lm_inputs])
            else:
                conv_output = lm_inputs
        if self.use_lstm:
            conv_output = kl.Bidirectional(
                kl.LSTM(self.lstm_units, return_sequences=True))(conv_output)
        if self.dense_output_units:
            pre_last_output = kl.TimeDistributed(
                kl.Dense(self.dense_output_units, activation="relu"),
                name="pre_output")(conv_output)
        else:
            pre_last_output = conv_output
        # финальный слой с softmax-активацией, чтобы получить распределение вероятностей
        output = kl.TimeDistributed(
            kl.Dense(self.target_symbols_number_, activation="softmax"), name="output")(pre_last_output)
        model = Model(inputs, [output])
        model.compile(optimizer=adam(clipnorm=5.0),
                      loss="categorical_crossentropy", metrics=["accuracy"])
        return model

    def _train_models(self, data_by_buckets, targets_by_buckets,
                      dev_data_by_buckets=None, dev_targets_by_buckets=None,
                      model_file=None, verbose=True):
        """
        data_by_buckets: list of lists of np.arrays,
            data_by_buckets[i] = [..., bucket_i, ...],
            bucket = [input_1, ..., input_k],
            input_j --- j-ый вход нейронной сети, вычисленный для текущей корзины
        targets_by_buckets: list of np.arrays,
            targets_by_buckets[i] --- закодированные ответы для i-ой корзины
        model_file: str or None, путь к файлу для сохранения модели
        """
        train_indexes_by_buckets, dev_indexes_by_buckets = [], []
        if dev_data_by_buckets is not None:
            train_indexes_by_buckets = [list(range(len(bucket[0]))) for bucket in data_by_buckets]
            for elem in train_indexes_by_buckets:
                np.random.shuffle(elem)
            dev_indexes_by_buckets = [list(range(len(bucket[0]))) for bucket in dev_data_by_buckets]
            train_data, dev_data = data_by_buckets, dev_data_by_buckets
            train_targets, dev_targets = targets_by_buckets, dev_targets_by_buckets
            # train_data, dev_data = data_by_buckets, data_by_buckets[:]
            # train_targets, dev_targets = targets_by_buckets, targets_by_buckets[:]
        else:
            for bucket in data_by_buckets:
                # разбиваем каждую корзину на обучающую и валидационную выборку
                L = len(bucket[0])
                indexes_for_bucket = list(range(L))
                np.random.shuffle(indexes_for_bucket)
                train_bucket_length = int(L*(1.0 - self.validation_split))
                train_indexes_by_buckets.append(indexes_for_bucket[:train_bucket_length])
                dev_indexes_by_buckets.append(indexes_for_bucket[train_bucket_length:])
       # разбиваем на батчи обучающую и валидационную выборку
        # (для валидационной этого можно не делать, а подавать сразу корзины)
        train_batches_indexes = list(chain.from_iterable(
            [[(i, elem[j:j+self.batch_size]) for j in range(0, len(elem), self.batch_size)]
             for i, elem in enumerate(train_indexes_by_buckets)]))
        # поскольку функции fit_generator нужен генератор, порождающий batch за batch'ем,
        # то приходится заводить генераторы для обеих выборок
        train_gen = DataGenerator(train_data, train_targets, train_batches_indexes,
                                  classes_number=self.target_symbols_number_, shuffle=True)
        if dev_data_by_buckets is not None:
            dev_batches_indexes = list(chain.from_iterable(
                [[(i, elem[j:j + self.batch_size]) for j in range(0, len(elem), self.batch_size)]
                 for i, elem in enumerate(dev_indexes_by_buckets)]))
            val_gen = DataGenerator(dev_data, dev_targets, dev_batches_indexes,
                                    classes_number=self.target_symbols_number_, shuffle=False)
            validation_steps = val_gen.steps_per_epoch
        else:
            val_gen, validation_steps = None, None
        for i, model in enumerate(self.models_):
            if model_file is not None:
                curr_model_file = make_model_file(model_file, i+1)
                # для сохранения модели с наилучшим результатом на валидационной выборке
                save_best_only = (val_gen is not None)
                save_callback = ModelCheckpoint(curr_model_file, save_weights_only=True,
                                                save_best_only=True, monitor="val_acc")
                curr_callbacks = self.callbacks + [save_callback]
            else:
                curr_callbacks = self.callbacks
            model.fit_generator(train_gen, train_gen.steps_per_epoch, verbose=int(verbose),
                                epochs=self.nepochs, callbacks=curr_callbacks,
                                validation_data=val_gen, validation_steps=validation_steps)
            if model_file is not None:
                model.load_weights(curr_model_file)
        return self

    def _memorize_morphemes(self, words, targets):
        """
        запоминает морфемы. встречающиеся в словах обучающей выборки
        """
        morphemes = defaultdict(lambda: defaultdict(int))
        for word, target in zip(words, targets):
            start = None
            for i, (symbol, label) in enumerate(zip(word, target)):
                if label.startswith("B-"):
                    start = i
                elif label.startswith("E-"):
                    dest = extract_morpheme_type(label)
                    morphemes[dest][word[start:i+1]] += 1
                elif label.startswith("S-"):
                    dest = extract_morpheme_type(label)
                    morphemes[dest][word[i]] += 1
                elif label == END:
                    break
        self.morphemes_ = dict()
        for key, counts in morphemes.items():
            self.morphemes_[key] = [x for x, count in counts.items() if count >= self.min_morpheme_count]
        self._make_morpheme_tries()
        if self.to_memorize_ngram_counts:
            self._memorize_ngram_counts(words, morphemes)
        return self

    def _memorize_ngram_counts(self, words, counts):
        """
        запоминает частоты морфем, встречающихся в словах обучающей выборки
        """
        prefix_counts, suffix_counts, ngram_counts  = defaultdict(int), defaultdict(int), defaultdict(int)
        for i, word in enumerate(words, 1):
            if i % 5000 == 0:
                print("{} words processed".format(i))
            positions = self.morpheme_trie_.find_substrings(word, return_positions=True)
            for starts, end in positions:
                for start in starts:
                    segment = word[start:end]
                    ngram_counts[segment] += 1
                    if start == 0:
                        prefix_counts[segment] += 1
                    if end == len(word):
                        suffix_counts[segment] += 1
        self.morpheme_counts_ = dict()
        for key, curr_counts in counts.items():
            curr_relative_counts = dict()
            curr_ngram_counts = (prefix_counts if key == "pref" else
                                 suffix_counts if key in ["end", "postfix"] else ngram_counts)
            for ngram, count in curr_counts.items():
                if count < self.min_morpheme_count or ngram not in curr_ngram_counts:
                    continue
                relative_count = min(count / curr_ngram_counts[ngram], 1.0)
                if relative_count >= self.min_relative_ngram_count:
                    curr_relative_counts[ngram] = relative_count
            self.morpheme_counts_[key] = curr_relative_counts
        return self

    def _make_morpheme_tries(self):
        """
        строит префиксный бор для морфем для более быстрого их поиска
        """
        self.left_morphemes_, self.right_morphemes_ = dict(), dict()
        if self.use_morpheme_types:
            for key in self.LEFT_MORPHEME_TYPES:
                self.left_morphemes_[key] = make_trie(list(self.morphemes_.get(key, [])))
            for key in self.RIGHT_MORPHEME_TYPES:
                self.right_morphemes_[key] = make_trie([x[::-1] for x in self.morphemes_.get(key, [])])
        if not self.use_morpheme_types or self.to_memorize_ngram_counts:
            morphemes = {x for elem in self.morphemes_.values() for x in elem}
            self.morpheme_trie_ = make_trie(list(morphemes))
        return self

    def _predict_label_probs(self, words):
        data_by_buckets, indexes_by_buckets = self._preprocess(words)
        word_probs = [None] * len(words)
        for r, (bucket_data, (_, bucket_indexes)) in \
                enumerate(zip(data_by_buckets, indexes_by_buckets), 1):
            # print("Bucket {} predicting".format(r))
            bucket_probs = np.mean([model.predict(bucket_data) for model in self.models_], axis=0)
            for i, elem in zip(bucket_indexes, bucket_probs):
                word_probs[i] = elem
        return word_probs

    def _predict_probs(self, words):
        """
        data = [word_1, ..., word_m]

        Возвращает:
        -------------
        answer = [probs_1, ..., probs_m]
        probs_i = [p_1, ..., p_k], k = len(word_i)
        p_j = [p_j1, ..., p_jr], r --- число классов
        (len(AUXILIARY) + 4 * 4 (BMES; PREF, ROOT, SUFF, END) + 3 (BME; POSTFIX) + 2 * 1 (S; LINK, HYPHEN) = 23)
        """
        word_probs = self._predict_label_probs(words)
        answer = [None] * len(words)
        for i, (elem, word) in enumerate(zip(word_probs, words)):
            if i % 1000 == 0 and i > 0:
                print("{} words decoded".format(i))
            answer[i] = self._decode_best(elem, len(word))
        return answer

    def labels_to_morphemes(self, word, labels, probs=None, return_probs=False, return_types=False):
        """

        Преобразует ответ из формата BMES в список морфем
        Дополнительно может возвращать список вероятностей морфем

        word: str, текущее слово,
        labels: list of strs, предсказанные метки в формате BMES,
        probs: list of floats or None, предсказанные вероятности меток

        answer = [morphemes, (morpheme_probs), (morpheme_types)]
            morphemes: list of strs, список морфем
            morpheme_probs: list of floats, список вероятностей морфем
            morpheme_types: list of strs, список типов морфем
        """
        morphemes, curr_morpheme, morpheme_types = [], "", []
        if self.use_morpheme_types:
            end_labels = ['E-ROOT', 'E-PREF', 'E-SUFF', 'E-END', 'E-POSTFIX', 'S-ROOT',
                          'S-PREF', 'S-SUFF', 'S-END', 'S-LINK', 'S-HYPH']
        else:
            end_labels = ['E-None', 'S-None']
        for letter, label in zip(word, labels):
            curr_morpheme += letter
            if label in end_labels:
                morphemes.append(curr_morpheme)
                curr_morpheme = ""
                morpheme_types.append(label.split("-")[-1])
        if return_probs:
            if probs is None:
                Warning("Для вычисления вероятностей морфем нужно передать вероятности меток")
                return_probs = False
        if return_probs:
            morpheme_probs, curr_morpheme_prob = [], 1.0
            for label, prob in zip(labels, probs):
                curr_morpheme_prob *= prob[self.target_symbol_codes_[label]]
                if label in end_labels:
                    morpheme_probs.append(curr_morpheme_prob)
                    curr_morpheme_prob = 1.0
            answer = [morphemes, morpheme_probs]
        else:
            answer = [morphemes]
        if return_types:
            answer.append(morpheme_types)
        return answer

    def predict(self, words, return_probs=False):
        labels_with_probs = self._predict_probs(words)
        return [self.labels_to_morphemes(word, elem[0], elem[1], return_probs=return_probs)
                for elem, word in zip(labels_with_probs, words)]

    def _decode_best(self, probs, length):
        """
        Поддерживаем в каждой позиции наилучшие гипотезы для каждого состояния
        Состояние --- последняя предсказанняя метка
        """
        # вначале нужно проверить заведомо наилучший вариант на корректность
        best_states = np.argmax(probs[:1+length], axis=1)
        best_labels = [self.target_symbols_[state_index] for state_index in best_states]
        if not is_correct_morpheme_sequence(best_labels[1:]):
            # наилучший вариант оказался некорректным
            initial_costs = [np.inf] * self.target_symbols_number_
            initial_states = [None] * self.target_symbols_number_
            initial_costs[BEGIN], initial_states[BEGIN] = -np.log(probs[0, BEGIN]), BEGIN
            costs, states = [initial_costs], [initial_states]
            for i in range(length):
                # состояний мало, поэтому можно сортировать на каждом шаге
                state_order = np.argsort(costs[-1])
                curr_costs = [np.inf] * self.target_symbols_number_
                prev_states = [None] * self.target_symbols_number_
                inf_count = self.target_symbols_number_
                for prev_state in state_order:
                    if np.isinf(costs[-1][prev_state]):
                        break
                    elif prev_state in AUXILIARY_CODES and i != 0:
                        continue
                    possible_states = self.get_possible_next_states(prev_state)
                    for state in possible_states:
                        if np.isinf(curr_costs[state]):
                            # поскольку новая вероятность не зависит от state,
                            # а старые перебираются по возрастанию штрафа,
                            # то оптимальное значение будет при первом обновлении
                            curr_costs[state] = costs[-1][prev_state] - np.log(probs[i+1,state])
                            prev_states[state] = prev_state
                            inf_count -= 1
                    if inf_count == len(AUXILIARY_CODES):
                        # все вероятности уже посчитаны
                        break
                costs.append(curr_costs)
                states.append(prev_states)
            # последнее состояние --- обязательно конец морфемы
            possible_states = [self.target_symbol_codes_["{}-{}".format(x, y)]
                               for x in "ES" for y in ["ROOT", "SUFF", "END", "POSTFIX", "None"]
                               if "{}-{}".format(x, y) in self.target_symbol_codes_]
            best_states = [min(possible_states, key=(lambda x: costs[-1][x]))]
            for j in range(length, 0, -1):
                # предыдущее состояние
                best_states.append(states[j][best_states[-1]])
            best_states = best_states[::-1]
        probs_to_return = np.zeros(shape=(length, self.target_symbols_number_), dtype=np.float32)
        # убираем невозможные состояния
        for j, state in enumerate(best_states[:-1]):
            possible_states = self.get_possible_next_states(state)
            # оставляем только возможные состояния.
            probs_to_return[j,possible_states] = probs[j+1,possible_states]
        return [self.target_symbols_[i] for i in best_states[1:]], probs_to_return

    def prob(self, words, morphemes, morph_types=None):
        if morph_types is None:
            morph_types = [None] * len(words)
        label_probs = self._predict_label_probs(words)
        answer = []
        for curr_words, curr_morphemes, curr_morph_types, probs in zip(words, morphemes, morph_types, label_probs):
            if isinstance(curr_morphemes[0], list):
                curr_morphemes, curr_morph_types = curr_morphemes
            start = 1
            curr_answer = []
            for i, morph in enumerate(curr_morphemes):
                # if isinstance(morph, tuple):
                #     morph, possible_morph_types = morph[0], [morph[1]]
                if curr_morph_types is None:
                    possible_morph_types = self.target_types_
                else:
                    possible_morph_types = [curr_morph_types[i]]
                if len(morph) > 1:
                    labels = "B" + "M" * (len(morph) - 2) + "E"
                else:
                    labels = "S"
                possible_morph_seqs = []
                for morph_type in possible_morph_types:
                    curr_seq = []
                    for label in labels:
                        label_code = self.target_symbol_codes_.get("{}-{}".format(label, morph_type))
                        if label_code is not None:
                            curr_seq.append(label_code)
                        else:
                            break
                    else:
                        possible_morph_seqs.append(curr_seq)
                possible_probs = [[probs[start+i,code] for i, code in enumerate(seq)]
                                  for seq in possible_morph_seqs]
                possible_total_probs = [-sum(np.log(x)) for x in possible_probs]
                index = np.argmin(possible_total_probs)
                curr_answer.append(np.exp(-possible_total_probs[index]))
                start += len(morph)
            answer.append(curr_answer)
        return answer



    def get_possible_next_states(self, state_index):
        state = self.target_symbols_[state_index]
        next_states = get_next_morpheme(state)
        return [self.target_symbol_codes_[x] for x in next_states if x in self.target_symbol_codes_]


def make_model_file(name, i):
    pos = name.rfind(".")
    if pos != -1:
        return "{}-{}.{}".format(name[:pos], i, name[pos+1:])
    else:
        return "{}-{}".format(name, i)

PREF, ROOT, LINK, SUFF, ENDING, POSTFIX, HYPH, FINAL = 0, 1, 2, 3, 4, 5, 6, 7
MORPHEME_TYPES = ["PREF", "ROOT", "LINK", "END", "POSTFIX", "HYPH"]

def get_next_morpheme_types(morpheme_type):
    """
    Определяет, какие морфемы могут идти за текущей.
    """
    if morpheme_type == "None":
        return ["None"]
    MORPHEMES = ["SUFF", "END", "LINK", "POSTFIX", "PREF", "ROOT"]
    if morpheme_type in ["ROOT", "SUFF", "HYPH"]:
        start = 0
    elif morpheme_type == "END":
        start = 2
    elif morpheme_type in ["PREF", "LINK", "BEGIN"]:
        start = 4
    else:
        start = 6
    answer = MORPHEMES[start:6]
    if len(answer) > 0 and morpheme_type != "HYPH":
        answer.append("HYPH")
    if morpheme_type == "BEGIN":
        answer.append("None")
    return answer


def get_next_morpheme(morpheme):
    """
    Строит список меток, которые могут идти за текущей
    """
    if morpheme == "END":
        return []
    if morpheme == "BEGIN":
        morpheme = "S-BEGIN"
    morpheme_label, morpheme_type = morpheme.split("-")
    if morpheme_label in "BM":
        new_morpheme_labels = "ME"
        new_morpheme_types = [morpheme_type]
    else:
        new_morpheme_labels = "BS"
        new_morpheme_types = get_next_morpheme_types(morpheme_type)
    answer = ["{}-{}".format(x, y) for x in new_morpheme_labels for y in new_morpheme_types]
    return answer


def is_correct_morpheme_sequence(morphemes):
    """
    Проверяет список морфемных меток на корректность
    """
    if morphemes == []:
        return False
    if any("-" not in morpheme for morpheme in morphemes):
        return False
    morpheme_label, morpheme_type = morphemes[0].split("-")
    if morpheme_label not in "BS" or morpheme_type not in ["PREF", "ROOT", "None"]:
        return False
    morpheme_label, morpheme_type = morphemes[-1].split("-")
    if morpheme_label not in "ES" or morpheme_type not in ["ROOT", "SUFF", "ENDING", "POSTFIX", "None"]:
        return False
    for i, morpheme in enumerate(morphemes[:-1]):
        if morphemes[i+1] not in get_next_morpheme(morpheme):
            return False
    return True


class Reversed(kl.Wrapper):

    def __init__(self, layer, **kwargs):
        super(Reversed, self).__init__(layer, **kwargs)

    def build(self, input_shape):
        assert len(input_shape) >= 2
        self.input_spec = InputSpec(shape=input_shape)
        if not self.layer.built:
            self.layer.build(input_shape)
            self.layer.built = True
        super(Reversed, self).build()

    def call(self, inputs, **kwargs):
        reversed_inputs = kb.reverse(inputs, axes=1)
        reversed_answer = self.layer.call(reversed_inputs, **kwargs)
        answer = kb.reverse(reversed_answer, axes=1)
        return answer

    def compute_output_shape(self, input_shape):
        return self.layer.compute_output_shape(input_shape)

class MultitaskPartitioner(Partitioner):

    """
    models_number: int, default=1, число моделей
    to_memorize_morphemes: bool, default=False,
        производится ли запоминание морфемных энграмм
    min_morpheme_count: int, default=2,
        минимальное количество раз, которое должна встречаться запоминаемая морфема
    to_memorize_ngram_counts: bool, default=False,
        используются ли частоты энграмм как морфем при вычислении признаков
    min_relative_ngram_count: float, default=0.1,
        минимальное отношение частоты энграммы как морфемы к её общей частоте,
        необходимое для её запоминания
    use_embeddings: bool, default=False,
        используется ли дополнительный слой векторных представлений символов
    embeddings_size: int, default=32, размер символьного представления
    conv_layers: int, default=1, число свёрточных слоёв
    window_size: int or list of ints, список размеров окна в свёрточном слое
    filters_number: int or list of ints or list of list of ints,
        число фильтров в свёрточных слоях,
        filters_number[i,j] --- число фильтров для i-го окна j-го слоя,
        если задан список, то filters_number[j] --- число фильтров в окнах j-го слоя,
        если число --- то одно и то же число фильтров для всех слоёв и окон
    dense_output_units: int, default=0,
        число нейронов на дополнительном слое перед вычислением выходных вероятностей.
        если 0, то этот слой отсутствует
    use_lstm: bool, default=False,
        используется ли дополнительный выходной слой LSTM (ухудшает качество)
    lstm_units: int, default=64, число нейронов в LSTM-слое
    dropout: float, default=0.0
        доля выкидываемых нейронов в dropout-слое, помогает бороться с переобучением
    context_dropout: float, default=0.0,
        вероятность маскировки векторного представления контекста
    buckets_number: int, default=10,
        число корзин, в одну корзину попадают данные примерно одинаковой длины
    nepochs: int, default=10, число эпох в обучении
    validation_split: float, default=0.2, доля элементов в развивающей выборке
    batch_size: int, default=32, число элементов в одном батче
    callbacks: list of keras.callbacks or None, default=None,
        коллбэки для управления процессом обучения,
    early_stopping: int, default=None,
        число эпох, в течение которого не должно улучшаться качество
        на валидационной выборке, чтобы обучение остановилось,
        если None, то в любом случае модель обучается nepochs эпох
    """

    LEFT_MORPHEME_TYPES = ["pref", "root"]
    RIGHT_MORPHEME_TYPES = ["root", "suff", "end", "postfix"]

    def __init__(self, use_lm=False, **kwargs):
        self.use_lm = use_lm
        super(MultitaskPartitioner, self).__init__(**kwargs)

    def to_json(self, outfile, model_file=None, language_model_file=None):
        info = dict()
        if model_file is None:
            pos = outfile.rfind(".")
            model_file = outfile[:pos] + ("-model.hdf5" if pos != -1 else "-model")
        model_files = [make_model_file(model_file, i+1) for i in range(self.models_number)]
        if language_model_file is not None:
            language_model_files = [make_model_file(language_model_file, i + 1)
                                    for i in range(self.models_number)]
        for i in range(self.models_number):
            # при сохранении нужен абсолютный путь, а не от текущей директории
            model_files[i] = os.path.abspath(model_files[i])
            if language_model_file is not None:
                language_model_files[i] = os.path.abspath(language_model_files[i])
        for (attr, val) in inspect.getmembers(self):
            # перебираем поля класса и сохраняем только задаваемые при инициализации
            if not (attr.startswith("__") or inspect.ismethod(val) or
                    isinstance(getattr(Partitioner, attr, None), property) or
                    attr.isupper() or attr in self.BAD_FIELDS):
                info[attr] = val
            elif attr == "models_":
                # для каждой модели сохраняем веса
                info["model_files"] = model_files
                for model, curr_model_file in zip(self.models_, model_files):
                    model.save_weights(curr_model_file)
            elif attr == "language_models_":
                info["language_model_files"] = language_model_files
                for model, curr_model_file in zip(self.language_models_, language_model_files):
                    model.save_weights(curr_model_file)
        info["cls"] = "MultitaskPartitioner"
        with open(outfile, "w", encoding="utf8") as fout:
            json.dump(info, fout)

    def _preprocess(self, data, targets=None):
        # к каждому слову добавляются символы начала и конца строки
        transformed_data = [[self._encode(elem, self.symbol_codes_) for elem in data]]
        if self.to_memorize_morphemes:
            morpheme_data = [self._morpheme_memo_func([elem], len(elem)+2)[0] for elem in data]
            transformed_data.append(morpheme_data)
        if targets is not None:
            targets = [self._encode(elem, self.target_symbol_codes_) for elem in targets]
            return transformed_data, targets
        return transformed_data

    def _encode(self, seq, encoding):
        answer = np.full(shape=(len(seq)+2,), fill_value=PAD, dtype=int)
        answer[0] = BEGIN
        answer[1:1 + len(seq)] = [encoding.get(x, UNKNOWN) for x in seq]
        answer[1 + len(seq)] = END
        return answer

    def _make_bucket_data(self, data, bucket_length, bucket_indexes, is_target=False):
        """
        data: list of lists, исходные данные
        bucket_length: int, максимальная длина элемента в корзине
        bucket_indexes: list of ints, индексы элементов в корзине
        is_target: boolean, default=False,
            являются ли данные исходными или ответами

        answer = [symbols, (classes)],
            symbols: array of shape (len(data), bucket_length)
                элементы data, дополненные символом PAD справа до bucket_length
            classes: array of shape (len(data), classes_number)
        """
        bucket_data = [data[i] for i in bucket_indexes]
        if is_target:
            return self._recode_bucket_data(bucket_data, bucket_length, self.target_symbol_codes_)
        else:
            answer = [self._recode_bucket_data(bucket_data, bucket_length, self.symbol_codes_)]
            if self.to_memorize_morphemes:
                # print("Processing morphemes for bucket length", bucket_length)
                answer.append(self._morpheme_memo_func(bucket_data, bucket_length))
            if self.lm_embedder is not None:
                answer.append(self._make_lm_embeddings_data(bucket_data, bucket_length))
                # answer = [self._make_lm_embeddings_data(bucket_data, bucket_length)]
            return answer

    def _recode_bucket_data(self, data, bucket_length, encoding):
        answer = np.full(shape=(len(data), bucket_length), fill_value=PAD, dtype=int)
        answer[:,0] = BEGIN
        for j, word in enumerate(data):
            answer[j,1:1+len(word)] = [encoding.get(x, UNKNOWN) for x in word]
            answer[j,1+len(word)] = END
        return answer

    def _make_morpheme_data(self, data, bucket_length):
        """
        строит для каждой позиции во входных словах вектор, кодирующий энграммы в контексте

        data: list of strs, список исходных слов
        bucket_length: int, максимальная длина слова в корзине

        answer: np.array[float] of shape (len(data), bucket_length, 15)
        """
        answer = np.zeros(shape=(len(data), bucket_length, 15), dtype=float)
        for j, word in enumerate(data):
            m = len(word)
            curr_answer = np.zeros(shape=(bucket_length, 15), dtype=int)
            root_starts = [0]
            ending_ends = [m]
            prefixes = self.left_morphemes_["pref"].descend_by_prefixes(word[:-1])
            for end in prefixes:
                score = self._get_ngram_score(word[:end], "pref")
                if end == 1:
                    curr_answer[1,10] = max(score, curr_answer[1,10])
                else:
                    curr_answer[1,0] = max(score, curr_answer[1,0])
                    curr_answer[end, 5] = max(score, curr_answer[end, 5])
            root_starts += prefixes
            postfix_lengths = self.right_morphemes_["postfix"].descend_by_prefixes(word[:0:-1])
            for k in postfix_lengths:
                score = self._get_ngram_score(word[-k:], "postfix")
                if k == 1:
                    curr_answer[m, 14] = max(score, curr_answer[m, 14])
                else:
                    curr_answer[m, 9] = max(score, curr_answer[m, 9])
                    curr_answer[m-k+1,4] = max(score, curr_answer[m-k+1,4])
                ending_ends.append(m-k)
            suffix_ends = set(ending_ends)
            for end in ending_ends[::-1]:
                ending_lengths = self.right_morphemes_["end"].descend_by_prefixes(word[end-1:0:-1])
                for k in ending_lengths:
                    score = self._get_ngram_score(word[end-k:end], "end")
                    if k == 1:
                        curr_answer[end, 13] = max(score, curr_answer[end, 13])
                    else:
                        curr_answer[end-k+1, 3] = max(score, curr_answer[end-k+1, 3])
                        curr_answer[end, 8] = max(score, curr_answer[end, 8])
                    suffix_ends.add(end-k)
            suffixes = self.right_morphemes_["suff"].descend_by_prefixes(
                word[::-1], start_pos=[m-k for k in suffix_ends], max_count=3, return_pairs=True)
            suffix_starts = suffix_ends
            for first, last in suffixes:
                score = self._get_ngram_score(word[m-last:m-first], "suff")
                if last == first + 1:
                    curr_answer[m-first, 12] = max(score, curr_answer[m-first, 12])
                else:
                    curr_answer[m-last+1, 2] = max(score, curr_answer[m-last+1, 2])
                    curr_answer[m-first, 7] = max(score, curr_answer[m-first, 7])
                suffix_starts.add(m-last)
            for start in root_starts:
                root_ends = self.left_morphemes_["root"].descend_by_prefixes(word[start:])
                for end in root_ends:
                    score = self._get_ngram_score(word[start:end], "root")
                    if end == start+1:
                        curr_answer[start + 1, 11] = max(score, curr_answer[start + 1, 11])
                    else:
                        curr_answer[start + 1, 1] = max(score, curr_answer[start + 1, 1])
                        curr_answer[end, 6] = max(score, curr_answer[end, 6])
            for end in suffix_starts:
                root_lengths = self.right_morphemes_["root"].descend_by_prefixes(word[end-1:-1:-1])
                for k in root_lengths:
                    score = self._get_ngram_score(word[end-k:end], 'root')
                    if k == 1:
                        curr_answer[end, 11] = max(curr_answer[end, 11], score)
                    else:
                        curr_answer[end-k+1, 1] = max(curr_answer[end-k+1, 1], score)
                        curr_answer[end, 6] = max(curr_answer[end, 6], score)
            answer[j] = curr_answer
        return answer

    def _make_morpheme_data_simple(self, data, bucket_length):
        answer = np.zeros(shape=(len(data), bucket_length, 3), dtype=float)
        for j, word in enumerate(data):
            m = len(word)
            curr_answer = np.zeros(shape=(bucket_length, 3), dtype=int)
            positions = self.morpheme_trie_.find_substrings(word, return_positions=True)
            for starts, end in positions:
                for start in starts:
                    score = self._get_ngram_score(word[start:end])
                    if end == start+1:
                        curr_answer[start+1, 2] = max(curr_answer[start+1, 2], score)
                    else:
                        curr_answer[start+1, 0] = max(curr_answer[start+0, 2], score)
                        curr_answer[end, 1] = max(curr_answer[end, 1], score)
            answer[j] = curr_answer
        return answer

    def _get_ngram_score(self, ngram, mode="None"):
        if self.to_memorize_ngram_counts:
            return self.morpheme_counts_[mode].get(ngram, 0)
        else:
            return 1.0

    def _make_lm_embeddings_data(self, data, length):
        answer = np.zeros(shape=(len(data), length, self.lm_state_size), dtype=float)
        embeddings = self.lm_embedder.transform([[elem] for elem in data])
        for i, elem in enumerate(embeddings):
            answer[i,:len(elem)] = elem
        return answer

    def train(self, data, targets, dev_data=None, dev_targets=None,
              lm_data=None, dev_lm_data=None, model_file=None, verbose=True):
        """

        source: list of strs, список слов для морфемоделения
        targets: list of strs, метки морфемоделения в формате BMES
        model_file: str or None, default=None, файл для сохранения моделей

        Возвращает:
        -------------
        self, обученный морфемоделитель
        """
        data_for_vocabulary = data + lm_data if lm_data is not None else data
        self.symbols_, self.symbol_codes_ = _make_vocabulary(data_for_vocabulary)
        self.target_symbols_, self.target_symbol_codes_ = _make_vocabulary(targets)
        self.target_types_ = [x.split("-")[1] for x in self.target_symbols_ if "-" in x]
        if self.to_memorize_morphemes:
            self._memorize_morphemes(data, targets)

        if dev_data is None:
            indexes = np.arange(len(data))
            np.random.shuffle(indexes)
            level = int(len(data) * self.validation_split)
            dev_data, dev_targets = [data[i] for i in indexes[:level]], [targets[i] for i in indexes[:level]]
            data, targets = [data[i] for i in indexes[level:]], [targets[i] for i in indexes[level:]]
        data, targets = self._preprocess(data, targets)
        dev_data, dev_targets = self._preprocess(dev_data, dev_targets)
        if lm_data is not None:
            lm_data = self._preprocess(lm_data)
        self.build(verbose=verbose)
        self._train_models(data, targets, dev_data, dev_targets, lm_data,
                           model_file=model_file, verbose=verbose)
        return self

    def build(self, verbose=True):
        """
        Создаёт нейронные модели
        """
        self.models_ = []
        if self.use_lm:
            self.language_models_ = []
        for _ in range(self.models_number):
            model, language_model = self.build_model()
            self.models_.append(model)
            if self.use_lm:
                self.language_models_.append(language_model)
        if verbose:
            print(self.models_[0].summary())
            if self.use_lm:
                print(self.language_models_[0].summary())
        return self

    def build_model(self):
        """
        Функция, задающая архитектуру нейронной сети
        """
        # symbol_inputs: array, 1D-массив длины m
        symbol_inputs = kl.Input(shape=(None,), dtype='uint8', name="symbol_inputs")
        # symbol_embeddings: array, 2D-массив размера m*self.symbols_number
        if self.use_embeddings:
            symbol_embeddings = kl.Embedding(self.symbols_number_, self.embeddings_size,
                                             name="symbol_embeddings")(symbol_inputs)
        else:
            symbol_embeddings = kl.Lambda(kb.one_hot, output_shape=(None, self.symbols_number_),
                                          arguments={"num_classes": self.symbols_number_},
                                          name="symbol_embeddings")(symbol_inputs)
        inputs, conv_inputs = [symbol_inputs], [symbol_embeddings]
        if self.to_memorize_morphemes:
            # context_inputs: array, 2D-массив размера m*15
            context_inputs = kl.Input(shape=(None, self.memory_dim), dtype='float32', name="context_inputs")
            inputs.append(context_inputs)
            if self.context_dropout > 0.0:
                context_inputs = kl.Dropout(self.context_dropout)(context_inputs)
            # представление контекста подклеивается к представлению символа
            conv_inputs.append(context_inputs)
        if self.lm_embedder is not None:
            lm_inputs = kl.Input(shape=(None, self.lm_state_size), dtype='float32', name="lm_inputs")
            inputs.append(lm_inputs)
            if self.lm_inputs_to_conv:
                conv_inputs.append(lm_inputs)
        if len(conv_inputs) == 1:
            conv_inputs = conv_inputs[0]
        else:
            conv_inputs = kl.Concatenate()(conv_inputs)
        conv_outputs = []
        for window_size, curr_filters_numbers in zip(self.window_size, self.filters_number):
            # свёрточный слой отдельно для каждой ширины окна
            for direction in ["left", "right"]:
                curr_conv_input = conv_inputs
                for j, filters_number in enumerate(curr_filters_numbers[:-1]):
                    # все слои свёртки, кроме финального (после них возможен dropout)
                    layer = kl.Conv1D(filters_number, window_size, activation="relu", padding="causal")
                    if direction == "right":
                        layer = Reversed(layer)
                    curr_conv_input = layer(curr_conv_input)
                    # curr_conv_input = kl.BatchNormalization()(curr_conv_input)
                    if self.dropout > 0.0:
                        # между однотипными слоями рекомендуется вставить dropout
                        curr_conv_input = kl.Dropout(self.dropout)(curr_conv_input)
                if not self.use_lstm:
                    layer = kl.Conv1D(curr_filters_numbers[-1], window_size, activation="relu", padding="causal")
                    if direction == "right":
                        layer = Reversed(layer)
                    curr_conv_output = layer(curr_conv_input)
                else:
                    curr_conv_output = curr_conv_input
                conv_outputs.append(curr_conv_output)
        # соединяем выходы всех свёрточных слоёв в один вектор
        if len(conv_outputs) == 2:
            left_conv_output, right_conv_output = conv_outputs
        else:
            left_conv_output = kl.Concatenate(name="left_conv_output")(conv_outputs[::2])
            right_conv_output = kl.Concatenate(name="right_conv_output")(conv_outputs[1::2])
        conv_output = kl.Concatenate(name="conv_output")([left_conv_output, right_conv_output])
        if self.use_lstm:
            conv_output = kl.Bidirectional(
                kl.LSTM(self.lstm_units, return_sequences=True))(conv_output)
        if self.dense_output_units:
            pre_last_output = kl.TimeDistributed(
                kl.Dense(self.dense_output_units, activation="relu"),
                name="pre_output")(conv_output)
        else:
            pre_last_output = conv_output
        # финальный слой с softmax-активацией, чтобы получить распределение вероятностей
        output = kl.TimeDistributed(
            kl.Dense(self.target_symbols_number_, activation="softmax"), name="output")(pre_last_output)
        # выход языковой модели
        model = Model(inputs, [output])
        model.compile(optimizer=adam(clipnorm=5.0),
                      loss="categorical_crossentropy", metrics=["accuracy"])
        if self.use_lm:
            next_symbol_output = kl.TimeDistributed(kl.Dense(
                self.symbols_number_, activation="softmax"), name="next_output")(left_conv_output)
            prev_symbol_output = kl.TimeDistributed(kl.Dense(
                self.symbols_number_, activation="softmax"), name="prev_output")(right_conv_output)
            language_model = Model(inputs, [next_symbol_output, prev_symbol_output])
            language_model.compile(optimizer=adam(clipnorm=5.0), loss="categorical_crossentropy")
        else:
            language_model = None
        return model, language_model

    def _train_models(self, data, targets, dev_data, dev_targets,
                      lm_data=None, model_file=None, verbose=True):
        for i in range(self.models_number):
            models, epochs = [self.models_[i]], [self.nepochs]
            lm_shifts = None
            target_vocabulary_size = [[self.target_symbols_number_]]
            if self.use_lm:
                models.append(self.language_models_[i])
                epochs.append(self.lm_epochs)
                lm_data, lm_shifts = [lm_data], [[1, -1]]
                target_vocabulary_size.append([self.symbols_number_, self.symbols_number_])
            train_gen = MultitaskDataGenerator(
                [data], [[targets]], lm_data=lm_data, lm_shifts=lm_shifts,
                target_vocabulary_size=target_vocabulary_size,
                batch_size=self.batch_size, epochs=epochs)
            dev_gen = SimpleDataGenerator(dev_data, [dev_targets],
                                          # data_vocabulary_size=[self.symbols_number_],
                                          target_vocabulary_size=[self.target_symbols_number_],
                                          batch_size=self.batch_size, shuffle=False)
            trainer = MultimodelTrainer(models, epochs=epochs,
                                        progbar_model_index=0, dev_model_index=0,
                                        early_stopping=self.early_stopping)
            trainer.train(train_gen, dev_gen)
        return self

    def _predict_label_probs(self, words):
        data = self._preprocess(words)
        word_probs = [None] * len(words)
        test_gen = SimpleDataGenerator(data, yield_indexes=True, shuffle=False, epochs=1)
        for batch_data, indexes in test_gen:
            batch_probs = np.mean([model.predict(batch_data) for model in self.models_], axis=0)
            for i, elem in zip(indexes, batch_probs):
                word_probs[i] = elem
        # for r, (bucket_data, (_, bucket_indexes)) in \
        #         enumerate(zip(data_by_buckets, indexes_by_buckets), 1):
        #     # print("Bucket {} predicting".format(r))
        #     bucket_probs = np.mean([model.predict(bucket_data) for model in self.models_], axis=0)
        #     for i, elem in zip(bucket_indexes, bucket_probs):
        #         word_probs[i] = elem
        return word_probs

    def _predict_probs(self, words):
        """
        data = [word_1, ..., word_m]

        Возвращает:
        -------------
        answer = [probs_1, ..., probs_m]
        probs_i = [p_1, ..., p_k], k = len(word_i)
        p_j = [p_j1, ..., p_jr], r --- число классов
        (len(AUXILIARY) + 4 * 4 (BMES; PREF, ROOT, SUFF, END) + 3 (BME; POSTFIX) + 2 * 1 (S; LINK, HYPHEN) = 23)
        """
        word_probs = self._predict_label_probs(words)
        answer = [None] * len(words)
        for i, (elem, word) in enumerate(zip(word_probs, words)):
            if i % 1000 == 0 and i > 0:
                print("{} words decoded".format(i))
            answer[i] = self._decode_best(elem, len(word))
        return answer

    def labels_to_morphemes(self, word, labels, probs=None, return_probs=False, return_types=False):
        """

        Преобразует ответ из формата BMES в список морфем
        Дополнительно может возвращать список вероятностей морфем

        word: str, текущее слово,
        labels: list of strs, предсказанные метки в формате BMES,
        probs: list of floats or None, предсказанные вероятности меток

        answer = [morphemes, (morpheme_probs), (morpheme_types)]
            morphemes: list of strs, список морфем
            morpheme_probs: list of floats, список вероятностей морфем
            morpheme_types: list of strs, список типов морфем
        """
        morphemes, curr_morpheme, morpheme_types = [], "", []
        if self.use_morpheme_types:
            end_labels = ['E-ROOT', 'E-PREF', 'E-SUFF', 'E-END', 'E-POSTFIX', 'S-ROOT',
                          'S-PREF', 'S-SUFF', 'S-END', 'S-LINK', 'S-HYPH']
        else:
            end_labels = ['E-None', 'S-None']
        for letter, label in zip(word, labels):
            curr_morpheme += letter
            if label in end_labels:
                morphemes.append(curr_morpheme)
                curr_morpheme = ""
                morpheme_types.append(label.split("-")[-1])
        if return_probs:
            if probs is None:
                Warning("Для вычисления вероятностей морфем нужно передать вероятности меток")
                return_probs = False
        if return_probs:
            morpheme_probs, curr_morpheme_prob = [], 1.0
            for label, prob in zip(labels, probs):
                curr_morpheme_prob *= prob[self.target_symbol_codes_[label]]
                if label in end_labels:
                    morpheme_probs.append(curr_morpheme_prob)
                    curr_morpheme_prob = 1.0
            answer = [morphemes, morpheme_probs]
        else:
            answer = [morphemes]
        if return_types:
            answer.append(morpheme_types)
        return answer

    def predict(self, words, return_probs=False):
        labels_with_probs = self._predict_probs(words)
        return [self.labels_to_morphemes(word, elem[0], elem[1], return_probs=return_probs)
                for elem, word in zip(labels_with_probs, words)]

    def _decode_best(self, probs, length):
        """
        Поддерживаем в каждой позиции наилучшие гипотезы для каждого состояния
        Состояние --- последняя предсказанняя метка
        """
        # вначале нужно проверить заведомо наилучший вариант на корректность
        best_states = np.argmax(probs[:1+length], axis=1)
        best_labels = [self.target_symbols_[state_index] for state_index in best_states]
        if not is_correct_morpheme_sequence(best_labels[1:]):
            # наилучший вариант оказался некорректным
            initial_costs = [np.inf] * self.target_symbols_number_
            initial_states = [None] * self.target_symbols_number_
            initial_costs[BEGIN], initial_states[BEGIN] = -np.log(probs[0, BEGIN]), BEGIN
            costs, states = [initial_costs], [initial_states]
            for i in range(length):
                # состояний мало, поэтому можно сортировать на каждом шаге
                state_order = np.argsort(costs[-1])
                curr_costs = [np.inf] * self.target_symbols_number_
                prev_states = [None] * self.target_symbols_number_
                inf_count = self.target_symbols_number_
                for prev_state in state_order:
                    if np.isinf(costs[-1][prev_state]):
                        break
                    elif prev_state in AUXILIARY_CODES and i != 0:
                        continue
                    possible_states = self.get_possible_next_states(prev_state)
                    for state in possible_states:
                        if np.isinf(curr_costs[state]):
                            # поскольку новая вероятность не зависит от state,
                            # а старые перебираются по возрастанию штрафа,
                            # то оптимальное значение будет при первом обновлении
                            curr_costs[state] = costs[-1][prev_state] - np.log(probs[i+1,state])
                            prev_states[state] = prev_state
                            inf_count -= 1
                    if inf_count == len(AUXILIARY_CODES):
                        # все вероятности уже посчитаны
                        break
                costs.append(curr_costs)
                states.append(prev_states)
            # последнее состояние --- обязательно конец морфемы
            possible_states = [self.target_symbol_codes_["{}-{}".format(x, y)]
                               for x in "ES" for y in ["ROOT", "SUFF", "END", "POSTFIX", "None"]
                               if "{}-{}".format(x, y) in self.target_symbol_codes_]
            best_states = [min(possible_states, key=(lambda x: costs[-1][x]))]
            for j in range(length, 0, -1):
                # предыдущее состояние
                best_states.append(states[j][best_states[-1]])
            best_states = best_states[::-1]
        probs_to_return = np.zeros(shape=(length, self.target_symbols_number_), dtype=np.float32)
        # убираем невозможные состояния
        for j, state in enumerate(best_states[:-1]):
            possible_states = self.get_possible_next_states(state)
            # оставляем только возможные состояния.
            probs_to_return[j,possible_states] = probs[j+1,possible_states]
        return [self.target_symbols_[i] for i in best_states[1:]], probs_to_return

    def prob(self, words, morphemes, morph_types=None):
        if morph_types is None:
            morph_types = [None] * len(words)
        label_probs = self._predict_label_probs(words)
        answer = []
        for curr_words, curr_morphemes, curr_morph_types, probs in zip(words, morphemes, morph_types, label_probs):
            if isinstance(curr_morphemes[0], list):
                curr_morphemes, curr_morph_types = curr_morphemes
            start = 1
            curr_answer = []
            for i, morph in enumerate(curr_morphemes):
                # if isinstance(morph, tuple):
                #     morph, possible_morph_types = morph[0], [morph[1]]
                if curr_morph_types is None:
                    possible_morph_types = self.target_types_
                else:
                    possible_morph_types = [curr_morph_types[i]]
                if len(morph) > 1:
                    labels = "B" + "M" * (len(morph) - 2) + "E"
                else:
                    labels = "S"
                possible_morph_seqs = []
                for morph_type in possible_morph_types:
                    curr_seq = []
                    for label in labels:
                        label_code = self.target_symbol_codes_.get("{}-{}".format(label, morph_type))
                        if label_code is not None:
                            curr_seq.append(label_code)
                        else:
                            break
                    else:
                        possible_morph_seqs.append(curr_seq)
                possible_probs = [[probs[start+i,code] for i, code in enumerate(seq)]
                                  for seq in possible_morph_seqs]
                possible_total_probs = [-sum(np.log(x)) for x in possible_probs]
                index = np.argmin(possible_total_probs)
                curr_answer.append(np.exp(-possible_total_probs[index]))
                start += len(morph)
            answer.append(curr_answer)
        return answer

    def get_possible_next_states(self, state_index):
        state = self.target_symbols_[state_index]
        next_states = get_next_morpheme(state)
        return [self.target_symbol_codes_[x] for x in next_states if x in self.target_symbol_codes_]