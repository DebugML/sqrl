# Modified from https://github.com/facebookresearch/detectron2/blob/master/detectron2/data/datasets/cityscapes.py # noqa
# and https://github.com/mcordts/cityscapesScripts/blob/master/cityscapesscripts/evaluation/evalInstanceLevelSemanticLabeling.py # noqa

import glob
import os
import os.path as osp
import tempfile

import mmcv
import numpy as np
import pycocotools.mask as maskUtils

from mmdet.utils import print_log
from .coco import CocoDataset
from .registry import DATASETS
from .pipelines import LoadAnnotations
from .lib_obj_detection import BoundingBoxes, BoundingBox
from .lib_obj_detection import BBType, MethodAveragePrecision, CoordinatesType, BBFormat, Evaluator


from cityscapesscripts.evaluation.evalPanopticSemanticLabeling import pq_compute_single_core, pq_compute_multi_core, \
                                                                      average_pq

import torch
import json
import shutil

PALETTE = [[128, 64, 128], [244, 35, 232], [70, 70, 70], [102, 102, 156],
           [190, 153, 153], [153, 153, 153], [250, 170, 30], [220, 220, 0],
           [107, 142, 35], [152, 251, 152], [70, 130, 180], [220, 20, 60],
           [255, 0, 0], [0, 0, 142], [0, 0, 70], [0, 60, 100],
           [0, 80, 100], [0, 0, 230], [119, 11, 32]]






