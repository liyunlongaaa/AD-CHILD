# -*- coding: utf-8 -*- #
"""*********************************************************************************************"""
#   FileName     [ dataset.py ]
#   Synopsis     [ the speaker diarization dataset ]
#   Source       [ Refactored from https://github.com/hitachi-speech/EEND ]
#   Author       [ Jiatong Shi ]
#   Copyright    [ Copyright(c), Johns Hopkins University ]
"""*********************************************************************************************"""


###############
# IMPORTATION #
###############
import io
import os
import random
import subprocess
import sys

# -------------#
import numpy as np
import pandas as pd
import soundfile as sf

# -------------#
import torch
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data.dataset import Dataset

# -------------#
import torchaudio


def _count_frames(data_len, size, step):
    # no padding at edges, last remaining samples are ignored
    return int((data_len - size + step) / step)


def _gen_frame_indices(
    data_length,
    size=2000,
    step=2000,
    use_last_samples=False,
    label_delay=0,
    subsampling=1,
):
    i = -1
    for i in range(_count_frames(data_length, size, step)):
        yield i * step, i * step + size                #返回每一个frame的起始和结束（采样点）
    if use_last_samples and i * step + size < data_length:  #如果 use_last_samples 为 True 并且最后一帧没有被包含，函数会再生成一组帧索引
        if data_length - (i + 1) * step - subsampling * label_delay > 0:
            yield (i + 1) * step, data_length


def _gen_chunk_indices(data_len, chunk_size):
    step = chunk_size
    start = 0
    while start < data_len:
        end = min(data_len, start + chunk_size)
        yield start, end
        start += step


#######################
# Diarization Dataset #
#######################
class DiarizationDataset(Dataset):
    def __init__(
        self,
        mode,
        data_dir,
        dtype=np.float32,
        chunk_size=2000,
        frame_shift=256,
        subsampling=1,
        rate=16000,
        input_transform=None,
        use_last_samples=True,
        label_delay=0,
        num_speakers=None,
    ):
        super(DiarizationDataset, self).__init__()

        self.mode = mode
        self.data_dir = data_dir
        self.dtype = dtype
        self.chunk_size = chunk_size
        self.frame_shift = frame_shift
        self.subsampling = subsampling
        self.n_speakers = num_speakers
        self.chunk_indices = [] if mode != "test" else {}
        self.label_delay = label_delay

        self.data = KaldiData(self.data_dir)

        # make chunk indices: filepath, start_frame, end_frame
        for rec in self.data.wavs:
            data_len = int(self.data.reco2dur[rec] * rate / frame_shift)  #reco2dur[rec] 取出时长
            data_len = int(data_len / self.subsampling)
            if mode == "test":
                self.chunk_indices[rec] = []
            if mode != "test":
                for st, ed in _gen_frame_indices(
                    data_len,
                    chunk_size,
                    chunk_size,
                    use_last_samples,
                    label_delay=self.label_delay,
                    subsampling=self.subsampling,
                ):
                    self.chunk_indices.append(
                        (rec, st * self.subsampling, ed * self.subsampling)    #训的时候可能下采样，而且是把所有语料分chunk作为列表（每个chunk对应mixid和起始和结束），不是test时的以rec为key的字典
                    )
            else:
                for st, ed in _gen_chunk_indices(data_len, chunk_size):
                    self.chunk_indices[rec].append(
                        (rec, st * self.subsampling, ed * self.subsampling)
                    )

        if mode != "test":
            print(len(self.chunk_indices), " chunks")
        else:
            self.rec_list = list(self.chunk_indices.keys())
            print(len(self.rec_list), " recordings")

    def __len__(self):
        return (
            len(self.rec_list)
            if type(self.chunk_indices) == dict
            else len(self.chunk_indices)
        )

    def __getitem__(self, i):
        if self.mode != "test":
            rec, st, ed = self.chunk_indices[i]
            Y, T = self._get_labeled_speech(rec, st, ed, self.n_speakers)
            # TODO: add subsampling here
            return Y, T
        else:
            chunks = self.chunk_indices[self.rec_list[i]]
            Ys, Ts = [], []
            for (rec, st, ed) in chunks:
                Y, T = self._get_labeled_speech(rec, st, ed, self.n_speakers)
                Ys.append(Y)
                Ts.append(T)
            return Ys, Ts, self.rec_list[i]

    def _get_labeled_speech(
        self, rec, start, end, n_speakers=None, use_speaker_id=False
    ):
        """Extracts speech chunks and corresponding labels

        Extracts speech chunks and corresponding diarization labels for
        given recording id and start/end times

        Args:
            rec (str): recording id
            start (int): start frame index
            end (int): end frame index
            n_speakers (int): number of speakers
                if None, the value is given from data
        Returns:
            data: speech chunk
                (n_samples)
            T: label
                (n_frmaes, n_speakers)-shaped np.int32 array.
        """
        data, rate = self.data.load_wav(
            rec, start * self.frame_shift, end * self.frame_shift
        )
        frame_num = end - start
        filtered_segments = self.data.segments[rec]
        # filtered_segments = self.data.segments[self.data.segments['rec'] == rec]
        speakers = np.unique(
            [self.data.utt2spk[seg["utt"]] for seg in filtered_segments]
        ).tolist()
        if n_speakers is None:
            n_speakers = len(speakers)
        T = np.zeros((frame_num, n_speakers), dtype=np.int32)

        if use_speaker_id:
            all_speakers = sorted(self.data.spk2utt.keys())
            S = np.zeros((frame_num, len(all_speakers)), dtype=np.int32)

        for seg in filtered_segments:
            speaker_index = speakers.index(self.data.utt2spk[seg["utt"]])
            if use_speaker_id:
                all_speaker_index = all_speakers.index(self.data.utt2spk[seg["utt"]])
            start_frame = np.rint(seg["st"] * rate / self.frame_shift).astype(int)
            end_frame = np.rint(seg["et"] * rate / self.frame_shift).astype(int)
            rel_start = rel_end = None
            if start <= start_frame and start_frame < end:
                rel_start = start_frame - start
            if start < end_frame and end_frame <= end:
                rel_end = end_frame - start
            if rel_start is not None or rel_end is not None:
                T[rel_start:rel_end, speaker_index] = 1
                if use_speaker_id:
                    S[rel_start:rel_end, all_speaker_index] = 1

        if use_speaker_id:
            return data, T, S
        else:
            return data, T

    def collate_fn(self, batch):
        batch_size = len(batch)
        len_list = [len(batch[i][1]) for i in range(batch_size)]
        wav = []
        label = []
        for i in range(batch_size):
            length = len_list[i]
            wav.append(batch[i][0].astype(np.float32))
            label.append(batch[i][1].astype(np.float32))
        length = np.array(len_list)
        return wav, label, length, None

    def collate_fn_rec_infer(self, batch):   #只在test的時候
        assert len(batch) == 1  # each batch should contain one recording
        chunk_num = len(batch[0][1])
        len_list = [len(batch[0][1][i]) for i in range(chunk_num)]
        wav = []
        label = []
        for i in range(chunk_num):
            length = len_list[i]
            wav.append(batch[0][0][i].astype(np.float32))
            label.append(batch[0][1][i].astype(np.float32))
        length = np.array(len_list)
        rec_id = batch[0][2]
        return wav, label, length, rec_id


