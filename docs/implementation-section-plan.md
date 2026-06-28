# Decision Engine — план раздела «Реализация» ПЗ ВКР

Документ построен по результату аудита репозитория `decision-engine` (ветка `main`, состояние на момент аудита). Сначала идёт фактическая инвентаризация (A), затем классификация материала по разделам ПЗ (B), затем предложение структуры глав раздела «Реализация» (C) и приложение с деревом файлов (D). Все ссылки — на конкретные пути и сущности; ничего не выдумано.

---

## A. Инвентарь репозитория

Таблицы сгруппированы по слоям. «Импортёры» — модули, реально содержащие `from ... import ...`. «Тесты» — `tests/test_*.py`, по которым модуль проходит хотя бы по одной функции. Покрытие — из последнего прогона `pytest --cov=src`.

### A.1. Точка входа и конфигурация

| Путь | Назначение | Ключевые сущности | Импортёры | Тесты / покрытие |
|---|---|---|---|---|
| `main.py` | Точка входа DE. Поднимает asyncpg-пул, Kafka-продюсер, VM-клиент, сегментатор, две стратегии, три триггера; стартует sync-gate; гоняет три async-задачи: `run_metrics_poller`, `_waiting_monitor_loop`, `_status_listener_loop`; реализует FSM IDLE↔WAITING с дедупликацией и LISTEN/NOTIFY на канале `reconfiguration_task_status_changed`. | `main()`, `_classify_batch_statuses`, `_build_segmenter`, `handle_metrics_batch` (closure), `_waiting_monitor_loop`, `_status_listener_loop`, `_set_waiting`, `_reset_to_idle`, `_evaluate_timeout_result`. | `tests/test_task_builder_scenarios.py` импортирует `_classify_batch_statuses`. | косвенно — через `_classify_batch_statuses`; основной цикл не покрыт unit-тестами. |
| `src/config.py` | `pydantic_settings.BaseSettings` со всеми параметрами DE; алиасы для ENV. | `Settings`, `settings` (singleton). | `src/db/network_state.py`, `src/db/sync_gate.py`, `src/kafka/producer.py`, `src/metrics_poller.py`, `src/strategies/threshold.py`, `src/strategies/orchestrator.py` (через `decide_with_priority`), `src/task_builder/builder.py`, `src/topology/graph.py`, `src/triggers/triggers.py`, `src/victoriametrics/client.py`, `src/victoriametrics/ip_utils.py`, `main.py`. | покрытие 100% (импортируется всеми тестами). |
| `src/__init__.py` | Пустой пакетный маркер. | — | автоматически. | 100%. |

### A.2. Модели данных

| Путь | Назначение | Ключевые сущности | Импортёры | Тесты / покрытие |
|---|---|---|---|---|
| `src/models/network.py` | Pydantic v2 модели сетевого инвентаря. | `DeviceType` (StrEnum), `PortMode` (StrEnum), `Device`, `VlanInfo` (с полем `is_protected`), `VlanAssignment`, `NetworkState` (включая `protected_vlans: set[int]` и `device_vlans`). | `src/db/network_state.py`, все стратегии, `src/task_builder/builder.py`, conftest, многие тесты. | 100%. |
| `src/models/operations.py` | Контракт сообщения `ReconfigurationTask` (отправляемого в Kafka) и его атомов. | `ActionType` (8 значений: ADD_VLAN, DELETE_VLAN, SET_ACCESS, SET_TRUNK, EDIT_TRUNK, SWITCH_VLAN, CREATE_SUBINTERFACE, DELETE_SUBINTERFACE), `ActionStatus`, `BatchCriticality` (CRITICAL/NORMAL/OPTIONAL), `TaskStatus`, `Action`, `Batch`, `ReconfigurationTask`. | `src/strategies/*`, `src/task_builder/*`, `src/topology/graph.py`, `src/kafka/producer.py`, `main.py`. | 100%. |

### A.3. Слой БД и стартовая синхра

| Путь | Назначение | Ключевые сущности | Импортёры | Тесты / покрытие |
|---|---|---|---|---|
| `src/db/network_state.py` | Чтения PostgreSQL. Сборка `NetworkState` через JOIN'ы, загрузка `Topology` из БД, чтение статусов задач, чтение статуса синхры из `topology_sync_status`. | `_fetch_rows`, `_fetch_row`, `get_network_state`, `get_assignments_for_vlan`, `get_device_vlans`, `get_router_subinterfaces`, `get_router_device_id`, `load_topology`, `get_task_batches`, `get_sync_state`, `create_pool`, `get_mock_network_state`. SQL-константы: `ACCESS_ASSIGNMENTS_SQL`, `TRUNK_ASSIGNMENTS_SQL`, `LINKS_SQL`, `DEVICE_INTERFACES_SQL`, `INTERFACE_MODES_SQL`, `TRUNK_ALLOWED_ALL_SQL`, `DEVICE_VLAN_ALL_SQL`, `ENDPOINT_ATTACH_SQL`, `PROTECTED_VLANS_SQL`, `SYNC_STATUS_SQL`. | `main.py`, `src/task_builder/builder.py`, `src/db/sync_gate.py`. | `test_topology_loader.py`, `test_sync_gate.py`, `test_protected_vlans.py`; ~83%. |
| `src/db/sync_gate.py` | Стартовый гейт: ожидание первой удачной синхры топологии перед первым циклом решений. LISTEN на канале `topology_sync_changed` подписывается ПЕРВЫМ, затем читается последняя строка `topology_sync_status`, далее цикл с NOTIFY+поллингом, проверкой свежести, таймаутом и политикой `proceed`/`keep_waiting`. | `TOPOLOGY_SYNC_NOTIFY_CHANNEL`, `STATUS_OK`/`FAILED`/`IN_PROGRESS`/`NEVER_SYNCED`, `RESULT_READY`/`RESULT_TIMEOUT`/`RESULT_DISABLED`, `BEHAVIOR_PROCEED`/`BEHAVIOR_KEEP_WAITING`, `_is_fresh`, `wait_for_initial_sync`, `_subscribe`, `run_sync_gate`. | `main.py`. | `test_sync_gate.py` (23 теста); ~93%. |
| `src/db/__init__.py` | Реэкспорт `create_pool`, `get_*`. | — | пакет. | 100%. |

### A.4. Структурная модель сети

| Путь | Назначение | Ключевые сущности | Импортёры | Тесты / покрытие |
|---|---|---|---|---|
| `src/topology/graph.py` (448 строк) | Топология сети как ориентированный объект «правды»: интерфейсы, режимы, allowed-VLAN, члены VLAN по устройствам, эндпойнты, защищённые VLAN. Стейнер-tree (`required_links`), генератор действий по линку (`trunk_actions_for_link`), per-interface capability gate (`interface_kind`), фабрики действий (`build_trunk_action`, `set_trunk_action`, `create_subinterface_action`, `delete_subinterface_action`), вспомогательные предикаты (`is_link_required`, `vlans_blocking_trunk_to_access`). | `src/strategies/rule_based.py`, `src/task_builder/builder.py`, `src/task_builder/dependencies.py`, `src/db/network_state.py` (deferred), `main.py` (тип). | `test_topology_loader.py`, `test_rule_based.py`, `test_vlan_expansion.py`, `test_capability_and_deps.py`, `test_protected_vlans.py`, `test_subinterface.py`, `test_edit_trunk.py`; ~87%. |
| `src/topology/__init__.py` | Реэкспорт публичных имён. | — | пакет. | 100%. |
| `src/task_builder/topology.py` (30 строк, шим) | Back-compat реэкспорт из `src.topology.graph`. | реэкспорт. | **нет импортёров.** | 0% — кандидат на DEAD_OR_UNUSED. |

