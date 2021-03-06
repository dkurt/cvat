
# Copyright (C) 2019 Intel Corporation
#
# SPDX-License-Identifier: MIT

from collections import OrderedDict
import logging as log
import os
import os.path as osp

from datumaro.components.converter import Converter
from datumaro.components.extractor import AnnotationType
from datumaro.components.formats.yolo import YoloPath
from datumaro.util.image import save_image


def _make_yolo_bbox(img_size, box):
    # https://github.com/pjreddie/darknet/blob/master/scripts/voc_label.py
    # <x> <y> <width> <height> - values relative to width and height of image
    # <x> <y> - are center of rectangle
    x = (box[0] + box[2]) / 2 / img_size[0]
    y = (box[1] + box[3]) / 2 / img_size[1]
    w = (box[2] - box[0]) / img_size[0]
    h = (box[3] - box[1]) / img_size[1]
    return x, y, w, h

class YoloConverter(Converter):
    # https://github.com/AlexeyAB/darknet#how-to-train-to-detect-your-custom-objects

    def __init__(self, task=None, save_images=False, apply_colormap=False):
        super().__init__()
        self._task = task
        self._save_images = save_images
        self._apply_colormap = apply_colormap

    def __call__(self, extractor, save_dir):
        os.makedirs(save_dir, exist_ok=True)

        label_categories = extractor.categories()[AnnotationType.label]
        label_ids = {label.name: idx
            for idx, label in enumerate(label_categories.items)}
        with open(osp.join(save_dir, 'obj.names'), 'w') as f:
            f.writelines('%s\n' % l[0]
                for l in sorted(label_ids.items(), key=lambda x: x[1]))

        subsets = extractor.subsets()
        if len(subsets) == 0:
            subsets = [ None ]

        subset_lists = OrderedDict()

        for subset_name in subsets:
            if subset_name and subset_name in YoloPath.SUBSET_NAMES:
                subset = extractor.get_subset(subset_name)
            elif not subset_name:
                subset_name = YoloPath.DEFAULT_SUBSET_NAME
                subset = extractor
            else:
                log.warn("Skipping subset export '%s'. "
                    "If specified, the only valid names are %s" % \
                    (subset_name, ', '.join(
                        "'%s'" % s for s in YoloPath.SUBSET_NAMES)))
                continue

            subset_dir = osp.join(save_dir, 'obj_%s_data' % subset_name)
            os.makedirs(subset_dir, exist_ok=True)

            image_paths = OrderedDict()

            for item in subset:
                image_name = '%s.jpg' % item.id
                image_paths[item.id] = osp.join('data',
                    osp.basename(subset_dir), image_name)

                if self._save_images:
                    image_path = osp.join(subset_dir, image_name)
                    if not osp.exists(image_path):
                        save_image(image_path, item.image)

                height, width, _ = item.image.shape

                yolo_annotation = ''
                for bbox in item.annotations:
                    if bbox.type is not AnnotationType.bbox:
                        continue
                    if bbox.label is None:
                        continue

                    yolo_bb = _make_yolo_bbox((width, height), bbox.points)
                    yolo_bb = ' '.join('%.6f' % p for p in yolo_bb)
                    yolo_annotation += '%s %s\n' % (bbox.label, yolo_bb)

                annotation_path = osp.join(subset_dir, '%s.txt' % item.id)
                with open(annotation_path, 'w') as f:
                    f.write(yolo_annotation)

            subset_list_name = '%s.txt' % subset_name
            subset_lists[subset_name] = subset_list_name
            with open(osp.join(save_dir, subset_list_name), 'w') as f:
                f.writelines('%s\n' % s for s in image_paths.values())

        with open(osp.join(save_dir, 'obj.data'), 'w') as f:
            f.write('classes = %s\n' % len(label_ids))

            for subset_name, subset_list_name in subset_lists.items():
                f.write('%s = %s\n' % (subset_name,
                    osp.join('data', subset_list_name)))

            f.write('names = %s\n' % osp.join('data', 'obj.names'))
            f.write('backup = backup/\n')