"""
Author: Benny
Date: Nov 2019
python main.py --model newE --exp_name try1 > nohup/model21A.out &
"""
import argparse
import os
import torch
import datetime
import logging
import sys
import time
import random
import importlib
import torch.multiprocessing as mp
import torch.distributed as dist
import torch.backends.cudnn as cudnn
import shutil
import provider
import numpy as np
import torch.optim as optim
import torch.nn.functional as F

from pathlib import Path
from tqdm import tqdm
from data_util import PartNormalDataset
from partutils import to_categorical, compute_overall_iou, merge_cfg_from_list, IOStream, find_free_port
from tensorboardX import SummaryWriter
from collections import defaultdict
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import CosineAnnealingLR, StepLR, MultiStepLR
from torch.autograd import Variable

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = BASE_DIR
sys.path.append(os.path.join(ROOT_DIR, 'models'))


classes_str = ['aero','bag','cap','car','chair','ear','guitar','knife','lamp','lapt','moto','mug','Pistol','rock','stake','table']


def parse_args():
    parser = argparse.ArgumentParser('Model')
    parser.add_argument('--model', type=str, default='pointnet_part_seg', help='model name')
    parser.add_argument('--batch_size', type=int, default=32, help='batch Size during training')
    parser.add_argument('--test_batch_size', type=int, default=16, help='batch Size during testing')
    parser.add_argument('--epochs', default=200, type=int, help='epoch to run')
    parser.add_argument('--lr', default=0.003, type=float, help='initial learning rate')
    parser.add_argument('--optimizer', type=str, default='Adam', help='Adam or SGD')
    parser.add_argument('--exp_name', type=str, default=None, help='log path')
    parser.add_argument('--scheduler', type=str, default="step", help='log path')
    parser.add_argument('--weight_decay', type=float, default=0, help='weight decay')
    parser.add_argument('--momentum', type=float, default=0.9, help='weight decay')
    parser.add_argument('--use_sgd', action='store_true', default=False, help='use_sgd')
    parser.add_argument('--cuda', action='store_true', default=True, help='cuda')
    parser.add_argument('--eval', action='store_true', default=False, help='eval')
    parser.add_argument('--step', type=int, default=40, help='decay step for lr decay')
    parser.add_argument('--manual_seed', type=int, default=0, help='manual_seed')
    parser.add_argument('--model_type', type=str, default="insiou", help='choose to test the best insiou/clsiou/acc model')
    # for ddp
    parser.add_argument('--sync_bn', action='store_true', default=True, help='sync_bn')
    parser.add_argument('--dist_url', type=str, default="tcp://127.0.0.1:6789")
    parser.add_argument('--dist_backend', type=str, default="nccl")
    parser.add_argument('--multiprocessing_distributed', action='store_true', default=True)
    parser.add_argument('--world_size', type=int, default=1)
    parser.add_argument('--rank', type=int, default=0)
    parser.add_argument('--ngpus_per_node', type=int)
    parser.add_argument('--gpu', type=int)
    parser.add_argument('--workers', type=int, default=8)

    return parser.parse_args()


def get_logger():
    logger_name = "main-logger"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    fmt = "[%(asctime)s %(levelname)s %(filename)s line %(lineno)d %(process)d] %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    file_handler = logging.FileHandler(
        os.path.join('checkpoints', args.exp_name, 'main-' + str(int(time.time())) + '.log'))
    file_handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(file_handler)
    return logger


def worker_init_fn(worker_id):
    random.seed(args.manual_seed + worker_id)


def main_process():
    return not args.multiprocessing_distributed or (args.multiprocessing_distributed and args.rank % args.ngpus_per_node == 0)




def _init_():
    if not os.path.exists('checkpoints'):
        os.makedirs('checkpoints')
    if not os.path.exists('checkpoints/' + args.exp_name):
        os.makedirs('checkpoints/' + args.exp_name)

    # if not args.eval:  # backup the running files
    #     os.system('cp main.py checkpoints' + '/' + args.exp_name + '/' + 'main.py.backup')
    #     os.system('cp util/PAConv_util.py checkpoints' + '/' + args.exp_name + '/' + 'PAConv_util.py.backup')
    #     os.system('cp util/data_util.py checkpoints' + '/' + args.exp_name + '/' + 'data_util.py.backup')
    #     os.system('cp DGCNN_PAConv.py checkpoints' + '/' + args.exp_name + '/' + 'DGCNN_PAConv.py.backup')

    global writer
    writer = SummaryWriter('checkpoints/' + args.exp_name)