### A.5. Стратегии

| Путь | Назначение | Ключевые сущности | Импортёры | Тесты / покрытие |
|---|---|---|---|---|
| `src/strategies/interface.py` | Контракт слоя стратегий. | `NetworkContext` (включает `topology`), `VlanDecision`, `StrategyDecision` (с `decisions`, `segmentation_result`, `batches`), `IDecisionStrategy` (Protocol). | все стратегии, `task_builder/builder.py`, `main.py`, `tests/test_*`. | 100%. |
| `src/strategies/threshold.py` (256 строк) | Пороговая эвристика над метриками: ISOLATE (anomaly/ICMP/new sources), REBALANCE (utilization), MERGE (inter_vlan_ratio + абсолютный пол `merge_min_inter_vlan_bytes_per_sec`); skip MERGE для protected-VLAN. | `ThresholdHeuristic`. Версия 3.1.0. | `main.py`, тесты. | `test_threshold_strategy.py`, `test_new_strategy.py`, `test_merge_loop_regression.py`, `test_protected_vlans.py`; ~89%. |
| `src/strategies/rule_based.py` (170 строк) | Топологические инварианты: group 1 — trunk-симметрия 802.1Q; group 2 — минимальность Штейнера. Реконсиляция per-interface `actual` vs `desired = ∪ {V: link∈required_links(V)}`; защищённые VLAN — hands-off; зависимостный batching через `batch_by_dependencies`. | `RuleBasedStrategy`. Версия 1.0.0. | `main.py`, тесты. | `test_rule_based.py`, `test_protected_vlans.py`, `test_orchestration.py`; ~95%. |
| `src/strategies/orchestrator.py` | Ленивая приоритетная диспетчеризация: primary (Threshold) сначала, secondary (RuleBased) только если primary пуст. | `decide_with_priority`. | `main.py`, тесты. | `test_orchestration.py`; 100%. |
| `src/strategies/__init__.py` | Пустой пакетный маркер. | — | пакет. | 100%. |

### A.6. Сегментаторы

| Путь | Назначение | Ключевые сущности | Импортёры | Тесты / покрытие |
|---|---|---|---|---|
| `src/segmenters/interface.py` | Контракт сегментатора + общая инфра. | `Segmentation = dict[int,int]`, `SegmentationResult`, `ConstraintSet` (с `validate()`), `Segmenter` (Protocol), `build_passthrough_result`. | три сегментатора, `strategies/threshold.py`, `task_builder/builder.py` (через тип `SegmentationResult`), тесты. | ~92%. |
| `src/segmenters/greedy.py` | Жадный Kernighan-Lin merge/split. `delta_merge`, `delta_split`; passthrough при нарушении constraints. | `GreedySegmenter` (`get_name="greedy"`, `get_complexity="O(n^2 log n)"`). | пакет, `main.py`, тесты. | `test_segmenters.py`, `test_algorithms_comparison.py`, `test_merge_loop_regression.py`; ~94%. |
| `src/segmenters/spectral.py` | Спектральное разбиение по нормированному лапласиану + KMeans над собственными векторами. | `SpectralSegmenter`. | пакет, `main.py`, тесты. | `test_segmenters.py`, `test_algorithms_comparison.py`; ~71%. |
| `src/segmenters/simulated_annealing.py` | Имитация отжига с геометрическим охлаждением. | `SimulatedAnnealingSegmenter`. | пакет, `main.py`, тесты. | `test_segmenters.py`, `test_algorithms_comparison.py`; ~89%. |
| `src/segmenters/__init__.py` | Реэкспорт интерфейса и трёх классов. | — | `main.py`, тесты. | 100%. |
| `src/segmenters/init.py` (3 строки) | Реэкспорт `ConstraintSet`, `Segmenter`. Дубликат `__init__.py`. | реэкспорт. | **нет импортёров.** | 0% — кандидат на DEAD_OR_UNUSED. |

### A.7. Граф трафика

| Путь | Назначение | Ключевые сущности | Импортёры | Тесты / покрытие |
|---|---|---|---|---|
| `src/network/model.py` (99 строк) | Граф трафика для сегментаторов: ребра между устройствами по `bytes_per_sec` из метрик, индекс device→vlan, evaluate(F) для целевой функции F(S). | `NetworkModel(state, metrics)`, методы `get_intra_segment_traffic`, `get_inter_segment_traffic`, `get_segment_size`, `evaluate`, `get_anomalous_vlans`. | все три сегментатора, `strategies/threshold.py`, `tests/*`. | `test_network_model.py`, `test_segmenters.py`, `test_merge_loop_regression.py`, `test_algorithms_comparison.py`; ~74%. |
| `src/network/__init__.py` | Реэкспорт `NetworkModel`. | — | пакет. | 100%. |

### A.8. Сборка задач

| Путь | Назначение | Ключевые сущности | Импортёры | Тесты / покрытие |
|---|---|---|---|---|
| `src/task_builder/builder.py` (569 строк, самый большой файл) | Преобразование `StrategyDecision` в `ReconfigurationTask`: расширение действий с учётом топологии (`expand_add_vlan`, `expand_delete_vlan`, `edit_trunk_remove`, `convert_port_to_access`), фолбэк к legacy-однодействиям при `topology is None`, защита protected-VLAN, дедупликация прерогативов, склейка с `batch_by_dependencies`, обработка ISOLATE/REBALANCE/MERGE/QUARANTINE. | `TaskBuilder`, методы `expand_*`, `build`. | `main.py`, тесты. | `test_task_builder.py`, `test_task_builder_scenarios.py`, `test_vlan_expansion.py`, `test_targeting.py`, `test_subinterface.py`, `test_edit_trunk.py`, `test_capability_and_deps.py`, `test_protected_vlans.py`; ~90%. |
| `src/task_builder/dependencies.py` (206 строк) | Модель зависимостей действий + batching под параллелизм CM (`max_parallel_batches=4`). Внедрение и dedup `CREATE_SUBINTERFACE` (один на VLAN), построение DiGraph по правилам ADD_VLAN→provision / CREATE_SUBINTERFACE→EDIT_TRUNK / evac→EDIT_TRUNK(-V)→DELETE_SUBINTERFACE→DELETE_VLAN, расщепление на weakly-connected components → батч на каждый, топологическая сортировка с `_PHASE_RANK`. | `batch_by_dependencies`, `_PHASE_RANK`, вспомогательные `_added_removed`, `_provisions`, `_removes`, `_inject_subinterface_prereqs`, `_peer_device`. | `src/task_builder/builder.py`, `src/strategies/rule_based.py`. | `test_capability_and_deps.py`; ~93%. |
| `src/task_builder/__init__.py` | Реэкспорт `TaskBuilder`. | — | пакет. | 100%. |

### A.9. Внешние интеграции

