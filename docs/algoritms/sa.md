# Описание имитиции отжига псевдокодом
```text
ВХОД: model, constraints, current

model - граф сети с весами трафика 
constraints - ограничения (запрещённые/обязательные пары, мин/макс размер VLAN)
current - текущая сегментация

ВЫХОД: SegmentationResult

ЕСЛИ |current| <= 1 ИЛИ число VLAN <= 1 ИЛИ нет рёбер:
    ВЕРНУТЬ passthrough(current)

rng ← Random(random_seed)
current_seg ← копия current
current_value ← evaluate(current_seg)
best_seg ← копия current_seg
best_value ← current_value

T ← initial_temperature
iterations ← 0
devices ← все устройства
vlan_pool ← все VLAN

ЕСЛИ |vlan_pool| < 2:
    ВЕРНУТЬ passthrough(current)

ПОКА T > min_temperature И iterations < max_iterations:
    iterations ← iterations + 1
    candidate ← копия current_seg

    device_id ← случайное устройство
    current_vlan ← candidate[device_id]
    alternatives ← VLAN кроме current_vlan
    ЕСЛИ alternatives пуст:
        T ← T * cooling_rate
        ПРОДОЛЖИТЬ
    candidate[device_id] ← случайная альтернатива

    ЕСЛИ constraints.validate(candidate) не прошёл:
        T ← T * cooling_rate
        ПРОДОЛЖИТЬ

    candidate_value ← evaluate(candidate)
    delta ← candidate_value - current_value

    ЕСЛИ delta > 0:
        accept ← True
    ИНАЧЕ:
        probability ← exp(delta / max(T, 1e-9))
        accept ← (rng.random() < probability)

    ЕСЛИ accept:
        current_seg ← candidate
        current_value ← candidate_value
        ЕСЛИ current_value > best_value:
            best_value ← current_value
            best_seg ← копия current_seg

    T ← T * cooling_rate

ВЕРНУТЬ SegmentationResult(best_seg, best_value, iterations, "sa", время)
```