def weight_init(m):
    if isinstance(m, torch.nn.Linear):
        torch.nn.init.xavier_normal_(m.weight)
        if m.bias is not None:
            torch.nn.init.constant_(m.bias, 0)
    elif isinstance(m, torch.nn.Conv2d):
        torch.nn.init.xavier_normal_(m.weight)
        if m.bias is not None:
            torch.nn.init.constant_(m.bias, 0)
    elif isinstance(m, torch.nn.Conv1d):
        torch.nn.init.xavier_normal_(m.weight)
        if m.bias is not None:
            torch.nn.init.constant_(m.bias, 0)
    elif isinstance(m, torch.nn.BatchNorm2d):
        torch.nn.init.constant_(m.weight, 1)
        torch.nn.init.constant_(m.bias, 0)
    elif isinstance(m, torch.nn.BatchNorm1d):
        torch.nn.init.constant_(m.weight, 1)
        torch.nn.init.constant_(m.bias, 0)




def train(gpu, ngpus_per_node):

    # ============= Model ===================
    num_part = 50

    MODELClass = importlib.import_module(args.model)
    model = MODELClass.get_model(num_part)
    model.apply(weight_init)
    if main_process():
        logger.info(model)

    if args.sync_bn and args.distributed:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    if args.distributed:
        torch.cuda.set_device(gpu)
        args.batch_size = int(args.batch_size / ngpus_per_node)
        args.test_batch_size = int(args.test_batch_size / ngpus_per_node)
        args.workers = int((args.workers + ngpus_per_node - 1) / ngpus_per_node)
        model = torch.nn.parallel.DistributedDataParallel(model.cuda(), device_ids=[gpu], find_unused_parameters=True)
    else:
        model = torch.nn.DataParallel(model.cuda())


    '''Use Pretrain or not'''
    try:
        state_dict = torch.load("checkpoints/%s/best_insiou_model.pth" % args.exp_name,
                              map_location=torch.device('cpu'))['model']
        for k in state_dict.keys():
            if 'module' not in k:
                from collections import OrderedDict
                new_state_dict = OrderedDict()
                for k in state_dict:
                    new_state_dict['module.' + k] = state_dict[k]
                state_dict = new_state_dict
            break
        model.load_state_dict(state_dict)
        if main_process():
            logger.info("Using pretrained model...")
            logger.info(torch.load("checkpoints/%s/best_insiou_model.pth" % args.exp_name).keys())
        else:
            if main_process():
                logger.info("Training from scratch...")
    except:
        if main_process():
            logger.info("Training from scratch...")

        # =========== Dataloader =================
        train_data = PartNormalDataset(npoints=2048, split='trainval', normalize=False)
        if main_process():
            logger.info("The number of training data is:%d", len(train_data))

        test_data = PartNormalDataset(npoints=2048, split='test', normalize=False)
        if main_process():
            logger.info("The number of test data is:%d", len(test_data))

        if args.distributed:
            train_sampler = torch.utils.data.distributed.DistributedSampler(train_data)
            test_sampler = torch.utils.data.distributed.DistributedSampler(test_data)
        else:
            train_sampler = None
            test_sampler = None

        train_loader = torch.utils.data.DataLoader(train_data, batch_size=args.batch_size, shuffle=(train_sampler is None),
                                                   num_workers=args.workers, pin_memory=True, sampler=train_sampler)
        test_loader = torch.utils.data.DataLoader(test_data, batch_size=args.test_batch_size, shuffle=False,
                                                  num_workers=args.workers, pin_memory=True, sampler=test_sampler)

        # ============= Optimizer ===================
        if args.use_sgd:
            if main_process():
                logger.info("Use SGD")
            opt = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
        else:
            if main_process():
                logger.info("Use Adam")
            opt = optim.Adam(model.parameters(), lr=args.lr, betas=(0.9, 0.999), eps=1e-08,
                             weight_decay=args.weight_decay)

        if args.scheduler == 'cos':
            if main_process():
                logger.info("Use CosLR")
            scheduler = CosineAnnealingLR(opt, args.epochs, eta_min=args.lr / 100)
        else:
            if main_process():
                logger.info("Use StepLR")
            scheduler = StepLR(opt, step_size=args.step, gamma=0.5)

    # ============= Training =================
    best_acc = 0
    best_class_iou = 0
    best_instance_iou = 0
    num_part = 50
    num_classes = 16

    for epoch in range(args.epochs):


        train_epoch(train_loader, model, opt, scheduler, epoch, num_part, num_classes)

        test_metrics, total_per_cat_iou = test_epoch(test_loader, model, epoch, num_part, num_classes)

        # 1. when get the best accuracy, save the model:
        if test_metrics['accuracy'] > best_acc and main_process():
            best_acc = test_metrics['accuracy']
            logger.info('Max Acc:%.5f' % best_acc)
            state = {
                'model': model.module.state_dict() if torch.cuda.device_count() > 1 else model.state_dict(),
                'optimizer': opt.state_dict(), 'epoch': epoch, 'test_acc': best_acc}
            torch.save(state, 'checkpoints/%s/best_acc_model.pth' % args.exp_name)

        # 2. when get the best instance_iou, save the model:
        if test_metrics['shape_avg_iou'] > best_instance_iou and main_process():
            best_instance_iou = test_metrics['shape_avg_iou']
            logger.info('Max instance iou:%.5f' % best_instance_iou)
            state = {
                'model': model.module.state_dict() if torch.cuda.device_count() > 1 else model.state_dict(),
                'optimizer': opt.state_dict(), 'epoch': epoch, 'test_instance_iou': best_instance_iou}
            torch.save(state, 'checkpoints/%s/best_insiou_model.pth' % args.exp_name)

        # 3. when get the best class_iou, save the model:
        # first we need to calculate the average per-class iou
        class_iou = 0
        for cat_idx in range(16):
            class_iou += total_per_cat_iou[cat_idx]
        avg_class_iou = class_iou / 16
        if avg_class_iou > best_class_iou  and main_process():
            best_class_iou = avg_class_iou
            # print the iou of each class:
            for cat_idx in range(16):
                if main_process():
                    logger.info(classes_str[cat_idx] + ' iou: ' + str(total_per_cat_iou[cat_idx]))
            logger.info('Max class iou:%.5f' % best_class_iou)
            state = {
                'model': model.module.state_dict() if torch.cuda.device_count() > 1 else model.state_dict(),
                'optimizer': opt.state_dict(), 'epoch': epoch, 'test_class_iou': best_class_iou}
            torch.save(state, 'checkpoints/%s/best_clsiou_model.pth' % args.exp_name)

    if main_process():
        # report best acc, ins_iou, cls_iou
        logger.info('Final Max Acc:%.5f' % best_acc)
        logger.info('Final Max instance iou:%.5f' % best_instance_iou)
        logger.info('Final Max class iou:%.5f' % best_class_iou)
        # save last model
        state = {
            'model': model.module.state_dict() if torch.cuda.device_count() > 1 else model.state_dict(),
            'optimizer': opt.state_dict(), 'epoch': args.epochs - 1, 'test_iou': best_instance_iou}
        torch.save(state, 'checkpoints/%s/model_ep%d.pth' % (args.exp_name, args.epochs))


