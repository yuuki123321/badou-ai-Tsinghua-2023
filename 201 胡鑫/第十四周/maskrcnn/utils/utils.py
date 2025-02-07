"""
Mask R-CNN
通用实用程序函数和类。
"""
import sys
import os
import logging
import math
import random
import skimage
import skimage.transform
import numpy as np
import tensorflow as tf
import scipy
import urllib.request
import shutil
import warnings
from distutils.version import LooseVersion

# 下载最新COCO训练权重的URL
COCO_MODEL_URL = "https://github.com/matterport/Mask_RCNN/releases/download/v2.0/mask_rcnn_coco.h5"


def extract_bboxes(mask):
    # 利用语义分割的mask找到包围他的框
    boxes = np.zeros([mask.shape[-1], 4], dtype=np.int32)
    for i in range(mask.shape[-1]):
        m = mask[:, :, i]
        horizontal_indices = np.where(np.any(m, axis=0))[0]
        vertical_indices = np.where(np.any(m, axis=1))[0]
        if horizontal_indices.shape[0]:
            x1, x2 = horizontal_indices[[0, -1]]
            y1, y2 = vertical_indices[[0, -1]]
            x2 += 1
            y2 += 1
        else:
            x1, x2, y1, y2 = 0, 0, 0, 0
        boxes[i] = np.array([y1, x1, y2, x2])
    return boxes.astype(np.int32)


def compute_iou(box, boxes, box_area, boxes_area):
    """使用给定框的数组计算给定框的IoU。
    box：1D矢量[y1，x1，y2，x2]
    boxes:[boxes_count，（y1，x1，y2，x2）]
    box_area：浮动。“box”的面积
    boxes_area：长度boxes_count的数组。
    注：此处传入面积而非计算面积
    效率在调用者中计算一次以避免重复工作。
    """
    # 计算i
    y1 = np.maximum(box[0], boxes[:, 0])
    x1 = np.maximum(box[1], boxes[:, 1])
    y2 = np.minimum(box[2], boxes[:, 2])
    x2 = np.minimum(box[3], boxes[:, 3])
    intersection = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)
    # u
    union = box_area + boxes_area[:] - intersection[:]
    iou = intersection / union
    return iou


def compute_overlaps(boxes1, boxes2):
    """
    计算两组框之间的IoU重叠。
    boxes1，boxes2: [N，（y1，x1，y2，x2）]。
    为了获得更好的性能，先通过最大的一组，然后通过较小的第二组。
    """
    # 先验框和gt_boxes的面积
    area1 = (boxes1[:, 2] - boxes1[:, 0]) * (boxes1[:, 3] - boxes1[:, 1])
    area2 = (boxes2[:, 2] - boxes2[:, 0]) * (boxes2[:, 3] - boxes2[:, 1])
    # 计算重叠以生成矩阵[boxes1 count，boxes2 count]
    # 每个单元格都包含IoU值。
    overlaps = np.zeros([boxes1.shape[0], boxes2.shape[0]])
    for i in range(overlaps.shape[1]):
        overlaps[:, i] = compute_iou(boxes2[i], boxes1, area2[i], area1)
    return overlaps


def non_max_suppression(boxes, scores, threshold):
    """
    执行非最大值抑制并返回保留框的索引。
    boxes：[N，（y1，x1，y2，x2）]。请注意，（y2，x2）位于长方体外部。
    scores：方框分数的一维数组。
    threshold：float。用于筛选的IoU阈值。
    """
    assert boxes.shape[0] > 0
    if boxes.dtype.kind != 'f':
        boxes = boxes.astype(np.float32)

    # 计算边框面积
    y1 = boxes[:, 0]
    x1 = boxes[:, 1]
    y2 = boxes[:, 2]
    x2 = boxes[:, 3]
    area = (y2 - y1) * (x2 - x1)

    # 获取按分数排序的方框索引（最高优先）
    ixs = scores.argsort()[::-1]

    pick = []
    while len(ixs) > 0:
        # 拾取顶部框并将其索引添加到列表中
        i = ixs[0]
        pick.append(i)
        # 计算拾取的框与其他框的IoU
        iou = compute_iou(boxes[i], boxes[ixs[1:]], area[i], area[ixs[1:]])
        # 识别IoU超过阈值的方框。这将索引返回到ixs[1:]中，
        # 所以加1得到索引为ixs。ixs[1:]中从1开始就等于ixs中从0开始
        remove_ixs = np.where(iou > threshold)[0] + 1
        # 删除拾取框和重叠框的索引。
        ixs = np.delete(ixs, remove_ixs)
        ixs = np.delete(ixs, 0)
    return np.array(pick, dtype=np.int32)