| Путь | Назначение | Ключевые сущности | Импортёры | Тесты / покрытие |
|---|---|---|---|---|
| `src/kafka/producer.py` | `aiokafka.AIOKafkaProducer`: подключение с ретраями (10×10с), публикация bootstrap-маркера при старте, метод `publish(task)` сериализует `ReconfigurationTask` в JSON. | `TaskProducer`, константы `MAX_RETRIES=10`, `RETRY_DELAY_SEC=10`. | `main.py`. | `test_kafka_producer.py`; ~80%. |
| `src/kafka/__init__.py` | Пакетный маркер. | — | пакет. | 100%. |
| `src/victoriametrics/client.py` (227 строк) | HTTP-клиент VictoriaMetrics: формирование запросов PromQL `query`/`query_range`, агрегация результатов по VLAN, нормализация рядов в строки метрик с полями `utilization`, `bytes_per_sec`, `inter_vlan_ratio`, `max_flow_bytes`, `icmp_per_sec`, `active_src_ips`, `anomaly_score` (z-score по истории), `vlan_id`. | `main.py`, `src/metrics_poller.py`, тесты. | `test_victoriametrics_client.py`, `test_vlan_resolution.py`; ~93%. |
| `src/victoriametrics/__init__.py` | Реэкспорт клиента. | — | пакет. | 100%. |
| `src/victoriametrics/ip_utils.py` (42 строки) | `decode_netflow_ip`, `find_vlan_by_ip`. | две функции. | **нет импортёров** вне самого файла. | 0% — кандидат на DEAD_OR_UNUSED. |
| `src/metrics_poller.py` (28 строк) | Async-цикл опроса VM с интервалом `metrics_poll_interval_sec`; передаёт батч в `handler`. | `run_metrics_poller`. | `main.py`. | косвенно; ~44%. |

### A.10. Триггеры

| Путь | Назначение | Ключевые сущности | Импортёры | Тесты / покрытие |
|---|---|---|---|---|
| `src/triggers/triggers.py` (76 строк) | Три простых триггера для решения «стоит ли запускать стратегию в этом цикле». | `Trigger` (Protocol), `ThresholdTrigger` (utilization/inter_ratio/max_flow), `PeriodicTrigger`, `AnomalyTrigger` (anomaly_score / icmp / new sources). | `main.py`. | `test_new_triggers.py`; ~80%. |
| `src/triggers/__init__.py` | Пакетный маркер. | — | пакет. | 100%. |

### A.11. Параметры конфигурации (использование)

Все поля `Settings` из `src/config.py`. Колонка «уп.» — число обращений `settings.<field>` в `src/` и `main.py` (не в тестах, не в самом `config.py`).

| поле | по умолч. | уп. | где используется |
|---|---|---|---|
| `kafka_bootstrap` | `"kafka:9092"` | 1 | `src/kafka/producer.py` |
| `kafka_tasks_topic` | `"reconfig.tasks"` | 1 | `src/kafka/producer.py` |
| `kafka_results_topic` | `"results.reconfig"` | **0** | **нигде** → DEAD_OR_UNUSED |
| `kafka_bootstrap_marker_enabled` | `True` | 2 | `src/kafka/producer.py` |
| `victoriametrics_url` | `"http://victoriametrics:8428"` | 2 | `main.py`, `src/victoriametrics/client.py` |
| `metrics_poll_interval_sec` | `60` | 1 | `src/metrics_poller.py` |
| `dedup_waiting_timeout_sec` | `600` | 1 | `main.py` (WAITING-таймаут) |
| `vlan_resolution_method` | `"auto"` | **0** | **нигде** → DEAD_OR_UNUSED |
| `netflow_src_encoding` | `"int"` | 1 | `src/victoriametrics/ip_utils.py` — но сам файл orphaned ⇒ эффективно DEAD |
| `postgres_dsn` | `"postgresql+asyncpg://…"` | 3 | `src/db/network_state.py`, `src/db/sync_gate.py`, `main.py` |
| `anomaly_threshold` | `3.0` | 2 | `main.py`, `src/strategies/threshold.py` |
| `bandwidth_threshold` | `0.85` | 2 | `main.py`, `src/strategies/threshold.py`, `src/triggers/triggers.py` |
| `trigger_change_percent` | `0.2` | 1 | `main.py` (создание `ThresholdTrigger`) |
| `periodic_interval_seconds` | `60` | 1 | `main.py` (создание `PeriodicTrigger`) |
| `segmenter_type` | `"greedy"` | 2 | `main.py` (`_build_segmenter`) |
| `sa_initial_temperature` | `100.0` | 1 | `main.py` |
| `sa_cooling_rate` | `0.95` | 1 | `main.py` |
| `sa_max_iterations` | `1000` | 1 | `main.py` |
| `greedy_max_iterations` | `100` | 2 | `main.py` |
| `lambda_coeff` | `0.5` | 2 | `main.py` |
| `quarantine_vlan` | `999` | 1 | `src/task_builder/builder.py` |
| `fallback_vlan_id` | `1` | 1 | `src/task_builder/builder.py` |
| `link_capacity_mbps` | `1000` | 1 | `src/victoriametrics/client.py` |
| `icmp_threshold` | `1000.0` | 4 | `threshold.py`, `triggers.py` |
| `new_sources_threshold` | `10` | 3 | `threshold.py`, `triggers.py` |
| `inter_vlan_ratio_threshold` | `0.4` | 4 | `threshold.py`, `triggers.py` |
| `max_flow_bytes_threshold` | `1e8` | 4 | `threshold.py`, `triggers.py` |
| `merge_min_inter_vlan_bytes_per_sec` | `1_000_000` | 1 | `src/strategies/threshold.py` (защита от MERGE-петли) |
| `subinterface_parent` | `"GigabitEthernet0/0/1"` | 2 | `src/task_builder/builder.py`, `src/topology/graph.py` |
| `subinterface_subnet_template` | `"192.168.{vlan_id}.1/24"` | 2 | `src/task_builder/builder.py`, `src/topology/graph.py` |
| `subinterface_enabled` | `True` | 4 | `src/task_builder/builder.py`, `src/topology/graph.py` |
| `wait_for_initial_sync` | `True` | 2 | `src/db/sync_gate.py` |
| `sync_wait_timeout_seconds` | `120` | 1 | `src/db/sync_gate.py` |
| `sync_poll_interval_seconds` | `5` | 1 | `src/db/sync_gate.py` |
| `sync_max_age_seconds` | `0` | 1 | `src/db/sync_gate.py` |
| `sync_wait_timeout_behavior` | `"proceed"` | 1 | `src/db/sync_gate.py` |
| `log_level` | `"INFO"` | 1 | `main.py` (logging.basicConfig) |

### A.12. Тесты (одна строка на файл)

