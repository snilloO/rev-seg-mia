from comet_ml import Experiment, ExistingExperiment
import sys
sys.path.append("..")
import torch
import torch.optim as optim
import torch.nn as nn
import bratsUtils
import torch.nn.functional as F
import revtorch.revtorch as rv
import random
import numpy as np
encDepth = 1
BATCH_SIZE = 16
EXPERIMENT_NAME = "ReversibleAll{}_{}".format(encDepth,BATCH_SIZE)
id = EXPERIMENT_NAME

#restore experiment
#VALIDATE_ALL = False
#PREDICT = True
#RESTORE_ID = EXPERIMENT_NAME
#RESTORE_EPOCH = "best1"
#LOG_COMETML_EXISTING_EXPERIMENT = ""

#general settings
SAVE_CHECKPOINTS = True #set to true to create a checkpoint at every epoch

EXPERIMENT_TAGS = ["bugfreeFinalDrop"]
EPOCHS = 1000

VIRTUAL_BATCHSIZE = 1
VALIDATE_EVERY_K_EPOCHS = 1
SAVE_EVERY_K_EPOCHS = 25
INPLACE = True

#hyperparameters
#CHANNELS = [80,160,320,640]
#CHANNELS = [64,128,256,512]
#CHANNELS = [96,192,384,768]
#CHANNELS = [72,144,288,576]
CHANNELS =[60,120,240,480]
INITIAL_LR = 5e-4
L2_REGULARIZER = 1e-5

#logging settings
LOG_EVERY_K_ITERATIONS = 50 #0 to disable logging
LOG_MEMORY_EVERY_K_ITERATIONS = False
LOG_MEMORY_EVERY_EPOCH = True
LOG_EPOCH_TIME = True
LOG_VALIDATION_TIME = True
LOG_HAUSDORFF_EVERY_K_EPOCHS = 0 #must be a multiple of VALIDATE_EVERY_K_EPOCHS
LOG_COMETML = False
LOG_PARAMCOUNT = True
LOG_LR_EVERY_EPOCH = True

#data and augmentation
TRAIN_ORIGINAL_CLASSES = False #train on original 5 classes
DATASET_WORKERS = 1
SOFT_AUGMENTATION = False #Soft augmetation directly works on the 3 classes. Hard augmentation augments on the 5 orignal labels, then takes the argmax
NN_AUGMENTATION = True #Has priority over soft/hard augmentation. Uses nearest-neighbor interpolation
DO_ROTATE = True
DO_SCALE = True
DO_FLIP = True
DO_ELASTIC_AUG = True
DO_INTENSITY_SHIFT = True
#RANDOM_CROP = [128, 128, 128]

ROT_DEGREES = 15
SCALE_FACTOR = 1.5
SIGMA = 10
MAX_INTENSITY_SHIFT = 0.1
topK=0.3
if LOG_COMETML:
    if not "LOG_COMETML_EXISTING_EXPERIMENT" in locals():
        experiment = Experiment(api_key="", project_name="", workspace="")
    else:
        experiment = ExistingExperiment(api_key="", previous_experiment=LOG_COMETML_EXISTING_EXPERIMENT, project_name="", workspace="")
else:
    experiment = None

#network funcitons
if TRAIN_ORIGINAL_CLASSES:
    loss = bratsUtils.bratsDiceLossOriginal5
else:
    #loss = bratsUtils.bratsDiceLoss
    if TRAIN_ORIGINAL_CLASSES:
        loss = bratsUtils.bratsDiceLossOriginal5
    else:
        def loss(outputs, labels):
            f = torch.nn.BCELoss(reduction='none')
            floss = f(outputs,labels)
            dloss = bratsUtils.bratsDiceLoss(outputs, labels, nonSquared=True)
            num_voxels = np.prod(floss.shape)
            res1, _ = torch.topk(floss.view((-1, )), int(num_voxels * topK), sorted=False)
            num_voxels = np.prod(dloss.shape)
            res2, _ = torch.topk(dloss.view((-1, )), int(num_voxels * topK), sorted=False)#for batch_size >1
            if len(res2)==0:
                return floss.mean()+dloss.mean()+res1.mean()
            else:
                return floss.mean()+dloss.mean()+res1.mean()+res2.mean()