def box_refinement_graph(box, gt_box):
    """编码运算"""
    box = tf.cast(box, tf.float32)
    gt_box = tf.cast(gt_box, tf.float32)

    height = box[:, 2] - box[:, 0]
    width = box[:, 3] - box[:, 1]
    center_y = box[:, 0] + 0.5 * height
    center_x = box[:, 1] + 0.5 * width

    gt_height = gt_box[:, 2] - gt_box[:, 0]
    gt_width = gt_box[:, 3] - gt_box[:, 1]
    gt_center_y = gt_box[:, 0] + 0.5 * gt_height
    gt_center_x = gt_box[:, 1] + 0.5 * gt_width

    dy = (gt_center_y - center_y) / height
    dx = (gt_center_x - center_x) / width
    dh = tf.log(gt_height / height)
    dw = tf.log(gt_width / width)

    result = tf.stack([dy, dx, dh, dw], axis=1)
    return result


def unmold_mask(mask, bbox, image_shape):
    """
    将神经网络生成的掩码转换为类似的格式
    恢复到原来的形状。
    mask：float类型的[height，width]。一个小的，通常是28x28的掩模。
    bbox:[y1，x1，y2，x2]。装口罩的盒子。
    返回与原始图像大小相同的二进制掩码。
    """
    threshold = 0.5
    y1, x1, y2, x2 = bbox
    mask = resize(mask, (y2 - y1, x2 - x1))
    mask = np.where(mask >= threshold, 1, 0).astype(np.bool)

    # 将mask放在正确的位置
    full_mask = np.zeros(image_shape[:2], dtype=np.bool)
    full_mask[y1:y2, x1:x2] = mask
    return full_mask


def batch_slice(inputs, graph_fn, batch_size, names=None):
    """
    将输入拆分为切片，并将每个切片提供给给定的
    计算图，然后将结果进行组合。它允许您运行
    一批输入上的图，即使该图是为支持一个输入而写的
    仅限实例。
    inputs：张量列表。所有第一维度长度必须相同
    graph_fn：一个返回TF张量的函数，该张量是图的一部分。
    batch_size：将数据划分为的切片数。
    names：如果提供，则为生成的张量指定名称。
    """
    if not isinstance(inputs, list):
        inputs = [inputs]
    outputs = []

    for i in range(batch_size):
        inputs_slice = [x[i] for x in inputs]
        output_slice = graph_fn(*inputs_slice)
        if not isinstance(output_slice, (tuple, list)):
            output_slice = [output_slice]
        outputs.append(output_slice)
    # 更改切片列表中的输出，其中每个切片输出列表到输出列表，
    # 并且每个都具有切片列表
    outputs = list(zip(*outputs))
    if names is None:
        names = [None] * len(outputs)
    result = [tf.stack(o, axis=0, name=n) for o, n in zip(outputs, names)]
    if len(result) == 1:
        result = result[0]
    return result


def norm_boxes(boxes, shape):
    """
    将长方体从像素坐标转换为标准化坐标。
    boxes：像素坐标中的[N，（y1，x1，y2，x2）]
    shape:[…，（高度、宽度）]（以像素为单位）
    注：像素内坐标（y2，x2）在框外。但在正常化
    坐标在盒子里。
    return：
    归一化坐标中的[N，（y1，x1，y2，x2）]
    """
    h, w = shape
    scale = np.array([h - 1, w - 1, h - 1, w - 1])
    shift = np.array([0, 0, 1, 1])
    return np.divide((boxes - shift), scale).astype(np.float32)


def denorm_boxes(boxes, shape):
    """
    将boxes从标准化坐标转换为像素坐标。
    boxes：归一化坐标中的[N，（y1，x1，y2，x2）]
    shape:[…，（高度、宽度）]（以像素为单位）
    注：像素内坐标（y2，x2）在框外。但在正常化
    坐标在盒子里。
    returns：
    像素坐标中的[N，（y1，x1，y2，x2）]
    """
    h, w = shape
    scale = np.array([h - 1, w - 1, h - 1, w - 1])
    shift = np.array([0, 0, 1, 1])
    return np.around(np.multiply(boxes, scale) + shift).astype(np.int32)


