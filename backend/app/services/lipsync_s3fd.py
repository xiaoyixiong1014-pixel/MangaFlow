"""S3FD 人脸检测器模块（文件级说明）。

vendor 自 Wav2Lip 官方仓库（Rudrabha/Wav2Lip，BSD 相关组件：
face_detection/detection/sfd，模型权重 s3fd.pth），供口型同步服务
（lipsync_service）逐帧检测人脸框使用。移植要点：
- 保留官方网络结构（s3fd + L2Norm）与 batch_detect / nms / batch_decode
  数值逻辑，保证检测框与官方推理一致；
- torch 2.x 兼容：权重加载显式 weights_only=False（老式 pickle checkpoint）；
- 精简掉 landmarks/FAN 相关依赖（本服务只需要人脸框）；
- 输入为 BGR uint8 帧（cv2.VideoCapture 原始读出），内部做
  [104,117,123] BGR 均值减除（官方预处理）。

检测口径（与官方 inference.py face_detect 对齐）：对每帧取**置信度最高**
的单个人脸框（分镜视频通常单角色特写/近景，多脸场景取主脸），由调用方
做框平滑与 chin padding。
"""

import logging
import math
from typing import List, Optional

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

logger = logging.getLogger("mangaflow.services.lipsync_s3fd")

# 检测框置信度阈值（官方 SFDDetector.detect_from_batch 口径 x[-1] > 0.5）
_SCORE_THRESHOLD = 0.5
# NMS 的 IoU 阈值（官方口径 0.3）
_NMS_THRESHOLD = 0.3
# 候选框筛选阈值（detect 内部 ocls > 0.05，官方口径）
_CANDIDATE_THRESHOLD = 0.05


class L2Norm(nn.Module):
    """通道级 L2 归一化层（官方 s3fd 组件，带可学习 scale 权重）。"""

    def __init__(self, n_channels: int, scale: float = 1.0) -> None:
        """初始化归一化层。

        Args:
            n_channels: 通道数。
            scale: 初始化的缩放权重值。

        Returns:
            None
        """
        super().__init__()
        self.n_channels = n_channels
        self.scale = scale
        self.eps = 1e-10
        self.weight = nn.Parameter(torch.Tensor(self.n_channels))
        self.weight.data *= 0.0
        self.weight.data += self.scale

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """前向：按通道 L2 归一化后乘以 scale 权重。

        Args:
            x: 输入特征图 (B, C, H, W)。

        Returns:
            torch.Tensor: 归一化后的特征图。
        """
        norm = x.pow(2).sum(dim=1, keepdim=True).sqrt() + self.eps
        x = x / norm * self.weight.view(1, -1, 1, 1)
        return x


