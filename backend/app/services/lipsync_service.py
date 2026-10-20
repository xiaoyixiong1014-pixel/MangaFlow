"""口型同步服务模块（Wav2Lip 本地推理，文件级说明）。

vendor 自 Wav2Lip 官方仓库（Rudrabha/Wav2Lip）推理链路（inference.py /
audio.py / hparams.py / models），将分镜视频的roles口型与平台 TTS 配音
逐帧对齐（2026-10-20 用户确认方案：LatentSync 本机显存不足，改用
CPU 可跑的 Wav2Lip 轻量管线）。

管线（官方 inference 数值逻辑等价，性能改造为两遍流式）：
1. cv2.VideoCapture 流式读帧 + fps 探测；
2. 配音 wav → librosa 16kHz → mel 频谱（官方 hparams：n_fft=800 /
   hop=200 / 80 mels / fmin=55 / fmax=7600 / preemphasis 0.97）→
   按 80/fps 帧率取 16-hop mel chunk 序列（官方 datagen 口径）；
3. 第一遍流式：S3FD 逐 batch 检测主脸框 → 时间邻近传播填空帧 →
   T=5 窗口平滑（官方 get_smoothened_boxes）→ chin padding (0,10,0,0)；
4. 第二遍流式：逐 batch 裁脸 96×96 → Wav2Lip 生成下半脸 → 贴回原帧 →
   cv2.VideoWriter 写 AVI（DIVX）；
5. ffmpeg 合成 AVI + 配音 → mp4（libx264 + aac 44.1kHz，与 compose 阶段
   编码参数对齐保证后续 concat 兼容），产物音轨 = 驱动配音。

许可提示：Wav2Lip 代码与模型为 CC BY-NC 4.0（非商业），商业化部署需
评估替换为可商用方案（如云端 VideoReTalk）。

内存策略：720p×15s 全帧驻留约 530MB，故帧不入内存缓存——检测与生成
两遍分别流式重开 VideoCapture，峰值内存仅为 batch 帧体积。

失败约定：任一环节失败（模型缺失/无人脸/异常）返回 False，调用方降级
为不做口型同步的原产物，不阻断视频任务主流程。
"""

import logging
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

import cv2
import librosa
import numpy as np
import torch
from scipy import signal

from app.services.lipsync_s3fd import S3FDDetector

logger = logging.getLogger("mangaflow.services.lipsync")

# 官方 hparams（mel 频谱参数，与训练一致，勿改）
_HPARAMS = {
    "num_mels": 80,
    "sample_rate": 16000,
    "n_fft": 800,
    "hop_size": 200,
    "win_size": 800,
    "fmin": 55,
    "fmax": 7600,
    "preemphasis": 0.97,
    "preemphasize": True,
    "min_level_db": -100,
    "ref_level_db": 20,
    "max_abs_value": 4.0,
    "symmetric_mels": True,
    "allow_clipping_in_normalization": True,
}
# 每帧消费的 mel 帧数（官方 mel_step_size）
_MEL_STEP_SIZE = 16
# 人脸 crop 尺寸（官方 img_size）
_IMG_SIZE = 96
# 检测框 chin padding（官方默认 pads：top, bottom, left, right）
_PADS = (0, 10, 0, 0)
# 框平滑时间窗（官方 T=5）
_SMOOTH_WINDOW = 5
# 检测帧长边上限（S3FD CPU 推理量与分辨率平方成正比，720p 原图逐帧检测
# 是主要耗时项；检测在缩小帧上执行后框按比例还原，对 Wav2Lip 精度无影响）
_DET_MAX_EDGE = 640
# Wav2Lip 生成 batch（CPU 内存可控值）
_WAV2LIP_BATCH = 32
# S3FD 检测 batch
_FACE_DET_BATCH = 8