| Файл | Что проверяет |
|---|---|
| `tests/conftest.py` | Фикстуры `mock_network_state`, `single_vlan_state` (минимальные NetworkState для unit-тестов). |
| `tests/test_algorithms_comparison.py` | Сравнительный прогон трёх сегментаторов на малой/средней/большой топологиях; запись `comparison_results.json`. По форме тест, по сути — бенчмарк. |
| `tests/test_capability_and_deps.py` | Per-interface capability gate (L2_SWITCHPORT vs L3_ROUTED), модель зависимостей действий, batching по weakly-connected-компонентам, dedup `CREATE_SUBINTERFACE`. |
| `tests/test_edit_trunk.py` | Разделение `SET_TRUNK` (ACCESS→TRUNK) и `EDIT_TRUNK` (изменение allowed-list уже-трунка). |
| `tests/test_kafka_producer.py` | Ретраи `TaskProducer.start()` при `KafkaConnectionError` и сдача после `MAX_RETRIES`. |
| `tests/test_merge_loop_regression.py` | Регрессия: жадный MERGE-loop с почти нулевым межсегментным трафиком; абсолютный пол подавляет петлю. |
| `tests/test_network_model.py` | `NetworkModel`: построение графа, intra/inter трафик, evaluate(F), `get_anomalous_vlans`. |
| `tests/test_new_strategy.py` | Ранние правила `ThresholdHeuristic` (ISOLATE/REBALANCE/MERGE). |
| `tests/test_new_triggers.py` | `AnomalyTrigger`, `ThresholdTrigger`, `PeriodicTrigger`. |
| `tests/test_orchestration.py` | Жёсткий приоритет: Threshold выигрывает у RuleBased; secondary вычисляется лениво. |
| `tests/test_protected_vlans.py` | Защищённые VLAN во всех четырёх слоях: load, RuleBased, Threshold-MERGE, TaskBuilder. |
| `tests/test_rule_based.py` | Group 1 (trunk-симметрия) и group 2 (минимальность Штейнера). |
| `tests/test_segmenters.py` | Все три сегментатора: контракт + сходимость на простых графах. |
| `tests/test_subinterface.py` | dot1q-сабинтерфейсы на роутере: `CREATE_SUBINTERFACE`/`DELETE_SUBINTERFACE`. |
| `tests/test_sync_gate.py` | Стартовый sync-gate: гонка старта, NOTIFY, поллинг-fallback, FAILED, freshness, таймаут, disabled (23 теста). |
| `tests/test_targeting.py` | TaskBuilder: отдельные батчи на устройство для ISOLATE с `segmentation_result`. |
| `tests/test_task_builder.py` | Базовые сценарии сборки `ReconfigurationTask`. |
| `tests/test_task_builder_scenarios.py` | Расширенные сценарии TaskBuilder, включая интеграцию с `_classify_batch_statuses` из `main.py`. |
| `tests/test_threshold_strategy.py` | `ThresholdHeuristic` целиком, версия 3.x. |
| `tests/test_topology_loader.py` | `load_topology()`: построение графа, режимы, allowed, эндпойнты, fallback при ошибке/пустых линках. |
| `tests/test_victoriametrics_client.py` | Парсинг ответа PromQL, агрегация по VLAN, z-score. |
| `tests/test_vlan_expansion.py` | `expand_add_vlan`/`expand_delete_vlan` с topology-aware расширением по пути. |
| `tests/test_vlan_resolution.py` | Резолвинг `vlan_id` из тегов VictoriaMetrics. |
| `tests/comparison_results.json` | Выгрузка из `test_algorithms_comparison.py`: objective, время, итерации, нарушения по трём топологиям. Это данные эксперимента, не тест. |

### A.13. Инфраструктура и артефакты репозитория

| Путь | Назначение |
|---|---|
| `Dockerfile` | python:3.11-slim, `pip install .`, `CMD ["python","main.py"]`. |
| `docker-compose.dev.yml` | Локальный стенд: DE + Kafka (KRaft) + postgres:16-alpine + victoriametrics. Передаёт ENV-параметры в DE. |
| `pyproject.toml` | Зависимости (`aiokafka`, `asyncpg`, `pydantic`, `pydantic-settings`, `networkx`, `scipy`, `scikit-learn`, `aiohttp`, `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`), `asyncio_mode="auto"`, `--cov-fail-under=70`, ruff/mypy конфиги. |
| `run_pipeline.bat` | Windows-скрипт для подъёма docker-compose, ожидания Kafka, создания топика. Локальная утилита. |
| `README.md` | Описание Data Flow, FSM IDLE/WAITING, схемы БД, перечня переменных окружения, инструкции. |
| `docs/algorithms.md` | Теоретическое введение: формальная задача F(S), NP-трудность, обзор Greedy/Spectral/SA с формулами и ссылками (Kernighan-Lin 1970, Shi-Malik 2000, Ng-Jordan-Weiss 2002, Kirkpatrick 1983). Содержит таблицы со ссылкой «см. JSON». |
| `scripts/mock_metrics_writer.py` | Локальный генератор NetFlow-метрик в VictoriaMetrics для разработки/демо. |
| `configs/` | Пустой каталог. |
| `0` | Файл нулевого размера в корне. Артефакт оболочки. |
| `src/decision_engine.egg-info/` | Метаданные packaging (`PKG-INFO`, `SOURCES.txt`, …), генерируется `setuptools`. Не исходник. |
| `.coverage`, `.pytest_cache/`, `__pycache__/`, `.idea/`, `.venv/` | Артефакты сборки и IDE. |

---

## B. Классификация и кандидаты на исключение

### B.1. Таблица «компонент → категория → обоснование»

