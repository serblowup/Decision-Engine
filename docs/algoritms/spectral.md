# Описание спектральной кластеризации псевдокодом
```text
ВХОД: model, constraints, current

model - граф сети с весами трафика 
constraints - ограничения (запрещённые/обязательные пары, мин/макс размер VLAN)
current - текущая сегментация

ВЫХОД: SegmentationResult

ЕСЛИ |current| <= 1 ИЛИ число VLAN <= 1:
    ВЕРНУТЬ passthrough(current)

device_ids ← отсортированные ключи current
index ← {device_id: позиция}
n ← |device_ids|

# Построить матрицу смежности
affinity ← нулевая матрица n×n
ДЛЯ каждого ребра (src, dst, weight) в графе:
    ЕСЛИ src И dst в index:
        i, j ← index[src], index[dst]
        affinity[i,j] += weight
        affinity[j,i] += weight

ЕСЛИ affinity ≈ 0:
    ВЕРНУТЬ passthrough(current)

# Нормализованный лапласиан
degrees ← сумма строк affinity
inv_sqrt ← 1/sqrt(degrees) (0 где degree=0)
D_inv_sqrt ← diag(inv_sqrt)
L ← diag(degrees) - affinity
L_norm ← D_inv_sqrt · L · D_inv_sqrt

# Собственное разложение
eigenvalues, eigenvectors ← eigh(L_norm)

# Выбор k
k ← n_clusters или число текущих VLAN
k ← clamp(k, 1, n)
ЕСЛИ k = 1: ВЕРНУТЬ passthrough(current)

# Спектральное вложение
nonzero ← индексы eigenvalues > 1e-10
ЕСЛИ |nonzero| < k:
    selected ← первые k индексов
ИНАЧЕ:
    selected ← первые k из nonzero
embedding ← eigenvectors[:, selected]
нормализовать каждую строку embedding к единичной длине

# K-means в спектральном пространстве
labels ← KMeans(k).fit_predict(embedding)

# Сопоставить кластеры с VLAN
mapping ← для каждого кластера выбрать VLAN,
          наиболее частый среди его устройств в current
candidate ← {device: mapping[labels[device]]}

# Починка под ограничения
candidate ← repair(candidate, constraints, fallback=current)

ЕСЛИ constraints.validate(candidate) не прошёл:
    candidate ← current

ВЕРНУТЬ SegmentationResult(candidate, evaluate(candidate), 1, "spectral", время)
```
