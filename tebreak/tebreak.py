#!/usr/bin/env python
 
import os
import gc
import re
import sys
import time
import shutil
import random
import argparse
import subprocess
import itertools
import traceback
import pickle
import multiprocessing as mp

import pysam

import numpy as np
#np.seterr(all='raise')
import scipy.stats as ss

import skbio.alignment as skalign
import skbio.sequence as skseq
 
from uuid import uuid4
from operator import itemgetter
from collections import Counter
from collections import OrderedDict as od
from collections import defaultdict as dd
from bx.intervals.intersection import Intersecter, Interval # pip install bx-python

import logging
FORMAT = '%(asctime)s %(message)s'
logging.basicConfig(format=FORMAT)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


#######################################
## Classes                           ##
#######################################


class Genome:
    def __init__(self, gfn, skip_chroms):
        ''' gfn = genome file name (.fai or chrom, length tsv) '''
        self.chrlen = {} # length of each chromosome
 
        self.bp = 0
 
        with open(gfn, 'r') as g:
            for line in g:
                if not line.startswith('#'):
                    chrom, length = line.strip().split()[:2]
                    if chrom not in skip_chroms:
                        self.chrlen[chrom] = int(length)
                        self.bp += int(length)

 
    def addpad(self, interval, pad):
        ''' pad interval such that it doesn't go out of bounds '''
        chrom, start, end = interval
        start = int(start) - int(pad)
        end   = int(end) + int(pad)

        assert chrom in self.chrlen, "error padding interval %s, %s not a known chromosome" % (str(interval), chrom)

        if start < 0: start = 0
        if end > self.chrlen[chrom]: end = self.chrlen[chrom]

        return (chrom, start, end)


    def chunk(self, n, seed=1, sorted=False, pad=0, flatten=True):
        ''' break genome into n evenly-sized chunks, return n lists of (chrom, start, end) '''
        chunklen = int(self.bp/n)
        
        chunks = []
        intervals = []
 
        chunkleft = chunklen # track how much genome needs to go into each chunk
 
        chromlist = list(self.chrlen.keys())
 
        random.seed(seed)

        if sorted:
            chromlist.sort()

 
        for chrom in chromlist:
            length = self.chrlen[chrom]
 
            lenleft = length
            if length <= chunkleft:
                chunkleft -= length
                lenleft -= length
                intervals.append( self.addpad((chrom, 0, length), pad) )
                assert lenleft == 0
 
                if chunkleft == 0:
                    chunkleft = chunklen
                    chunks.append(intervals)
                    intervals = []
            else:
                while lenleft > 0:
                    if lenleft >= chunkleft:
                        intervals.append( self.addpad((chrom, length-lenleft, length-lenleft+chunkleft), pad) )
                        lenleft -= chunkleft
 
                        chunkleft = chunklen
                        chunks.append(intervals)
                        intervals = []
 
                    else: # lenleft < chunkleft
                        intervals.append( self.addpad((chrom, length-lenleft, length), pad) )
                        chunkleft -= lenleft
                        lenleft -= lenleft

        if flatten: 
            return list(itertools.chain.from_iterable(chunks)) # flatten list

        return chunks


class LASTResult:
    def __init__(self, res):
        self.raw = res
        self.score = int(res[0].split()[1].replace('score=', ''))

        self.target_id      = res[1].split()[1]
        self.target_start   = int(res[1].split()[2])
        self.target_alnsize = int(res[1].split()[3])
        self.target_end     = self.target_start + self.target_alnsize
        self.target_strand  = res[1].split()[4]
        self.target_seqsize = int(res[1].split()[5])
        self.target_align   = res[1].split()[6]

        self.query_id = res[2].split()[1]
        self.query_distnum = 0
        if '|' in self.query_id:
            self.query_id = res[2].split()[1].split('|')[0]
            self.query_distnum = int(res[2].split()[1].split('|')[-1]) # track which distal read this came from

        self.query_start   = int(res[2].split()[2])
        self.query_alnsize = int(res[2].split()[3])
        self.query_end     = self.query_start + self.query_alnsize
        self.query_strand  = res[2].split()[4]
        self.query_seqsize = int(res[2].split()[5])
        self.query_align   = res[2].split()[6]

    def pct_match(self):
        return float(sum([a.upper()==b.upper() for a,b in zip(list(self.query_align), list(self.target_align))])) / float(self.query_alnsize)

    def only_polyA(self):
        ''' guess if alignment corresponds to only polyA sequence (>95 pct A or T)'''
        a = [b for b in list(self.query_align) if b in ('A', 'T')]
        return len(a)/float(len(self.query_align)) > 0.95

    def __lt__(self, other):
        return self.score > other.score

    def __gt__(self, other):
        return self.score < other.score

    def __str__(self):
        return "\n".join(self.raw)


class SortableRead:
    def __init__(self, read):
        self.read = read
        self.seq  = read.seq
        self.seqstart = read.reference_start-read.query_alignment_start

    def __gt__(self, other):
        if self.read.tid == other.read.tid:
            return self.seqstart > other.seqstart
        else:
            return self.read.tid > other.read.tid

 
class SplitRead:
    ''' store information about split read alignment '''
    def __init__(self, chrom, read, bamfn, minqual):
        self.uuid  = str(uuid4())
        self.chrom = chrom
        self.read  = read
        self.bamfn = os.path.basename(bamfn)
        self.minqual = minqual

        self.cliplen = len(read.seq) - len(read.query_alignment_sequence)

        self.breakleft  = False
        self.breakright = False
        self.breakpos   = None
 
        if read.qstart < read.rlen - read.qend:
            self.breakpos   = read.get_reference_positions()[-1] # breakpoint on right
            self.breakright = True
 
        else:
            self.breakpos  = read.get_reference_positions()[0] # breakpoint on left
            self.breakleft = True
 
        assert self.breakpos is not None
        assert self.breakleft != self.breakright
 
    def getRG(self):
        ''' return read group from RG aux tag '''
        for tag, val in self.read.tags:
            if tag == 'RG': return val
        return None

    def __gt__(self, other):
        ''' enables sorting of SplitRead objects '''
        if self.chrom == other.chrom:
            return self.breakpos > other.breakpos
        else:
            return self.chrom > other.chrom
 
    def __str__(self):
        dir = 'left'
        if self.breakright: dir='right'
        return ' '.join(map(str, ('SplitRead:', self.chrom, self.breakpos, self.cliplen, self.read.qname, dir)))
 
 
class DiscoRead:
    ''' store information about discordant pair alignment '''
    def __init__(self, chrom, read, bamfn, mate_chrom=None):
        self.chrom = chrom
        self.read  = read
        self.bamfn = os.path.basename(bamfn)

        self.mate_chrom = mate_chrom # can be None
        self.mate_read  = None  # set later
 
    def mate_mapped(self):
        return self.mate_read is not None and not self.mate_read.is_unmapped

    def getRG(self):
        ''' return read group from RG aux tag '''
        for tag, val in self.read.tags:
            if tag == 'RG': return val
        return None

    def __gt__(self, other):
        if self.mate_mapped() and other.mate_mapped():
            return self.mate_read.get_reference_positions()[0] > other.mate_read.get_reference_positions()[0]
        else:
            return self.read.get_reference_positions()[0] > other.read.get_reference_positions()[0]

    def __str__(self):
        return  ' '.join(map(str, (self.chrom, self.read, self.mate_chrom, self.mate_start)))


class ReadCluster:
    ''' parent class for read clusters '''
    def __init__(self, firstread=None):
        self.uuid   = str(uuid4())
        self.reads  = []
        self.start  = 0
        self.end    = 0
        self.median = 0
        self.chrom  = None

        if firstread is not None:
            self.add_read(firstread)

    def add_read(self, r):
        ''' add a read and update '''
        self.reads.append(r)
 
        if self.chrom is None: self.chrom = r.chrom
 
        assert self.chrom == r.chrom # clusters can't include > 1 chromosome
 
        ''' update statistics '''
        positions = []
        positions += [pos for r in self.reads for pos in r.read.positions]

        self.reads.sort()
        self.start  = max(positions)
        self.end    = min(positions)
        self.median = int(np.median(positions))

    def readgroups(self):
        c = Counter([r.getRG() for r in self.reads])
        return [str(k[0]) + '|' + str(k[1]) for k in zip(c.keys(), c.values())]

    def bamfiles(self):
        c = Counter([r.bamfn for r in self.reads])
        return [str(k[0]) + '|' + str(k[1]) for k in zip(c.keys(), c.values())]

    def find_extrema(self):
        ''' return leftmost and rightmost aligned positions in cluster vs. reference '''
        positions = []
        positions += [pos for r in self.reads for pos in r.read.positions]
        return min(positions), max(positions)
 
    def avg_matchpct(self):
        return np.mean([read_matchpct(r.read) for r in self.reads])
 
    def __len__(self):
        return len(self.reads)


class SplitCluster(ReadCluster):
    ''' store and manipulate groups of SplitRead objects '''

    def add_splitread(self, sr):
        ''' add a SplitRead and update '''
        self.reads.append(sr)
 
        if self.chrom is None: self.chrom = sr.chrom
 
        assert self.chrom == sr.chrom # clusters can't include > 1 chromosome
 
        ''' update statistics '''
        self.reads.sort()
        self.start  = self.reads[0].breakpos
        self.end    = self.reads[-1].breakpos
        self.median = self.reads[int(len(self)/2)].breakpos
 
    def subcluster_by_breakend(self, breakends, direction='both'):
        ''' return a new cluster containing only reads with breakpoints in passed list '''
        new = SplitCluster()
        assert direction in ('both', 'left', 'right')
        
        if direction == 'both':
            [new.add_splitread(sr) for sr in self.reads if sr.breakpos in breakends]
 
        if direction == 'left':
            [new.add_splitread(sr) for sr in self.reads if sr.breakpos in breakends and sr.breakleft]
 
        if direction == 'right':
            [new.add_splitread(sr) for sr in self.reads if sr.breakpos in breakends and sr.breakright]
 
        return new

    def consensus(self, minscore = 0.9, seed=1):
        ''' build consensus from sorted aligned reads iteratively '''

        np.random.seed(seed)

        minqual = self.reads[0].minqual

        sortable_reads = [SortableRead(sr.read) for sr in self.reads]
        seqs = [qualtrim(sorted_read.read, minqual=minqual) for sorted_read in sorted(sortable_reads)]
        seqs = [s for s in seqs if len(s) > 20]

        if len(seqs) == 0:
            return '', 0.0

        if len(seqs) == 1: # no consensus necessary
            return seqs[0], 1.0

        uniq_seqs = [seqs[0]]
        for i, seq in enumerate(seqs[1:], start=1):
            if seq != seqs[i-1]:
                uniq_seqs.append(seq)

        if len(uniq_seqs) == 1: # all seqs were the same!
            return uniq_seqs[0], 1.0

        cons = uniq_seqs[0]
        scores = []

        if len(uniq_seqs) > 1000:
            uniq_seqs = [uniq_seqs[u] for u in sorted(np.random.choice(range(len(uniq_seqs)), size=1000))]

        for seq in uniq_seqs[1:]:
            cons = cons.replace('N','A') # N not allowed in conservation calculation from scikit-bio
            seq = seq.replace('N','A')

            s1 = skseq.DNA(cons)
            s2 = skseq.DNA(seq)

            try:
                aln_res = skalign.local_pairwise_align_ssw(s1, s2)
            except IndexError: # scikit-bio throws this if no bases align  >:|
                return cons, 0.0
                
            aln_tab = aln_res[0]

            s1_aln, s2_aln = aln_res[2]

            a1 = cons[s1_aln[0]:s1_aln[1]+1]

            score = 0.0
            if aln_tab.shape.position > 10: # param?
                score = sum(aln_tab.conservation(gap_mode='include')==1.)/aln_tab.shape.position

            if re.search(a1, cons):
                cons_start, cons_end = s1_aln[0], s1_aln[1]+1

                if score >= minscore and cons_end > len(cons)-5:
                    scores.append(score)
                    align_end = s2_aln[1]+1
                    cons += seq[align_end:]

        if scores:
            return cons, np.mean(scores)

        else:
            return cons, 0.0

    def all_breakpoints(self):
        ''' returns uniquified list of breakpoints '''
        return list(set([read.breakpos for read in self.reads]))
 
    def median_D(self):
        return np.median([splitqual(sr.read) for sr in self.reads])

    def min_cliplen(self):
        return min([sr.cliplen for sr in self.reads])

    def max_cliplen(self):
        return max([sr.cliplen for sr in self.reads])
 
    def __str__(self):
        break_count = Counter([read.breakpos for read in self.reads])
        return '\t'.join(map(str, ('SplitCluster:', self.chrom, self.start, self.end, len(self.reads), break_count)))


class DiscoCluster(ReadCluster):
    ''' store and manipulate groups of DiscoRead objects '''

    def overlap_insertion(self, insertion):
        iv_ins = [insertion.min_supporting_base(), insertion.max_supporting_base()]
        iv_dsc = self.find_extrema()

        return min(iv_ins[1], iv_dsc[1]) - max(iv_ins[0], iv_dsc[0]) > 0

    def find_mate_extrema(self):
        ''' return leftmost and rightmost aligned positions in cluster vs. reference '''
        positions = []
        for r in self.reads:
            if r.mate_mapped():
                positions += [pos for pos in r.mate_read.positions]

        if len(positions) == 0: return -1, -1
        return min(positions), max(positions)

    def summary_tuple(self):
        return (self.reads[0].mate_chrom, self.find_mate_extrema()[0], self.find_mate_extrema()[1], self.readgroups(), self.bamfiles())

 
class BreakEnd:
    ''' coallate information about a breakend '''
    def __init__(self, chrom, breakpos, cluster, consensus, score, direction):
        self.uuid      = str(uuid4())
        self.cluster   = cluster
        self.chrom     = chrom
        self.breakpos  = breakpos
        self.consensus = consensus
        self.consscore = score
        self.direction = direction
 
        self.mappings = []
 
    def proximal_subread(self):
        ''' return mapping(s) containing breakpoint '''
        return [read for read in self.mappings if self.breakpos in read.get_reference_positions()]
 
    def distal_subread(self):
        ''' return mapping(s) not containing breakpoint '''
        return [read for read in self.mappings if not read.is_unmapped and self.breakpos not in read.get_reference_positions()]
 
    def unmapped_subread(self):
        ''' returns list of intervals and corresponding subseqs '''
        covered_bases = []
        for subseq in map(lambda x : x.query_alignment_sequence, self.mappings):
            subseq = orient_subseq(self.consensus, subseq)
            covered_bases += range(*locate_subseq(self.consensus, subseq))
 
        subseqs   = []
        intervals = []
 
        interval = []
        subseq   = []
 
        for p, b in enumerate(list(self.consensus)):
            if p not in covered_bases:
                if len(interval) > 0 and interval[-1]+1 < p:
                    intervals.append( (min(interval), max(interval)) )
                    subseqs.append(''.join(subseq))
                    interval = []
                    subseq   = []
 
                else:
                    interval.append(p)
                    subseq.append(b)
 
        if len(interval) > 0:
            intervals.append( (min(interval), max(interval)) )
            subseqs.append(''.join(subseq))
 
        return intervals, subseqs
 
    def __len__(self):
        return len(self.cluster)

    def __str__(self):
        return '%s:%d:%s:%s' % (self.chrom, self.breakpos, self.consensus, self.direction)