class S3FDNet(nn.Module):
    """S3FD 单阶段人脸检测网络（官方结构，VGG16 骨干 + FPN 特征层）。"""

    def __init__(self) -> None:
        """按官方结构初始化网络层。

        Args:
            无。

        Returns:
            None
        """
        super().__init__()
        self.conv1_1 = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1)
        self.conv1_2 = nn.Conv2d(64, 64, kernel_size=3, stride=1, padding=1)

        self.conv2_1 = nn.Conv2d(64, 128, kernel_size=3, stride=1, padding=1)
        self.conv2_2 = nn.Conv2d(128, 128, kernel_size=3, stride=1, padding=1)

        self.conv3_1 = nn.Conv2d(128, 256, kernel_size=3, stride=1, padding=1)
        self.conv3_2 = nn.Conv2d(256, 256, kernel_size=3, stride=1, padding=1)
        self.conv3_3 = nn.Conv2d(256, 256, kernel_size=3, stride=1, padding=1)

        self.conv4_1 = nn.Conv2d(256, 512, kernel_size=3, stride=1, padding=1)
        self.conv4_2 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)
        self.conv4_3 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)

        self.conv5_1 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)
        self.conv5_2 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)
        self.conv5_3 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)

        self.fc6 = nn.Conv2d(512, 1024, kernel_size=3, stride=1, padding=3)
        self.fc7 = nn.Conv2d(1024, 1024, kernel_size=1, stride=1, padding=0)

        self.conv6_1 = nn.Conv2d(1024, 256, kernel_size=1, stride=1, padding=0)
        self.conv6_2 = nn.Conv2d(256, 512, kernel_size=3, stride=2, padding=1)

        self.conv7_1 = nn.Conv2d(512, 128, kernel_size=1, stride=1, padding=0)
        self.conv7_2 = nn.Conv2d(128, 256, kernel_size=3, stride=2, padding=1)

        self.conv3_3_norm = L2Norm(256, scale=10)
        self.conv4_3_norm = L2Norm(512, scale=8)
        self.conv5_3_norm = L2Norm(512, scale=5)

        self.conv3_3_norm_mbox_conf = nn.Conv2d(256, 4, kernel_size=3, stride=1, padding=1)
        self.conv3_3_norm_mbox_loc = nn.Conv2d(256, 4, kernel_size=3, stride=1, padding=1)
        self.conv4_3_norm_mbox_conf = nn.Conv2d(512, 2, kernel_size=3, stride=1, padding=1)
        self.conv4_3_norm_mbox_loc = nn.Conv2d(512, 4, kernel_size=3, stride=1, padding=1)
        self.conv5_3_norm_mbox_conf = nn.Conv2d(512, 2, kernel_size=3, stride=1, padding=1)
        self.conv5_3_norm_mbox_loc = nn.Conv2d(512, 4, kernel_size=3, stride=1, padding=1)

        self.fc7_mbox_conf = nn.Conv2d(1024, 2, kernel_size=3, stride=1, padding=1)
        self.fc7_mbox_loc = nn.Conv2d(1024, 4, kernel_size=3, stride=1, padding=1)
        self.conv6_2_mbox_conf = nn.Conv2d(512, 2, kernel_size=3, stride=1, padding=1)
        self.conv6_2_mbox_loc = nn.Conv2d(512, 4, kernel_size=3, stride=1, padding=1)
        self.conv7_2_mbox_conf = nn.Conv2d(256, 2, kernel_size=3, stride=1, padding=1)
        self.conv7_2_mbox_loc = nn.Conv2d(256, 4, kernel_size=3, stride=1, padding=1)

    def forward(self, x: torch.Tensor) -> list:
        """前向：输出 6 个特征层的分类/回归图列表（官方 max-out 背景标签）。

        Args:
            x: 预处理后的输入图像 (B, 3, H, W)。

        Returns:
            list: [cls1, reg1, ..., cls6, reg6] 共 12 个张量。
        """
        h = F.relu(self.conv1_1(x))
        h = F.relu(self.conv1_2(h))
        h = F.max_pool2d(h, 2, 2)

        h = F.relu(self.conv2_1(h))
        h = F.relu(self.conv2_2(h))
        h = F.max_pool2d(h, 2, 2)

        h = F.relu(self.conv3_1(h))
        h = F.relu(self.conv3_2(h))
        h = F.relu(self.conv3_3(h))
        f3_3 = h
        h = F.max_pool2d(h, 2, 2)

        h = F.relu(self.conv4_1(h))
        h = F.relu(self.conv4_2(h))
        h = F.relu(self.conv4_3(h))
        f4_3 = h
        h = F.max_pool2d(h, 2, 2)

        h = F.relu(self.conv5_1(h))
        h = F.relu(self.conv5_2(h))
        h = F.relu(self.conv5_3(h))
        f5_3 = h
        h = F.max_pool2d(h, 2, 2)

        h = F.relu(self.fc6(h))
        h = F.relu(self.fc7(h))
        ffc7 = h
        h = F.relu(self.conv6_1(h))
        h = F.relu(self.conv6_2(h))
        f6_2 = h
        h = F.relu(self.conv7_1(h))
        h = F.relu(self.conv7_2(h))
        f7_2 = h

        f3_3 = self.conv3_3_norm(f3_3)
        f4_3 = self.conv4_3_norm(f4_3)
        f5_3 = self.conv5_3_norm(f5_3)

        cls1 = self.conv3_3_norm_mbox_conf(f3_3)
        reg1 = self.conv3_3_norm_mbox_loc(f3_3)
        cls2 = self.conv4_3_norm_mbox_conf(f4_3)
        reg2 = self.conv4_3_norm_mbox_loc(f4_3)
        cls3 = self.conv5_3_norm_mbox_conf(f5_3)
        reg3 = self.conv5_3_norm_mbox_loc(f5_3)
        cls4 = self.fc7_mbox_conf(ffc7)
        reg4 = self.fc7_mbox_loc(ffc7)
        cls5 = self.conv6_2_mbox_conf(f6_2)
        reg5 = self.conv6_2_mbox_loc(f6_2)
        cls6 = self.conv7_2_mbox_conf(f7_2)
        reg6 = self.conv7_2_mbox_loc(f7_2)

        # max-out background label（官方口径：cls1 的 4 通道取前 3 通道最大值合并）
        chunk = torch.chunk(cls1, 4, 1)
        bmax = torch.max(torch.max(chunk[0], chunk[1]), chunk[2])
        cls1 = torch.cat([bmax, chunk[3]], dim=1)

        return [cls1, reg1, cls2, reg2, cls3, reg3, cls4, reg4, cls5, reg5, cls6, reg6]