class _Conv2d(torch.nn.Module):
    """卷积+BN+ReLU 块（官方 models/conv.py，residual 时输出+=输入）。"""

    def __init__(self, cin: int, cout: int, kernel_size: int, stride, padding, residual: bool = False) -> None:
        """初始化卷积块。

        Args:
            cin: 输入通道数。
            cout: 输出通道数。
            kernel_size: 卷积核尺寸。
            stride: 步幅。
            padding: 零填充。
            residual: 是否残差连接。

        Returns:
            None
        """
        super().__init__()
        self.conv_block = torch.nn.Sequential(
            torch.nn.Conv2d(cin, cout, kernel_size, stride, padding),
            torch.nn.BatchNorm2d(cout),
        )
        self.act = torch.nn.ReLU()
        self.residual = residual

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """前向：卷积 BN ReLU（residual 时跳连）。

        Args:
            x: 输入张量。

        Returns:
            torch.Tensor: 输出张量。
        """
        out = self.conv_block(x)
        if self.residual:
            out += x
        return self.act(out)


class _Conv2dTranspose(torch.nn.Module):
    """转置卷积+BN+ReLU 块（官方 models/conv.py 解码侧组件）。"""

    def __init__(self, cin: int, cout: int, kernel_size: int, stride, padding, output_padding: int = 0) -> None:
        """初始化转置卷积块。

        Args:
            cin: 输入通道数。
            cout: 输出通道数。
            kernel_size: 卷积核尺寸。
            stride: 步幅。
            padding: 零填充。
            output_padding: 输出尺寸补偿。

        Returns:
            None
        """
        super().__init__()
        self.conv_block = torch.nn.Sequential(
            torch.nn.ConvTranspose2d(cin, cout, kernel_size, stride, padding, output_padding),
            torch.nn.BatchNorm2d(cout),
        )
        self.act = torch.nn.ReLU()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """前向：转置卷积 BN ReLU。

        Args:
            x: 输入张量。

        Returns:
            torch.Tensor: 输出张量。
        """
        return self.act(self.conv_block(x))