@DATASETS.register_module
class CityscapesDataset(CocoDataset):

    CLASSES = ('person', 'rider', 'car', 'truck', 'bus', 'train', 'motorcycle',
               'bicycle')

    STUFF_CLASSES = ('road', 'sidewalk', 'building', 'wall', 'fence', 'pole',
               'traffic light', 'traffic sign', 'vegetation', 'terrain', 'sky')


    def _filter_imgs(self, min_size=32):
        """Filter images too small or without ground truths."""
        valid_inds = []
        ids_with_ann = set(_['image_id'] for _ in self.coco.anns.values())
        for i, img_info in enumerate(self.img_infos):
            img_id = img_info['id']
            ann_ids = self.coco.getAnnIds(imgIds=[img_id])
            ann_info = self.coco.loadAnns(ann_ids)
            all_iscrowd = all([_['iscrowd'] for _ in ann_info])
            if self.filter_empty_gt and (self.img_ids[i] not in ids_with_ann
                                         or all_iscrowd):
                continue
            if min(img_info['width'], img_info['height']) >= min_size:
                valid_inds.append(i)
        return valid_inds

    def _parse_ann_info(self, img_info, ann_info):
        """Parse bbox and mask annotation.

        Args:
            img_info (dict): Image info of an image.
            ann_info (list[dict]): Annotation info of an image.

        Returns:
            dict: A dict containing the following keys: bboxes, bboxes_ignore,
                labels, masks, seg_map.
                "masks" are already decoded into binary masks.
        """
        gt_bboxes = []
        gt_labels = []
        gt_bboxes_ignore = []
        gt_masks_ann = []

        for i, ann in enumerate(ann_info):
            if ann.get('ignore', False):
                continue
            x1, y1, w, h = ann['bbox']
            if ann['area'] <= 0 or w < 1 or h < 1:
                continue
            bbox = [x1, y1, x1 + w - 1, y1 + h - 1]
            if ann.get('iscrowd', False):
                gt_bboxes_ignore.append(bbox)
            else:
                gt_bboxes.append(bbox)
                gt_labels.append(self.cat2label[ann['category_id']])
                gt_masks_ann.append(ann['segmentation'])

        if gt_bboxes:
            gt_bboxes = np.array(gt_bboxes, dtype=np.float32)
            gt_labels = np.array(gt_labels, dtype=np.int64)
        else:
            gt_bboxes = np.zeros((0, 4), dtype=np.float32)
            gt_labels = np.array([], dtype=np.int64)

        if gt_bboxes_ignore:
            gt_bboxes_ignore = np.array(gt_bboxes_ignore, dtype=np.float32)
        else:
            gt_bboxes_ignore = np.zeros((0, 4), dtype=np.float32)

        seg_map = img_info['filename'] 

        ann = dict(
            bboxes=gt_bboxes,
            labels=gt_labels,
            bboxes_ignore=gt_bboxes_ignore,
            masks=gt_masks_ann,
            seg_map=seg_map)

        return ann

    def results2txt(self, results, outfile_prefix):
        """Dump the detection results to a txt file.

        Args:
            results (list[list | tuple | ndarray]): Testing results of the
                dataset.
            outfile_prefix (str): The filename prefix of the json files.
                If the prefix is "somepath/xxx",
                the txt files will be named "somepath/xxx.txt".

        Returns:
            list[str: str]: result txt files which contains corresponding
            instance segmentation images.
        """
        try:
            import cityscapesscripts.helpers.labels as CSLabels
        except ImportError:
            raise ImportError('Please run "pip install citscapesscripts" to '
                              'install cityscapesscripts first.')
        result_files = []
        os.makedirs(outfile_prefix, exist_ok=True)
        prog_bar = mmcv.ProgressBar(len(self))
        for idx in range(len(self)):
            result = results[idx]
            filename = self.img_infos[idx]['filename']
            basename = osp.splitext(osp.basename(filename))[0]
            pred_txt = osp.join(outfile_prefix, basename + '_pred.txt')

            bbox_result, segm_result = result[:2]
            bboxes = np.vstack(bbox_result)
            segms = mmcv.concat_list(segm_result)
            labels = [
                np.full(bbox.shape[0], i, dtype=np.int32)
                for i, bbox in enumerate(bbox_result)
            ]
            labels = np.concatenate(labels)

            assert len(bboxes) == len(segms) == len(labels)
            num_instances = len(bboxes)
            prog_bar.update()
            with open(pred_txt, 'w') as fout:
                for i in range(num_instances):
                    pred_class = labels[i]
                    classes = self.CLASSES[pred_class]
                    class_id = CSLabels.name2label[classes].id
                    score = bboxes[i, -1]
                    mask = maskUtils.decode(segms[i]).astype(np.uint8)
                    png_filename = osp.join(
                        outfile_prefix,
                        basename + '_{}_{}.png'.format(i, classes))
                    mmcv.imwrite(mask, png_filename)
                    fout.write('{} {} {}\n'.format(
                        osp.basename(png_filename), class_id, score))
            result_files.append(pred_txt)

        return result_files

    def format_results(self, results, txtfile_prefix=None):
        """Format the results to txt (standard format for Cityscapes evaluation).

        Args:
            results (list): Testing results of the dataset.
            txtfile_prefix (str | None): The prefix of txt files. It includes
                the file path and the prefix of filename, e.g., "a/b/prefix".
                If not specified, a temp file will be created. Default: None.

        Returns:
            tuple: (result_files, tmp_dir), result_files is a dict containing
                the json filepaths, tmp_dir is the temporal directory created
                for saving txt/png files when txtfile_prefix is not specified.
        """
        assert isinstance(results, list), 'results must be a list'
        assert len(results) == len(self), (
            'The length of results is not equal to the dataset len: {} != {}'.
            format(len(results), len(self)))

        assert isinstance(results, list), 'results must be a list'
        assert len(results) == len(self), (
            'The length of results is not equal to the dataset len: {} != {}'.
            format(len(results), len(self)))

        if txtfile_prefix is None:
            tmp_dir = tempfile.TemporaryDirectory()
            txtfile_prefix = osp.join(tmp_dir.name, 'results')
        else:
            tmp_dir = None
        result_files = self.results2txt(results, txtfile_prefix)

        return result_files, tmp_dir

    def evaluate(self,
                 results,
                 metric='bbox',
                 logger=None,
                 outfile_prefix=None,
                 classwise=False,
                 proposal_nums=(100, 300, 1000),
                 iou_thrs=np.arange(0.5, 0.96, 0.05), out_dir=None):
        """Evaluation in Cityscapes protocol.

        Args:
            results (list): Testing results of the dataset.
            metric (str | list[str]): Metrics to be evaluated.
            logger (logging.Logger | str | None): Logger used for printing
                related information during evaluation. Default: None.
            outfile_prefix (str | None):
            classwise (bool): Whether to evaluating the AP for each class.
            proposal_nums (Sequence[int]): Proposal number used for evaluating
                recalls, such as recall@100, recall@1000.
                Default: (100, 300, 1000).
            iou_thrs (Sequence[float]): IoU threshold used for evaluating
                recalls. If set to a list, the average recall of all IoUs will
                also be computed. Default: 0.5.

        Returns:
            dict[str: float]
        """
        eval_results = dict()
        metrics = metric.copy() if isinstance(metric, list) else [metric]

        if 'panoptic' in metrics:
            eval_results.update(
                self._evaluate_panoptic(results, outfile_prefix, logger, out_dir=out_dir))
  
        if 'cityscapes' in metrics:
            eval_results.update(
                self._evaluate_cityscapes(results, outfile_prefix, logger))
            metrics.remove('cityscapes')

        if "bbox" in metrics:
            self.output_pred_to_files(results[0], results[1], out_dir)
            self.compute_bbox_statistics(out_dir)

        # left metrics are all coco metric
        if len(metrics) > 0 and 'panoptic' not in metrics and 'bbox' not in metrics:
            # create CocoDataset with CityscapesDataset annotation
            self_coco = CocoDataset(self.ann_file, self.pipeline.transforms,
                                    self.data_root, self.img_prefix,
                                    self.seg_prefix, self.proposal_file,
                                    self.test_mode, self.filter_empty_gt)
            eval_results.update(
                self_coco.evaluate(results, metrics, logger, outfile_prefix,
                                   classwise, proposal_nums, iou_thrs))

        return eval_results

    def _evaluate_cityscapes(self, results, txtfile_prefix, logger):
        try:
            import cityscapesscripts.evaluation.evalInstanceLevelSemanticLabeling as CSEval  # noqa
        except ImportError:
            raise ImportError('Please run "pip install citscapesscripts" to '
                              'install cityscapesscripts first.')
        msg = 'Evaluating in Cityscapes style'
        if logger is None:
            msg = '\n' + msg
        print_log(msg, logger=logger)

        result_files, tmp_dir = self.format_results(results, txtfile_prefix)

        if tmp_dir is None:
            result_dir = osp.join(txtfile_prefix, 'results')
        else:
            result_dir = osp.join(tmp_dir.name, 'results')

        eval_results = {}
        print_log(
            'Evaluating results under {} ...'.format(result_dir),
            logger=logger)

        # set global states in cityscapes evaluation API
        CSEval.args.cityscapesPath = os.path.join(self.img_prefix, '../..')
        CSEval.args.predictionPath = os.path.abspath(result_dir)
        CSEval.args.predictionWalk = None
        CSEval.args.JSONOutput = False
        CSEval.args.colorized = False
        CSEval.args.gtInstancesFile = os.path.join(result_dir,
                                                   'gtInstances.json')
        CSEval.args.groundTruthSearch = os.path.join(
            self.img_prefix.replace('leftImg8bit', 'gtFine'),
            '*/*_gtFine_instanceIds.png')

        groundTruthImgList = glob.glob(CSEval.args.groundTruthSearch)
        assert len(groundTruthImgList), \
            'Cannot find ground truth images in {}.'.format(
                CSEval.args.groundTruthSearch)
        predictionImgList = []
        for gt in groundTruthImgList:
            predictionImgList.append(CSEval.getPrediction(gt, CSEval.args))
        CSEval_results = CSEval.evaluateImgLists(predictionImgList,
                                                 groundTruthImgList,
                                                 CSEval.args)['averages']

        eval_results['mAP'] = CSEval_results['allAp']
        eval_results['AP@50'] = CSEval_results['allAp50%']
        if tmp_dir is not None:
            tmp_dir.cleanup()
        return eval_results

    def _evaluate_panoptic(self, results, txtfile_prefix, logger, out_dir=None):
        with open(self.panoptic_gt + '.json', 'r') as f:
            gt_json = json.load(f)

        categories = {el['id']: el for el in gt_json['categories']}

        gt_folder = self.panoptic_gt
        if out_dir is None:
            pred_folder = 'tmpDir/tmp'
            pred_json = 'tmpDir/tmp_json'
        else:
            pred_folder = os.path.join(out_dir, 'tmpDir/tmp')
            pred_json = os.path.join(out_dir, 'tmpDir/tmp_json')
        
        assert os.path.isdir(gt_folder)
        assert os.path.isdir(pred_folder)
        
        pred_annotations = {}  
        for pred_ann in os.listdir(pred_json):
            with open(os.path.join(pred_json, pred_ann), 'r') as f:
                tmp_json = json.load(f)

            pred_annotations.update({el['image_id']: el for el in tmp_json['annotations']})

        matched_annotations_list = []
        for gt_ann in gt_json['annotations']:
            image_id = gt_ann['image_id']
            if image_id not in pred_annotations:
                raise Exception('no prediction for the image with id: {}'.format(image_id))
            matched_annotations_list.append((gt_ann, pred_annotations[image_id]))


        # pq_stat = pq_compute_multi_core(matched_annotations_list, 
                #   gt_folder, pred_folder, categories)
        
        pq_stat = pq_compute_single_core(0, matched_annotations_list, gt_folder, pred_folder, categories)

        results = average_pq(pq_stat, categories)

        metrics = ["All", "Things", "Stuff"]
        msg = "{:14s}| {:>5s}  {:>5s}  {:>5s}".format("Category", "PQ", "SQ", "RQ")
        print_log(msg, logger=logger)

        labels = sorted(results['per_class'].keys())
        for label in labels:
            msg = "{:14s}| {:5.1f}  {:5.1f}  {:5.1f}".format(
                categories[label]['name'],
                100 * results['per_class'][label]['pq'],
                100 * results['per_class'][label]['sq'],
                100 * results['per_class'][label]['rq']
            )
            print_log(msg, logger=logger)

        msg = "-" * 41
        print_log(msg, logger=logger)

        msg = "{:14s}| {:>5s}  {:>5s}  {:>5s} {:>5s}".format("", "PQ", "SQ", "RQ", "N")
        print_log(msg, logger=logger)

        eval_results = {} 
        for name in metrics:
            msg = "{:14s}| {:5.1f}  {:5.1f}  {:5.1f} {:5d}".format(
                name,
                100 * results[name]['pq'],
                100 * results[name]['sq'],
                100 * results[name]['rq'],
                results[name]['n']
            )
            print_log(msg, logger=logger)
            eval_results[name+'_pq'] = 100 * results[name]['pq']
            eval_results[name+'_sq'] = 100 * results[name]['sq']
            eval_results[name+'_rq'] = 100 * results[name]['rq']

        # shutil.rmtree('tmpDir')
        return eval_results

    def getBoundingBoxes(self, directory,
                     isGT,
                     bbFormat,
                     coordType,
                     allBoundingBoxes=None,
                     allClasses=None,
                     imgSize=(0, 0)):
        """Read txt files containing bounding boxes (ground truth and detections)."""

        
        
        if allBoundingBoxes is None:
            allBoundingBoxes = BoundingBoxes()
        if allClasses is None:
            allClasses = []
        # Read ground truths
        os.chdir(directory)
        files = glob.glob("*.txt")
        files.sort()
        # Read GT detections from txt file
        # Each line of the files in the groundtruths folder represents a ground truth bounding box
        # (bounding boxes that a detector should detect)
        # Each value of each line is  "class_id, x, y, width, height" respectively
        # Class_id represents the class of the bounding box
        # x, y represents the most top-left coordinates of the bounding box
        # x2, y2 represents the most bottom-right coordinates of the bounding box
        for f in files:
            nameOfImage = f.replace(".txt", "")
            fh1 = open(f, "r")
            for line in fh1:
                line = line.replace("\n", "")
                if line.replace(' ', '') == '':
                    continue
                splitLine = line.split(" ")
                if isGT:
                    # idClass = int(splitLine[0]) #class
                    idClass = (splitLine[0])  # class
                    x = float(splitLine[1])
                    y = float(splitLine[2])
                    w = float(splitLine[3])
                    h = float(splitLine[4])
                    bb = BoundingBox(nameOfImage,
                                    idClass,
                                    x,
                                    y,
                                    w,
                                    h,
                                    coordType,
                                    imgSize,
                                    BBType.GroundTruth,
                                    format=bbFormat)
                else:
                    # idClass = int(splitLine[0]) #class
                    idClass = (splitLine[0])  # class
                    confidence = float(splitLine[1])
                    x = float(splitLine[2])
                    y = float(splitLine[3])
                    w = float(splitLine[4])
                    h = float(splitLine[5])
                    bb = BoundingBox(nameOfImage,
                                    idClass,
                                    x,
                                    y,
                                    w,
                                    h,
                                    coordType,
                                    imgSize,
                                    BBType.Detected,
                                    confidence,
                                    format=bbFormat)
                allBoundingBoxes.addBoundingBox(bb)
                if idClass not in allClasses:
                    allClasses.append(idClass)
            fh1.close()

        return allBoundingBoxes, allClasses

    def compute_bbox_statistics(self, savePath, showPlot=False, iouThreshold=0.5, imgSize=None):
        gtFolder = os.path.join(savePath, "groundtruths/")
        detFolder = os.path.join(savePath, "detections/")
        root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

        gtCoordType = CoordinatesType.Absolute
        detCoordType=CoordinatesType.Absolute
        gtFormat=BBFormat.XYX2Y2
        detFormat = BBFormat.XYX2Y2

        allBoundingBoxes, allClasses = self.getBoundingBoxes(gtFolder,
                                                    True,
                                                    gtFormat,
                                                    gtCoordType,
                                                    imgSize=imgSize)
        # Get detected boxes
        allBoundingBoxes, allClasses = self.getBoundingBoxes(detFolder,
                                                        False,
                                                        detFormat,
                                                        detCoordType,
                                                        allBoundingBoxes,
                                                        allClasses,
                                                        imgSize=imgSize)
        allClasses.sort()

        

        evaluator = Evaluator()
        acc_AP = 0
        validClasses = 0

        # Plot Precision x Recall curve
        detections = evaluator.PlotPrecisionRecallCurve(
            allBoundingBoxes,  # Object containing all bounding boxes (ground truths and detections)
            IOUThreshold=iouThreshold,  # IOU threshold
            method=MethodAveragePrecision.EveryPointInterpolation,
            showAP=True,  # Show Average Precision in the title of the plot
            showInterpolatedPrecision=False,  # Don't plot the interpolated precision curve
            savePath=savePath,
            showGraphic=showPlot)

        f = open(os.path.join(savePath, 'results.txt'), 'w')
        f.write('Object Detection Metrics\n')
        f.write('https://github.com/rafaelpadilla/Object-Detection-Metrics\n\n\n')
        f.write('Average Precision (AP), Precision and Recall per class:')

        # each detection is a class
        for metricsPerClass in detections:

            # Get metric values per each class
            cl = metricsPerClass['class']
            ap = metricsPerClass['AP']
            precision = metricsPerClass['precision']
            recall = metricsPerClass['recall']
            totalPositives = metricsPerClass['total positives']
            total_TP = metricsPerClass['total TP']
            total_FP = metricsPerClass['total FP']

            if totalPositives > 0:
                validClasses = validClasses + 1
                acc_AP = acc_AP + ap
                prec = ['%.2f' % p for p in precision]
                rec = ['%.2f' % r for r in recall]
                ap_str = "{0:.2f}%".format(ap * 100)
                # ap_str = "{0:.4f}%".format(ap * 100)
                print('AP: %s (%s)' % (ap_str, cl))
                f.write('\n\nClass: %s' % cl)
                f.write('\nAP: %s' % ap_str)
                f.write('\nPrecision: %s' % prec)
                f.write('\nRecall: %s' % rec)

        mAP = acc_AP / validClasses
        mAP_str = "{0:.2f}%".format(mAP * 100)
        print('mAP: %s' % mAP_str)
        f.write('\n\n\nmAP: %s' % mAP_str)



    def output_pred_to_files(self, results, img_metas, save_dir):

        output_dir = osp.join(save_dir, "detections/")

        os.makedirs(output_dir, exist_ok=True)

        for idx in range(len(results)):
            curr_results = results[idx]
            img_meta = img_metas[idx]
            
            # for k in range(len(curr_results)):
            k = 0
            res = curr_results[k]
            file_name = img_meta[k][0]['filename']
            scale_factor = img_meta[k][0]['scale_factor']
            file_name_no_suffix = file_name.split(".")[k]
            if "/" in file_name_no_suffix:
                file_name_no_suffix = file_name_no_suffix.split("/")[-1]

            output_file_name = osp.join(output_dir, file_name_no_suffix + ".txt")

            with open(output_file_name, "w") as f:
                for class_idx in range(len(res)):
                    if len(res[class_idx]) <= 0:
                        continue
                    bbox_ls = np.copy(res[class_idx][:,0:4]).tolist()
                    confidence_ls = np.copy(res[class_idx][:,-1]).tolist()
                    for bid in range(len(bbox_ls)):
                        curr_bbox = bbox_ls[bid]
                        curr_bbox = (np.array(curr_bbox)).tolist()
                        line = str(class_idx + 1) + " " + str(confidence_ls[bid]) + " " + str(curr_bbox[0]) + " " + str(curr_bbox[1]) + " " + str(curr_bbox[2]) + " " + str(curr_bbox[3])
                        f.write(line + "\n")

                f.close()