def _nms(dets: np.ndarray, thresh: float) -> list:
    """非极大值抑制（官方 bbox.nms 实现）。

    Args:
        dets: 检测框数组 (N, 5)，每行 [x1, y1, x2, y2, score]。
        thresh: IoU 阈值。

    Returns:
        list: 保留框的下标列表。
    """
    if 0 == len(dets):
        return []
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]

    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        xx1, yy1 = np.maximum(x1[i], x1[order[1:]]), np.maximum(y1[i], y1[order[1:]])
        xx2, yy2 = np.minimum(x2[i], x2[order[1:]]), np.minimum(y2[i], y2[order[1:]])

        w, h = np.maximum(0.0, xx2 - xx1 + 1), np.maximum(0.0, yy2 - yy1 + 1)
        ovr = w * h / (areas[i] + areas[order[1:]] - w * h)

        inds = np.where(ovr <= thresh)[0]
        order = order[inds + 1]

    return keep


def _batch_decode(loc: torch.Tensor, priors: torch.Tensor, variances: list) -> torch.Tensor:
    """按先验框解码回归偏移为绝对坐标（官方 bbox.batch_decode 实现）。

    Args:
        loc: 回归偏移 (B, N, 4)。
        priors: 先验框 (1, N, 4)（cx, cy, w, h）。
        variances: [0.1, 0.2] 编码方差。

    Returns:
        torch.Tensor: 解码后的绝对框 (B, N, 4)（x1, y1, x2, y2）。
    """
    boxes = torch.cat((
        priors[:, :, :2] + loc[:, :, :2] * variances[0] * priors[:, :, 2:],
        priors[:, :, 2:] * torch.exp(loc[:, :, 2:] * variances[1])), 2)
    boxes[:, :, :2] -= boxes[:, :, 2:] / 2
    boxes[:, :, 2:] += boxes[:, :, :2]
    return boxes