def resize_image(image, min_dim=None, max_dim=None, min_scale=None, mode="square"):
    # 保持原有的image
    image_dtype = image.dtype
    # 初始化参数
    h, w = image.shape[:2]
    window = (0, 0, h, w)
    scale = 1
    padding = [(0, 0), (0, 0), (0, 0)]
    crop = None
    if mode == 'none':
        return image, window, scale, padding, crop

    # 计算变化的尺度
    if min_dim:
        scale = max(1, min_dim/min(h, w))
    if min_scale and scale < min_scale:
        scale = min_scale

    # 判断按照原来的尺寸缩放是否会超过最大边长
    if max_dim and mode == "square":
        image_max = max(h, w)
        if round(image_max * scale) > max_dim:
            scale = max_dim / image_max

    # 对图片进行resize
    if scale != 1:
        image = resize(image, (round(h * scale), round(w * scale)), preserve_range=True)

    # 是否需要padding填充
    if mode == "square":
        # 计算四周的padding情况
        h, w = image.shape[:2]

        top_pad = (max_dim - h) // 2
        bottom_pad = max_dim - h - top_pad
        left_pad = (max_dim - w) // 2
        right_pad = max_dim - w - left_pad

        # 向四周进行填充
        padding = [(top_pad, bottom_pad), (left_pad, right_pad), (0, 0)]
        image = np.pad(image, padding, mode='constant', constant_values=0)
        window = (top_pad, left_pad, h + top_pad, w + left_pad)
    elif mode == "pad64":
        h, w = image.shape[:2]
        assert min_dim % 64 == 0, "最小尺寸必须是64的倍数"

        if h % 64 > 0:
            max_h = h - (h % 64) + 64
            top_pad = (max_h - h) // 2
            bottom_pad = max_h - h - top_pad
        else:
            top_pad = bottom_pad = 0

        if w % 64 > 0:
            max_w = w - (w % 64) + 64
            left_pad = (max_w - w) // 2
            right_pad = max_w - w - left_pad
        else:
            left_pad = right_pad = 0
        padding = [(top_pad, bottom_pad), (left_pad, right_pad), (0, 0)]
        image = np.pad(image, padding, mode='constant', constant_values=0)
        window = (top_pad, left_pad, h + top_pad, w + left_pad)
    elif mode == "crop":
        # 随机选取crop
        h, w = image.shape[:2]
        y = random.randint(0, (h - min_dim))
        x = random.randint(0, (w - min_dim))
        crop = (y, x, min_dim, min_dim)
        image = image[y:y+min_dim, x:x+min_dim]
        window = (0, 0, min_dim, min_dim)
    else:
        raise Exception(f"Model {mode} not supported")

    return image.astype(image_dtype), window, scale, padding, crop


def resize_mask(mask, scale, padding, crop=None):
    # 将mask按照scale放大缩小后
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mask = scipy.ndimage.zoom(mask, zoom=[scale, scale, 1], order=0)
    if crop is not None:
        y, x, h, w = crop
        mask = mask[y:y+h, x:x+w]
    else:
        mask = np.pad(mask, padding, mode='constant', constant_values=0)
    return mask


def minimize_mask(bbox, mask, mini_shape):
    """减少语义分割载入时的size"""
    mini_mask = np.zeros(mini_shape + (mask.shape[-1],), dtype=bool)
    for i in range(mask.shape[-1]):
        # 如果load_mask（）返回错误的数据类型，则拾取切片并强制转换为布尔
        m = mask[:, :, i].astype(bool)
        y1, x1, y2, x2 = bbox[i][:4]
        m = m[y1:y2, x1:x2]
        if m.size == 0:
            raise Exception("Invalid bounding box with area of zero")

        # 使用双线性插值调整大小
        m = resize(m, mini_shape)
        mini_mask[:, :, i] = np.around(m).astype(np.bool)
    return mini_mask


def resize(image, output_shape, order=1, mode='constant', cval=0, clip=True,
           preserve_range=False, anti_aliasing=False, anti_aliasing_sigma=None):
    """
    Scikit Image resize（）的包装器。
    Scikit Image在每次调用resize（）时都会生成警告，如果没有
    接收正确的参数。正确的参数取决于版本吝啬。这通过根据
    版本它提供了一个中心位置来控制调整默认大小。
    """
    if LooseVersion(skimage.__version__) >= LooseVersion("0.14"):
        # 0.14中新增：抗锯齿。默认为False表示向后与撇渣的兼容性0.13。
        return skimage.transform.resize(image, output_shape, order=order, mode=mode, cval=cval,
                                        clip=clip, preserve_range=preserve_range,
                                        anti_aliasing=anti_aliasing, anti_aliasing_sigma=anti_aliasing_sigma)
    else:
        return skimage.transform.resize(image, output_shape, order=order, mode=mode, cval=cval,
                                        clip=clip, preserve_range=preserve_range)


def mold_image(images, config):
    """
    应为RGB图像（或图像数组）并进行减法
    平均像素，并将其转换为浮点。需要图像
    RGB顺序的颜色。
    """
    return images.astype(np.float32) - config.MEAN_PIXEL


