
# Copyright (C) 2019 Intel Corporation
#
# SPDX-License-Identifier: MIT

from collections import OrderedDict
import numpy as np
import os.path as osp

from pycocotools.coco import COCO
import pycocotools.mask as mask_utils

from datumaro.components.extractor import (Extractor, DatasetItem,
    AnnotationType,
    LabelObject, MaskObject, PointsObject, PolygonObject,
    BboxObject, CaptionObject,
    LabelCategories, PointsCategories
)
from datumaro.components.formats.ms_coco import CocoTask, CocoPath
from datumaro.util.image import lazy_image


class RleMask(MaskObject):
    # pylint: disable=redefined-builtin
    def __init__(self, rle=None, label=None,
            id=None, attributes=None, group=None):
        lazy_decode = lambda: mask_utils.decode(rle).astype(np.bool)
        super().__init__(image=lazy_decode, label=label,
            id=id, attributes=attributes, group=group)

        self._rle = rle
    # pylint: enable=redefined-builtin

    def area(self):
        return mask_utils.area(self._rle)

    def bbox(self):
        return mask_utils.toBbox(self._rle)

    def __eq__(self, other):
        if not isinstance(other, __class__):
            return super().__eq__(other)
        return self._rle == other._rle


class CocoExtractor(Extractor):
    class Subset(Extractor):
        def __init__(self, name, parent):
            super().__init__()
            self._name = name
            self._parent = parent
            self.loaders = {}
            self.items = OrderedDict()

        def __iter__(self):
            for img_id in self.items:
                yield self._parent._get(img_id, self._name)

        def __len__(self):
            return len(self.items)

        def categories(self):
            return self._parent.categories()

    def __init__(self, path, task, merge_instance_polygons=False):
        super().__init__()

        rootpath = path.rsplit(CocoPath.ANNOTATIONS_DIR, maxsplit=1)[0]
        self._path = rootpath
        self._task = task
        self._subsets = {}

        subset_name = osp.splitext(osp.basename(path))[0] \
            .rsplit('_', maxsplit=1)[1]
        subset = CocoExtractor.Subset(subset_name, self)
        loader = self._make_subset_loader(path)
        subset.loaders[task] = loader
        for img_id in loader.getImgIds():
            subset.items[img_id] = None
        self._subsets[subset_name] = subset

        self._load_categories()

        self._merge_instance_polygons = merge_instance_polygons

    @staticmethod
    def _make_subset_loader(path):
        # COCO API has an 'unclosed file' warning
        coco_api = COCO()
        with open(path, 'r') as f:
            import json
            dataset = json.load(f)

        coco_api.dataset = dataset
        coco_api.createIndex()
        return coco_api

    def _load_categories(self):
        loaders = {}

        for subset in self._subsets.values():
            loaders.update(subset.loaders)

        self._categories = {}

        label_loader = loaders.get(CocoTask.labels)
        instances_loader = loaders.get(CocoTask.instances)
        person_kp_loader = loaders.get(CocoTask.person_keypoints)

        if label_loader is None and instances_loader is not None:
            label_loader = instances_loader
        if label_loader is None and person_kp_loader is not None:
            label_loader = person_kp_loader
        if label_loader is not None:
            label_categories, label_map = \
                self._load_label_categories(label_loader)
            self._categories[AnnotationType.label] = label_categories
            self._label_map = label_map

        if person_kp_loader is not None:
            person_kp_categories = \
                self._load_person_kp_categories(person_kp_loader)
            self._categories[AnnotationType.points] = person_kp_categories

    # pylint: disable=no-self-use
    def _load_label_categories(self, loader):
        catIds = loader.getCatIds()
        cats = loader.loadCats(catIds)

        categories = LabelCategories()
        label_map = {}
        for idx, cat in enumerate(cats):
            label_map[cat['id']] = idx
            categories.add(name=cat['name'], parent=cat['supercategory'])

        return categories, label_map
    # pylint: enable=no-self-use

    def _load_person_kp_categories(self, loader):
        catIds = loader.getCatIds()
        cats = loader.loadCats(catIds)

        categories = PointsCategories()
        for cat in cats:
            label_id, _ = self._categories[AnnotationType.label].find(cat['name'])
            categories.add(label_id=label_id,
                labels=cat['keypoints'], adjacent=cat['skeleton'])

        return categories

    def categories(self):
        return self._categories

    def __iter__(self):
        for subset in self._subsets.values():
            for item in subset:
                yield item

    def __len__(self):
        length = 0
        for subset in self._subsets.values():
            length += len(subset)
        return length

    def subsets(self):
        return list(self._subsets)

    def get_subset(self, name):
        return self._subsets[name]

    def _get(self, img_id, subset):
        file_name = None
        image_info = None
        image = None
        annotations = []
        for ann_type, loader in self._subsets[subset].loaders.items():
            if image is None:
                image_info = loader.loadImgs(img_id)[0]
                file_name = image_info['file_name']
                if file_name != '':
                    image_dir = osp.join(self._path, CocoPath.IMAGES_DIR)
                    search_paths = [
                        osp.join(image_dir, file_name),
                        osp.join(image_dir, subset, file_name),
                    ]
                    for image_path in search_paths:
                        if osp.exists(image_path):
                            image = lazy_image(image_path)
                            break

            annIds = loader.getAnnIds(imgIds=img_id)
            anns = loader.loadAnns(annIds)

            for ann in anns:
                self._parse_annotation(ann, ann_type, annotations, image_info)
        return DatasetItem(id=img_id, subset=subset,
            image=image, annotations=annotations)

    def _parse_label(self, ann):
        cat_id = ann.get('category_id')
        if cat_id in [0, None]:
            return None
        return self._label_map[cat_id]

    def _parse_annotation(self, ann, ann_type, parsed_annotations,
            image_info=None):
        ann_id = ann.get('id')
        attributes = {}
        if 'score' in ann:
            attributes['score'] = ann['score']

        if ann_type is CocoTask.instances:
            x, y, w, h = ann['bbox']
            label_id = self._parse_label(ann)
            group = None

            is_crowd = bool(ann['iscrowd'])
            attributes['is_crowd'] = is_crowd

            segmentation = ann.get('segmentation')
            if segmentation is not None:
                group = ann_id
                rle = None

                if isinstance(segmentation, list):
                    # polygon - a single object can consist of multiple parts
                    for polygon_points in segmentation:
                        parsed_annotations.append(PolygonObject(
                            points=polygon_points, label=label_id,
                            id=ann_id, group=group, attributes=attributes
                        ))

                    if self._merge_instance_polygons:
                        # merge all parts into a single mask RLE
                        img_h = image_info['height']
                        img_w = image_info['width']
                        rles = mask_utils.frPyObjects(segmentation, img_h, img_w)
                        rle = mask_utils.merge(rles)
                elif isinstance(segmentation['counts'], list):
                    # uncompressed RLE
                    img_h, img_w = segmentation['size']
                    rle = mask_utils.frPyObjects([segmentation], img_h, img_w)[0]
                else:
                    # compressed RLE
                    rle = segmentation

                if rle is not None:
                    parsed_annotations.append(RleMask(rle=rle, label=label_id,
                        id=ann_id, group=group, attributes=attributes
                    ))

            parsed_annotations.append(
                BboxObject(x, y, w, h, label=label_id,
                    id=ann_id, attributes=attributes, group=group)
            )
        elif ann_type is CocoTask.labels:
            label_id = self._parse_label(ann)
            parsed_annotations.append(
                LabelObject(label=label_id,
                    id=ann_id, attributes=attributes)
            )
        elif ann_type is CocoTask.person_keypoints:
            keypoints = ann['keypoints']
            points = [p for i, p in enumerate(keypoints) if i % 3 != 2]
            visibility = keypoints[2::3]
            bbox = ann.get('bbox')
            label_id = self._parse_label(ann)
            group = None
            if bbox is not None:
                group = ann_id
            parsed_annotations.append(
                PointsObject(points, visibility, label=label_id,
                    id=ann_id, attributes=attributes, group=group)
            )
            if bbox is not None:
                parsed_annotations.append(
                    BboxObject(*bbox, label=label_id, group=group)
                )
        elif ann_type is CocoTask.captions:
            caption = ann['caption']
            parsed_annotations.append(
                CaptionObject(caption,
                    id=ann_id, attributes=attributes)
            )
        else:
            raise NotImplementedError()

        return parsed_annotations

class CocoImageInfoExtractor(CocoExtractor):
    def __init__(self, path, **kwargs):
        super().__init__(path, task=CocoTask.image_info, **kwargs)

class CocoCaptionsExtractor(CocoExtractor):
    def __init__(self, path, **kwargs):
        super().__init__(path, task=CocoTask.captions, **kwargs)

class CocoInstancesExtractor(CocoExtractor):
    def __init__(self, path, **kwargs):
        super().__init__(path, task=CocoTask.instances, **kwargs)

class CocoPersonKeypointsExtractor(CocoExtractor):
    def __init__(self, path, **kwargs):
        super().__init__(path, task=CocoTask.person_keypoints,
            **kwargs)

class CocoLabelsExtractor(CocoExtractor):
    def __init__(self, path, **kwargs):
        super().__init__(path, task=CocoTask.labels, **kwargs)