#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Candidate tree rankers.

"""
from __future__ import unicode_literals
import numpy as np
import cPickle as pickle
import random
import time
import datetime

from alex.components.nlg.tectotpl.core.util import file_stream

from ml import DictVectorizer, StandardScaler
from logf import log_info, log_debug
from features import Features
from futil import read_das, read_ttrees, trees_from_doc, sentences_from_doc
from planner import SamplingPlanner, ASearchPlanner
from candgen import RandomCandidateGenerator
from eval import Evaluator, EvalTypes
from tree import TreeNode
from tgen.eval import ASearchListsAnalyzer


class Ranker(object):

    @staticmethod
    def load_from_file(model_fname):
        """Load a pre-trained model from a file."""
        log_info("Loading ranker from %s..." % model_fname)
        with file_stream(model_fname, 'rb', encoding=None) as fh:
            return pickle.load(fh)

    def save_to_file(self, model_fname):
        """Save the model to a file."""
        log_info("Saving ranker to %s..." % model_fname)
        with file_stream(model_fname, 'wb', encoding=None) as fh:
            pickle.dump(self, fh, protocol=pickle.HIGHEST_PROTOCOL)


class PerceptronRanker(Ranker):
    """Global ranker for whole trees, based on linear Perceptron by Collins & Duffy (2002)."""

    def __init__(self, cfg):
        if not cfg:
            cfg = {}
        self.w = None
        self.feats = ['bias: bias']
        self.vectorizer = None
        self.normalizer = None
        self.alpha = cfg.get('alpha', 1)
        self.passes = cfg.get('passes', 5)
        self.rival_number = cfg.get('rival_number', 10)
        self.language = cfg.get('language', 'en')
        self.selector = cfg.get('selector', '')
        self.rival_gen_strategy = cfg.get('rival_gen_strategy', ['other_inst'])
        self.rival_gen_max_iter = cfg.get('rival_gen_max_iter', 50)
        self.rival_gen_max_defic_iter = cfg.get('rival_gen_max_defic_iter', 3)
        self.rival_gen_beam_size = cfg.get('rival_gen_beam_size')
        self.candgen_model = cfg.get('candgen_model')
        # initialize feature functions
        if 'features' in cfg:
            self.feats.extend(cfg['features'])
        self.feats = Features(self.feats)

    def score(self, cand_ttree, da):
        return self._score(self._extract_feats(cand_ttree, da))

    def _score(self, cand_feats):
        ret = np.dot(self.w, cand_feats)
        log_debug('SCORING: ' + str(hash(tuple(self.w))) + ' * ' + str(hash(tuple(cand_feats))) + ' = ' + str(ret))
        return ret

    def _extract_feats(self, ttree, da):
        return self.normalizer.transform(
                        self.vectorizer.transform(
                                [self.feats.get_features(ttree, {'da': da})]))[0]

    def train(self, das_file, ttree_file, data_portion=1.0):
        """Run training on the given training data."""
        self._init_training(das_file, ttree_file, data_portion)
        for iter_no in xrange(1, self.passes + 1):
            self._training_iter(iter_no)

    def _init_training(self, das_file, ttree_file, data_portion):
        """Initialize training (read input files, reset weights, reset
        training data features.)"""
        # read input
        log_info('Reading DAs from ' + das_file + '...')
        das = read_das(das_file)
        log_info('Reading t-trees from ' + ttree_file + '...')
        ttree_doc = read_ttrees(ttree_file)
        self.train_sents = sentences_from_doc(ttree_doc, self.language, self.selector)
        ttrees = trees_from_doc(ttree_doc, self.language, self.selector)

        # make training data smaller if necessary
        train_size = int(round(data_portion * len(ttrees)))
        self.train_trees = ttrees[:train_size]
        self.train_das = das[:train_size]
        log_info('Using %d training instances.' % train_size)

        # precompute training data features
        X = []
        for da, tree in zip(self.train_das, self.train_trees):
            X.append(self.feats.get_features(tree, {'da': da}))
        # vectorize and normalize (+train normalizer and vectorizer)
        self.vectorizer = DictVectorizer(sparse=False)
        self.normalizer = StandardScaler(copy=False)
        self.train_feats = self.normalizer.fit_transform(self.vectorizer.fit_transform(X))

        # initialize candidate generator + planner if needed
        if self.candgen_model is not None:
            self.candgen = RandomCandidateGenerator({})
            self.candgen.load_model(self.candgen_model)
            self.sampling_planner = SamplingPlanner({'langugage': self.language,
                                                     'selector': self.selector,
                                                     'candgen': self.candgen})
        if 'gen_cur_weights' in self.rival_gen_strategy:
            assert self.candgen is not None
            self.asearch_planner = ASearchPlanner({'candgen': self.candgen,
                                                   'language': self.language,
                                                   'selector': self.selector,
                                                   'ranker': self, })

        # initialize diagnostics
        self.lists_analyzer = ASearchListsAnalyzer()
        self.evaluator = Evaluator()

        # initialize weights
        self.w = np.ones(self.train_feats.shape[1])

        log_debug('\n***\nINIT:')
        log_debug(self._feat_val_str(self.w))
        log_info('Training ...')

    def _training_iter(self, iter_no):

        iter_start_time = time.clock()
        iter_errs = 0
        self.evaluator.reset()
        self.lists_analyzer.reset()

        log_debug('\n***\nTR %05d:' % iter_no)

        for tree_no in xrange(len(self.train_trees)):
            # obtain some 'rival', alternative incorrect candidates
            gold_ttree, gold_feats = self.train_trees[tree_no], self.train_feats[tree_no]
            rival_ttrees, rival_feats = self._get_rival_candidates(tree_no)
            cands = [gold_feats] + rival_feats

            # score them along with the right one
            scores = [self._score(cand) for cand in cands]
            top_cand_idx = scores.index(max(scores))

            # find the top-scoring generated tree, evaluate F-score against gold t-tree
            # (disregarding whether it was selected as the best one)
            self.evaluator.append(TreeNode(gold_ttree),
                                  TreeNode(rival_ttrees[scores[1:].index(max(scores[1:]))]))

            log_debug('TTREE-NO: %04d, SEL_CAND: %04d, LEN: %02d' % (tree_no, top_cand_idx, len(cands)))
            log_debug('SENT: %s' % self.train_sents[tree_no])
            log_debug('ALL CAND TREES:')
            for ttree, score in zip([gold_ttree] + rival_ttrees, scores):
                log_debug("%.3f" % score, "\t", ttree)

            # update weights if the system doesn't give the highest score to the right one
            if top_cand_idx != 0:
                log_info('UPDATING WEIGHTS: ' + str(hash(tuple(self.w))))
                self.w += (self.alpha * gold_feats -
                           self.alpha * cands[top_cand_idx])
                log_debug('UPDATED  WEIGHTS: ' + str(hash(tuple(self.w))))
                iter_errs += 1
                log_debug('ITER ERRS: %d' % iter_errs)
            else:
                log_debug('NO WEIGHTS UPDATE.')

        iter_acc = (1.0 - (iter_errs / float(len(self.train_trees))))
        log_debug(self._feat_val_str(self.w), '\n***')
        log_debug('ITER ACCURACY: %.3f' % iter_acc)

        iter_end_time = time.clock()

        log_info('Iteration %05d -- tree-level accuracy: %.4f' % (iter_no, iter_acc))
        log_info(' * Generated trees NODE scores: P: %.4f, R: %.4f, F: %.4f' %
                 self.evaluator.p_r_f1())
        log_info(' * Generated trees DEP  scores: P: %.4f, R: %.4f, F: %.4f' %
                 self.evaluator.p_r_f1(EvalTypes.DEP))
        log_info(' * Gold tree BEST: %.4f, on CLOSE: %.4f, on ANY list: %4f' %
                 self.lists_analyzer.stats())
        log_info(' * Duration: %s' % str(datetime.timedelta(seconds=(iter_end_time - iter_start_time))))

    def _feat_val_str(self, vec, sep='\n', nonzero=False):
        return sep.join(['%s: %.3f' % (name, weight)
                         for name, weight in zip(self.vectorizer.get_feature_names(), vec)
                         if not nonzero or weight != 0])

    def _get_rival_candidates(self, tree_no):
        """Generate some rival candidates for a DA and the correct (gold) t-tree,
        given the current rival generation strategy (self.rival_gen_strategy).

        TODO: checking for trees identical to the gold one slows down the process

        @param tree_no: the index of the current training data item (tree, DA)
        @rtype: tuple
        @return: an array of rival t-trees and an array of the corresponding features
        """
        da = self.train_das[tree_no]
        train_trees = self.train_trees

        rival_trees, rival_feats = [], []

        # use current DA but change trees when computing features
        if 'other_inst' in self.rival_gen_strategy:
            # use alternative indexes, avoid the correct one
            rival_idxs = map(lambda idx: len(train_trees) - 1 if idx == tree_no else idx,
                             random.sample(xrange(len(train_trees) - 1), self.rival_number))
            other_inst_trees = [train_trees[rival_idx] for rival_idx in rival_idxs]
            rival_trees.extend(other_inst_trees)
            rival_feats.extend([self._extract_feats(tree, da) for tree in other_inst_trees])

        # candidates generated using the random planner (use the current DA)
        if 'random' in self.rival_gen_strategy:
            random_trees = []
            while len(random_trees) < self.rival_number:
                tree = self.sampling_planner.generate_tree(da)
                if (tree != train_trees[tree_no]):  # don't generate trees identical to the gold one
                    random_trees.append(tree)
            rival_trees.extend(random_trees)
            rival_feats.extend([self._extract_feats(tree, da) for tree in random_trees])

        # candidates generated using the A*search planner, which uses this ranker with current
        # weights to guide the search, and the current DA as the input
        # TODO: use just one!, others are meaningless
        if 'gen_cur_weights' in self.rival_gen_strategy:
            open_list, close_list = self.asearch_planner.run(da,
                                                             self.rival_gen_max_iter,
                                                             self.rival_gen_max_defic_iter,
                                                             self.rival_gen_beam_size)
            self.lists_analyzer.append(train_trees[tree_no], open_list, close_list)
            gen_trees = []
            while close_list and len(gen_trees) < self.rival_number:
                tree = close_list.pop()[0]
                if tree != train_trees[tree_no]:
                    gen_trees.append(tree)
            rival_trees.extend(gen_trees[:self.rival_number])
            rival_feats.extend([self._extract_feats(tree, da)
                                for tree in gen_trees[:self.rival_number]])

        # return all resulting candidates
        return rival_trees, rival_feats
