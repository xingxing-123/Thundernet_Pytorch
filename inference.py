from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import _init_paths
import os
import sys
import numpy as np
import argparse
import pprint
import pdb
import time
import cv2
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
from torchvision import transforms as trans
# import torch._utils as utils
import pickle
from roi_data_layer.roidb import combined_roidb

from model.utils.config import cfg, cfg_from_file, cfg_from_list, get_output_dir
from model.rpn.bbox_transform import clip_boxes
from torchvision.ops import nms
from model.rpn.bbox_transform import bbox_transform_inv
from model.utils.net_utils import save_net, load_net, vis_detections
from model.faster_rcnn.Snet import snet
from PIL import Image
# import pdb
from utils import color_list
from roi_data_layer.utils import BaseTransform
from roi_data_layer.roibatchLoader import Detection
from external.nms import soft_nms
try:
    xrange  # Python 2
except NameError:
    xrange = range  # Python 3


def parse_args():
    """
    Parse input arguments
    """
    parser = argparse.ArgumentParser(description='Train a Fast R-CNN network')
    parser.add_argument('--dataset', dest='dataset', help='training dataset', default='pascal_voc_0712', type=str)
    parser.add_argument('--cfg', dest='cfg_file', help='optional config file', default='cfgs/snet.yml', type=str)
    parser.add_argument('--net', dest='net', help='snet_49, snet_146', default='snet_146', type=str)
    parser.add_argument('--set', dest='set_cfgs', help='set config keys', default=None, nargs=argparse.REMAINDER)

    parser.add_argument('--cag', dest='class_agnostic', help='whether perform class_agnostic bbox regression', action='store_true')
    parser.set_defaults(cag=True)

    parser.add_argument('--save_dir', dest='save_dir', help='directory to save models', default="weights", type=str)
    parser.add_argument('--cuda', dest='cuda', help='whether use CUDA', action='store_true')
    parser.add_argument('--ls', dest='large_scale', help='whether use large imag scale', action='store_true')
    parser.set_defaults(cuda=True)
    args = parser.parse_args()
    return args


