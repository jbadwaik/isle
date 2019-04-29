r"""!\file
\ingroup evolvers

"""

from copy import deepcopy
from math import sqrt, exp, floor, ceil
from logging import getLogger
from itertools import chain

import h5py as h5
import numpy as np
from scipy.stats import norm, skewnorm
from scipy.optimize import least_squares, curve_fit

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

from .evolver import Evolver
from .selector import BinarySelector
from .leapfrog import ConstStepLeapfrog
from .. import leapfrog
from ..h5io import createH5Group, loadList


TARGET_ACC_RATE = 0.67

MAX_NSTEP = 1000


# targeted confidence for acc rate and probability per nMD
TARGET_CONF = 0.92
TARGET_CONF_INT_TP = 0.25 / 10 / 2
TARGET_CONF_INT_PROB = 0.25 / 2

ONE_SIGMA_PROB = 0.682689492
TWO_SIGMA_PROB = 0.954499736


def _confIntTrajPoints(trajPoints, quantileProb):
    '''
    Confidence Interval
    The formula is from paper:
    Vollset, S.E., 1993. Confidence intervals for a binomial proportion. Statistics in medicine, 12(9), pp.809-824.

    More information:
    Newcombe, Robert G. "Two-Sided Confidence Intervals for the Single Proportion: Comparison of Seven Methods," Statistics in Medicine, 17, 857-872 (1998).
    http://vassarstats.net/prop1.html
    https://www.medcalc.org/manual/values_of_the_normal_distribution.php
    http://slideplayer.com/slide/5055000/

    Assumes the probability to accept follows a normal distribution.
    Uses binomial confidence interval because we can either accept or reject, nothing more (yet).
    '''

    accepted = trajPoints.count(1)
    total = len(trajPoints)

    # quantile such that norm.cdf(quantile) == quantile_prob
    # gives the x value for the boundary of the quantileProb interval (e.g. 92% interval)
    quantile = norm.ppf(quantileProb)

    # acceptance rate
    mean = accepted / total
    # the wald interval relative to mean
    interval = quantile/sqrt(total) * sqrt(mean*(1-mean)) + 1/(2*total)

    # TODO is it valid to just take the max/min? Does the CDF need to be modified for that?
    # # used with binary selector => can't be < 0
    # lower = max(0, mean - interval)
    # # used with binary selector => can't be > 1
    # higher = min(1, mean + interval)

    lower = mean - interval
    upper = mean + interval

    return lower, upper


def _errorTrajPoints(trajPoints, quantileProb):
    # TODO is this valid??
    return _intervalLength(_confIntTrajPoints(trajPoints, quantileProb)) \
        / 2 / sqrt(len(trajPoints))


def _confIntProbabilities(probabilities, quantileProb):
    mean = np.mean(probabilities)
    err = np.std(probabilities)
    # endpoints of quantileProb confidence interval
    result = norm.interval(quantileProb, loc=mean, scale=err)
    # TODO see above, not really correct but close enough
    #      taking the min/max decreaes the error for extreme nstep, that makes the fits worse!
    # # used with probabilities => can't be < 0
    # lower = max(0, result[0])
    # # used with probabilities => can't be > 1
    # higher = min(1, result[1])

    lower = result[0]
    upper = result[1]

    return lower, upper

def _errorProbabilities(probabilities, quantileProb):
    # divide by two because standard deviation is only half the confidence interval
    return _intervalLength(_confIntProbabilities(probabilities, quantileProb)) \
        / 2 / sqrt(len(probabilities))

def _intervalLength(interval):
    return interval[1] - interval[0]


# def appendToListInDict(dictionary, key, value):
#     try:
#         dictionary[key].append(value)
#     except KeyError:
#         dictionary[key] = [value]

def extendListInDict(dictionary, key, values):
    try:
        dictionary[key].extend(values)
    except KeyError:
        dictionary[key] = deepcopy(values)