def train_epoch(train_loader, model, opt, scheduler, epoch, num_part, num_classes):
    train_loss = 0.0
    count = 0.0
    accuracy = []
    shape_ious = 0.0
    metrics = defaultdict(lambda: list())
    model.train()

    for batch_id, (points, label, target, norm_plt) in tqdm(enumerate(train_loader), total=len(train_loader), smoothing=0.9):
        batch_size, num_point, _ = points.size()
        points, label, target, norm_plt = Variable(points.float()), Variable(label.long()), Variable(target.long()), \
                                          Variable(norm_plt.float())
        points = points.transpose(2, 1)
        norm_plt = norm_plt.transpose(2, 1)
        points, label, target, norm_plt = points.cuda(non_blocking=True), label.squeeze(1).cuda(non_blocking=True), \
                                          target.cuda(non_blocking=True), norm_plt.cuda(non_blocking=True)
        # target: b,n
        seg_pred, loss = model(points, norm_plt, to_categorical(label, num_classes), target)  # seg_pred: b,n,50

        # instance iou without considering the class average at each batch_size:
        batch_shapeious = compute_overall_iou(seg_pred, target, num_part)  # list of of current batch_iou:[iou1,iou2,...,iou#b_size]
        # total iou of current batch in each process:
        batch_shapeious = seg_pred.new_tensor([np.sum(batch_shapeious)], dtype=torch.float64)  # same device with seg_pred!!!

        # Loss backward
        if not args.multiprocessing_distributed:
            loss = torch.mean(loss)
        opt.zero_grad()
        loss.backward()
        opt.step()

        # accuracy
        seg_pred = seg_pred.contiguous().view(-1, num_part)  # b*n,50
        target = target.view(-1, 1)[:, 0]   # b*n
        pred_choice = seg_pred.contiguous().data.max(1)[1]  # b*n
        correct = pred_choice.eq(target.contiguous().data).sum()  # torch.int64: total number of correct-predict pts

        # sum
        if args.multiprocessing_distributed:
            _count = seg_pred.new_tensor([batch_size], dtype=torch.long)  # same device with seg_pred!!!
            dist.all_reduce(loss)
            dist.all_reduce(_count)
            dist.all_reduce(batch_shapeious)  # sum the batch_ious across all processes
            dist.all_reduce(correct)  # sum the correct across all processes
            # ! batch_size: the total number of samples in one iteration when with dist, equals to batch_size when without dist:
            batch_size = _count.item()
        shape_ious += batch_shapeious.item()  # count the sum of ious in each iteration
        count += batch_size   # count the total number of samples in each iteration
        train_loss += loss.item() * batch_size
        accuracy.append(correct.item()/(batch_size * num_point))   # append the accuracy of each iteration

        # Note: We do not need to calculate per_class iou during training

    if args.scheduler == 'cos':
        scheduler.step()
    elif args.scheduler == 'step':
        if opt.param_groups[0]['lr'] > 0.9e-5:
            scheduler.step()
        if opt.param_groups[0]['lr'] < 0.9e-5:
            for param_group in opt.param_groups:
                param_group['lr'] = 0.9e-5
    if main_process():
        logger.info('Learning rate: %f', opt.param_groups[0]['lr'])

    metrics['accuracy'] = np.mean(accuracy)
    metrics['shape_avg_iou'] = shape_ious * 1.0 / count

    outstr = 'Train %d, loss: %f, train acc: %f, train ins_iou: %f' % (epoch+1, train_loss * 1.0 / count,
                                                                       metrics['accuracy'], metrics['shape_avg_iou'])
    if main_process():
        logger.info(outstr)
        # Write to tensorboard
        writer.add_scalar('loss_train', train_loss * 1.0 / count, epoch + 1)
        writer.add_scalar('Acc_train', metrics['accuracy'], epoch + 1)
        writer.add_scalar('ins_iou', metrics['shape_avg_iou'])