class Insertion:
    ''' store and compile information about an insertion with 1 or 2 breakpoints '''
    def __init__(self, be1=None, be2=None):
        self.uuid = str(uuid4())

        self.be1 = None
        self.be2 = None
 
        if be1 is not None:
            self.be1 = be1
        if be2 is not None:
            self.be2 = be2
 
        self.be1_alt = None
        self.be2_alt = None

        self.be1_improved_cons = False
        self.be2_improved_cons = False

        if self.paired():
            if self.be1.breakpos > self.be2.breakpos:
                self.be1, self.be2 = self.be2, self.be1 # keep breakends in position order

        self.info = od() # set with self.compile_info()
        self.discreads = []
        self.fastqrecs = []
        self.genotypes = []
        self.mappability = None

    def __len__(self):
        ''' return total number of reads (sr+dr) associated with insertion '''
        return self.num_sr() + len(self.discreads)

    def num_sr(self):
        ''' return number of split reads supporting insertion '''
        l = 0
        if self.be1 is not None: l += len(self.be1.cluster)
        if self.be2 is not None: l += len(self.be2.cluster)
        return l

    def paired(self):
        return None not in (self.be1, self.be2)
 
    def breakend_overlap(self):
        if not self.paired(): return None
        if len(self.be1.proximal_subread()) == 0 or len(self.be2.proximal_subread()) == 0: return None
        return ref_dist(self.be1.proximal_subread()[0], self.be2.proximal_subread()[0])
 
    def min_supporting_base(self):
        ''' return leftmost supporting reference position covered '''
        sites = []
        for be in (self.be1, self.be2):
            if be is not None:
                if len(be.proximal_subread()) > 0:
                    for proxread in be.proximal_subread(): 
                        sites += proxread.get_reference_positions()

        if len(sites) == 0:
            return None

        return min(sites)

    def max_supporting_base(self):
        ''' return rightmost supporting reference position covered '''
        sites = []
        for be in (self.be1, self.be2):
            if be is not None:
                if len(be.proximal_subread()) > 0:
                    for proxread in be.proximal_subread(): 
                        sites += proxread.get_reference_positions()
                    
        if len(sites) == 0:
            return None

        return max(sites)

    def tsd(self, be1_use_prox=0, be2_use_prox=0):
        ''' target site duplication '''
        if not self.paired():
            return None

        if self.breakend_overlap() is None:
            return None
 
        if self.breakend_overlap() > 0:
            return None

        else:
            if len(self.be1.proximal_subread()) == 0 or len(self.be2.proximal_subread()) == 0: return None

            junc1 = self.be1.proximal_subread()[be1_use_prox]
            junc2 = self.be2.proximal_subread()[be2_use_prox]
 
            tsd_ref_interval = ref_overlap(junc1, junc2)

            if tsd_ref_interval is None: return None

            tsd_ref_interval[1] += 1
 
            tsdseq1 = ''
            tsdseq2 = ''
 
            #for (qrypos, refpos) in junc1.get_aligned_pairs(): # broken by pysam 8.3
            for qrypos, refpos in enumerate(junc1.get_reference_positions()):
                if refpos in range(*tsd_ref_interval):
                    if qrypos is not None:
                        tsdseq1 += junc1.seq[qrypos+junc1.qstart]
 
            #for (qrypos, refpos) in junc2.get_aligned_pairs(): # broken by pysam 8.3
            for qrypos, refpos in enumerate(junc2.get_reference_positions()):
                if refpos in range(*tsd_ref_interval):
                    if qrypos is not None:
                        tsdseq2 += junc2.seq[qrypos+junc2.qstart]

            return tsdseq1, tsdseq2


    def genotype(self, bams):
        ''' add supporting read count, VAF for each BAM '''
        if self.tsd():
            for bam in bams:
                self.genotypes.append([os.path.basename(bam.filename.decode())] + list(getVAF(bam, self.be1.chrom, (self.be1.breakpos, self.be2.breakpos))))


    def fetch_discordant_reads(self, bams, isize=10000, debug=True, logger=None, max_fetch=50):
        ''' Return list of DiscoRead objects '''
        chrom = self.be1.chrom
        start = self.min_supporting_base()
        end   = self.max_supporting_base()

        if None in (start, end): return []
     
        ins_debug_name = '%s:%d-%d' % (self.be1.chrom, self.min_supporting_base(), self.max_supporting_base())

        assert start < end, 'Ins %s: fetch_discordant_reads: start > end' % ins_debug_name

        # track across all BAMs
        all_mapped   = {}
        all_unmapped = {}

        for bam in bams:
            # track for just this BAM
            bam_mapped   = {}
            bam_unmapped = {}

            for read in bam.fetch(chrom, start, end):
                if read.is_paired and not read.is_unmapped and not read.is_secondary and not is_supplementary(read) and not read.is_duplicate and read.mapq > 0:
                    chrom = str(bam.getrname(read.tid))
         
                    if read.mate_is_unmapped:
                        bam_unmapped[read.qname] = DiscoRead(chrom, read, bam.filename.decode())
         
                    else:
                        pair_dist = abs(read.reference_start - read.next_reference_start)
                        if read.tid != read.next_reference_id or pair_dist > isize:
                            mate_chrom = str(bam.getrname(read.next_reference_id))
                            bam_mapped[read.qname] = DiscoRead(chrom, read, bam.filename.decode(), mate_chrom)

            #if logger and debug:
                #logger.debug('Ins %s: fetch discordant from BAM %s: %d mapped, %d unmapped' % (ins_debug_name, bam.filename, len(bam_mapped), len(bam_unmapped)))

            if len(bam_mapped) + len(bam_unmapped) > max_fetch:
                total_drc = len(bam_mapped) + len(bam_unmapped)
                logger.debug('Ins %s: discordant read count (%d) is over --max_disc_fetch (%d), subsampling discordant reads' % (ins_debug_name, total_drc, max_fetch))

                mapped_sub   = (len(bam_mapped) / float(len(bam_mapped) + len(bam_unmapped))) * max_fetch
                unmapped_sub = (len(bam_unmapped) / float(len(bam_mapped) + len(bam_unmapped))) * max_fetch

                #logger.debug('mapped subsamp: %d, unmapped subsamp: %d' % (mapped_sub, unmapped_sub))

                bam_mapped   = subsample_dict(bam_mapped, int(mapped_sub))
                bam_unmapped = subsample_dict(bam_unmapped, int(unmapped_sub))

            # get mate info

            # mate mapped
            for qname, dr in bam_mapped.items():
                for read in bam.fetch(dr.mate_chrom, dr.read.next_reference_start, dr.read.next_reference_start+1):
                    if read.qname == qname and not read.is_secondary and not is_supplementary(read):
                        if read.seq != bam_mapped[qname].read.seq:
                            bam_mapped[qname].mate_read = read
                            break

            # mate unmapped
            for read in bam.fetch(chrom, start, end):
                if read.is_unmapped and read.qname in bam_unmapped:
                    if not read.is_secondary and not is_supplementary(read):
                        bam_unmapped[read.qname].mate_read = read
                        break

            # merge into all_(un)mapped
            all_mapped.update(bam_mapped)
            all_unmapped.update(bam_unmapped)

        self.discreads = list(all_mapped.values()) + list(all_unmapped.values())


    def improve_consensus(self, ctg_fa, bwaref, tmpdir='/tmp'):
        ''' attempt to use assembly of all supporting reads to build a better consensus '''
        cons_fasta = self.consensus_fasta(tmpdir)

        ctg_falib = load_falib(ctg_fa)

        build_last_db(ctg_fa)

        la_results = align_last(cons_fasta, ctg_fa, e=20)

        if self.be1 is not None:
            # find corresponding contig, if possible

            for res in la_results:
                if res.query_id == self.be1.uuid:
                    # criteria for deciding the new consensus is better
                    unaln_frac = float(len(self.be1.consensus) - res.query_alnsize) / len(self.be1.consensus)

                    if res.target_seqsize > len(self.be1.consensus) and unaln_frac < 0.15 and res.pct_match() > 0.95:
                        self.be1_alt = self.be1
                        self.be1.consensus = ctg_falib[res.target_id]
                        self.be1_improved_cons = True


        if self.be2 is not None:
            # find corresponding contig, if possible

            for res in la_results:
                if res.query_id == self.be2.uuid:
                    # criteria for deciding the new consensus is better
                    unaln_frac = float(len(self.be2.consensus) - res.query_alnsize) / len(self.be2.consensus)

                    if res.target_seqsize > len(self.be2.consensus) and unaln_frac < 0.1 and res.pct_match() > 0.95:
                        self.be2_alt = self.be2
                        self.be2.consensus = ctg_falib[res.target_id]
                        self.be2_improved_cons = True

        for ext in ('','.amb','.ann','.bck','.bwt','.des','.fai','.pac','.prj','.sa','.sds','.ssp','.suf','.tis'):
            if os.path.exists(ctg_fa+ext): os.remove(ctg_fa+ext)

        if os.path.exists(cons_fasta): os.remove(cons_fasta)

        return self.be1_improved_cons, self.be2_improved_cons


    def supportreads_fastq(self, outdir, min_readlen=50, limit=1000, seed=1):
        ''' discordant support reads marked DR, split support reads marked SR '''
        assert os.path.exists(outdir)

        random.seed(seed)

        outreads  = od()
        usedreads = {}

        out_fastq = outdir + '/' + '.'.join(('supportreads', self.be1.chrom, str(self.be1.breakpos), str(uuid4()), 'fq'))
        with open(out_fastq, 'w') as out:
            for readstore in (self.be1, self.be2, self.discreads):
                if readstore:
                    try:
                        rtype = 'SR'
                        readlist = readstore.cluster.reads
                    except:
                        rtype = 'DR'
                        readlist = readstore

                    for r in readlist:
                        read = r.read
                        name = read.qname
                        unseen = True

                        assert not (read.is_read1 and read.is_read2)

                        if read.is_read1:
                            if name + '/1' in usedreads: unseen = False
                            usedreads[name + '/1'] = True
                            name += '.%s/1' % rtype

                        elif read.is_read2:
                            if name + '/2' in usedreads: unseen = False
                            usedreads[name + '/2'] = True
                            name += '.%s/2' % rtype

                        else: # single end reads
                            if name + '/0' in usedreads: unseen = False
                            usedreads[name + '/0'] = True
                            name += '.%s/0' % rtype


                        if len(read.seq) > min_readlen and unseen: outreads[name] = read.seq + '\n+\n' + read.qual

                        if rtype == 'DR' and r.mate_read is not None: # get discordant mates
                            read = r.mate_read
                            name = r.mate_read.qname
                            unseen = True

                            if read.is_read1:
                                if name + '/1' in usedreads: unseen = False
                                usedreads[name + '/1'] = True
                                name += '.%s/1' % rtype

                            if read.is_read2:
                                if name + '/2' in usedreads: unseen = False
                                usedreads[name + '/2'] = True
                                name += '.%s/2' % rtype

                            if len(read.seq) > min_readlen and unseen: outreads[name] = read.seq + '\n+\n' + read.qual

            #if len(outreads) >= limit or len(outreads) == 0: return None
            if len(outreads) == 0:
                return None

            if len(outreads) > limit:
                sampled = random.sample(list(outreads), limit)
                subsamp = od()

                for name, data in outreads.items():
                    if name in sampled:
                        subsamp[name] = data

                outreads = subsamp

            for name, data in outreads.items():
                out.write('@%s\n%s\n' % (name, data))
                self.fastqrecs.append('@%s\n%s\n' % (name, data))

        return out_fastq

    def consensus_fasta(self, tmpdir='/tmp'):
        assert os.path.exists(tmpdir)

        out_fasta = tmpdir + '/' + '.'.join(('consensus', self.be1.chrom, str(self.be1.breakpos), str(uuid4()), 'fa'))
        with open(out_fasta, 'w') as out:
            out.write('>%s\n%s\n' % (self.be1.uuid, self.be1.consensus))
            if self.be2 is not None:
                out.write('>%s\n%s\n' % (self.be2.uuid, self.be2.consensus))

        return out_fasta

    def compile_info(self, bams, genotype=True):
        ''' fill self.info with summary info, needs original bam for chromosome lookup '''
        if self.be1 == None and self.be2 == None:
            return None

        self.info['ins_uuid'] = self.uuid
        self.info['chrom'] = self.be1.chrom
        self.info['min_supporting_base'] = self.min_supporting_base()
        self.info['max_supporting_base'] = self.max_supporting_base()
        self.info['mappability'] = self.mappability

        self.info['be1_breakpos'] = self.be1.breakpos
        self.info['be1_obj_uuid'] = self.be1.uuid
        
        #seqs
        self.info['be1_cons_seq'] = self.be1.consensus
        self.info['be1_prox_seq'] = ','.join(map(lambda x : x.query_alignment_sequence, self.be1.proximal_subread()))
        self.info['be1_dist_seq'] = ','.join(map(lambda x : x.query_alignment_sequence, self.be1.distal_subread()))
        self.info['be1_umap_seq'] = ','.join(self.be1.unmapped_subread()[1])

        if self.be1.proximal_subread() and self.be1.proximal_subread()[0].is_reverse:
            self.info['be1_prox_str'] = '-'
        else:
            self.info['be1_prox_str'] = '+'

        self.info['be1_prox_loc'] = []
        for subseq in self.info['be1_prox_seq'].split(','):
            self.info['be1_prox_loc'].append(locate_subseq(self.be1.consensus, orient_subseq(self.be1.consensus, subseq)))

        # stats
        self.info['be1_sr_count'] = len(self.be1)
        self.info['be1_num_maps'] = len(self.be1.mappings)
        self.info['be1_cons_scr'] = self.be1.consscore
        self.info['be1_median_D'] = self.be1.cluster.median_D()
        self.info['be1_avgmatch'] = self.be1.cluster.avg_matchpct()
        self.info['be1_rg_count'] = self.be1.cluster.readgroups()
        self.info['be1_bf_count'] = self.be1.cluster.bamfiles()
        self.info['be1_prox_mpq'] = ','.join(map(lambda x : str(x.mapq), self.be1.proximal_subread()))
        self.info['be1_improved'] = self.be1_improved_cons

        if self.info['be1_dist_seq'] == '':
            self.info['be1_dist_seq'] = None
        else:
            self.info['be1_dist_chr'] = ','.join(map(lambda x : bams[0].getrname(x.tid), self.be1.distal_subread()))
            self.info['be1_dist_pos'] = ','.join(map(lambda x : str(x.get_reference_positions()[0]), self.be1.distal_subread()))
            self.info['be1_dist_end'] = ','.join(map(lambda x : str(x.get_reference_positions()[-1]), self.be1.distal_subread()))
            self.info['be1_dist_mpq'] = ','.join(map(lambda x : str(x.mapq), self.be1.distal_subread()))

        if self.info['be1_umap_seq'] == '':
            self.info['be1_umap_seq'] = None

        if self.be2 is not None:
            self.info['be2_breakpos'] = self.be2.breakpos
            self.info['be2_obj_uuid'] = self.be2.uuid
            self.info['be2_cons_seq'] = self.be2.consensus
            self.info['be2_prox_seq'] = ','.join(map(lambda x : x.query_alignment_sequence, self.be2.proximal_subread()))
            self.info['be2_dist_seq'] = ','.join(map(lambda x : x.query_alignment_sequence, self.be2.distal_subread()))
            self.info['be2_umap_seq'] = ','.join(self.be2.unmapped_subread()[1])

            if self.be2.proximal_subread() and self.be2.proximal_subread()[0].is_reverse:
                self.info['be2_prox_str'] = '-'
            else:
                self.info['be2_prox_str'] = '+'


            self.info['be2_prox_loc'] = []
            for subseq in self.info['be2_prox_seq'].split(','):
                self.info['be2_prox_loc'].append(locate_subseq(self.be2.consensus, orient_subseq(self.be2.consensus, subseq)))

            # stats
            self.info['be2_sr_count'] = len(self.be2)
            self.info['be2_num_maps'] = len(self.be2.mappings)
            self.info['be2_cons_scr'] = self.be2.consscore
            self.info['be2_median_D'] = self.be2.cluster.median_D()
            self.info['be2_avgmatch'] = self.be2.cluster.avg_matchpct()
            self.info['be2_rg_count'] = self.be2.cluster.readgroups()
            self.info['be2_bf_count'] = self.be2.cluster.bamfiles()
            self.info['be2_prox_mpq'] = ','.join(map(lambda x : str(x.mapq), self.be2.proximal_subread()))
            self.info['be2_improved'] = self.be2_improved_cons

            if self.info['be2_dist_seq'] == '':
                self.info['be2_dist_seq'] = None
            else:
                self.info['be2_dist_chr'] = ','.join(map(lambda x: bams[0].getrname(x.tid), self.be2.distal_subread()))
                self.info['be2_dist_pos'] = ','.join(map(lambda x: str(x.get_reference_positions()[0]), self.be2.distal_subread()))
                self.info['be2_dist_end'] = ','.join(map(lambda x: str(x.get_reference_positions()[-1]), self.be2.distal_subread()))
                self.info['be2_dist_mpq'] = ','.join(map(lambda x : str(x.mapq), self.be2.distal_subread()))                

            if self.info['be2_umap_seq'] == '':
                self.info['be2_umap_seq'] = None

            self.info['be1_use_prox'] = 0
            self.info['be2_use_prox'] = 0

            if self.info['be1_prox_loc'] == self.info['be2_prox_loc']: # insertion may be completely assembled
                if len(self.info['be1_prox_loc']) > 1:
                    self.info['be1_use_prox'] = 1

                elif len(self.info['be2_prox_loc']) > 1:
                    self.info['be2_use_prox'] = 1

            tsdpair = self.tsd(be1_use_prox=self.info['be1_use_prox'], be2_use_prox=self.info['be2_use_prox'])
            if tsdpair is not None:
                self.info['be1_end_over'], self.info['be2_end_over'] = tsdpair

        if 'be2_breakpos' not in self.info:
            self.info['be2_breakpos'] = self.info['be1_breakpos']

        if 'be2_sr_count' not in self.info:
            self.info['be2_sr_count'] = 0

        self.info['dr_count'] = len(self.discreads)
        self.info['dr_unmapped_mates'] = len([dr for dr in self.discreads if dr.mate_read is not None and dr.mate_read.is_unmapped])
        if not self.genotypes:
            self.genotype(bams)

        self.info['genotypes'] = ','.join(['|'.join(map(str, fields)) for fields in self.genotypes])


