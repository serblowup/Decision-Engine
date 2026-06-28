from __future__ import annotations

import logging
from collections import Counter
from time import perf_counter

import numpy as np
from scipy import linalg
from sklearn.cluster import KMeans

from src.network.model import NetworkModel
from src.segmenters.interface import (
    ConstraintSet,
    Segmentation,
    SegmentationResult,
    build_passthrough_result,
)

logger = logging.getLogger(__name__)


class SpectralSegmenter:
    """Spectral graph partitioning segmenter.

    The method is based on spectral graph theory and normalized cuts:

    - Shi J., Malik J. "Normalized Cuts and Image Segmentation", IEEE TPAMI, 2000.
    - Ng A., Jordan M., Weiss Y. "On Spectral Clustering: Analysis and an Algorithm",
      NeurIPS, 2002.

    Mathematical foundation:
    Let W be the affinity matrix and D the degree diagonal matrix.
    The normalized Laplacian is:

        L = D^{-1/2} (D - W) D^{-1/2}

    Eigenvectors of L encode optimal graph partition structure under
    the Normalized Cut criterion:

        NCut(S) = sum_i cut(S_i, V\\S_i) / vol(S_i)

    where cut is the sum of crossing edge weights and vol is segment volume.

    Fiedler's theorem states that the second smallest eigenvector of L gives the
    optimal 2-way split by sign. For k clusters, we use the k smallest non-zero
    eigenvectors and run k-means in that spectral embedding.

    Complexity is O(n^3) for dense eigendecomposition; practical sparse/iterative
    methods (e.g. scipy.sparse.linalg.eigsh) often reduce it to around O(n * k).
    """

    def __init__(self, n_clusters: int | None = None, random_state: int = 42) -> None:
        self.n_clusters = n_clusters
        self.random_state = random_state

    def get_name(self) -> str:
        return "spectral"

    def get_complexity(self) -> str:
        return "O(n^3) dense, ~O(n*k) practical iterative"

    def optimize(
        self,
        model: NetworkModel,
        constraints: ConstraintSet,
        current: Segmentation,
    ) -> SegmentationResult:
        start = perf_counter()
        if len(current) <= 1 or len(set(current.values())) <= 1:
            return build_passthrough_result(self.get_name(), model, current)

        device_ids = sorted(current.keys())
        index = {device_id: idx for idx, device_id in enumerate(device_ids)}
        n = len(device_ids)

        affinity = np.zeros((n, n), dtype=float)
        for src, dst, attrs in model.graph.edges(data=True):
            if src not in index or dst not in index:
                continue
            i = index[src]
            j = index[dst]
            weight = float(attrs.get("weight", 0.0))
            affinity[i, j] += weight
            affinity[j, i] += weight

        if np.allclose(affinity, 0.0):
            logger.warning("SpectralSegmenter cannot optimize without traffic data; returning current segmentation")
            return build_passthrough_result(self.get_name(), model, current)

        degrees = np.sum(affinity, axis=1)
        with np.errstate(divide="ignore"):
            inv_sqrt = np.where(degrees > 0, 1.0 / np.sqrt(degrees), 0.0)
        d_inv_sqrt = np.diag(inv_sqrt)
        laplacian = np.diag(degrees) - affinity
        normalized_laplacian = d_inv_sqrt @ laplacian @ d_inv_sqrt

        eigenvalues, eigenvectors = linalg.eigh(normalized_laplacian)

        current_vlans = sorted(set(current.values()))
        k = self.n_clusters if self.n_clusters is not None else len(current_vlans)
        k = max(1, min(k, n))
        if k == 1:
            return build_passthrough_result(self.get_name(), model, current)

        nonzero_indices = [idx for idx, eig in enumerate(eigenvalues) if eig > 1e-10]
        if len(nonzero_indices) < k:
            selected_indices = list(range(k))
        else:
            selected_indices = nonzero_indices[:k]

        embedding = eigenvectors[:, selected_indices]
        row_norms = np.linalg.norm(embedding, axis=1, keepdims=True)
        row_norms[row_norms == 0.0] = 1.0
        embedding = embedding / row_norms

        kmeans = KMeans(n_clusters=k, random_state=self.random_state, n_init=10)
        labels = kmeans.fit_predict(embedding)

        mapping = self._map_clusters_to_vlans(labels, device_ids, current, current_vlans)
        candidate: Segmentation = {device_id: mapping[labels[idx]] for idx, device_id in enumerate(device_ids)}
        candidate = self._repair_candidate(candidate, constraints, current)

        if constraints.validate(candidate):
            candidate = dict(current)

        return SegmentationResult(
            segmentation=candidate,
            objective_value=model.evaluate(candidate),
            iterations=1,
            algorithm_name=self.get_name(),
            duration_seconds=perf_counter() - start,
        )

    def _map_clusters_to_vlans(
        self,
        labels: np.ndarray,
        device_ids: list[int],
        current: Segmentation,
        current_vlans: list[int],
    ) -> dict[int, int]:
        cluster_to_vlan: dict[int, int] = {}
        available_vlans = list(current_vlans)

        for cluster_id in sorted(set(labels.tolist())):
            members = [device_ids[idx] for idx, label in enumerate(labels) if label == cluster_id]
            preferred = Counter(current[device_id] for device_id in members).most_common(1)[0][0]
            if preferred in available_vlans:
                cluster_to_vlan[cluster_id] = preferred
                available_vlans.remove(preferred)
            elif available_vlans:
                cluster_to_vlan[cluster_id] = available_vlans.pop(0)
            else:
                cluster_to_vlan[cluster_id] = max(current_vlans) + cluster_id + 1
        return cluster_to_vlan

    def _repair_candidate(
        self,
        segmentation: Segmentation,
        constraints: ConstraintSet,
        fallback: Segmentation,
    ) -> Segmentation:
        candidate = dict(segmentation)
        vlans = sorted(set(candidate.values()))

        for left, right in constraints.required_pairs:
            if left in candidate and right in candidate:
                candidate[right] = candidate[left]

        for left, right in constraints.forbidden_pairs:
            if left in candidate and right in candidate and candidate[left] == candidate[right]:
                alternatives = [vlan for vlan in vlans if vlan != candidate[left]]
                if alternatives:
                    candidate[right] = alternatives[0]

        if constraints.min_vlan_size > 1 or constraints.max_vlan_size < 10**9:
            counts = Counter(candidate.values())
            for vlan_id, size in list(counts.items()):
                while size > constraints.max_vlan_size:
                    movable = [d for d, v in candidate.items() if v == vlan_id]
                    if not movable:
                        break
                    target_vlan = min(counts, key=counts.get)
                    device_id = movable[-1]
                    candidate[device_id] = target_vlan
                    counts[vlan_id] -= 1
                    counts[target_vlan] += 1
                    size = counts[vlan_id]

            for vlan_id, size in list(counts.items()):
                while size < constraints.min_vlan_size:
                    donor = max(counts, key=counts.get)
                    donor_members = [d for d, v in candidate.items() if v == donor]
                    if donor == vlan_id or not donor_members or counts[donor] <= constraints.min_vlan_size:
                        return dict(fallback)
                    device_id = donor_members[-1]
                    candidate[device_id] = vlan_id
                    counts[vlan_id] += 1
                    counts[donor] -= 1
                    size = counts[vlan_id]

        return candidate

