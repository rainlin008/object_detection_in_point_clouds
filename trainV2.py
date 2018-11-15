import torch
import torch.nn as nn
from torch.optim import Adam
from torch.optim.lr_scheduler import MultiStepLR
from torch.utils.data import DataLoader
import os
import time
import traceback
from queue import Queue
import argparse

from networks.networks import PointCloudDetector as HawkEye
from datautils.dataloader import *
import config as cnf
from lossUtils import computeLoss
import misc

parser = argparse.ArgumentParser(description='Train network')
parser.add_argument('--step-lr', action='store_true')
parser.add_argument('--aug-data', action='store_true')
parser.add_argument('-f', '--model-file', default=None)
parser.add_argument('-r', '--root-dir', default=None)
parser.add_argument('-p', '--pixor', action='store_true')
parser.add_argument('-v', '--voxelnet', action='store_true')
parser.add_argument('-e', '--epochs', type=int, default=None)
parser.add_argument('--aug-scheme', default=None)
parser.add_argument('-m', '--multi-gpu', action='store_true')
parser.add_argument('--val', action='store_true')
parser.add_argument('-c', '--clip', type=float, default=0.25)
args = parser.parse_args()

torch.manual_seed(0)

if args.model_file:
	cnf.model_file = args.model_file
	cnf.trainlog = cnf.trainlog[:-9] + args.model_file.split('/')[-1][:-11] + 'train.txt'
	cnf.trainlog2 = cnf.trainlog2[:-9] + args.model_file.split('/')[-1][:-11] + 'etime.txt'
	cnf.vallog = cnf.vallog[:-8] + args.model_file.split('/')[-1][:-11] + 'val.txt'
	cnf.gradNormlog = cnf.gradNormlog[:-9] + args.model_file.split('/')[-1][:-11] + 'gnorm.txt'
if args.root_dir:
	cnf.rootDir = args.root_dir
if args.pixor:
	args.aug_scheme = 'pixor'
elif args.voxelnet:
	args.aug_scheme = 'voxelnet'
else:
	args.aug_scheme = None
if args.epochs:
	cnf.epochs = args.epochs

# data loaders
train_loader = DataLoader(
	LidarLoader_2(cnf.rootDir+'/train', cnf.objtype, args=args, train=True, standarize=False),
	batch_size = cnf.batchSize, shuffle=True, num_workers=0,
	collate_fn=collate_fn_3, pin_memory=True
)
if args.val:
	val_loader = DataLoader(
		LidarLoader_2(cnf.rootDir+'/val', cnf.objtype, args=args, train=True, augData=False, standarize=False),
		batch_size = cnf.batchSize, shuffle=True, num_workers=0,
		collate_fn=collate_fn_3, pin_memory=True
	)

carMean = torch.from_numpy(cnf.carMean)
carSTD = torch.from_numpy(cnf.carSTD)

# create detector object and intialize weights
hawkEye = HawkEye(cnf.res_block_layers, cnf.up_sample_layers, cnf.deconv, carMean, carSTD).to(cnf.device)
hawkEye.apply(misc.weights_init)

if args.multi_gpu:
	hawkEye = nn.DataParallel(hawkEye)

# network optimization method
if args.step_lr:
	optimizer = Adam(hawkEye.parameters(), lr=cnf.slr)	
	scheduler = MultiStepLR(optimizer, milestones=[20,30], gamma=0.1)
else:	
	optimizer = Adam(hawkEye.parameters(), lr=cnf.lr)

# status string writer thread and queue
queue = Queue()
worker = misc.FileWriterThread(queue, cnf.trainlog)
worker.daemon = True
worker.start()

# status string writer thread and queue
queue = Queue()
worker = misc.FileWriterThread(queue, cnf.trainlog)
worker.daemon = True
worker.start()

if args.val:
	valqueue = Queue()
	valworker = misc.FileWriterThread(valqueue, cnf.vallog)
	valworker.daemon = True
	valworker.start()

