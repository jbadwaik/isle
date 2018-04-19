#!/usr/bin/env python3
"""!
Investigate the ergodicity problem.
"""

import sys
from pathlib import Path
import numpy as np
import h5py as h5

import core
core.prepare_module_import()
import cns
import cns.meas


# TODO nt, spacetime in lattice
# TODO acceptance rate and action as post process from cfg file

def main(argv):
    """!Run HMC and measurements."""

    cns.env["latticeDirectory"] = str(Path(__file__).resolve().parent.parent/"lattices")

    ensemble = cns.importEnsemble(argv[1])

    # TODO encode initial conditions and RNG in ensemble
    rng = cns.random.NumpyRNG(1075)
    # initial state (hot)
    phi = cns.Vector(rng.normal(0, np.sqrt(ensemble.UTilde), ensemble.spacetime)+0j)

    measurements = [
        (1, cns.meas.LogDet(ensemble.kappaTilde, ensemble.mu, ensemble.sigma_kappa), "/logdet"),
        (1, cns.meas.TotalPhi(), "/field"),
        (100, cns.meas.SingleParticleCorrelator(ensemble.nt, ensemble.kappaTilde,
                                                ensemble.mu, ensemble.sigma_kappa,
                                                cns.Species.PARTICLE),
         "/correlation_functions/single_particle"),
        (100, cns.meas.SingleParticleCorrelator(ensemble.nt, ensemble.kappaTilde,
                                                ensemble.mu, ensemble.sigma_kappa,
                                                cns.Species.HOLE),
         "/correlation_functions/single_hole")
    ]

    print("thermalizing")
    phi = cns.hmc.hmc(phi, ensemble.hamiltonian,
                      ensemble.thermalizer,
                      ensemble.nTherm,
                      rng,
                      [
                          (ensemble.nTherm/10, cns.meas.Progress("Thermalization", ensemble.nTherm)),
                      ],
                      [(20, cns.checks.realityCheck)])

    print("running production")
    write = cns.meas.WriteConfiguration(ensemble.name+".h5", "/cfg/cfg_{itr}")
    phi = cns.hmc.hmc(phi, ensemble.hamiltonian, ensemble.proposer,
                      ensemble.nProduction, rng,
                      [
                          (100, write),
                          (500, cns.meas.Progress("Production", ensemble.nProduction)),
                          *[(freq, meas) for freq, meas, _ in measurements]
                      ],
                      [(20, cns.checks.realityCheck)])

    print("Saving measurements...")
    with h5.File(ensemble.name+".measurements.h5", "w") as measFile:
        for _, meas, path in measurements:
            meas.save(measFile, path)

if __name__ == "__main__":
    main(sys.argv)