#######################
# Kaldi-style Dataset #
#######################
class KaldiData:
    """This class holds data in kaldi-style directory."""

    def __init__(self, data_dir):
        """Load kaldi data directory."""
        self.data_dir = data_dir
        self.segments = self._load_segments_rechash(
            os.path.join(self.data_dir, "segments")
        )
        self.utt2spk = self._load_utt2spk(os.path.join(self.data_dir, "utt2spk"))
        self.wavs = self._load_wav_scp(os.path.join(self.data_dir, "wav.scp"))
        self.reco2dur = self._load_reco2dur(os.path.join(self.data_dir, "reco2dur"))
        self.spk2utt = self._load_spk2utt(os.path.join(self.data_dir, "spk2utt"))
        # text 这个文件可以看成一个map，key是utterance id，value是utterance的词序列(词之间用空格分开)。
        # spk2gender 说话人(speaker)id到性别的map。
        # spk2utt 某个说话人的所有utternace id
        # utt2spk 某个utterance的说话人，前面的spk2utt可以由这个文件生成(当然反过来也行)
        # wav.scp 这是Kaldi提取特征时真正用到的文件，这个文件的第一列是utterance id，第二列是扩展文件名(extended filename)，扩展文件名可以是普通的文件路径，也可以是一些命令行的输出，更多扩展文件名的信息请参考这里。我们这里可以先把第二列当作录音文件的路径。
        # segments 格式是 mix_id_start_end mix_id start end
    def load_wav(self, recid, start=0, end=None):
        """Load wavfile given recid, start time and end time."""
        data, rate = self._load_wav(self.wavs[recid], start, end)
        return data, rate

    def _load_segments(self, segments_file):
        """Load segments file as array."""
        if not os.path.exists(segments_file):
            return None
        return np.loadtxt(
            segments_file,
            dtype=[("utt", "object"), ("rec", "object"), ("st", "f"), ("et", "f")],
            ndmin=1,
        )

    def _load_segments_hash(self, segments_file):
        """Load segments file as dict with uttid index."""
        ret = {}
        if not os.path.exists(segments_file):
            return None
        for line in open(segments_file):
            utt, rec, st, et = line.strip().split()
            ret[utt] = (rec, float(st), float(et))
        return ret

    def _load_segments_rechash(self, segments_file):
        """Load segments file as dict with recid index."""
        ret = {}
        if not os.path.exists(segments_file):
            return None
        for line in open(segments_file):
            utt, rec, st, et = line.strip().split()
            if rec not in ret:
                ret[rec] = []
            ret[rec].append({"utt": utt, "st": float(st), "et": float(et)})
        return ret

    def _load_wav_scp(self, wav_scp_file):
        """Return dictionary { rec: wav_rxfilename }."""
        if os.path.exists(wav_scp_file):
            lines = [line.strip().split(None, 1) for line in open(wav_scp_file)]
            return {x[0]: x[1] for x in lines}
        else:
            wav_dir = os.path.join(self.data_dir, "wav")
            return {
                os.path.splitext(filename)[0]: os.path.join(wav_dir, filename)
                for filename in sorted(os.listdir(wav_dir))
            }

    def _load_wav(self, wav_rxfilename, start=0, end=None):
        """This function reads audio file and return data in numpy.float32 array.
        "lru_cache" holds recently loaded audio so that can be called
        many times on the same audio file.
        OPTIMIZE: controls lru_cache size for random access,
        considering memory size
        """
        if wav_rxfilename.endswith("|"):
            # input piped command
            p = subprocess.Popen(
                wav_rxfilename[:-1],
                shell=True,
                stdout=subprocess.PIPE,
            )
            data, samplerate = sf.read(
                io.BytesIO(p.stdout.read()),
                dtype="float32",
            )
            # cannot seek
            data = data[start:end]
        elif wav_rxfilename == "-":
            # stdin
            data, samplerate = sf.read(sys.stdin, dtype="float32")
            # cannot seek
            data = data[start:end]
        else:
            # normal wav file
            data, samplerate = sf.read(wav_rxfilename, start=start, stop=end)
        return data, samplerate

    def _load_utt2spk(self, utt2spk_file):
        """Returns dictionary { uttid: spkid }."""
        lines = [line.strip().split(None, 1) for line in open(utt2spk_file)]
        return {x[0]: x[1] for x in lines}

    def _load_spk2utt(self, spk2utt_file):
        """Returns dictionary { spkid: list of uttids }."""
        if not os.path.exists(spk2utt_file):
            return None
        lines = [line.strip().split() for line in open(spk2utt_file)]
        return {x[0]: x[1:] for x in lines}

    def _load_reco2dur(self, reco2dur_file):
        """Returns dictionary { recid: duration }."""
        if not os.path.exists(reco2dur_file):
            return None
        lines = [line.strip().split(None, 1) for line in open(reco2dur_file)]
        return {x[0]: float(x[1]) for x in lines}

    def _process_wav(self, wav_rxfilename, process):
        """This function returns preprocessed wav_rxfilename.
        Args:
            wav_rxfilename:
                input
            process:
                command which can be connected via pipe, use stdin and stdout
        Returns:
            wav_rxfilename: output piped command
        """
        if wav_rxfilename.endswith("|"):
            # input piped command
            return wav_rxfilename + process + "|"
        # stdin "-" or normal file
        return "cat {0} | {1} |".format(wav_rxfilename, process)

    def _extract_segments(self, wavs, segments=None):
        """This function returns generator of segmented audio.
        Yields (utterance id, numpy.float32 array).
        TODO?: sampling rate is not converted.
        """
        if segments is not None:
            # segments should be sorted by rec-id
            for seg in segments:
                wav = wavs[seg["rec"]]
                data, samplerate = self.load_wav(wav)
                st_sample = np.rint(seg["st"] * samplerate).astype(int)
                et_sample = np.rint(seg["et"] * samplerate).astype(int)
                yield seg["utt"], data[st_sample:et_sample]
        else:
            # segments file not found,
            # wav.scp is used as segmented audio list
            for rec in wavs:
                data, samplerate = self.load_wav(wavs[rec])
                yield rec, data
