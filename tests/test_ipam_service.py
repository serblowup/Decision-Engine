"""Unit-тесты для IPAMService."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.ipam.ipam_service import IPAMService, SubnetCalculator, SplitResult, MergeResult

class TestSubnetCalculator:
    """Тесты для SubnetCalculator."""
    
    def test_split_basic(self):
        """Базовое разбиение подсети."""
        calc = SubnetCalculator()
        prefix = "192.168.10.0/24"
        print(f"\nИсходный префикс: {prefix}")
        
        child1, child2 = calc.split(prefix)
        print(f"Результат разбиения:")
        print(f"  -> {child1}")
        print(f"  -> {child2}")
        
        assert child1 == "192.168.10.0/25"
        assert child2 == "192.168.10.128/25"

    def test_split_minimum(self):
        """Разбиение слишком маленькой подсети (должно вызвать исключение)."""
        calc = SubnetCalculator()
        prefix = "192.168.10.0/30"
        print(f"\nИсходный префикс: {prefix}")
        print(f"Ожидаемая ошибка: Prefix too small for split (prefixlen >= 30)")
        
        with pytest.raises(ValueError, match="Prefix too small"):
            calc.split(prefix)
        print("Исключение получено корректно")

    def test_split_30_mask(self):
        """Разбиение /30 (должно вызвать исключение)."""
        calc = SubnetCalculator()
        prefix = "192.168.10.0/30"
        print(f"\nИсходный префикс: {prefix}")
        
        with pytest.raises(ValueError, match="Prefix too small"):
            calc.split(prefix)

    def test_split_31_mask(self):
        """Разбиение /31 (должно вызвать исключение)."""
        calc = SubnetCalculator()
        prefix = "192.168.10.0/31"
        print(f"\nИсходный префикс: {prefix}")
        
        with pytest.raises(ValueError, match="Prefix too small"):
            calc.split(prefix)

    def test_split_32_mask(self):
        """Разбиение /32 (должно вызвать исключение)."""
        calc = SubnetCalculator()
        prefix = "192.168.10.1/32"
        print(f"\nИсходный префикс: {prefix}")
        
        with pytest.raises(ValueError, match="Prefix too small"):
            calc.split(prefix)

    def test_merge_adjacent(self):
        """Слияние двух смежных подсетей."""
        calc = SubnetCalculator()
        prefix1 = "192.168.10.0/25"
        prefix2 = "192.168.10.128/25"
        print(f"\nИсходные префиксы:")
        print(f"  P1: {prefix1}")
        print(f"  P2: {prefix2}")
        
        merged = calc.merge(prefix1, prefix2)
        print(f"Результат слияния: {merged}")
        
        assert merged == "192.168.10.0/24"

    def test_merge_non_adjacent(self):
        """Слияние двух несмежных подсетей (должно вызвать исключение)."""
        calc = SubnetCalculator()
        prefix1 = "192.168.10.0/25"
        prefix2 = "192.168.20.0/25"
        print(f"\nИсходные префиксы:")
        print(f"  P1: {prefix1}")
        print(f"  P2: {prefix2}")
        
        with pytest.raises(ValueError, match="not in the same supernet"):
            calc.merge(prefix1, prefix2)

    def test_merge_different_prefix_lengths(self):
        """Слияние подсетей с разной длиной маски (должно вызвать исключение)."""
        calc = SubnetCalculator()
        prefix1 = "192.168.10.0/24"
        prefix2 = "192.168.10.0/25"
        print(f"\nИсходные префиксы:")
        print(f"  P1: {prefix1} (длина маски: 24)")
        print(f"  P2: {prefix2} (длина маски: 25)")
        print(f"Ожидаемая ошибка: Prefix lengths differ")
        
        with pytest.raises(ValueError, match="Prefix lengths differ"):
            calc.merge(prefix1, prefix2)

    def test_merge_not_covering_entire_supernet(self):
        """Слияние подсетей, которые не покрывают весь супернет."""
        calc = SubnetCalculator()
        prefix1 = "192.168.10.0/26"
        prefix2 = "192.168.10.192/26"
        print(f"\nИсходные префиксы:")
        print(f"  P1: {prefix1}")
        print(f"  P2: {prefix2}")
        
        with pytest.raises(ValueError, match="not in the same supernet"):
            calc.merge(prefix1, prefix2)

    def test_get_gateway(self):
        """Получение адреса шлюза."""
        calc = SubnetCalculator()
        
        test_cases = [
            ("192.168.10.0/24", "192.168.10.1"),
            ("10.0.0.0/8", "10.0.0.1"),
            ("192.168.1.128/25", "192.168.1.129"),
        ]
        
        print(f"\nТестирование получения шлюзов:")
        for prefix, expected in test_cases:
            gateway = calc.get_gateway(prefix)
            print(f"  {prefix} -> {gateway}")
            assert gateway == expected

    def test_get_dhcp_range(self):
        """Получение DHCP-диапазона."""
        calc = SubnetCalculator()
        prefix = "192.168.10.0/24"
        print(f"\nИсходный префикс: {prefix}")
        
        start, end = calc.get_dhcp_range(prefix)
        print(f"DHCP диапазон: {start} - {end}")
        
        assert start == "192.168.10.11"
        assert end == "192.168.10.244"

    def test_get_dhcp_range_custom_exclude(self):
        """Получение DHCP-диапазона с кастомными исключениями."""
        calc = SubnetCalculator()
        prefix = "192.168.10.0/24"
        exclude_first = 20
        exclude_last = 20
        print(f"\nИсходный префикс: {prefix}")
        print(f"Исключения: первые {exclude_first}, последние {exclude_last}")
        
        start, end = calc.get_dhcp_range(prefix, exclude_first=exclude_first, exclude_last=exclude_last)
        print(f"DHCP диапазон: {start} - {end}")
        
        assert start == "192.168.10.21"
        assert end == "192.168.10.234"

    def test_get_dhcp_range_too_small(self):
        """DHCP-диапазон для слишком маленькой подсети."""
        calc = SubnetCalculator()
        prefix = "192.168.10.0/29"
        print(f"\nИсходный префикс: {prefix}")
        
        with pytest.raises(ValueError, match="DHCP range invalid"):
            calc.get_dhcp_range(prefix)

class TestIPAMService:
    """Тесты для IPAMService."""
    
    def test_generate_actions_split(self):
        """Генерация действий для SplitResult."""
        service = IPAMService(db_pool=None)
        result = SplitResult(
            parent_vlan_id=10,
            child1_vlan_id=20,
            child1_prefix="192.168.10.0/25",
            child2_vlan_id=30,
            child2_prefix="192.168.10.128/25",
            child1_gateway="192.168.10.1",
            child2_gateway="192.168.10.129",
        )
        
        print(f"\nSplitResult:")
        print(f"  Родительский VLAN: {result.parent_vlan_id}")
        print(f"  Дочерние VLAN: {result.child1_vlan_id}, {result.child2_vlan_id}")
        print(f"  Префиксы: {result.child1_prefix}, {result.child2_prefix}")

        actions = service.generate_actions(result)
        
        print(f"Сгенерировано {len(actions)} действий:")
        for i, action in enumerate(actions):
            print(f"  {i+1}. {action['action_type']}: {action['params']}")

        assert len(actions) == 3
        assert actions[0]["action_type"] == "DELETE_SUBINTERFACE"
        assert actions[0]["params"]["vlan_id"] == 10
        assert actions[1]["action_type"] == "CREATE_SUBINTERFACE"
        assert actions[1]["params"]["vlan_id"] == 20
        assert actions[1]["params"]["ip_address"] == "192.168.10.1"
        assert actions[2]["action_type"] == "CREATE_SUBINTERFACE"
        assert actions[2]["params"]["vlan_id"] == 30
        assert actions[2]["params"]["ip_address"] == "192.168.10.129"

    def test_generate_actions_merge(self):
        """Генерация действий для MergeResult."""
        service = IPAMService(db_pool=None)
        result = MergeResult(
            vlan1_id=10,
            vlan2_id=20,
            merged_vlan_id=20,
            merged_prefix="192.168.10.0/24",
            merged_gateway="192.168.10.1",
        )
        
        print(f"\nMergeResult:")
        print(f"  VLAN для слияния: {result.vlan1_id}, {result.vlan2_id}")
        print(f"  Целевой VLAN: {result.merged_vlan_id}")
        print(f"  Новый префикс: {result.merged_prefix}")

        actions = service.generate_actions(result)
        
        print(f"Сгенерировано {len(actions)} действий:")
        for i, action in enumerate(actions):
            print(f"  {i+1}. {action['action_type']}: {action['params']}")

        assert len(actions) == 3
        assert actions[0]["action_type"] == "DELETE_SUBINTERFACE"
        assert actions[0]["params"]["vlan_id"] == 10
        assert actions[1]["action_type"] == "DELETE_SUBINTERFACE"
        assert actions[1]["params"]["vlan_id"] == 20
        assert actions[2]["action_type"] == "CREATE_SUBINTERFACE"
        assert actions[2]["params"]["vlan_id"] == 20
        assert actions[2]["params"]["ip_address"] == "192.168.10.1"

    def test_generate_actions_unknown_type(self):
        """Генерация действий для неизвестного типа результата."""
        service = IPAMService(db_pool=None)
        print(f"\nТест с неизвестным типом: 'unknown'")
        
        actions = service.generate_actions("unknown")
        print(f"Результат: {actions}")
        
        assert actions == []

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])