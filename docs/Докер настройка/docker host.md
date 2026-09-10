# Проверка версии docker
```bash
docker --version
```

## Создание рабочей директории
На диске "C" создаём папку "docker-system".
В папке "docker-system" создаём файл "docker-compose.yml":
```text
services:
  backend:
    image: mrdoka/network-admin-panel-backend:latest
    container_name: admin-panel-backend
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
      kafka:
        condition: service_started
    environment:
      SPRING_DATASOURCE_URL: jdbc:postgresql://postgres:5432/admin_panel
      SPRING_DATASOURCE_USERNAME: postgres
      SPRING_DATASOURCE_PASSWORD: postgres
      SPRING_KAFKA_BOOTSTRAP_SERVERS: kafka:9092
      KAFKA_TASKS_TOPIC: ${KAFKA_TASKS_TOPIC:-reconfig.tasks}
      APP_INSTANCE_ID: ${APP_INSTANCE_ID:-admin-panel}
      CORS_ALLOWED_ORIGINS: ${CORS_ALLOWED_ORIGINS:-http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173}
    ports:
      - "8080:8080"

  frontend:
    image: mrdoka/network-admin-panel-frontend:latest
    container_name: admin-panel-frontend
    restart: unless-stopped
    depends_on:
      - backend
    ports:
      - "3000:80"

  postgres:
    image: postgres:16-alpine
    container_name: admin-panel-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: admin_panel
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d admin_panel"]
      interval: 5s
      timeout: 5s
      retries: 5

  victoria-metrics:
    image: victoriametrics/victoria-metrics:v1.106.0
    container_name: admin-panel-victoria-metrics
    restart: unless-stopped
    command:
      - "-storageDataPath=/victoria-metrics-data"
      - "-httpListenAddr=:8428"
    ports:
      - "8428:8428"
    volumes:
      - victoria-metrics_data:/victoria-metrics-data

  kafka:
    image: confluentinc/cp-kafka:7.6.0
    container_name: admin-panel-kafka
    restart: unless-stopped
    environment:
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_NODE_ID: 1
      KAFKA_LISTENERS: PLAINTEXT://0.0.0.0:9092,CONTROLLER://0.0.0.0:9093
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092
      KAFKA_INTER_BROKER_LISTENER_NAME: PLAINTEXT
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "true"
      CLUSTER_ID: "MkU3OEVBNTcwNTJENDM2Qk"
    ports:
      - "9092:9092"

  decision-engine:
    image: tem4ik4545/decision-engine:latest
    container_name: admin-panel-decision-engine
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
      kafka:
        condition: service_started
      victoria-metrics:
        condition: service_started
    environment:
      KAFKA_BOOTSTRAP: kafka:9092
      KAFKA_TASKS_TOPIC: reconfig.tasks
      KAFKA_BOOTSTRAP_MARKER_ENABLED: "false"
      POSTGRES_DSN: postgresql+asyncpg://postgres:postgres@postgres/admin_panel
      VICTORIA_METRICS_URL: http://victoria-metrics:8428
      METRICS_POLL_INTERVAL_SEC: "60"
      DEDUP_WAITING_TIMEOUT_SEC: "600"
      VLAN_RESOLUTION_METHOD: auto
      ANOMALY_THRESHOLD: "3.0"
      BANDWIDTH_THRESHOLD: "0.85"
      QUARANTINE_VLAN: "999"
      LINK_CAPACITY_MBPS: "100"
      ICMP_THRESHOLD: "1000.0"
      NEW_SOURCES_THRESHOLD: "10"
      INTER_VLAN_RATIO_THRESHOLD: "0.4"
      MAX_FLOW_BYTES_THRESHOLD: "1000000000"
      SUBINTERFACE_PARENT: "GigabitEthernet0/0/1"
      SUBINTERFACE_SUBNET_TEMPLATE: "192.168.{vlan_id}.1/24"
      SUBINTERFACE_ENABLED: "true"
      SEGMENTER_TYPE: greedy
      LOG_LEVEL: DEBUG

  netflow-collector:
    image: scotix/avlan-netflow-collector:latest
    container_name: avlan-netflow-collector
    depends_on:
      - victoria-metrics
    environment:
      VM_URL: http://victoria-metrics:8428
      NETFLOW_LISTEN_HOST: 0.0.0.0
      NETFLOW_LISTEN_PORT: "2055"
      TEMPLATE_TTL_SEC: "3600"
      LOG_LEVEL: DEBUG
    ports:
      - "2055:2055/udp"
    restart: unless-stopped

  config-manager:
    image: thmsshn/config_manager:latest
    container_name: admin-panel-config-manager
    restart: unless-stopped
    depends_on:
      - kafka
      - postgres
    environment:
      APP_NAME: config-manager
      APP_VERSION: 0.1.0
      DEBUG: "true"
      LOG_LEVEL: DEBUG
      DATABASE_URL: postgresql+psycopg2://postgres:postgres@postgres/admin_panel
      DB_ECHO: "false"
      DB_POOL_PRE_PING: "true"
      KAFKA_ENABLED: "true"
      KAFKA_BOOTSTRAP_SERVERS: kafka:9092
      KAFKA_REQUEST_TOPIC: reconfig.tasks
      KAFKA_GROUP_ID: config-manager
      CONNECTOR_TYPE: NETMIKO
      QUEUE_ENABLED: "false"
      QUEUE_URL: ""
      APPROVAL_REQUIRED: "true"
      AUTO_EXECUTE_TASKS: "true"
      DEFAULT_ROLLBACK_ON_FAILURE: "true"
      EXECUTION_TIMEOUT_SECONDS: "60"
      MAX_PARALLEL_BATCHES: "4"
      CHECKPOINTS_DIR: data/checkpoints
      SSH_DEFAULT_USERNAME: admin
      SSH_DEFAULT_PASSWORD: adminpass
      SSH_ENABLE_SECRET: enablepass
      SSH_DEVICE_TYPE: cisco_ios
      SSH_CONNECT_TIMEOUT_SECONDS: "10"
      SSH_GLOBAL_DELAY_FACTOR: "1"
      SSH_SAVE_CONFIG: "false"

volumes:
  postgres_data:
  victoria-metrics_data:
```

После этого:
```bash
cd C:\docker-system
```

Скачиваем контейнеры:
```bash
docker pull tem4ik4545/decision-engine:latest
docker pull scotix/avlan-netflow-collector:latest
docker pull thmsshn/config_manager:latest
docker pull mrdoka/network-admin-panel-backend:latest
docker pull mrdoka/network-admin-panel-frontend:latest
docker pull postgres:16-alpine
docker pull victoriametrics/victoria-metrics:v1.106.0
docker pull confluentinc/cp-kafka:7.6.0
```

Запустить\остановить:
```bash
# Остановить контейнеры
docker-compose down

# Запустить контейнеры
docker-compose up -d

# Статус контейнеров
docker-compose ps
```

### Проверка в браузере
```text
# Админ-панель (frontend)
http://localhost:3000
login: admin
password: 1234

# Victoria Metrics
http://localhost:8428

# Админ-панель (backend)
http://localhost:8080/swagger-ui/index.html
```
