from __future__ import division

import onmt
import onmt.Markdown
import onmt.Models
import onmt.modules
import argparse
import torch
import torch.nn as nn
from torch import cuda
import dill
from train_opts import add_model_arguments, add_optim_arguments

parser = argparse.ArgumentParser(description='train.py')
onmt.Markdown.add_md_help_argument(parser)

# Data options

parser.add_argument('-data', required=True,
                    help='Path to the *-train.pt file from preprocess.py')
parser.add_argument('-save_model', default='model',
                    help="""Model filename (the model will be saved as
                    <save_model>_epochN_PPL.pt where PPL is the
                    validation perplexity""")
parser.add_argument('-train_from_state_dict', default='', type=str,
                    help="""If training from a checkpoint then this is the
                    path to the pretrained model's state_dict.""")
# parser.add_argument('-train_from', default='', type=str,
#                     help="""If training from a checkpoint then this is the
#                     path to the pretrained model.""")

# pretrained word vectors
parser.add_argument('-pre_word_vecs_enc',
                    help="""If a valid path is specified, then this will load
                    pretrained word embeddings on the encoder side.
                    See README for specific formatting instructions.""")
parser.add_argument('-pre_word_vecs_dec',
                    help="""If a valid path is specified, then this will load
                    pretrained word embeddings on the decoder side.
                    See README for specific formatting instructions.""")

# GPU
parser.add_argument('-gpus', default=[], nargs='+', type=int,
                    help="Use CUDA on the listed devices.")

parser.add_argument('-log_interval', type=int, default=50,
                    help="Print stats at this interval.")
parser.add_argument('-log_server', type=str, default="",
                    help="Send logs to this crayon server.")
parser.add_argument('-experiment_name', type=str, default="",
                    help="Name of the experiment for logging.")

parser.add_argument('-seed', type=int, default=-1,
                    help="""Random seed used for the experiments
                    reproducibility.""")

add_model_arguments(parser)
add_optim_arguments(parser)

opt = parser.parse_args()
print(opt)

if opt.seed > 0:
    torch.manual_seed(opt.seed)

if torch.cuda.is_available() and not opt.gpus:
    print("WARNING: You have a CUDA device, should run with -gpus 0")

if opt.gpus:
    cuda.set_device(opt.gpus[0])
    if opt.seed > 0:
        torch.cuda.manual_seed(opt.seed)


# Set up the Crayon logging server.
if opt.log_server != "":
    from pycrayon import CrayonClient
    cc = CrayonClient(hostname=opt.log_server)

    experiments = cc.get_experiment_names()
    print(experiments)
    if opt.experiment_name in experiments:
        cc.remove_experiment(opt.experiment_name)
    experiment = cc.create_experiment(opt.experiment_name)


def make_features(batch, fields):
    feats = []
    for j in range(100):
        key = "src_feats_" + str(j)
        if key not in fields:
            break
        feats.append(batch.__dict__[key])
    cat = [batch.src[0]] + feats
    cat = [c.unsqueeze(2) for c in cat]
    return torch.cat(cat, 2)


def eval(model, criterion, data, fields):
    validData = onmt.IO.OrderedIterator(
        dataset=data, device=opt.gpus if opt.gpus else -1,
        batch_size=opt.batch_size, train=False, sort=True)

    stats = onmt.Statistics()
    model.eval()
    loss_compute = LossCompute(model.generator, criterion,
                               fields["tgt"].vocab)

    for batch in validData:
        _, src_lengths = batch.src
        src = make_features(batch, fields)
        outputs, attn, _ = model(src, batch.tgt, src_lengths)
        gen_state = loss_compute.makeLossBatch(outputs, batch, attn,
                                               (0, batch.tgt.size(0)))
        _, batch_stats = loss_compute.computeLoss(**gen_state)
        stats.update(batch_stats)
        break
    model.train()
    return stats


