import asyncio, inspect, functools, re, itertools
from . import util
from .sound_mixin import soundmixin
import numpy as np
from collections.abc import Iterable
import soundfile as sf, wave, io
from pydub import AudioSegment
from pydub.generators import Sine
from gpiozero.tones import Tone


class Speaker(util.InputStreamComponent, soundmixin):
    """扬声器"""

    def __init__(self, robot):
        util.InputStreamComponent.__init__(self, robot)
        soundmixin.__init__(self, dtype='int16', sample_rate=22050, block_duration=.1, gain=25)
        self._lock = asyncio.Lock()

    def _get_rpc(self):
        return self._rpc.speaker(self._t_sr, self._t_dt, self._t_bs, request_stream=self._input_stream)

    def _volume(self):
        return self._rpc.speaker_volume

    @util.mode()
    async def play(self, src, repeat=1, preload=1, **kw):
        """播放

        :param src: 要播放的声音资源（文件/网址/数据）
        :type src: str/np.ndarray/bytes/iterable/file-like obj
        :param repeat: 播放次数，默认为 1
        :type repeat: int, optional
        """
        sr = kw.get('sample_rate', self.sample_rate)
        dt = kw.get('dtype', self.dtype)
        bd = kw.get('block_duration', self.block_duration)
        bs = int(bd * sr)
        if isinstance(src, str) or hasattr(src, 'read'): # for file-like obj
            sr, bs, src = await asyncio.get_running_loop().run_in_executor(None, file_sr_bs_gen, src, sr, dt, bd)

        elif isinstance(src, np.ndarray):
            src = np_gen(src, dt, bs)

        elif isinstance(src, bytes):
            src = raw_gen(src, dt, bs)

        # if inspect.isasyncgen(src):
        if isinstance(src, Iterable):
            async with self._lock:
                self._t_sr = sr
                self._t_dt = dt
                self._t_bs = bs
                async with self:
                    count = 0
                    for raw in repeat_gen(src, repeat):
                        await self._input_stream.put(raw)
                        if count < preload:
                            count += bd
                        else:
                            await asyncio.sleep(bd * .95)
        else:
            raise TypeError(f'Unable to open {src}')

    @util.mode()
    async def say(self, txt, repeat=1, **options):
        """说话

        :param txt: 要说的字符串
        :type txt: str
        :param repeat: 播放次数，默认为 1
        :type repeat: int, optional
        :param options:
            * voice 语言
            * volume 音量
            * pitch 音调
            * speed 语速
            * word_gap 字间停顿

            见 `py-espeak-ng <https://github.com/gooofy/py-espeak-ng>`_
        :type options: optional
        """
        if not hasattr(self, '_esng'):
            from espeakng import ESpeakNG
            self._esng = ESpeakNG()
        op = {'voice': 'zh' if re.findall(r'[\u4e00-\u9fff]+', txt) else 'en'}
        op.update(options)
        for k, v in op.items():
            setattr(self._esng, k, v)
        wav_data = await asyncio.get_running_loop().run_in_executor(None, self._esng.synth_wav, txt)
        with wave.open(io.BytesIO(wav_data)) as f:
            await self.play(f.readframes(f.getnframes()), repeat=repeat, sample_rate=f.getframerate(), dtype='int16')

    @util.mode()
    async def beep(self, tones, repeat=1, tempo=120, duty_cycle=.9):
        """哼一段曲子

        :param tones: 一串音调组成的曲子
        :type tones: collections.Iterable
        :param tempo: 播放速度，BPM，默认是 `120` 拍/分钟
        :type tempo: int
        :param duty_cycle: 占空比，即音节播放时间与整个音节的时间的比值，0~1，默认是 `0.9`
        :type duty_cycle: float
        :param repeat: 播放次数，默认为 1
        :type repeat: int, optional

        .. note::

            这个 API 将来可能会改变，我们还在探索更方便播放音调的 API

        """
        if not 0< duty_cycle <=1:
            raise ValueError('duty_cycle out of range (0, 1]')
        # find min freq required to save bandwidth
        sr = max_freq(tones)
        if sr > 11025:
            sr = 44100
        elif sr > 800:
            sr = 22050
        else:
            sr = 16000
        aud = await asyncio.get_running_loop().run_in_executor(None, tone2audio, tones, 60000.0/tempo, duty_cycle, sr)
        await self.play(aud.raw_data, repeat=repeat, sample_rate=sr, dtype='int16')


def max_freq(tones):
    return functools.reduce(lambda r,e: max(r, max_freq(e) if isinstance(e, (list, tuple)) else Tone(e).frequency), tones, 0)

def tone2audio(tones, base_beat_ms, duty_cycle, sr):
    duty = base_beat_ms * duty_cycle
    empty = base_beat_ms - duty
    return functools.reduce(lambda r,e: r+(tone2audio(e, base_beat_ms/2, duty_cycle, sr) if isinstance(e, (list, tuple)) else \
            Sine(Tone(e).frequency, sample_rate=sr).to_audio_segment(duration=duty).append(AudioSegment.silent(duration=empty, frame_rate=sr), crossfade=empty)), \
        tones, AudioSegment.empty())

def file_sr_bs_gen(src, sr, dt, block_duration):
    # input recommended samplerate, dtype, and block_duration
    # return actual samplerate, blocksize and generator
    if src.startswith('http'):
        from urllib.request import urlopen
        import io
        src = io.BytesIO(urlopen(src).read())

    try:
        file = sf.SoundFile(src)
        assert file.samplerate <= sr
        bs = int(block_duration * file.samplerate)
        return file.samplerate, bs, map(lambda b: (b if b.ndim==1 else b.mean(axis=1, dtype=dt)).tobytes(),
            file.blocks(dtype=dt, blocksize=bs, fill_value=0))

    except (AssertionError, RuntimeError):
        import functools, librosa # librosa supports more formats than soundfile
        # down-sample if needed
        if librosa.get_samplerate(src) < sr:
            sr = None
        y, sr = librosa.load(src, sr=sr, mono=True, res_type='kaiser_fast')
        bs = int(sr * block_duration)
        return sr, bs, np_gen(y, dt, bs)

def np_gen(data, dt, bs):
    # convert data to specified dtype
    if data.ndim >1:
        data = data.mean(axis=1, dtype=dt)
    if str(data.dtype).startswith('float') and dt.startswith('int'):
        data = (data * np.iinfo(dt).max).astype(dt)
    elif str(data.dtype).startswith('int') and dt.startswith('float'):
        data = data.astype(dt) / np.iinfo(data.dtype).max
    # elif (int8 <--> int16 <--> int32 convertion, but int8/32 is very rarely used)
    yield from raw_gen(data.tobytes(), dt, bs)

def raw_gen(data, dt, bs):
    bs = bs* util.sample_width(dt)
    for i in range(0, len(data), bs):
        b = data[i: i+ bs]
        if len(b) < bs:
            b += b'\x00' * (bs-len(b))
        yield b

def repeat_gen(gen, repeat):
    if repeat == 1:
        yield from gen
    else:
        for g in itertools.tee(gen, repeat):
            yield from g