| Компонент | Категория | Обоснование |
|---|---|---|
| `main.py` (FSM IDLE/WAITING, async-задачи) | **REALIZATION** | Конкретная склейка модулей DE: пул, продюсер, два слушателя, дедуп, политика таймаута WAITING — это инженерное воплощение, не общая архитектура. |
| `src/config.py` (поля, алиасы ENV) | **REALIZATION** | Перечень параметров и где они используются — факт реализации. Обоснование значений (почему `merge_min_inter_vlan_bytes_per_sec=1e6` и т.п.) → DESIGN. |
| `src/models/network.py`, `src/models/operations.py` | **REALIZATION** | Конкретные Pydantic-классы и enum-ы — реализация контрактов. Сам факт «контракт `ReconfigurationTask` стандартизован между DE и CM» → DESIGN. |
| `src/db/network_state.py` (SQL, JOIN'ы, fallback'и) | **REALIZATION** | Конкретные SQL-запросы и сборка `NetworkState`. Решение «PostgreSQL — источник истины» → DESIGN. |
| `src/db/sync_gate.py` (LISTEN-first, поллинг-fallback) | **REALIZATION** | Конкретный алгоритм ожидания с обработкой гонки — реализация. Архитектурное решение «DE ждёт CM перед первым циклом» → DESIGN. |
| `src/topology/graph.py` (`Topology`, `required_links`, `trunk_actions_for_link`, capability gate) | **REALIZATION** | Сам код графа, Steiner-сборки путей и фабрик действий. Идея «использовать Steiner-tree как канонический источник правды для allowed-VLAN» → ANALYSIS (теоретическое обоснование) + DESIGN (выбор подхода). |
| `src/strategies/interface.py` (`StrategyDecision`, `NetworkContext`, Protocol) | **REALIZATION** | Конкретный Protocol и dataclasses. |
| `src/strategies/threshold.py` (правила, флоор, protected) | **REALIZATION** | Конкретные ветви ISOLATE/REBALANCE/MERGE и пороги. Выбор пороговой эвристики vs ML → DESIGN/ANALYSIS. |
| `src/strategies/rule_based.py` (инварианты 1 и 2) | **REALIZATION** | Реконсиляция per-interface — это конкретная реализация. Формулировка инвариантов 802.1Q-симметрии и least-privilege → ANALYSIS. |
| `src/strategies/orchestrator.py` (`decide_with_priority`) | **REALIZATION** | Конкретный приоритетный диспетчер. |
| `src/segmenters/interface.py` (`Segmenter` Protocol, `ConstraintSet`, `SegmentationResult`) | **REALIZATION** | Контракт и валидатор ограничений. |
| `src/segmenters/greedy.py` | **REALIZATION** | Код алгоритма. Формулы `delta_merge`/`delta_split` и ссылка на Kernighan-Lin → ANALYSIS. |
| `src/segmenters/spectral.py` | **REALIZATION** | Код алгоритма (нормированный лапласиан → KMeans). NCut и теорема Фидлера → ANALYSIS. |
| `src/segmenters/simulated_annealing.py` | **REALIZATION** | Код алгоритма. Сходимость по Kirkpatrick → ANALYSIS. |
| `src/network/model.py` (`NetworkModel`, evaluate(F)) | **REALIZATION** | Конкретная реализация графа трафика и F(S). Сама F(S) и её NP-трудность → ANALYSIS. |
| `src/task_builder/builder.py` (expand_add/delete, edit_trunk_remove, fallback, protected guards) | **REALIZATION** | Самый объёмный модуль; вся механика конвертации решений в задачи — это реализация. |
| `src/task_builder/dependencies.py` (DiGraph действий, WCC-батчинг, dedup) | **REALIZATION** | Конкретная модель зависимостей и batching под параллелизм CM. |
| `src/kafka/producer.py` (ретраи, bootstrap-маркер) | **REALIZATION** | Код продюсера. Решение «Kafka как шина» → DESIGN. |
| `src/victoriametrics/client.py` (PromQL, агрегация, z-score) | **REALIZATION** | Конкретные запросы и преобразования. Решение «VictoriaMetrics как TSDB» → DESIGN. Объяснение PromQL/NetFlow → ANALYSIS. |
| `src/metrics_poller.py` | **REALIZATION** | Простой async-цикл опроса. Лучше как подраздел главы об интеграциях. |
| `src/triggers/triggers.py` | **REALIZATION** | Три простых триггера. Лучше как подраздел. |
| `docs/algorithms.md` | **ANALYSIS** + ссылка на **EXPERIMENT** | Постановка задачи F(S), NP-трудность, обзор Greedy/Spectral/SA с формулами и литературой — это анализ. Финальная таблица сравнения — отсылка к экспериментальной части. |
| `tests/test_*.py` (за исключением `test_algorithms_comparison.py`) | **TESTING** | Unit-тесты слоёв; описывают методологию и покрытие. |
| `tests/test_algorithms_comparison.py` + `tests/comparison_results.json` | **EXPERIMENT** | Тест-обёртка для бенчмарка; ассерты слабые, основной результат — JSON-выгрузка по objective/времени/итерациям/нарушениям на трёх топологиях. Это экспериментальная оценка алгоритмов. |
| `Dockerfile`, `docker-compose.dev.yml`, `pyproject.toml`, `run_pipeline.bat` | **REALIZATION** (короткий подраздел «развёртывание») или **DESIGN** | По выбору автора. В ВКР бакалавра обычно — короткий пункт в «Реализации». Не отдельная глава. |
| `README.md` | материал распределён по всем разделам ПЗ | Содержит и архитектурные решения (FSM, источники), и реализацию (схемы БД, перечни ENV). Использовать как сводный источник для соответствующих глав. |
| `scripts/mock_metrics_writer.py` | **OUT_OF_SCOPE_FOR_DE** (инструмент локальной разработки) или **TESTING** (в роли стимула) | Не входит в исполняемый функционал DE. Упомянуть однострочно в «Тестировании» как генератор синтетических метрик. |
| Контракт `ReconfigurationTask` (DE→CM), канал NOTIFY `reconfiguration_task_status_changed`, чтение `topology_sync_status` (CM пишет) | **DESIGN** | Граница между DE и CM. Описать в проектировании; в «Реализации» сослаться. |
| FSM DE (IDLE/WAITING), приоритетная диспетчеризация стратегий, выбор PostgreSQL/Kafka/VictoriaMetrics | **DESIGN** | Архитектурные решения. В «Реализации» — только их воплощение. |
| Свойства алгоритмов (NP-трудность, сходимость, complexity) | **ANALYSIS** | Теория. В «Реализации» — конкретный код. |

### B.2. Кандидаты на удаление / не описание (DEAD_OR_UNUSED)

Перечислены сущности, по которым `grep` не находит ни одного импортёра / использования. В ПЗ их описывать не следует. Рекомендую также убрать из репозитория перед сдачей.

| Объект | Причина |
|---|---|
| `src/segmenters/init.py` (3 строки) | Дубликат `__init__.py`. Никто не импортирует `from src.segmenters.init …`. Покрытие тестами 0%. Артефакт. |
| `src/task_builder/topology.py` (back-compat шим, 30 строк) | Все импорты идут напрямую из `src.topology.graph`. Покрытие 0%. Шим без потребителей. |
| `src/victoriametrics/ip_utils.py` (42 строки) | Функции `decode_netflow_ip`, `find_vlan_by_ip` нигде не вызываются. Покрытие 0%. Использует `settings.netflow_src_encoding` — настройку, которая вне этого файла тоже не нужна. |
| Параметр `kafka_results_topic` (default `"results.reconfig"`) | Ни одного `settings.kafka_results_topic` в коде. DE не читает результирующий топик (для WAITING он использует `reconfiguration_task_status` в PostgreSQL и LISTEN/NOTIFY). |
| Параметр `vlan_resolution_method` (default `"auto"`) | Ни одного использования. |
| Параметр `netflow_src_encoding` | Используется только в `ip_utils.py`, который сам никем не вызывается. Эффективно DEAD. |
| Функция `get_mock_network_state()` в `src/db/network_state.py` | Реэкспортируется из `src/db/__init__.py`, но не вызывается ни в `src/`, ни в `main.py`, ни в тестах (тесты используют фикстуру `mock_network_state` из `conftest.py`). |
| Файл `0` в корне (0 байт) | Артефакт оболочки (вероятно опечатка вида `> 0`). |
| Каталог `configs/` (пустой) | Пустой плейсхолдер. |
| `src/decision_engine.egg-info/` | Артефакт `setuptools`. Не код, в репозитории храниться не должен (попадает в `.gitignore`-обычно). |

«Требует уточнения у автора» — нет: всё перечисленное однозначно не используется.

### B.3. Материал, который правильнее отнести в другие разделы ВКР