class Registrar:

    class Record:
        def __init__(self, length, nstep, verification=False):
            self.length = length
            self.nstep = nstep
            self.probabilities = []
            self.trajPoints = []
            self.verification = verification

        def __len__(self):
            return len(self.trajPoints)

        def add(self, probability, trajPoint):
            self.probabilities.append(probability)
            self.trajPoints.append(trajPoint)

        def confIntProbabilities(self, quantileProb):
            return _confIntProbabilities(self.probabilities, quantileProb)

        def confIntTrajPoints(self, quantileProb):
            return _confIntTrajPoints(self.trajPoints, quantileProb)

        def __eq__(self, other):
            return self.length == other.length \
                and self.nstep == other.nstep \
                and self.probabilities == other.probabilities \
                and self.trajPoints == other.trajPoints \

        def __str__(self):
            return f"""Record(length={self.length}, nstep={self.nstep}
       probabilities={self.probabilities}
       trajPoints={self.trajPoints})"""

        def save(self, h5group):
            h5group["length"] = self.length
            h5group["nstep"] = self.nstep
            h5group["probabilities"] = self.probabilities
            h5group["trajPoints"] = self.trajPoints
            h5group["verification"] = self.verification

        @classmethod
        def fromH5(cls, h5group):
            record = cls(h5group["length"][()], h5group["nstep"][()],
                         h5group["verification"][()])
            record.probabilities = list(h5group["probabilities"][()])
            record.trajPoints = list(h5group["trajPoints"][()])
            return record

    def __init__(self, initialLength, initialNstep):
        self.records = []
        self._knownLength = set()
        self._knownNstep = set()
        self.newRecord(initialLength, initialNstep)

        self.fitResults = []

    def __len__(self):
        return len(self.records)

    def currentRecord(self):
        return self.records[-1]

    def newRecord(self, length, nstep, verification=False):
        record = self.Record(length, nstep, verification)
        self.records.append(record)
        self._knownLength.add(length)
        self._knownNstep.add(nstep)
        return record

    def addFitResult(self, result):
        self.fitResults.append(result)

    def gather(self, *, length=None, nstep=None, maxRecord=None):
        if length is None:
            if nstep is None:
                raise ValueError("One of length and nstep must not be None")
            # filter with respect to nstep
            recordFilter = lambda record: record.nstep == nstep
            # and use length as key
            selectParam = lambda record: record.length
        else:
            if nstep is not None:
                raise ValueError("One of length and nstep must be None")
            # filter with respect to length
            recordFilter = lambda record: record.length == length
            # and use nstep as key
            selectParam = lambda record: record.nstep

        probDict = dict()
        tpDict = dict()
        for record in filter(recordFilter, self.records[:maxRecord]):
            extendListInDict(probDict, selectParam(record), record.probabilities)
            extendListInDict(tpDict, selectParam(record), record.trajPoints)

        probabilities = [(param, np.mean(probs), _errorProbabilities(probs, ONE_SIGMA_PROB))
                         for param, probs in probDict.items()]
        trajPoints = [(param, np.mean(tps), _errorTrajPoints(tps, ONE_SIGMA_PROB))
                      for param, tps in tpDict.items()]

        return probabilities, trajPoints

    def seenBefore(self, *, length=None, nstep=None):
        if length is None:
            if nstep is None:
                raise ValueError("At least one of length and nstep must not be None")
            return nstep in self._knownNstep

        # else: length is not None
        if nstep is None:
            return length in self._knownLength

        # else: both not None
        return length in self._knownLength and nstep in self._knownNstep

    def _saveRecords(self, h5group):
        maxStored = -1
        for idx, grp in loadList(h5group):
            if idx >= len(self.records):
                getLogger(__name__).error("Cannot save recording, there are more records in the "
                                          "file than currently recorded")
                raise RuntimeError("More records in the file that currently stored")
            storedRecord = self.Record.fromH5(grp)
            if storedRecord != self.records[idx]:
                getLogger(__name__).error("Cannot save recording, record %d stored in the file "
                                          "does dot match record in memory.", idx)
                raise RuntimeError("Record in file does not match record in memory")
            maxStored = idx

        for idx, record in filter(lambda pair: pair[0] > maxStored, enumerate(self.records)):
            if idx == len(self) and len(record) == 0:
                # TODO still true?
                # the last record might be empty, do not save it
                break
            record.save(h5group.create_group(str(idx)))

    def _saveFitResults(self, h5group):
        maxStored = -1
        for idx, grp in loadList(h5group):
            if idx >= len(self.fitResults):
                getLogger(__name__).error("Cannot save recording, there are more fit results in "
                                          "the file than currently recorded")
                raise RuntimeError("More fit results in the file that currently stored")

            storedResult = Fitter.Result.fromH5(grp)
            if storedResult != self.fitResults[idx]:
                getLogger(__name__).error("Cannot save recording, fit result %d stored in the file "
                                          "does dot match fit result in memory.", idx)
                raise RuntimeError("Fit result in file does not match fit result in memory")
            maxStored = idx

        for idx, fitResult in filter(lambda pair: pair[0] > maxStored, enumerate(self.fitResults)):
            fitResult.save(h5group.create_group(str(idx)))

    def save(self, h5group):
        self._saveRecords(createH5Group(h5group, "records"))
        self._saveFitResults(createH5Group(h5group, "fitResults"))

    @classmethod
    def fromH5(cls, h5group):
        # build a completely empty instance
        registrar = cls(0, 0)
        registrar.records = []
        registrar._knownLength = set()
        registrar._knownNstep = set()

        for _, grp in sorted(h5group["records"].items(),
                                 key=lambda pair: int(pair[0])):
            storedRecord = cls.Record.fromH5(grp)
            # go through this function to make sure all internal variables are set up properly
            record = registrar.newRecord(storedRecord.length, storedRecord.nstep,
                                         storedRecord.verification)
            record.probabilities = storedRecord.probabilities
            record.trajPoints = storedRecord.trajPoints

        for _, grp in sorted(h5group["fitResults"].items(),
                             key=lambda pair: int(pair[0])):
            registrar.addFitResult(Fitter.Result.fromH5(grp))

        return registrar