# imported from discocluster.py


class DiscoCoord:
    def __init__(self, chrom, start, end, strand, mchrom, mstart, mend, mstrand, label, bam_name):
        self.chrom   = chrom
        self.start   = int(start)
        self.end     = int(end)
        self.strand  = strand
        self.mchrom  = mchrom
        self.mstart  = int(mstart)
        self.mend    = int(mend)
        self.mstrand = mstrand
        self.label   = label
        self.bam     = bam_name.decode()

        # if strand of genome element is '-', flip apparent mate strand
        elt_str = self.label.split('|')[-1]
        assert elt_str in ('+', '-'), 'malformed input BED: last three cols need to be class, family, orientation (+/-)'

        if elt_str == '-': self.mstrand = flip(self.mstrand)


    def __gt__(self, other):
        if self.chrom == other.chrom:
            return self.start > other.start
        else:
            return self.chrom > other.chrom


    def __str__(self):
        return '\t'.join(map(str, (self.bam, self.label, self.chrom, self.start, self.end, self.strand, self.mchrom, self.mstart, self.mend, self.mstrand)))


class DiscoInsCall:
    def __init__(self, coord_list, chrom, start, end, strand, bamlist):
        self.coord_list = coord_list
        self.chrom      = chrom
        self.start      = int(start)
        self.end        = int(end)
        self.strand     = strand
        self.bamlist    = bamlist
        self.length     = len(coord_list)


    def out(self, pad=0, verbose=True):
        output = ['#BEGIN']
        output.append('%s\t%d\t%d\t%s\t%s\t%d' % (self.chrom, self.start-pad, self.end+pad, self.strand, self.bamlist, self.length))
        if verbose:
            for c in self.coord_list: output.append(str(c))
        output.append('#END')

        return '\n'.join(output)


    def overlaps(self, other):
        ''' return true if overlap > 0 '''
        return min(self.end, other.end) - max(self.start, other.start) > 0


    def __gt__(self, other):
        if self.chrom == other.chrom:
            return self.start > other.start
        else:
            return self.chrom > other.chrom


    def __str__(self):
        return self.out(verbose=False)

###############################
## classes used by resolve() ##
###############################


class Annotator:
    def __init__(self, tabix_list):
        self.tbx = od()

        for fn in tabix_list.strip().split(','):
            self.tbx[os.path.basename(fn)] = pysam.Tabixfile(fn)

    def annotate(self, chrom, start, end):
        out = od()
        for fn, tbx in self.tbx.items():
            out[fn] = []
            if chrom in tbx.contigs:
                for rec in tbx.fetch(chrom, start, end):
                    if len(rec.strip().split()) == 3:
                        out[fn].append(','.join('Y'))
                    else:
                        out[fn].append(','.join(rec.strip().split()[3:]))

            out[fn] = ';'.join(out[fn])

            if not out[fn]: out[fn] = 'NA'

        return out


class Ins:
    def __init__(self, ins, annotation_tabix, use_rg, callmuts=False, allow_unmapped=False):
        self.allow_unmapped=allow_unmapped

        self.annotator = None
        if annotation_tabix is not None:
            self.annotator = Annotator(annotation_tabix)

        self.ins = ins['INFO']
        self.bestref = best_ref(ins)
        self.out = od()
        self.end3 = 'NA'
        self.end5 = 'NA'

        self.use_rg = use_rg

        self._fill_out(callmuts=callmuts)


    def _fill_out(self, callmuts=False):
        self.out['UUID'] = self.ins['ins_uuid']

        self.out['Chromosome']    = chrom = self.ins['chrom']
        self.out['Left_Extreme']  = start = self.ins['min_supporting_base']
        self.out['Right_Extreme'] = end   = self.ins['max_supporting_base']
        self.assign35ends()
        self.te_family()
        self.elt_coords()
        self.support()
        self.improve_cons()
        self.tsd()
        self.sample_list()
        self.consensus()

        if self.annotator is not None: self.out.update(self.annotator.annotate(chrom, start, end))
        self.annotator = None # can't return cython objects through multiprocessing

        if callmuts: self.call_mutations()

        self.out['Genotypes'] = 'NA'

        if 'genotypes' in self.ins:
            self.out['Genotypes'] = self.ins['genotypes']

        if not self.out['Genotypes']:
            self.out['Genotypes'] = 'NA'

    def assign35ends(self):
        self.out['5_Prime_End'] = 'NA'
        self.out['3_Prime_End'] = 'NA'

        if 'be1_is_3prime' in self.ins:
            if self.ins['be1_is_3prime']:
                self.out['3_Prime_End'] = self.ins['be1_breakpos']
                self.out['5_Prime_End'] = self.ins['be2_breakpos']
                self.end3 = 'be1'
                self.end5 = 'be2'
            else:
                self.out['3_Prime_End'] = self.ins['be2_breakpos']
                self.out['5_Prime_End'] = self.ins['be1_breakpos']
                self.end3 = 'be2'
                self.end5 = 'be1'

        if self.allow_unmapped and 'NA' in (self.out['5_Prime_End'], self.out['3_Prime_End']):
                self.out['3_Prime_End'] = self.ins['be1_breakpos']
                self.out['5_Prime_End'] = self.ins['be2_breakpos']
                self.end3 = 'be1'
                self.end5 = 'be2'

    def te_family(self):
        ''' inslib input can have headers superfamily:subfamily e.g. L1:L1Ta or Alu:AluYa5 '''
        self.out['Superfamily'] = 'NA'
        self.out['Subfamily']   = 'NA'

        if self.bestref:
            self.out['Superfamily'] = self.bestref.split(':')[0]

            if ':' in self.bestref:
                self.out['Subfamily'] = self.bestref.split(':')[1]


    def elt_coords(self):
        self.out['TE_Align_Start'] = 'NA'
        self.out['TE_Align_End']   = 'NA'

        self.out['Orient_5p'] = 'NA'
        self.out['Orient_3p'] = 'NA'

        if self.end5 + '_orient' in self.ins: self.out['Orient_5p'] = self.ins[self.end5 + '_orient']
        if self.end3 + '_orient' in self.ins: self.out['Orient_3p'] = self.ins[self.end3 + '_orient']

        if self.end5 + '_bestmatch' in self.ins: self.out['TE_Align_Start'] = self.ins[self.end5 + '_bestmatch'].target_start
        if self.end3 + '_bestmatch' in self.ins: self.out['TE_Align_End']   = self.ins[self.end3 + '_bestmatch'].target_end

        if 'remap_min_pos' in self.ins:
            if self.out['TE_Align_Start'] == 'NA' or self.out['TE_Align_Start'] > self.ins['remap_min_pos']:
                self.out['TE_Align_Start'] = self.ins['remap_min_pos']

        if 'remap_min_pos' in self.ins:
            
            if self.out['TE_Align_End'] == 'NA' or self.out['TE_Align_End'] < self.ins['remap_max_pos']:
                self.out['TE_Align_End'] = self.ins['remap_max_pos']

        self.out['Inversion'] = 'NA'
        if 'inversion' in self.ins:
            if self.ins['inversion']: self.out['Inversion'] = 'Y'
            else: self.out['Inversion'] = 'N'

    def junctions(self):
        j = []
        if 'be1_breakpos' in self.ins: j.append(self.ins['be1_breakpos'])
        if 'be2_breakpos' in self.ins: j.append(self.ins['be2_breakpos'])
        return j

    def support(self):
        self.out['5p_Elt_Match'] = 0.0
        self.out['3p_Elt_Match'] = 0.0

        if self.end5 + '_bestmatch' in self.ins: self.out['5p_Elt_Match'] = self.ins[self.end5 + '_bestmatch'].pct_match()
        if self.end3 + '_bestmatch' in self.ins: self.out['3p_Elt_Match'] = self.ins[self.end3 + '_bestmatch'].pct_match()

        self.out['5p_Genome_Match'] = 0.0
        self.out['3p_Genome_Match'] = 0.0

        if self.end5 + '_avgmatch' in self.ins: self.out['5p_Genome_Match'] = self.ins[self.end5 + '_avgmatch']
        if self.end3 + '_avgmatch' in self.ins: self.out['3p_Genome_Match'] = self.ins[self.end3 + '_avgmatch']

        self.out['Split_reads_5prime'] = 0
        self.out['Split_reads_3prime'] = 0

        if self.end5 + '_sr_count' in self.ins: self.out['Split_reads_5prime'] = self.ins[self.end5 + '_sr_count']
        if self.end3 + '_sr_count' in self.ins: self.out['Split_reads_3prime'] = self.ins[self.end3 + '_sr_count']

        self.out['Remapped_Discordant'] = 0
        self.out['Remap_Disc_Fraction'] = 0.0
        self.out['Remapped_Splitreads'] = 0
        self.out['Remap_Split_Fraction'] = 0.0

        if 'remap_dr_count' in self.ins:
            self.out['Remapped_Discordant'] = self.ins['remap_dr_count']
            if self.ins['dr_count'] > 0:
                self.out['Remap_Disc_Fraction'] = self.out['Remapped_Discordant']/float(self.ins['dr_count'])


        sr_count = 0
        for be in ('be1', 'be2'):
            if be+'_sr_count' in self.ins: sr_count += self.ins[be+'_sr_count']

        if 'remap_sr_count' in self.ins:
            self.out['Remapped_Splitreads'] = self.ins['remap_sr_count']
            if sr_count > 0:
                self.out['Remap_Split_Fraction'] = self.out['Remapped_Splitreads']/float(sr_count)

        self.out['5p_Cons_Len'] = 0
        self.out['3p_Cons_Len'] = 0

        if self.end5 + '_cons_seq' in self.ins: self.out['5p_Cons_Len'] = len(self.ins[self.end5 + '_cons_seq'])
        if self.end3 + '_cons_seq' in self.ins: self.out['3p_Cons_Len'] = len(self.ins[self.end3 + '_cons_seq'])

    def tsd(self):
        self.out['TSD_5prime'] = 'NA'
        self.out['TSD_3prime'] = 'NA'

        if self.end5 + '_end_over' in self.ins: self.out['TSD_5prime'] = self.ins[self.end5 + '_end_over']
        if self.end3 + '_end_over' in self.ins: self.out['TSD_3prime'] = self.ins[self.end3 + '_end_over']
        

    def improve_cons(self):
        self.out['5p_Improved'] = 'N'
        self.out['3p_Improved'] = 'N'

        if self.end5 + '_improved' in self.ins and self.ins[self.end5 + '_improved']:
            self.out['5p_Improved'] = 'Y'

        if self.end3 + '_improved' in self.ins and self.ins[self.end3 + '_improved']:
            self.out['3p_Improved'] = 'Y'

    def sample_list(self):
        samples = dd(int)
        sample_list = []

        self.out['Sample_support_5p'] = 'NA'
        self.out['Sample_support_3p'] = 'NA'

        ctype = 'bf'
        if self.use_rg: ctype = 'rg'

        for be in ('be1', 'be2'):
            end = '5p'

            if self.end3 == be:
                end = '3p'

            if be + '_' + ctype + '_count' in self.ins:
                samples = dd(int)

                for sample_support in self.ins[be + '_' + ctype + '_count']:
                    sample, count = sample_support.split('|')
                    sample_list.append(sample)
                    samples[sample] = int(count)

                self.out['Sample_support_' + end] = ','.join(['%s|%d' % (sample, count) for sample, count in samples.items()])

        sample_list = list(set(sample_list))
        self.out['Sample_count'] = len(sample_list)
            

    def consensus(self):
        self.out['Genomic_Consensus_5p'] = 'NA'
        self.out['Genomic_Consensus_3p'] = 'NA'

        self.out['Insert_Consensus_5p'] = 'NA'
        self.out['Insert_Consensus_3p'] = 'NA'

        if self.end5 + '_cons_seq' in self.ins: self.out['Genomic_Consensus_5p'] = self.ins[self.end5 + '_cons_seq']
        if self.end3 + '_cons_seq' in self.ins: self.out['Genomic_Consensus_3p'] = self.ins[self.end3 + '_cons_seq']

        if self.end5 + '_joined_cons' in self.ins:
            self.out['Genomic_Consensus_5p'] = self.ins[self.end5 + '_joined_cons']
            self.out['5p_Cons_Len'] = len(self.ins[self.end5 + '_joined_cons'])

        if self.end3 + '_joined_cons' in self.ins:
            self.out['Genomic_Consensus_3p'] = self.ins[self.end3 + '_joined_cons']
            self.out['3p_Cons_Len'] = len(self.ins[self.end3 + '_joined_cons'])

        if self.end5 + '_te_cons_seq' in self.ins: self.out['Insert_Consensus_5p'] = self.ins[self.end5 + '_te_cons_seq']
        if self.end3 + '_te_cons_seq' in self.ins: self.out['Insert_Consensus_3p'] = self.ins[self.end3 + '_te_cons_seq']


    def end_align_flush(self, tolerance=2):
        flush = False

        for be in ('be1','be2'):
            if be+'_prox_loc' in self.ins:
                aligned_segs = self.ins[be+'_prox_loc']
                cons_len = len(self.ins[be+'_cons_seq'])

                for s in aligned_segs:
                    if s[0] <= tolerance or s[1] >= cons_len-tolerance: flush = True

        return flush

    def header(self):
        return '\t'.join(self.out.keys())

    def call_mutations(self):
        ''' requires bcftools '''
        self.out['Variants'] = 'NA'
        if 'support_bam_file' not in self.ins: return 'NA'
        if not os.path.exists(self.ins['support_bam_file']): return 'NA'
        if not os.path.exists(self.ins['support_bam_file'] + '.bai'): return 'NA'
        if not os.path.exists(self.ins['inslib_fa'] + '.fai'): return 'NA'

        samtools_cmd = ['samtools', 'mpileup', '-ugf', self.ins['inslib_fa'], self.ins['support_bam_file']]
        bcftools_cmd = ['bcftools', 'call', '-vm']

        FNULL = open(os.devnull, 'w')

        p1 = subprocess.Popen(samtools_cmd, stdout=subprocess.PIPE, stderr=FNULL)
        p2 = subprocess.Popen(bcftools_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=FNULL)

        logger.debug('Calling muts on %s:%d-%d (%s)' % (self.ins['chrom'], self.ins['min_supporting_base'], self.ins['max_supporting_base'], self.ins['ins_uuid']))

        muts = []

        for vcfline in p2.stdout:
            vcfline = vcfline.decode()
            if not vcfline.startswith('#'):
                chrom, pos, uid, ref, alt = vcfline.split()[:5]
                muts.append('%s:%s>%s' % (pos, ref, alt))

        if len(muts) > 0:
            self.out['Variants'] = ','.join(muts)

    def __lt__(self, other):
        if self.out['Chromosome'] == other.out['Chromosome']:
            return self.out['Left_Extreme'] < other.out['Left_Extreme']

        return self.out['Chromosome'] < other.out['Chromosome']

    def __str__(self):
        return '\t'.join(map(str, self.out.values()))


#######################################
## Functions                         ##
#######################################
 
 
def rc(dna):
    ''' reverse complement '''
    complements = str.maketrans('acgtrymkbdhvACGTRYMKBDHV', 'tgcayrkmvhdbTGCAYRKMVHDB')
    return dna.translate(complements)[::-1]


def subsample_dict(orig, n, seed=1):
    random.seed(seed)

    assert len(orig) >= n

    if n == 0:
        return {}

    sample = random.sample(orig.keys(), n)
    d = {}
    for k in sample:
        d[k] = orig[k]

    return d
 

def read_matchpct(read):
    ''' return number of mismatches / aligned length of read '''
    nm = [value for (tag, value) in read.tags if tag == 'NM'][0]
    return 1.0 - (float(nm)/float(read.alen))

 
def ref_overlap(read1, read2):
    ''' return overlapping interval in ref. coords (not chrom), none otherwise '''
 
    if read1 is None or read2 is None:
        return None
 
    if read1.is_unmapped or read2.is_unmapped:
        return None

    iv1 = sorted((read1.get_reference_positions()[0], read1.get_reference_positions()[-1]))
    iv2 = sorted((read2.get_reference_positions()[0], read2.get_reference_positions()[-1]))
 
    if min(iv1[1], iv2[1]) - max(iv1[0], iv2[0]) > 0: # is there overlap?
        return [max(iv1[0], iv2[0]), min(iv1[1], iv2[1])]
 
    return None
 
 
def ref_dist(read1, read2):
    ''' return distance between intervals in ref., overlapping = negative values '''
 
    if read1 is None or read2 is None:
        return None
 
    iv1 = sorted((read1.get_reference_positions()[0], read1.get_reference_positions()[-1]))
    iv2 = sorted((read2.get_reference_positions()[0], read2.get_reference_positions()[-1]))
 
    return max(iv1[0], iv2[0]) - min(iv1[1], iv2[1])