| Материал в коде/доках | Куда в ПЗ | Почему не «Реализация» |
|---|---|---|
| Постановка задачи F(S), регуляризация λ/μ, NP-трудность (см. `docs/algorithms.md`) | **Анализ предметной области** | Это теоретическая основа; в «Реализации» — только итоговая `evaluate()` в `NetworkModel`. |
| Обзор Greedy / Spectral / SA с формулами `delta_merge`/`delta_split`, нормированным лапласианом, законом охлаждения, ссылками на Kernighan-Lin / Shi-Malik / Ng-Jordan-Weiss / Kirkpatrick | **Анализ** | Это обзор методов из литературы. В «Реализации» — конкретный код классов. |
| Свойства алгоритмов: «локальный оптимум», «глобальный оптимум при логарифмическом охлаждении», complexity `O(n^2 log n)` | **Анализ** | Свойства методов. Реализация эту complexity не доказывает, а имеет её как следствие. |
| Group-инварианты 1/2 для trunk-фабрики (802.1Q-симметрия как необходимое условие форвардинга; Steiner-минимальность как least-privilege) — формулировки в docstring `src/strategies/rule_based.py` | **Анализ** или **Проектирование** | Это объяснение, ПОЧЕМУ реконсиляция корректна. В «Реализации» остаётся, КАК она реализована (per-interface desired vs actual). |
| Выбор: PostgreSQL как источник истины, Kafka как шина, VictoriaMetrics как TSDB | **Проектирование** | Архитектурные решения. В «Реализации» — конкретные клиенты/запросы. |
| Контракт DE↔CM: `ReconfigurationTask` (поля, типы действий, критичность батчей), `reconfiguration_task_status` (DE читает, CM пишет), NOTIFY-канал, `topology_sync_status` (CM пишет, DE читает) | **Проектирование** | Это межмодульный интерфейс. В «Реализации» DE — сошлись на главу проектирования и опишите код чтения/публикации. |
| FSM IDLE/WAITING, политики дедупликации, политика таймаута WAITING | **Проектирование** (схема) + **Реализация** (код) | Картинка состояний и стрелок — в проектирование; код `_waiting_monitor_loop` — в реализацию. |
| Жёсткий приоритет Threshold над RuleBased; ленивая secondary | **Проектирование** | Это решение об оркестрации. Реализация (`decide_with_priority`) тривиальна. |
| `tests/test_algorithms_comparison.py` + `tests/comparison_results.json` (objective/время/итерации/нарушения для small/medium/large) | **Экспериментальная часть** | Это сравнительная количественная оценка трёх алгоритмов; в «Реализации» им места нет. |
| Состав unit-тестов, покрытие по модулям, общий процент (87% в последнем прогоне), use cases (защищённые VLAN, merge-loop регрессия, sync-gate) | **Тестирование** | Это методология верификации, отдельный раздел ПЗ. |
| `scripts/mock_metrics_writer.py` | **Тестирование** (как генератор стимулов) | Не входит в исполняемый DE. |
| Стенд (R1/S1/S2, IOS, VLAN 10/20/90/998 как protected) | **Тестирование** или **Экспериментальная часть** | Это описание тестовой инфраструктуры, а не реализации DE. |
| `Dockerfile`, `docker-compose.dev.yml`, `run_pipeline.bat` | **Реализация** (краткий подраздел «развёртывание»), либо **Тестирование** | Не оправдывает отдельной главы; короткая ссылка достаточно. |

---

## C. Предложенная структура глав раздела «Реализация»

Структура выведена из доминирующего объёма и связности кода. Никаких глав «по умолчанию»: каждая глава имеет 100+ строк реализационного кода и/или содержит ключевые инженерные решения, без которых картина DE неполна. Указанная оценка объёма — для академической вёрстки (страница ≈ 1800 знаков с пробелами).

### Глава 1. Точка входа и каркас цикла принятия решений

- **Покрытие модулей:** `main.py`, `src/config.py`, `src/metrics_poller.py`, `src/triggers/triggers.py`.
- **Содержание:** инициализация (asyncpg-пул, Kafka-продюсер, VM-клиент, выбор сегментатора по `segmenter_type`, две стратегии, три триггера); конечный автомат IDLE↔WAITING (`_set_waiting`, `_reset_to_idle`, `_waiting_monitor_loop`, `_status_listener_loop`); функция-обработчик `handle_metrics_batch` (триггеры → стратегия → диспетчер → TaskBuilder → publish → WAITING); классификация терминальных статусов батчей (`_classify_batch_statuses`); функция `_evaluate_timeout_result`. Перечень фактически используемых параметров `Settings` и их роли.
- **Почему «Реализация»:** это конкретная склейка компонентов в работающий процесс. Архитектурное решение «DE — поллер с дедупликацией WAITING» относится к проектированию; здесь — его код.
- **Объём:** 5–7 страниц.
- **Приоритет:** обязательная. Без неё непонятно, где и в какой момент вызываются последующие главы.

### Глава 2. Слой данных: модели и доступ к PostgreSQL

- **Покрытие модулей:** `src/models/network.py`, `src/models/operations.py`, `src/db/network_state.py`, `src/db/__init__.py`.
- **Содержание:** Pydantic-модели инвентаря и операций (включая `ActionType` с восемью значениями, `BatchCriticality`, `protected_vlans`); SQL-запросы; сборка `NetworkState` через JOIN'ы; загрузка топологии (`load_topology`) с graceful-fallback на legacy single-actions; чтение `reconfiguration_task_status` для WAITING; служебные читатели роутерных сабинтерфейсов и `protected_vlans`.
- **Почему «Реализация»:** конкретные SQL-запросы, индексирование колонок (`is_protected`, `parent_interface_id`, `dot1q_vlan_id`, `ip_address`), pydantic-модели и обработка ошибок чтения — всё это инженерное воплощение. Решение «PostgreSQL как источник истины» уже принято в проектировании.
- **Объём:** 6–8 страниц.
- **Приоритет:** обязательная. Любая следующая глава либо читает `NetworkState`, либо читает `Topology`.

### Глава 3. Структурная модель сети: модуль `topology`

