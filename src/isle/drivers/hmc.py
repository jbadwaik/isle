from pathlib import Path

import numpy as np
import yaml
import h5py as h5

from .. import Vector
from .. import fileio

class HMC:
    def __init__(self, lat, params, rng, action, outfname, startIdx):
        self.lat = lat
        self.params = params
        self.rng = rng
        self.ham = action  # TODO rewrite for pure action
        self.outfname = str(outfname)

        self._trajIdx = startIdx


    def __call__(self, phi, proposer, ntr,  saveFreq, checkpointFreq):
        if checkpointFreq != 0 and checkpointFreq % saveFreq != 0:
            raise ValueError("checkpointFreq must be a multiple of saveFreq."
                             f" Got {checkpointFreq} and {saveFreq}, resp.")

        acc = 1  # was last trajectory accepted? (int so it can be written as trajPoint)
        act = None  # running action (without pi)
        for _ in range(ntr):
            # get initial conditions for proposer
            startPhi, startPi, startEnergy = _initialConditions(self.ham, phi, act, self.rng)

            # evolve fields using proposer
            endPhi, endPi = proposer(startPhi, startPi, acc)
            # get new energy
            endEnergy = self.ham.eval(endPhi, endPi)

            # TODO consistency checks

            # accept-reject
            deltaE = np.real(endEnergy - startEnergy)
            if deltaE < 0 or np.exp(-deltaE) > self.rng.uniform(0, 1):
                acc = 1
                phi = endPhi
                act = self.ham.stripMomentum(endPi, endEnergy)
            else:
                acc = 0
                phi = startPhi
                act = self.ham.stripMomentum(startPi, startEnergy)

            # TODO inline meas

            if saveFreq != 0 and self._trajIdx % saveFreq == 0:
                if checkpointFreq != 0 and self._trajIdx % checkpointFreq == 0:
                    self.saveFieldAndCheckpoint(phi, act, acc)
                else:
                    self.save(phi, act, acc)
            else:
                self.advance()

        return phi

    def saveFieldAndCheckpoint(self, phi, act, acc):
        "!Write a trajectory (endpoint) and checkpoint to file and advance internal counter."
        with h5.File(self.outfname, "a") as outf:
            cfgGrp = self._writeTrajectory(outf, phi, act, acc)
            self._writeCheckpoint(outf, cfgGrp)
        self._trajIdx += 1

    def save(self, phi, act, acc):
        "!Write a trajectory (endpoint) to file and advance internal counter."
        with h5.File(self.outfname, "a") as outf:
            self._writeTrajectory(outf, phi, act, acc)
        self._trajIdx += 1

    def advance(self, amount=1):
        "!Advance the internal trajectory counter by amount without saving."
        self._trajIdx += amount

    def _writeTrajectory(self, h5file, phi, act, trajPoint):
        "!Write a trajectory (endpoint) to a HDF5 group."
        try:
            return fileio.h5.writeTrajectory(h5file["configuration"], self._trajIdx,
                                             phi, act, trajPoint)
        except (ValueError, RuntimeError) as err:
            if "name already exists" in err.args[0]:
                raise RuntimeError(f"Cannot write trajectory {self._trajIdx} to file '{self.outfname}'."
                                   " A dataset with the same name already exists.") from None
            raise

    def _writeCheckpoint(self, h5file, trajGrp):
        "!Write a checkpoint to a HDF5 group."
        try:
            return fileio.h5.writeCheckpoint(h5file["checkpoint"], self._trajIdx,
                                             self.rng, trajGrp.name)
        except (ValueError, RuntimeError) as err:
            if "name already exists" in err.args[0]:
                raise RuntimeError(f"Cannot write checkpoint for trajectory {self._trajIdx} to file '{self.outfname}'."
                                   " A dataset with the same name already exists.") from None
            raise


def readMetadata(fname):
    """!
    Read metadata on ensemble from HDF5 file.

    \returns Lattice, parameters, makeAction (source code of function)
    """
    with h5.File(str(fname), "r") as inf:
        lat = yaml.safe_load(inf["lattice"][()])
        params = yaml.safe_load(inf["params"][()])
        makeActionSrc = inf["action"][()]
    return lat, params, makeActionSrc