def _batch_detect(net: S3FDNet, imgs: np.ndarray, device: str) -> np.ndarray:
    """批量检测人脸（官方 detect.batch_detect 数值逻辑精简版）。

    Args:
        net: S3FD 网络实例（eval 模式）。
        imgs: BGR uint8 图像批次 (B, H, W, 3)。
        device: 推理设备（"cpu"）。

    Returns:
        np.ndarray: (B, K, 5) 每张图的候选框集（K 随层拼接变化，含低分框，
            由调用方 NMS + 阈值筛选）。
    """
    imgs = imgs - np.array([104, 117, 123])
    imgs = imgs.transpose(0, 3, 1, 2)

    imgs = torch.from_numpy(imgs).float().to(device)
    BB, CC, HH, WW = imgs.size()
    with torch.no_grad():
        olist = net(imgs)

    bboxlist = []
    for i in range(len(olist) // 2):
        olist[i * 2] = F.softmax(olist[i * 2], dim=1)
    olist = [oelem.data.cpu() for oelem in olist]
    for i in range(len(olist) // 2):
        ocls, oreg = olist[i * 2], olist[i * 2 + 1]
        FB, FC, FH, FW = ocls.size()
        stride = 2 ** (i + 2)  # 4,8,16,32,64,128
        poss = zip(*np.where(ocls[:, 1, :, :] > _CANDIDATE_THRESHOLD))
        for _Iindex, hindex, windex in poss:
            axc, ayc = stride / 2 + windex * stride, stride / 2 + hindex * stride
            score = ocls[:, 1, hindex, windex]
            loc = oreg[:, :, hindex, windex].contiguous().view(BB, 1, 4)
            priors = torch.Tensor(
                [[axc / 1.0, ayc / 1.0, stride * 4 / 1.0, stride * 4 / 1.0]]
            ).view(1, 1, 4)
            variances = [0.1, 0.2]
            box = _batch_decode(loc, priors, variances)
            box = box[:, 0] * 1.0
            bboxlist.append(torch.cat([box, score.unsqueeze(1)], 1).cpu().numpy())
    bboxlist = np.array(bboxlist)
    if 0 == len(bboxlist):
        bboxlist = np.zeros((1, BB, 5))
    return bboxlist


class S3FDDetector:
    """S3FD 人脸检测器（CPU 推理，单例复用）。

    职责：加载官方 s3fd.pth 权重，逐 batch 检测输入帧的主脸框
    （每帧取置信度最高且过 0.5 阈值的框；无检出返回 None）。
    """

    def __init__(self, weight_path: str, device: str = "cpu") -> None:
        """加载网络与权重。

        Args:
            weight_path: s3fd.pth 权重文件路径。
            device: 推理设备（本服务固定 "cpu"）。

        Raises:
            Exception: 权重文件缺失或损坏时向上抛出（由调用方降级）。
        """
        self.device = device
        self.net = S3FDNet()
        # 老式 pickle checkpoint：显式关闭 weights_only 校验（torch 2.6+ 默认翻转）
        state = torch.load(weight_path, map_location=device, weights_only=False)
        self.net.load_state_dict(state)
        self.net.to(device)
        self.net.eval()

    def detect_main_faces(self, frames: List[np.ndarray]) -> List[Optional[np.ndarray]]:
        """批量检测每帧的主脸框（内部函数约定输入 BGR uint8）。

        Args:
            frames: BGR uint8 帧列表 (H, W, 3)。

        Returns:
            List[Optional[np.ndarray]]: 每帧主脸框 [x1, y1, x2, y2, score]；
                该帧无检出为 None。
        """
        if not frames:
            return []
        try:
            bboxlists = _batch_detect(self.net, np.array(frames), self.device)
        except Exception:  # noqa: BLE001 —— 检测失败按全部未检出处理（调用方降级）
            logger.exception("S3FD 批量检测失败（batch=%d）", len(frames))
            return [None] * len(frames)
        results: List[Optional[np.ndarray]] = []
        for i in range(len(frames)):
            dets = bboxlists[:, i, :]
            keep = _nms(dets, _NMS_THRESHOLD)
            dets = dets[keep, :]
            dets = [x for x in dets if x[-1] > _SCORE_THRESHOLD]
            if not dets:
                results.append(None)
                continue
            best = max(dets, key=lambda x: float(x[-1]))
            results.append(np.array(best, dtype=np.float32))
        return results