class ResidualInner(nn.Module):
    def __init__(self, channels, groups,dia):
        super(ResidualInner, self).__init__()
        self.gn = nn.GroupNorm(groups, channels)
        if dia:
            dialation = 2
            pad = 2
        else:
            dialation = 1
            pad = 1
        self.conv = nn.Conv3d(channels, channels, 3, padding=pad, bias=False,dilation=dialation)

    def forward(self, x):
        x = self.conv(F.leaky_relu(self.gn(x), inplace=INPLACE))
        return x

def makeReversibleSequence(channels,dia):
    innerChannels = channels // 2
    groups = CHANNELS[0] // 2
    fBlock = ResidualInner(innerChannels, groups,dia)
    gBlock = ResidualInner(innerChannels, groups,dia)
    #gBlock = nn.Sequential()
    return rv.ReversibleBlock(fBlock, gBlock)

def makeReversibleComponent(channels, blockCount,dia):
    modules = []
    for i in range(blockCount):
        modules.append(makeReversibleSequence(channels,dia))
    return rv.ReversibleSequence(nn.ModuleList(modules))

def getChannelsAtIndex(index):
    if index < 0: index = 0
    if index >= len(CHANNELS): index = len(CHANNELS) - 1
    return CHANNELS[index]

class EncoderModule(nn.Module):
    def __init__(self, inChannels, outChannels, depth, downsample=True,dia=True):
        super(EncoderModule, self).__init__()
        self.downsample = downsample
        if downsample:
            self.conv = nn.Conv3d(inChannels, outChannels, 1)
        self.reversibleBlocks = makeReversibleComponent(outChannels, depth,dia)

    def forward(self, x):
        if self.downsample:
            x = F.avg_pool3d(x, 2)
            x = self.conv(x) #increase number of channels
        x = self.reversibleBlocks(x)
        return x

class DecoderModule(nn.Module):
    def __init__(self, inChannels, outChannels, depth, upsample=True,dia=True):
        super(DecoderModule, self).__init__()
        self.reversibleBlocks = makeReversibleComponent(inChannels, depth,dia)
        self.upsample = upsample
        if self.upsample:
            self.conv = nn.Conv3d(inChannels, outChannels, 1)

    def forward(self, x):
        x = self.reversibleBlocks(x)
        if self.upsample:
            x = self.conv(x)
            x = F.interpolate(x, scale_factor=2, mode="trilinear", align_corners=False)
        return x

class NoNewReversible(nn.Module):
    def __init__(self):
        super(NoNewReversible, self).__init__()
        encoderDepth = encDepth
        decoderDepth = 1
        self.levels = 5

        self.firstConv = nn.Conv3d(1, CHANNELS[0], 3, padding=1, bias=False)
        #self.dropout = nn.Dropout3d(0.2, True)
        self.lastConv = nn.Conv3d(CHANNELS[0], 2, 1, bias=True)

        #create encoder levels
        encoderModules = []
        for i in range(self.levels):
            encoderModules.append(EncoderModule(getChannelsAtIndex(i - 1), getChannelsAtIndex(i), encoderDepth, i != 0,i <self.levels-2))
        self.encoders = nn.ModuleList(encoderModules)

        #create decoder levels
        decoderModules = []
        for i in range(self.levels):
            decoderModules.append(DecoderModule(getChannelsAtIndex(self.levels - i - 1), getChannelsAtIndex(self.levels - i - 2), decoderDepth, i != (self.levels -1),i>1))
        self.decoders = nn.ModuleList(decoderModules)

    def forward(self, x):
        x = self.firstConv(x)
        #x = self.dropout(x)

        inputStack = []
        for i in range(self.levels):
            x = self.encoders[i](x)
            if i < self.levels - 1:
                inputStack.append(x)

        for i in range(self.levels):
            x = self.decoders[i](x)
            if i < self.levels - 1:
                x = x + inputStack.pop()

        x = self.lastConv(x)
        x = torch.sigmoid(x)
        return x

net = NoNewReversible()

optimizer = optim.Adam(net.parameters(), lr=INITIAL_LR, weight_decay=L2_REGULARIZER)
lr_sheudler = optim.lr_scheduler.ReduceLROnPlateau(optimizer,mode='max', factor=0.5, threshold=0.0001,patience=10,min_lr=1e-7)