class Wav2Lip(torch.nn.Module):
    """Wav2Lip 生成器网络（官方 models/wav2lip.py 等价实现）。

    输入：audio_sequences (B, 1, 80, 16) + face_sequences (B, 6, 96, 96)
    （下半脸遮蔽图与原图 concat）；输出：下半脸生成图 (B, 3, 96, 96)。
    """

    def __init__(self) -> None:
        """按官方结构初始化编码-解码块。

        Args:
            无。

        Returns:
            None
        """
        super().__init__()

        self.face_encoder_blocks = torch.nn.ModuleList([
            torch.nn.Sequential(_Conv2d(6, 16, kernel_size=7, stride=1, padding=3)),
            torch.nn.Sequential(
                _Conv2d(16, 32, kernel_size=3, stride=2, padding=1),
                _Conv2d(32, 32, kernel_size=3, stride=1, padding=1, residual=True),
                _Conv2d(32, 32, kernel_size=3, stride=1, padding=1, residual=True)),
            torch.nn.Sequential(
                _Conv2d(32, 64, kernel_size=3, stride=2, padding=1),
                _Conv2d(64, 64, kernel_size=3, stride=1, padding=1, residual=True),
                _Conv2d(64, 64, kernel_size=3, stride=1, padding=1, residual=True),
                _Conv2d(64, 64, kernel_size=3, stride=1, padding=1, residual=True)),
            torch.nn.Sequential(
                _Conv2d(64, 128, kernel_size=3, stride=2, padding=1),
                _Conv2d(128, 128, kernel_size=3, stride=1, padding=1, residual=True),
                _Conv2d(128, 128, kernel_size=3, stride=1, padding=1, residual=True)),
            torch.nn.Sequential(
                _Conv2d(128, 256, kernel_size=3, stride=2, padding=1),
                _Conv2d(256, 256, kernel_size=3, stride=1, padding=1, residual=True),
                _Conv2d(256, 256, kernel_size=3, stride=1, padding=1, residual=True)),
            torch.nn.Sequential(
                _Conv2d(256, 512, kernel_size=3, stride=2, padding=1),
                _Conv2d(512, 512, kernel_size=3, stride=1, padding=1, residual=True)),
            torch.nn.Sequential(
                _Conv2d(512, 512, kernel_size=3, stride=1, padding=0),
                _Conv2d(512, 512, kernel_size=1, stride=1, padding=0)),
        ])

        self.audio_encoder = torch.nn.Sequential(
            _Conv2d(1, 32, kernel_size=3, stride=1, padding=1),
            _Conv2d(32, 32, kernel_size=3, stride=1, padding=1, residual=True),
            _Conv2d(32, 32, kernel_size=3, stride=1, padding=1, residual=True),
            _Conv2d(32, 64, kernel_size=3, stride=(3, 1), padding=1),
            _Conv2d(64, 64, kernel_size=3, stride=1, padding=1, residual=True),
            _Conv2d(64, 64, kernel_size=3, stride=1, padding=1, residual=True),
            _Conv2d(64, 128, kernel_size=3, stride=3, padding=1),
            _Conv2d(128, 128, kernel_size=3, stride=1, padding=1, residual=True),
            _Conv2d(128, 128, kernel_size=3, stride=1, padding=1, residual=True),
            _Conv2d(128, 256, kernel_size=3, stride=(3, 2), padding=1),
            _Conv2d(256, 256, kernel_size=3, stride=1, padding=1, residual=True),
            _Conv2d(256, 512, kernel_size=3, stride=1, padding=0),
            _Conv2d(512, 512, kernel_size=1, stride=1, padding=0),
        )

        self.face_decoder_blocks = torch.nn.ModuleList([
            torch.nn.Sequential(_Conv2d(512, 512, kernel_size=1, stride=1, padding=0)),
            torch.nn.Sequential(
                _Conv2dTranspose(1024, 512, kernel_size=3, stride=1, padding=0),
                _Conv2d(512, 512, kernel_size=3, stride=1, padding=1, residual=True)),
            torch.nn.Sequential(
                _Conv2dTranspose(1024, 512, kernel_size=3, stride=2, padding=1, output_padding=1),
                _Conv2d(512, 512, kernel_size=3, stride=1, padding=1, residual=True),
                _Conv2d(512, 512, kernel_size=3, stride=1, padding=1, residual=True)),
            torch.nn.Sequential(
                _Conv2dTranspose(768, 384, kernel_size=3, stride=2, padding=1, output_padding=1),
                _Conv2d(384, 384, kernel_size=3, stride=1, padding=1, residual=True),
                _Conv2d(384, 384, kernel_size=3, stride=1, padding=1, residual=True)),
            torch.nn.Sequential(
                _Conv2dTranspose(512, 256, kernel_size=3, stride=2, padding=1, output_padding=1),
                _Conv2d(256, 256, kernel_size=3, stride=1, padding=1, residual=True),
                _Conv2d(256, 256, kernel_size=3, stride=1, padding=1, residual=True)),
            torch.nn.Sequential(
                _Conv2dTranspose(320, 128, kernel_size=3, stride=2, padding=1, output_padding=1),
                _Conv2d(128, 128, kernel_size=3, stride=1, padding=1, residual=True),
                _Conv2d(128, 128, kernel_size=3, stride=1, padding=1, residual=True)),
            torch.nn.Sequential(
                _Conv2dTranspose(160, 64, kernel_size=3, stride=2, padding=1, output_padding=1),
                _Conv2d(64, 64, kernel_size=3, stride=1, padding=1, residual=True),
                _Conv2d(64, 64, kernel_size=3, stride=1, padding=1, residual=True)),
        ])

        self.output_block = torch.nn.Sequential(
            _Conv2d(80, 32, kernel_size=3, stride=1, padding=1),
            torch.nn.Conv2d(32, 3, kernel_size=1, stride=1, padding=0),
            torch.nn.Sigmoid())

    def forward(self, audio_sequences: torch.Tensor, face_sequences: torch.Tensor) -> torch.Tensor:
        """前向：音频嵌入与面部多尺度特征逐层融合解码下半脸。

        Args:
            audio_sequences: mel 批次 (B, 1, 80, 16)。
            face_sequences: 面部批次 (B, 6, 96, 96)。

        Returns:
            torch.Tensor: 生成下半脸 (B, 3, 96, 96)，值域 [0,1]。
        """
        audio_embedding = self.audio_encoder(audio_sequences)  # (B, 512, 1, 1)

        feats = []
        x = face_sequences
        for f in self.face_encoder_blocks:
            x = f(x)
            feats.append(x)

        x = audio_embedding
        for f in self.face_decoder_blocks:
            x = f(x)
            x = torch.cat((x, feats[-1]), dim=1)
            feats.pop()

        return self.output_block(x)


