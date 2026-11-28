"""Compute the equilibrium window of a particular entry, without shifting energies."""
from __future__ import annotations

import math

import numpy as np
from pymatgen.analysis.phase_diagram import GrandPotentialPhaseDiagram, PDEntry, PhaseDiagram
from pymatgen.core import Composition, Element
from scipy.optimize import linprog

HULL_EPS_EV_ATOM = 1e-8  # numerical zero for hull bookkeeping, not a grading tolerance
VOLTAGE_EPS = 1e-7


def to_entries(rows: list[dict]) -> list[PDEntry]:
    entries = []
    seen = set()
    for row in rows:
        phase_id = row['id']
        if phase_id in seen or not row['converged']:
            raise ValueError(f'Duplicate or unconverged phase: {phase_id}')
        seen.add(phase_id)
        comp = Composition(row['formula'])
        energy = float(row['energy_eV'])
        if not math.isfinite(energy) or comp.num_atoms != row['n_sites']:
            raise ValueError(f'Invalid energy or atom count: {phase_id}')
        entries.append(PDEntry(comp, energy, name=phase_id))
    return entries


def grand_potential_bounds(pd: PhaseDiagram, target: PDEntry, mobile: Element) -> tuple[float, float] | None:
    """Use pymatgen's exact transition bands; don't substitute a same-formula entry."""
    mus = pd.get_transition_chempots(mobile)
    bands = []
    for hi, lo in zip(mus, mus[1:]):
        diagram = GrandPotentialPhaseDiagram(pd.all_entries, {mobile: (hi + lo) / 2}, pd.elements)
        if any(entry.original_entry is target for entry in diagram.stable_entries):
            bands.append((hi, lo))
    if not bands:
        return None
    # For a finite interval, adjacent stable bands must form a single interval.
    if any(abs(a[1] - b[0]) > VOLTAGE_EPS for a, b in zip(bands, bands[1:])):
        raise RuntimeError('Target stable in disconnected bands')
    mu_ref = pd.el_refs[mobile].energy_per_atom
    return mu_ref - bands[0][0], mu_ref - bands[-1][1]


def supporting_plane_bounds(pd: PhaseDiagram, target: PDEntry, mobile: Element) -> tuple[float, float] | None:
    """Independent check: optimize chemical potentials subject to all phase inequalities."""
    elements = list(pd.elements)
    baseline = np.array([pd.el_refs[el].energy_per_atom for el in elements])
    # Use formation energies per atom to make the LP numerically well scaled.
    def constraint(entry):
        coeffs = np.array([entry.composition[el] for el in elements]) / entry.composition.num_atoms
        return coeffs, entry.energy_per_atom - coeffs @ baseline

    rows = [constraint(entry) for entry in pd.all_entries]
    a, b = zip(*rows, strict=True)
    at, bt = constraint(target)
    unit = np.zeros(len(elements))
    unit[elements.index(mobile)] = 1
    out = []
    for objective in (-unit, unit):
        result = linprog(objective, A_ub=a, b_ub=b, A_eq=[at], b_eq=[bt],
                         bounds=[(None, None)] * len(elements), method='highs',
                         options={'primal_feasibility_tolerance': 1e-9,
                                  'dual_feasibility_tolerance': 1e-9})
        if result.status == 2:
            return None
        if not result.success:
            raise RuntimeError(f'Nonfinite or failed chemical-potential optimization: {result.message}')
        out.append(float(-result.x[elements.index(mobile)]))
    return tuple(out) if out[1] - out[0] > VOLTAGE_EPS else None


def equilibrium_window(rows: list[dict], target_id: str = 'target_cif') -> dict:
    entries = to_entries(rows)
    target, = [e for e in entries if e.name == target_id]
    pd = PhaseDiagram(entries)
    li = Element('Li')
    decomposition, above = pd.get_decomp_and_e_above_hull(target)
    above = float(above)
    if above < -HULL_EPS_EV_ATOM:
        raise RuntimeError(f'Inconsistent energy above hull: {above}')
    stable = above <= HULL_EPS_EV_ATOM
    oracle_bounds = grand_potential_bounds(pd, target, li)
    check_bounds = supporting_plane_bounds(pd, target, li)
    if (oracle_bounds is None) != (check_bounds is None) or (
        oracle_bounds is not None and not np.allclose(oracle_bounds, check_bounds, atol=VOLTAGE_EPS, rtol=0)
    ):
        raise RuntimeError(f'Grand-potential bands and supporting plane disagree: {oracle_bounds} vs {check_bounds}')
    if not stable and oracle_bounds is not None:
        raise RuntimeError('A phase above the closed-system hull cannot have an equilibrium window')
    reduction, oxidation = (None, None) if oracle_bounds is None else oracle_bounds
    return {
        'reduction_voltage_V': reduction, 'oxidation_voltage_V': oxidation,
        'window_width_V': None if reduction is None else oxidation - reduction,
        'stable': stable, 'energy_above_hull_meV_atom': max(0., above) * 1000,
        'decomposition': {e.name: float(amount) for e, amount in decomposition.items()},
        'stable_phase_ids': sorted(e.name for e in pd.stable_entries),
        'independent_bounds_V': check_bounds,
    }