def fitFunction(x, *a):
    return skewnorm.cdf(x, *a)

def squareSum(func, indep, dep, deperr, par):
    return np.sum((func(indep, *par)-dep)**2 / deperr**2)


class Fitter:
    class Result:
        def __init__(self, bestFit, otherFits):
            self.bestFit = bestFit
            self.otherFits = otherFits

        def bestNstep(self, targetAccRate):
            return skewnorm.ppf(targetAccRate, *self.bestFit)

        def evalOn(self, x):
            return fitFunction(x, *self.bestFit), \
                [fitFunction(x, *params) for params in self.otherFits]

        def __eq__(self, other):
            return np.array_equal(self.bestFit, other.bestFit) \
                and np.array_equal(self.otherFits, other.otherFits)

        def save(self, h5group):
            h5group["best"] = self.bestFit
            h5group["others"] = self.otherFits

        @classmethod
        def fromH5(cls, h5group):
            return cls(h5group["best"][()],
                       h5group["others"][()])

    def __init__(self, startParams=None, artificialPoints=None):

        self._startParams = startParams if startParams is not None else \
            [(2, 3, 1), (1, 1, 1), (10, 2, 1)]
        self._lastFit = None   # parameters only
        self.artificialPoints = artificialPoints if artificialPoints is not None else \
            [(0, 0.0, 1e-8), (MAX_NSTEP, 1.0, 1e-8)]

    def _joinFitData(self, probabilityPoints, trajPointPoints):
        return np.asarray([*zip(*(probabilityPoints + trajPointPoints + self.artificialPoints))])

    def fitNstep(self, probabilityPoints, trajPointPoints):
        independent, dependent, dependenterr = self._joinFitData(probabilityPoints, trajPointPoints)
        startParams = self._startParams + (self._lastFit if self._lastFit is not None else [])

        fittedParams = []
        for guess in startParams:
            try:
                fittedParams.append(curve_fit(fitFunction, independent, dependent,
                                              p0=guess, sigma=dependenterr,
                                              absolute_sigma=True, method="trf")[0])
            except RuntimeError as err:
                getLogger(__name__).info("Fit failed with starting parameters %s: %s",
                                         guess, err)

        if not fittedParams:
            getLogger(__name__).error("No fit converged, unable to continue tuning.")
            # raise RuntimeError("No fit converged")
            return None

        bestFit, *otherFits = sorted(fittedParams,
                                     key=lambda params: squareSum(fitFunction, independent,
                                                                  dependent, dependenterr, params))
        self._lastFit = bestFit

        return self.Result(bestFit, otherFits)