class LossCompute:
    def __init__(self, generator, crit, tgt_vocab):
        self.generator = generator
        self.crit = crit
        self.tgt_vocab = tgt_vocab

    @staticmethod
    def makeLossBatch(outputs, batch, attns, range_):
        return {"out": outputs,
                "target": batch.tgt[range_[0] + 1: range_[1]],
                "align": batch.alignment[range_[0] + 1: range_[1]],
                "coverage": attns.get("coverage"),
                "attn": attns.get("copy")}

    def computeLoss(self, out, target, attn=None, align=None, coverage=None):
        def bottle(v):
            return v.view(-1, v.size(2))

        if not opt.copy_attn:
            # Standard loss.
            scores = self.generator(bottle(out))
            loss = self.crit(scores, target.view(-1))
        else:
            # Need extra args for copy.
            scores, c_attn = self.generator(bottle(out), bottle(attn))
            loss = self.crit(scores, c_attn, target, bottle(align))

        # Coverage can be applied for either.
        if opt.coverage_attn:
            loss += opt.lambda_coverage * \
                    torch.min(coverage, attn).sum()

        stats = onmt.Statistics.score(loss, scores.data, target.data,
                                      self.tgt_vocab.stoi[onmt.IO.PAD_WORD])
        return loss, stats


def trainModel(model, criterion, trainData, validData, fields, optim):
    def trainEpoch(epoch):
        # if opt.extra_shuffle and epoch > opt.curriculum:
        #     trainData.shuffle()

        model.train()
        loss_compute = LossCompute(model.generator, criterion,
                                   fields["tgt"].vocab)
        splitter = onmt.modules.Splitter(opt.max_generator_batches)

        train = onmt.IO.OrderedIterator(
            dataset=trainData, batch_size=opt.batch_size,
            sort=True,
            device=opt.gpus if opt.gpus else -1)

        total_stats = onmt.Statistics()
        report_stats = onmt.Statistics()

        for i, batch in enumerate(train):
            target_size = batch.tgt.size(0)
            dec_state = None
            trunc_size = opt.truncated_decoder if opt.truncated_decoder \
                else target_size

            for j in range(0, target_size-1, trunc_size):
                # Main training loop
                _, src_lengths = batch.src
                src = make_features(batch, fields)
                tgt_r = (j, j + trunc_size)

                model.zero_grad()
                outputs, attn, dec_state = \
                    model(src, batch.tgt[tgt_r[0]: tgt_r[1]],
                          src_lengths, dec_state)

                gen_state = loss_compute.makeLossBatch(outputs, batch, attn,
                                                       tgt_r)
                batch_stats = onmt.Statistics()
                for shard in splitter.splitIter(gen_state):
                    loss, stats = loss_compute.computeLoss(**shard)

                    # Compute statistics.
                    batch_stats.update(stats)
                    loss.div(batch.batch_size).backward()

                # Update the parameters.
                optim.step()
                total_stats.update(batch_stats)
                report_stats.update(batch_stats)

                # If truncated, don't backprop fully.
                if dec_state is not None:
                    dec_state.detach()

            report_stats.n_src_words += src_lengths.sum()

            if i % opt.log_interval == -1 % opt.log_interval:
                report_stats.output(epoch, i+1, len(trainData),
                                    total_stats.start_time)
                if opt.log_server:
                    report_stats.log("progress", experiment, optim)
                report_stats = onmt.Statistics()
        return total_stats

    for epoch in range(opt.start_epoch, opt.epochs + 1):
        print('')

        #  (1) train for one epoch on the training set
        train_stats = trainEpoch(epoch)
        print('Train perplexity: %g' % train_stats.ppl())
        print('Train accuracy: %g' % train_stats.accuracy())

        #  (2) evaluate on the validation set
        valid_stats = eval(model, criterion, validData, fields)
        print('Validation perplexity: %g' % valid_stats.ppl())
        print('Validation accuracy: %g' % valid_stats.accuracy())

        # Log to remote server.
        if opt.log_server:
            train_stats.log("train", experiment, optim)
            valid_stats.log("valid", experiment, optim)

        #  (3) update the learning rate
        optim.updateLearningRate(valid_stats.ppl(), epoch)

        model_state_dict = (model.module.state_dict() if len(opt.gpus) > 1
                            else model.state_dict())
        model_state_dict = {k: v for k, v in model_state_dict.items()
                            if 'generator' not in k}
        generator_state_dict = (model.generator.module.state_dict()
                                if len(opt.gpus) > 1
                                else model.generator.state_dict())
        #  (4) drop a checkpoint
        if epoch >= opt.start_checkpoint_at:
            checkpoint = {
                'model': model_state_dict,
                'generator': generator_state_dict,
                'fields': fields,
                'opt': opt,
                'epoch': epoch,
                'optim': optim
            }
            torch.save(checkpoint,
                       '%s_acc_%.2f_ppl_%.2f_e%d.pt'
                       % (opt.save_model, valid_stats.accuracy(),
                          valid_stats.ppl(), epoch), pickle_module=dill)