- **Покрытие модулей:** `src/topology/graph.py`, `src/topology/__init__.py`.
- **Содержание:** датакласс `Topology` (граф `networkx`, индексы `_peer`, `_by_device`, `_children_by_parent`, `device_vlan_members`, `endpoints`, `protected_vlans`); `Topology.build(...)` и его аргументы; функция `required_links` (Steiner-tree как канонический источник правды о том, какие линки нужны VLAN'у); per-interface capability gate (`interface_kind`, различающее L2_SWITCHPORT / L3_ROUTED); фабрики действий (`build_trunk_action`, `set_trunk_action`, `create_subinterface_action`, `delete_subinterface_action`); генератор действий по линку (`trunk_actions_for_link`) с protected-guards; вспомогательные предикаты (`is_link_required`, `vlans_blocking_trunk_to_access`).
- **Почему «Реализация»:** 448 строк собственного кода, центральная модель для двух следующих глав. Обоснование Steiner-tree как канонического источника правды уходит в «Анализ» / «Проектирование»; здесь — реализация.
- **Объём:** 6–8 страниц (значительная часть — на `trunk_actions_for_link` и `required_links`).
- **Приоритет:** обязательная.

### Глава 4. Стратегии принятия решений

- **Покрытие модулей:** `src/strategies/interface.py`, `src/strategies/threshold.py`, `src/strategies/rule_based.py`, `src/strategies/orchestrator.py`.
- **Содержание:**
  - контракт стратегии (`IDecisionStrategy`, `NetworkContext`, `StrategyDecision` с тремя полями `decisions`/`segmentation_result`/`batches`, `VlanDecision`);
  - `ThresholdHeuristic` (версия 3.1.0): ветви ISOLATE (`anomaly`, `icmp`, `new_sources`), REBALANCE, MERGE; абсолютный пол `merge_min_inter_vlan_bytes_per_sec` как защита от MERGE-петли; skip MERGE для защищённых VLAN;
  - `RuleBasedStrategy` (версия 1.0.0): реконсиляция per-interface `actual` vs `desired = ∪ {V: link∈required_links(V)}`; разделение на `symmetry_actions` (NORMAL) и `prune_actions` (OPTIONAL); зависимостный batching;
  - `decide_with_priority`: жёсткий приоритет, ленивая secondary (sync или async).
- **Почему «Реализация»:** конкретные правила, пороги, имена решений (ISOLATE / REBALANCE / MERGE / TRUNK_SYNC / TRUNK_PRUNE). Сама идея «приоритет Threshold над RuleBased» относится к проектированию.
- **Объём:** 7–9 страниц.
- **Приоритет:** обязательная.

### Глава 5. Алгоритмы сегментации: реализация

- **Покрытие модулей:** `src/network/model.py`, `src/segmenters/interface.py`, `src/segmenters/greedy.py`, `src/segmenters/spectral.py`, `src/segmenters/simulated_annealing.py`.
- **Содержание:** `NetworkModel` (граф трафика по метрикам, `evaluate(F)`, `get_anomalous_vlans`); контракт `Segmenter` (Protocol, `optimize`, `get_name`, `get_complexity`); `ConstraintSet` и его `validate()`; `build_passthrough_result`; реализация трёх классов: `GreedySegmenter` (KL-style local search), `SpectralSegmenter` (нормированный лапласиан + KMeans), `SimulatedAnnealingSegmenter` (геометрическое охлаждение); выбор сегментатора через `_build_segmenter` (это уже в Главе 1).
- **Почему «Реализация»:** код алгоритмов и валидатора ограничений. Теоретические основы (формулы, NP-трудность, ссылки на литературу) — в «Анализ». Сравнительная оценка — в «Экспериментальную часть».
- **Объём:** 6–8 страниц.
- **Приоритет:** обязательная.

### Глава 6. Сборка реконфигурационных задач: `task_builder`

- **Покрытие модулей:** `src/task_builder/builder.py`, `src/task_builder/dependencies.py`, `src/task_builder/__init__.py`.
- **Содержание:**
  - `TaskBuilder.build()` как маршрутизатор по типам решений;
  - topology-aware расширение действий: `expand_add_vlan`, `expand_delete_vlan`, `edit_trunk_remove`, `convert_port_to_access`;
  - fallback к legacy single-actions при `topology is None` (обоснование graceful degradation);
  - protected-VLAN-guards (`vlan.is_protected`);
  - dedup ключ `(device_id, port | parent_interface, action_type)`;
  - модель зависимостей действий (`batch_by_dependencies`): правила ADD_VLAN→provision, CREATE_SUBINTERFACE→EDIT_TRUNK, зеркальные правила для удаления; внедрение и dedup `CREATE_SUBINTERFACE` (один на VLAN); расщепление на weakly-connected-компоненты — батч на компонент; топологическая сортировка с tie-break по `_PHASE_RANK`;
  - batching под `max_parallel_batches=4` в CM (зависимые действия — в одном батче, независимые — в разных).
- **Почему «Реализация»:** самый большой модуль проекта (569+206 = ~775 строк), центральная инженерная сложность. Архитектурное «делать ли расширение в DE или в CM» уже решено в проектировании.
- **Объём:** 8–10 страниц.
- **Приоритет:** обязательная.

### Глава 7. Стартовая синхронизация: `sync_gate`

- **Покрытие модулей:** `src/db/sync_gate.py`, `get_sync_state` в `src/db/network_state.py`, параметры `WAIT_FOR_INITIAL_SYNC`, `SYNC_WAIT_TIMEOUT_SECONDS`, `SYNC_POLL_INTERVAL_SECONDS`, `SYNC_MAX_AGE_SECONDS`, `SYNC_WAIT_TIMEOUT_BEHAVIOR`.
- **Содержание:** обработка гонки старта (LISTEN-first → чтение `topology_sync_status` → цикл); три источника возобновления (NOTIFY-канал `topology_sync_changed`, поллинг как race-free fallback, начальное чтение); маппинг статусов; политика свежести по `last_full_sync_at`; политики таймаута `proceed`/`keep_waiting`; деградация при недоступном LISTEN.
- **Почему «Реализация»:** ~210 строк собственного кода, 23 теста, чётко выраженный сценарий. Решение «DE ждёт CM перед первым циклом» — это проектирование; здесь — код.
- **Объём:** 3–4 страницы.
- **Приоритет:** желательная. Если автор предпочитает компактнее — материал органично сливается с главой 2 (тогда у главы 2 появится подраздел «Стартовая синхронизация»).

### Глава 8. Интеграции с шиной и источником метрик

- **Покрытие модулей:** `src/kafka/producer.py`, `src/victoriametrics/client.py`, `src/victoriametrics/__init__.py`.
- **Содержание:** `TaskProducer` (ретраи `MAX_RETRIES=10`, bootstrap-маркер, сериализация `model_dump(mode="json")`); `VictoriaMetricsClient` (PromQL `query`/`query_range`, агрегация по VLAN, z-score `anomaly_score` по истории, вычисление `utilization` через `link_capacity_mbps`, нормализация в `bytes_per_sec`/`inter_vlan_ratio`/`max_flow_bytes`/`icmp_per_sec`/`active_src_ips`).
- **Почему «Реализация»:** конкретные клиенты и форматы; выбор протоколов — в проектировании.
- **Объём:** 4–5 страниц.
- **Приоритет:** обязательная (без неё непонятно, ЧТО DE читает и КУДА публикует).

### Глава 9. Развёртывание (короткая)

- **Покрытие модулей:** `Dockerfile`, `docker-compose.dev.yml`, `pyproject.toml`, `run_pipeline.bat`, `scripts/mock_metrics_writer.py` (упоминание).
- **Содержание:** одностраничное описание контейнеризации, зависимостей и локального стенда.
- **Объём:** 1–2 страницы.
- **Приоритет:** опциональная. Если в ПЗ есть отдельный раздел «Развёртывание / Эксплуатация», материал уйдёт туда.

### C.1. Альтернативные варианты группировки

**Вариант α (как выше, 8 содержательных глав + 1 короткая).** Плюсы: каждая глава имеет цельный нарратив и опирается на 1–4 модуля; объёмы сбалансированы. Минусы: глава 7 (sync_gate) короткая.

**Вариант β (объединяем главы 2 и 7).** Глава 2 «Слой данных: модели, доступ к PostgreSQL, стартовая синхронизация». Плюсы: один блок «всё про PostgreSQL»; sync_gate не висит отдельной короткой главой. Минусы: sync_gate — это отдельный сюжет (LISTEN/NOTIFY + гонка старта + поллинг-fallback), который размывается в data-главе. Рекомендую только если по объёму глава 7 не дотянет до 2,5 страниц.

**Вариант γ (объединяем главы 4 и 5: стратегии + сегментаторы в одну главу).** Плюсы: единый сюжет «как DE принимает решения». Минусы: сегментатор используется только одной из стратегий (`ThresholdHeuristic` при REBALANCE); материал разнородный по уровню абстракции (правила vs алгоритмы оптимизации); объём вырастает до 13–17 страниц — слишком много для одной главы бакалаврской ВКР. **Не рекомендую.**

**Вариант δ (разделяем главу 6 надвое: «6a. TaskBuilder» + «6b. Зависимости действий и batching»).** Плюсы: 6b — самостоятельная инженерная глава про DAG зависимостей и WCC. Минусы: они тесно связаны (TaskBuilder вызывает `batch_by_dependencies` в каждом методе расширения); раздельное описание ведёт к дублированию контекста. Применить можно, если объём по 6 превысит 12 страниц.

### C.2. Замечания и риски

- Глава 7 (sync_gate) может выйти на грани отдельной главы по объёму. Решение принять по факту черновика. Слияние с главой 2 — приемлемый запасной вариант.
- В главах 4 и 5 неизбежны короткие отсылки в «Анализ» («формальная задача F(S) и обоснование сводимости к graph cut приведены в §X.Y»). Следить, чтобы реализационная глава не пересказывала теорию.
- Глава 9 (развёртывание) — на усмотрение. Если у вас уже есть раздел «Эксплуатация» / «Внедрение» в ПЗ, главу 9 не делать.
- Любые попытки описать `src/segmenters/init.py`, `src/task_builder/topology.py`, `src/victoriametrics/ip_utils.py` или параметры `kafka_results_topic` / `vlan_resolution_method` / `netflow_src_encoding` в «Реализации» — не нужны: это мёртвый код / неиспользуемые параметры (см. B.2).
- Файл `0` в корне и каталог `configs/` — артефакты, не упоминать.
- Контракт DE↔CM (`ReconfigurationTask`, `reconfiguration_task_status`, `topology_sync_status`, NOTIFY-каналы) — описывать в проектировании; в реализации только сослаться.
- Сравнительный анализ алгоритмов (`tests/test_algorithms_comparison.py`, `tests/comparison_results.json`) — экспериментальная часть, не реализация.

---

## D. Приложение: дерево файлов с однострочными описаниями

```
decision-engine/
├── 0                                       # АРТЕФАКТ оболочки, 0 байт, удалить
├── Dockerfile                              # python:3.11-slim → pip install . → python main.py
├── README.md                               # Data Flow, FSM, схема БД, ENV-параметры
├── docker-compose.dev.yml                  # Локальный стенд: DE + Kafka + Postgres + VictoriaMetrics
├── main.py                                 # Точка входа; FSM IDLE↔WAITING; три async-задачи
├── pyproject.toml                          # Зависимости, ruff/mypy/pytest конфиги
├── run_pipeline.bat                        # Windows-скрипт подъёма compose + создания топика
├── configs/                                # ПУСТО, плейсхолдер
├── docs/
│   └── algorithms.md                       # ТЕОРИЯ: F(S), NP-трудность, обзор Greedy/Spectral/SA
├── scripts/
│   └── mock_metrics_writer.py              # Локальный генератор NetFlow-метрик
├── src/
│   ├── __init__.py                         # Пакетный маркер
│   ├── config.py                           # pydantic-settings Settings + singleton `settings`
│   ├── db/
│   │   ├── __init__.py                     # Реэкспорт create_pool/get_network_state и др.
│   │   ├── network_state.py                # SQL-чтения, NetworkState, load_topology, get_sync_state
│   │   └── sync_gate.py                    # Стартовое ожидание первой синхры (LISTEN+poll)
│   ├── kafka/
│   │   ├── __init__.py                     # Реэкспорт TaskProducer
│   │   └── producer.py                     # AIOKafkaProducer: ретраи, bootstrap-маркер, publish
│   ├── metrics_poller.py                   # Async-цикл опроса VM
│   ├── models/
│   │   ├── __init__.py
│   │   ├── network.py                      # Device, VlanInfo (is_protected), VlanAssignment, NetworkState
│   │   └── operations.py                   # ActionType×8, Action/Batch/ReconfigurationTask, статусы
│   ├── network/
│   │   ├── __init__.py                     # Реэкспорт NetworkModel
│   │   └── model.py                        # Граф трафика, evaluate(F), get_anomalous_vlans
│   ├── segmenters/
│   │   ├── __init__.py                     # Реэкспорт интерфейса и трёх классов
│   │   ├── init.py                         # МЁРТВЫЙ ДУБЛИКАТ __init__.py — удалить
│   │   ├── interface.py                    # Segmenter Protocol, ConstraintSet.validate, SegmentationResult
│   │   ├── greedy.py                       # KL-style merge/split, O(n² log n)
│   │   ├── spectral.py                     # Нормированный лапласиан + KMeans
│   │   └── simulated_annealing.py          # Геометрическое охлаждение
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── interface.py                    # IDecisionStrategy, NetworkContext, StrategyDecision
│   │   ├── orchestrator.py                 # decide_with_priority (ленивая secondary)
│   │   ├── rule_based.py                   # Group 1 trunk-симметрия + group 2 минимальность
│   │   └── threshold.py                    # ISOLATE/REBALANCE/MERGE + merge-loop floor + protected skip
│   ├── task_builder/
│   │   ├── __init__.py                     # Реэкспорт TaskBuilder
│   │   ├── builder.py                      # expand_add/delete_vlan, edit_trunk_remove, build()
│   │   ├── dependencies.py                 # batch_by_dependencies: DAG действий, WCC-батчинг, dedup
│   │   └── topology.py                     # ШИМ без потребителей — удалить
│   ├── topology/
│   │   ├── __init__.py                     # Реэкспорт публичных имён
│   │   └── graph.py                        # Topology, required_links, trunk_actions_for_link, capability
│   ├── triggers/
│   │   ├── __init__.py
│   │   └── triggers.py                     # AnomalyTrigger, ThresholdTrigger, PeriodicTrigger
│   └── victoriametrics/
│       ├── __init__.py                     # Реэкспорт VictoriaMetricsClient
│       ├── client.py                       # PromQL, агрегация по VLAN, z-score anomaly_score
│       └── ip_utils.py                     # МЁРТВЫЙ: функции без потребителей — удалить
└── tests/
    ├── conftest.py                         # Фикстуры mock_network_state / single_vlan_state
    ├── comparison_results.json             # ДАННЫЕ эксперимента (генерируются test_algorithms_comparison)
    ├── test_algorithms_comparison.py       # ЭКСПЕРИМЕНТ: бенчмарк трёх сегментаторов на small/medium/large
    ├── test_capability_and_deps.py         # Capability gate, DAG зависимостей, WCC, dedup
    ├── test_edit_trunk.py                  # Разделение SET_TRUNK и EDIT_TRUNK
    ├── test_kafka_producer.py              # Ретраи и сдача TaskProducer.start
    ├── test_merge_loop_regression.py       # Регрессия MERGE-петли
    ├── test_network_model.py               # NetworkModel: граф, intra/inter, evaluate
    ├── test_new_strategy.py                # Ранние правила ThresholdHeuristic
    ├── test_new_triggers.py                # Три триггера
    ├── test_orchestration.py               # Приоритет Threshold над RuleBased
    ├── test_protected_vlans.py             # Protected VLAN в 4 слоях
    ├── test_rule_based.py                  # Group 1 и group 2
    ├── test_segmenters.py                  # Сходимость трёх сегментаторов
    ├── test_subinterface.py                # dot1q-сабинтерфейсы на роутере
    ├── test_sync_gate.py                   # Стартовый sync-gate (23 теста)
    ├── test_targeting.py                   # Per-device батчи для ISOLATE
    ├── test_task_builder.py                # Базовые сценарии TaskBuilder
    ├── test_task_builder_scenarios.py      # Расширенные сценарии, _classify_batch_statuses
    ├── test_threshold_strategy.py          # ThresholdHeuristic целиком
    ├── test_topology_loader.py             # load_topology + fallback
    ├── test_victoriametrics_client.py      # PromQL парсинг + агрегация
    ├── test_vlan_expansion.py              # expand_add/delete_vlan
    └── test_vlan_resolution.py             # vlan_id из тегов VM
```

Кандидаты на удаление из репозитория, не упоминать в ПЗ: файл `0`, каталог `configs/`, `src/segmenters/init.py`, `src/task_builder/topology.py`, `src/victoriametrics/ip_utils.py`, поля `kafka_results_topic`, `vlan_resolution_method`, `netflow_src_encoding`, `src/db/network_state.get_mock_network_state` (если использование не появится), каталог `src/decision_engine.egg-info/`.