class LeapfrogTuner(Evolver):
    r"""! \ingroup evolvers

    """

    def __init__(self, action, initialLength, initialNstep, rng, recordFname,
                 targetAccRate=0.61, runsPerParam=(10, 100), maxRuns=10):
        r"""!

        """

        self.registrar = Registrar(initialLength, initialNstep)
        self.action = action
        self.targetAccRate = targetAccRate
        self.runsPerParam = runsPerParam
        self.maxRuns = maxRuns
        self.recordFname = recordFname

        self._fitter = Fitter()
        self._selector = BinarySelector(rng)
        self._pickNextNStep = self._pickNextNStep_search
        self._finished = False
        self._tunedParameters = None

    def evolve(self, phi, pi, actVal, trajPoint):
        r"""!
        Run one step of leapfrog integration and tune parameters.
        \param phi Input configuration.
        \param pi Input Momentum.
        \param actVal Value of the action at phi.
        \param trajPoint 0 if previous trajectory was rejected, 1 if it was accepted.
        \returns In order:
          - New configuration
          - New momentum
          - Action evaluated at new configuration
          - Point along trajectory that was selected
        """

        # do not even evolve any more (we don't want to waste precious time)
        if self._finished:
            raise StopIteration()

        phi, pi, actVal, trajPoint = self._doEvolve(phi, pi, actVal, trajPoint)

        log = getLogger("atune")
        currentRecord = self.registrar.currentRecord()

        if len(currentRecord) >= self.runsPerParam[0]:
            # confIntProb = currentRecord.confIntProbabilities(TWO_SIGMA_PROB)
            # confIntTP = currentRecord.confIntTrajPoints(TWO_SIGMA_PROB)

            errProb = _errorProbabilities(currentRecord.probabilities, TWO_SIGMA_PROB)
            errTP = _errorTrajPoints(currentRecord.trajPoints, TWO_SIGMA_PROB)

            # log.debug(f"{_intervalLength(confIntProb)}, {_intervalLength(confIntTP)}")
            # log.debug(f"prob = {currentRecord.probabilities}\n  tp = {currentRecord.trajPoints}")
            # log.debug(f"errors: {errProb}, {errTP}")

            # if _intervalLength(confIntTP) < TARGET_CONF_INT_TP:
            if errTP < TARGET_CONF_INT_TP:
                log.debug("Stopping because of tp")
                self._pickNextNStep()

            # elif _intervalLength(confIntProb) < TARGET_CONF_INT_PROB:
            elif errProb < TARGET_CONF_INT_PROB:
                log.debug("Stopping because of prob")
                self._pickNextNStep()

            elif len(currentRecord) > self.runsPerParam[1]:
                log.debug("reached max runs for current params")
                # raise RuntimeError("Did not converge")
                self._pickNextNStep()

        # Check here not at the beginning of the function because
        # one of the above steps may have inserted a new record.
        if not self._finished and len(self.registrar) > self.maxRuns:
            log.error("Tuning was unsuccessful within the given maximum number of runs")
            self._finalize(None)

        return phi, pi, actVal, trajPoint

    def currentParams(self):
        record = self.registrar.currentRecord()
        return record.length, record.nstep

    def _doEvolve(self, phi0, pi0, actVal0, _trajPoint0):
        phi1, pi1, actVal1 = leapfrog(phi0, pi0, self.action, *self.currentParams())
        energy0 = actVal0 + pi0@pi0/2
        energy1 = actVal1 + pi1@pi1/2
        trajPoint1 = self._selector.selectTrajPoint(energy0, energy1)

        self.registrar.currentRecord().add(min(1, exp(np.real(energy0 - energy1))),
                                           trajPoint1)

        return (phi1, pi1, actVal1, trajPoint1) if trajPoint1 == 1 \
            else (phi0, pi0, actVal0, trajPoint1)

    def _shiftNstep(self):
        probPoints, TPPoints = self.registrar.gather(length=self.currentParams()[0])

        tps = [tp for (_, tp, _) in TPPoints]


        # small nstep is faster => try that first
        if min(tps) > 0.1:
            minStep = min(self.registrar._knownNstep)
            nextStep = max(1, minStep//2)
            if not self.registrar.seenBefore(nstep=nextStep):
                getLogger(__name__).info("Picked small nstep: %d in run %d",
                                         nextStep, len(self.registrar)-1)
                self.registrar.addFitResult(self._fitter.Result([0, 0, 0], []))
                return nextStep

        # else: just try a bigger one, there should be enough room to expand
        # TODO don't go super big, if tp at the large nstep is 1,
        #      try a smaller one in between existing ones
        maxStep = max(self.registrar._knownNstep)
        nextStep = maxStep * 2
        getLogger(__name__).info("Picked large nstep: %d in run %d",
                                 nextStep, len(self.registrar)-1)
        self.registrar.addFitResult(self._fitter.Result([0, 0, 0], []))

        return nextStep

    def _nstepFromFit(self):
        log = getLogger(__name__)
        fitResult = self._fitter.fitNstep(*self.registrar.gather(
            length=self.currentParams()[0]))

        if fitResult is not None:
            # pick nstep from fit
            log.info("Completed fit for run %d, best parameters: %s",
                     len(self.registrar)-1, fitResult.bestFit)
            self.registrar.addFitResult(fitResult)
            floatStep = fitResult.bestNstep(self.targetAccRate)
            log.info("Optimal nstep from current fit: %f", floatStep)

            return floatStep

        return None

    def _pickNextNStep_search(self):
        log = getLogger(__name__)
        floatStep = self._nstepFromFit()

        self.saveRecording()

        if floatStep is None:
            log.info("Fit unsuccessful, shifting nstep")
            # try a different nstep at an extreme end to stabilise the fit
            nextStep = self._shiftNstep()

        else:
            nextStep = max(int(floor(floatStep)), 1)
            if self.registrar.seenBefore(nstep=nextStep):
                nextStep = int(ceil(floatStep))
                if self.registrar.seenBefore(nstep=nextStep):
                    self._enterVerification(floatStep)
                    return

        if nextStep > MAX_NSTEP:
            raise RuntimeError(f"nstep is too large: {nextStep}")

        self.registrar.newRecord(self.currentParams()[0], nextStep)
        getLogger("atune").debug("New nstep: %d", nextStep)

    def _verificationIntStep(self, oldFloatStep):
        log = getLogger(__name__)
        floatStep = self._nstepFromFit()
        if floatStep is None:
            log.info("Fit unsuccessful in verification")
            self._cancelVerification(self._shiftNstep())
            return None

        if abs(floatStep-oldFloatStep) > 1:
            log.info("Nstep changed by more than 1 in verification: %d vs %d",
                     floatStep, oldFloatStep)
            self._cancelVerification(max(int(floor(floatStep)), 1))
            return None

        return floatStep

    def _enterVerification(self, floatStep):

        def _pickNextNStep_verificationUpper():
            "Check run with upper end of interval around floatStep."

            getLogger(__name__).debug("Checking upper end of interval around floatStep")
            nextFloatStep = self._verificationIntStep(floatStep)
            self.saveRecording()  # save also fit from above function

            if nextFloatStep is not None:
                self._finalize(nextFloatStep)
            else:
                # something is seriously unstable if this happens
                getLogger(__name__).error("The final fit did not converge, "
                                          "unable to extract nstep from tuning results. "
                                          "Continuing search.")
                # verification has been canceled => do nothing more here

        def _pickNextNStep_verificationLower():
            "Check run with lowre end of interval around floatStep."

            getLogger(__name__).debug("Checking lower end of interval around floatStep")
            nextFloatStep = self._verificationIntStep(floatStep)
            self.saveRecording()  # save also fit from above function

            if nextFloatStep is not None:
                # run with upper end of interval next
                self.registrar.newRecord(self.currentParams()[0],
                                         int(ceil(floatStep)),
                                         True)
                self._pickNextNStep = _pickNextNStep_verificationUpper

            # else: verification has been canceled => do nothing here

        getLogger(__name__).info("Entering verification stage with nstep = %f", floatStep)
        getLogger(__name__).debug("Checking lower end of interval around floatStep")

        # run with lower end of interval next
        self.registrar.newRecord(self.currentParams()[0],
                                 max(int(floor(floatStep)), 1),
                                 True)
        self._pickNextNStep = _pickNextNStep_verificationLower

    def _cancelVerification(self, nextStep):
        getLogger(__name__).info("Cancelling verification, reverting back to search")
        self.registrar.newRecord(self.currentParams()[0], nextStep, False)
        self._pickNextNStep = self._pickNextNStep_search

    def _finalize(self, finalFloatStep):
        self._finished = True
        self.saveRecording()

        if finalFloatStep is not None:
            nstep = max(int(floor(finalFloatStep)), 1)
            # linearly interpolate between floor(floatStep) and ceil(floatStep)
            length = nstep / finalFloatStep
            self._tunedParameters = {"nstep": nstep, "length": length}

            with h5.File(self.recordFname, "a") as h5f:
                h5f["leapfrogTuner/finished"] = True
            getLogger(__name__).info("Finished tuning with length = %f and nstep = %d",
                                     length, nstep)

    def saveRecording(self):
        getLogger(__name__).info("Saving current recording")
        with h5.File(self.recordFname, "a") as h5f:
            self.registrar.save(createH5Group(h5f, "leapfrogTuner"))

    def tunedParameters(self):
        if not self._finished:
            raise RuntimeError("LeapfrogTuner has not finished, parameters have not been tuned")
        if not self._tunedParameters:
            raise RuntimeError("LeapfrogTuner has finished but parameters could not be tuned")

        return self._tunedParameters.copy()

    def tunedEvolver(self):
        params = self.tunedParameters()
        return ConstStepLeapfrog(self.action, params["length"],
                                 params["nstep"], self._selector.rng)

    @classmethod
    def loadTunedParameters(cls, h5group):
        h5group = h5group["leapfrogTuner"]

        if "finished" not in h5group:
            raise RuntimeError("LeapfrogTuner has not finished, parameters have not been tuned")

        lastRecordGrp = loadList(h5group["records"])[-1][1]
        return {"length": lastRecordGrp["length"][()],
                "nstep": lastRecordGrp["nstep"][()]}

    @classmethod
    def loadTunedEvolver(cls, h5group, action, rng):
        params = cls.loadTunedParameters(h5group)
        return ConstStepLeapfrog(action, params["length"],
                                 params["nstep"], rng)

    @classmethod
    def loadRecording(cls, h5group):
        return Registrar.fromH5(h5group)

    def save(self, h5group, manager):
        r"""!
        Save the evolver to HDF5.
        \param h5group HDF5 group to save to.
        \param manager EvolverManager whose purview to save the evolver in.
        """
        # TODO

    @classmethod
    def fromH5(cls, h5group, _manager, action, _lattice, rng):
        r"""!
        Construct from HDF5.
        \param h5group HDF5 group to load parameters from.
        \param _manager \e ignored.
        \param action Action to use.
        \param _lattice \e ignored.
        \param rng Central random number generator for the run.
        \returns A newly constructed leapfrog evolver.
        """
        # TODO
        return None
