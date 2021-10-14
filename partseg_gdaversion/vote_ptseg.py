from __future__ import print_function
import os
import argparse
import torch
import torch.optim as optim
from torch.optim.lr_scheduler import CosineAnnealingLR, StepLR
from util.data_util import PartNormalDataset
import torch.nn.functional as F
import torch.nn as nn
import model as models
import numpy as np
from torch.utils.data import DataLoader
from util.util import to_categorical, compute_overall_iou, IOStream
from tqdm import tqdm
from collections import defaultdict
from torch.autograd import Variable
import random


classes_str = ['aero','bag','cap','car','chair','ear','guitar','knife','lamp','lapt','moto','mug','Pistol','rock','stake','table']

class PointcloudScale(object):
    def __init__(self, scale_low=2. / 3., scale_high=3. / 2.):
        self.scale_low = scale_low
        self.scale_high = scale_high

    def __call__(self, pc):
        bsize = pc.size()[0]
        for i in range(bsize):
            xyz = np.random.uniform(low=self.scale_low, high=self.scale_high, size=[3])
            pc[i, :, 0:3] = torch.mul(pc[i, :, 0:3], torch.from_numpy(xyz).float().cuda())
        return pc


def vote(args, io):

    # ============= Model ===================
    num_part = 50
    device = torch.device("cuda" if args.cuda else "cpu")

    model = models.__dict__[args.model](num_part).to(device)
    io.cprint(str(model))

    model = nn.DataParallel(model)
    print("Let's use ", torch.cuda.device_count(), " GPUs!")

    '''Resume'''
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

    print("Resume training model...")
    print(torch.load("checkpoints/%s/best_insiou_model.pth" % args.exp_name).keys())


    # =========== Dataloader =================
    test_data = PartNormalDataset(npoints=2048, split='test', normalize=False)
    print("The number of test data is:%d", len(test_data))

    test_loader = DataLoader(test_data, batch_size=args.test_batch_size, shuffle=False,
                             num_workers=args.workers, drop_last=False)

    # ============= Training =================
    best_acc = 0
    best_class_iou = 0
    best_instance_iou = 0
    num_part = 50
    num_classes = 16

    for epoch in range(args.epochs):

        test_metrics, total_per_cat_iou = test_epoch(test_loader, model, epoch, num_part, num_classes, io)

        # 1. when get the best accuracy, save the model:
        if test_metrics['accuracy'] > best_acc:
            best_acc = test_metrics['accuracy']
            io.cprint('Max Acc:%.5f' % best_acc)

        # 2. when get the best instance_iou, save the model:
        if test_metrics['shape_avg_iou'] > best_instance_iou:
            best_instance_iou = test_metrics['shape_avg_iou']
            io.cprint('Max instance iou:%.5f' % best_instance_iou)

        # 3. when get the best class_iou, save the model:
        # first we need to calculate the average per-class iou
        class_iou = 0
        for cat_idx in range(16):
            class_iou += total_per_cat_iou[cat_idx]
        avg_class_iou = class_iou / 16
        if avg_class_iou > best_class_iou:
            best_class_iou = avg_class_iou
            # print the iou of each class:
            for cat_idx in range(16):
                io.cprint(classes_str[cat_idx] + ' iou: ' + str(total_per_cat_iou[cat_idx]))
            io.cprint('Max class iou:%.5f' % best_class_iou)

    # report best acc, ins_iou, cls_iou
    io.cprint('Final Max Acc:%.5f' % best_acc)
    io.cprint('Final Max instance iou:%.5f' % best_instance_iou)
    io.cprint('Final Max class iou:%.5f' % best_class_iou)



def test_epoch(test_loader, model, epoch, num_part, num_classes, io):
    pointscale = PointcloudScale(scale_low=0.87, scale_high=1.15)
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
        # points = points.transpose(2, 1)
        norm_plt = norm_plt.transpose(2, 1)
        points, label, target, norm_plt = points.cuda(non_blocking=True), label.squeeze(1).cuda(non_blocking=True), \
                                          target.cuda(non_blocking=True), norm_plt.cuda(non_blocking=True)

        seg_pred = 0.
        for v in range(args.NUM_VOTE):
            scaled_point = points
            scaled_point.data = pointscale(points.data)
            scaled_point = scaled_point.transpose(2, 1)
            with torch.no_grad():
                seg_pred_v = model(scaled_point, norm_plt, to_categorical(label, num_classes))  # b,n,50
            seg_pred += seg_pred_v
        seg_pred /= args.NUM_VOTE


        # instance iou without considering the class average at each batch_size:
        batch_shapeious = compute_overall_iou(seg_pred, target, num_part)  # [b]
        # per category iou at each batch_size:

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

    io.cprint(outstr)

    return metrics, final_total_per_cat_iou




if __name__ == "__main__":
    # Training settings
    parser = argparse.ArgumentParser(description='3D Shape Part Segmentation')
    parser.add_argument('--model', type=str, default='PointMLP1')
    parser.add_argument('--exp_name', type=str, default='demo1', metavar='N',
                        help='Name of the experiment')
    parser.add_argument('--batch_size', type=int, default=32, metavar='batch_size',
                        help='Size of batch)')
    parser.add_argument('--test_batch_size', type=int, default=32, metavar='batch_size',
                        help='Size of batch)')
    # parser.add_argument('--epochs', type=int, default=350, metavar='N',
    #                     help='number of episode to train')
    parser.add_argument('--use_sgd', type=bool, default=False,
                        help='Use SGD')
    parser.add_argument('--scheduler', type=str, default='step',
                        help='lr scheduler')
    parser.add_argument('--step', type=int, default=40,
                        help='lr decay step')
    parser.add_argument('--lr', type=float, default=0.003, metavar='LR',
                        help='learning rate')
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
                        help='SGD momentum (default: 0.9)')
    parser.add_argument('--no_cuda', type=bool, default=False,
                        help='enables CUDA training')
    parser.add_argument('--manual_seed', type=int, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--eval', type=bool,  default=False,
                        help='evaluate the model')
    parser.add_argument('--num_points', type=int, default=2048,
                        help='num of points to use')
    parser.add_argument('--workers', type=int, default=12)
    parser.add_argument('--resume', type=bool, default=False,
                        help='Resume training or not')
    parser.add_argument('--model_type', type=str, default='insiou',
                        help='choose to test the best insiou/clsiou/acc model (options: insiou, clsiou, acc)')
     # voting
    parser.add_argument('--NUM_VOTE', type=int, default=10)
    parser.add_argument('--epochs', type=int, default=200)

    args = parser.parse_args()
    args.exp_name = args.model+"_"+args.exp_name


    io = IOStream('checkpoints/' + args.exp_name + '/%s_vote.log' % (args.exp_name))
    io.cprint(str(args))

    if args.manual_seed is not None:
        random.seed(args.manual_seed)
        np.random.seed(args.manual_seed)
        torch.manual_seed(args.manual_seed)

    args.cuda = not args.no_cuda and torch.cuda.is_available()

    if args.cuda:
        io.cprint('Using GPU')
        if args.manual_seed is not None:
            torch.cuda.manual_seed(args.manual_seed)
            torch.cuda.manual_seed_all(args.manual_seed)
    else:
        io.cprint('Using CPU')

    vote(args, io)