def test_epoch(test_loader, model, epoch, num_part, num_classes):
    test_loss = 0.0
    count = 0.0
    accuracy = []
    shape_ious = 0.0
    final_total_per_cat_iou = np.zeros(16).astype(np.float32)
    final_total_per_cat_seen = np.zeros(16).astype(np.int32)
    metrics = defaultdict(lambda: list())
    model.eval()

    # label_size: b, means each sample has one corresponding class
    for batch_id, (points, label, target, norm_plt) in tqdm(enumerate(test_loader), total=len(test_loader), smoothing=0.9):
        batch_size, num_point, _ = points.size()
        points, label, target, norm_plt = Variable(points.float()), Variable(label.long()), Variable(target.long()), \
                                          Variable(norm_plt.float())
        points = points.transpose(2, 1)
        norm_plt = norm_plt.transpose(2, 1)
        points, label, target, norm_plt = points.cuda(non_blocking=True), label.squeeze(1).cuda(non_blocking=True), \
                                          target.cuda(non_blocking=True), norm_plt.cuda(non_blocking=True)
        seg_pred = model(points, norm_plt, to_categorical(label, num_classes))  # b,n,50

        # instance iou without considering the class average at each batch_size:
        batch_shapeious = compute_overall_iou(seg_pred, target, num_part)  # [b]

        # per category iou at each batch_size:
        if args.multiprocessing_distributed:
            # creat new zero to only count current iter to avoid counting the value of last iterations twice in reduce!
            cur_total_per_cat_iou = np.zeros(16).astype(np.float32)
            cur_total_per_cat_seen = np.zeros(16).astype(np.int32)
            for shape_idx in range(seg_pred.size(0)):  # sample_idx
                cur_gt_label = label[shape_idx]  # label[sample_idx], denotes current sample belongs to which cat
                cur_total_per_cat_iou[cur_gt_label] += batch_shapeious[shape_idx]  # add the iou belongs to this cat
                cur_total_per_cat_seen[cur_gt_label] += 1  # count the number of this cat is chosen
        else:
            for shape_idx in range(seg_pred.size(0)):  # sample_idx
                cur_gt_label = label[shape_idx]  # label[sample_idx], denotes current sample belongs to which cat
                final_total_per_cat_iou[cur_gt_label] += batch_shapeious[shape_idx]  # add the iou belongs to this cat
                final_total_per_cat_seen[cur_gt_label] += 1  # count the number of this cat is chosen
        # total iou of current batch in each process:
        batch_ious = seg_pred.new_tensor([np.sum(batch_shapeious)], dtype=torch.float64)  # same device with seg_pred!!!

        # prepare seg_pred and target for later calculating loss and acc:
        seg_pred = seg_pred.contiguous().view(-1, num_part)
        target = target.view(-1, 1)[:, 0]
        # Loss
        loss = F.nll_loss(seg_pred.contiguous(), target.contiguous())

        # accuracy:
        pred_choice = seg_pred.data.max(1)[1]  # b*n
        correct = pred_choice.eq(target.data).sum()  # torch.int64: total number of correct-predict pts

        if args.multiprocessing_distributed:
            _count = seg_pred.new_tensor([batch_size], dtype=torch.long)  # same device with seg_pred!!!
            dist.all_reduce(loss)
            dist.all_reduce(_count)
            dist.all_reduce(batch_ious)  # sum the batch_ious across all processes
            dist.all_reduce(correct)  # sum the correct across all processes
            cur_total_per_cat_iou = seg_pred.new_tensor(cur_total_per_cat_iou,
                                                        dtype=torch.float32)  # same device with seg_pred!!!
            cur_total_per_cat_seen = seg_pred.new_tensor(cur_total_per_cat_seen,
                                                         dtype=torch.int32)  # same device with seg_pred!!!
            dist.all_reduce(cur_total_per_cat_iou)  # sum the per_cat_iou across all processes (element-wise)
            dist.all_reduce(cur_total_per_cat_seen)  # sum the per_cat_seen across all processes (element-wise)
            final_total_per_cat_iou += cur_total_per_cat_iou.cpu().numpy()
            final_total_per_cat_seen += cur_total_per_cat_seen.cpu().numpy()
            # ! batch_size: the total number of samples in one iteration when with dist, equals to batch_size when without dist:
            batch_size = _count.item()
        else:
            loss = torch.mean(loss)

        shape_ious += batch_ious.item()  # count the sum of ious in each iteration
        count += batch_size  # count the total number of samples in each iteration
        test_loss += loss.item() * batch_size
        accuracy.append(correct.item() / (batch_size * num_point))  # append the accuracy of each iteration

    for cat_idx in range(16):
        if final_total_per_cat_seen[cat_idx] > 0:  # indicating this cat is included during previous iou appending
            final_total_per_cat_iou[cat_idx] = final_total_per_cat_iou[cat_idx] / final_total_per_cat_seen[cat_idx]  # avg class iou across all samples

    metrics['accuracy'] = np.mean(accuracy)
    metrics['shape_avg_iou'] = shape_ious * 1.0 / count

    outstr = 'Test %d, loss: %f, test acc: %f  test ins_iou: %f' % (epoch + 1, test_loss * 1.0 / count,
                                                                    metrics['accuracy'], metrics['shape_avg_iou'])

    if main_process():
        logger.info(outstr)
        # Write to tensorboard
        writer.add_scalar('loss_train', test_loss * 1.0 / count, epoch + 1)
        writer.add_scalar('Acc_train', metrics['accuracy'], epoch + 1)
        writer.add_scalar('ins_iou', metrics['shape_avg_iou'])


    return metrics, final_total_per_cat_iou