def qualtrim(read, minqual=35):
    ''' return quality-trimmed sequence given a pysam.AlignedSegment '''
    q = [ord(b)-minqual for b in list(read.qual)]

    for i in range(0,len(q)-4): # sliding window, 4bp
        if np.mean(q[i:i+4]) < 5:
            return read.seq[:i]

    return read.seq


def orient_subseq(longseq, shortseq):
    ''' return shortseq in same orientation as longseq '''
    assert len(longseq) >= len(shortseq), 'orient_subseq: %s < %s' % (longseq, shortseq)
 
    if re.search(shortseq, longseq):
        return shortseq
    else:
        assert re.search(rc(shortseq), longseq), "orient_subseq: %s not a subseq of %s" %(shortseq, longseq)
        return rc(shortseq)
 
 
def is_primary(read):
    ''' not included in pysam '''
    return not (read.is_secondary or is_supplementary(read))
 
 
def is_supplementary(read):
    ''' pysam does not currently include a check for this flag '''
    return bin(read.flag & 2048) == bin(2048)


def joinseqs(seq1, seq2, minscore=0.95, minlen=20):
    ''' join two seqs if the ends match going left to right '''

    if not seq1 or not seq2:
        return None, None, None

    seq1_dna = skseq.DNA(seq1)
    seq2_dna = skseq.DNA(seq2)

    try:
        aln_res = skalign.local_pairwise_align_ssw(seq1_dna, seq2_dna)
    except IndexError:
        return None, 0.0, 0

    aln_tab = aln_res[0]

    score = sum(aln_tab.conservation(gap_mode='include')==1.)/aln_tab.shape.position

    joined = None

    seq1_aln, seq2_aln = aln_res[2]

    if score > minscore and aln_tab.shape.position >= minlen and (seq1_aln[1] > len(seq1)-2 or seq2_aln[0] < 2):
        joined = seq1 + seq2[seq2_aln[1]+1:]

    return joined, score, aln_tab.shape.position


def asm_rescue(fa):
    ''' attempt to join best two contigs together '''
    seqdict = load_falib(fa)
    seqs = seqdict.values()
    seqs.sort(lambda x,y: cmp(len(y), len(x)))

    if len(seqs) < 2:
        return fa

    joined_01, score_01, matchlen_01 = joinseqs(seqs[0], seqs[1])
    joined_10, score_10, matchlen_10 = joinseqs(seqs[1], seqs[0])

    if joined_01 == joined_10 == None:
        return fa

    if joined_01 or joined_10:
        if score_01*matchlen_01 > score_10*matchlen_10:
            seqdict['rescue_' + os.path.basename(fa)] = joined_01

        else:
            seqdict['rescue_' + os.path.basename(fa)] = joined_10

    with open(fa, 'w') as out:
        for name, seq in seqdict.items():
            out.write(">%s\n%s\n" % (name, seq))

    return fa

 
def fetch_clipped_reads(bams, chrom, start, end, filters, logger=None, limit=500):
    ''' Return list of SplitRead objects '''
    assert filters['min_minclip'] >= 2 
 
    splitreads = []
 
    start = int(start)
    end   = int(end)
 
    assert start < end

    if start < 0: start = 0

    position_counter = dd(int)

    masked_read_count = 0
 
    for bam in bams:
        minqual = guess_minqual(bam) # used for quality trimming when building consensus

        for read in bam.fetch(chrom, start, end):
            masked = False
            if filters['genome_mask'] is not None and chrom in filters['genome_mask']:
                if filters['genome_mask'][chrom].find(read.pos, read.pos+1):
                    masked = True
                    masked_read_count += 1

                if read.is_duplicate:
                    masked = True

            if not masked and not read.is_unmapped and not read.is_duplicate: #and read.mapq > 0:
                if read.rlen - read.alen >= int(filters['min_minclip']): # 'soft' clipped?
     
                    # length of 'minor' clip
                    altclip = min(read.qstart, read.rlen-read.qend)

                    # junk bases
                    N_count = 0
                    if 'N' in read.seq: N_count = Counter(read.seq)['N']
     
                    if altclip <= 2: # could add as a filter
                        if N_count <= filters['max_N_consensus'] and splitqual(read) >= filters['min_MW_P'] and len(read.get_reference_positions()) > 0:
                            chrom = str(bam.getrname(read.tid))

                            limited = False

                            if position_counter[read.get_reference_positions()[0]] >= limit:
                                limited = True

                            if position_counter[read.get_reference_positions()[-1]] >= limit:
                                limited = True

                            position_counter[read.get_reference_positions()[0]] += 1
                            position_counter[read.get_reference_positions()[-1]] += 1
                            
                            if len(read.get_reference_positions()) > 0 and not limited:
                                splitreads.append(SplitRead(chrom, read, bam.filename.decode(), minqual))

    if logger:
        logger.debug('Chunk %s:%d-%d: masked %d reads due to -m/--mask' % (chrom, start, end, masked_read_count))
 
    return splitreads
 
 
def splitqual(read):
    ''' return Mann-Whitney P for clipped vs unclipped quals '''
    
    breakpos = None
 
    breakpos = read.get_aligned_pairs()[-1][0] # breakpoint on right
 
    q1 = list(map(ord, list(read.qual[:breakpos])))
    q2 = list(map(ord, list(read.qual[breakpos:])))

    if min(q1) == max(q1) == min(q2) == max(q2):
        return 1.0
 
    return ss.mannwhitneyu(q1, q2)[1]


def concat_fa(falist, tmpdir='/tmp'):
    outfn = '%s/tebreak.concat.fa.%s.fa' % (tmpdir, str(uuid4()))
    out = open(outfn, 'w')

    for fn in falist:
        with open(fn, 'r') as fa:
            for line in fa:
                out.write(line)

    out.close()

    for fn in falist:
        os.remove(fn)

    return outfn
 

def load_falib(infa):
    seqdict = {}

    with open(infa, 'r') as fa:
        seqid = ''
        seq   = ''
        for line in fa:
            if line.startswith('>'):
                if seq != '':
                    seqdict[seqid] = seq
                seqid = line.lstrip('>').strip().split()[0]
                seq   = ''
            else:
                assert seqid != ''
                seq = seq + line.strip()

    if seqid not in seqdict and seq != '':
        seqdict[seqid] = seq

    return seqdict
 

def build_sr_clusters(splitreads, searchdist=100): # TODO PARAM, 
    ''' cluster SplitRead objects into Cluster objects and return a list of them '''
    clusters  = []
 
    for sr in splitreads:
        if len(clusters) == 0:
            clusters.append(SplitCluster(sr))
 
        elif clusters[-1].chrom != sr.chrom:
            clusters.append(SplitCluster(sr))
 
        else:
            if abs(clusters[-1].median - sr.breakpos) > searchdist:
                clusters.append(SplitCluster(sr))
 
            else:
                clusters[-1].add_splitread(sr)

    return clusters


def build_breakends(cluster, filters, tmpdir='/tmp'):
    ''' returns list of breakends from cluster '''
    breakends = []

    for breakpos in cluster.all_breakpoints():
        for dir in ('left', 'right'):
            subcluster = cluster.subcluster_by_breakend([breakpos], direction=dir)
            if len(subcluster) >= filters['min_sr_per_break'] and subcluster.max_cliplen() >= filters['min_maxclip']:
                seq     = subcluster.reads[0].read.seq
                score   = 1.0

                if len(subcluster) > 1: seq, score = subcluster.consensus()
 
                N_count = 0
                if 'N' in seq: N_count = Counter(seq)['N']

                if seq != '' and score >= filters['min_consensus_score'] and N_count <= filters['max_N_consensus']:
                    breakends.append(BreakEnd(cluster.chrom, breakpos, subcluster, seq, score, dir))

 
    return breakends


def map_breakends(breakends, db, tmpdir='/tmp'):
    ''' remap consensus sequences stored in BreakEnd objects '''
    tmp_fa = tmpdir + '/' + '.'.join(('tebreak', str(uuid4()), 'be.fa'))
    breakdict = {} # for faster lookup
 
    with open(tmp_fa, 'w') as out:
        for be in breakends:
            be.mappings = []
            breakdict[be.uuid] = be
            qual = 'I' * len(be.consensus)
            out.write('>%s\n%s\n+\n%s\n' % (be.uuid, be.consensus, qual))
 
    tmp_sam = '.'.join(tmp_fa.split('.')[:-1]) + '.sam'

    FNULL = open(os.devnull, 'w')

    with open(tmp_sam, 'w') as out:
        sam_cmd  = ['bwa', 'mem', '-k', '10', '-w', '500', '-M', '-v', '0', db, tmp_fa]
        p = subprocess.Popen(sam_cmd, stdout=subprocess.PIPE, stderr=FNULL)

        for line in p.stdout:
            out.write(line.decode())
 
    sam = pysam.AlignmentFile(tmp_sam)
 
    passed_parse = False

    # rarely, pysam will fail to parse the bwa-mem generated SAM and I haven't worked out why... workaround for now
    while not passed_parse:
        try:
            for i, read in enumerate(sam.fetch(until_eof=True)):
                breakdict[read.qname].mappings.append(read)
            passed_parse = True

        except IOError as e:
            sys.stderr.write("warning: pysam failed parse at read %d, modifying file and re-try...\n" % i)
            sam.close()
            lines = []
            with open(tmp_sam, 'r') as sam:
                lines = [line for line in sam]

            with open(tmp_sam, 'w') as sam:
                for n, line in enumerate(lines):
                    if line.startswith('@'): n -= 1 # header
                    if n < i: sam.write(line)

            sam = pysam.AlignmentFile(tmp_sam)

    sam.close()

    os.remove(tmp_fa)
    os.remove(tmp_sam)

    return breakdict.values()


def build_last_db(fa):
    ''' make db for LAST alignments '''
    subprocess.call(['lastdb', '-s', '4G', fa, fa])
    if not os.path.exists(fa + '.tis'):
        sys.stderr.write('Warning: could not lastdb -4G %s %s\n' % (fa, fa))


def align_last(fa, db, e=20):
    ''' returns list of LASTResult objects '''
    if not os.path.exists(db + '.tis'):
        sys.stderr.write('Warning: no lastdb index for %s\n' % db)
        return []

    last_cmd = ['lastal', '-e', str(e), db, fa]

    la_lines   = []
    la_results = []

    p = subprocess.Popen(last_cmd, stdout=subprocess.PIPE)

    for line in p.stdout:
        line = line.decode()
        if not line.startswith('#'):
            if line.strip() != '':
                la_lines.append(line.strip())

            else:
                la_results.append(LASTResult(la_lines))
                la_lines = []

    return la_results


def score_breakend_pair(be1, be2, k=2.5, s=3.0):
    ''' assign a score to a breakend, higher is "better" '''
    prox1 = be1.proximal_subread()
    prox2 = be2.proximal_subread()

    if prox1 and prox2:
        prox1 = prox1[0]
        prox2 = prox2[0]
        overlap = abs(min(0, ref_dist(prox1, prox2))) # overlap = negative distance between proximal read mappings i.e. potential TSD
        weighted_overlap = ss.gamma(k, scale=s).pdf(overlap) * float(overlap)**2 # TSD length distribution taken into account
        distance_penalty = 0

        if overlap > 0:  distance_penalty = abs(abs(be1.breakpos-be2.breakpos) - overlap) # disagreement in TSD length
        if overlap == 0: distance_penalty = abs(be1.breakpos-be2.breakpos) # no TSD

        score = weighted_overlap - distance_penalty + len(be1) + len(be2)

        return score
 
    return None


def checkref(ref_fasta):
    assert os.path.exists(ref_fasta), 'reference not found: %s' % ref_fasta
    assert os.path.exists(ref_fasta + '.fai'), 'please run samtools faidx on %s' % ref_fasta
    assert os.path.exists(ref_fasta + '.bwt'), 'please run bwa index on %s' % ref_fasta


def build_insertions(breakends, maxdist=100):
    ''' return list of Insertion objects '''
    insertions = []
    be_dict = dict([(be.uuid, be) for be in breakends])

    be_itree = Intersecter() # interval tree

    for be in breakends:
        be_itree.add_interval(Interval(be.breakpos-maxdist, be.breakpos+maxdist, value=be.uuid))

    pair_scores = []

    checked_pairs = {}

    for be1 in breakends:
        for be2_coords in be_itree.find(be1.breakpos, be1.breakpos+1):
            be2 = be_dict[be2_coords.value]

            pair_name = '-'.join(sorted((be1.uuid, be2.uuid)))

            if be1.uuid != be2.uuid and be1.direction != be2.direction and pair_name not in checked_pairs:
                if abs(be1.breakpos-be2.breakpos) <= maxdist:
                    pair_scores.append((be1.uuid, be2.uuid, score_breakend_pair(be1, be2)))

            checked_pairs[pair_name] = True

    # sort breakends by score, descending
    pair_scores = [score for score in pair_scores if score[2] is not None]
    pair_scores.sort(key=itemgetter(2),reverse=True)

    used = {} # each breakend can only be used once
    for be1_uuid, be2_uuid, score in pair_scores:
        if be1_uuid not in used and be2_uuid not in used:
            insertions.append(Insertion(be_dict[be1_uuid], be_dict[be2_uuid]))

            used[be1_uuid] = True
            used[be2_uuid] = True

    # single-end detections
    for be in breakends:
        if be.uuid not in used:
            insertions.append(Insertion(be))
            used[be.uuid] = True
     
    return insertions


def minia(fq, tmpdir='/tmp', rescue_asm=False):
    ''' sequence assembly '''
    # minia temp files don't seem to be compatabile with concurrency, workaround w/ temp cwd
    oldcwd = os.getcwd()
    tmpcwd = '%s/%s' % (tmpdir, 'tebreak.'+str(uuid4()))
    os.mkdir(tmpcwd)
    assert os.path.exists(tmpcwd), 'cannot create temp dir: %s' % tmpcwd

    os.chdir(tmpcwd)

    if not os.path.exists(fq):
        fq = oldcwd + '/' + fq

    ctg_fa_list = []

    for param in ('1', '3'):
        ctgbase = tmpdir + '/tebreak.minia.%s' % str(uuid4())

        cmd = ['minia', '-in', fq, '-abundance-min', param, '-no-length-cutoff', '-nb-cores', '1', '-out', ctgbase]

        FNULL = open(os.devnull, 'w')
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=FNULL)
        for line in p.stdout:
            line = line.decode()
            if line.strip().startswith('max_length'):
                max_len = int(line.strip()[-1]) 

        if os.path.exists(ctgbase + '.h5'):
            os.remove(ctgbase + '.h5')

        if os.path.exists(ctgbase + '.contigs.fa'):
            ctg_fa = ctgbase + '.contigs.fa'

            if rescue_asm:
                ctg_fa = asm_rescue(ctg_fa)

            ctg_fa_list.append(ctg_fa)

    os.chdir(oldcwd)
    os.rmdir(tmpcwd)

    return concat_fa(ctg_fa_list, tmpdir=tmpdir)


def build_mask(bedfile, logger):
    ''' return a dictionary of interval trees '''
    forest = dd(Intersecter)

    with open(bedfile, 'r') as bed:
        for line in bed:
            if line.startswith('#'):
                continue

            chrom, start, end = line.strip().split()[:3]
            start = int(start)
            end   = int(end)

            try:
                forest[chrom].add_interval(Interval(start, end))
            except Exception as e:
                logger.exception('bx interval tree crashed on interval %s:%d-%d' % (chrom, start, end))
                

    return forest


def avgmap(maptabix, chrom, start, end):
    ''' return average mappability across chrom:start-end region; maptabix = pysam.Tabixfile'''
    scores = []

    if None in (start, end): return None

    if chrom in maptabix.contigs:
        for rec in maptabix.fetch(chrom, int(start), int(end)):
            mchrom, mstart, mend, mscore = rec.strip().split()
            mstart, mend = int(mstart), int(mend)
            mscore = float(mscore)

            while mstart < mend and mstart:
                mstart += 1
                if mstart >= int(start) and mstart <= int(end):
                    scores.append(mscore)

        if len(scores) > 0:
            return sum(scores) / float(len(scores))
        else:
            return 0.0
    else:
        return 0.0


def getVAF(bam, chrom, poslist):
    ''' return number of reads supporting alt (insertion), ref (reference) and vaf (variant allele fraction) '''
    poslist = list(map(int, poslist))
    alt, ref = break_count(bam, chrom, poslist)
    vaf = 0.0 

    if float(ref+alt) > 0:
        vaf = float(alt)/float(alt+ref)

    return alt, ref, vaf


