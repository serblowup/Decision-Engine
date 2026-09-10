"""IPAM-модуль для управления IP-адресацией VLAN:
- SPLIT: разбиение подсети на две;
- MERGE: слияние двух подсетей.
"""

import ipaddress
import logging
import json
from typing import Optional, Tuple, List, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class SplitResult:
    parent_vlan_id: int
    child1_vlan_id: int
    child1_prefix: str
    child2_vlan_id: int
    child2_prefix: str
    child1_gateway: str
    child2_gateway: str

@dataclass
class MergeResult:
    vlan1_id: int
    vlan2_id: int
    merged_vlan_id: int
    merged_prefix: str
    merged_gateway: str

class SubnetCalculator:
    """Калькулятор подсетей"""
    
    @staticmethod
    def split(prefix: str) -> Tuple[str, str]:
        network = ipaddress.IPv4Network(prefix, strict=False)
        if network.prefixlen >= 30:
            raise ValueError(f"Prefix too small for split: {prefix} (prefixlen >= 30)")
        subnets = list(network.subnets(new_prefix=network.prefixlen + 1))
        if len(subnets) != 2:
            raise ValueError(f"Expected 2 subnets, got {len(subnets)}")
        return str(subnets[0]), str(subnets[1])
    
    @staticmethod
    def merge(prefix1: str, prefix2: str) -> str:
        net1 = ipaddress.IPv4Network(prefix1, strict=False)
        net2 = ipaddress.IPv4Network(prefix2, strict=False)
        if net1.prefixlen != net2.prefixlen:
            raise ValueError(f"Prefix lengths differ: {net1.prefixlen} vs {net2.prefixlen}")
        if net1.network_address > net2.network_address:
            net1, net2 = net2, net1
        parent = net1.supernet(new_prefix=net1.prefixlen - 1)
        if not (net1.subnet_of(parent) and net2.subnet_of(parent)):
            raise ValueError(f"Subnets {prefix1} and {prefix2} are not in the same supernet")
        subnets = list(parent.subnets(new_prefix=net1.prefixlen))
        if len(subnets) != 2 or subnets[0] != net1 or subnets[1] != net2:
            raise ValueError(f"Subnets {prefix1} and {prefix2} do not cover the entire supernet")
        return str(parent)
    
    @staticmethod
    def get_gateway(prefix: str) -> str:
        network = ipaddress.IPv4Network(prefix, strict=False)
        return str(network.network_address + 1)
    
    @staticmethod
    def get_dhcp_range(prefix: str, exclude_first: int = 10, exclude_last: int = 10) -> Tuple[str, str]:
        network = ipaddress.IPv4Network(prefix, strict=False)
        first = network.network_address + exclude_first + 1
        last = network.broadcast_address - exclude_last - 1
        if first > last:
            raise ValueError(f"DHCP range invalid: {first} > {last}")
        return str(first), str(last)