# mel 频谱缓存基（官方 _mel_basis 惰性构建）
_MEL_BASIS: Optional[np.ndarray] = None


def _preemphasis(wav: np.ndarray) -> np.ndarray:
    """官方 preemphasis 滤波（scipy.signal.lfilter 实现）。

    Args:
        wav: 16kHz 单声道波形。

    Returns:
        np.ndarray: 预加重后的波形。
    """
    return signal.lfilter([1, -_HPARAMS["preemphasis"]], [1], wav)


def _amp_to_db(x: np.ndarray) -> np.ndarray:
    """幅度转分贝（官方 _amp_to_db，含 min_level 底噪钳制）。

    Args:
        x: 线性幅度谱。

    Returns:
        np.ndarray: dB 谱。
    """
    min_level = np.exp(_HPARAMS["min_level_db"] / 20 * np.log(10))
    return 20 * np.log10(np.maximum(min_level, x))


def _linear_to_mel(spectrogram: np.ndarray) -> np.ndarray:
    """线性谱投影到 mel 刻度（官方 _linear_to_mel，librosa 滤波器组）。

    Args:
        spectrogram: 线性幅度谱 (n_mels?, T)——实际 (freq, T)。

    Returns:
        np.ndarray: mel 幅度谱 (80, T)。
    """
    global _MEL_BASIS
    if _MEL_BASIS is None:
        _MEL_BASIS = librosa.filters.mel(
            sr=_HPARAMS["sample_rate"], n_fft=_HPARAMS["n_fft"],
            n_mels=_HPARAMS["num_mels"], fmin=_HPARAMS["fmin"], fmax=_HPARAMS["fmax"],
        )
    return np.dot(_MEL_BASIS, spectrogram)


def _normalize(S: np.ndarray) -> np.ndarray:
    """官方 _normalize：dB 谱归一到 [-4, 4]（对称 mel）。"""
    return np.clip(
        (2 * _HPARAMS["max_abs_value"]) * (
            (S - _HPARAMS["min_level_db"]) / (-_HPARAMS["min_level_db"])
        ) - _HPARAMS["max_abs_value"],
        -_HPARAMS["max_abs_value"], _HPARAMS["max_abs_value"],
    )


def melspectrogram(wav: np.ndarray) -> np.ndarray:
    """官方 audio.melspectrogram 等价实现（librosa stft 数值口径）。

    Args:
        wav: 16kHz 单声道波形。

    Returns:
        np.ndarray: mel 频谱 (80, T)，值域 [-4, 4]。
    """
    D = librosa.stft(
        y=_preemphasis(wav), n_fft=_HPARAMS["n_fft"],
        hop_length=_HPARAMS["hop_size"], win_length=_HPARAMS["win_size"],
    )
    S = _amp_to_db(_linear_to_mel(np.abs(D))) - _HPARAMS["ref_level_db"]
    return _normalize(S)


def _load_model(model_dir: Path, device: str = "cpu") -> Wav2Lip:
    """加载 Wav2Lip 生成器权重（官方 load_model 口径：state_dict 剥 module. 前缀）。

    Args:
        model_dir: 模型目录（含 wav2lip_gan.pth）。
        device: 推理设备（"cpu"）。

    Returns:
        Wav2Lip: eval 模式的生成器。

    Raises:
        Exception: 权重缺失/损坏向上抛出（由调用方降级）。
    """
    model = Wav2Lip()
    # 老式 pickle checkpoint：显式关闭 weights_only 校验（torch 2.6+ 默认翻转）
    checkpoint = torch.load(model_dir / "wav2lip_gan.pth", map_location=device, weights_only=False)
    state = checkpoint["state_dict"]
    new_state = {k.replace("module.", ""): v for k, v in state.items()}
    model.load_state_dict(new_state)
    return model.to(device).eval()