def init(lat, params, rng, makeAction, outfile,
         overwrite, startIdx=0):

    _ensureIsValidOutfile(outfile, overwrite, startIdx, lat, params)

    makeActionSrc = fileio.sourceOfFunction(makeAction)
    if not outfile[0].exists():
        _prepareOutfile(outfile[0], lat, params, makeActionSrc)

    driver = HMC(lat, params, rng, fileio.callFunctionFromSource(makeActionSrc, lat, params),
                 outfile[0], startIdx)
    return driver


def _prepareOutfile(outfname, lat, params, makeActionSrc):
    # TODO write Version(s)  -  write function in h5io

    with h5.File(str(outfname), "w") as outf:
        outf["lattice"] = yaml.dump(lat)
        outf["params"] = yaml.dump(params)
        outf["action"] = makeActionSrc
        fileio.h5.createH5Group(outf, "configuration")
        fileio.h5.createH5Group(outf, "checkpoint")

def _latestConfig(fname):
    "!Get greatest index of stored configs."
    with h5.File(str(fname), "r") as h5f:
        return max(map(int, h5f["configuration"].keys()), default=0)

def _verifyConfigsByException(outfname, startIdx):
    # TODO what about checkpoints?

    lastStored = _latestConfig(outfname)
    if lastStored > startIdx:
        print(f"Error: Output file '{outfname}' exists and has entries with higher index than HMC start index."
              f"Greates index in file: {lastStored}, user set start index: {startIdx}")
        raise RuntimeError("Cannot write into output file, contains newer data")

def _verifyMetadataByException(outfname, lat, params):
    storedLat, storedParams, _ = readMetadata(outfname)

    if storedLat.name != lat.name:
        print(f"Error: Name of lattice in output file is {storedLat.name} but new lattice has name {lat.name}. Cannot write into existing output file.")
        raise RuntimeError("Lattice name inconsistent")

    if storedParams.asdict() != params.asdict():
        print(f"Error: Stored parameters do not match new parameters. Cannot write into existing output file.")
        raise RuntimeError("Parameters inconsistent")

def _ensureIsValidOutfile(outfile, overwrite, startIdx, lat, params):
    """!
    Check if the output file is a valid parameter and if it is possible to write to it.
    Deletes the file if `overwrite == True`.

    Writing is not possible if the file exists and `overwrite == False` and
    it contains configurations with an index greater than `startIdx`.

    \throws ValueError if output file type is not supported.
    \throws RuntimeError if the file is not valid.
    """

    if outfile is None:
        print("Error: no output file given")
        raise RuntimeError("No output file given to HMC driver.")

    if outfile[1] != fileio.FileType.HDF5:
        raise ValueError(f"Output file type no supported by HMC driver. Output file is '{outfile[0]}'")

    outfname = outfile[0]
    if outfname.exists():
        if overwrite:
            print(f"Output file '{outfname}' exists -- overwriting")
            outfname.unlink()

        else:
            _verifyConfigsByException(outfname, startIdx)
            _verifyMetadataByException(outfname, lat, params)
            # TODO verify version(s)
            print(f"Output file '{outfname}' exists -- appending")


def _initialConditions(ham, oldPhi, oldAct, rng):
    r"""!
    Construct initial conditions for proposer.

    \param ham Hamiltonian.
    \param oldPhi Old configuration, result of previous run or some new phi.
    \param oldAct Old action, result of previous run or `None` if first run.
    \param rng Randum number generator that implements isle.random.RNGWrapper.

    \returns Tuple `(phi, pi, energy)`.
    """

    # new random pi
    pi = Vector(rng.normal(0, 1, len(oldPhi))+0j)
    if oldAct is None:
        # need to compute energy from scratch
        energy = ham.eval(oldPhi, pi)
    else:
        # use old action for energy
        energy = ham.addMomentum(pi, oldAct)
    return oldPhi, pi, energy