class IPAMService:
    def __init__(self, db_pool=None):
        self.db_pool = db_pool
        self.calculator = SubnetCalculator()
    
    async def get_vlan_prefix(self, vlan_id: int) -> Optional[str]:
        if self.db_pool is None:
            return None
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT ip_prefix FROM vlan WHERE vlan_id = $1",
                    vlan_id
                )
                return str(row['ip_prefix']) if row and row['ip_prefix'] else None
        except Exception as e:
            logger.error(f"Failed to get prefix for VLAN {vlan_id}: {e}")
            return None
    
    async def get_vlan_info(self, vlan_id: int) -> Optional[Dict[str, Any]]:
        if self.db_pool is None:
            return None
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT vlan_id, name, ip_prefix, gateway_ip, dhcp_scope_start, dhcp_scope_end "
                    "FROM vlan WHERE vlan_id = $1",
                    vlan_id
                )
                if row:
                    result = dict(row)
                    for key in ['ip_prefix', 'gateway_ip', 'dhcp_scope_start', 'dhcp_scope_end']:
                        if key in result and result[key] is not None:
                            result[key] = str(result[key])
                    return result
                return None
        except Exception as e:
            logger.error(f"Failed to get info for VLAN {vlan_id}: {e}")
            return None
    
    async def split_subnet(self, vlan_id: int, new_vlan_ids: Tuple[int, int]) -> SplitResult:
        prefix = await self.get_vlan_prefix(vlan_id)
        if prefix is None:
            raise ValueError(f"VLAN {vlan_id} has no IP prefix")
        child1, child2 = self.calculator.split(prefix)
        gateway1 = self.calculator.get_gateway(child1)
        gateway2 = self.calculator.get_gateway(child2)
        
        await self._check_conflicts(child1, exclude=[vlan_id])
        await self._check_conflicts(child2, exclude=[vlan_id])
        
        logger.info(f"SPLIT: {prefix} -> {child1} (VLAN {new_vlan_ids[0]}) + {child2} (VLAN {new_vlan_ids[1]})")
        return SplitResult(
            parent_vlan_id=vlan_id,
            child1_vlan_id=new_vlan_ids[0],
            child1_prefix=child1,
            child2_vlan_id=new_vlan_ids[1],
            child2_prefix=child2,
            child1_gateway=gateway1,
            child2_gateway=gateway2
        )
    
    async def merge_subnets(self, vlan_id_1: int, vlan_id_2: int, target_vlan_id: int) -> MergeResult:
        prefix1 = await self.get_vlan_prefix(vlan_id_1)
        prefix2 = await self.get_vlan_prefix(vlan_id_2)
        if prefix1 is None:
            raise ValueError(f"VLAN {vlan_id_1} has no IP prefix")
        if prefix2 is None:
            raise ValueError(f"VLAN {vlan_id_2} has no IP prefix")
        merged = self.calculator.merge(prefix1, prefix2)
        gateway = self.calculator.get_gateway(merged)
        
        await self._check_conflicts(merged, exclude=[vlan_id_1, vlan_id_2])
        
        logger.info(f"MERGE: {prefix1} + {prefix2} -> {merged} (VLAN {target_vlan_id})")
        return MergeResult(
            vlan1_id=vlan_id_1,
            vlan2_id=vlan_id_2,
            merged_vlan_id=target_vlan_id,
            merged_prefix=merged,
            merged_gateway=gateway
        )
    
    async def _check_conflicts(self, prefix: str, exclude: List[int] = None):
        """Проверка конфликтов с другими VLAN"""
        if self.db_pool is None:
            return
        
        exclude = exclude or []
        network = ipaddress.IPv4Network(prefix, strict=False)
        
        try:
            async with self.db_pool.acquire() as conn:
                if exclude:
                    rows = await conn.fetch(
                        """
                        SELECT vlan_id, ip_prefix
                        FROM vlan
                        WHERE ip_prefix IS NOT NULL
                        AND vlan_id NOT IN ({})
                        """.format(','.join([str(e) for e in exclude])),
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT vlan_id, ip_prefix
                        FROM vlan
                        WHERE ip_prefix IS NOT NULL
                        """
                    )
                
                for row in rows:
                    existing = ipaddress.IPv4Network(str(row['ip_prefix']), strict=False)
                    if network.overlaps(existing):
                        raise ValueError(
                            f"Prefix {prefix} overlaps with existing VLAN {row['vlan_id']}: {existing}"
                        )
        except Exception as e:
            logger.error(f"Conflict check failed: {e}")
            raise
    
    def generate_actions(self, result) -> List[Dict[str, Any]]:
        actions = []
        if isinstance(result, SplitResult):
            actions.append({
                'action_type': 'DELETE_SUBINTERFACE',
                'params': {'vlan_id': result.parent_vlan_id}
            })
            actions.append({
                'action_type': 'CREATE_SUBINTERFACE',
                'params': {
                    'vlan_id': result.child1_vlan_id,
                    'ip_address': result.child1_gateway
                }
            })
            actions.append({
                'action_type': 'CREATE_SUBINTERFACE',
                'params': {
                    'vlan_id': result.child2_vlan_id,
                    'ip_address': result.child2_gateway
                }
            })
        elif isinstance(result, MergeResult):
            actions.append({
                'action_type': 'DELETE_SUBINTERFACE',
                'params': {'vlan_id': result.vlan1_id}
            })
            actions.append({
                'action_type': 'DELETE_SUBINTERFACE',
                'params': {'vlan_id': result.vlan2_id}
            })
            actions.append({
                'action_type': 'CREATE_SUBINTERFACE',
                'params': {
                    'vlan_id': result.merged_vlan_id,
                    'ip_address': result.merged_gateway
                }
            })
        return actions