def _detect_face_boxes(video_path: Path, frame_count: int, detector: S3FDDetector) -> Optional[List[np.ndarray]]:
    """第一遍流式：逐 batch 检测人脸框 → 空帧邻近传播 → 平滑 → padding。

    Args:
        video_path: 输入视频路径。
        frame_count: 参与处理的帧数。
        detector: S3FD 检测器。

    Returns:
        Optional[List[np.ndarray]]: 每帧人脸框 [x1, y1, x2, y2]；全视频
            无人脸返回 None。
    """
    cap = cv2.VideoCapture(str(video_path))
    raw_boxes: List[Optional[np.ndarray]] = []
    batch: List[np.ndarray] = []
    det_scale = 1.0
    while len(raw_boxes) < frame_count:
        ok, frame = cap.read()
        if not ok:
            break
        # 检测缩放：长边超限时等比缩小（S3FD CPU 提速 ~4 倍），框后续还原
        h, w = frame.shape[:2]
        max_edge = max(h, w)
        if max_edge > _DET_MAX_EDGE:
            det_scale = _DET_MAX_EDGE / max_edge
            frame = cv2.resize(frame, (int(w * det_scale), int(h * det_scale)))
        else:
            det_scale = 1.0
        batch.append(frame)
        if len(batch) >= _FACE_DET_BATCH or len(raw_boxes) + len(batch) >= frame_count:
            for b in detector.detect_main_faces(batch):
                if b is None:
                    raw_boxes.append(None)
                else:
                    raw_boxes.append(b / det_scale)
            batch = []
    if batch:
        for b in detector.detect_main_faces(batch):
            if b is None:
                raw_boxes.append(None)
            else:
                raw_boxes.append(b / det_scale)
    cap.release()
    if len(raw_boxes) < frame_count:
        raw_boxes.extend([None] * (frame_count - len(raw_boxes)))

    # 全视频无人脸：无法口型同步
    if all(b is None for b in raw_boxes):
        return None

    # 空帧传播：None 框用最近的检出框填充（前后就近，官方以报错处理，
    # 本服务降级传播保证产物完整）
    last_seen: Optional[np.ndarray] = None
    for i, b in enumerate(raw_boxes):
        if b is not None:
            last_seen = b
        elif last_seen is not None:
            raw_boxes[i] = last_seen
    for i in range(len(raw_boxes) - 1, -1, -1):
        if raw_boxes[i] is not None:
            break
    for i in range(len(raw_boxes)):
        if raw_boxes[i] is None:
            nxt = next((raw_boxes[j] for j in range(i, len(raw_boxes)) if raw_boxes[j] is not None), None)
            if nxt is not None:
                raw_boxes[i] = nxt

    # 时间窗平滑（官方 get_smoothened_boxes T=5）
    boxes = np.array([b[:4] for b in raw_boxes], dtype=np.float32)
    T = _SMOOTH_WINDOW
    for i in range(len(boxes)):
        window = boxes[max(0, min(i, len(boxes) - T)): i + T if i + T <= len(boxes) else len(boxes)]
        if len(window):
            boxes[i] = np.mean(window, axis=0)

    # chin padding（官方 face_detect pads 逻辑）
    pady1, pady2, padx1, padx2 = _PADS
    h, w = _probe_frame_size(video_path)
    results: List[np.ndarray] = []
    for (x1, y1, x2, y2) in boxes:
        ry1 = max(0, int(y1) - pady1)
        ry2 = min(h, int(y2) + pady2)
        rx1 = max(0, int(x1) - padx1)
        rx2 = min(w, int(x2) + padx2)
        if ry2 - ry1 < 8 or rx2 - rx1 < 8:
            rx1, ry1, rx2, ry2 = int(x1), int(y1), int(x2), int(y2)
        results.append(np.array([rx1, ry1, rx2, ry2], dtype=np.int32))
    return results