def main():
    train = torch.load(opt.data + '.train.pt', pickle_module=dill)
    fields = torch.load(opt.data + '.fields.pt', pickle_module=dill)
    valid = torch.load(opt.data + '.valid.pt', pickle_module=dill)
    fields = dict(fields)
    src_features = [fields["src_feats_"+str(j)]
                    for j in range(train.nfeatures)]

    checkpoint = None
    dict_checkpoint = opt.train_from_state_dict
    if dict_checkpoint:
        print('Loading dicts from checkpoint at %s' % dict_checkpoint)
        checkpoint = torch.load(dict_checkpoint)
        fields = checkpoint['fields']

    print(' * vocabulary size. source = %d; target = %d' %
          (len(fields['src'].vocab), len(fields['tgt'].vocab)))
    for j, feat in enumerate(src_features):
        print(' * src feature %d size = %d' %
              (j, len(feat.vocab)))
    print(' * number of training sentences. %d' %
          len(train))
    print(' * maximum batch size. %d' % opt.batch_size)
    print('Building model...')

    cuda = (len(opt.gpus) >= 1)
    model = onmt.Models.make_base_model(opt, opt, fields, cuda, checkpoint)

    # Define criterion of each GPU.
    vocabSize = len(fields['tgt'].vocab)
    if not opt.copy_attn:
        weight = torch.ones(vocabSize)
        weight[fields['tgt'].vocab.stoi[onmt.IO.PAD_WORD]] = 0
        criterion = nn.NLLLoss(weight, size_average=False)
    else:
        criterion = onmt.modules.CopyCriterion
    if cuda:
        criterion.cuda()
    else:
        criterion.cpu()

    # Multi-gpu
    if len(opt.gpus) > 1:
        print('Multi gpu training ', opt.gpus)
        model = nn.DataParallel(model, device_ids=opt.gpus, dim=1)
        model.generator = nn.DataParallel(model.generator, device_ids=opt.gpus, dim=0)

    if not opt.train_from_state_dict:
        if opt.param_init != 0.0:
            print('Intializing params')
            for p in model.parameters():
                p.data.uniform_(-opt.param_init, opt.param_init)

        model.encoder.embeddings.load_pretrained_vectors(opt.pre_word_vecs_enc)
        model.decoder.embeddings.load_pretrained_vectors(opt.pre_word_vecs_dec)

        optim = onmt.Optim(
            opt.optim, opt.learning_rate, opt.max_grad_norm,
            lr_decay=opt.learning_rate_decay,
            start_decay_at=opt.start_decay_at,
            opt=opt
        )
    else:
        print('Loading optimizer from checkpoint:')
        optim = checkpoint['optim']
        print(optim)

    optim.set_parameters(model.parameters())

    if opt.train_from_state_dict:
        optim.optimizer.load_state_dict(
            checkpoint['optim'].optimizer.state_dict())

    nParams = sum([p.nelement() for p in model.parameters()])
    print('* number of parameters: %d' % nParams)

    trainModel(model, criterion, train, valid, fields, optim)


if __name__ == "__main__":
    main()