def test(gpu, ngpus_per_node):

    # Try to load models
    num_part = 50

    MODELClass = importlib.import_module(args.model)
    model = MODELClass.get_model(num_part)

    from collections import OrderedDict
    state_dict = torch.load("checkpoints/%s/best_%s_model.pth" % (args.exp_name, args.model_type),
                            map_location=torch.device('cpu'))['model']
    new_state_dict = OrderedDict()
    for layer in state_dict:
        new_state_dict[layer.replace('module.', '')] = state_dict[layer]
    model.load_state_dict(new_state_dict)

    from collections import OrderedDict
    state_dict = torch.load("checkpoints/%s/best_%s_model.pth" % (args.exp_name, args.model_type),
                            map_location=torch.device('cpu'))['model']

    new_state_dict = OrderedDict()
    for layer in state_dict:
        new_state_dict[layer.replace('module.', '')] = state_dict[layer]
    model.load_state_dict(new_state_dict)

    if main_process():
        logger.info(model)
    if args.sync_bn:
        assert args.distributed == True
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
    if args.distributed:
        torch.cuda.set_device(gpu)
        args.batch_size = int(args.batch_size / ngpus_per_node)
        args.test_batch_size = int(args.test_batch_size / ngpus_per_node)
        args.workers = int((args.workers + ngpus_per_node - 1) / ngpus_per_node)
        model = torch.nn.parallel.DistributedDataParallel(model.cuda(), device_ids=[gpu], find_unused_parameters=True)
    else:
        model = model.cuda()
    # Dataloader
    test_data = PartNormalDataset(npoints=2048, split='test', normalize=False)
    if main_process():
        logger.info("The number of test data is:%d", len(test_data))
    if args.distributed:
        test_sampler = torch.utils.data.distributed.DistributedSampler(test_data)
    else:
        test_sampler = None
    test_loader = torch.utils.data.DataLoader(test_data, batch_size=args.test_batch_size, shuffle=False,
                                              num_workers=args.workers, pin_memory=True, sampler=test_sampler)

    model.eval()
    num_part = 50
    num_classes = 16
    metrics = defaultdict(lambda: list())
    hist_acc = []
    shape_ious = []
    total_per_cat_iou = np.zeros((16)).astype(np.float32)
    total_per_cat_seen = np.zeros((16)).astype(np.int32)

    for batch_id, (points, label, target, norm_plt) in tqdm(enumerate(test_loader), total=len(test_loader), smoothing=0.9):
        batch_size, num_point, _ = points.size()
        points, label, target, norm_plt = Variable(points.float()), Variable(label.long()), Variable(target.long()), Variable(norm_plt.float())
        points = points.transpose(2, 1)
        norm_plt = norm_plt.transpose(2, 1)
        points, label, target, norm_plt = points.cuda(non_blocking=True), label.squeeze().cuda(
            non_blocking=True), target.cuda(non_blocking=True), norm_plt.cuda(non_blocking=True)

        with torch.no_grad():
            seg_pred = model(points, norm_plt, to_categorical(label, num_classes))  # b,n,50

        # instance iou without considering the class average at each batch_size:
        batch_shapeious = compute_overall_iou(seg_pred, target, num_part)  # [b]
        shape_ious += batch_shapeious  # iou +=, equals to .append

        # per category iou at each batch_size:
        for shape_idx in range(seg_pred.size(0)):  # sample_idx
            cur_gt_label = label[shape_idx]  # label[sample_idx]
            total_per_cat_iou[cur_gt_label] += batch_shapeious[shape_idx]
            total_per_cat_seen[cur_gt_label] += 1

        # accuracy:
        seg_pred = seg_pred.contiguous().view(-1, num_part)
        target = target.view(-1, 1)[:, 0]
        pred_choice = seg_pred.data.max(1)[1]
        correct = pred_choice.eq(target.data).cpu().sum()
        metrics['accuracy'].append(correct.item() / (batch_size * num_point))

    hist_acc += metrics['accuracy']
    metrics['accuracy'] = np.mean(hist_acc)
    metrics['shape_avg_iou'] = np.mean(shape_ious)
    for cat_idx in range(16):
        if total_per_cat_seen[cat_idx] > 0:
            total_per_cat_iou[cat_idx] = total_per_cat_iou[cat_idx] / total_per_cat_seen[cat_idx]

    # First we need to calculate the iou of each class and the avg class iou:
    class_iou = 0
    for cat_idx in range(16):
        class_iou += total_per_cat_iou[cat_idx]
        if main_process():
            logger.info(classes_str[cat_idx] + ' iou: ' + str(
                total_per_cat_iou[cat_idx]))  # print the iou of each class
    avg_class_iou = class_iou / 16
    outstr = 'Test :: test acc: %f  test class mIOU: %f, test instance mIOU: %f' % (metrics['accuracy'], avg_class_iou, metrics['shape_avg_iou'])
    if main_process():
        logger.info(outstr)