def break_count(bam, chrom, poslist, minpad=5, flex=1, minmapq=10):
    ''' ref = number of reads spanning TSD, alt = number of reads clipped at breakpoint in poslist '''
    altcount = 0
    refcount = 0
    discards = 0

    tsd_start = min(poslist)
    tsd_end   = max(poslist)

    tsd_len = tsd_end - tsd_start

    if tsd_start < minpad: tsd_start = minpad

    for read in bam.fetch(chrom, tsd_start-minpad, tsd_end+minpad):
        if read.is_unmapped or read.is_duplicate:
            continue

        if read.mapq < minmapq:
            continue

        rclip = len(read.seq) - read.query_alignment_end 
        lclip = read.query_alignment_start

        rbreak = 0
        lbreak = 0

        if rclip > max(tsd_len, minpad):
            rbreak = read.reference_end

        if lclip > max(tsd_len, minpad):
            lbreak = read.reference_start

        support_alt = False

        for pos in poslist: # does this read support a breakpoint in the list?
            if (rbreak >= pos-flex and rbreak <= pos+flex) or (lbreak >= pos-flex and lbreak <= pos+flex):
                support_alt = True

        if support_alt:
            altcount += 1

        else:
            if read.alen == len(read.seq):
                if read.reference_start < tsd_start and read.reference_end > tsd_end: # span TSD
                    refcount += 1


    return altcount, refcount


def guess_minqual(bam):
    minscore = None
    n = 0

    for read in bam.fetch():
        n += 1
        m = min([ord(q) for q in list(read.qual)])
        if minscore is None or minscore > m:
            minscore = m

        if n > 10000: break

    return minscore


def summarise_insertion(ins):
    ''' returns a pickleable version of the insertion information '''
    pi = dd(dict)

    pi['INFO'] = ins.info
    pi['READSTORE'] = ins.fastqrecs

    return pi


def filter_insertions(insertions, filters, tmpdir='/tmp', debug=True, logger=None):
    filtered = []
    if logger is None: debug = False

    for ins in insertions:
        ins_debug_name = '%s:%d-%d' % (ins.be1.chrom, ins.min_supporting_base(), ins.max_supporting_base())

        exclude = False

        mapq = list(map(lambda x : int(x.mapq), ins.be1.proximal_subread()))
        rgs  = [rg.split('|')[0] for rg in ins.be1.cluster.readgroups()]
        bams = [bam.split('|')[0] for bam in ins.be1.cluster.bamfiles()]

        if ins.be2 is not None and len(ins.be2.proximal_subread()) > 0:
            mapq += list(map(lambda x : int(x.mapq), ins.be2.proximal_subread()))
            rgs  += [rg.split('|')[0] for rg in ins.be2.cluster.readgroups()]
            bams += [bam.split('|')[0] for bam in ins.be2.cluster.bamfiles()]

        bams = list(set(bams)) # uniqify

        if len(ins) >= filters['max_ins_reads']:
            if debug: logger.debug('%s downsampled due to max_ins_reads: %d >= %d' % (ins_debug_name, len(ins), filters['max_ins_reads']))
            #exclude = True

        if max(mapq) < filters['min_prox_mapq']:
            if debug: logger.debug('%s filtered due to min_prox_mapq: %d < %d' % (ins_debug_name, max(mapq), filters['min_prox_mapq']))
            exclude = True

        if ins.num_sr() < filters['min_split_reads']:
            if debug: logger.debug('%s filtered due to min_split_reads: %d < %d' % (ins_debug_name, ins.num_sr(), filters['min_split_reads']))
            exclude = True

        if filters['exclude_bam']:
            for bam in bams:
                if bam in filters['exclude_bam']:
                    if debug: logger.debug('%s filtered due to bam %s in exclude_bam' % (ins_debug_name, bam))
                    exclude = True

        if filters['exclude_readgroup']:
            for rg in rgs:
                if rg in filters['exclude_rg']:
                    if debug: logger.debug('%s filtered due to readgroup %s in exclude_readgroup' % (ins_debug_name, rg))
                    exclude = True

        if filters['max_bam_count'] > 0:
            if len(bams) > filters['max_bam_count']:
                if debug: logger.debug('%s filtered due to max_bam_count: %d > %d' % (ins_debug_name, len(bams), filters['max_bam_count']))
                exclude = True

        if filters['map_tabix'] is not None and not exclude:
            if ins.be1.chrom in filters['map_tabix'].contigs:
                ins.mappability = avgmap(filters['map_tabix'], ins.be1.chrom, ins.min_supporting_base(), ins.max_supporting_base())
            else:
                ins.mappability = 0.0

            if ins.mappability < filters['min_mappability']:
                if debug: logger.debug('%s filtered due to min_mappability: %d < %d' % (ins_debug_name, ins.mappability, filters['min_mappability']))
                exclude = True

        if not exclude: filtered.append(ins)

    return filtered


def postprocess_insertions(insertions, filters, bwaref, bams, tmpdir='/tmp', genotype=True, rescue_asm=False):
    for ins in insertions:
        support_fq  = ins.supportreads_fastq(tmpdir, limit=filters['max_ins_reads'])
        if support_fq is None: return insertions

        cwd = os.getcwd()
        support_asm = minia(support_fq, tmpdir=tmpdir, rescue_asm=rescue_asm)

        retry_counter = 0 # minia might not be the most reliable option...
        while not os.path.exists(support_asm) and retry_counter < 10:
            retry_counter += 1
            sys.stderr.write('***Assembly retry: %s:%d\n' % (ins.be1.chrom, ins.be1.breakpos))
            support_asm = minia(support_fq, tmpdir=tmpdir, rescue_asm=rescue_asm)

        if not os.path.exists(support_asm):
            sys.stderr.write('***Assembly failed!: %s:%d\n' % (ins.be1.chrom, ins.be1.breakpos))

        else:
            ins.improve_consensus(support_asm, bwaref, tmpdir=tmpdir)

        if os.path.exists(support_fq): os.remove(support_fq)
        if os.path.exists(support_asm): os.remove(support_asm)

        if os.getcwd() != cwd: os.chdir(cwd)

    # collect altered breakends
    alt_be_list = []
    for ins in insertions:
        if ins.be1_improved_cons: alt_be_list.append(ins.be1)
        if ins.be2_improved_cons: alt_be_list.append(ins.be2)

    remap_be_dict = {}
    for be in map_breakends(alt_be_list, bwaref, tmpdir=tmpdir):
        remap_be_dict[be.uuid] = be

    for ins in insertions:
        # use previous mapping if new consensus did not map
        if ins.be1_improved_cons:
            if ins.be1.uuid in remap_be_dict:
                if len(remap_be_dict[ins.be1.uuid].proximal_subread()) > 0:
                    ins.be1 = remap_be_dict[ins.be1.uuid]
                else:
                    ins.be1 = ins.be1_alt
                    ins.be1_improved_cons = False

        if ins.be2_improved_cons:
            if ins.be2.uuid in remap_be_dict:
                if len(remap_be_dict[ins.be2.uuid].proximal_subread()) > 0:
                    ins.be2 = remap_be_dict[ins.be2.uuid]
                else:
                    ins.be2 = ins.be2_alt
                    ins.be2_improved_cons = False

        if ins.be1_improved_cons or ins.be2_improved_cons:
            ins.compile_info(bams, genotype=genotype)

    return insertions



def run_chunk(args, bamlist, chrom, start, end):
    logger = logging.getLogger(__name__)
    if args.debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    chunkname = '%s:%d-%d' % (chrom, start, end)

    try:
        for _ in range(5):
            try:
                bams = [pysam.AlignmentFile(bam, 'rb') for bam in bamlist]
                break

            except IOError:
                logger.warning("IOError trying to read input BAMs, retry in 5s...")
                time.sleep(5)


        # table of minimum quality scores
        minqual = {}

        # would do this outside but can't pass a non-pickleable object
        if args.mask is not None:
            for _ in range(5):
                try:
                    args.mask = build_mask(args.mask, logger)
                    break

                except IOError:
                    logger.warning("IOError trying to read %s, retry in 5s..." % args.mask)
                    time.sleep(5)


        if args.map_tabix is not None:
            for _ in range(5):
                try:
                    args.map_tabix = pysam.Tabixfile(args.map_tabix)
                    break

                except IOError:
                    logger.warning("IOError trying to read %s, retry in 5s..." % args.map_tabix)
                    time.sleep(5)
                

        start = int(start)
        end   = int(end)

        filters = {
            'min_maxclip':           int(args.min_maxclip),
            'min_minclip':           int(args.min_minclip),
            'min_sr_per_break':      int(args.min_sr_per_break),
            'min_consensus_score':   float(args.min_consensus_score),
            'min_MW_P':              float(args.minMWP),
            'max_ins_reads':         int(args.max_ins_reads),
            'min_split_reads':       int(args.min_split_reads),
            'min_prox_mapq':         int(args.min_prox_mapq),
            'max_N_consensus':       int(args.max_N_consensus),
            'exclude_bam':           [],
            'exclude_readgroup':     [],
            'max_bam_count':         int(args.max_bam_count),
            'genome_mask':           args.mask,
            'map_tabix':             args.map_tabix,
            'min_mappability':       float(args.min_mappability)
        }

        if args.exclude_bam is not None: filters['exclude_bam'] = list(map(os.path.basename, args.exclude_bam.split(',')))
        if args.exclude_readgroup is not None: filters['exclude_readgroup'] = args.exclude_readgroup.split(',')

        insertions = []
     
        logger.debug('Processing chunk: %s ...' % chunkname)
        logger.debug('Chunk %s: Parsing split reads from bam(s): %s ...' % (chunkname, args.bam))
        sr = fetch_clipped_reads(bams, chrom, start, end, filters, logger=logger, limit=int(args.clip_limit))

        sr.sort()

        logger.debug('Chunk %s: Building clusters from %d split reads ...' % (chunkname, len(sr)))
        clusters = build_sr_clusters(sr)
    
        logger.debug('Chunk %s: Building breakends...' % chunkname)

        breakends = []

        for cluster in clusters:
            cl_min, cl_max = cluster.find_extrema()
            breakends += build_breakends(cluster, filters, tmpdir=args.tmpdir)

        logger.debug('Chunk %s: Mapping %d breakends ...' % (chunkname, len(breakends)))
        if len(breakends) > 0:
            breakends = map_breakends(breakends, args.bwaref, tmpdir=args.tmpdir)

            logger.debug('Chunk %s: Building insertions...' % chunkname)

            insertions = build_insertions(breakends)

            insertions = [ins for ins in insertions if len(ins.be1.proximal_subread()) > 0] # remove bogus insertions

            logger.debug('Chunk %s: Processing and filtering %d potential insertions ...' % (chunkname, len(insertions)))

            insertions = filter_insertions(insertions, filters, tmpdir=args.tmpdir, logger=logger)

            for ins in insertions:
                ins_debug_name = '%s:%d-%d' % (ins.be1.chrom, ins.min_supporting_base(), ins.max_supporting_base())
                if int(args.min_disc_reads) > 0:
                    logger.debug('Chunk: %s, fetch discordant mates for insertion %s ...' % (chunkname, ins_debug_name))
                    ins.fetch_discordant_reads(bams, logger=logger, max_fetch=int(args.max_disc_fetch))
                ins.compile_info(bams, genotype=True)

            logger.debug('Chunk %s: Postprocessing %d filtered insertions, trying to improve consensus breakend sequences ...' % (chunkname, len(insertions)))
            processed_insertions  = postprocess_insertions(insertions, filters, args.bwaref, bams, tmpdir=args.tmpdir, rescue_asm=args.rescue_asm)

            logger.debug('Chunk %s: Summarising insertions ...' % chunkname)
            summarised_insertions = [summarise_insertion(ins) for ins in processed_insertions]

            logger.debug('Finished chunk: %s' % chunkname)

            for bam in bams: bam.close()
            return summarised_insertions

        else:
            for bam in bams: bam.close()
            return []

    except Exception as e:
        sys.stderr.write('*'*60 + '\nencountered error in chunk: %s\n' % chunkname)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.write("*"*60 + "\n")

        return []


def resolve_duplicates(insertions):
    ''' resolve instances where breakpoints occur > 1x in the insertion list '''
    ''' this can happen if intervals overlap, e.g. in  genome chunking '''

    insdict = od() # --> index in insertions

    for n, ins in enumerate(insertions):
        be1 = ins['INFO']['chrom'] + ':' + str(ins['INFO']['be1_breakpos'])
        be2 = ins['INFO']['chrom'] + ':' + str(ins['INFO']['be2_breakpos'])
        
        if be1 not in insdict:
            insdict[be1] = n 
            insdict[be2] = n
        
        else:
            if prefer_insertion(ins, insertions[insdict[be1]]):
                insdict[be1] = n
                insdict[be2] = n

    return [insertions[n] for n in list(set(insdict.values()))]


def prefer_insertion(ins1, ins2):
    ''' return true if ins1 has more evidence than ins2, false otherwise '''
    # prefer two-end support over one end
    if ins1['INFO']['be1_breakpos'] != ins1['INFO']['be2_breakpos'] and ins2['INFO']['be1_breakpos'] == ins2['INFO']['be2_breakpos']:
        return True

    # prefer higher split read count
    if ins1['INFO']['be1_sr_count'] + ins1['INFO']['be2_sr_count'] > ins2['INFO']['be1_sr_count'] + ins2['INFO']['be2_sr_count']:
        return True

    # prefer higher discordant read count
    if ins1['INFO']['dr_count'] > ins2['INFO']['dr_count']:
        return True

    return False


def text_summary(insertions, cmd=None, outfile='tebreak.out'):
    with open(outfile, 'w') as out:
        if cmd:
            out.write('##CMD: %s\n' % cmd)
        for ins in insertions:
            if ins is not None:
                out.write('#BEGIN\n')
                for label, value in ins['INFO'].items():
                    out.write('%s: %s\n' % (label, str(value)))
                out.write('#END\n')

                out.write('\n')


## imported from discocluster.py
def flip(strand):
    if strand == '+': return '-'
    if strand == '-': return '+'


def interval_forest(bed_file):
    ''' build dictionary of interval trees '''
    forest = dd(Intersecter)

    with open(bed_file, 'r') as bed:
        for line in bed:
            chrom, start, end = line.strip().split()[:3]
            label = '|'.join(line.strip().split())
            forest[chrom].add_interval(Interval(int(start), int(end), value=label))

    return forest


def read_gen(bam, chrom=None, start=None, end=None):
    if None in (chrom, start, end):
        for read in bam.fetch():
            yield read

    else:
        for read in bam.fetch(chrom, start, end):
            yield read


def disco_get_coords(forest, bams, logger, chrom=None, start=None, end=None, min_mapq=1, min_dist=10000):
    coords = []

    for bam in bams:
        tick = 10000000
        try:
            tick = int((bam.mapped + bam.unmapped) * 0.01)
            if tick == 0: tick = 1
            logger.debug('outputting status every %d reads (1 pct)' % tick)

        except ValueError as e:
            logger.debug('no index found, outputting status every %d reads' % tick)

        for i, read in enumerate(read_gen(bam, chrom=chrom, start=start, end=end)):
            if not read.is_unmapped and not read.mate_is_unmapped and not read.is_duplicate:

                rchrom = bam.getrname(read.reference_id)
                rstart = read.reference_start
                rend   = read.reference_end

                rstr = '+'
                if read.is_reverse: rstr = '-'

                mdist = abs(read.reference_start-read.next_reference_start)
                if read.reference_id != read.next_reference_id: mdist=3e9

                if read.mapq >= min_mapq and mdist >= min_dist:
                    mchrom = bam.getrname(read.next_reference_id)
                    mstart = read.next_reference_start
                    mend   = mstart + len(read.seq)

                    mstr = '+'
                    if read.mate_is_reverse: mstr = '-'

                    if mchrom in forest:
                        for rec in forest[mchrom].find(mstart, mend):
                            coords.append(DiscoCoord(rchrom, rstart, rend, rstr, mchrom, mstart, mend, mstr, rec.value, os.path.basename(bam.filename)))
                            break

            if i % tick == 0:
                if read.is_unmapped:
                    logger.debug('parsed %d reads, last position unmapped' % i)
                else:
                    logger.debug('parsed %d reads, last position: %s:%d' % (i, bam.getrname(read.tid), read.pos))

    return coords


def disco_infer_strand(cluster):
    c1 = [c.strand for c in cluster]
    c2 = [c.mstrand for c in cluster]

    if c1[0] == c2[0] and c1[-1] == c2[-1]: return '-'
    if c1[0] != c2[0] and c1[-1] != c2[-1]: return '+'

    return 'NA'


def disco_output_cluster(cluster, forest, mapping, min_size=4):
    if len(cluster) >= min_size:
        cluster_chrom = cluster[0].chrom
        cluster_start = cluster[0].start
        if cluster_start < 0: cluster_start = 0

        cluster_end = cluster[-1].end

        bamlist = ','.join(list(set([c.bam for c in cluster])))

        return DiscoInsCall(cluster, cluster_chrom, cluster_start, cluster_end, disco_infer_strand(cluster), bamlist)


def disco_cluster(forest, coords, mapping, min_size=4, max_spacing=250):
    coords.sort()

    cluster = []
    insertion_list = []

    for c in coords:
        if len(cluster) == 0:
            cluster = [c]
        else:
            if c.chrom == cluster[-1].chrom and c.start - cluster[-1].end <= max_spacing:
                cluster.append(c)
            else:
                i = disco_output_cluster(cluster, forest, mapping, min_size=min_size)
                if i is not None:
                    insertion_list.append(i)

                cluster = [c]

    i = disco_output_cluster(cluster, forest, mapping, min_size=min_size)
    if i is not None:
        insertion_list.append(i)

    return insertion_list


