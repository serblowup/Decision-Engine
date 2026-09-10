"""Тесты IPAM с реальной PostgreSQL."""

import pytest
import asyncpg
import ipaddress
from src.ipam.ipam_service import IPAMService

@pytest.fixture(scope="function")
async def db_pool():
    """Фикстура для подключения к БД."""
    try:
        pool = await asyncpg.create_pool(
            host='localhost',
            port=5432,
            database='admin_panel',
            user='postgres',
            password='postgres',
            min_size=1,
            max_size=5,
        )
        yield pool
        await pool.close()
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

@pytest.mark.asyncio
async def test_get_vlan_prefix_real_db(db_pool):
    """Получение префикса VLAN из реальной БД."""
    pool = db_pool
    vlan_id = 100
    
    # Очистка
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vlan WHERE vlan_id = $1", vlan_id)
    
    # Создаём тестовый VLAN
    prefix = ipaddress.IPv4Network('192.168.100.0/24')
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO vlan (vlan_id, name, admin_status, ip_prefix)
            VALUES ($1, 'test', 'ACTIVE', $2)
        """, vlan_id, prefix)
    
    print(f"\nСоздан VLAN {vlan_id} с префиксом {prefix}")
    
    ipam = IPAMService(db_pool=pool)
    result = await ipam.get_vlan_prefix(vlan_id)
    
    print(f"Полученный префикс: {result}")
    
    assert str(result) == "192.168.100.0/24"
    
    # Очистка
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vlan WHERE vlan_id = $1", vlan_id)

@pytest.mark.asyncio
async def test_split_subnet_real_db(db_pool):
    """Тест SPLIT с реальной БД."""
    pool = db_pool
    parent_vlan = 200
    child1_vlan = 201
    child2_vlan = 202
    
    # Очистка
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vlan WHERE vlan_id IN ($1, $2, $3)", 
                          parent_vlan, child1_vlan, child2_vlan)
    
    # Создаём тестовый VLAN
    prefix = ipaddress.IPv4Network('192.168.200.0/24')
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO vlan (vlan_id, name, admin_status, ip_prefix)
            VALUES ($1, 'parent', 'ACTIVE', $2)
        """, parent_vlan, prefix)
    
    print(f"\nИсходный VLAN {parent_vlan} с префиксом {prefix}")
    print(f"Целевые VLAN: {child1_vlan}, {child2_vlan}")
    
    ipam = IPAMService(db_pool=pool)
    result = await ipam.split_subnet(parent_vlan, (child1_vlan, child2_vlan))
    
    print(f"\nРезультат разбиения:")
    print(f"  Родитель: VLAN {result.parent_vlan_id}")
    print(f"  Дочерний 1: VLAN {result.child1_vlan_id}, {result.child1_prefix}, шлюз {result.child1_gateway}")
    print(f"  Дочерний 2: VLAN {result.child2_vlan_id}, {result.child2_prefix}, шлюз {result.child2_gateway}")
    
    assert result.parent_vlan_id == parent_vlan
    assert result.child1_prefix == "192.168.200.0/25"
    assert result.child2_prefix == "192.168.200.128/25"
    
    # Очистка
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vlan WHERE vlan_id IN ($1, $2, $3)", 
                          parent_vlan, child1_vlan, child2_vlan)

@pytest.mark.asyncio
async def test_merge_subnets_real_db(db_pool):
    """Тест MERGE с реальной БД."""
    pool = db_pool
    vlan1 = 300
    vlan2 = 301
    target_vlan = 300
    
    # Очистка
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vlan WHERE vlan_id IN ($1, $2)", vlan1, vlan2)
    
    # Создаём два VLAN для слияния
    prefix1 = ipaddress.IPv4Network('192.168.30.0/25')
    prefix2 = ipaddress.IPv4Network('192.168.30.128/25')
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO vlan (vlan_id, name, admin_status, ip_prefix)
            VALUES 
                ($1, 'vlan1', 'ACTIVE', $2),
                ($3, 'vlan2', 'ACTIVE', $4)
        """, 
            vlan1, prefix1,
            vlan2, prefix2
        )
    
    print(f"\nИсходные VLAN:")
    print(f"  VLAN {vlan1}: {prefix1}")
    print(f"  VLAN {vlan2}: {prefix2}")
    print(f"Целевой VLAN для слияния: {target_vlan}")
    
    ipam = IPAMService(db_pool=pool)
    result = await ipam.merge_subnets(vlan1, vlan2, target_vlan)
    
    print(f"\nРезультат слияния:")
    print(f"  VLAN {result.vlan1_id} + VLAN {result.vlan2_id} -> VLAN {result.merged_vlan_id}")
    print(f"  Новый префикс: {result.merged_prefix}")
    print(f"  Новый шлюз: {result.merged_gateway}")
    
    assert result.vlan1_id == vlan1
    assert result.vlan2_id == vlan2
    assert result.merged_vlan_id == target_vlan
    assert result.merged_prefix == "192.168.30.0/24"
    
    # Очистка
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vlan WHERE vlan_id IN ($1, $2)", vlan1, vlan2)

@pytest.mark.asyncio
async def test_check_conflicts_real_db(db_pool):
    """Тест проверки конфликтов с реальной БД."""
    pool = db_pool
    existing_vlan = 400
    
    # Очистка
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vlan WHERE vlan_id = $1", existing_vlan)
    
    # Создаём существующий VLAN
    existing_prefix = ipaddress.IPv4Network('192.168.40.0/24')
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO vlan (vlan_id, name, admin_status, ip_prefix)
            VALUES ($1, 'existing', 'ACTIVE', $2)
        """, existing_vlan, existing_prefix)
    
    print(f"\nСуществующий VLAN {existing_vlan} с префиксом {existing_prefix}")
    
    ipam = IPAMService(db_pool=pool)
    
    # Тест 1: Должен быть конфликт
    new_prefix = "192.168.40.0/25"
    print(f"\nПроверка конфликта с префиксом {new_prefix} (должен быть конфликт)")
    
    with pytest.raises(ValueError, match="overlaps with existing VLAN"):
        await ipam._check_conflicts(new_prefix, exclude=[])
    print("  Конфликт обнаружен")
    
    # Тест 2: Без конфликта (исключаем существующий VLAN)
    print(f"\nПроверка конфликта с префиксом {new_prefix} (исключаем VLAN {existing_vlan})")
    await ipam._check_conflicts(new_prefix, exclude=[existing_vlan])
    print("  Конфликтов нет")
    
    # Очистка
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vlan WHERE vlan_id = $1", existing_vlan)

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])