def main_worker(gpu, ngpus_per_node, argss):
    global args
    args = argss

    if args.distributed:
        if args.dist_url == "env://" and args.rank == -1:
            args.rank = int(os.environ["RANK"])
        if args.multiprocessing_distributed:
            args.rank = args.rank * ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url, world_size=args.world_size,
                                rank=args.rank)

    if main_process():
        if not os.path.exists('checkpoints'):
            os.makedirs('checkpoints')
        if not os.path.exists('checkpoints/' + args.exp_name):
            os.makedirs('checkpoints/' + args.exp_name)

        if not args.eval:  # backup the running files
            os.system('cp main_ddp.py checkpoints' + '/' + args.exp_name + '/' + 'main_ddp.py.backup')
            # os.system('cp util/PAConv_util.py checkpoints' + '/' + args.exp_name + '/' + 'PAConv_util.py.backup')
            os.system('cp util/data_util.py checkpoints' + '/' + args.exp_name + '/' + 'data_util.py.backup')
            # os.system('cp DGCNN_PAConv.py checkpoints' + '/' + args.exp_name + '/' + 'DGCNN_PAConv.py.backup')

        global logger, writer
        writer = SummaryWriter('checkpoints/' + args.exp_name)
        logger = get_logger()
        logger.info(args)

    args.cuda = torch.cuda.is_available()

    assert not args.eval, "The all_reduce function of PyTorch DDP will ignore/repeat inputs " \
                          "(leading to the wrong result), " \
                          "please use main.py to test (avoid DDP) for getting the right result."

    train(gpu, ngpus_per_node)