def disco_run_chunk(args, chunk):
    ''' chunk is a list of (chrom, start, end) tuples '''
    logger = logging.getLogger(__name__)
    if args.debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    try:
        bams = []

        if args.bam.endswith('.bam'):
            bams = [pysam.AlignmentFile(bam, 'rb') for bam in args.bam.split(',')]

        elif args.bam.endswith('.txt'):
            with open(args.bam, 'r') as bamtxt:
                for line in bamtxt:
                    bams.append(pysam.AlignmentFile(line.strip(), 'rb'))

        mapping = None
        if args.map_tabix is not None:
            mapping = pysam.Tabixfile(args.map_tabix)

        logger.debug('building interval trees for %s' % args.disco_target)
        forest = interval_forest(args.disco_target)

        coords = []

        chrom, start, end = chunk

        logger.debug('%s:%d-%d: fetching coordinates from %s' % (chrom, start, end, args.bam))

        coords += disco_get_coords(forest, bams, logger, chrom=chrom, start=start, end=end)

        logger.debug('%s:%d-%d: found %d anchored reads' % (chrom, start, end, len(coords)))

        return disco_cluster(forest, coords, mapping, min_size=int(args.min_disc_reads))

    except Exception as e:
        sys.stderr.write('*'*60 + '\nencountered error in chunk: %s\n' % list(map(str, chunk)))
        traceback.print_exc(file=sys.stderr)
        sys.stderr.write("*"*60 + "\n")

        return []


def disco_resolve_dups(ins_list):
    ''' resolve cases where the same insertion has been called in multiple chunks '''
    ins_list.sort()
    new_list = []

    last = None

    for ins in ins_list:
        if last is None:
            last = ins

        elif last.overlaps(ins):
            if ins.length > last.length:
                last = ins

        else:
            new_list.append(last)
            last = ins

    if last is not None:
        new_list.append(last)

    return new_list


def tebreak(args):
    logger.info("commandline: %s" % ' '.join(sys.argv))

    checkref(args.bwaref)

    if not args.skipshm:
        logger.info("loading bwa index %s into shared memory ..." % args.bwaref)
        p = subprocess.Popen(['bwa', 'shm', args.bwaref], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for line in p.stdout: pass # wait for bwa to load
        logger.info("loaded.")

    ''' Chunk genome or use input BED '''
    
    skip_chroms = {}
    if args.skip_chroms is not None:
        with open(args.skip_chroms, 'r') as _:
            for line in _:
                skip_chroms[line.strip().split()[0]] = True

    procs = int(args.processes)
    chunk_count = int(args.chunks)

    if chunk_count < procs: chunks = procs

    pool = mp.Pool(processes=procs)

    genome = Genome(args.bwaref + '.fai', skip_chroms)

    chunks = []
    bamlist = []

    if args.bam.endswith('.bam'):
        bamlist = args.bam.split(',')

    elif args.bam.endswith('.txt'):
        with open(args.bam, 'r') as bamtxt:
            for line in bamtxt:
                bamlist.append(line.strip())

    else:
        sys.exit('Unrecognised input to -b/--bam: %s, must be .bam or .txt' % args.bam)

    if len(bamlist) == 0:
        sys.exit('No entries in -b/--bam input .txt: %s' % args.bam)

    if args.interval_bed is None:
        chunks = genome.chunk(chunk_count, sorted=True, pad=5000)

        if args.disco_target is not None:
            logger.info('discordant targets in: %s' % args.disco_target)
            chunks = genome.chunk(procs, pad=5000)

            chunkout = re.sub('.bam$', '.tebreak.chunklist.out', os.path.basename(bamlist[0]))

            with open(chunkout, 'w') as bugout:
                for c in chunks:
                    bugout.write('\t'.join(map(str, c)) + '\n')

            reslist = []
            for i, chunk in enumerate(chunks, 1):
                res = pool.apply_async(disco_run_chunk, [args, chunk])
                reslist.append(res)

            ins_list = []
            for res in reslist:
                ins_list += res.get()

            ins_list = disco_resolve_dups(ins_list)

            chunks = []

            discfn = re.sub('.bam$', '.tebreak.disc.txt', os.path.basename(bamlist[0]))

            if args.disc_out:
                discfn = args.disc_out

            with open(discfn, 'w') as disc_out:
                for i in ins_list:
                    disc_out.write(i.out(pad=500) + '\n')
                    chunks.append((i.chrom, i.start-500, i.end+500))

            if args.disc_only:
                sys.exit('quitting due to --disc_only, discordant cluster locations are in %s' % discfn)


    if args.interval_bed is not None:
        logger.info('interval .bed: %s' % args.interval_bed)
        with open(args.interval_bed, 'r') as bed:
            chunks = [(line.strip().split()[0], int(line.strip().split()[1]), int(line.strip().split()[2])) for line in bed]

        if args.disco_target is not None:
            logger.info('discordant targets in: %s' % args.disco_target)
            reslist = []
            for i, chunk in enumerate(chunks, 1):
                res = pool.apply_async(disco_run_chunk, [args, chunk])
                reslist.append(res)

            ins_list = []
            for res in reslist:
                ins_list += res.get()

            ins_list = disco_resolve_dups(ins_list)

            chunks = []

            discfn = re.sub('.bam$', '.tebreak.disc.txt', os.path.basename(bamlist[0]))

            if args.disc_out:
                discfn = args.disc_out

            with open(discfn, 'w') as disc_out:
                for i in ins_list:
                    disc_out.write(i.out(pad=500) + '\n')
                    chunks.append((i.chrom, i.start-500, i.end+500))

            if args.disc_only:
                sys.exit('quitting due to --disc_only, discordant cluster locations are in %s' % discfn)


    logger.info("genome chunk count: %d" % len(chunks))

    reslist = []

    pct = int(len(chunks)*.01)+1

    for i, chunk in enumerate(chunks, 1):
        res = pool.apply_async(run_chunk, [args, bamlist, chunk[0], chunk[1], chunk[2]])
        reslist.append(res)

    insertions = []
    for res in reslist:
        insertions += res.get()

    insertions = resolve_duplicates(insertions)

    detailfn = re.sub('.bam$', '.tebreak.detail.out', os.path.basename(bamlist[0]))

    if args.detail_out is not None:
        detailfn = args.detail_out

    text_summary(insertions, cmd=' '.join(sys.argv), outfile=detailfn)

    logger.info('Wrote detail output to %s' % detailfn)

    pickoutfn = re.sub('.bam$', '.tebreak.pickle', os.path.basename(bamlist[0]))

    if args.pickle is not None:
        pickoutfn = args.pickle

    with open(pickoutfn, 'wb') as pickout:
        pickle.dump(insertions, pickout)

    logger.info('Pickled to %s' % pickoutfn)

    return pickoutfn


## functions used by resolve() ##

def filter(ins, args):
    passed = True

    if 'filter' not in ins.ins:
        ins.ins['filter'] = []

    if ins.end3 == ins.end5 == 'NA':
        passed = False
        ins.ins['filter'].append('no_ends')

    if ins.out['Insert_Consensus_5p'] == ins.out['Insert_Consensus_3p'] == 'NA':
        passed = False
        ins.ins['filter'].append('insert_consensus')

    prox_len = [len(ins.ins['be1_prox_seq'])]
    if 'be2_prox_seq' in ins.ins:
        prox_len.append(len(ins.ins['be2_prox_seq']))

    if int(ins.out['3p_Cons_Len']) + int(ins.out['5p_Cons_Len']) < int(args.min_cons_len):
        passed=False
        ins.ins['filter'].append('min_cons_len')

    if max(prox_len) < 20:
        passed = False
        ins.ins['filter'].append('prox_len')

    if not ins.end_align_flush():
        passed = False
        ins.ins['filter'].append('end_align_flush')

    ins.ins['passedfilter'] = passed

    return ins


def extend_consensus(ins, bam):
    ''' extend consensus sequence using teref pileup '''

    ctglen = dict(zip(bam.references, bam.lengths))

    covered_segs = get_covered_segs(bam.filename.decode(), mindepth=2)

    mq = guess_minqual(bam)

    if len(covered_segs) > 0:

        for be in ('be1', 'be2'):
            if be+'_cons_seq' in ins['INFO'] and ins['INFO'][be+'_cons_seq'] is not None:
                seg = None

                if (ins['INFO']['be1_is_3prime'] and be == 'be1') or (ins['INFO']['be2_is_3prime'] and be == 'be2'):
                    seg = covered_segs[-1]

                else: # 5'
                    seg = covered_segs[0]

                seqs = [unequal_qualtrim(read, ctglen[seg['chrom']], minqual=mq) for read in bam.fetch(seg['chrom'], seg['start'], seg['end'])]
                seqs = [s for s in seqs if len(s) > 50]

                best_cons_seq = 'NA'
                best_cons_score = 0.0

                for sc_thresh in [0.95, 0.92]:
                    te_cons_seq, te_cons_score = consensus(seqs, minscore=sc_thresh)
                    if len(te_cons_seq)*te_cons_score**2 > len(best_cons_seq)*best_cons_score**2:
                        best_cons_seq = te_cons_seq
                        best_cons_score = te_cons_score

                ins['INFO'][be+'_te_cons_score'] = best_cons_score
                ins['INFO'][be+'_te_cons_seq'] = best_cons_seq

    return ins



def get_covered_segs(bam, mindepth=1, minlength=50):
    ''' return covered segments from BAM file '''
    seglist = []
    cmd = ['samtools', 'mpileup', bam]
    
    FNULL = open(os.devnull, 'w')

    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=FNULL)

    seg = {'chrom': None, 'start': None, 'end': None}

    for line in p.stdout:
        line = line.decode()
        chrom, pos, base, depth = line.strip().split()[:4]

        depth = int(depth)
        pos   = int(pos)

        if seg['chrom'] != chrom:
            if seg['chrom'] is not None:
                if seg['end'] - seg['start'] >= minlength:
                    seglist.append(seg)

            seg = {'chrom': chrom, 'start': pos, 'end': pos}

        else:
            if pos == seg['end']+1 and depth > mindepth:
                seg['end'] = pos

            else:
                if seg['end'] - seg['start'] >= minlength:
                    seglist.append(seg)

                seg = {'chrom': chrom, 'start': pos, 'end': pos}

    if seg['chrom'] is not None and seg['end'] - seg['start'] >= minlength:
        seglist.append(seg)

    return seglist


def unequal_qualtrim(read, ctglen, chopclip=False, minqual=35):
    ''' return quality-trimmed sequence given a pysam.AlignedSegment '''

    seq = read.seq
    qual = read.qual

    if chopclip:
        if read.reference_end < ctglen:
            seq = seq[:read.query_alignment_end]
            qual = qual[:read.query_alignment_end]

        if read.reference_start > 1:
            seq = seq[read.query_alignment_start:]
            qual = qual[read.query_alignment_start:]


    q = [ord(b)-minqual for b in list(qual)]

    for i in range(0,len(q)-4): # sliding window, 4bp
        if np.mean(q[i:i+4]) < 5:
            return seq[:i]

    return seq


def consensus(seqs, minscore=0.9):
    ''' build consensus from sorted aligned reads iteratively, expects seqs to be sorted in ref genome order '''

    if len(seqs) == 0:
        return '', 0.0

    if len(seqs) == 1: # no consensus necessary
        return seqs[0], 1.0

    uniq_seqs = [seqs[0]]
    for i, seq in enumerate(seqs[1:], start=1):
        if seq != seqs[i-1]:
            uniq_seqs.append(seq)

    if len(uniq_seqs) == 1: # all seqs were the same!
        return uniq_seqs[0], 1.0

    start_index = 0
    cons = uniq_seqs[start_index]
    scores = []

    align_init = False

    for i, seq in enumerate(uniq_seqs[1:]):
        cons = cons.replace('N','A')
        seq = seq.replace('N', 'A')

        s1 = skseq.DNA(cons)
        s2 = skseq.DNA(seq)

        try:
            aln_res = skalign.local_pairwise_align_ssw(s1, s2)
        except IndexError:
            continue

        aln_tab = aln_res[0]

        s1_aln, s2_aln = aln_res[2]

        a1 = cons[s1_aln[0]:s1_aln[1]+1]

        if len(a1) == 0:
            continue
            
        score = sum(aln_tab.conservation(gap_mode='include')==1.)/aln_tab.shape.position

        scores.append(score)

        if re.search(a1, cons):
            cons_start, cons_end = s1_aln[0], s1_aln[1]+1

            if score >= minscore and cons_end > len(cons)-5:
                align_end = s2_aln[1]+1
                cons += seq[align_end:]
                align_init = True

            elif not align_init: # haven't found a scaffold yet
                start_index += 1
                cons = uniq_seqs[start_index]

    return cons, np.mean(scores)


def best_match(last_results, query_name, req_target=None, min_match=0.9):
    qres = []

    for res in sorted(last_results):
        if res.query_id == query_name:
            if req_target is None or req_target == res.target_id:
                if res.pct_match() > min_match:
                    return res

    return None


def locate_subseq(longseq, shortseq, end='L'):
    ''' return (start, end) of shortseq in longseq '''
    assert len(longseq) >= len(shortseq), 'orient_subseq: %s < %s' % (longseq, shortseq)
 
    matches = [[match.start(0), match.end(0)] for match in re.finditer(shortseq, longseq) if match]

    if len(matches) > 0:
        if end == 'L':
            return sorted(matches[0])
        else:
            return sorted(matches[-1])
 
    return None