def eval_result(args, output_dir):
    if torch.cuda.is_available() and not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")

    args.batch_size = 1
    imdb, roidb, ratio_list, ratio_index = combined_roidb(args.imdbval_name, False)

    imdb.competition_mode(on=True)
    load_name = os.path.join(output_dir, 'thundernet146_voc_map67.pth')

    layer = int(args.net.split("_")[1])
    _RCNN = snet(imdb.classes, layer, pretrained_path=None, class_agnostic=args.class_agnostic)

    _RCNN.create_architecture()

    print("load checkpoint %s" % (load_name))
    if args.cuda:
        checkpoint = torch.load(load_name)
    else:
        checkpoint = torch.load(load_name, map_location=lambda storage, loc: storage)  # Load all tensors onto the CPU
    _RCNN.load_state_dict(checkpoint['model'])

    im_data = torch.FloatTensor(1)
    im_info = torch.FloatTensor(1)
    num_boxes = torch.LongTensor(1)
    gt_boxes = torch.FloatTensor(1)
    # hm = torch.FloatTensor(1)
    # reg_mask = torch.LongTensor(1)
    # wh = torch.FloatTensor(1)
    # offset = torch.FloatTensor(1)
    # ind = torch.LongTensor(1)
    # ship to cuda
    if args.cuda:
        im_data = im_data.cuda()
        im_info = im_info.cuda()
        num_boxes = num_boxes.cuda()
        gt_boxes = gt_boxes.cuda()
        # hm = hm.cuda()
        # reg_mask = reg_mask.cuda()
        # wh = wh.cuda()
        # offset = offset.cuda()
        # ind = ind.cuda()

    # make variable
    with torch.no_grad():
        im_data = Variable(im_data)
        im_info = Variable(im_info)
        num_boxes = Variable(num_boxes)
        gt_boxes = Variable(gt_boxes)
        # hm = Variable(hm)
        # reg_mask = Variable(reg_mask)
        # wh = Variable(wh)
        # offset = Variable(offset)
        # ind = Variable(ind)

    if args.cuda:
        cfg.CUDA = True

    if args.cuda:
        _RCNN.cuda()

    start = time.time()
    max_per_image = 100

    vis = False

    if vis:
        thresh = 0.05
    else:
        thresh = 0.0

    save_name = 'thundernet'
    num_images = len(imdb.image_index)
    all_boxes = [[[] for _ in xrange(num_images)] for _ in xrange(imdb.num_classes)]

    output_dir = get_output_dir(imdb, save_name)
    # dataset = roibatchLoader(roidb, ratio_list, ratio_index, args.batch_size, \
    #                          imdb.num_classes, training=False, normalize=False)
    # dataset = roibatchLoader(roidb, imdb.num_classes, training=False)
    dataset = Detection(roidb, num_classes=imdb.num_classes, transform=BaseTransform(cfg.TEST.SIZE, cfg.PIXEL_MEANS), training=False)

    dataloader = torch.utils.data.DataLoader(dataset, batch_size=args.batch_size, shuffle=False, num_workers=0, pin_memory=True)

    data_iter = iter(dataloader)

    _t = {'im_detect': time.time(), 'misc': time.time()}
    det_file = os.path.join(output_dir, 'detections.pkl')

    _RCNN.eval()

    empty_array = np.transpose(np.array([[], [], [], [], []]), (1, 0))

    for i in range(num_images):

        data = next(data_iter)

        with torch.no_grad():
            im_data.resize_(data[0].size()).copy_(data[0])
            im_info.resize_(data[1].size()).copy_(data[1])
            gt_boxes.resize_(data[2].size()).copy_(data[2])
            num_boxes.resize_(data[3].size()).copy_(data[3])
            # hm.resize_(data[4].size()).copy_(data[4])
            # reg_mask.resize_(data[5].size()).copy_(data[5])
            # wh.resize_(data[6].size()).copy_(data[6])
            # offset.resize_(data[7].size()).copy_(data[7])
            # ind.resize_(data[8].size()).copy_(data[8])

        det_tic = time.time()
        with torch.no_grad():
            time_measure, \
            rois, cls_prob, bbox_pred, \
            rpn_loss_cls, rpn_loss_box, \
            RCNN_loss_cls, RCNN_loss_bbox, \
            rois_label = _RCNN(im_data, im_info, gt_boxes, num_boxes,
                               # hm,reg_mask,wh,offset,ind
                               )

        scores = cls_prob.data
        boxes = rois.data[:, :, 1:5]

        if cfg.TEST.BBOX_REG:
            # Apply bounding-box regression deltas
            box_deltas = bbox_pred.data
            if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
                # Optionally normalize targets by a precomputed mean and stdev
                if args.class_agnostic:
                    box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                                 + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
                    box_deltas = box_deltas.view(args.batch_size, -1, 4)
                else:
                    box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                                 + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
                    box_deltas = box_deltas.view(args.batch_size, -1, 4 * len(imdb.classes))

            pred_boxes = bbox_transform_inv(boxes, box_deltas, 1)
            pred_boxes = clip_boxes(pred_boxes, im_info.data, 1)
        else:
            # Simply repeat the boxes, once for each class
            pred_boxes = np.tile(boxes, (1, scores.shape[1]))

        # pred_boxes /= data[1][0][2].item()
        pred_boxes[:, :, 0::2] /= data[1][0][2].item()
        pred_boxes[:, :, 1::2] /= data[1][0][3].item()

        scores = scores.squeeze()
        pred_boxes = pred_boxes.squeeze()
        det_toc = time.time()
        detect_time = det_toc - det_tic
        misc_tic = time.time()
        if vis:
            im = cv2.imread(imdb.image_path_at(i))
            im2show = np.copy(im)
        for j in xrange(1, imdb.num_classes):
            inds = torch.nonzero(scores[:, j] > thresh).view(-1)
            # if there is det
            if inds.numel() > 0:
                cls_scores = scores[:, j][inds]
                _, order = torch.sort(cls_scores, 0, True)
                if args.class_agnostic:
                    cls_boxes = pred_boxes[inds, :]
                else:
                    cls_boxes = pred_boxes[inds][:, j * 4:(j + 1) * 4]

                cls_dets = torch.cat((cls_boxes, cls_scores.unsqueeze(1)), 1)
                # cls_dets = torch.cat((cls_boxes, cls_scores), 1)
                cls_dets = cls_dets[order]
                keep = nms(cls_boxes[order, :], cls_scores[order], cfg.TEST.NMS)

                # keep = soft_nms(cls_dets.cpu().numpy(), Nt=0.5, method=2)
                # keep = torch.as_tensor(keep, dtype=torch.long)

                cls_dets = cls_dets[keep.view(-1).long()]
                if vis:
                    vis_detections(im2show, imdb.classes[j], color_list[j - 1].tolist(), cls_dets.cpu().numpy(), 0.6)
                all_boxes[j][i] = cls_dets.cpu().numpy()
            else:
                all_boxes[j][i] = empty_array

        # Limit to max_per_image detections *over all classes*
        if max_per_image > 0:
            image_scores = np.hstack([all_boxes[j][i][:, -1] for j in xrange(1, imdb.num_classes)])
            if len(image_scores) > max_per_image:
                image_thresh = np.sort(image_scores)[-max_per_image]
                for j in xrange(1, imdb.num_classes):
                    keep = np.where(all_boxes[j][i][:, -1] >= image_thresh)[0]
                    all_boxes[j][i] = all_boxes[j][i][keep, :]

        misc_toc = time.time()
        nms_time = misc_toc - misc_tic

        sys.stdout.write(
            'im_detect: {:d}/{:d}\tDetect: {:.3f}s (RPN: {:.3f}s, Pre-RoI: {:.3f}s, RoI: {:.3f}s, Subnet: {:.3f}s)\tNMS: {:.3f}s\r' \
            .format(i + 1, num_images, detect_time, time_measure[0], time_measure[1], time_measure[2],
                    time_measure[3], nms_time))
        sys.stdout.flush()

    with open(det_file, 'wb') as f:
        pickle.dump(all_boxes, f, pickle.HIGHEST_PROTOCOL)

    print('Evaluating detections')
    ap_50 = imdb.evaluate_detections(all_boxes, output_dir)
    print("map_50: %0.4f" % (ap_50))

    end = time.time()
    print("test time: %0.4fs" % (end - start))


