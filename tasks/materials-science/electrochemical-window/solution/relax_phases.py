"""Generate a resumable energy ledger for the pinned ECW phase set."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

import matgl
import numpy as np
import torch
from ase.filters import FrechetCellFilter
from ase.optimize import FIRE, LBFGS
from huggingface_hub import snapshot_download
from matgl.ext.ase import PESCalculator
from pymatgen.analysis.structure_matcher import StructureMatcher
from pymatgen.core import Structure
from pymatgen.io.ase import AseAtomsAdaptor

CHECKPOINT = 'materialyze/TensorNet-PES-MatPES-PBE-2025.2'
REVISION = '7ac2b90400a87eca7f44c1e7956dca6b1c85e418'
FILES = ('model.json', 'model.pt', 'state.pt')
TARGET = 'target_cif'
# Convergence is defined on physical forces/stresses, not only filter norms.
DEFAULT_FORCE = 0.01  # eV/Å
DEFAULT_STRESS = 0.0005  # eV/Å³ (roughly 0.08 GPa)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def initial_phases(snapshot: dict, cif: Path) -> list[tuple[str, Structure]]:
    if sha256(cif) != snapshot['target_cif_sha256']:
        raise ValueError('CIF differs from the frozen target')
    phases = {r['id']: Structure.from_dict(r['structure']) for r in snapshot['records']}
    if len(phases) != len(snapshot['records']):
        raise ValueError('Repeated phase ID in snapshot')
    target = Structure.from_file(cif)
    alias = phases.pop(snapshot['target_summary_id'])
    if not StructureMatcher(ltol=1e-4, stol=1e-4, angle_tol=0.01, scale=False).fit(target, alias):
        raise ValueError('Target alias does not match supplied CIF')
    return [(TARGET, target), *sorted(phases.items(), key=lambda item: (len(item[1]), item[0]))]


def relax(structure: Structure, calc: PESCalculator, path: Path, *, optimizer: str,
          force: float, stress: float, steps: int) -> dict:
    atoms = AseAtomsAdaptor.get_atoms(structure)
    atoms.calc = calc
    initial = float(atoms.get_potential_energy())
    cell_filter = FrechetCellFilter(atoms, exp_cell_factor=len(atoms))
    opt_class = {'fire': FIRE, 'lbfgs': LBFGS}[optimizer]
    with opt_class(cell_filter, logfile=str(path), maxstep=0.10) as opt:
        converged = False
        residual_force = residual_stress = float('inf')
        for _ in opt.irun(fmax=1e-10, steps=steps):
            residual_force = float(np.max(np.linalg.norm(atoms.get_forces(), axis=1)))
            residual_stress = float(np.max(np.abs(atoms.get_stress(voigt=False))))
            if residual_force <= force and residual_stress <= stress:
                converged = True
                break
        nsteps = opt.nsteps
    final = float(atoms.get_potential_energy())
    volume = float(atoms.get_volume())
    if not np.isfinite([initial, final, volume, residual_force, residual_stress]).all() or volume <= 0:
        raise RuntimeError('Nonfinite relaxation result or invalid cell')
    return {'converged': converged, 'steps': nsteps, 'formula': structure.composition.formula,
            'n_sites': len(atoms), 'energy_eV': final, 'initial_energy_eV': initial,
            'max_force_eV_A': residual_force, 'max_stress_eV_A3': residual_stress,
            'structure': AseAtomsAdaptor.get_structure(atoms).as_dict()}


def write_json(path: Path, obj: dict) -> None:
    partial = path.with_suffix('.tmp')
    partial.write_text(json.dumps(obj, sort_keys=True, allow_nan=False) + '\n')
    os.replace(partial, path)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('snapshot', type=Path)
    p.add_argument('cif', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('--device', choices=('cpu', 'cuda'), default='cuda')
    p.add_argument('--optimizer', choices=('fire', 'lbfgs'), default='fire')
    p.add_argument('--force', type=float, default=DEFAULT_FORCE)
    p.add_argument('--stress', type=float, default=DEFAULT_STRESS)
    p.add_argument('--steps', type=int, default=900)
    p.add_argument('--checkpoint-dir', type=Path)
    p.add_argument('--only', nargs='+', help='Run only these phase IDs; same protocol and ledger')
    p.add_argument('--restart-from', type=Path, help='Use converged geometries from a preceding ledger')
    p.add_argument('--allow-unconverged', action='store_true', help='Keep failed rows for an optional second pass')
    args = p.parse_args()
    if min(args.force, args.stress, args.steps) <= 0:
        p.error('convergence thresholds and step limit must be positive')
    snapshot = json.loads(args.snapshot.read_text())
    phases = initial_phases(snapshot, args.cif)
    selected = set(args.only) if args.only else {name for name, _ in phases}
    if not selected <= {name for name, _ in phases}:
        p.error('Unknown phase in --only')
    restart_sha = {}
    if args.restart_from:
        geometries = []
        for name, structure in phases:
            if name in selected:
                src = args.restart_from / f'{name}.json'
                row = json.loads(src.read_text())
                if row['id'] != name or not row['converged']:
                    p.error(f'Missing converged restart geometry for {name}')
                restarted = Structure.from_dict(row['structure'])
                if restarted.composition != structure.composition:
                    p.error(f'Invalid restart composition for {name}')
                structure = restarted
                restart_sha[name] = sha256(src)
            geometries.append((name, structure))
        phases = geometries
    directory = args.checkpoint_dir or Path(snapshot_download(CHECKPOINT, revision=REVISION, allow_patterns=FILES))
    config = {
        'snapshot_sha256': sha256(args.snapshot), 'target_cif_sha256': sha256(args.cif),
        'checkpoint': CHECKPOINT, 'revision': REVISION,
        'checkpoint_sha256': {name: sha256(directory / name) for name in FILES},
        'device': args.device, 'optimizer': args.optimizer, 'max_force_eV_A': args.force,
        'max_stress_eV_A3': args.stress, 'step_limit': args.steps,
        'packages': {name: importlib.metadata.version(name) for name in ('ase', 'matgl', 'numpy', 'pymatgen', 'torch')},
    }
    if restart_sha:
        config['restart_row_sha256'] = restart_sha
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / 'protocol.json'
    if manifest.exists() and json.loads(manifest.read_text()) != config:
        p.error('Existing relaxation ledger uses a different protocol')
    write_json(manifest, config)
    model = matgl.load_model(str(directory)).to(args.device)
    torch.set_float32_matmul_precision('highest')
    calc = PESCalculator(potential=model, stress_unit='eV/A3')
    failures = 0
    for i, (name, structure) in enumerate(phases, 1):
        dest = args.output / f'{name}.json'
        if name not in selected:
            continue
        if dest.exists() and json.loads(dest.read_text()).get('converged'):
            continue
        try:
            result = relax(structure, calc, args.output / f'{name}.log', optimizer=args.optimizer,
                           force=args.force, stress=args.stress, steps=args.steps)
        except Exception as exc:
            result = {'converged': False, 'error': f'{type(exc).__name__}: {exc}'}
        write_json(dest, {'id': name, **result})
        failures += not result['converged']
        print(f'{i}/{len(phases)} {name} {len(structure)} sites steps={result.get("steps")} '
              f'E={result.get("energy_eV")} converged={result["converged"]} {result.get("error", "")}', flush=True)
    if failures:
        message = f'{failures} phases did not converge; do not use this ledger alone for grading'
        if not args.allow_unconverged:
            sys.exit(message)
        print(message, file=sys.stderr)


if __name__ == '__main__':
    main()