def prepare_ref(fasta, refoutdir='tebreak_refs', makeFAI=True, makeBWA=True, makeLAST=True, usecached=False):
    if not os.path.exists(refoutdir):
        os.mkdir(refoutdir)
        assert os.path.exists(refoutdir), 'could not create ref output directory: %s' % refoutdir

    ref_fa = refoutdir + '/' + os.path.basename(fasta)

    if not os.path.exists(ref_fa):
        logger.debug('Copying %s to %s ...' % (fasta, ref_fa))
        shutil.copy(fasta, ref_fa)

    if makeFAI:
        logger.debug('Samtools indexing %s ...' % ref_fa)
        subprocess.call(['samtools', 'faidx', ref_fa])
        assert os.path.exists(ref_fa + '.fai'), 'could not samtools faidx %s' % ref_fa

    if makeBWA:
        logger.debug('Create BWA db for %s ...' % ref_fa)
        p = subprocess.Popen(['bwa', 'index', ref_fa], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for line in p.stdout: pass
        assert os.path.exists(ref_fa + '.bwt'), 'could not bwa index %s' % ref_fa

    if makeLAST:
        if usecached and os.path.exists(ref_fa + '.tis'): return ref_fa
        logger.debug('Create LAST db for %s ...' % ref_fa)
        subprocess.call(['lastdb', '-s', '4G', ref_fa, ref_fa])
        assert os.path.exists(ref_fa + '.tis'), 'could not lastdb -s 4G %s %s' % (ref_fa, ref_fa)

    return ref_fa


def last_alignment(ins, ref_fa, tmpdir='/tmp'):
    tmpfa = tmpdir + '/' + 'tebreak.resolve.%s.fa' % ins['INFO']['ins_uuid']
    with open(tmpfa, 'w') as fa:
        # realign all distal sequences
        if 'be1_dist_seq' in ins['INFO'] and ins['INFO']['be1_dist_seq'] is not None:
            for i, dist_seq in enumerate(ins['INFO']['be1_dist_seq'].split(',')):
                fa.write('>%s|%s\n%s\n' % (ins['INFO']['be1_obj_uuid'],str(i), dist_seq))

        if 'be2_dist_seq' in ins['INFO'] and ins['INFO']['be2_dist_seq'] is not None:
            for i, dist_seq in enumerate(ins['INFO']['be2_dist_seq'].split(',')):
                fa.write('>%s|%s\n%s\n' % (ins['INFO']['be2_obj_uuid'], str(i), dist_seq))

        # realign all unmapped sequences, use negative index (-1-i) as flag for unmapped
        if 'be1_umap_seq' in ins['INFO'] and ins['INFO']['be1_umap_seq'] is not None:
            for i, umap_seq in enumerate(ins['INFO']['be1_umap_seq'].split(',')):
                fa.write('>%s|%s\n%s\n' % (ins['INFO']['be1_obj_uuid'],str(-1-i), umap_seq))

        if 'be2_umap_seq' in ins['INFO'] and ins['INFO']['be2_umap_seq'] is not None:
            for i, umap_seq in enumerate(ins['INFO']['be2_umap_seq'].split(',')):
                fa.write('>%s|%s\n%s\n' % (ins['INFO']['be2_obj_uuid'], str(-1-i), umap_seq))

    # increasing -m accounts for poly-A tails
    cmd = ['lastal', '-e', '20', '-m', '100', ref_fa, tmpfa]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    maf_lines   = []
    maf_results = []

    for line in p.stdout:
        line = line.decode()
        if not line.startswith('#'):
            if line.strip() != '':
                maf_lines.append(line.strip())

            else:
                maf_results.append(LASTResult(maf_lines))
                maf_lines = []

    os.remove(tmpfa)

    return maf_results


def poly_A_frac(seq):
    ''' home much of the sequence is A '''
    if seq is None: return 0.0

    f = []
    for b in ('A', 'T'):
        f.append(len([base for base in list(seq.upper()) if base == b]))

    return float(max(f)) / float(len(seq))


def add_insdata(ins, last_res):
    be1_bestmatch = best_match(last_res, ins['INFO']['be1_obj_uuid'])
    be2_bestmatch = None
    if 'be2_obj_uuid' in ins['INFO'] and ins['INFO']['be2_obj_uuid'] != ins['INFO']['be1_obj_uuid']:
        be2_bestmatch = best_match(last_res, ins['INFO']['be2_obj_uuid'])

    better_match_maxdiff = 5

    if None not in (be1_bestmatch, be2_bestmatch):
        if be1_bestmatch.target_id != be2_bestmatch.target_id:
            logger.debug('UUID %s target mismatch: %s vs %s' % (ins['INFO']['ins_uuid'], be1_bestmatch.target_id, be2_bestmatch.target_id))

            if be1_bestmatch.score > be2_bestmatch.score:
                # try to get be2 target to match be1 target
                if max(poly_A_frac(ins['INFO']['be2_dist_seq']), poly_A_frac(ins['INFO']['be2_umap_seq'])) > 0.75:
                    better_match_maxdiff = 20 # if seq is poly_A make it easy to match

                newmatch = best_match(last_res, ins['INFO']['be2_obj_uuid'], req_target=be1_bestmatch.target_id)
                if newmatch is not None and be2_bestmatch.score - newmatch.score < better_match_maxdiff:
                    be2_bestmatch = newmatch

            else:
                # try to get be1 target to match be2 target
                if max(poly_A_frac(ins['INFO']['be1_dist_seq']), poly_A_frac(ins['INFO']['be1_umap_seq'])) > 0.75:
                    better_match_maxdiff = 20

                newmatch = best_match(last_res, ins['INFO']['be1_obj_uuid'], req_target=be2_bestmatch.target_id)
                if newmatch is not None and be1_bestmatch.score - newmatch.score < better_match_maxdiff:
                    be1_bestmatch = newmatch

            # if targets still don't match and one is clearly polyA, no confidence in polyA end family call.
            if be1_bestmatch.target_id != be2_bestmatch.target_id:
                if be1_bestmatch.only_polyA():
                    be1_bestmatch = None

                if be2_bestmatch.only_polyA():
                    be2_bestmatch = None

                # last-ditch attempt to make the ends match the same reference element
            if None not in (be1_bestmatch, be2_bestmatch) and be1_bestmatch != be2_bestmatch:
                if be1_bestmatch.score > be2_bestmatch.score:
                    be2_bestmatch = best_match(last_res, ins['INFO']['be2_obj_uuid'], req_target=be1_bestmatch.target_id)
                else:
                    be1_bestmatch = best_match(last_res, ins['INFO']['be1_obj_uuid'], req_target=be2_bestmatch.target_id)


    if be1_bestmatch is not None: ins['INFO']['be1_bestmatch'] = be1_bestmatch
    if be2_bestmatch is not None: ins['INFO']['be2_bestmatch'] = be2_bestmatch

    ins = assign_insertion_ends(ins)

    ins['INFO']['ins_length'] = infer_length(ins)

    ins = infer_orientation(ins)

    if be1_bestmatch is not None: ins['INFO']['best_ins_matchpct'] = be1_bestmatch.pct_match()

    if be2_bestmatch is not None:
        if 'best_ins_matchpct' in ins['INFO'] and be2_bestmatch.pct_match() > be1_bestmatch.pct_match():
            ins['INFO']['best_ins_matchpct'] = be2_bestmatch.pct_match()
        elif 'best_ins_matchpct' not in ins['INFO']:
            ins['INFO']['best_ins_matchpct'] = be2_bestmatch.pct_match()

    return ins


def assign_insertion_ends(ins):
    be1 = None
    be2 = None

    if 'be1_bestmatch' in ins['INFO']: be1 = ins['INFO']['be1_bestmatch']
    if 'be2_bestmatch' in ins['INFO']: be2 = ins['INFO']['be2_bestmatch']

    if None not in (be1, be2): # compare to decide 3' end
        ins['INFO']['be1_is_3prime'] = be1.target_start > be2.target_start
        ins['INFO']['be2_is_3prime'] = not ins['INFO']['be1_is_3prime']

    else: # for single end evidence, use coordinates to decide 3' end
        if be1 is not None:
            # 10bp to end of elt reference or has >= 10bp polyA we'll call it 3 prime
            ins['INFO']['be1_is_3prime'] = be1.target_seqsize - (be1.target_start+be1.target_alnsize) < 10
            if not ins['INFO']['be1_is_3prime']: ins['INFO']['be1_is_3prime'] = be1.query_align.endswith('A'*10)
            if not ins['INFO']['be1_is_3prime']: ins['INFO']['be1_is_3prime'] = be1.target_align.endswith('A'*10)
            ins['INFO']['be2_is_3prime'] = not ins['INFO']['be1_is_3prime']

        elif be2 is not None:
            ins['INFO']['be2_is_3prime'] = be2.target_seqsize - (be2.target_start+be2.target_alnsize) < 10
            if not ins['INFO']['be2_is_3prime']: ins['INFO']['be2_is_3prime'] = be2.query_align.endswith('A'*10)
            if not ins['INFO']['be2_is_3prime']: ins['INFO']['be2_is_3prime'] = be2.target_align.endswith('A'*10)
            ins['INFO']['be1_is_3prime'] = not ins['INFO']['be2_is_3prime']

    return ins


def infer_orientation(ins):

    # defaults
    ins['INFO']['be1_orient'] = None
    ins['INFO']['be2_orient'] = None
    
    # in case there is more than one distal mapping (e.g. transduced seq.)
    be1_distal_num = 0
    be2_distal_num = 0

    if 'be1_bestmatch' in ins['INFO'] and ins['INFO']['be1_bestmatch'] is not None:
        be1_distal_num = ins['INFO']['be1_bestmatch'].query_distnum

    if 'be2_bestmatch' in ins['INFO'] and ins['INFO']['be2_bestmatch'] is not None:
        be1_distal_num = ins['INFO']['be2_bestmatch'].query_distnum

    for be in ('be1', 'be2'):
        if be+'_prox_loc' in ins['INFO'] and len(ins['INFO'][be+'_prox_loc']) > 0:
            # work out which end of the consensus belongs to the distal sequence: _dist to proximal seq.
            # more than one proximal mapping may indicate the insertion is completely assembled (but not always)
            which_prox = 0
            if be+'_use_prox' in ins['INFO']: which_prox = ins['INFO'][be+'_use_prox']

            right_dist = len(ins['INFO'][be+'_prox_seq']) - max(ins['INFO'][be+'_prox_loc'][which_prox])
            left_dist  = min(ins['INFO'][be+'_prox_loc'][which_prox])

            distal_is_left = False

            if left_dist > right_dist:
                distal_is_left = True

            if be+'_is_3prime' in ins['INFO']:
                if ins['INFO'][be+'_is_3prime']:
                    if distal_is_left:
                        ins['INFO'][be+'_orient'] = '+'
                    else:
                        ins['INFO'][be+'_orient'] = '-'

                else:
                    if distal_is_left:
                        ins['INFO'][be+'_orient'] = '-'
                    else:
                        ins['INFO'][be+'_orient'] = '+'

                if ins['INFO'][be+'_prox_str'] == '-': ins['INFO'][be+'_orient'] = swapstrand(ins['INFO'][be+'_orient'])

    if None not in (ins['INFO']['be1_orient'], ins['INFO']['be2_orient']):
        ins['INFO']['inversion'] = ins['INFO']['be1_orient'] != ins['INFO']['be2_orient']

    return ins


def swapstrand(strand):
    if strand == '+': return '-'
    if strand == '-': return '+'


def infer_length(ins):
    be1 = None
    be2 = None

    if 'be1_bestmatch' in ins['INFO']: be1 = ins['INFO']['be1_bestmatch']
    if 'be2_bestmatch' in ins['INFO']: be2 = ins['INFO']['be2_bestmatch']

    # length is known if both ends are known
    if None not in (be1, be2):
        coords = (be1.target_start+be1.target_alnsize, be1.target_start, be2.target_start+be2.target_alnsize, be2.target_start)
        return max(coords) - min(coords)

    # can reasonably approximate length if only 5' end is known
    if be1 is not None and not ins['INFO']['be1_is_3prime']:
        return be1.target_seqsize - min(be1.target_start+be1.target_alnsize, be1.target_start)

    if be2 is not None and not ins['INFO']['be2_is_3prime']:
        return be2.target_seqsize - min(be2.target_start+be2.target_alnsize, be2.target_start)

    # no idea
    return None


def best_ref(ins):
    be1 = None
    be2 = None

    if 'be1_bestmatch' in ins['INFO']: be1 = ins['INFO']['be1_bestmatch']
    if 'be2_bestmatch' in ins['INFO']: be2 = ins['INFO']['be2_bestmatch']

    if None not in (be1, be2):
        if be1.target_id == be2.target_id:
            return be1.target_id

        elif be1.only_polyA():
            return be2.target_id

        elif be2.only_polyA():
            return be1.target_id

        elif be1.score > be2.score:
            return be1.target_id

        elif be1.score < be2.score:
            return be2.target_id

    else:
        if be1 is not None: return be1.target_id
        if be2 is not None: return be2.target_id

    return None


def make_tmp_ref(ins, ref_fa, tmpdir='/tmp'):
    ref_id = best_ref(ins)
    if ref_id is None:
        return None

    inslib = load_falib(ref_fa)
    assert ref_id in inslib, 'Reference is missing: %s' % ref_id

    tmp_ref = tmpdir + '/tebreak.ref.%s.%s.fa' % (ref_id, ins['INFO']['ins_uuid'])

    with open(tmp_ref, 'w') as fa:
        fa.write('>%s\n%s\n' % (ref_id, inslib[ref_id]))

    return prepare_ref(tmp_ref, refoutdir=tmpdir, makeLAST=False)


def remap_discordant(ins, inslib_fa=None, useref=None, tmpdir='/tmp'):
    ''' will build temporary ref from inslib_fasta unless useref is specified '''
    if len(ins['READSTORE']) == 0:
        return None

    if inslib_fa is not None:
    # make references for best target
        tmp_ref = make_tmp_ref(ins, inslib_fa, tmpdir)
        if tmp_ref is None: return None

    if useref is not None: tmp_ref = useref

    if tmp_ref is None: return None

    tmp_fq  = '%s/tebreak.%s.discoremap.fq' % (tmpdir, ins['INFO']['ins_uuid'])
    tmp_sam = '.'.join(tmp_fq.split('.')[:-1]) + '.sam'
    tmp_bam = '.'.join(tmp_fq.split('.')[:-1]) + '.bam'
    tmp_srt = '.'.join(tmp_fq.split('.')[:-1]) + '.srt.bam'

    with open(tmp_fq, 'w') as fq:
        for dr in ins['READSTORE']:
            #TODO: quality trim reads
            fq.write(dr)

    sam_cmd = ['bwa', 'mem', '-v', '1', '-k', '10', '-M', '-S', '-P', tmp_ref, tmp_fq]
    bam_cmd = ['samtools', 'view', '-bt', tmp_ref + '.fai', '-o', tmp_bam, tmp_sam]
    srt_cmd = ['samtools', 'sort', '-T', tmp_srt, '-o', tmp_srt, tmp_bam]
    idx_cmd = ['samtools', 'index', tmp_bam]

    FNULL = open(os.devnull, 'w')

    with open(tmp_sam, 'w') as sam:
        p = subprocess.Popen(sam_cmd, stdout=subprocess.PIPE, stderr=FNULL)
        for line in p.stdout:
            line = line.decode()
            if not line.startswith('@'):
                if bin(int(line.split('\t')[1]) & 4) != bin(4): # not unmapped
                    sam.write(line)
            else:
                sam.write(line)

    p = subprocess.Popen(bam_cmd, stdout=subprocess.PIPE, stderr=FNULL)
    for line in p.stdout: pass
    
    subprocess.call(srt_cmd)
    shutil.move(tmp_srt, tmp_bam)

    subprocess.call(idx_cmd)

    for ext in ('','.amb','.ann','.bck','.bwt','.des','.fai','.pac','.prj','.sa','.sds','.ssp','.suf','.tis'):
        if os.path.exists(tmp_ref+ext): os.remove(tmp_ref+ext)

    os.remove(tmp_sam)
    os.remove(tmp_fq)

    return tmp_bam


def exonerate_align(qryseq, refseq, tmpdir='/tmp', minmatch=90.0):
    rnd = str(uuid4())
    tgtfa = tmpdir + '/tmp.' + rnd + '.tgt.fa'
    qryfa = tmpdir + '/tmp.' + rnd + '.qry.fa'

    tgt = open(tgtfa, 'w')
    qry = open(qryfa, 'w')

    tgt.write('>ref' + '\n' + refseq + '\n')
    qry.write('>qry' + '\n' + qryseq + '\n')

    tgt.close()
    qry.close()

    cmd = ['exonerate', '--bestn', '1', '-m', 'ungapped', '--showalignment', '1', '--ryo', 'ALN' + '\t%s\t%qab\t%qae\t%tab\t%tae\t%pi\t%qS\t%tS\n', qryfa, tgtfa]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    best = []
    topscore = 0

    for pline in p.stdout.readlines():
        pline = pline.decode()
        if pline.startswith('ALN'):
            c = pline.strip().split()
            if int(c[1]) > topscore and float(c[6]) >= minmatch:
                topscore = int(c[1])
                best = c

    os.remove(tgtfa)
    os.remove(qryfa)

    return best


def get_bam_info(bam, ins):
    max_positions = []
    min_positions = []
    sr_count = 0
    dr_count = 0

    for read in bam.fetch():
        max_positions.append(read.get_reference_positions()[-1])
        min_positions.append(read.get_reference_positions()[0])
        if read.qname.split('.')[-1] == 'DR': dr_count += 1
        if read.qname.split('.')[-1] == 'SR': sr_count += 1

    if len(min_positions) > 0: ins['INFO']['remap_min_pos'] = min(min_positions)
    if len(max_positions) > 0: ins['INFO']['remap_max_pos'] = max(max_positions)

    ins['INFO']['remap_sr_count'] = sr_count
    ins['INFO']['remap_dr_count'] = dr_count

    return ins


def resolve_insertion(args, ins, inslib_fa):
    ''' add data based on alignments of library to consensus '''
    try:
        last_res = last_alignment(ins, inslib_fa)
        ins = add_insdata(ins, last_res)
        ins['INFO']['inslib_fa'] = inslib_fa

        if 'best_ins_matchpct' in ins['INFO'] and ins['INFO']['best_ins_matchpct'] >= float(args.min_ins_match):
            tmp_bam = remap_discordant(ins, inslib_fa=inslib_fa, tmpdir=args.refoutdir)

            if tmp_bam is not None:
                bam = pysam.AlignmentFile(tmp_bam, 'rb')
                ins['INFO']['support_bam_file'] = tmp_bam
                ins['INFO']['mapped_target'] = bam.mapped
                ins = get_bam_info(bam, ins)

                extend_consensus(ins, bam)

                if args.callmuts and ins['INFO']['mapped_target'] > int(args.min_disc_reads):
                    tmp_bam_base = os.path.basename(tmp_bam)
                    ins_obj = Ins(ins, None, False)

                    if not args.keep_all_tmp_bams:
                        if os.path.exists(tmp_bam): os.remove(tmp_bam)
                        if os.path.exists(tmp_bam + '.bai'): os.remove(tmp_bam + '.bai')

        return ins

    except Exception as e:
        sys.stderr.write('*'*60 + '\tencountered error:\n')
        traceback.print_exc(file=sys.stderr)

        if ins and 'INFO' in ins and 'chrom' in ins['INFO'] and 'be1_breakpos' in ins['INFO']:
            sys.stderr.write("Insertion location: %s:%d\n" % (ins['INFO']['chrom'], ins['INFO']['be1_breakpos']))

        sys.stderr.write("*"*60 + "\n")

        return None


def guess_forward(seq):
    nA = len([b for b in list(seq.upper()) if b == 'A'])
    nT = len([b for b in list(seq.upper()) if b == 'T'])

    if nA > nT:
        return True

    return False


def load_uuids(fn):
    ''' read UUIDs from first column of input file '''
    with open(fn, 'r') as insfile:
        return dict.fromkeys([line.strip().split()[0] for line in insfile if line.strip().split()[0].find('-') == 8], True)


def prefilter(args, ins, uuids):

    minmatch = 0.0
    if 'be1_avgmatch' in ins['INFO'] and ins['INFO']['be1_avgmatch'] > minmatch: minmatch = ins['INFO']['be1_avgmatch']
    if 'be2_avgmatch' in ins['INFO'] and ins['INFO']['be2_avgmatch'] > minmatch: minmatch = ins['INFO']['be2_avgmatch']

    if minmatch < float(args.min_ref_match):
        return 'minmatch'

    if ins['INFO']['dr_count'] < int(args.min_disc_reads):
        return 'mindisc'

    if ins['INFO']['be1_sr_count'] + ins['INFO']['be2_sr_count'] < int(args.min_split_reads):
        return 'minsplit'

    if not args.unmapped:
        unmap = True
        if 'be1_dist_seq' in ins['INFO'] and ins['INFO']['be1_dist_seq'] is not None: unmap = False
        if 'be2_dist_seq' in ins['INFO'] and ins['INFO']['be2_dist_seq'] is not None: unmap = False

        if unmap:
            return 'mapends'

    if uuids is not None and ins['INFO']['ins_uuid'] not in uuids:
        return 'uuidlist'

    return False


def fix_orient(rec, inslib, ref):
    ins_id = '%s:%s' % (rec['Superfamily'], rec['Subfamily'])

    refseq = ref.fetch(rec['Chromosome'], int(rec['Left_Extreme']), int(rec['Right_Extreme']))

    elt_5p_align = exonerate_align(rec['Genomic_Consensus_5p'], inslib[ins_id])
    elt_3p_align = exonerate_align(rec['Genomic_Consensus_3p'], inslib[ins_id])
    gen_5p_align = exonerate_align(rec['Genomic_Consensus_5p'], refseq)
    gen_3p_align = exonerate_align(rec['Genomic_Consensus_3p'], refseq)

    # try using the insertion-based consensus if no luck with the genomic one
    if not elt_5p_align or not gen_5p_align:
        retry_elt_5p_align = exonerate_align(rec['Insert_Consensus_5p'], inslib[ins_id])
        retry_gen_5p_align = exonerate_align(rec['Insert_Consensus_5p'], refseq)

        if retry_gen_5p_align and retry_elt_5p_align:
            elt_5p_align = retry_elt_5p_align
            gen_5p_align = retry_gen_5p_align


    if not elt_3p_align or not gen_3p_align:
        retry_elt_3p_align = exonerate_align(rec['Insert_Consensus_3p'], inslib[ins_id])
        retry_gen_3p_align = exonerate_align(rec['Insert_Consensus_3p'], refseq)

        if retry_gen_3p_align and retry_elt_3p_align:
            elt_3p_align = retry_elt_3p_align
            gen_3p_align = retry_gen_3p_align


    elt_5p_orient = 'NA'
    elt_3p_orient = 'NA'
    gen_5p_orient = 'NA'
    gen_3p_orient = 'NA'

    if elt_5p_align:
        elt_5p_orient = elt_5p_align[-1]

    if elt_3p_align:
        elt_3p_orient = elt_3p_align[-1]

    if gen_5p_align:
        gen_5p_orient = gen_5p_align[-1]

    if gen_3p_align:
        gen_3p_orient = gen_3p_align[-1]

    new_5p_orient = 'NA'
    new_3p_orient = 'NA'

    if 'NA' not in (elt_5p_orient, gen_5p_orient):
        if elt_5p_orient == gen_5p_orient:
            new_5p_orient = '+'
        else:
            new_5p_orient = '-'

    if 'NA' not in (elt_3p_orient, gen_3p_orient):
        if elt_3p_orient == gen_3p_orient:
            new_3p_orient = '+'
        else:
            new_3p_orient = '-'

    coords_5p = []
    coords_3p = []

    if elt_5p_align:
        coords_5p = sorted(map(int, (elt_5p_align[4], elt_5p_align[5])))

    if elt_3p_align:
        coords_3p = sorted(map(int, (elt_3p_align[4], elt_3p_align[5])))

    flip = False
    if coords_5p and coords_3p and coords_5p[1] > coords_3p[1]:
        flip = True

    if rec['Orient_5p'] != new_5p_orient:
        logger.info('Changed 5p orientation for %s' % rec['UUID'])

    if rec['Orient_3p'] != new_3p_orient:
        logger.info('Changed 3p orientation for %s' % rec['UUID'])

    rec['Orient_5p'] = new_5p_orient
    rec['Orient_3p'] = new_3p_orient

    if 'NA' not in (new_5p_orient, new_3p_orient) and 'None' not in (rec['Orient_5p'], rec['Orient_3p']):
        if rec['Orient_5p'] != rec['Orient_3p']:
            rec['Inversion'] = 'Y'
        else:
            rec['Inversion'] = 'N'

    else:
        rec['Inversion'] = 'N'


    if flip:
        rec = flip_ends(rec)

    return rec


def flip_ends(rec):
    rec['5_Prime_End'], rec['3_Prime_End'] = rec['3_Prime_End'], rec['5_Prime_End']
    rec['Orient_5p'], rec['Orient_3p'] = rec['Orient_3p'], rec['Orient_5p']
    rec['5p_Elt_Match'], rec['3p_Elt_Match'] = rec['3p_Elt_Match'], rec['5p_Elt_Match']
    rec['5p_Genome_Match'], rec['3p_Genome_Match'] = rec['3p_Genome_Match'], rec['5p_Genome_Match']
    rec['Split_reads_5prime'], rec['Split_reads_3prime'] = rec['Split_reads_3prime'], rec['Split_reads_5prime']
    rec['5p_Cons_Len'], rec['3p_Cons_Len'] = rec['3p_Cons_Len'], rec['5p_Cons_Len']
    rec['5p_Improved'], rec['3p_Improved'] = rec['3p_Improved'], rec['5p_Improved']
    rec['TSD_3prime'], rec['TSD_5prime'] = rec['TSD_5prime'], rec['TSD_3prime']
    rec['Genomic_Consensus_5p'], rec['Genomic_Consensus_3p'] = rec['Genomic_Consensus_3p'], rec['Genomic_Consensus_5p']
    rec['Insert_Consensus_5p'], rec['Insert_Consensus_3p'] = rec['Insert_Consensus_3p'], rec['Insert_Consensus_5p']

    return rec


def finalise_ins(ins, args):
    ''' called concurrently; build insertion annotations for output '''
    try:
        return Ins(ins, args.annotation_tabix, args.use_rg, callmuts=args.callmuts, allow_unmapped=args.unmapped)

    except Exception as e:
        sys.stderr.write('*'*60 + '\tencountered error:\n')
        traceback.print_exc(file=sys.stderr)

        if ins and 'INFO' in ins and 'chrom' in ins['INFO'] and 'be1_breakpos' in ins['INFO']:
            sys.stderr.write("Insertion location: %s:%d\n" % (ins['INFO']['chrom'], ins['INFO']['be1_breakpos']))

        sys.stderr.write("*"*60 + "\n")

        return None


def resolve(args):
    raw_insertions = []

    logger.info('tebreak.py called with args: %s' % ' '.join(sys.argv))
    logger.info('loading pickle: %s' % args.pickle)

    with open(args.pickle, 'rb') as pickin:
        raw_insertions = pickle.load(pickin)

    logger.info('finished loading %s' % args.pickle)
    logger.info('raw candidate count: %d' % len(raw_insertions))

    uuids = None

    if args.uuid_list is not None:
        uuids = load_uuids(args.uuid_list)

    insertions = []
    prefilter_reasons = []

    for ins in raw_insertions:
        prefiltered = prefilter(args, ins, uuids)

        if not prefiltered:
            insertions.append(ins)

        else:
            prefilter_reasons.append(prefiltered)
            if args.ignore_filters:
                ins['filter'] = [prefiltered]
                insertions.append(ins)


    logger.debug('prefiltering stats:')
    for pfr, count in Counter(prefilter_reasons).items():
        logger.debug('reason %s: %d' % (pfr, count))

    # clean up memory?
    raw_insertions = []
    gc_c = gc.collect()


    logger.info('prefiltered candidate count: %d' % len(insertions))
    logger.info('prepare reference %s...' % args.inslib_fasta)

    inslib_fa = prepare_ref(args.inslib_fasta, refoutdir=args.refoutdir, makeFAI=args.callmuts, makeBWA=False, usecached=args.usecachedLAST)

    results = []
    
    processed_insertions = []

    pool_resolve = mp.Pool(processes=int(args.processes))

    gc_c = gc.collect()

    onepct = int(len(insertions)*.01)+1

    for counter, ins in enumerate(insertions):
        res = pool_resolve.apply_async(resolve_insertion, [args, ins, inslib_fa])
        results.append(res)

        if counter % onepct == 0:
            logger.info('submitted %d candidates, last uuid: %s, pct complete: %f' % (counter, ins['INFO']['ins_uuid'], counter/float(len(insertions))))

            new_insertions = [res.get() for res in results if res is not None]
            if new_insertions:
                processed_insertions += new_insertions
                results = []

    new_insertions = [res.get() for res in results if res is not None]
    processed_insertions += new_insertions

    if args.detail_out is None:
        args.detail_out = '.'.join(args.pickle.split('.')[:-1]) + '.resolve.out'

    pool_resolve.close()
    pool_resolve.join()

    inslib = load_falib(args.inslib_fasta)
    ref = pysam.Fastafile(args.bwaref)

    text_summary(processed_insertions, cmd=' '.join(sys.argv), outfile=args.detail_out) # debug

    results = []

    pool_final = mp.Pool(processes=int(args.processes))

    final_insertions = []

    for ins in processed_insertions:
        if ins is not None:
            res = pool_final.apply_async(finalise_ins, [ins, args])
            results.append(res)

    final_insertions = [res.get() for res in results if res is not None]

    pool_final.close()
    pool_final.join()

    out_table_fn = args.out
    if out_table_fn is None:
        out_table_fn = '.'.join(args.pickle.split('.')[:-1]) + '.table.txt'

    with open(out_table_fn, 'w') as out_table:
        if len(final_insertions) > 0:
            if args.ignore_filters:
                final_insertions[0].out['Filters'] = 'NA'

            out_table.write('%s\n' % final_insertions[0].header())

        for ins in sorted(final_insertions):
            ins = filter(ins, args)

            if args.ignore_filters:
                ins.out['Filters'] = 'NA'
                if not ins.ins['passedfilter']:
                    if ins.ins['filter']:
                        ins.out['Filters'] = ','.join(ins.ins['filter'])

                out_table.write('%s\n' % ins)

            elif ins.ins['passedfilter']:
                # last-minute orientation fix
                if not args.unmapped and not args.skip_orient_fix:
                    ins.out = fix_orient(ins.out, inslib, ref)
                out_table.write('%s\n' % ins)

    return out_table_fn


def main(args):
    if args.debug:
        logger.setLevel(logging.DEBUG)

    if args.use_pickle is None:
        args.pickle = tebreak(args)

    else:
        args.pickle = args.use_pickle

    logger.info('resolving insertions from %s' % args.pickle)

    table_fn = resolve(args)
    logger.info('tebreak finished, final output table: %s' % table_fn)

 
if __name__ == '__main__':
    # set up logger
    FORMAT = '%(asctime)s %(message)s'
    logging.basicConfig(format=FORMAT)
 
    parser = argparse.ArgumentParser(description='Find inserted sequences vs. reference')
    parser.add_argument('-b', '--bam', required=True, help='target BAM(s): can be comma-delimited list or .txt file with bam locations')
    parser.add_argument('-r', '--bwaref', required=True, help='bwa/samtools indexed reference genome')
    parser.add_argument('-p', '--processes', default=1, help='split work across multiple processes')
    parser.add_argument('-c', '--chunks', default=1, help='split genome into chunks (default = # processes), helps control memory usage')

    parser.add_argument('-i', '--inslib_fasta', required=True, help="reference for insertions (not genome)")

    parser.add_argument('--interval_bed', default=None, help='BED file with intervals to scan')
    parser.add_argument('-d', '--disco_target', default=None, help='limit breakpoint search to discordant mate-linked targets (e.g. generated with /lib/make_discref_hg19.sh)')
    parser.add_argument('-m', '--mask', default=None, help='BED file of masked regions')
    parser.add_argument('-o', '--out', default=None, help="output table")

    parser.add_argument('-u', '--use_pickle', default=None, help="pickle intermediate to use (skips breakpoint finding stage)")

    parser.add_argument('--minMWP', default=0.01, help='minimum Mann-Whitney P-value for split qualities (default = 0.01)')
    parser.add_argument('--clip_limit', default=500, help='limit number of clipped reads gathered for each breakend (default = 500)')
    parser.add_argument('--min_minclip', default=3, help='min. shortest clipped bases per cluster (default = 3)')
    parser.add_argument('--min_maxclip', default=10, help='min. longest clipped bases per cluster (default = 10)')
    parser.add_argument('--min_sr_per_break', default=1, help='minimum split reads per breakend (default = 1)')
    parser.add_argument('--min_consensus_score', default=0.9, help='quality of consensus alignment (default = 0.9)')
    parser.add_argument('--skip_chroms', default=None, help='skip chromsomes when finding discordant targets (.txt file)')
    parser.add_argument('--max_ins_reads', default=100000, help='maximum number of reads to use per insertion call (default = 100000)')
    parser.add_argument('--min_split_reads', default=4, help='minimum total split reads per insertion call (default = 4)')
    parser.add_argument('--min_prox_mapq', default=10, help='minimum map quality for proximal subread (default = 10)')
    parser.add_argument('--max_N_consensus', default=4, help='exclude breakend seqs with > this number of N bases (default = 4)')
    parser.add_argument('--exclude_bam', default=None, help='may be comma delimited')
    parser.add_argument('--exclude_readgroup', default=None, help='may be comma delimited')
    parser.add_argument('--max_bam_count', default=0, help='maximum number of bams supporting per insertion')
    parser.add_argument('--map_tabix', default=None, help='tabix-indexed BED of mappability scores')
    parser.add_argument('--min_mappability', default=0.1, help='minimum mappability (default = 0.1; only matters with --map_tabix)')
    parser.add_argument('--max_disc_fetch', default=50, help='maximum number of discordant reads to fetch per insertion site per BAM (default = 50)')
    parser.add_argument('--min_disc_reads', default=4, help='if using -d/--disco_target, minimum number of discordant reads to trigger a call (default = 4)')

    parser.add_argument('--min_ins_match', default=0.90, help="(output) minumum match to insertion library (default 0.90)")
    parser.add_argument('--min_ref_match', default=0.98, help="(output) minimum match to reference genome (default 0.98)")
    parser.add_argument('--min_cons_len', default=250, help='(output) min total consensus length (default=250)')
    parser.add_argument('--ignore_filters', action='store_true', default=False, help='(output) unfiltered mode')
    parser.add_argument('-a', '--annotation_tabix', default=None, help="(output) can be comma-delimited list")
    parser.add_argument('--refoutdir', default='tebreak_refs', help="output directory for generating tebreak references (default=tebreak_refs)")
    parser.add_argument('--use_rg', action='store_true', default=False, help="(output) use RG instead of BAM filename for samples")
    parser.add_argument('--keep_all_tmp_bams', action='store_true', default=False, help="leave ALL temporary BAMs (warning: lots of files!)")
    #parser.add_argument('--detail_out', default=None, help="file to write detailed output")
    parser.add_argument('--unmapped', default=False, action='store_true', help="report insertions that do not match insertion library")
    parser.add_argument('--usecachedLAST', default=False, action='store_true', help="try to used cached LAST db, if found")
    parser.add_argument('--uuid_list', default=None, help='limit resolution to UUIDs in first column of input list (can be tabular output from previous run)')
    parser.add_argument('--callmuts', default=False, action='store_true', help='detect changes in inserted seq. vs ref. (requires bcftools)')
    parser.add_argument('--nogeno', default=False, action='store_true', help='do not output genotype calls')
    parser.add_argument('--skip_orient_fix', default=False, action='store_true', help='do not apply fix for orientation')

    parser.add_argument('--tmpdir', default='/tmp', help='temporary directory (default = /tmp)')
    parser.add_argument('--pickle', default=None, help='pickle output name')
    parser.add_argument('--detail_out', default=None, help='file to write detailed output')
    parser.add_argument('--disc_out', default=None, help='file to write discordant cluster output')
    parser.add_argument('--disc_only', action='store_true', help='only identify discordant clusters and exit (does not run tebreak)')
    parser.add_argument('--rescue_asm', action='store_true', help='try harder to improve consensus (warning: may cause chimeras)', default=False)
    parser.add_argument('--skipshm', action='store_true', help='dont load bwa index into shared memory (warning: may increase runtime)')
    parser.add_argument('--debug', action='store_true', default=False)
 
    args = parser.parse_args()
    main(args)
