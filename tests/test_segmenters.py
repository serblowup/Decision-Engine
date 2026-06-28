from datetime import datetime, timezone
from uuid import uuid4

from src.models.network import Device, DeviceType, NetworkState, PortMode, VlanAssignment, VlanInfo
from src.network.model import NetworkModel
from src.segmenters.greedy import GreedySegmenter
from src.segmenters.interface import ConstraintSet
from src.segmenters.simulated_annealing import SimulatedAnnealingSegmenter
from src.segmenters.spectral import SpectralSegmenter


def _tiny_state() -> tuple[NetworkState, dict[int, object]]:
    d1 = uuid4()
    d2 = uuid4()
    state = NetworkState(
        devices=[
            Device(
                id=d1,
                hostname="sw-1",
                mgmt_ip="10.0.0.1",
                device_type=DeviceType.SWITCH,
                status="ACTIVE",
            ),
            Device(
                id=d2,
                hostname="sw-2",
                mgmt_ip="10.0.0.2",
                device_type=DeviceType.SWITCH,
                status="ACTIVE",
            ),
        ],
        vlans=[
            VlanInfo(vlan_id=10, name="a", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=20, name="b", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[
            VlanAssignment(device_id=d1, port="Gi0/1", vlan_id=10, mode=PortMode.ACCESS),
            VlanAssignment(device_id=d2, port="Gi0/1", vlan_id=20, mode=PortMode.ACCESS),
        ],
        timestamp=datetime.now(timezone.utc),
    )
    return state, {1: d1, 2: d2}


def test_constraint_set_validate_detects_pair_violations():
    constraints = ConstraintSet(forbidden_pairs=[(1, 2)], required_pairs=[(2, 3)])
    violations = constraints.validate({1: 10, 2: 10, 3: 20})
    assert violations


def test_segmenters_return_passthrough_on_no_traffic():
    state, idx = _tiny_state()
    model = NetworkModel(state, metrics=[])
    current = {idx[1]: 10, idx[2]: 20}
    constraints = ConstraintSet()

    greedy_result = GreedySegmenter().optimize(model, constraints, current)
    spectral_result = SpectralSegmenter(n_clusters=2).optimize(model, constraints, current)
    sa_result = SimulatedAnnealingSegmenter(max_iterations=10).optimize(model, constraints, current)

    assert greedy_result.segmentation == current
    assert spectral_result.segmentation == current
    assert sa_result.segmentation == current