def train(epoch):
	hawkEye.train()
	# empty the gradient buffer
	hawkEye.zero_grad()

	for batchId, batch_data in enumerate(train_loader):
		st1 = time.time()
		
		data, target, filenames, zoom0_3, zoom1_2 = batch_data
		
		if data.size(0) < cnf.batchSize:
			del data
			del target
			del filenames
			del zoom0_3
			del zoom1_2
			continue

		data = data.cuda(non_blocking=True)

		# pass data through network and predict
		cla, loc = hawkEye(data)
		
		targets = []
		zoom0_3s = []
		zoom1_2s = []

		for i in range(cnf.batchSize):
			targets.append(target[i].cuda(non_blocking=True))
			zoom0_3s.append(zoom0_3[i].cuda(non_blocking=True))
			zoom1_2s.append(zoom1_2[i].cuda(non_blocking=True))

		# compute loss, gradient, and optimize
		st = time.time()
		claLoss, locLoss, iou, meanConfidence, ps, ns = computeLoss(cla, loc, targets, zoom0_3s, zoom1_2s)
		ed = time.time()
		if claLoss is None:
			trainLoss = None
			tl = None
			cl = None
			ll = None
			# ls = cnf.logString3.format(epoch, batchId)
		elif locLoss is not None:
			trainLoss = claLoss + locLoss
			tl = trainLoss.item()
			cl = claLoss.item()
			ll = locLoss.item()
			# ls = cnf.logString1.format(epoch, batchId, claLoss.item(), locLoss.item(), trainLoss.item())
		else:
			trainLoss = claLoss
			tl = trainLoss.item()
			cl = claLoss.item()
			ll = None
			# ls = cnf.logString2.format(epoch, batchId, claLoss.item(), trainLoss.item())

		# trainLoss = claLoss+locLoss
		if trainLoss is not None:
			trainLoss.backward()

		# gradients are accumulated over cnf.accumulationSteps
		if (batchId+1)%cnf.accumulationSteps == 0:
			gradVec = misc.parameters_to_vector(hawkEye.parameters())
			gradNorm = gradVec.norm(2)
			misc.writeToFile(cnf.gradNormlog, cnf.normLogString.format(batchId, epoch, gradNorm))

			torch.nn.utils.clip_grad_norm_(hawkEye.parameters(), args.clip)
			optimizer.step()
			hawkEye.zero_grad()

		ed1 = time.time()
		queue.put((epoch, batchId, cl, ll, tl, int(ps), int(ns), iou, meanConfidence, ed-st, ed1-st1))

		del data
		del target
		del filenames
		del zoom0_3
		del zoom1_2

def validation(epoch):
	hawkEye.eval()

	for batchId, batch_data in enumerate(val_loader):
		st1 = time.time()
		
		data, target, filenames, zoom0_3, zoom1_2 = batch_data
		
		if data.size(0) < cnf.batchSize:
			del data
			del target
			del filenames
			del zoom0_3
			del zoom1_2
			continue

		data = data.cuda(non_blocking=True)

		# pass data through network and predict
		cla, loc = hawkEye(data)
		
		targets = []
		zoom0_3s = []
		zoom1_2s = []

		for i in range(cnf.batchSize):
			targets.append(target[i].cuda(non_blocking=True))
			zoom0_3s.append(zoom0_3[i].cuda(non_blocking=True))
			zoom1_2s.append(zoom1_2[i].cuda(non_blocking=True))

		# compute loss, gradient, and optimize
		st = time.time()
		claLoss, locLoss, iou, meanConfidence, ps, ns = computeLoss(cla, loc, targets, zoom0_3s, zoom1_2s)
		ed = time.time()
		if claLoss is None:
			trainLoss = None
			tl = None
			cl = None
			ll = None
			# ls = cnf.logString3.format(epoch, batchId)
		elif locLoss is not None:
			trainLoss = claLoss + locLoss
			tl = trainLoss.item()
			cl = claLoss.item()
			ll = locLoss.item()
			# ls = cnf.logString1.format(epoch, batchId, claLoss.item(), locLoss.item(), trainLoss.item())
		else:
			trainLoss = claLoss
			tl = trainLoss.item()
			cl = claLoss.item()
			ll = None
			# ls = cnf.logString2.format(epoch, batchId, claLoss.item(), trainLoss.item())

		ed1 = time.time()
		valqueue.put((epoch, batchId, cl, ll, tl, int(ps), int(ns), iou, meanConfidence, ed-st, ed1-st1))

		del data
		del target
		del filenames
		del zoom0_3
		del zoom1_2

if __name__ == '__main__':
	# load model file if present
	if os.path.isfile(cnf.model_file):
		hawkEye.load_state_dict(torch.load(cnf.model_file,
			map_location=lambda storage, loc: storage))

	try:
		for epoch in range(cnf.epochs):
			# learning rate decay scheduler
			if args.step_lr:
				scheduler.step()

			st = time.time()
			train(epoch)
			ed = time.time()
			misc.writeToFile(cnf.trainlog2, '~~~~~epoch ' + str(epoch) + ' end time taken: '+str(ed-st)+' secs~~~~\n')

			# run validation every 10 epochs
			if args.val:
				validation(epoch)

			if (epoch+1)%10 == 0:
				torch.save(hawkEye.state_dict(), cnf.model_file)

	except BaseException as e:
		trace = traceback.format_exc()
		misc.writeToFile(cnf.errorlog, trace+'\n\n\n')
	finally:
		torch.save(hawkEye.state_dict(), cnf.model_file)
		del hawkEye

	# finish all tasks
	queue.join()

	if args.val:
		valqueue.join()