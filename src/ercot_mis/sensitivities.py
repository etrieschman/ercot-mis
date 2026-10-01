"""Shift factors on an assembled :class:`~ercot_mis.out.network.Network`: the one place they are computed.

``DcSystem`` factorizes the DC model once (``B theta = q`` with ERCOT's slack removed)
and answers flows and shift-factor rows from that factorization. ``DcSystem.outaged``
gives the same network under a contingency, by removing branches and moving split-bus
ends and factorizing again, which is exact for any number of outaged branches and
needs no special case when the contingency cuts part of the network off: what stays
connected to the slack is solved, and nodes cut off get a shift factor of zero.

Everything that needs a shift factor (``scripts/check_network.py``,
``scripts/check_prices.py``, later the flow check) goes through this module. It needs
``numpy`` and ``scipy``, which the rest of the package does not: install the
``sensitivities`` extra.
"""

from __future__ import annotations

import time

import numpy as np
import polars as pl
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from scipy.sparse.csgraph import connected_components

from .out.network import Network


class DcSystem:
    """Sparse DC power flow on a :class:`Network`."""

    def __init__(self, net: Network):
        b = net.branches
        self.n, self.m = net.n_nodes, net.n_branches
        self.n_all = self.n
        self.f, self.t = b["from_index"].to_numpy(), b["to_index"].to_numpy()
        self.y = 1.0 / (b["x_pu"].to_numpy() * b["tap_ratio"].to_numpy())
        self.slack = int(net.nodes.filter(pl.col("is_slack"))["index"][0])
        self.connected = np.ones(self.n, dtype=bool)
        self.n_islanded = 0
        self._factorize()

    def _factorize(self) -> None:
        m = self.m
        self.A = sp.csc_matrix((np.r_[np.ones(m), -np.ones(m)], (np.r_[self.f, self.t], np.r_[np.arange(m), np.arange(m)])), shape=(self.n_all, m))
        self.keep = np.array([i for i in range(self.n_all) if i != self.slack and self.connected[i]])
        B = (self.A @ sp.diags(self.y) @ self.A.T).tocsc()
        started = time.perf_counter()
        self.lu = spla.splu(B[self.keep][:, self.keep])
        self.seconds = time.perf_counter() - started

    def flows(self, q: np.ndarray) -> np.ndarray:
        """Branch flows for nodal injections ``q`` (length ``n``; the slack absorbs the imbalance)."""
        theta = np.zeros(self.n_all)
        rhs = np.zeros(self.n_all)
        rhs[:self.n] = q
        theta[self.keep] = self.lu.solve(rhs[self.keep])
        return self.y * (theta[self.f] - theta[self.t])

    def shift_factors(self, branch_idx) -> np.ndarray:
        """One row per branch in ``branch_idx``: the flow on it per unit injected at each node (withdrawn at the slack)."""
        branch_idx = list(branch_idx)
        if not branch_idx:
            return np.zeros((0, self.n))
        rhs = (self.A[:, branch_idx].toarray() * self.y[branch_idx])[self.keep]
        rows = np.zeros((self.n_all, len(branch_idx)))
        rows[self.keep] = self.lu.solve(rhs, trans="T")
        return rows[:self.n].T

    ptdf_rows = shift_factors

    def outaged(self, branch_idx, splits=()) -> DcSystem:
        """This network under a contingency.

        ``branch_idx`` are removed. ``splits`` lists ``(branch index, "from" or "to")``: a
        split-bus row moves that end of the branch from its bus to a new bus section, and
        the branches a contingency moves off the same bus stay joined to each other there.
        ``n_islanded`` counts the nodes the contingency cuts off from the slack.
        """
        other = object.__new__(DcSystem)
        other.n, other.m, other.slack = self.n, self.m, self.slack
        other.y = self.y.copy()
        other.y[list(branch_idx)] = 0.0
        other.f, other.t = self.f.copy(), self.t.copy()
        sections: dict[int, int] = {}
        for j, end in splits:
            ends = other.f if end == "from" else other.t
            ends[j] = sections.setdefault(int(ends[j]), self.n + len(sections))
        other.n_all = self.n + len(sections)
        live = other.y != 0
        graph = sp.csr_matrix((np.ones(int(live.sum())), (other.f[live], other.t[live])), shape=(other.n_all, other.n_all))
        _, component = connected_components(graph, directed=False)
        other.connected = component == component[self.slack]
        other.n_islanded = int((~other.connected[:self.n]).sum())
        other._factorize()
        return other