if __name__ == "__main__":
    args = parse_args()
    if args.exp_name is None:
        args.exp_name = args.model+str(datetime.datetime.now().strftime('-%Y%m%d%H%M%S'))
    else:
        args.exp_name = args.model+ "-" + args.exp_name

    args.gpu = [int(i) for i in os.environ['CUDA_VISIBLE_DEVICES'].split(',')]
    if args.manual_seed is not None:
        random.seed(args.manual_seed)
        np.random.seed(args.manual_seed)
        torch.manual_seed(args.manual_seed)
        torch.cuda.manual_seed(args.manual_seed)
        torch.cuda.manual_seed_all(args.manual_seed)
        cudnn.benchmark = False
        cudnn.deterministic = True
    if args.dist_url == "env://" and args.world_size == -1:
        args.world_size = int(os.environ["WORLD_SIZE"])
    args.distributed = args.world_size > 1 or args.multiprocessing_distributed
    args.ngpus_per_node = len(args.gpu)
    if len(args.gpu) == 1:
        args.sync_bn = False
        args.distributed = False
        args.multiprocessing_distributed = False
    if args.multiprocessing_distributed:
        port = find_free_port()
        args.dist_url = f"tcp://127.0.0.1:{port}"
        args.world_size = args.ngpus_per_node * args.world_size
        mp.spawn(main_worker, nprocs=args.ngpus_per_node, args=(args.ngpus_per_node, args))
    else:
        main_worker(args.gpu, args.ngpus_per_node, args)


