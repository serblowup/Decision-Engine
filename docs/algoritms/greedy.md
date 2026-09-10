# Описание жадного алгоритма псевдокодом
```text
ВХОД: model, constraints, current

model - граф сети с весами трафика 
constraints - ограничения (запрещённые/обязательные пары, мин/макс размер VLAN)
current - текущая сегментация

ВЫХОД: SegmentationResult

ЕСЛИ |current| <= 1 ИЛИ число VLAN <= 1 ИЛИ нет рёбер:
    ВЕРНУТЬ passthrough(current)

best ← копия current
current_value ← evaluate(best)
iterations ← 0

ПОКА iterations < max_iterations:
    iterations ← iterations + 1
    best_candidate ← None
    best_delta ← -∞
    groups ← сгруппировать устройства по VLAN

    ДЛЯ каждой пары VLAN (a, b) из groups:
        delta ← 2*W(Sa, Sb) - λ*(W(Sa,Sa) + W(Sb,Sb))
        ЕСЛИ delta <= threshold ИЛИ delta <= best_delta:
            ПРОДОЛЖИТЬ
        candidate ← слить a и b
        ЕСЛИ constraints.validate(candidate) не прошёл:
            ПРОДОЛЖИТЬ
        eval_delta ← evaluate(candidate) - current_value
        ЕСЛИ eval_delta > best_delta И eval_delta > threshold:
            best_delta ← eval_delta
            best_candidate ← candidate

    ДЛЯ каждого VLAN v с |devices| >= 2:
        part_a, part_b ← split_partition(devices)
        ЕСЛИ part_a пуст ИЛИ part_b пуст:
            ПРОДОЛЖИТЬ
        delta ← λ*W(A,B) - W(A,A) - W(B,B)
        ЕСЛИ delta <= threshold ИЛИ delta <= best_delta:
            ПРОДОЛЖИТЬ
        candidate ← разделить v на part_a и part_b
        ЕСЛИ constraints.validate(candidate) не прошёл:
            ПРОДОЛЖИТЬ
        eval_delta ← evaluate(candidate) - current_value
        ЕСЛИ eval_delta > best_delta И eval_delta > threshold:
            best_delta ← eval_delta
            best_candidate ← candidate

    ЕСЛИ best_candidate = None:
        ПРЕРВАТЬ
    best ← best_candidate
    current_value ← evaluate(best)

ВЕРНУТЬ SegmentationResult(best, current_value, iterations, "greedy", время)
```
