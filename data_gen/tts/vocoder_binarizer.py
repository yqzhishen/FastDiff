import os

os.environ["OMP_NUM_THREADS"] = "1"

from collections import Counter
from utils.text_encoder import TokenTextEncoder

from utils.multiprocess_utils import chunked_multiprocess_run
import random
import traceback
import json
from resemblyzer import VoiceEncoder
from tqdm import tqdm
from data_gen.tts.data_gen_utils import get_mel2ph, get_pitch, build_phone_encoder, is_sil_phoneme
from utils.hparams import hparams, set_hparams
import numpy as np
from utils.indexed_datasets import IndexedDatasetBuilder
from vocoders.base_vocoder import get_vocoder_cls
import pandas as pd


class BinarizationError(Exception):
    pass


class VocoderBinarizer:
    def __init__(self, processed_data_dir=None):
        if processed_data_dir is None:
            processed_data_dir = hparams['processed_data_dir']
        self.processed_data_dirs = processed_data_dir.split(",")
        self.binarization_args = hparams['binarization_args']
        self.pre_align_args = hparams['pre_align_args']
        self.item2wavfn = {}

    def load_meta_data(self):
        for ds_id, processed_data_dir in enumerate(self.processed_data_dirs):
            self.meta_df = pd.read_csv(f"{processed_data_dir}/metadata_phone.csv", dtype=str)
            for r_idx, r in tqdm(self.meta_df.iterrows(), desc='Loading meta data.'):
                item_name = raw_item_name = r['item_name']
                if len(self.processed_data_dirs) > 1:
                    item_name = f'ds{ds_id}_{item_name}'
                self.item2wavfn[item_name] = r['wav_fn']
        self.item_names = sorted(list(self.item2wavfn.keys()))
        if self.binarization_args['shuffle']:
            random.seed(1234)
            random.shuffle(self.item_names)

    @property
    def train_item_names(self):
        return self.item_names[hparams['test_num']:]

    @property
    def valid_item_names(self):
        return self.item_names[:hparams['test_num']]

    @property
    def test_item_names(self):
        return self.valid_item_names

    def meta_data(self, prefix):
        if prefix == 'valid':
            item_names = self.valid_item_names
        elif prefix == 'test':
            item_names = self.test_item_names
        else:
            item_names = self.train_item_names
        for item_name in item_names:
            wav_fn = self.item2wavfn[item_name]
            yield item_name, wav_fn

    def process(self):
        self.load_meta_data()
        os.makedirs(hparams['binary_data_dir'], exist_ok=True)
        self.process_data('valid')
        self.process_data('test')
        self.process_data('train')

    def process_data(self, prefix):
        data_dir = hparams['binary_data_dir']
        args = []
        builder = IndexedDatasetBuilder(f'{data_dir}/{prefix}')
        mel_lengths = []
        total_sec = 0
        meta_data = list(self.meta_data(prefix))
        for m in meta_data:
            args.append(list(m) + [self.binarization_args])
        num_workers = self.num_workers
        for f_id, (_, item) in enumerate(
                zip(tqdm(meta_data), chunked_multiprocess_run(self.process_item, args, num_workers=num_workers))):
            if item is None:
                continue
            if not self.binarization_args['with_wav'] and 'wav' in item:
                del item['wav']
            builder.add_item(item)
            mel_lengths.append(item['len'])
            total_sec += item['sec']
        builder.finalize()
        np.save(f'{data_dir}/{prefix}_lengths.npy', mel_lengths)
        print(f"| {prefix} total duration: {total_sec:.3f}s")

    @classmethod
    def process_item(cls, item_name, wav_fn, binarization_args):
        res = {'item_name': item_name, 'wav_fn': wav_fn}
        if binarization_args['with_linear']:
            wav, mel, linear_stft = get_vocoder_cls(hparams).wav2spec(wav_fn, return_linear=True)
            res['linear'] = linear_stft
        else:
            wav, mel = get_vocoder_cls(hparams).wav2spec(wav_fn)
        wav = wav.astype(np.float16)
        res.update({'mel': mel, 'wav': wav,
                    'sec': len(wav) / hparams['audio_sample_rate'], 'len': mel.shape[0]})
        
        return res

    @classmethod
    def process_mel_item(cls, item_name, mel, wav_fn, binarization_args):
        res = {'item_name': item_name, 'wav_fn': wav_fn}
        mel = mel
        wav = np.ones((1,500,100))
        res.update({'mel': mel, 'wav': wav,
                    'sec': 0, 'len': mel.shape[0]})
        return res

    @property
    def num_workers(self):
        return int(os.getenv('N_PROC', hparams.get('N_PROC', os.cpu_count())))


set_hparams()
if __name__ == "__main__":
    VocoderBinarizer().process()