def _probe_frame_size(video_path: Path) -> tuple:
    """读取视频首帧尺寸（平滑 padding 用）。

    Args:
        video_path: 视频路径。

    Returns:
        tuple: (height, width)；读取失败返回 (720, 1280) 兜底。
    """
    cap = cv2.VideoCapture(str(video_path))
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        return (720, 1280)
    return frame.shape[0], frame.shape[1]


def lipsync_video_sync(
    video_path: Path,
    audio_path: Path,
    out_path: Path,
    model_dir: Path,
) -> bool:
    """口型同步主流程（同步阻塞实现，调用方用 asyncio.to_thread 包装）。

    Args:
        video_path: 输入视频（无声或带声均可，产物音轨以配音为准）。
        audio_path: 驱动配音 WAV 路径。
        out_path: 输出 mp4 路径（口型对齐，音轨=配音）。
        model_dir: 模型目录（wav2lip_gan.pth + s3fd.pth）。

    Returns:
        bool: 成功 True；模型缺失/无人脸/任一环节失败 False（调用方降级）。

    Raises:
        无：内部捕获全部异常收敛为 False。
    """
    wav2lip_path = model_dir / "wav2lip_gan.pth"
    s3fd_path = model_dir / "s3fd.pth"
    if not wav2lip_path.is_file() or not s3fd_path.is_file():
        logger.warning("口型同步模型权重缺失（%s / %s），跳过", wav2lip_path.name, s3fd_path.name)
        return False
    if not video_path.is_file() or not audio_path.is_file():
        logger.warning("口型同步输入缺失，跳过")
        return False
    try:
        # 1) 音频 → mel chunk 序列（官方口径）
        wav, _sr = librosa.load(str(audio_path), sr=_HPARAMS["sample_rate"])
        # TTS 静音段可能产生 NaN（官方提示加 epsilon 噪声）
        if np.isnan(wav).any():
            wav = np.where(np.isnan(wav), 0.0, wav) + 1e-6 * np.random.uniform(-1, 1, size=wav.shape)
        mel = melspectrogram(wav)
        if np.isnan(mel.reshape(-1)).sum() > 0:
            logger.warning("mel 含 NaN，口型同步中止")
            return False

        cap = cv2.VideoCapture(str(video_path))
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        if not fps or fps <= 0 or math.isnan(fps):
            fps = 25.0

        # mel chunk 序列（官方：80/fps 索引步长，每 chunk 16 mel hop）
        mel_chunks: List[np.ndarray] = []
        mel_idx_multiplier = 80.0 / fps
        i = 0
        while True:
            start_idx = int(i * mel_idx_multiplier)
            if start_idx + _MEL_STEP_SIZE > mel.shape[1]:
                mel_chunks.append(mel[:, mel.shape[1] - _MEL_STEP_SIZE:])
                break
            mel_chunks.append(mel[:, start_idx: start_idx + _MEL_STEP_SIZE])
            i += 1

        # 输出帧数 = min(mel chunk 数, 视频实际帧数)
        cap = cv2.VideoCapture(str(video_path))
        video_frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        if video_frame_count <= 0:
            video_frame_count = len(mel_chunks)
        frame_count = min(len(mel_chunks), video_frame_count)
        if frame_count <= 0:
            return False

        # 2) 第一遍：人脸检测（每帧主脸框）
        detector = S3FDDetector(str(s3fd_path))
        boxes = _detect_face_boxes(video_path, frame_count, detector)
        if boxes is None:
            logger.warning("视频未检出任何人脸，口型同步中止：%s", video_path.name)
            return False

        # 3) 第二遍流式：逐 batch 生成下半脸并贴回写帧
        model = _load_model(model_dir)
        frame_h, frame_w = _probe_frame_size(video_path)[0], _probe_frame_size(video_path)[1]
        tmp_dir = Path(tempfile.mkdtemp(prefix="lipsync_"))
        avi_path = tmp_dir / "result.avi"
        writer = cv2.VideoWriter(
            str(avi_path), cv2.VideoWriter_fourcc(*"DIVX"), fps, (frame_w, frame_h))

        cap = cv2.VideoCapture(str(video_path))
        img_batch: List[np.ndarray] = []
        mel_batch: List[np.ndarray] = []
        frame_batch: List[np.ndarray] = []
        coords_batch: List[np.ndarray] = []

        def _flush() -> None:
            """消费当前 batch：Wav2Lip 推理并写帧（内部闭包）。

            Returns:
                None
            """
            if not img_batch:
                return
            imgs = np.asarray(img_batch)
            # 官方链路：mels (N,80,16) → reshape (N,80,16,1) → transpose
            # (0,3,1,2) → tensor (N,1,80,16)（audio encoder 首层 Conv2d(1,32)）
            mels = np.asarray(mel_batch)
            mels = np.reshape(mels, [len(mels), mels.shape[1], mels.shape[2], 1])
            img_masked = imgs.copy()
            img_masked[:, _IMG_SIZE // 2:] = 0
            inp = np.concatenate((img_masked, imgs), axis=3) / 255.0
            inp = torch.FloatTensor(np.transpose(inp, (0, 3, 1, 2)))
            mel_inp = torch.FloatTensor(np.transpose(mels, (0, 3, 1, 2)))
            with torch.no_grad():
                pred = model(mel_inp, inp)
            pred = pred.cpu().numpy().transpose(0, 2, 3, 1) * 255.0
            for p, frame, (y1, y2, x1, x2) in zip(pred, frame_batch, coords_batch):
                p = cv2.resize(p.astype(np.uint8), (x2 - x1, y2 - y1))
                frame[y1:y2, x1:x2] = p
                writer.write(frame)
            img_batch.clear()
            mel_batch.clear()
            frame_batch.clear()
            coords_batch.clear()

        for idx in range(frame_count):
            ok, frame = cap.read()
            if not ok:
                break
            mel_idx = min(idx, len(mel_chunks) - 1)
            m = mel_chunks[mel_idx]
            face_box = boxes[idx]
            x1, y1, x2, y2 = face_box
            face = frame[y1:y2, x1:x2]
            if face.size == 0:
                continue
            face = cv2.resize(face, (_IMG_SIZE, _IMG_SIZE))
            img_batch.append(face)
            mel_batch.append(m)
            frame_batch.append(frame)
            coords_batch.append((y1, y2, x1, x2))
            if len(img_batch) >= _WAV2LIP_BATCH:
                _flush()
        if img_batch:
            _flush()
        cap.release()
        writer.release()

        # 4) ffmpeg 合成（产物音轨 = 驱动配音；编码参数与 compose 阶段对齐）
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(avi_path), "-i", str(audio_path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-shortest",
            "-movflags", "+faststart",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
        try:
            import shutil as _shutil

            _shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001 —— 临时目录清理失败不阻断
            pass
        if proc.returncode != 0 or not out_path.is_file():
            logger.warning("口型同步 ffmpeg 合成失败：%s", (proc.stderr or b"")[-300:])
            return False
        logger.info("口型同步完成：%s（%d 帧 @ %.1ffps）", out_path.name, frame_count, fps)
        return True
    except Exception:  # noqa: BLE001 —— 口型同步任何失败降级，不阻断任务
        logger.exception("口型同步异常：%s", video_path.name)
        return False


async def lipsync_video(
    video_path: Path,
    audio_path: Path,
    out_path: Path,
    model_dir: Path,
) -> bool:
    """口型同步异步入口（CPU 密集推理经 to_thread 包装不阻塞事件循环）。

    Args:
        video_path: 输入视频路径。
        audio_path: 驱动配音 WAV 路径。
        out_path: 输出 mp4 路径。
        model_dir: 模型目录。

    Returns:
        bool: 成功 True；失败 False（调用方降级为不做口型同步）。

    Raises:
        无：内部同步实现已收敛全部异常。
    """
    import asyncio

    return await asyncio.to_thread(
        lipsync_video_sync, video_path, audio_path, out_path, model_dir,
    )