def compose_image_meta(image_id, original_image_shape, image_shape, window, scale,
                       active_class_ids):
    """
    获取图像的属性，并将它们放在一个1D阵列中。
    image_id：图像的int id。对调试很有用。
    original_image_shape:调整大小或填充之前的[H，W，C]。
    image_shape:调整大小和填充后的[H，W，C]
    window：以像素为单位的（y1，x1，y2，x2）。真实图像所在的区域
    图像为（不包括填充）
    scale：应用于原始图像的缩放因子（float32）
    active_class_ids：数据集中可用的class_ids列表，其中
    图像出现了。如果对来自多个数据集的图像进行训练，则非常有用
    其中并非所有类都存在于所有数据集中。
    """
    meta = np.array(
        [image_id] +                   # size=1
        list(original_image_shape) +   # size=3
        list(image_shape) +            # size=3
        list(window) +                 # size=4 (y1, x1, y2, x2) 图象坐标
        [scale] +                      # size=1
        list(active_class_ids)         # size=num_classes
    )
    return meta


def mold_inputs(config, images):
    molded_images = []
    image_metas = []
    windows = []
    for image in images:
        # Resize image
        molded_image, window, scale, padding, crop = resize_image(
            image,
            min_dim=config.IMAGE_MIN_DIM,
            min_scale=config.IMAGE_MIN_SCALE,
            max_dim=config.IMAGE_MAX_DIM,
            mode=config.IMAGE_RESIZE_MODE
        )

        # print(np.shape(molded_image))
        molded_image = mold_image(molded_image, config)

        # build image_meta
        image_meta = compose_image_meta(0, image.shape, molded_image.shape, window,
                                        scale, np.zeros([config.NUM_CLASSES], dtype=np.int32))

        # append
        molded_images.append(molded_image)
        windows.append(window)
        image_metas.append(image_meta)

    # 打包到阵列中
    molded_images = np.stack(molded_images)
    image_metas = np.stack(image_metas)
    windows = np.stack(windows)

    return molded_images, image_metas, windows


def unmold_detections(detections, mrcnn_mask, original_image_shape, image_shape, window):
    zero_ix = np.where(detections[:, 4] == 0)[0]
    N = zero_ix[0] if zero_ix.shape[0] > 0 else detections.shape[0]

    boxes = detections[:N, :4]
    class_ids = detections[:N, 4].astype(np.int32)
    scores = detections[:N, 5]
    masks = mrcnn_mask[np.arange(N), :, :, class_ids]

    window = norm_boxes(window, image_shape[:2])

    wy1, wx1, wy2, wx2 = window
    shift = np.array([wy1, wx1, wy1, wx1])
    wh = wy2 - wy1
    ww = wx2 - wx1

    scale = np.array([wh, ww, wh, ww])
    boxes = np.divide(boxes - shift, scale)
    boxes = denorm_boxes(boxes, original_image_shape[:2])

    exclude_ix = np.where((boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1]) <= 0)[0]

    if exclude_ix.shape[0] > 0:
        boxes = np.delete(boxes, exclude_ix, axis=0)
        class_ids = np.delete(class_ids, exclude_ix, axis=0)
        scores = np.delete(scores, exclude_ix, axis=0)
        masks = np.delete(masks, exclude_ix, axis=0)
        N = class_ids.shape[0]

    full_masks = []
    for i in range(N):
        full_mask = unmold_mask(masks[i], boxes[i], original_image_shape)
        full_masks.append(full_mask)
    full_masks = np.stack(full_masks, axis=-1) if full_masks else np.empty(original_image_shape[:2] + (0,))

    return boxes, class_ids, scores, full_masks


def norm_boxes_graph(boxes, shape):
    """标准化，限制在0-1之间"""
    h, w = tf.split(tf.cast(shape, tf.float32), 2)
    scale = tf.concat([h, w, h, w], axis=-1) - tf.constant(1.0)
    shift = tf.constant([0., 0., 1., 1.])
    return tf.divide(boxes - shift, scale)


def parse_image_meta_graph(meta):
    """
        对输入的meta进行拆解
        将包含图像属性的张量解析为其组件。
        返回解析的张量的dict。
    """
    image_id = meta[:, 0]  # 图片的id
    original_image_shape = meta[:, 1:4]  # 原始图片的大小
    image_shape = meta[:, 4:7]  # resize后的图片的大小
    window = meta[:, 7:11]  # (y1, x1, y2, x2)有效的区域在图片中的位置
    scale = meta[:, 11]  # 长宽的变化状况
    active_class_ids = meta[:, 12:]  # 各类别的id
    return {
        "image_id": image_id,
        "original_image_shape": original_image_shape,
        "image_shape": image_shape,
        "window": window,
        "scale": scale,
        "active_class_ids": active_class_ids
    }
