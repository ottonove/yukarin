import glob
from pathlib import Path
from typing import Dict, NamedTuple
from typing import List

import chainer
import numpy

from yukarin.acoustic_feature import AcousticFeature
from yukarin.align_indexes import AlignIndexes
from yukarin.config import DatasetConfig


class Inputs(NamedTuple):
    in_feature_path: Path
    out_feature_path: Path
    indexes_path: Path


def encode_feature(data: AcousticFeature, targets: List[str]):
    feature = numpy.concatenate([getattr(data, t) for t in targets], axis=1)
    feature = feature.T
    return feature


def decode_feature(data: numpy.ndarray, targets: List[str], sizes: Dict[str, int]):
    data = data.T

    lasts = numpy.cumsum([sizes[t] for t in targets]).tolist()
    assert data.shape[1] == lasts[-1]

    return AcousticFeature(**{
        t: data[:, bef:aft]
        for t, bef, aft in zip(targets, [0] + lasts[:-1], lasts)
    })


def make_mask(feature: AcousticFeature):
    return AcousticFeature(
        f0=feature.voiced,
        sp=numpy.ones_like(feature.sp, dtype=numpy.bool),
        ap=numpy.ones_like(feature.ap, dtype=numpy.bool),
        coded_ap=numpy.ones_like(feature.coded_ap, dtype=numpy.bool),
        mc=numpy.ones_like(feature.mc, dtype=numpy.bool),
        voiced=numpy.ones_like(feature.voiced, dtype=numpy.bool),
    ).astype(numpy.float32)


def random_pad(data: numpy.ndarray, seed: int, min_size: int, time_axis: int = 1):
    random = numpy.random.RandomState(seed)

    if data.shape[time_axis] >= min_size:
        return data

    pre = random.randint(min_size - data.shape[time_axis] + 1)
    post = min_size - pre
    pad = [(0, 0)] * data.ndim
    pad[time_axis] = (pre, post)
    return numpy.pad(data, pad, mode='constant')


def random_crop(data: numpy.ndarray, seed: int, crop_size: int, time_axis: int = 1):
    random = numpy.random.RandomState(seed)

    len_time = data.shape[time_axis]
    assert len_time >= crop_size

    start = random.randint(len_time - crop_size + 1)
    return numpy.split(data, [start, start + crop_size], axis=time_axis)[1]


def add_noise(data: numpy.ndarray, p_global: float = None, p_local: float = None):
    assert p_global is None or 0 <= p_global
    assert p_local is None or 0 <= p_local

    g = numpy.random.randn() * p_global
    l = numpy.random.randn(*data.shape).astype(data.dtype) * p_local
    return data + g + l


class Dataset(chainer.dataset.DatasetMixin):
    def __init__(self, inputs: List[Inputs], config: DatasetConfig) -> None:
        self.inputs = inputs
        self.config = config

    def __len__(self):
        return len(self.inputs)

    def get_example(self, i):
        train = chainer.config.train

        inputs = self.inputs[i]
        p_input, p_target, p_indexes = inputs.in_feature_path, inputs.out_feature_path, inputs.indexes_path

        indexes = AlignIndexes.load(p_indexes)

        # input feature
        f_in = AcousticFeature.load(p_input)
        f_in = f_in.indexing(indexes.indexes1)
        input = encode_feature(f_in, targets=self.config.features)

        # target feature
        f_tar = AcousticFeature.load(p_target)
        f_tar = f_tar.indexing(indexes.indexes2)
        target = encode_feature(f_tar, targets=self.config.features)

        mask = encode_feature(make_mask(f_tar), targets=self.config.features)

        # padding
        seed = numpy.random.randint(2 ** 32)
        input = random_pad(input, seed=seed, min_size=self.config.train_crop_size)
        target = random_pad(target, seed=seed, min_size=self.config.train_crop_size)
        mask = random_pad(mask, seed=seed, min_size=self.config.train_crop_size)

        # crop
        seed = numpy.random.randint(2 ** 32)
        input = random_crop(input, seed=seed, crop_size=self.config.train_crop_size)
        target = random_crop(target, seed=seed, crop_size=self.config.train_crop_size)
        mask = random_crop(mask, seed=seed, crop_size=self.config.train_crop_size)

        if train:
            input = add_noise(input, p_global=self.config.target_global_noise, p_local=self.config.target_local_noise)
            target = add_noise(target, p_global=self.config.target_global_noise, p_local=self.config.target_local_noise)

        return dict(
            input=input,
            target=target,
            mask=mask,
        )


def create(config: DatasetConfig):
    input_paths = list(sorted([Path(p) for p in glob.glob(str(config.input_glob))]))
    target_paths = list(sorted([Path(p) for p in glob.glob(str(config.target_glob))]))
    indexes_paths = list(sorted([Path(p) for p in glob.glob(str(config.indexes_glob))]))
    assert len(input_paths) == len(target_paths) == len(indexes_paths)

    num_test = config.num_test
    pairs = [
        Inputs(
            in_feature_path=input_path,
            out_feature_path=target_path,
            indexes_path=indexes_path,
        )
        for input_path, target_path, indexes_path in zip(input_paths, target_paths, indexes_paths)
    ]
    numpy.random.RandomState(config.seed).shuffle(pairs)
    train_paths = pairs[num_test:]
    test_paths = pairs[:num_test]
    train_for_evaluate_paths = train_paths[:num_test]

    return {
        'train': Dataset(train_paths, config=config),
        'test': Dataset(test_paths, config=config),
        'train_eval': Dataset(train_for_evaluate_paths, config=config),
    }