if __name__ == '__main__':

    args = parse_args()

    print('Called with args:')
    print(args)

    if args.dataset == "pascal_voc_0712":
        args.imdb_name = "voc_2007_trainval+voc_2012_trainval"
        args.imdbval_name = "voc_2007_test"
        # args.set_cfgs = [
        #     'ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]',
        #     'MAX_NUM_GT_BOXES', '20'
        # ]
        args.set_cfgs = ['ANCHOR_SCALES', '[2, 4 , 8, 16, 32]', 'ANCHOR_RATIOS', '[1.0/2 , 3.0/4 , 1 , 4.0/3 , 2 ]', 'MAX_NUM_GT_BOXES', '20']
    elif args.dataset == "coco":
        args.imdb_name = "coco_2017_train"
        args.imdbval_name = "coco_2017_val"
        args.set_cfgs = ['ANCHOR_SCALES', '[2, 4 , 8, 16, 32]', 'ANCHOR_RATIOS', '[1.0/2 , 3.0/4 , 1 , 4.0/3 , 2 ]', 'MAX_NUM_GT_BOXES', '50']

    args.cfg_file = "cfgs/{}_ls.yml".format(args.net) if args.large_scale else "cfgs/{}.yml".format(args.net.split("_")[0])

    if args.cfg_file is not None:
        cfg_from_file(args.cfg_file)
    if args.set_cfgs is not None:
        cfg_from_list(args.set_cfgs)

    print('Using config:')
    pprint.pprint(cfg)
    np.random.seed(cfg.RNG_SEED)

    # torch.backends.cudnn.benchmark = True
    if torch.cuda.is_available() and not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")

    # train set
    # -- Note: Use validation set and disable the flipped to enable faster loading.
    cfg.TRAIN.USE_FLIPPED = True
    cfg.USE_GPU_NMS = args.cuda
    imdb, roidb = combined_roidb(args.imdb_name)

    print('{:d} roidb entries'.format(len(roidb)))

    output_dir = args.save_dir
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    if args.cuda:
        cfg.CUDA = True

    eval_result(args, output_dir)
