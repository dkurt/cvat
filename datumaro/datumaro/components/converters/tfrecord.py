
# Copyright (C) 2019 Intel Corporation
#
# SPDX-License-Identifier: MIT

import codecs
from collections import OrderedDict
from io import BytesIO
import numpy as np
import os
import os.path as osp
from PIL import Image
import string

from datumaro.components.extractor import AnnotationType, DEFAULT_SUBSET_NAME
from datumaro.components.formats.tfrecord import DetectionApiPath
from datumaro.util.tf_util import import_tf as _import_tf


# we need it to filter out non-ASCII characters, otherwise training will crash
_printable = set(string.printable)
def _make_printable(s):
    return ''.join(filter(lambda x: x in _printable, s))

def _make_tf_example(item, get_label_id, get_label, save_images=False):
    tf = _import_tf()

    def int64_feature(value):
        return tf.train.Feature(int64_list=tf.train.Int64List(value=[value]))

    def int64_list_feature(value):
        return tf.train.Feature(int64_list=tf.train.Int64List(value=value))

    def bytes_feature(value):
        return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))

    def bytes_list_feature(value):
        return tf.train.Feature(bytes_list=tf.train.BytesList(value=value))

    def float_list_feature(value):
        return tf.train.Feature(float_list=tf.train.FloatList(value=value))


    features = {
        'image/source_id': bytes_feature(str(item.id).encode('utf-8')),
        'image/filename': bytes_feature(
            ('%s%s' % (item.id, DetectionApiPath.IMAGE_EXT)).encode('utf-8')),
    }

    if item.has_image:
        height, width, _ = item.image.shape
    else:
        height = 0
        width = 0

    features.update({
        'image/height': int64_feature(height),
        'image/width': int64_feature(width),
    })

    if save_images and item.has_image:
        fmt = DetectionApiPath.IMAGE_FORMAT
        with BytesIO() as buffer:
            image = Image.fromarray(item.image.astype(np.uint8))
            image.save(buffer, format=fmt)

            features.update({
                'image/encoded': bytes_feature(buffer.getvalue()),
                'image/format': bytes_feature(fmt.encode('utf-8')),
            })

    xmins = [] # List of normalized left x coordinates in bounding box (1 per box)
    xmaxs = [] # List of normalized right x coordinates in bounding box (1 per box)
    ymins = [] # List of normalized top y coordinates in bounding box (1 per box)
    ymaxs = [] # List of normalized bottom y coordinates in bounding box (1 per box)
    classes_text = [] # List of string class name of bounding box (1 per box)
    classes = [] # List of integer class id of bounding box (1 per box)

    boxes = [ann for ann in item.annotations if ann.type is AnnotationType.bbox]
    for box in boxes:
        box_label = _make_printable(get_label(box.label))

        xmins.append(box.points[0] / width)
        xmaxs.append(box.points[2] / width)
        ymins.append(box.points[1] / height)
        ymaxs.append(box.points[3] / height)
        classes_text.append(box_label.encode('utf-8'))
        classes.append(get_label_id(box.label))

    if boxes:
        features.update({
            'image/object/bbox/xmin': float_list_feature(xmins),
            'image/object/bbox/xmax': float_list_feature(xmaxs),
            'image/object/bbox/ymin': float_list_feature(ymins),
            'image/object/bbox/ymax': float_list_feature(ymaxs),
            'image/object/class/text': bytes_list_feature(classes_text),
            'image/object/class/label': int64_list_feature(classes),
        })

    tf_example = tf.train.Example(
        features=tf.train.Features(feature=features))

    return tf_example

class DetectionApiConverter:
    def __init__(self, save_images=True):
        self.save_images = save_images

    def __call__(self, extractor, save_dir):
        tf = _import_tf()

        os.makedirs(save_dir, exist_ok=True)

        subsets = extractor.subsets()
        if len(subsets) == 0:
            subsets = [ None ]

        for subset_name in subsets:
            if subset_name:
                subset = extractor.get_subset(subset_name)
            else:
                subset_name = DEFAULT_SUBSET_NAME
                subset = extractor

            label_categories = subset.categories()[AnnotationType.label]
            get_label = lambda label_id: label_categories.items[label_id].name \
                if label_id is not None else ''
            label_ids = OrderedDict((label.name, 1 + idx)
                for idx, label in enumerate(label_categories.items))
            map_label_id = lambda label_id: label_ids.get(get_label(label_id), 0)

            labelmap_path = osp.join(save_dir, DetectionApiPath.LABELMAP_FILE)
            with codecs.open(labelmap_path, 'w', encoding='utf8') as f:
                for label, idx in label_ids.items():
                    f.write(
                        'item {\n' +
                        ('\tid: %s\n' % (idx)) +
                        ("\tname: '%s'\n" % (label)) +
                        '}\n\n'
                    )

            anno_path = osp.join(save_dir, '%s.tfrecord' % (subset_name))
            with tf.io.TFRecordWriter(anno_path) as writer:
                for item in subset:
                    tf_example = _make_tf_example(
                        item,
                        get_label=get_label,
                        get_label_id=map_label_id,
                        save_images=self.save_images,
                    )
                    writer.write(tf_example.SerializeToString())